# Aedos changelog

The version-by-version evolution. The current shape is in
[`ARCHITECTURE.md`](ARCHITECTURE.md); this file preserves the why /
what-changed-from-what for context.

## v0.7 — refactor for simplicity (current)

Eight-commit consolidation pass; no functionality change, no test loss.

  * **CacheGate**: single owner of cache scoping + lookup + write.
    Replaces ~180 lines scattered across `Pipeline` + `Router`.
  * **Router split** into a 4-file package: `__init__.py` (public
    re-exports) + `types.py` (Decision + RoutingOutcome) +
    `constants.py` (confidence levels + pattern-shape maps) +
    `router.py` (the dispatcher class). 854-line monolith → 920-line
    package across 4 files.
  * **Pipeline stage methods**: 230-line `_run_turn_inner` → 30-line
    orchestrator + 7 named stage methods (`_stage_user_side`,
    `_stage_chat_draft`, `_stage_assistant_extract`, `_stage_verify`,
    `_stage_anomaly_and_failure_events`, `_stage_correct`,
    `_stage_finalize`). Reading the orchestrator tells you the shape
    of a turn.
  * **`Decision.display_status`** projects the 8 internal verification
    statuses to 4 user-facing buckets (verified / contradicted /
    inconclusive / not_applicable). Routing logic still keys off the
    fine grain; the buckets are pure UI sugar.
  * **Dropped `code_triage` stage** (removed from emission in v0.5,
    lingered in PIPELINE_STAGES).
  * **UI shell collapse**: 5 tabs (Chat+Flow / Trace / Fact Store /
    Patterns / Cache) → 1 main view (chat + live flow with
    click-to-expand inline detail) + slide-out Inspector drawer for
    Facts / Patterns / Cache.
  * **app.js rewrite**: 1658 → 1027 lines (-38%). 28-branch event
    renderer switch → 5 PIPELINE_STEPS + one annotation renderer
    that buckets the long tail under each step.
  * **CSS rewrite**: 1008 → 765 lines (-24%).
  * **Test consolidation**: merged
    `test_verification_cache_schema.py` into
    `test_verification_cache.py`. Moved 4 dogfooding scripts to
    `scripts/legacy/`.
  * **Docs**: ARCHITECTURE.md rewritten as a clean current-state
    document (~250 lines, was ~540 with v0.1–v0.6 evolution narrative).
    Evolution moved here.

552 tests passing, same wire formats, same DB schema, no perf hit.

## v0.6 — Tier 2 verification cache + UI surface

  * Scoping classifier (`user_specific` / `session_specific` /
    `world_fact`) + stability classifier (immutable / decade_stable /
    years_stable / months_stable / days_stable / volatile) +
    `VerificationCache` storage.
  * `canonicalize_claim_key` with case-fold + whitespace-collapse +
    slot-order independence + polarity distinction.
  * Cache lookup short-circuits retrieval; cache write fills after
    successful retrieval verdicts.
  * Tier 2 cache **always on** (initial gating env vars
    `AEDOS_CACHE_TIER2/SCOPING/STABILITY/WRITES` were dropped — caches
    should accumulate over time, not opt-in).
  * **Stem normalization** in canonicalize_claim_key: `is_X`, `was_X`,
    `has_X`, `have_X`, `are_X`, `were_X`, `does_X`, `did_X`, `do_X`
    prefixes strip so equivalent predicates collide.
  * **Semantic-shape lookup**: on exact-key miss, fetch entries with
    same {pattern, polarity, identity slots} and Jaccard-rank
    predicate tokens. Catches `child_of` ↔ `son_of` style synonymy
    without going to embeddings or LLM. Identity-slot anchoring
    prevents the dangerous reverse-relation case.
  * **Historical-period shortcut**: claims with `valid_until` strictly
    in the past force `immutable` stability without an LLM call.
  * **Cost telemetry**: per-call cost recording + end-of-turn
    `turn_cost` event with by-model breakdown.
  * **Tier 1 user_id scoping**: facts and turns scoped per user.
  * **Pluggable chat backends**: Anthropic + Modal/GLM-5.1, selected
    per-turn via the chat UI dropdown.
  * **Per-backend `max_tokens`**: Modal/GLM gets 4096 (reasoning
    headroom); Anthropic stays at 1024.
  * **Single-model selection** in the chat UI: one dropdown drives
    every step (chat / extraction / routing / judge / corrector /
    cross-check).
  * **Live SSE flow**: `POST /api/chat/stream` pushes pipeline_events
    as they fire. 2KB padded preamble defeats Chrome's initial chunk
    buffering.
  * **Cache UI**: Cache tab with live hit rate, per-stability hit
    breakdown, semantic_hit badge, ↺ CACHED marker on cached
    Decisions.
  * **Wikipedia-direct retrieval** as the primary provider. Pure
    Python, no key, no rate limit at our scale. Replaces failing
    DDG-only path; falls through to Tavily / SerpAPI / DDG on miss
    or error.
  * **Tolerant judge parser**: handles markdown bolds (`**SUPPORTED**`),
    `Verdict:` prefixes, preambles, and `NOT SUPPORTED` (negation
    flips SUPPORTED ↔ CONTRADICTED).
  * **Snake_case → natural language** conversion in retrieval query
    templates so `parent_of` becomes `parent of` for the search
    engine.
  * **Substitution warning loosening**: dropped value-not-in-source
    check (false-positive on word-form numbers like "five" → 5);
    loosened source-text check to be punctuation-fuzzy.
  * **Corrector internal consistency**: corrector now demands the
    rewritten response be internally consistent with verified values
    — no more "0 words ... These are likely: Donald, children, ..."
    contradictions.

## v0.5 — LLM-routed verification

  * Replaced v0.4's pattern + predicate-override dispatch with
    `src/llm_router.py` (one LLM call per claim picks the method:
    `python` / `python_with_canonical_constants` / `retrieval` /
    `user_authoritative` / `unverifiable`).
  * Worked-example calibration in the router system prompt for
    multi-claim arithmetic, external-string boundary, canonical
    references.
  * Triage stage removed (the LLM router decides python-verifiability
    before code generation).
  * Canonical-constants cross-check at temp 0.0 / 0.3 (forced Sonnet
    4.6 because Opus 4.7 dropped temperature).
  * Routing-anomaly check hardcoded (was a per-pattern YAML flag).

## v0.4 — code-generated python verification

  * Replaced hand-written python verifiers with a four-stage pipeline:
    triage → neutral-prompt → code writer → sandbox → comparator.
  * Firewall: code writer never sees the asserted value; only the
    neutral question. Prevents confirmation-bias code.
  * Sandbox runs subprocess with closed stdin, empty cwd, minimal env.
  * `predicate_overrides` per-pattern routing escape hatch.

## v0.3 — patterns + slots

  * Closed predicate vocabulary (~37 entries) → 8 structural patterns
    + free-form predicates within each pattern.
  * Slots-aware retrieval queries (multi-attempt with slot-template
    substitution).
  * Granular verification statuses: `retrieval_inconclusive` vs
    `retrieval_failed` (the corrector hedges the former, not the
    latter).

## v0.2 — retrieval + corrector

  * Real retrieval verifier (Tavily / SerpAPI / DDG fallback) + LLM
    judge with verdict + justification.
  * Aggressive corrector with per-claim intervention planning
    (replace / hedge / soften / remove).
  * Role predicates (`holds_role`, `headed_by`, `member_of`, etc.)
    closed the role-claim gap that v0.1 missed.

## v0.1 — bones

  * Closed predicate vocabulary, hand-written python verifiers,
    extract → store → corrector pipeline. The shape that everything
    else iterates on.
