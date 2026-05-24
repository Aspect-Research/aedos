"""Phase H D47 — Wikipedia normalizer.

Resolves bare ambiguous entity references to canonical Wikipedia article
titles before the substrate sees them. Two-stage design:

  Stage 1 — deterministic Wikipedia-redirect resolution via the MediaWiki
            `action=query&redirects=1&prop=pageprops` API. Four outcomes:
            canonical_no_redirect | clean_redirect | disambiguation_page |
            not_found. A fifth (api_error) covers transient HTTP failures.

  Stage 2 — LLM-mediated selection over the disambiguation page's candidate
            links, biased to explicit abstention when context does not
            disambiguate. Implemented in step 2 of D47.

Stage 1 lives in this commit (D47 step 1). Stage 2 lands in the next
commit. The normalizer is wired into `EntityResolver.resolve` in step 3.

Audit log: every normalization produces an `entity_normalization` event
with the surface form, Stage 1 outcome, normalized form, Stage 2 details
when applicable, and timing. Verbose by design — Phase 10.5 post-hoc
analysis consumes these events.

Patterns deliberately mirror `kb_wikidata.py`:
  - HTTP layer via `CachingHTTPClient` (User-Agent + LRU + TTL from Config)
  - Rate limiter via `RateLimiter` (10/s default; well below MediaWiki's
    per-IP fairness budget)
  - `_cfg_value` defensive accessor so test paths can construct without a
    Config (the fall-through defaults match Config's defaults)
  - Audit logging best-effort: never let a logging failure break a
    normalization
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from ..audit.log import log_event
from ..utils.rate_limit import RateLimiter

# Stage 2: explicit abstention sentinel. The prompt instructs Haiku to
# emit this literal string when no candidate clearly matches; the parser
# treats it as "leave surface form unchanged."
STAGE_2_ABSTAIN = "ABSTAIN"

# Phase H Cluster 1 step 1 (2026-05-24): Wikidata Q-id pattern. The
# walker's D5 KB neighbor enumeration substitutes Q-id-keyed children
# into claims that then re-enter `EntityResolver.resolve`, which calls
# this normalizer. A Q-id IS already a canonical KB identifier — sending
# it through Wikipedia normalization either (a) returns not_found
# (Q618779 has no Wikipedia article titled "Q618779") or (b) lands on a
# real but unrelated Wikipedia article (Q5 is a Wikipedia disambig page
# about the alphanumeric label, not "human"). Either way the result is
# wasted LLM/HTTP cost. Skip Q-ids at entry.
_QID_PATTERN = re.compile(r"^Q\d+$")

# Stage 2 tool schema — closed-set selection over candidate strings, with
# abstention as a first-class first-option. The output is one of the
# candidate strings or the literal "ABSTAIN", plus reasoning for the
# audit log.
_STAGE_2_TOOL: dict[str, Any] = {
    "name": "select_disambiguation",
    "description": (
        "Pick the candidate Wikipedia article whose subject the user most "
        "plausibly meant, based on the surrounding source text. If the text "
        "does not provide clear evidence for one candidate, output ABSTAIN. "
        "Abstention is the correct response when context does not determine "
        "the answer."
    ),
    "input_schema": {
        "type": "object",
        "required": ["selection", "reasoning"],
        "properties": {
            "selection": {
                "type": "string",
                "description": (
                    "The candidate string you selected, copied exactly from the "
                    "`candidates` list provided, OR the literal string 'ABSTAIN' "
                    "when no candidate clearly matches the source text."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "One or two sentences explaining the choice (or the "
                    "abstention). Cite the phrase from the source text that "
                    "supports the choice when applicable."
                ),
            },
        },
    },
}

_STAGE_2_SYSTEM_PROMPT = """\
You disambiguate ambiguous entity references using surrounding context.

The user wrote a claim whose entity reference matches multiple Wikipedia
articles. Pick the article whose subject the user most plausibly meant,
based on the surrounding text. If the surrounding text does not provide
clear evidence for one candidate, output ABSTAIN.

Abstention is the correct response when context does not determine the
answer. Do NOT guess based on prior probability or what seems most likely
in general — pick a candidate only when the source text actively supports
the pick. A wrong selection is worse than an abstention; abstention lets
the system honestly report it could not verify, which is the intended
behaviour.

Your selection must be either:
  - One of the strings from the `candidates` list, copied exactly, OR
  - the literal string ABSTAIN.

Do not invent a new candidate. Do not paraphrase a candidate.
"""

# Defaults used when no Config is wired (test paths that construct the
# normalizer directly without a Config object). Production paths come
# through build_pipeline which passes a Config.
_DEFAULT_API_URL = "https://en.wikipedia.org/w/api.php"
_DEFAULT_RATE = 10.0
_DEFAULT_ENTITY_TTL_SECONDS = 3600
_DEFAULT_STAGE_2_MAX_CANDIDATES = 20
_RETRY_BACKOFF_SECONDS = 1.0

# Stage 1 outcome strings — keep stable, audit log readers depend on them.
OUTCOME_CANONICAL_NO_REDIRECT = "canonical_no_redirect"
OUTCOME_CLEAN_REDIRECT = "clean_redirect"
OUTCOME_DISAMBIGUATION_PAGE = "disambiguation_page"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_API_ERROR = "api_error"
# Phase H Cluster 1 step 1: short-circuit outcome for Q-id surface forms.
# Distinct from the four real Stage 1 outcomes so audit log readers can
# count "skipped vs. attempted" separately.
OUTCOME_SKIPPED_KB_IDENTIFIER = "skipped_kb_identifier"


@dataclass
class NormalizationResult:
    """Result of normalizing a single entity reference.

    `normalized_form` equals `surface_form` when Stage 1 returns
    canonical_no_redirect or not_found, or when Stage 2 abstains. The
    `stage_1_outcome` is one of the OUTCOME_* constants. Stage 2 fields
    are populated only when Stage 2 was invoked.
    """

    surface_form: str
    normalized_form: str
    stage_1_outcome: str
    stage_1_redirect_target: Optional[str] = None
    stage_2_invoked: bool = False
    stage_2_candidates: list[str] = field(default_factory=list)
    stage_2_selection: Optional[str] = None
    stage_2_reasoning: Optional[str] = None
    duration_ms: float = 0.0
    error: Optional[str] = None
    # Phase H Cluster 1 step 1: True when this result was served from
    # the per-instance memo rather than freshly computed. The audit
    # event still fires (observability of resolver call patterns), so
    # downstream readers count memo hits via this flag rather than via
    # counts of unique normalize() invocations.
    from_memo: bool = False


@dataclass
class Stage1Outcome:
    """Raw Stage 1 result for one reference. The normalizer's public
    `normalize()` method composes this with Stage 2 logic into a
    NormalizationResult; `normalize_batch()` returns these directly for
    callers that want to drive Stage 2 themselves (e.g. tests)."""

    surface_form: str
    outcome: str
    canonical_title: Optional[str] = None  # set when clean_redirect or canonical_no_redirect
    disambiguation_title: Optional[str] = None  # set when disambiguation_page
    error: Optional[str] = None


class WikipediaNormalizer:
    def __init__(
        self,
        http_cache=None,
        llm_client=None,
        db=None,
        config=None,
    ) -> None:
        self._http = http_cache
        self._llm = llm_client
        self._db = db
        self._config = config

        rate = self._cfg_value("wikipedia_request_rate_per_second", _DEFAULT_RATE)
        self._limiter = RateLimiter(rate)

        # Phase H Cluster 1 step 1: per-instance memo of normalize()
        # outcomes. The Cluster 1 diagnostic surfaced that the walker
        # calls `EntityResolver.resolve("President", ctx)` eight or more
        # times per case (multiple slots × KB neighbor enumeration paths).
        # Each call drove a fresh Stage 2 Haiku invocation, multiplying
        # cost. The memo holds the NormalizationResult for the duration
        # of the normalizer instance; the calibration runner and the
        # pipeline both build one normalizer per session, so the memo
        # spans a single walk / verification request.
        #
        # Key is the full set of inputs that change Stage 2's prompt
        # (surface form + structured claim context + source text +
        # slot). Two calls with identical inputs get the same answer
        # without firing the LLM again. Audit events still fire on
        # memo hits (with `from_memo=True`) so observability is
        # preserved.
        self._normalize_memo: dict[tuple, NormalizationResult] = {}

    def _cfg_value(self, attr: str, default):
        if self._config is None:
            return default
        return getattr(self._config, attr, default)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def normalize(
        self,
        surface_form: str,
        claim_subject: Optional[str] = None,
        claim_predicate: Optional[str] = None,
        claim_object: Optional[str] = None,
        source_text: Optional[str] = None,
        slot_position: Optional[str] = None,
        claim_id: Optional[str] = None,
    ) -> NormalizationResult:
        """Normalize a single entity reference. Stage 1 + Stage 2.

        Stage 1: Wikipedia redirect resolution. When the surface form
        cleanly resolves (canonical_no_redirect or clean_redirect) the
        normalized form is the canonical Wikipedia title and Stage 2 is
        skipped. When it returns disambiguation_page Stage 2 invokes the
        LLM selection; on abstention or candidate-fetch failure the
        surface form is preserved unchanged.

        not_found / api_error: surface form preserved (downstream
        resolution behaves as it does today, likely abstaining).

        Phase H Cluster 1 step 1: Q-id surface forms short-circuit
        without an HTTP call (they're already canonical KB identifiers;
        Wikipedia has nothing useful to say about them). Repeat calls
        with identical context are served from `_normalize_memo` —
        Stage 1's HTTP fetch and Stage 2's Haiku call both happen at
        most once per (surface, context) per normalizer instance.
        """
        # Q-id short-circuit (Mechanism F fix). Skips Stage 1, Stage 2,
        # and the memo — the answer is structural, not data-driven.
        if isinstance(surface_form, str) and _QID_PATTERN.match(surface_form):
            result = NormalizationResult(
                surface_form=surface_form,
                normalized_form=surface_form,
                stage_1_outcome=OUTCOME_SKIPPED_KB_IDENTIFIER,
            )
            self._log_audit_event(
                result,
                claim_id=claim_id,
                slot_position=slot_position,
                claim_subject=claim_subject,
                claim_predicate=claim_predicate,
                claim_object=claim_object,
                source_text=source_text,
            )
            return result

        memo_key = (
            surface_form,
            claim_subject,
            claim_predicate,
            claim_object,
            source_text,
            slot_position,
        )
        memo_hit = self._normalize_memo.get(memo_key)
        if memo_hit is not None:
            # Return a fresh result so the caller doesn't mutate the
            # stored value. `from_memo=True` flags the audit event.
            result = NormalizationResult(
                surface_form=memo_hit.surface_form,
                normalized_form=memo_hit.normalized_form,
                stage_1_outcome=memo_hit.stage_1_outcome,
                stage_1_redirect_target=memo_hit.stage_1_redirect_target,
                stage_2_invoked=memo_hit.stage_2_invoked,
                stage_2_candidates=list(memo_hit.stage_2_candidates),
                stage_2_selection=memo_hit.stage_2_selection,
                stage_2_reasoning=memo_hit.stage_2_reasoning,
                duration_ms=memo_hit.duration_ms,
                error=memo_hit.error,
                from_memo=True,
            )
            self._log_audit_event(
                result,
                claim_id=claim_id,
                slot_position=slot_position,
                claim_subject=claim_subject,
                claim_predicate=claim_predicate,
                claim_object=claim_object,
                source_text=source_text,
            )
            return result

        start = time.monotonic()
        stage1 = self._stage_1_for_single(surface_form)
        result = self._compose_result(
            stage1,
            surface_form,
            start,
            claim_subject=claim_subject,
            claim_predicate=claim_predicate,
            claim_object=claim_object,
            source_text=source_text,
        )
        self._normalize_memo[memo_key] = result
        self._log_audit_event(
            result,
            claim_id=claim_id,
            slot_position=slot_position,
            claim_subject=claim_subject,
            claim_predicate=claim_predicate,
            claim_object=claim_object,
            source_text=source_text,
        )
        return result

    def normalize_batch(self, references: list[str]) -> dict[str, Stage1Outcome]:
        """Stage 1 over a batch of references in one API call (up to 50
        titles per request — the MediaWiki limit). Returns a dict keyed by
        the input surface form.

        Caller-driven Stage 2: this method does NOT invoke Stage 2 on any
        disambiguation outcomes; the caller can iterate the outcomes and
        decide whether/how to drive Stage 2 (per claim, with source text
        context, etc.).

        An empty input returns an empty dict. The order of the dict's
        items is the input order (Python 3.7+ dict-insertion-order
        guarantee).
        """
        out: dict[str, Stage1Outcome] = {}
        if not references:
            return out

        # Deduplicate while preserving input order — MediaWiki rejects
        # duplicate titles within one request as the same page anyway.
        seen: dict[str, None] = {}
        for ref in references:
            if ref not in seen:
                seen[ref] = None
        unique = list(seen.keys())

        # MediaWiki accepts up to 50 titles per query.
        batch_size = 50
        for start_idx in range(0, len(unique), batch_size):
            batch = unique[start_idx : start_idx + batch_size]
            batch_outcomes = self._stage_1_query_batch(batch)
            for ref in batch:
                out[ref] = batch_outcomes.get(
                    ref,
                    Stage1Outcome(surface_form=ref, outcome=OUTCOME_API_ERROR, error="missing_in_response"),
                )

        return out

    # ------------------------------------------------------------------
    # Stage 1 internals
    # ------------------------------------------------------------------

    def _stage_1_for_single(self, surface_form: str) -> Stage1Outcome:
        outcomes = self._stage_1_query_batch([surface_form])
        return outcomes.get(
            surface_form,
            Stage1Outcome(surface_form=surface_form, outcome=OUTCOME_API_ERROR, error="missing_in_response"),
        )

    def _stage_1_query_batch(self, titles: list[str]) -> dict[str, Stage1Outcome]:
        """Issue one MediaWiki query and parse the response into per-title
        Stage1Outcome objects.

        Empty titles in the batch are skipped (returned as not_found) —
        querying empty strings against MediaWiki returns garbage that the
        parse logic can't disambiguate from a real missing page.
        """
        # Defensive: skip empty titles, MediaWiki normalizes them
        # unhelpfully and we'd lose the per-title mapping.
        non_empty = [t for t in titles if t and t.strip()]
        outcomes: dict[str, Stage1Outcome] = {}
        for t in titles:
            if not t or not t.strip():
                outcomes[t] = Stage1Outcome(
                    surface_form=t,
                    outcome=OUTCOME_NOT_FOUND,
                    error="empty_title",
                )

        if not non_empty:
            return outcomes

        if self._http is None:
            # Wiring-gap defence: a normalization was attempted without an
            # HTTP cache. Surface honestly rather than silently no-op.
            raise RuntimeError(
                "WikipediaNormalizer requires an http_cache; "
                "build_pipeline must construct the normalizer with a CachingHTTPClient"
            )

        url = self._cfg_value("wikipedia_api_url", _DEFAULT_API_URL)
        params = {
            "action": "query",
            "titles": "|".join(non_empty),
            "redirects": "1",
            "prop": "pageprops",
            "format": "json",
            "formatversion": "2",
        }
        ttl = self._cfg_value("http_cache_entity_ttl_seconds", _DEFAULT_ENTITY_TTL_SECONDS)

        data = None
        last_error: Optional[str] = None
        for attempt in range(2):
            self._limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
                last_error = None
                break
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break

        if data is None:
            # Total failure: every title in the batch is an api_error.
            for t in non_empty:
                outcomes[t] = Stage1Outcome(
                    surface_form=t,
                    outcome=OUTCOME_API_ERROR,
                    error=last_error or "unknown_error",
                )
            return outcomes

        outcomes.update(self._parse_stage_1_response(data, non_empty))
        return outcomes

    def _parse_stage_1_response(
        self, data: dict, requested_titles: list[str]
    ) -> dict[str, Stage1Outcome]:
        """Parse a MediaWiki query response into per-title Stage1Outcome
        objects.

        Response shape (formatversion=2):

          {
            "query": {
              "normalized": [{"from": "...", "to": "..."}, ...],   # optional
              "redirects":  [{"from": "...", "to": "..."}, ...],   # optional
              "pages": [
                {
                  "title": "Barack Obama",
                  "pageid": 534366,
                  "pageprops": {"wikibase_item": "Q76"}
                  // OR: "pageprops": {"disambiguation": ""} for a disambig page
                  // OR: "missing": true for a 404
                },
                ...
              ]
            }
          }

        MediaWiki may also `normalize` the input title (case/encoding fixes)
        before processing — we honor that as the input-title mapping.
        """
        outcomes: dict[str, Stage1Outcome] = {}
        if not isinstance(data, dict):
            for t in requested_titles:
                outcomes[t] = Stage1Outcome(
                    surface_form=t,
                    outcome=OUTCOME_API_ERROR,
                    error="malformed_response",
                )
            return outcomes

        query = data.get("query", {}) if isinstance(data.get("query"), dict) else {}
        pages = query.get("pages", []) if isinstance(query.get("pages"), list) else []
        # Map: input title → normalized title (after MediaWiki's
        # normalization pass; e.g. "obama" → "Obama").
        normalize_map: dict[str, str] = {}
        for n in query.get("normalized", []) or []:
            if isinstance(n, dict) and "from" in n and "to" in n:
                normalize_map[n["from"]] = n["to"]
        # Map: normalized title → redirect target (after MediaWiki's
        # redirect-following; e.g. "Obama" → "Barack Obama").
        redirect_map: dict[str, str] = {}
        for r in query.get("redirects", []) or []:
            if isinstance(r, dict) and "from" in r and "to" in r:
                redirect_map[r["from"]] = r["to"]

        # Map: page title (final, after redirect) → page dict.
        page_by_title: dict[str, dict] = {}
        for p in pages:
            if isinstance(p, dict) and "title" in p:
                page_by_title[p["title"]] = p

        for original in requested_titles:
            normalized = normalize_map.get(original, original)
            redirected = redirect_map.get(normalized, normalized)
            page = page_by_title.get(redirected)

            if page is None:
                # MediaWiki returned no page for this title — treat as
                # not_found rather than crashing.
                outcomes[original] = Stage1Outcome(
                    surface_form=original,
                    outcome=OUTCOME_NOT_FOUND,
                    error="no_page_for_title",
                )
                continue

            # Missing page → not_found.
            if page.get("missing"):
                outcomes[original] = Stage1Outcome(
                    surface_form=original,
                    outcome=OUTCOME_NOT_FOUND,
                )
                continue

            page_title = page.get("title", redirected)
            pageprops = page.get("pageprops") if isinstance(page.get("pageprops"), dict) else {}

            # Disambiguation page: pageprops contains the "disambiguation" key
            # (its value is the empty string by convention, but presence is
            # what matters).
            if pageprops and "disambiguation" in pageprops:
                outcomes[original] = Stage1Outcome(
                    surface_form=original,
                    outcome=OUTCOME_DISAMBIGUATION_PAGE,
                    disambiguation_title=page_title,
                )
                continue

            # The page is a real article. Did we follow a redirect to get here?
            # Compare against the input title (case-insensitive, matching
            # MediaWiki's normalization behavior).
            redirect_followed = page_title.lower() != original.lower()
            if redirect_followed:
                outcomes[original] = Stage1Outcome(
                    surface_form=original,
                    outcome=OUTCOME_CLEAN_REDIRECT,
                    canonical_title=page_title,
                )
            else:
                outcomes[original] = Stage1Outcome(
                    surface_form=original,
                    outcome=OUTCOME_CANONICAL_NO_REDIRECT,
                    canonical_title=page_title,
                )

        return outcomes

    # ------------------------------------------------------------------
    # Composition + audit
    # ------------------------------------------------------------------

    def _compose_result(
        self,
        stage1: Stage1Outcome,
        surface_form: str,
        start_time: float,
        claim_subject: Optional[str] = None,
        claim_predicate: Optional[str] = None,
        claim_object: Optional[str] = None,
        source_text: Optional[str] = None,
    ) -> NormalizationResult:
        """Compose a NormalizationResult from a Stage 1 outcome. When the
        outcome is disambiguation_page, invokes Stage 2 (LLM-mediated
        selection over the disambiguation page's candidate links). When
        Stage 2 abstains, returns the surface form unchanged.
        """
        if stage1.outcome == OUTCOME_CANONICAL_NO_REDIRECT:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return NormalizationResult(
                surface_form=surface_form,
                normalized_form=stage1.canonical_title or surface_form,
                stage_1_outcome=stage1.outcome,
                duration_ms=duration_ms,
            )

        if stage1.outcome == OUTCOME_CLEAN_REDIRECT:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return NormalizationResult(
                surface_form=surface_form,
                normalized_form=stage1.canonical_title or surface_form,
                stage_1_outcome=stage1.outcome,
                stage_1_redirect_target=stage1.canonical_title,
                duration_ms=duration_ms,
            )

        if stage1.outcome == OUTCOME_DISAMBIGUATION_PAGE:
            return self._stage_2(
                stage1=stage1,
                surface_form=surface_form,
                start_time=start_time,
                claim_subject=claim_subject,
                claim_predicate=claim_predicate,
                claim_object=claim_object,
                source_text=source_text,
            )

        # not_found or api_error: surface form unchanged.
        duration_ms = (time.monotonic() - start_time) * 1000.0
        return NormalizationResult(
            surface_form=surface_form,
            normalized_form=surface_form,
            stage_1_outcome=stage1.outcome,
            duration_ms=duration_ms,
            error=stage1.error,
        )

    # ------------------------------------------------------------------
    # Stage 2 — LLM-mediated selection
    # ------------------------------------------------------------------

    def _stage_2(
        self,
        stage1: Stage1Outcome,
        surface_form: str,
        start_time: float,
        claim_subject: Optional[str],
        claim_predicate: Optional[str],
        claim_object: Optional[str],
        source_text: Optional[str],
    ) -> NormalizationResult:
        """Drive Stage 2: fetch candidate links from the disambiguation
        page, ask Haiku to pick one (or abstain), apply the selection.

        Failure modes:
          - Candidate fetch fails → record error, abstain (surface form
            unchanged). Architecture §3.2: false-abstain is cheaper than
            silently picking wrong.
          - LLM call fails → record error, abstain.
          - LLM picks something not in the candidate list → reject,
            abstain (defence against a future model hallucinating a title).
          - LLM emits ABSTAIN → record reasoning, abstain.
        """
        disambig_title = stage1.disambiguation_title or surface_form

        candidates, fetch_error = self._fetch_disambiguation_candidates(disambig_title)
        if fetch_error is not None:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return NormalizationResult(
                surface_form=surface_form,
                normalized_form=surface_form,
                stage_1_outcome=stage1.outcome,
                stage_1_redirect_target=stage1.disambiguation_title,
                stage_2_invoked=False,
                duration_ms=duration_ms,
                error=fetch_error,
            )

        if not candidates:
            # Empty candidate list — disambig page with no usable links.
            # Abstain.
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return NormalizationResult(
                surface_form=surface_form,
                normalized_form=surface_form,
                stage_1_outcome=stage1.outcome,
                stage_1_redirect_target=stage1.disambiguation_title,
                stage_2_invoked=False,
                duration_ms=duration_ms,
                error="no_candidates_on_disambiguation_page",
            )

        if self._llm is None:
            # Wiring-gap: Stage 2 requires an LLM. Abstain visibly.
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return NormalizationResult(
                surface_form=surface_form,
                normalized_form=surface_form,
                stage_1_outcome=stage1.outcome,
                stage_1_redirect_target=stage1.disambiguation_title,
                stage_2_invoked=False,
                stage_2_candidates=candidates,
                duration_ms=duration_ms,
                error="no_llm_client_for_stage_2",
            )

        selection, reasoning, llm_error = self._stage_2_llm_select(
            surface_form=surface_form,
            claim_subject=claim_subject,
            claim_predicate=claim_predicate,
            claim_object=claim_object,
            source_text=source_text,
            candidates=candidates,
        )

        duration_ms = (time.monotonic() - start_time) * 1000.0

        if llm_error is not None:
            return NormalizationResult(
                surface_form=surface_form,
                normalized_form=surface_form,
                stage_1_outcome=stage1.outcome,
                stage_1_redirect_target=stage1.disambiguation_title,
                stage_2_invoked=True,
                stage_2_candidates=candidates,
                stage_2_selection=None,
                stage_2_reasoning=reasoning,
                duration_ms=duration_ms,
                error=llm_error,
            )

        # Abstention: empty selection or the literal ABSTAIN sentinel.
        # Surface form preserved.
        if not selection or selection.strip().upper() == STAGE_2_ABSTAIN:
            return NormalizationResult(
                surface_form=surface_form,
                normalized_form=surface_form,
                stage_1_outcome=stage1.outcome,
                stage_1_redirect_target=stage1.disambiguation_title,
                stage_2_invoked=True,
                stage_2_candidates=candidates,
                stage_2_selection=None,
                stage_2_reasoning=reasoning,
                duration_ms=duration_ms,
            )

        # Defence-in-depth: the model must pick from the closed candidate
        # set. If it invents a title, treat as abstention so a stray
        # hallucination cannot drive a wrong KB query downstream.
        if selection not in candidates:
            return NormalizationResult(
                surface_form=surface_form,
                normalized_form=surface_form,
                stage_1_outcome=stage1.outcome,
                stage_1_redirect_target=stage1.disambiguation_title,
                stage_2_invoked=True,
                stage_2_candidates=candidates,
                stage_2_selection=None,
                stage_2_reasoning=reasoning,
                duration_ms=duration_ms,
                error=f"selection_not_in_candidates: {selection!r}",
            )

        return NormalizationResult(
            surface_form=surface_form,
            normalized_form=selection,
            stage_1_outcome=stage1.outcome,
            stage_1_redirect_target=stage1.disambiguation_title,
            stage_2_invoked=True,
            stage_2_candidates=candidates,
            stage_2_selection=selection,
            stage_2_reasoning=reasoning,
            duration_ms=duration_ms,
        )

    def _fetch_disambiguation_candidates(
        self, disambig_title: str
    ) -> tuple[list[str], Optional[str]]:
        """Fetch the namespace-0 (article) links from a disambiguation
        page via ``action=parse&prop=links``. Returns ``(candidates, error)``;
        on success ``error`` is None.

        Truncates to ``Config.wikipedia_stage_2_max_candidates`` (default
        20) — Stage 2's LLM doesn't benefit from a longer list and the
        prompt budget is finite.
        """
        if self._http is None:
            return ([], "no_http_cache_for_stage_2_fetch")

        url = self._cfg_value("wikipedia_api_url", _DEFAULT_API_URL)
        params = {
            "action": "parse",
            "page": disambig_title,
            "prop": "links",
            "format": "json",
            "formatversion": "2",
        }
        ttl = self._cfg_value("http_cache_entity_ttl_seconds", _DEFAULT_ENTITY_TTL_SECONDS)
        max_candidates = self._cfg_value(
            "wikipedia_stage_2_max_candidates", _DEFAULT_STAGE_2_MAX_CANDIDATES
        )

        data = None
        last_error: Optional[str] = None
        for attempt in range(2):
            self._limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
                last_error = None
                break
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break

        if data is None or not isinstance(data, dict):
            return ([], last_error or "malformed_disambiguation_response")

        parse = data.get("parse", {}) if isinstance(data.get("parse"), dict) else {}
        links = parse.get("links", []) if isinstance(parse.get("links"), list) else []

        candidates: list[str] = []
        seen: set[str] = set()
        for link in links:
            if not isinstance(link, dict):
                continue
            # formatversion=2 link shape: {"ns": 0, "title": "...", "exists": true}.
            # ns 0 = main article namespace; skip meta-pages, files,
            # categories, etc.
            ns = link.get("ns")
            title = link.get("title")
            exists = link.get("exists", True)
            if ns != 0 or not isinstance(title, str) or not title:
                continue
            if not exists:
                continue  # red link → no article to choose
            if title == disambig_title:
                continue  # self-link
            if title in seen:
                continue
            seen.add(title)
            candidates.append(title)
            if len(candidates) >= max_candidates:
                break

        return (candidates, None)

    def _stage_2_llm_select(
        self,
        surface_form: str,
        claim_subject: Optional[str],
        claim_predicate: Optional[str],
        claim_object: Optional[str],
        source_text: Optional[str],
        candidates: list[str],
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Invoke Haiku with the Stage 2 tool. Returns
        ``(selection, reasoning, error)``: ``selection`` may be ``None``
        on abstention or error.
        """
        candidate_lines = "\n".join(f"  - {c}" for c in candidates)
        user_message = (
            f"surface form : {surface_form}\n"
            f"claim        : "
            f"{claim_subject or '(unknown)'} → "
            f"{claim_predicate or '(unknown)'} → "
            f"{claim_object or '(unknown)'}\n"
            f"source text  :\n"
            f"---\n"
            f"{source_text or '(no surrounding text)'}\n"
            f"---\n"
            f"candidates   :\n"
            f"{candidate_lines}\n"
            f"\n"
            f"Output the candidate string that best matches the source text, "
            f"OR ABSTAIN if no candidate clearly matches."
        )
        try:
            raw = self._llm.extract_with_tool(
                system=_STAGE_2_SYSTEM_PROMPT,
                user_message=user_message,
                tool=_STAGE_2_TOOL,
                purpose="layer1:entity_normalization",
            )
        except Exception as exc:
            return (None, None, f"{type(exc).__name__}: {exc}")

        if not isinstance(raw, dict):
            return (None, None, "malformed_tool_response")

        selection = raw.get("selection")
        reasoning = raw.get("reasoning")
        if not isinstance(selection, str):
            return (None, reasoning if isinstance(reasoning, str) else None, "missing_selection")

        return (
            selection.strip(),
            reasoning if isinstance(reasoning, str) else None,
            None,
        )

    def _log_audit_event(
        self,
        result: NormalizationResult,
        claim_id: Optional[str] = None,
        slot_position: Optional[str] = None,
        claim_subject: Optional[str] = None,
        claim_predicate: Optional[str] = None,
        claim_object: Optional[str] = None,
        source_text: Optional[str] = None,
    ) -> None:
        """Best-effort audit logging. No-ops when no db is wired (test
        constructions that don't pass one). Never raises."""
        if self._db is None:
            return
        try:
            log_event(
                self._db,
                event_type="entity_normalization",
                event_subject=result.surface_form,
                event_data={
                    "claim_id": claim_id,
                    "slot_position": slot_position,
                    "claim_subject": claim_subject,
                    "claim_predicate": claim_predicate,
                    "claim_object": claim_object,
                    "source_text_present": source_text is not None,
                    "surface_form": result.surface_form,
                    "normalized_form": result.normalized_form,
                    "stage_1_outcome": result.stage_1_outcome,
                    "stage_1_redirect_target": result.stage_1_redirect_target,
                    "stage_2_invoked": result.stage_2_invoked,
                    "stage_2_candidates": result.stage_2_candidates,
                    "stage_2_selection": result.stage_2_selection,
                    "stage_2_reasoning": result.stage_2_reasoning,
                    "duration_ms": round(result.duration_ms, 2),
                    "error": result.error,
                    "from_memo": result.from_memo,
                },
            )
        except Exception:
            # Audit logging is observability, not correctness; never let
            # a logging failure break the normalization path.
            pass
