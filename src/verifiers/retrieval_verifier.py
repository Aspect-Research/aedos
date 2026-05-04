"""Retrieval verifier (v0.3 — slots-aware multi-attempt query strategy).

Per the v0.2 dogfooding traces, query construction was the main retrieval
failure mode. v0.3 changes:

- Queries come from the PATTERN's ``query_strategy`` list, not from a
  per-predicate template. Slots fill in the placeholders.
- The verifier tries each attempt in order. The first attempt with ≥ 2
  results is used. Failed/empty attempts continue.
- Each attempt is cached independently so retries are cheap.
- We never inject "current" into a query — temporal scope comes from the
  slots, not from query string manipulation. The judge prompt asks
  current-vs-historical using the slot values.
- Each attempt is logged as a ``retrieval_query_attempt`` pipeline_event
  so the trace UI shows the strategy.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from src.fact_store import FactStore
from src.llm_client import LLMClient
from src.pattern_registry import PatternRegistry, Pattern
from src.verifiers.comparative import (
    ComparativeClaim,
    comparative_queries,
    detect_comparative,
)
from src.verifiers.types import VerificationOutcome, VerificationResult


_REQUEST_TIMEOUT = 10.0
_TOP_N = 3
_MIN_RESULTS_TO_USE = 2  # spec: "If a query returns ≥ 2 results, use those"
_DEFAULT_TTL_HOURS = 24
# Retry budget when the judge returns INSUFFICIENT_EVIDENCE on the
# first viable attempt's snippets. Originally added in v0.7.9 for
# comparative claims only; v0.12.x extends the same retry-walk to
# ALL claims because the medical/encyclopedic-but-fuzzy cases that
# now route to retrieval (post-Phase-1) often need a second query
# phrasing to land relevant snippets. Bounded so a pathological
# case can't run away with LLM cost.
_MAX_JUDGE_RETRIES = 3


# Phase 2b (v0.12.x): reformulation prompt. Fired ONCE per claim
# after the pattern's static query-strategy list has been exhausted
# AND the judge cleanly returned INSUFFICIENT_EVIDENCE on at least
# one attempt. The judge's justification tells us WHY the snippets
# weren't enough; the reformulator targets the specific gap.
#
# Uses the cache_classify-tier model (gpt-4.1-nano in the default
# routing) because the task is narrow and the cost matters — this
# fires on every retrieval-bound claim that the static strategies
# can't settle, so it's a per-turn cost amplifier if it goes to a
# big model.
_REFORMULATE_SYSTEM = """You rewrite search queries.

A previous Wikipedia search for a factual claim came back with snippets that didn't settle the question. The judge said WHY (its justification line). Your job: write ONE new Wikipedia search query that targets the SPECIFIC fact the judge said was missing.

Rules:
  - Output ONLY the query string — no quotes, no explanation, no preamble.
  - Keep it short (2-8 words). Wikipedia ranks better on focused queries than long ones.
  - Don't repeat queries that were already tried (you'll see them in the prompt).
  - Target the specific entity, relationship, number, date, or definition the judge said was absent.
  - If the judge said "snippets describe X and Y separately without explicitly stating the relationship", search for the relationship itself ("X is a type of Y", "X causes Y").
  - If the judge said "the snippets are about a different time period", add the relevant year or era to the query.
  - If the judge said "no comparison context across other entities", search for a list/ranking page ("list of X by Y", "comparison of X").

Reply with the new query and nothing else."""


def _format_reformulate_user_message(
    claim: dict, last_verdict: "JudgeVerdict",
    tried_queries: list[str],
) -> str:
    slots = claim.get("slots") or {}
    slot_lines = "\n".join(f"  {k}: {v!r}" for k, v in slots.items())
    tried_block = "\n".join(f"  - {q!r}" for q in tried_queries) or "  (none)"
    return (
        f"Claim:\n"
        f"  pattern: {claim.get('pattern')!r}\n"
        f"  predicate: {claim.get('predicate')!r}\n"
        f"  slots:\n{slot_lines}\n"
        f"  source_text: {claim.get('source_text', '')!r}\n\n"
        f"Queries already tried:\n{tried_block}\n\n"
        f"Last judge verdict: {last_verdict.verdict}\n"
        f"Last judge justification: {last_verdict.justification}\n\n"
        "Write ONE new search query targeting the specific gap. "
        "Reply with the query string only."
    )


@dataclass
class Snippet:
    title: str
    snippet: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "snippet": self.snippet, "url": self.url}


@dataclass
class JudgeVerdict:
    verdict: str
    justification: str
    # v0.7.13: judge's reported conviction in [0, 1]. Multiplied into
    # the path prior to compute the final Decision.confidence — a
    # judge that hedges ("0.6") shouldn't produce the same downstream
    # confidence as one that's certain ("0.97"). Defaults to 1.0 so
    # legacy responses without a Confidence: line still produce a
    # full-strength verdict (no behavior change for unupdated mocks).
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "justification": self.justification,
            "confidence": self.confidence,
        }


@dataclass
class QueryAttempt:
    query: str
    result_count: int
    used: bool
    from_cache: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "result_count": self.result_count,
            "used": self.used,
            "from_cache": self.from_cache,
            "error": self.error,
        }


@dataclass
class RetrievalResult:
    """Returned by RetrievalVerifier.verify().

    Carries enough metadata to render a full debugging view: every query
    attempt, the snippets used, the judge's verdict and justification,
    and the temporal scope used by the judge.
    """

    outcome: VerificationOutcome
    attempts: list[QueryAttempt] = field(default_factory=list)
    snippets: list[Snippet] = field(default_factory=list)
    verdict: Optional[JudgeVerdict] = None
    error_flag: Optional[str] = None
    explanation: str = ""
    actual_value: Any | None = None
    historical: bool = False  # True if judge used the historical-claim prompt

    @property
    def from_cache(self) -> bool:
        for a in self.attempts:
            if a.used:
                return a.from_cache
        return False

    @property
    def verified(self) -> bool:
        return self.outcome is VerificationOutcome.VERIFIED

    @property
    def contradicted(self) -> bool:
        return self.outcome is VerificationOutcome.CONTRADICTED

    @property
    def inconclusive(self) -> bool:
        return self.outcome is VerificationOutcome.INCONCLUSIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "attempts": [a.to_dict() for a in self.attempts],
            "from_cache": self.from_cache,
            "snippets": [s.to_dict() for s in self.snippets],
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "error_flag": self.error_flag,
            "explanation": self.explanation,
            "actual_value": self.actual_value,
            "historical": self.historical,
        }


# ---- search providers (Wikipedia-only, post-v0.7.15) -----------------


def default_search(query: str) -> list[Snippet]:
    """Wikipedia via the MediaWiki API. Free, no key, no meaningful
    rate limit, and the highest-quality factual source for the bulk
    of AEDOS's queries (biographical / historical / definitional).

    Errors return []. Empty results return []. The verifier's
    multi-attempt query strategy then walks to the next template,
    and the comparative-claim path (v0.7.9) prepends ranking-page
    queries before the standard ones for superlative claims.

    History: pre-v0.7.15 there were three additional providers
    (Tavily, SerpAPI, DuckDuckGo) as paid + scraped fallbacks. They
    were removed because Wikipedia covers the corpus AEDOS targets,
    the paid providers added a key-management burden, and the DDG
    scrape was unreliable enough to be net-negative.
    """
    try:
        from src.verifiers.scrapers import search_wikipedia
        return search_wikipedia(query) or []
    except Exception:
        return []


# ---- judge — current vs historical ----------------------------------

_JUDGE_SYSTEM_CURRENT = """You are a strict, evidence-bounded judge.

You receive a structured CURRENT-TENSE claim, the original source text
it was extracted from, and a small set of search-result snippets.
Decide whether the snippets SUPPORT, CONTRADICT, or are
INSUFFICIENT_EVIDENCE for the claim. Use only the snippets — never your
prior knowledge.

CRITICAL — respect the speaker's tense in source_text. The structured
slots strip tense; the source text preserves it.

  * Past tense in source text ("X was Y", "X were Y", "X had Y",
    "X used to be Y", "was founded in YEAR", "encompassed", "served
    as", "formerly", "previously", "originally") → the claim is a
    HISTORICAL assertion. Verify whether the snippets confirm the
    fact EVER held. The present-day status of the entity is
    IRRELEVANT — a true historical fact stays SUPPORTED even when
    the entity no longer exists today. Example: source text says
    "the Soviet Union was a communist superpower" — snippets that
    confirm the USSR was a communist superpower (even ones noting
    its 1991 dissolution) → SUPPORTED, NOT contradicted.

  * Present tense in source text ("X is Y", "X has Y", "X currently
    Y") with no explicit time period → verify whether the snippets
    confirm the fact is CURRENTLY true.

  * Mixed / ambiguous source text → favor the interpretation that
    best fits the slots. If the entity is widely known to be
    historical (no longer exists / dissolved / deceased), default to
    the historical interpretation.

A claim is SUPPORTED only if the snippets clearly state or directly
imply it under the appropriate tense interpretation. CONTRADICTED
only if they clearly state the opposite (under the same tense).
Otherwise INSUFFICIENT_EVIDENCE.

The dissolution / death / end-of-existence of an entity does NOT
contradict a past-tense claim about that entity. It only contradicts
present-tense claims.

COMPARATIVE / SUPERLATIVE claims ("X had the most Y", "X is the
heaviest Y", "X is the first Z to do W"): a snippet that lists the
measure across multiple entities (e.g. a "list of Y by country" or
"Y ranking" article) is sufficient evidence to judge — read whether
X is at the named extreme of that list. A snippet about only X
without comparison context against other entities is
INSUFFICIENT_EVIDENCE for the comparative dimension. The comparative
phrasing in source_text is the signal — a structured slot with
``relation: had_heaviest_X`` or source text like "the most/heaviest
/largest of any" should activate this rule.

Output exactly THREE lines, no preamble:
VERDICT
Justification: <one sentence>
Confidence: <number 0.0-1.0>

Confidence reflects YOUR conviction in the verdict given the
snippets — 1.0 = the snippets state the claim verbatim, 0.5 = the
snippets imply it but indirectly, 0.3 = you're unsure. Use the
full range; do not default to 1.0."""

_JUDGE_SYSTEM_HISTORICAL = """You are a strict, evidence-bounded judge.

You receive a structured HISTORICAL claim with an explicit time period
(valid_from / valid_until), the original source text, and a small set
of search-result snippets. Decide whether the snippets SUPPORT,
CONTRADICT, or are INSUFFICIENT_EVIDENCE for the claim FOR THAT
SPECIFIC PERIOD.

Pay attention to dates. A snippet describing a different time period
is NOT support — it's INSUFFICIENT_EVIDENCE. A snippet stating a
different time-bounded fact is CONTRADICTION only if it directly
conflicts with the claim's period.

The dissolution / death / end-of-existence of the entity AFTER the
claim's period does NOT contradict the claim — the claim is bounded
to its period.

Output exactly THREE lines, no preamble:
VERDICT
Justification: <one sentence>
Confidence: <number 0.0-1.0>

Confidence reflects YOUR conviction in the verdict given the
snippets for the stated period. Use the full range — 1.0 only when
the snippets state the claim verbatim for that period."""


# Map of accepted verdict tokens (after upper + strip) → canonical label.
# The judge prompt asks for SUPPORTED / CONTRADICTED / INSUFFICIENT_EVIDENCE,
# but real LLM output abbreviates ('SUPPORT', 'CONTRADICT', 'INCONCLUSIVE').
# Accepting the abbreviated forms turns the dogfood-observed
# 'judge_parse_error' on Tokyo→Edo into the SUPPORT verdict the judge
# clearly intended. Canonical labels stay unchanged downstream.
_JUDGE_VERDICT_ALIASES = {
    "SUPPORTED": "SUPPORTED",
    "SUPPORT": "SUPPORTED",
    "SUPPORTS": "SUPPORTED",
    "CONTRADICTED": "CONTRADICTED",
    "CONTRADICT": "CONTRADICTED",
    "CONTRADICTS": "CONTRADICTED",
    "INSUFFICIENT_EVIDENCE": "INSUFFICIENT_EVIDENCE",
    "INSUFFICIENT": "INSUFFICIENT_EVIDENCE",
    "INCONCLUSIVE": "INSUFFICIENT_EVIDENCE",
    "UNCLEAR": "INSUFFICIENT_EVIDENCE",
}


def parse_judge_response(text: str) -> JudgeVerdict | None:
    """Tolerant verdict parser.

    The judge prompt asks for "VERDICT \\n Justification: ..." but real
    Claude output is messy:

      * ``**SUPPORTED**`` — markdown bolds around the verdict
      * ``Verdict: SUPPORTED`` — labeled prefix instead of bare verdict
      * ``## SUPPORTED`` — markdown heading
      * ``Based on the snippets, the verdict is SUPPORTED.`` — preamble
      * ``The claim is NOT SUPPORTED.`` — negation flips the meaning

    Strategy:

      1. Search the first 600 chars of the response for any aliased
         verdict token, matched as a whole word (case-insensitive).
      2. If the verdict is preceded immediately by ``not`` / ``no``
         (within ~5 chars), flip the canonical label:
         not-SUPPORTED → CONTRADICTED, not-CONTRADICTED → SUPPORTED.
      3. The earliest-occurring (post-flip) verdict wins.
      4. Justification = everything from the verdict onward, with
         the verdict word removed and "Justification:" / markdown
         stripped.

    Returns None only when no aliased verdict word appears at all.
    """
    if not text:
        return None
    head = text[:600]

    candidates: list[tuple[int, str]] = []  # (position, canonical_label)
    for token, label in _JUDGE_VERDICT_ALIASES.items():
        for m in re.finditer(
            rf"\b{re.escape(token)}\b", head, flags=re.IGNORECASE,
        ):
            # Negation flip: "NOT SUPPORTED" / "no SUPPORTED" reads as
            # the opposite of the bare token. Look in the ~10 chars
            # before the match for a negation cue.
            preceding = head[max(0, m.start() - 10):m.start()].lower()
            negated = bool(re.search(r"\b(not|no)\s*$", preceding))
            actual = label
            if negated:
                if label == "SUPPORTED":
                    actual = "CONTRADICTED"
                elif label == "CONTRADICTED":
                    actual = "SUPPORTED"
                # INSUFFICIENT_EVIDENCE preceded by "not" stays
                # INSUFFICIENT — it's not a polar verdict.
            candidates.append((m.start(), actual))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    pos, canonical = candidates[0]

    # Justification: text after the verdict word.
    # Find the end of the matched word; everything after is the
    # justification (sans "Justification:" prefix and markdown).
    rest = text[pos:]
    # Drop the verdict word and any immediately-following bold/header
    # markers.
    rest = re.sub(
        r"^\s*\*{0,2}_?[A-Z_]+_?\*{0,2}\s*[:.,]?\s*",
        "",
        rest,
        count=1,
    )
    # Strip a "Justification:" lead.
    rest = re.sub(
        r"^\s*\**\s*[Jj]ustification\s*:?\s*\**\s*",
        "",
        rest,
        count=1,
    )
    # Drop residual markdown markers and surrounding whitespace.
    rest = rest.strip().strip("*_#>`-").strip()

    # v0.7.13: pull a `Confidence: <number>` line out of the rest, if
    # present. Defaults to 1.0 when missing — preserves behavior for
    # mocks/older judges that don't emit a confidence line. Strip the
    # confidence line from the justification so it doesn't leak into
    # the displayed text.
    confidence = 1.0
    conf_match = re.search(
        r"\b[Cc]onfidence\s*[:=]?\s*\**\s*([0-9]*\.?[0-9]+)\s*\**",
        rest,
    )
    if conf_match:
        try:
            confidence = max(0.0, min(1.0, float(conf_match.group(1))))
        except (TypeError, ValueError):
            confidence = 1.0
        # Remove the confidence sentence from the justification text.
        rest = re.sub(
            r"\s*\b[Cc]onfidence\s*[:=]?\s*\**\s*[0-9]*\.?[0-9]+\s*\**\s*\.?\s*",
            " ",
            rest,
        ).strip().strip("*_#>`-").strip()

    return JudgeVerdict(
        verdict=canonical,
        justification=rest or "(no justification)",
        confidence=confidence,
    )


def _is_historical(claim: dict) -> bool:
    """A claim is historical if its slots specify a valid_until."""
    slots = claim.get("slots") or {}
    return bool(slots.get("valid_until"))


def _format_judge_user_message(claim: dict, snippets: list[Snippet], historical: bool) -> str:
    slots = claim.get("slots") or {}
    polarity_word = "asserts" if int(claim.get("polarity", 1)) == 1 else "denies"
    source_text = (claim.get("source_text") or "").strip()
    snippets_block = "\n\n".join(
        f"[{i + 1}] {s.title}\n{s.snippet}\nSource: {s.url}"
        for i, s in enumerate(snippets)
    )

    slot_lines = "\n".join(f"  {k}: {v!r}" for k, v in slots.items())
    framing_parts: list[str] = []
    if source_text:
        # The source text is the speaker's literal phrasing — the only
        # place tense survives. The judge prompt instructs the model to
        # use this for tense interpretation.
        framing_parts.append(f'Original source text: "{source_text}"')
    if historical:
        period = f"{slots.get('valid_from') or 'unspecified'} to {slots.get('valid_until')}"
        framing_parts.append(f"Time period: {period}")
        framing_parts.append(
            f"The speaker {polarity_word} that this relation held during that period."
        )
    else:
        framing_parts.append(
            f"The speaker {polarity_word} this relation. Match the verdict to "
            "the tense used in the source text — past-tense source means "
            "verify the historical fact (snippets confirm it ever held); "
            "present-tense source means verify current truth."
        )
    framing = "\n".join(framing_parts)

    return (
        f"Claim:\n"
        f"  pattern: {claim.get('pattern')!r}\n"
        f"  predicate: {claim.get('predicate')!r}\n"
        f"  slots:\n{slot_lines}\n\n"
        f"{framing}\n\n"
        f"Snippets:\n{snippets_block}\n\n"
        "Respond with the required two-line format."
    )


# ---- query construction --------------------------------------------


_SLOT_REF_RE = re.compile(r"\{(\w+)\}")


def _slot_refs(template: str) -> list[str]:
    return _SLOT_REF_RE.findall(template)


def _enrich_slots(slots: dict[str, Any]) -> dict[str, Any]:
    """Add derived keys + natural-language conversion for query templates.

    Two transformations:

    1. ``participants_joined`` — for the event pattern's list slot.
    2. snake_case → space-separated for slots whose values are AEDOS-
       internal predicate/category identifiers (``relation``,
       ``property``, ``relation_kind``, ``event_type``). Without this,
       query templates emit garbage like "Donald Trump parent_of
       Donald Jr." or "presidential_campaign 2024" — search engines
       (and Wikipedia in particular) rank pages much better against
       "Donald Trump parent of Donald Jr." or "presidential campaign
       2024". The judge always sees the original slot values; this
       enrichment is query-only.
    """
    out = dict(slots)
    parts = slots.get("participants")
    if isinstance(parts, list):
        out["participants_joined"] = " ".join(str(p) for p in parts)
    for key in ("relation", "property", "relation_kind", "event_type",
                "predicate", "role"):
        val = slots.get(key)
        if isinstance(val, str) and "_" in val:
            out[key] = val.replace("_", " ")
    return out


def build_queries(pattern: Pattern, slots: dict[str, Any]) -> list[str]:
    """Return the ordered list of query attempts for these slots.

    Templates that reference missing slots are skipped silently; we'd
    rather skip an over-specified template than emit a query with empty
    placeholders.
    """
    enriched = _enrich_slots(slots)
    queries: list[str] = []
    for template in pattern.query_strategy:
        refs = _slot_refs(template)
        if not all(refs and r in enriched and str(enriched[r]).strip() for r in refs):
            continue
        # Spec: never prepend "current" — the temporal context comes from
        # slots. Defensive guard:
        assert "current" not in template.lower(), (
            f"query_strategy template {template!r} contains 'current'; "
            "remove it — temporal scope is determined by slots"
        )
        formatted = template.format_map(enriched).strip()
        formatted = " ".join(formatted.split())  # collapse whitespace
        if formatted and formatted not in queries:
            queries.append(formatted)
    return queries


# ---- verifier -------------------------------------------------------


class RetrievalVerifier:
    def __init__(
        self,
        store: FactStore,
        llm: LLMClient,
        registry: PatternRegistry,
        search_fn: Callable[[str], list[Snippet]] | None = None,
        ttl_hours: int | None = None,
    ):
        self.store = store
        self.llm = llm
        self.registry = registry
        self._search = search_fn or default_search
        if ttl_hours is None:
            ttl_hours = int(
                os.getenv("AEDOS_RETRIEVAL_CACHE_TTL_HOURS", str(_DEFAULT_TTL_HOURS))
            )
        self.ttl_seconds = max(0, ttl_hours) * 3600

    def verify(
        self, claim: dict, *, source_turn_id: int | None = None
    ) -> RetrievalResult:
        pattern = self.registry.get(claim["pattern"])
        slots = claim.get("slots") or {}
        historical = _is_historical(claim)

        # v0.7.9: detect comparative / superlative claims and prepend
        # comparative-aware query templates. Standard pattern queries
        # stay as fallback. When detection fires we also enable the
        # retry-on-inconclusive path so a first-attempt INSUFFICIENT
        # verdict can step through the rest of the queue rather than
        # giving up.
        comparative = detect_comparative(claim)
        if comparative is not None and source_turn_id is not None:
            try:
                self.store.insert_pipeline_event(
                    source_turn_id, "comparative_detected",
                    {
                        "claim": {
                            "pattern": claim.get("pattern"),
                            "predicate": claim.get("predicate"),
                            "source_text": claim.get("source_text"),
                        },
                        **comparative.to_dict(),
                    },
                )
            except Exception:
                pass

        std_queries = build_queries(pattern, slots)
        if comparative is not None:
            queries = comparative_queries(comparative) + [
                q for q in std_queries if q not in set(comparative_queries(comparative))
            ]
        else:
            queries = std_queries

        if not queries:
            return RetrievalResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                error_flag="no_query_constructible",
                explanation=(
                    f"could not construct any query for pattern {pattern.name!r} "
                    f"from slots {slots!r}"
                ),
                historical=historical,
            )

        attempts: list[QueryAttempt] = []
        last_inconclusive: RetrievalResult | None = None
        judge_calls = 0

        # Single lazy loop: walk queries in order, run the judge each
        # time a viable attempt lands, stop on a conclusive verdict.
        # Caps judging at _MAX_JUDGE_RETRIES so a pathologically
        # ambiguous claim doesn't burn the whole pattern's strategy
        # list at LLM-call cost. Non-viable attempts (< MIN_RESULTS)
        # don't consume the budget — they're free (cached or empty
        # search) and just feed the trace.
        for q in queries:
            attempt, sn = self._run_query(q, source_turn_id)
            attempts.append(attempt)

            if attempt.result_count < _MIN_RESULTS_TO_USE:
                continue

            attempt.used = True
            judge_calls += 1
            result = self._judge_one(claim, attempt, sn, attempts, historical)
            if result.outcome != VerificationOutcome.INCONCLUSIVE:
                return result
            # Prefer a "clean" inconclusive (judge ran, returned
            # INSUFFICIENT_EVIDENCE) over an error result (judge_error
            # / judge_parse_error). The downstream dispatcher maps
            # these to different statuses (retrieval_inconclusive vs
            # retrieval_failed) and a clean inconclusive carries more
            # information than a later crash. So once we have a
            # clean inconclusive we keep it; we only overwrite if
            # the previous tracked result was itself an error.
            if (
                last_inconclusive is None
                or (last_inconclusive.error_flag and not result.error_flag)
            ):
                last_inconclusive = result

            if judge_calls >= _MAX_JUDGE_RETRIES:
                break

            # Log the retry decision so the trace shows we tried again.
            if source_turn_id is not None:
                try:
                    self.store.insert_pipeline_event(
                        source_turn_id,
                        "judge_retry_after_inconclusive",
                        {
                            "tried_query": attempt.query,
                            "verdict": (result.verdict.verdict if result.verdict else None),
                            "judge_calls_so_far": judge_calls,
                        },
                    )
                except Exception:
                    pass

        # Phase 2b: LLM reformulation hop. Fires once when the static
        # strategy list exhausted with at least one CLEAN inconclusive
        # (judge ran, said insufficient — not a parse error or crash).
        # The reformulator targets the specific gap the judge named.
        if (
            last_inconclusive is not None
            and last_inconclusive.error_flag is None
            and last_inconclusive.verdict is not None
            and last_inconclusive.verdict.justification.strip()
        ):
            tried = [a.query for a in attempts]
            reformulated_q = self._reformulate_query(
                claim, last_inconclusive.verdict, tried, source_turn_id,
            )
            if reformulated_q and reformulated_q not in tried:
                ref_attempt, ref_snippets = self._run_query(
                    reformulated_q, source_turn_id,
                )
                attempts.append(ref_attempt)
                if ref_attempt.result_count >= _MIN_RESULTS_TO_USE:
                    ref_attempt.used = True
                    ref_result = self._judge_one(
                        claim, ref_attempt, ref_snippets, attempts, historical,
                    )
                    if ref_result.outcome != VerificationOutcome.INCONCLUSIVE:
                        return ref_result
                    # Reformulation also inconclusive — keep the cleaner
                    # of the two for the final verdict (same rule as
                    # the static-loop tracking).
                    if (
                        last_inconclusive.error_flag
                        and not ref_result.error_flag
                    ):
                        last_inconclusive = ref_result

        if last_inconclusive is not None:
            return last_inconclusive

        # No viable attempt landed. Distinguish search-side errors from
        # genuinely empty results so the trace shows the right flag.
        any_error = any(a.error for a in attempts)
        flag = "retrieval_error" if any_error else "no_results"
        err_summary = next((a.error for a in attempts if a.error), None)
        return RetrievalResult(
            outcome=VerificationOutcome.INCONCLUSIVE,
            attempts=attempts,
            error_flag=flag,
            explanation=(
                err_summary
                or f"all {len(attempts)} query attempt(s) returned < "
                f"{_MIN_RESULTS_TO_USE} results"
            ),
            historical=historical,
        )

    def _run_query(
        self, q: str, source_turn_id: int | None,
    ) -> tuple[QueryAttempt, list[Snippet]]:
        """Run a single query through the cache + search path. Returns
        the attempt record and the snippets. Logs the attempt.

        Used by both the static strategy loop and the Phase 2b
        reformulation hop so they share retrieval semantics."""
        cached = self.store.get_cached_retrieval(q, self.ttl_seconds)
        if cached is not None:
            sn = [Snippet(**s) for s in cached]
            attempt = QueryAttempt(
                query=q, result_count=len(sn), used=False, from_cache=True,
            )
        else:
            try:
                sn = list(self._search(q))
                attempt = QueryAttempt(
                    query=q, result_count=len(sn), used=False, from_cache=False,
                )
                self.store.cache_retrieval(q, [s.to_dict() for s in sn])
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                attempt = QueryAttempt(
                    query=q, result_count=0, used=False, from_cache=False,
                    error=f"{type(e).__name__}: {e}",
                )
                sn = []
            except Exception as e:
                attempt = QueryAttempt(
                    query=q, result_count=0, used=False, from_cache=False,
                    error=f"{type(e).__name__}: {e}",
                )
                sn = []
        self._log_attempt(source_turn_id, attempt)
        return attempt, sn

    def _reformulate_query(
        self, claim: dict, last_verdict: "JudgeVerdict",
        tried_queries: list[str], source_turn_id: int | None,
    ) -> str | None:
        """Ask a small model for ONE new search query targeting the
        specific gap the judge named. Returns the query string or
        None if the call failed / produced empty output / repeated a
        prior attempt. Best-effort: any exception falls back to None
        so verification still returns the prior inconclusive."""
        try:
            raw = self.llm.rewrite(
                _REFORMULATE_SYSTEM,
                _format_reformulate_user_message(
                    claim, last_verdict, tried_queries,
                ),
                purpose="cache_classify",
            )
        except Exception as e:
            if source_turn_id is not None:
                try:
                    self.store.insert_pipeline_event(
                        source_turn_id, "reformulation_failed",
                        {"error": f"{type(e).__name__}: {e}"},
                    )
                except Exception:
                    pass
            return None
        # Tolerate the model wrapping the query in quotes or backticks.
        candidate = (raw or "").strip().strip("`").strip('"').strip("'").strip()
        # Drop a leading "Query:" prefix some models add despite the
        # rules block.
        candidate = re.sub(r"^[Qq]uery\s*:\s*", "", candidate).strip()
        # Single-line: take the first non-empty line only.
        for line in candidate.splitlines():
            line = line.strip().strip("`").strip('"').strip("'").strip()
            if line:
                candidate = line
                break
        if not candidate:
            return None
        if source_turn_id is not None:
            try:
                self.store.insert_pipeline_event(
                    source_turn_id, "reformulation_emitted",
                    {
                        "reformulated_query": candidate,
                        "tried_queries": tried_queries,
                        "judge_justification": last_verdict.justification,
                    },
                )
            except Exception:
                pass
        return candidate

    def _judge_one(
        self, claim: dict, attempt: QueryAttempt,
        chosen_snippets: list[Snippet], attempts: list[QueryAttempt],
        historical: bool,
    ) -> RetrievalResult:
        """Run the judge against one attempt's snippets. Extracted so
        the verify() loop can call it multiple times for the
        comparative retry-on-inconclusive path."""
        try:
            system = _JUDGE_SYSTEM_HISTORICAL if historical else _JUDGE_SYSTEM_CURRENT
            judge_text = self.llm.rewrite(
                system, _format_judge_user_message(claim, chosen_snippets, historical),
                purpose="retrieval_judge",
            )
        except Exception as e:
            return RetrievalResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                attempts=attempts,
                snippets=chosen_snippets,
                error_flag="judge_error",
                explanation=f"judge call failed: {type(e).__name__}: {e}",
                historical=historical,
            )

        verdict = parse_judge_response(judge_text)
        if verdict is None:
            return RetrievalResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                attempts=attempts,
                snippets=chosen_snippets,
                error_flag="judge_parse_error",
                explanation=f"judge returned malformed output: {judge_text!r}",
                historical=historical,
            )

        if verdict.verdict == "SUPPORTED":
            outcome = VerificationOutcome.VERIFIED
        elif verdict.verdict == "CONTRADICTED":
            outcome = VerificationOutcome.CONTRADICTED
        else:
            outcome = VerificationOutcome.INCONCLUSIVE

        return RetrievalResult(
            outcome=outcome,
            attempts=attempts,
            snippets=chosen_snippets,
            verdict=verdict,
            explanation=verdict.justification,
            historical=historical,
        )

    def _log_attempt(
        self,
        source_turn_id: int | None,
        attempt: QueryAttempt,
        *,
        is_decision: bool = False,
    ) -> None:
        if source_turn_id is None:
            return
        # We log twice in the "used" case: once when discovered, once when
        # marked used. Keep it simple — only emit on the discovery side.
        if is_decision:
            return
        try:
            self.store.insert_pipeline_event(
                source_turn_id, "retrieval_query_attempt", attempt.to_dict()
            )
        except Exception:
            # Logging must never crash verification.
            pass


