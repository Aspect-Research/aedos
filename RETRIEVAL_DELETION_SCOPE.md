# RETRIEVAL_DELETION_SCOPE.md

**Status:** Phase 0 deliverable — drives Phase 1 of the v0.15 pivot.
**Scope:** Complete removal of web/Wikipedia-retrieval verification from
Aedos. Wikidata becomes the primary world-fact source in a later phase;
this document covers ONLY the removal of the existing retrieval stack.

This is an inventory, not a patch. Line numbers are accurate as of
`v0.15` branch HEAD (v0.14.8 + version bump) but WILL drift as Phase 1
edits land — re-confirm with a grep pass at the start of each Phase 1
work item. Anchors (function names, table names, constants) are stable;
prefer them over raw line numbers.

A claim's path through the system today: extraction → routing
(validate + classify) → walker (Tier U / W / derivation / **fresh**) →
decision. Retrieval lives almost entirely in the **fresh** tier and the
`src/verifiers/` modules it dispatches to. Python (code-generation)
verification is a sibling of retrieval inside fresh and is KEPT.

---

## §0. Architectural decision points (resolve before Phase 1 starts)

Two items in this scope touch load-bearing invariants documented in
`CLAUDE.md`. They are inventoried below in their respective sections,
but flagged here because they are NOT routine deletions:

1. **The 8-state `verification_status` enum.** `retrieval_inconclusive`
   and `retrieval_failed` are two of the eight states. `CLAUDE.md`
   lists the 8-state enum as a load-bearing invariant ("Do not
   collapse this enum — each state encodes a distinct Layer 5
   intervention"). Removing the two retrieval states collapses it to
   six. **Decision needed:** drop the two states, OR keep them as
   dead-but-reserved tokens (no code path produces them once fresh
   retrieval is gone), OR rename them generically. Phase 1 should not
   silently collapse the enum. See §C and §B.

2. **The `retrieval` routing-method constant.** `retrieval` is one of
   five values in the `routing_memo` table's `method` CHECK constraint
   and in the LLM router's method enum. Routing is *pre-verification
   classification* — the router can still classify a claim as
   "retrieval" even if no verifier implements it. Removing the
   constant forces a schema change to the `routing_memo` CHECK
   constraint (a core-table change, flagged "don't change without
   discussion" in `CLAUDE.md`). **Decision needed:** remove `retrieval`
   from the routing vocabulary entirely (schema migration + router
   prompt rewrite), OR leave it as a classification label that now
   dead-ends at fresh dispatch. See §H.

The rest of this document inventories everything exhaustively and
marks the decision-dependent items.

---

## §A. Files to delete entirely

| File | Lines | Notes |
|---|---|---|
| `src/verifiers/retrieval_verifier.py` | ~954 | The entire retrieval verifier: query construction, Wikipedia fetch, snippet handling, LLM judge, reformulation, retrieval-cache integration. 100% retrieval. |
| `src/verifiers/comparative.py` | ~253 | Comparative-claim detection + ranking-page query templates. Only consumer is `retrieval_verifier.py` (`detect_comparative`). Note: `verifiability_triage.py` ALSO imports `detect_comparative` — see §B (triage keeps a comparative *signal* but not the retrieval query templates; confirm at Phase 1 whether the detector must be relocated rather than deleted). |
| `src/verifiers/scrapers/wikipedia.py` | ~153 | Wikipedia search provider (`search_wikipedia`). Pure scraper. |
| `src/verifiers/scrapers/__init__.py` | ~16 | Only re-exports `search_wikipedia`. Delete the file; if `src/verifiers/scrapers/` ends up empty, delete the directory. |
| `tests/test_retrieval_verifier.py` | — | See §D. |
| `tests/test_wikipedia_search.py` | — | See §D. |
| `tests/test_comparative.py` | — | See §D. |

**⚠ comparative.py / triage coupling:** `verifiability_triage.py` Rule 4
(comparative/superlative) calls `comparative.detect_comparative`. If
`comparative.py` is deleted outright, that import breaks. Phase 1 must
either (a) move `detect_comparative` into `verifiability_triage.py` or a
neutral util, or (b) delete only the retrieval-query-template portion of
`comparative.py` and keep the detector. Recommend (a). This is the one
"delete entirely" file that is NOT cleanly standalone.

**KEEP (siblings, not retrieval):**
- `src/verifiers/types.py` — shared `VerificationOutcome` enum + base
  result; used by code-generation too.
- `src/verifiers/__init__.py` — edit, do not delete (see §B).
- `src/verifiers/code_generation/` — the python verifier; untouched.

---

## §B. Files to modify (with line ranges)

Line ranges are HEAD-of-`v0.15` approximate. Re-grep before editing.

### `src/layer4_lookup/fresh.py`
The retrieval dispatch lives here alongside python dispatch.
- **~107–116** — `_RETRIEVAL_ERROR_FLAGS` constant (retrieval error
  classification). Delete.
- **~119–210** — `_classify_stability_for_caching()` — used only by the
  retrieval cache-write path (called from `_dispatch_retrieval`). Delete.
- **~262–266** — `if method == "retrieval":` dispatch branch calling
  `_dispatch_retrieval()`. Delete the branch.
- **~410–481** — `_dispatch_retrieval()` — entire function. Delete.
- **~483–507** — `_map_retrieval_status()` — maps retrieval outcome →
  8-state status (`retrieval_inconclusive` / `retrieval_failed`).
  Delete.
- **~40–55, ~44–52** — module docstring describing the retrieval status
  mapping. Update.
- KEEP `_dispatch_python()` / `_map_python_status()` and the
  `python` / `python_with_canonical_constants` branches untouched.

### `src/fact_store.py`
- **~13** — module docstring lists `retrieval_cache` among tables.
  Update.
- **322–326** — `CREATE TABLE retrieval_cache` DDL. Delete (see §C).
- **94–95** — `retrieval_inconclusive` / `retrieval_failed` in the
  `verification_status` set. **Decision-dependent** (§0 item 1).
- **~1093–1099** — `cache_retrieval()` method. Delete.
- **~1101–1114** — `get_cached_retrieval()` method. Delete.
- **~1127** — `DROP TABLE IF EXISTS retrieval_cache;` in `reset()`.
  Delete.
- Pipeline-event vocabulary (search the `PIPELINE_*` / event-name
  block, ~line 100–135): `retrieval_query_attempt`,
  `comparative_detected`, `judge_retry_after_inconclusive`,
  `reformulation_emitted`, `reformulation_failed` — these events are
  emitted only by `retrieval_verifier.py`. Remove from the event
  catalog when the verifier is deleted. Re-grep to confirm exact names.

### `src/llm_client.py`
- **87** — `"retrieval_judge": "gpt-4.1-mini"` in the model map. See §G.

### `src/layer4_lookup/tier_w.py`
- **~12–13** — docstring enumerates the 8-state enum incl. the two
  retrieval states. Update if §0 item 1 collapses the enum.
- **~261–262** — `retrieval_inconclusive` / `retrieval_failed` in the
  non-cacheable / fall-through status set (`_CACHEABLE_*` logic around
  ~261–271). Decision-dependent (§0 item 1).
- **~1307–1308, ~1557–1573** — migration-sketch docstring referencing
  retrieval statuses. Cosmetic; update.

### `src/layer4_lookup/walker.py`
- **~32, ~88, ~113–114** — `retrieval_inconclusive` / `retrieval_failed`
  in the Tier-W-fall-through status set (a Tier W MATCH on one of these
  statuses does NOT terminate the walk). Decision-dependent (§0 item 1);
  if the statuses are kept as reserved tokens this code can stay.

### `src/layer4_lookup/types.py`
- **~15, ~211–212** — docstring describing the retrieval statuses.
  Update.

### `src/layer4_lookup/derivation.py`
- **~433** — comment listing `retrieval_inconclusive` among "other
  statuses". Cosmetic; update.

### `src/layer5_decision/confidence.py`
- **~58–64** — `PATH_PRIOR_BY_VERIFIER` dict has `"retrieval": 0.85`.
  Delete the entry (no claim reaches Layer 5 with
  `routing_method="retrieval"` once fresh retrieval is gone — but a
  defensive `KeyError` guard may be wanted; confirm with §0 item 2).
- **~33, ~271–272** — docstrings referencing the retrieval statuses.
  Update.

### `src/layer5_decision/intervention.py`
- **~106** — condition includes `status == "retrieval_failed"`.
- **~215–216** — `if status == "retrieval_inconclusive": → HEDGE`.
- **~221–222** — `if status == "retrieval_failed": → NOOP`.
- **~29–30, ~78** — docstring/comment lines on the retrieval statuses.
  If §0 item 1 keeps the statuses as reserved tokens, this code stays
  (it just never fires). If the statuses are dropped, delete these
  branches.

### `src/layer5_decision/corrector.py`
- **~40, ~55, ~121** — docstring mentions of the retrieval statuses.
  Cosmetic; update. No executable retrieval logic.

### `src/layer2_routing/llm_router.py`
- **~5** — module docstring lists `retrieval` among methods.
- **~34** — `retrieval` in the `ROUTING_METHODS` tuple.
- **~52, ~61** — `retrieval_query_hint` field on `RoutingDecision` +
  its `to_dict()` serialization.
- **~76–121** — `_ROUTING_TOOL` schema: `retrieval` in the method enum
  and the `retrieval_query_hint` property (~101–107).
- **~124–323** — extensive system-prompt text describing retrieval
  routing, worked examples (`## retrieval`, `## retrieval
  (mereological…)`, `## retrieval (encyclopedic-but-fuzzy…)`),
  precedence text ("python > … > retrieval > …").
  **Decision-dependent (§0 item 2).** If `retrieval` stays a
  classification label, leave most of this; if removed, this prompt
  needs a substantial rewrite (and is where Wikidata routing language
  will later go).

### `src/layer2_routing/reconciler.py`
- **~80–82** — `_RETRIEVAL_FAMILY` set.
- **~91, ~96** — `_method_family()` returns `"retrieval"`.
- **~132, ~156, ~160–162** — `retrieval` used as the ultimate
  fallback routing method (`pattern.default_routing_method or
  "retrieval"`). **Decision-dependent (§0 item 2)** — if `retrieval`
  is removed as a routing constant, the fallback method must change.
- **~172–176** — comment text. Cosmetic.

### `src/layer2_routing/routing_memo.py`
- **~55** — `"retrieval"` in the `ROUTING_METHODS` tuple. This tuple
  mirrors the `routing_memo` table's `method` CHECK constraint.
  **Decision-dependent (§0 item 2)** — see §H.

### `src/layer1_extraction/verifiability_triage.py`
- **~5** — docstring: "fresh dispatch — retrieval + LLM judge".
- Imports `detect_comparative` from `src/verifiers/comparative.py`
  (Rule 4). See §A coupling warning — this import must be repointed if
  `comparative.py` is deleted.
- The triage gate itself is verifier-agnostic; it decides *whether* to
  run fresh dispatch, not which verifier. No rule logic deletion
  needed beyond the comparative-import fix.

### `src/layer1_extraction/pattern_registry.py`
- **~56** — `query_strategy` field on the `Pattern` dataclass.
- **~222** — `query_strategy = tuple(body.get("query_strategy") or ())`
  in `_build_pattern()`.
- **~257** — `query_strategy=query_strategy` in the `Pattern(...)`
  constructor call.
  Delete all three (the field is consumed only by the retrieval
  verifier's query construction). See §F.

### `src/layer1_extraction/patterns.yaml`
- See §F (YAML fields).

### `src/pipeline.py`
- **~795** (and the chat-system-prompt template, search for
  `Wikipedia` / `retrieval`) — `CHAT_SYSTEM_TEMPLATE` tells the chat
  model the pipeline "can also search Wikipedia for world facts".
  That sentence is now false; update the prompt text. Also a comment
  near fresh-dispatch wiring mentions "retrieval + LLM judge".

### `src/app.py`
- Search for an error-hint string mentioning "retrieval verifier
  network timeout" (~lines 340–345). Update or remove.

### `src/verifiers/__init__.py`
- Remove any re-export of `RetrievalVerifier` / retrieval symbols.
  Keep code-generation exports.

---

## §C. Tables / columns to drop

| Object | Location | Action |
|---|---|---|
| `retrieval_cache` table | `fact_store.py` DDL 322–326 | DROP. Columns: `query` (PK), `snippets`, `fetched_at`. No FK in or out. Also remove the `DROP TABLE IF EXISTS retrieval_cache` line in `reset()` (~1127). |
| `verification_status` values `retrieval_inconclusive`, `retrieval_failed` | `fact_store.py` set 94–95 | **Decision-dependent (§0 item 1).** Not a column — a value-domain set in Python. If dropped, the enum becomes 6-state, contradicting `CLAUDE.md`'s "do not collapse" invariant — requires explicit sign-off. |
| `routing_memo.method` CHECK value `retrieval` | `fact_store.py` `routing_memo` DDL | **Decision-dependent (§0 item 2).** The CHECK constraint is `method ∈ {python, python_with_canonical_constants, retrieval, user_authoritative, unverifiable}`. Removing `retrieval` requires editing the CHECK — a core-table change. |

No retrieval-related columns exist on `facts`, `verification_cache`,
`turns`, or `pipeline_events`. `verification_cache` rows written by the
retrieval verifier are indistinguishable at the column level from any
other Tier W row; they age out by TTL. No data migration needed (v0.14
resets the DB on schema change).

---

## §D. Tests to delete entirely

| File | Reason |
|---|---|
| `tests/test_retrieval_verifier.py` | Tests `RetrievalVerifier` end to end. 100% retrieval. |
| `tests/test_wikipedia_search.py` | Tests `search_wikipedia()`. 100% retrieval. |
| `tests/test_comparative.py` | Tests `comparative.py`. ⚠ If Phase 1 keeps `detect_comparative` (relocated, per §A), the comparative-*detector* tests should be relocated rather than deleted — only the ranking-query-template tests are pure retrieval. Re-triage at Phase 1. |

---

## §E. Tests to modify (specific functions)

For each file below, the named test functions are retrieval-coupled
and will fail / become dead once retrieval is removed. Delete or
rewrite them; the rest of each file stays.

### `tests/test_fresh.py`
- `TestRetrievalStatusMapping` class (entire class, ~361–417):
  `test_retrieval_status_mapped` (parametrized) + helper
  `_canned_retrieval`.
- `TestRetrievalCaching` class (entire class, ~419–545):
  `test_retrieval_inconclusive_does_not_write_to_tier_w`,
  `test_retrieval_failed_does_not_write_to_tier_w`,
  `test_retrieval_uses_classifier_stability_class`,
  `test_retrieval_volatile_skips_cache`,
  `test_retrieval_user_specific_skips_cache`,
  `test_retrieval_session_specific_skips_cache`,
  `test_retrieval_classifier_failure_skips_cache_no_crash`,
  plus the `_retrieval_with` helper.
- `test_no_llm_returns_pending_for_retrieval` (~243).
- Module docstring (~8–13) mentions retrieval routing — update.
- KEEP all python-dispatch tests.

### `tests/test_router.py`
- `test_classifies_retrieval` (~146) — delete.
- `_retrieval_decision` helper (~95) — used by MANY router tests
  (~249, 268, 291, 325, 339, 397). **Decision-dependent (§0 item 2):**
  if `retrieval` stays a routing label, these tests stay valid
  (routing classification still works); if `retrieval` is removed from
  the routing vocabulary, every test using `_retrieval_decision` must
  be repointed to another method. Re-triage after §0 item 2 resolves.

### `tests/test_walker.py`
- Tests for Tier-W-fall-through on retrieval statuses (~305–390):
  the `retrieval_inconclusive` "should NOT terminate" test and the
  `retrieval_failed` fall-through test. Decision-dependent (§0 item 1).
- `_classified_decision(..., method="retrieval")` helper (~111) and
  tests using it (~269, 489) — decision-dependent (§0 item 2).

### `tests/test_pipeline.py`
- The retrieval-routed end-to-end case at ~529–550: a fact with
  `expected_verifier="retrieval"` and a `RoutingDecision(
  method="retrieval", …)` ("behavioral world fact" → fresh dispatch
  falls through to retrieval). Delete or repoint this scenario.
- Numerous `RoutingDecision(... retrieval_query_hint=None ...)`
  constructions (~98, 187, 766) — these only break if the
  `retrieval_query_hint` field is removed from `RoutingDecision`
  (§B, llm_router.py). Mechanical fix.

### `tests/test_app_dispatch_one_endpoint.py`
- `_seed_memo(..., "retrieval", ...)` (~119) and the assertion
  `walker_decision["routing_method"] == "retrieval"` (~140).
  Decision-dependent (§0 item 2).

### `tests/test_layer5_confidence.py`
- Any test asserting the `retrieval` path prior (0.85) or a Tier W
  decade/years-stable row classified as retrieval. Grep for
  `retrieval` in this file at Phase 1; the v0.14.6 commit also added
  `test_freshly_cached_retrieval_row_clears_threshold` which uses
  `routing_method="retrieval"` — repoint or delete.

### `tests/test_layer5_intervention.py`
- Tests asserting `HEDGE` on `retrieval_inconclusive` / `NOOP` on
  `retrieval_failed`. Decision-dependent (§0 item 1).

### `tests/test_app_chat_endpoint.py`
- **No retrieval references found.** The task brief flagged this file,
  but a grep for `retrieval` / `Wikipedia` returns nothing. No change
  expected — listed here only to record that it was checked.

### `tests/test_verifiability_triage.py`
- Only cosmetic: several docstrings say "Wikipedia handles this fine".
  No assertions depend on retrieval. Update docstrings if desired; not
  required for green tests. If `detect_comparative` is relocated (§A),
  confirm Rule-4 tests still import correctly.

> Phase 1 should run `pytest -q` after each deletion batch and diff
> against `tests/v0_15_baseline.txt` to catch any retrieval-coupled
> test not enumerated above.

---

## §F. YAML fields to remove (`src/layer1_extraction/patterns.yaml`)

The `query_strategy` field — an ordered list of query templates the
retrieval verifier used — appears on these patterns. Remove each
`query_strategy:` block and its list items:

| Pattern | `query_strategy` block at ~line |
|---|---|
| `role_assignment` | 48 |
| `categorical` | 207 |
| `spatial_temporal` | 285 |
| `relational` | 370 |
| `event` | 451 |
| `quantitative` | 537 |
| `propositional_attitude` | 667 |

(7 of 9 patterns carry it; `preference` and `mereological` do not.)

Also remove the schema-doc comment describing the field at the top of
the file (~line 22: "`query_strategy: ordered list of query templates
used by the retrieval verifier…`").

**`default_routing_method: retrieval`** also appears on several
patterns (`role_assignment` ~94, `spatial_temporal` ~412, `relational`
~492, `propositional_attitude` ~568, `mereological` ~326, `event`
~735). This is a *routing* default, not a retrieval-query field —
**decision-dependent (§0 item 2)**. Leave as-is if `retrieval` remains
a routing label; change per-pattern (or to a new Wikidata method) if
the routing constant is removed.

**`triage_verify_predicates`** (on `preference`, `spatial_temporal`,
`relational`) is a verifiability-triage allow-list, NOT retrieval —
KEEP.

When `query_strategy` is removed from the YAML, also remove the
corresponding `pattern_registry.py` field/parsing (§B).

---

## §G. Model-wrapper methods / entries to remove (`src/llm_client.py`)

- **Line 87** — `"retrieval_judge": "gpt-4.1-mini"` entry in the
  per-purpose model map. The `retrieval_judge` purpose is requested
  only by `RetrievalVerifier._judge_one()`. Remove the entry.
- No dedicated retrieval *method* exists on `LLMClient` — the verifier
  calls the generic completion/tool path with `purpose="retrieval_
  judge"`. So only the model-map entry is removed; no method deletion.
- After removal, grep the codebase for any remaining
  `purpose="retrieval_judge"` string (should be zero once
  `retrieval_verifier.py` is deleted).

---

## §H. Routing constants to remove

**Decision-dependent — see §0 item 2.** If the decision is to remove
`retrieval` from the routing vocabulary entirely:

- `src/layer2_routing/routing_memo.py` ~55 — `"retrieval"` in
  `ROUTING_METHODS`.
- `src/layer2_routing/llm_router.py` ~34 — `"retrieval"` in
  `ROUTING_METHODS`; ~76–121 — the method enum in `_ROUTING_TOOL`.
- `src/fact_store.py` — the `routing_memo` table `method` CHECK
  constraint (`… , 'retrieval', …`). Editing a core-table CHECK is
  flagged "don't change without discussion" in `CLAUDE.md`.
- `src/layer2_routing/reconciler.py` ~81 `_RETRIEVAL_FAMILY`, ~96
  `_method_family` return, ~156/162 the `or "retrieval"` fallback —
  the fallback routing method must be reassigned.

If the decision is to KEEP `retrieval` as a classification label that
dead-ends at fresh dispatch: none of the above changes; only fresh's
`_dispatch_retrieval` branch is removed (it would then return a
`routing_anomaly` or `unverifiable_pending_implementation` for a
retrieval-classified claim — confirm desired behavior at Phase 1).

The `retrieval_query_hint` field on `RoutingDecision`
(`llm_router.py` ~52/61/101–107) is retrieval-specific regardless of
the §0 item 2 decision and can be removed (it is only ever consumed by
the retrieval verifier).

---

## §I. Environment variables to remove

| Var | Location | Notes |
|---|---|---|
| `AEDOS_RETRIEVAL_CACHE_TTL_HOURS` | `.env.example` ~55 (already commented out); read via `os.getenv` in `retrieval_verifier.py` ~588 | The `os.getenv` call disappears with the file (§A). Remove the `.env.example` line. |
| `retrieval_judge` model note | `.env.example` ~32 (comment listing per-purpose models) | Cosmetic — remove the `retrieval_judge → gpt-4.1-mini` line from the comment block. |

No other `RETRIEVAL` / `WIKIPEDIA` / `SEARCH` / `SERP` environment
variables exist in `.env.example` or in `os.environ` / `os.getenv`
call sites.

---

## §J. Order-of-operations suggestion for Phase 1

1. Relocate `detect_comparative` out of `comparative.py` (§A warning),
   repoint the `verifiability_triage.py` import.
2. Delete `src/verifiers/retrieval_verifier.py`,
   `scrapers/wikipedia.py`, `scrapers/__init__.py`, `comparative.py`;
   edit `verifiers/__init__.py`.
3. Strip the retrieval branch + helpers from `fresh.py`.
4. Delete `tests/test_retrieval_verifier.py`,
   `tests/test_wikipedia_search.py`, `tests/test_comparative.py`.
5. Remove `query_strategy` from `patterns.yaml` + `pattern_registry.py`.
6. Drop the `retrieval_cache` table + `FactStore` methods.
7. Remove `retrieval_judge` from `llm_client.py` + `.env.example`.
8. Resolve §0 item 1 (enum) and §0 item 2 (routing constant), then
   apply §C / §H / the decision-dependent test edits.
9. `pytest -q`, diff against `tests/v0_15_baseline.txt`, fix the
   retrieval-coupled tests in §E.
10. Update the chat-system-prompt text in `pipeline.py` (no more
    "search Wikipedia").

---

*End of scope. Phase 1 should treat §0 as gating: do not start the
enum / routing-constant work items until those two decisions are
recorded.*
