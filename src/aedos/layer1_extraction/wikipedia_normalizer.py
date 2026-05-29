"""Phase H D47 / D53 — Wikipedia + Wikidata entity normalizer.

Resolves bare ambiguous entity references to canonical Wikidata Q-ids
before the substrate sees them. Three-stage design (D53 hybrid; D47
was the two-stage Wikipedia-only ancestor):

  Stage A — deterministic Wikipedia-redirect resolution via the MediaWiki
            `action=query&redirects=1&prop=pageprops` API. Four outcomes:
            canonical_no_redirect | clean_redirect | disambiguation_page |
            not_found. A fifth (api_error) covers transient HTTP failures.

            Stage A's role: take a short ambiguous reference and resolve
            redirect aliases to canonical article titles (Obama → Barack
            Obama, Einstein → Albert Einstein). This is exactly what
            Wikipedia's redirect system is designed for.

  Stage B — Wikidata `wbsearchentities` query on the Stage A-canonicalized
            form (or the surface form when Stage A produced disambig /
            not_found). Returns ranked Q-id candidates with labels,
            descriptions, and aliases — the architecturally correct API
            for programmatic entity disambiguation.

            Stage B's role: convert a string into a ranked set of Wikidata
            entity candidates. The empirical investigation in
            `docs/phase_H/d53_design.md` established that bare-surface
            wbsearchentities buries canonical entities (Obama → Q76 isn't
            in the top 20), but the Stage A-canonicalized form returns
            them cleanly (Barack Obama → Q76 at rank 1).

  Stage C — Type filter (D33) + heuristic shortcut + LLM-mediated
            selection over the Stage B candidates. The LLM picks the
            Q-id that best matches the source_text + structured claim,
            or abstains when context doesn't disambiguate. Single-
            candidate shortcut skips the LLM.

            Stage C's role: choose among multiple Q-id candidates using
            context the API can't see (the user's source text + the
            other slots of the claim).

The normalizer is wired into `EntityResolver.resolve`. The resolver
prefers `selected_qid` when available, falling back to label-based KB
resolution otherwise (per the operator's Q1 decision in the D53 design
review).

Audit log: every normalization produces an `entity_normalization` event
with all three stages' details. Verbose by design — Phase 10.5 post-hoc
analysis consumes these events.

Patterns deliberately mirror `kb_wikidata.py`:
  - HTTP layer via `CachingHTTPClient` (User-Agent + LRU + TTL from Config)
  - Rate limiter via `RateLimiter` (10/s default for Stage A; Wikidata's
    50/s limiter shared via `WikidataAdapter` for Stage B/C wbgetentities)
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

# Phase 10.5 Step 6 Fix #14: known-title short-circuit map. Public-role
# position titles whose canonical Wikidata Q-IDs the multi-stage
# normalizer struggles to surface (Stage B wbsearchentities ranks them
# too low; Stage C LLM-selection has variance). Direct mapping here is
# the minimum-viable fix; v0.16 will pursue a generalizable
# Wikipedia-article-title-to-Q path via the MediaWiki API.
_KNOWN_ROLE_TITLES: dict[str, str] = {
    # United States political roles
    "President of the United States": "Q11696",
    "President of the United States of America": "Q11696",
    "Vice President of the United States": "Q11699",
    # United Kingdom political roles
    "Prime Minister of the United Kingdom": "Q14211",
    "Prime Minister of Great Britain": "Q14211",
    # Religious roles
    "Pope": "Q19546",
    # Generic positions that resolve well already are NOT in this map —
    # this is a list of titles known to have low wbsearchentities
    # ranking for their canonical Q-id (validated empirically against
    # the medium-bar Pattern B cases).
}

# Phase H D53 step 2: Stage C tool — closed-set selection over Wikidata
# Q-id candidates. The model picks a Q-id (not a label, not a Wikipedia
# article title) so downstream KB lookup is keyed on the canonical
# identifier directly. Abstention discipline preserved verbatim from
# Stage 2.
_STAGE_C_TOOL: dict[str, Any] = {
    "name": "select_wikidata_entity",
    "description": (
        "Pick the Wikidata entity (by Q-id) whose subject the user most "
        "plausibly meant, based on the surrounding source text. If the "
        "text does not provide clear evidence for one candidate, output "
        "ABSTAIN. Abstention is the correct response when context does "
        "not determine the answer."
    ),
    "input_schema": {
        "type": "object",
        "required": ["selection", "reasoning"],
        "properties": {
            "selection": {
                "type": "string",
                "description": (
                    "The Q-id of the candidate you selected, copied exactly "
                    "from the `candidates` list provided (e.g. 'Q76'), OR "
                    "the literal string 'ABSTAIN' when no candidate clearly "
                    "matches the source text."
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

_STAGE_C_SYSTEM_PROMPT = """\
You disambiguate ambiguous entity references using surrounding context.

The user wrote a claim whose entity reference matches multiple Wikidata
entities. Each candidate is presented with its Q-id, label, description,
and aliases. Pick the entity whose subject the user most plausibly meant,
based on the surrounding text. If the surrounding text does not provide
clear evidence for one candidate, output ABSTAIN.

Abstention is the correct response when context does not determine the
answer. Do NOT guess based on prior probability or what seems most likely
in general — pick a candidate only when the source text actively supports
the pick. A wrong selection is worse than an abstention; abstention lets
the system honestly report it could not verify, which is the intended
behaviour.

Your selection must be either:
  - A Q-id from the `candidates` list (e.g. 'Q76'), copied exactly, OR
  - the literal string ABSTAIN.

Do not invent a new Q-id. The Q-id you return must appear in the
candidate list.
"""

# Same abstention sentinel as Stage 2 — keep the string for consistency
# with downstream audit-log consumers; the variable name is generic.
STAGE_C_ABSTAIN = "ABSTAIN"

# Defaults used when no Config is wired (test paths that construct the
# normalizer directly without a Config object). Production paths come
# through build_pipeline which passes a Config.
_DEFAULT_API_URL = "https://en.wikipedia.org/w/api.php"
_DEFAULT_RATE = 10.0
_DEFAULT_ENTITY_TTL_SECONDS = 3600
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
    """Result of normalizing a single entity reference (Phase H D53).

    Three-stage architecture:

    - `stage_a_outcome` is one of the OUTCOME_* constants (Wikipedia
      redirect resolution outcome).
    - `stage_b_*` fields describe the Wikidata wbsearchentities query
      and its candidate count.
    - `stage_c_*` fields describe the type-filter + heuristic + LLM
      selection.
    - `normalized_form` is the canonical label (Wikipedia article title
      or Wikidata label) corresponding to the selected entity, or the
      surface form when the flow abstained.
    - `selected_qid` is the Wikidata Q-id of the selected entity, or
      None when the flow abstained. Downstream KB code prefers this
      when available; falls back to label-based resolution otherwise.
    """

    surface_form: str
    normalized_form: str
    stage_a_outcome: str
    selected_qid: Optional[str] = None
    stage_a_redirect_target: Optional[str] = None
    # Stage B: wbsearchentities query and result summary.
    stage_b_query: Optional[str] = None
    stage_b_candidate_count: int = 0
    stage_b_top_candidates: list[dict] = field(default_factory=list)
    # Stage C: type filter + heuristic + LLM.
    stage_c_type_filter_applied: bool = False
    stage_c_filtered_count: int = 0
    stage_c_shortcut_fired: bool = False
    stage_c_llm_invoked: bool = False
    stage_c_candidates: list[dict] = field(default_factory=list)
    stage_c_selection: Optional[str] = None  # Q-id picked by LLM, or None
    stage_c_reasoning: Optional[str] = None
    # Common.
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
        kb_adapter=None,
    ) -> None:
        self._http = http_cache
        self._llm = llm_client
        self._db = db
        self._config = config
        # Phase H D53 step 2: KB adapter (typically `WikidataAdapter`)
        # supplies the Stage B wbsearchentities client and the Stage C
        # P31 type-filter batch fetch. When None, Stage B/C abstain
        # visibly (test paths that don't wire it; production
        # `build_pipeline` always wires it).
        self._kb_adapter = kb_adapter

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
        expected_entity_types: Optional[list[str]] = None,
    ) -> NormalizationResult:
        """Normalize a single entity reference. Stage A + Stage B + Stage C.

        Stage A: Wikipedia redirect resolution. Canonicalizes surface
        forms whose canonical entity Wikipedia knows under a longer
        title (Obama → Barack Obama). Produces a `stage_b_query` for
        Stage B.

        Stage B: Wikidata `wbsearchentities` query on the Stage A query.
        Returns ranked Q-id candidates with label / description /
        aliases.

        Stage C: D33 type filter + heuristic shortcut + LLM selection.
        Single-candidate-after-filter shortcut skips the LLM; otherwise
        Haiku picks a Q-id given source_text + claim context, or
        abstains.

        Phase H Cluster 1 step 1: Q-id surface forms short-circuit
        without an HTTP call (they're already canonical KB identifiers).
        Repeat calls with identical context are served from
        `_normalize_memo` — each of Stage A's HTTP fetch, Stage B's
        wbsearchentities call, and Stage C's Haiku call happens at most
        once per (surface, context) per normalizer instance.
        """
        # Q-id short-circuit (Mechanism F fix). Skips Stage A/B/C and
        # the memo — the answer is structural, not data-driven.
        if isinstance(surface_form, str) and _QID_PATTERN.match(surface_form):
            result = NormalizationResult(
                surface_form=surface_form,
                normalized_form=surface_form,
                stage_a_outcome=OUTCOME_SKIPPED_KB_IDENTIFIER,
                selected_qid=surface_form,  # Q-id surface form IS the canonical id
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

        # Phase 10.5 Step 6 Fix #14: known-title short-circuit. For a
        # small allow-list of public-role position titles whose canonical
        # Wikidata Q-IDs the multi-stage normalizer struggles to surface
        # consistently ("President of the United States" → Q11696, "Prime
        # Minister of the United Kingdom" → Q14211, etc.), resolve them
        # directly without the Stage A/B/C round-trip. The walker can
        # then match Lincoln's P39 = Q11696 against the canonical Q-ID.
        # This is the minimum-viable fix for the Pattern B
        # holds_role-of-org cases that Fix 8's Rule 20 successfully
        # extracts but Stage B's wbsearchentities ranks Q11696 too low
        # to surface. Future v0.16 work: a generalizable
        # "Wikipedia article title → Q-id" path via MediaWiki API.
        normalized = surface_form.strip() if isinstance(surface_form, str) else surface_form
        if normalized and normalized in _KNOWN_ROLE_TITLES:
            qid = _KNOWN_ROLE_TITLES[normalized]
            result = NormalizationResult(
                surface_form=surface_form,
                normalized_form=normalized,
                stage_a_outcome=OUTCOME_SKIPPED_KB_IDENTIFIER,
                selected_qid=qid,
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

        # Memo key includes expected_entity_types because Stage C's type
        # filter behavior depends on it — same (surface, context) with
        # different expected types could produce different selected_qid.
        types_key = tuple(sorted(expected_entity_types or []))
        memo_key = (
            surface_form,
            claim_subject,
            claim_predicate,
            claim_object,
            source_text,
            slot_position,
            types_key,
        )
        memo_hit = self._normalize_memo.get(memo_key)
        if memo_hit is not None:
            # Return a fresh result so the caller doesn't mutate the
            # stored value. `from_memo=True` flags the audit event.
            result = NormalizationResult(
                surface_form=memo_hit.surface_form,
                normalized_form=memo_hit.normalized_form,
                stage_a_outcome=memo_hit.stage_a_outcome,
                selected_qid=memo_hit.selected_qid,
                stage_a_redirect_target=memo_hit.stage_a_redirect_target,
                stage_b_query=memo_hit.stage_b_query,
                stage_b_candidate_count=memo_hit.stage_b_candidate_count,
                stage_b_top_candidates=list(memo_hit.stage_b_top_candidates),
                stage_c_type_filter_applied=memo_hit.stage_c_type_filter_applied,
                stage_c_filtered_count=memo_hit.stage_c_filtered_count,
                stage_c_shortcut_fired=memo_hit.stage_c_shortcut_fired,
                stage_c_llm_invoked=memo_hit.stage_c_llm_invoked,
                stage_c_candidates=list(memo_hit.stage_c_candidates),
                stage_c_selection=memo_hit.stage_c_selection,
                stage_c_reasoning=memo_hit.stage_c_reasoning,
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
            expected_entity_types=expected_entity_types or [],
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
        expected_entity_types: Optional[list[str]] = None,
    ) -> NormalizationResult:
        """Phase H D53: orchestrate Stage A (Wikipedia redirect) → Stage B
        (wbsearchentities) → Stage C (type filter + heuristic + LLM).

        Stage A's outcome decides the Stage B query:
          - clean_redirect       → query = redirect target
          - canonical_no_redirect → query = canonical title (same as surface)
          - disambiguation_page  → query = surface form (let Stage B/C
                                   handle the ambiguity rather than
                                   scrape Wikipedia's disambig page)
          - not_found            → query = surface form (still try Stage B)
          - api_error            → abstain with error; preserve surface

        Stage B's candidates feed Stage C. Stage C selects a Q-id or
        abstains. On abstention or any stage's error, `normalized_form`
        falls back to the most informative string available (Stage A's
        canonical title when present, else the surface form), and
        `selected_qid` is None.
        """
        expected_entity_types = expected_entity_types or []

        # ----- Stage A finalization ---------------------------------
        # Compute the Stage B query string and the fallback label.
        if stage1.outcome == OUTCOME_CLEAN_REDIRECT:
            stage_b_query = stage1.canonical_title or surface_form
            fallback_label = stage_b_query
        elif stage1.outcome == OUTCOME_CANONICAL_NO_REDIRECT:
            stage_b_query = stage1.canonical_title or surface_form
            fallback_label = stage_b_query
        elif stage1.outcome == OUTCOME_DISAMBIGUATION_PAGE:
            stage_b_query = surface_form
            fallback_label = surface_form
        elif stage1.outcome == OUTCOME_NOT_FOUND:
            stage_b_query = surface_form
            fallback_label = surface_form
        elif stage1.outcome == OUTCOME_API_ERROR:
            # Wikipedia outage: don't run Stage B (no canonicalization
            # signal anyway). Preserve surface form and surface the error.
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return NormalizationResult(
                surface_form=surface_form,
                normalized_form=surface_form,
                stage_a_outcome=stage1.outcome,
                duration_ms=duration_ms,
                error=stage1.error,
            )
        else:
            # Unknown outcome — defence-in-depth, treat as not_found.
            stage_b_query = surface_form
            fallback_label = surface_form

        # `stage_a_redirect_target` is non-None only when Stage A
        # actually followed a redirect or landed on a disambig page.
        # canonical_no_redirect / not_found / api_error → None.
        stage_a_redirect = (
            stage1.canonical_title
            if stage1.outcome == OUTCOME_CLEAN_REDIRECT
            else stage1.disambiguation_title
            if stage1.outcome == OUTCOME_DISAMBIGUATION_PAGE
            else None
        )

        # ----- Stage B: wbsearchentities ----------------------------
        if self._kb_adapter is None:
            # Wiring-gap defence: Stage B requires a KB adapter.
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return NormalizationResult(
                surface_form=surface_form,
                normalized_form=fallback_label,
                stage_a_outcome=stage1.outcome,
                stage_a_redirect_target=stage_a_redirect,
                stage_b_query=stage_b_query,
                duration_ms=duration_ms,
                error="no_kb_adapter_for_stage_b",
            )

        try:
            stage_b_candidates = self._kb_adapter.wbsearchentities(stage_b_query)
        except Exception as exc:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return NormalizationResult(
                surface_form=surface_form,
                normalized_form=fallback_label,
                stage_a_outcome=stage1.outcome,
                stage_a_redirect_target=stage_a_redirect,
                stage_b_query=stage_b_query,
                duration_ms=duration_ms,
                error=f"stage_b_error: {type(exc).__name__}: {exc}",
            )

        stage_b_top = [
            {
                "qid": c.qid,
                "label": c.label,
                "description": c.description,
                "rank": c.rank,
                "match_type": c.match_type,
            }
            for c in stage_b_candidates[:5]
        ]

        if not stage_b_candidates:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return NormalizationResult(
                surface_form=surface_form,
                normalized_form=fallback_label,
                stage_a_outcome=stage1.outcome,
                stage_a_redirect_target=stage_a_redirect,
                stage_b_query=stage_b_query,
                stage_b_candidate_count=0,
                stage_b_top_candidates=[],
                duration_ms=duration_ms,
                error="no_stage_b_candidates",
            )

        # ----- Stage C: type filter + heuristic + LLM ---------------
        c_result = self._stage_c_select(
            candidates=stage_b_candidates,
            surface_form=surface_form,
            claim_subject=claim_subject,
            claim_predicate=claim_predicate,
            claim_object=claim_object,
            source_text=source_text,
            expected_entity_types=expected_entity_types,
        )

        # Choose the normalized_form label: prefer the picked candidate's
        # label (if any); else the fallback.
        normalized_form = fallback_label
        if c_result["selected_qid"]:
            sel = next(
                (c for c in stage_b_candidates if c.qid == c_result["selected_qid"]),
                None,
            )
            if sel is not None:
                normalized_form = sel.label or fallback_label

        duration_ms = (time.monotonic() - start_time) * 1000.0
        return NormalizationResult(
            surface_form=surface_form,
            normalized_form=normalized_form,
            stage_a_outcome=stage1.outcome,
            selected_qid=c_result["selected_qid"],
            stage_a_redirect_target=stage_a_redirect,
            stage_b_query=stage_b_query,
            stage_b_candidate_count=len(stage_b_candidates),
            stage_b_top_candidates=stage_b_top,
            stage_c_type_filter_applied=c_result["type_filter_applied"],
            stage_c_filtered_count=c_result["filtered_count"],
            stage_c_shortcut_fired=c_result["shortcut_fired"],
            stage_c_llm_invoked=c_result["llm_invoked"],
            stage_c_candidates=c_result["candidates_shown"],
            stage_c_selection=c_result["selected_qid"],
            stage_c_reasoning=c_result["reasoning"],
            duration_ms=duration_ms,
            error=c_result["error"],
        )

    # ------------------------------------------------------------------
    # Stage C — type filter + heuristic + LLM selection (D53 step 2)
    # ------------------------------------------------------------------

    def _stage_c_select(
        self,
        candidates: list,                  # list[WBSearchCandidate]
        surface_form: str,
        claim_subject: Optional[str],
        claim_predicate: Optional[str],
        claim_object: Optional[str],
        source_text: Optional[str],
        expected_entity_types: list[str],
    ) -> dict:
        """Run Stage C: D33 type filter + heuristic shortcut + LLM
        selection over the Stage B candidates.

        Returns a dict with:
          selected_qid: Optional[str]    — the chosen Q-id or None on abstain
          reasoning: Optional[str]       — LLM's reasoning, or shortcut/error note
          type_filter_applied: bool
          filtered_count: int            — candidates after filter
          shortcut_fired: bool           — True if single-candidate skip
          llm_invoked: bool
          candidates_shown: list[dict]   — what was presented (filtered set)
          error: Optional[str]
        """
        # ----- Type filter (D33) ------------------------------------
        filter_applied = False
        filtered: list = list(candidates)
        if expected_entity_types and self._kb_adapter is not None:
            try:
                p31_by_qid, fetch_error = self._kb_adapter._fetch_p31_for_candidates(
                    [c.qid for c in candidates]
                )
            except Exception as exc:
                p31_by_qid, fetch_error = {}, f"{type(exc).__name__}: {exc}"

            if fetch_error is None:
                type_set = set(expected_entity_types)
                kept = [
                    c for c in candidates
                    if type_set & set(p31_by_qid.get(c.qid, []))
                ]
                filter_applied = True
                # D33 fail-open: if the filter eliminates all, pass the
                # unfiltered list to Stage C's LLM rather than abstain
                # silently. The audit log records that the filter ran
                # but produced an empty set.
                if kept:
                    filtered = kept
                # If `kept` is empty, leave `filtered` as the original
                # unfiltered list — the filter was advisory, not strict.

        candidates_shown = [
            {
                "qid": c.qid,
                "label": c.label,
                "description": c.description,
                "aliases": c.aliases,
                "rank": c.rank,
                "match_type": c.match_type,
            }
            for c in filtered
        ]

        # ----- Heuristic shortcut: single candidate -----------------
        if len(filtered) == 1:
            return {
                "selected_qid": filtered[0].qid,
                "reasoning": "single_candidate_shortcut",
                "type_filter_applied": filter_applied,
                "filtered_count": len(filtered),
                "shortcut_fired": True,
                "llm_invoked": False,
                "candidates_shown": candidates_shown,
                "error": None,
            }

        # ----- LLM selection ----------------------------------------
        if self._llm is None:
            # Wiring-gap: Stage C needs an LLM for multi-candidate cases.
            # Abstain visibly.
            return {
                "selected_qid": None,
                "reasoning": None,
                "type_filter_applied": filter_applied,
                "filtered_count": len(filtered),
                "shortcut_fired": False,
                "llm_invoked": False,
                "candidates_shown": candidates_shown,
                "error": "no_llm_client_for_stage_c",
            }

        selection, reasoning, llm_error = self._stage_c_llm_select(
            surface_form=surface_form,
            claim_subject=claim_subject,
            claim_predicate=claim_predicate,
            claim_object=claim_object,
            source_text=source_text,
            candidates=filtered,
        )

        if llm_error is not None:
            return {
                "selected_qid": None,
                "reasoning": reasoning,
                "type_filter_applied": filter_applied,
                "filtered_count": len(filtered),
                "shortcut_fired": False,
                "llm_invoked": True,
                "candidates_shown": candidates_shown,
                "error": llm_error,
            }

        # Abstention.
        if not selection or selection.strip().upper() == STAGE_C_ABSTAIN:
            return {
                "selected_qid": None,
                "reasoning": reasoning,
                "type_filter_applied": filter_applied,
                "filtered_count": len(filtered),
                "shortcut_fired": False,
                "llm_invoked": True,
                "candidates_shown": candidates_shown,
                "error": None,
            }

        # Defence-in-depth: selection must be a Q-id from the candidate
        # set. A hallucinated Q-id is treated as abstention.
        candidate_qids = {c.qid for c in filtered}
        if selection not in candidate_qids:
            return {
                "selected_qid": None,
                "reasoning": reasoning,
                "type_filter_applied": filter_applied,
                "filtered_count": len(filtered),
                "shortcut_fired": False,
                "llm_invoked": True,
                "candidates_shown": candidates_shown,
                "error": f"selection_not_in_candidates: {selection!r}",
            }

        return {
            "selected_qid": selection,
            "reasoning": reasoning,
            "type_filter_applied": filter_applied,
            "filtered_count": len(filtered),
            "shortcut_fired": False,
            "llm_invoked": True,
            "candidates_shown": candidates_shown,
            "error": None,
        }

    def _stage_c_llm_select(
        self,
        surface_form: str,
        claim_subject: Optional[str],
        claim_predicate: Optional[str],
        claim_object: Optional[str],
        source_text: Optional[str],
        candidates: list,                  # list[WBSearchCandidate]
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Invoke Haiku with the Stage C tool over Q-id candidates.

        Returns (selection_qid, reasoning, error). The selection may be
        the literal "ABSTAIN" string or None on error.
        """
        candidate_lines = []
        for c in candidates:
            desc = f" — {c.description}" if c.description else ""
            aliases_str = (
                f"   aliases: {', '.join(c.aliases[:5])}"
                if c.aliases
                else ""
            )
            candidate_lines.append(
                f"  - {c.qid}  | {c.label}{desc}\n{aliases_str}".rstrip()
            )
        candidates_block = "\n".join(candidate_lines)

        user_message = (
            f"surface form : {surface_form}\n"
            f"claim        : "
            f"{claim_subject or '(unknown)'} -> "
            f"{claim_predicate or '(unknown)'} -> "
            f"{claim_object or '(unknown)'}\n"
            f"source text  :\n"
            f"---\n"
            f"{source_text or '(no surrounding text)'}\n"
            f"---\n"
            f"candidates   :\n"
            f"{candidates_block}\n"
            f"\n"
            f"Output the Q-id of the candidate that best matches the "
            f"source text, OR ABSTAIN if no candidate clearly matches."
        )
        try:
            raw = self._llm.extract_with_tool(
                system=_STAGE_C_SYSTEM_PROMPT,
                user_message=user_message,
                tool=_STAGE_C_TOOL,
                purpose="layer1:entity_normalization",
            )
        except Exception as exc:
            return (None, None, f"{type(exc).__name__}: {exc}")

        if not isinstance(raw, dict):
            return (None, None, "malformed_tool_response")

        selection = raw.get("selection")
        reasoning = raw.get("reasoning")
        if not isinstance(selection, str):
            return (
                None,
                reasoning if isinstance(reasoning, str) else None,
                "missing_selection",
            )
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
                    "selected_qid": result.selected_qid,
                    "stage_a_outcome": result.stage_a_outcome,
                    "stage_a_redirect_target": result.stage_a_redirect_target,
                    "stage_b_query": result.stage_b_query,
                    "stage_b_candidate_count": result.stage_b_candidate_count,
                    "stage_b_top_candidates": result.stage_b_top_candidates,
                    "stage_c_type_filter_applied": result.stage_c_type_filter_applied,
                    "stage_c_filtered_count": result.stage_c_filtered_count,
                    "stage_c_shortcut_fired": result.stage_c_shortcut_fired,
                    "stage_c_llm_invoked": result.stage_c_llm_invoked,
                    "stage_c_candidates": result.stage_c_candidates,
                    "stage_c_selection": result.stage_c_selection,
                    "stage_c_reasoning": result.stage_c_reasoning,
                    "duration_ms": round(result.duration_ms, 2),
                    "error": result.error,
                    "from_memo": result.from_memo,
                },
            )
        except Exception:
            # Audit logging is observability, not correctness; never let
            # a logging failure break the normalization path.
            pass
