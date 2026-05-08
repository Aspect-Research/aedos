# AEDOS v0.14 implementation plan — revision 2

*Phases 6 through 9. Phases 0-8 shipped on the v0.14 branch (tags v0.14-phase-0-complete through v0.14-phase-8-complete) at the time of this revision. The Phase 6/7/8 sections below remain as historical record; the **"What the substrate looks like as Phase 9 begins"** section near the end is the authoritative current-state for the Phase 9 cutover work, and the **Phase 9 — Cutover** section that follows it is the only section describing forward work.*

## What the substrate looks like as Phase 6 begins

Phases 0-5 built the substrate in `src/aedos_v2/`. The next session should read the existing code before planning anything; the inventory below is for context, not for re-implementing.

**Layer 1 (extraction)** is complete. 9-pattern extractor including `mereological`. Predicates within a pattern are free-form. Validation lives in Layer 2, not the extractor. `POST /v2/api/extract` is the test entry point.

**Layer 2 (routing)** is complete. Two-step: rule-based `validator.validate(claim)` returns `Pass | Anomaly`, then on Pass `llm_router.route_claim(claim)` runs with `routing_memo` lookup-then-write. The memo key is `(pattern, predicate)`. The router is classification-only; verifier dispatch was deferred for later phases. Layer 2 enforces the four invariants: USER_SUBJECT_PATTERNS → user agent, mereological → part != whole, event → non-empty participants, all required slots present and non-empty.

**Layer 3 (substrate)** is complete. Four oracles built and calibrated, all dormant:

- `predicate_equivalence` — symmetric pair, lex-canonical at SQL layer, `consult(query, stored)` API. 0.967 calibration on a 70-entry corpus.
- `entity_equivalence` — symmetric pair, lex-canonical at SQL layer, no lowercase (case is semantic). 0.978 calibration on a 50-entry corpus.
- `entity_taxonomy` — directional pair, no canonical swap (each direction is its own row), 4 labels including `parent_subsumed_by_child` for caller-passed-them-backwards cases. `consult(child, parent, relation_type)` API. 0.966 calibration on a 62-entry corpus.
- `predicate_distribution` — singleton 4-tuple key `(pattern, predicate, polarity, taxonomy_relation_type)`, no pairing. `consult(pattern, predicate, polarity, taxonomy_relation_type)` API. 0.977 calibration on a 50-entry corpus.

`classifier_base.py` documents the three shapes (symmetric-pair, directional-pair, singleton-key) and shares `_now_iso`, `_safe_emit_event`, the `_ClassificationFailed` sentinel, and `confidence_from_counts`. Each oracle emits three pipeline events (`{name}_hit`, `{name}_write`, `{name}_classification_failed`) plus the shared `oracle_consulted` event. Each has a GET inspector endpoint at `/v2/api/substrate/{oracle_name}`. None increment counts on lookup hits; counts only increment on operator action (deferred to Phase 8) or contradiction propagation (Phase 7+).

**Layer 4 (lookup)** is partial. `tier_u.py` exists with three-stage resolution: literal SQL match → `entity_equivalence` for alias-identity candidates → `predicate_equivalence` for predicate matching. `tier_u.py` does NOT yet handle the session model (Phase 6) or the derivation walk (Phase 7). `tier_w.py`, `derivation.py`, `walker.py`, and `fresh.py` do not exist yet.

The Tier U code path's API:
```python
tier_u.lookup(
    claim, store, *,
    key_slot_names: list[str],
    predicate_oracle: PredicateEquivalence | None = None,
    entity_oracle: EntityEquivalence | None = None,
    llm: LLMClient | None = None,
) -> TierUResult
```

`TierUResult` carries `outcome ∈ {MATCH, CONTRADICTION, MISS}`, `via: list[str]` ordered by consultation, `predicate_equivalence_row_id: int | None`, `entity_equivalence_row_ids: list[int]`, `polarity_flipped: bool`, `slot_reversal_applied: bool`. `slot_reversal != 'none'` is logged but not yet acted on; cases that would require slot-swap currently return MISS with the slot_reversal recorded for later.

**Layer 5 (decision)** does not exist yet in v2. Phase 8 builds it.

**Storage** is in `src/aedos_v2/fact_store.py`. Tables: `facts` (with `affirmed_count`, `contradicted_count`, `is_session_local`, `session_ids` JSON column with CHECK constraint), `routing_memo`, `predicate_equivalence`, `entity_equivalence`, `entity_taxonomy`, `predicate_distribution`. Pipeline events table covers all stages. Schema reset drops all tables.

**Smoke corpus** lives at `tests/v2/smoke_corpus.jsonl`. It has accreted multiple optional fields across phases — `expected_facts`, `expected_routing`, `expected_memo_state`, `oracles_consulted`, `expected_via`, `expected_label`, `future_match_via`, `oracle_call`. Phase 9's parity check needs to dispatch on entry shape. Sketching this dispatcher early in Phase 7 or Phase 8 prevents Phase 9 surprises.

**Test counts** (mocked; live API tests gated behind `RUN_API_TESTS=1`):
- v2: 497 passed, 12 skipped (gated)
- legacy: 580 passed, 9 skipped — unchanged from pre-v0.14

**Disciplines that have held across all five phases** and must continue to hold:
- Reads are not writes (counts only on independent external evidence)
- No persisted derivations
- No collapsing the 8-state verification_status enum
- No lowercasing entity strings
- Plan-then-build per phase: Claude Code proposes a plan with surfaced ambiguities, user reviews and resolves, work proceeds with mutual sign-off
- Phase-end commit on `v0.14` branch with tag `v0.14-phase-N-complete`
- Both `tests/v2/` and legacy `tests/` must stay green at every phase boundary

**Resolved architectural decisions from earlier phases** that the next phases inherit:
- Mereological scope is constitutive parthood only; locational containment stays in `spatial_temporal`.
- Routing memo key is `(pattern, predicate)`; the extractor is held to the discipline of using distinct predicate labels for semantically distinct subtypes.
- Session-local facts: `session_ids` is a single-element list (CHECK enforced).
- Cascade invalidation: deferred to v0.15.
- Smoke corpus `via` field is a `list[str]`, not a delimited string.
- Smoke corpus `oracles_consulted` field is a `list[str]`.
- entity_taxonomy directional swap policy: option (b) — each direction is its own row, label disambiguates orientation.
- Predicate string normalization: `strip().lower()`. Entity string normalization: `strip()` only.
- The four oracles never auto-increment counts on lookup hits; only operator endpoints and Phase 7+ contradiction propagation will.

---

## Phase 6 — Tier U session model rewrite

### Scope

The substrate Phases 3-5 built the oracle-mediated resolution chain in `tier_u.py` but did not implement the session model. Phase 6 extends `tier_u.py` to honor `is_session_local` and `session_ids`, and threads session-aware insertion through the router's storage path.

This is not a clean-slate rewrite. The literal-match → entity_equivalence → predicate_equivalence chain stays. Phase 6 adds session-locality filtering on top of that chain and updates the storage path that feeds U.

**Concrete changes:**

- `tier_u.py` lookup: SQL filter is extended with `WHERE is_session_local=0 OR (is_session_local=1 AND json_each(session_ids)=current_session)`. Cross-session callers pass `current_session=None` and the filter becomes `is_session_local=0`. The oracle resolution chain runs unchanged on the filtered candidate set.

- `tier_u.py` insertion path (called by Layer 2's router on user-authoritative claims): when the source text contains a session-scope marker ("let's say for this conversation", "for this session", etc. — port `is_session_scoped()` from v1), insert with `is_session_local=1, session_ids=[current_session]`. Otherwise `is_session_local=0` and `session_ids=[]` initially.

- Reaffirmation path: when a cross-session claim (`is_session_local=0`) is reasserted in a new session, append `current_session` to `session_ids` (idempotent — set semantics) AND increment `affirmed_count`. When reasserted in the same session that's already in `session_ids`, do NOT increment. This is the load-bearing case for principle 3 (independent external evidence). Same-session repetition is not new evidence.

- The router's storage hook gets a `current_session` parameter threaded through from the caller. The pipeline (which Phase 7 will build) passes it; for now, Phase 6's tests pass it directly.

### What Phase 6 does NOT do

- No Tier W. That's Phase 7.
- No derivation walk. That's Phase 7.
- No Layer 5. That's Phase 8.
- No new oracles. The substrate is closed at four.

### Tests

- New: `tests/v2/test_session_local_lifetime.py` (~15 tests). Session-local fact visible in originating session, invisible elsewhere. CHECK constraint on `session_ids` length when `is_session_local=1`. Cross-session reaffirmation appends and increments. Same-session reaffirmation is idempotent (no append, no increment).
- New: `tests/v2/test_tier_u_session_model.py` (~10 tests). Existing oracle resolution chain still works under session-locality filtering. Cheetahs case still passes when stored as cross-session vs session-local.
- Existing: `tests/v2/test_tier_u_with_oracle.py` updated to pass `current_session` parameter; existing assertions preserved.
- Smoke corpus: 2-3 entries demonstrating session-local visibility (one in-session match, one cross-session miss for the same fact).

### Calibration

None. Phase 6 has no LLM-classifier component; it's bookkeeping discipline over existing oracle infrastructure. The risk profile is "subtle bookkeeping bug ships without calibration corpus catching it" — this is why the test design above leans heavy on edge cases (idempotency, same-session vs cross-session, the CHECK constraint, the recency-without-reinforcement case).

### Rollback criteria

Schema is unchanged from Phase 0; rolling back Phase 6 means reverting the router code changes and the tier_u filter extension. Schema CHECK constraint on session_ids stays. Concretely revertable as `git revert <phase-6-merge>`.

### Done when

- "let's say for this conversation I live in Williamsburg" creates a session-local fact visible only in that conversation.
- "I live in Boston" creates a cross-session fact.
- Cross-session reaffirmation increments `affirmed_count` exactly once per new session; same-session repeats do not increment.
- The Phase 5 cheetahs regression assertion still passes.
- All four oracle integration tests still pass under the new tier_u filter.

### Phase 6 risk callouts

The architectural document notes that Phase 6's risk is highest because there's no calibration corpus to catch bookkeeping bugs. The next session should treat the test plan as load-bearing rather than perfunctory. Specifically: write the tests *first* against the desired bookkeeping invariants, then implement the code that makes them pass. The reaffirmation-counter discipline is the one most likely to ship subtly wrong.

The session-marker detection logic (`is_session_scoped(claim)`) is being ported from v1. Re-read the v1 implementation; the regex set has accumulated phrases beyond the architecture document's "let's say for this conversation" canonical example.

---

## Phase 7 — Derivation walk

### Scope

The substrate exists; Phase 7 builds the consumer. Phase 7 produces the architecture's headline capability: a claim that matches no stored fact directly is resolved by walking the substrate to find a derivation chain that supports it.

**Concrete changes:**

- New `src/aedos_v2/layer4_lookup/walker.py`: tier orchestrator. Order is U direct → W direct → derivation → fresh. Each step returns a verdict; on miss or below-threshold, fall through.

- New `src/aedos_v2/layer4_lookup/tier_w.py`: world cache successor to v1's `verification_cache.py`. Same shape, with the oracle resolution chain bolted on (literal SQL match → entity_equivalence → predicate_equivalence). TTL by stability class as in v1.

- New `src/aedos_v2/layer4_lookup/derivation.py`: BFS over (entity, predicate, polarity) triples. Bounds: `MAX_DEPTH = 4`, `MIN_CHAIN_RELIABILITY = 0.4`. Cycle detection via a visited-set on `(entity, predicate, polarity)` tuples. The walker consults `entity_taxonomy` (with `relation_type` filter) and `predicate_distribution` (with `relation_type` and `polarity`) for subsumption steps; consults `predicate_equivalence` and `entity_equivalence` at slot-comparison time within each step.

- New `src/aedos_v2/layer4_lookup/fresh.py`: classify-and-route entry to the verifier stack. Phase 7 connects this to whatever verifier scaffolding exists; full verifier ports are Phase 8 territory if needed.

- The `Decision` dataclass extends with `chain_reliability: float`, `derivation_path: list[dict]` (oracle rows consulted), `served_from_tier: 'u' | 'w' | 'derivation' | 'fresh'`. The `via` list is now populated for derivation paths with the full chain of oracle names.

- New pipeline events: `derivation_walk_attempt` (with depth, oracles consulted, min-link reliability, verdict), `derivation_walk_completed`, `derivation_walk_aborted_depth`, `derivation_walk_aborted_reliability`.

### What Phase 7 does NOT do

- No persisted derivations. The discipline is firm: derivation is a query operation; the result is computed fresh every time.
- No new oracles. The substrate stays closed at four.
- No Layer 5. Phase 7's output is the resolved verdict; Layer 5's planner (intervention selection) is Phase 8.
- No new patterns or routing methods.

### The directional asymmetry that matters in derivation

`predicate_distribution` consultations are the load-bearing piece. The walker must know:
- When walking *up* a `part_of` chain (Williamstown → Massachusetts), it consults `predicate_distribution(pattern, predicate, polarity, 'part_of')` and acts on `distributes_up` or `both`.
- When walking *down* an `is_a` chain (animal → cheetah), it consults `predicate_distribution(pattern, predicate, polarity, 'is_a')` and acts on `distributes_down` or `both`.
- A single chain can mix both: a derivation might walk down `is_a` then up `part_of`. Each step consults the oracle independently for that step's relation type.
- The directional-swap convention for `entity_taxonomy` (option b: each direction is its own row) means the walker may need to consult both `(child=X, parent=Y, relation_type)` and `(child=Y, parent=X, relation_type)` orderings during exploration. Bounded LLM cost; explicit doubling of oracle rows for fully-explored node pairs.

### Tests

- New: `tests/v2/test_derivation_walk.py` (~30 tests). The Williamstown/Massachusetts canonical case. Depth-bounded walks that hit the limit cleanly. Min-link floor rejection. Multi-oracle chains (entity_equivalence + entity_taxonomy + predicate_distribution). Chains where `predicate_distribution` returns `neither` and the walk falls through. Chains where `entity_taxonomy` returns `neither` and the walk falls through. Cycle detection (constructed cycles in the substrate must terminate cleanly). Polarity-flipping mid-walk (substrate says polarity inverts under some predicate equivalence — the walker must track polarity correctly through the chain).

- New: `tests/v2/test_derivation_corpus.py` — 25 end-to-end derivation scenarios. Each scenario specifies starting facts in U and W, oracle row preconditions, the query claim, and the expected verdict + derivation path.

- New: `tests/v2/test_walker.py` — combined tier-walk tests covering the U → W → derivation → fresh order.

- Existing: tier_u tests adjusted to call through the walker rather than directly.

- Smoke corpus: append 5-7 derivation entries with full `expected_via` chains. Include the Williamstown case, a multi-step is_a derivation (cheetahs-via-derivation), a part_of + is_a mixed-chain case, and at least two intentional MISS cases (derivation walks the substrate and finds no chain that supports the claim).

### Calibration

Phase 7 is the first phase where calibration runs end-to-end across the substrate. The 25-scenario corpus in `test_derivation_corpus.py` is the calibration target. Floor: 0.80 (lower than per-oracle floors because derivation chains compound oracle-level error). Each scenario specifies oracle row preconditions; Phase 7's calibration pre-populates those rows from the existing oracle calibration corpora rather than inventing new ones, ensuring the derivation walker is tested against the substrate as it actually calibrates.

### Rollback criteria

Disable the derivation tier in walker.py via a config flag. Walker reverts to U → W → fresh. The substrate tables remain populated but unconsulted. Clean.

### Done when

- The Williamstown/Massachusetts canonical case derives correctly and returns VERIFIED with the full derivation path in the `via` list.
- The 25-scenario derivation corpus passes ≥0.80.
- Depth-4 walks complete in <100ms warm-cache.
- Cycle detection works on constructed cyclic substrates.
- No derived facts in U or W (verified by post-test SELECT).
- All Phase 0-6 tests still pass.

### Phase 7 risk callouts

This is the most consequential single phase remaining. Risks worth managing:

1. **Chain reliability propagation.** Min-link is conservative but every oracle row consulted contributes. A 4-hop chain with one fresh row at 0.5 prior produces chain_reliability 0.5; with the floor at 0.4 this admits the chain. Verify the math against the architectural intent before shipping.

2. **Cycle detection on derived-equivalence loops.** Predicate equivalence + entity equivalence + entity taxonomy can produce loops in the explored substrate. The visited-set must be constructed correctly to prevent infinite walks. Test this explicitly with a synthetic cyclic substrate.

3. **Polarity tracking through chains.** `predicate_equivalence` can produce verdicts that flip polarity (the contradictory label). The walker must track polarity correctly through chains where polarity inverts mid-walk. Test this explicitly.

4. **Oracle write amplification.** The walker may consult oracle rows that don't exist yet, triggering LLM calls. A first-time derivation walk through unfamiliar substrate territory could produce many oracle writes. Bounded by memoization (every consulted row caches), but the cold-start cost of the *first* derivation in unfamiliar territory is real. Plan for this in cost projections.

5. **Smoke corpus dispatcher.** The smoke corpus has accumulated enough field variants across phases that Phase 9's parity check needs a structured dispatcher. Sketch this dispatcher in Phase 7 (or as a Phase 7.5 side task) so Phase 9 isn't a surprise.

---

## Phase 8 — Layer 5 confidence formula + observability

### Scope

Layer 5 (decision and response) was deferred through all prior phases. Phase 8 builds it.

**Concrete changes:**

- New `src/aedos_v2/layer5_decision/confidence.py`: implements `decision_confidence = path_prior × chain_reliability × evidence_strength`. Threshold T from `AEDOS_DECISION_THRESHOLD` env var (default 0.5). Path-prior table: Python ≈ 0.99, retrieval ≈ 0.85, user-authoritative ≈ 1.0.

- New `src/aedos_v2/layer5_decision/intervention.py`: 5-action planner (pass-through, replace, hedge, soften, noop). Keyed on the 8-state verification_status table from the architecture doc. No collapsing.

- New `src/aedos_v2/layer5_decision/corrector.py`: rewrite step. Takes the model's draft and the per-claim interventions; produces the corrected draft. Single LLM call.

- Operator-action endpoints to increment oracle row counts on disputed-row revaluation. POST endpoints:
  - `/v2/api/substrate/{oracle_name}/{key}/affirm` increments affirmed_count
  - `/v2/api/substrate/{oracle_name}/{key}/contradict` increments contradicted_count
  These are the *only* code paths that increment oracle counts. (Phase 7's derivation walker may also propagate contradictions; that's handled in Phase 7's contradiction-cascade logic, not here.)

- Trace UI panels: derivation path visualization (showing the full `via` chain with oracle row links), per-oracle row inspection panels, decision_confidence breakdown showing the three factors. The existing inspector endpoints from Phases 3-5 are reused.

- ARCHITECTURE.md sync (already done — it's Draft 4 above). CLAUDE.md v0.14 section.

### Smoke corpus dispatcher

If not done in Phase 7, build the smoke corpus dispatcher here. The corpus has these field variants by entry type:
- `expected_facts` + `oracles_consulted` — extraction-and-routing entries
- `expected_routing` + `expected_memo_state` — routing-memo entries
- `oracle_call` + `expected_label` — direct substrate entries
- `expected_via` + `future_match_via` — derivation-aware entries

The dispatcher inspects each entry's shape and runs the appropriate validation. This is the foundation for Phase 9's behavioral parity check.

### Tests

- New: `tests/v2/test_confidence_formula.py` (~20 tests). Three-factor product, threshold T, chain_reliability propagation through trace UI payload.
- New: `tests/v2/test_intervention_planner.py` (~25 tests). One per (verification_status, decision_confidence relative to T) combination, plus edge cases.
- New: `tests/v2/test_corrector.py` (~15 tests). Mocked LLM rewriter with each of the 5 action types.
- New: `tests/v2/test_substrate_operator_endpoints.py` (~12 tests). Affirm/contradict POSTs, count increment correctness, idempotency or lack thereof (decide and document).
- Existing: smoke corpus runs through the dispatcher on every test pass; this becomes the always-on regression check.

### Calibration

None. Layer 5 is rule-based intervention planning over the 8-state status table. The corrector's LLM call is opaque to calibration (it's a rewrite task with no ground-truth label).

### Rollback criteria

Revert the formula to direct `confidence_from_counts` output (skip the path_prior × evidence_strength factors). The 5-action planner stays as additive behavior. Trace UI panels stay (additive). Clean.

### Done when

- End-to-end smoke run through both stacks produces correct decision_confidence for verified, contradicted, derived, and unverifiable cases.
- Trace UI renders all four oracles inspectable, decision confidence broken down, derivation path visible.
- Operator can affirm/contradict an oracle row and see the count update.
- Smoke corpus dispatcher passes on all current entries.
- No behavioral regression on the cheetahs and Williamstown cases.

---

## What the substrate looks like as Phase 9 begins

*This section refreshes the "as Phase 6 begins" inventory above with the post-Phase-8 state. The original section is preserved as historical record; this is the authoritative current-state for the Phase 9 cutover work.*

Phases 6 through 8 shipped on the v0.14 branch (tags `v0.14-phase-6-complete`, `v0.14-phase-7-complete`, `v0.14-phase-8-complete`).

**Layer 1 (extraction)** — unchanged from Phase 5: 9-pattern extractor with free-form predicates within patterns. `POST /v2/api/extract` is the entry point.

**Layer 2 (routing)** — unchanged from Phase 5: validator + LLM router + `routing_memo` lookup-then-write at the (pattern, predicate) key.

**Layer 3 (substrate)** — unchanged at four oracles, all calibrated. Phase 8e added two operator-action helpers (`affirm_oracle_row`, `contradict_oracle_row`) on `classifier_base.py`; the four oracles' tables now have a documented mutation path through the `/v2/api/substrate/{slug}/{row_id}/{action}` POST endpoints.

**Layer 4 (lookup)** — complete:
- `tier_u.py` — Phase 6 session model + Phase 8d lookup-first refactor on stages 2 and 3 (cold cells under `llm=None` return None gracefully instead of raising).
- `tier_w.py` — Phase 7's world cache successor with the three-stage resolution chain + Phase 8d lookup-first refactor on stages 2 and 3.
- `derivation.py` — Phase 7's BFS engine + Phase 8c bounded-active classification budget (default 20 per walk; `predicate_distribution` is the only oracle the walker actively classifies during expansion).
- `walker.py` — Phase 7d tier orchestrator (U → W → derivation → fresh) with the refined Tier W fall-through table.
- `fresh.py` — Phase 7e dispatcher + Phase 8f stability-classifier wiring (replaces the hardcoded `decade_stable`; honors scope and volatile classes).

**Layer 5 (decision and response)** — Phase 8a-8b complete:
- `confidence.py` — three-factor `decision_confidence = path_prior × chain_reliability × evidence_strength` with env-driven `AEDOS_DECISION_THRESHOLD` (default 0.5). Tier W path_prior is derived heuristically from `stability_class` (immutable→0.99 python; else→0.85 retrieval); revisit in v0.15 with a `verifier_method` cache column if new writers land.
- `intervention.py` — 5-action planner (pass_through, replace, hedge, soften, noop) over the 8 verification_statuses × outcome × confidence-vs-T matrix. `pass_through` and `noop` are explicit so the trace UI sees every claim's resolution.
- `corrector.py` — single LLM call per turn; v1's `CORRECTOR_SYSTEM` prompt verbatim. Filters `pass_through`+`noop` out of the rewrite prompt; trace UI keeps the full intervention list.

**Storage** — schema unchanged from Phase 7 (no v0.14 migrations after Phase 7 closed). Phase 8 added pipeline event names: `derivation_walk_active_classification`, `derivation_walk_budget_exhausted`, `oracle_affirmed`, `oracle_contradicted`, plus reuse of `cache_scoping_decision` and `cache_stability_decision` from v1's vocabulary.

**Smoke corpus** — Phase 7+ entries renamed from `expected_tier_u_outcome` to `expected_walker_outcome` + `expected_served_from_tier`. Phase 0-6 entries kept the legacy field. The dispatcher accepts both vocabularies; assistant_lookup entries must carry at least one of the two fields.

**Operator endpoints** (Phase 8e) — 8 POSTs at `/v2/api/substrate/{slug}/{row_id}/{action}` where slug is one of `predicate-equivalence`, `entity-equivalence`, `entity-taxonomy`, `predicate-distribution` and action is `affirm` or `contradict`. NOT idempotent: each request increments by 1; operator UI debounces. Audit events fire on every action.

**Trace UI** — Phase 8.5 follow-up. Backend data is complete; the JS/CSS work was scoped to a focused phase rather than bundled into Phase 8.

**Test counts** (mocked; live API tests gated behind `RUN_API_TESTS=1`):
- v2: ~824 passed, 14 skipped (Phase 8 added ~138 tests over Phase 7's 686)
- legacy: 580 passed, 9 skipped — unchanged from pre-v0.14

**Disciplines that have held across all eight phases** and must continue to hold through Phase 9 cutover:
- Reads are not writes (counts only on independent external evidence — operator actions or contradiction propagation)
- No persisted derivations
- No collapsing the 8-state verification_status enum
- No lowercasing entity strings
- Plan-then-build per phase
- Phase-end commit on `v0.14` branch with tag `v0.14-phase-N-complete`
- Both `tests/v2/` and legacy `tests/` green at every phase boundary

**Resolved architectural decisions added in Phases 6-8** that Phase 9 inherits:
- Bounded-active classification budget on derivation walks (default 20). Phase 9 does NOT change this default; cutover preserves the architectural commitment to lazy substrate population.
- Tier U / Tier W stages 2 and 3 are lookup-first under `llm=None`. The lookup-first contract is part of the v0.14 architectural surface.
- Operator-action endpoints use row_id (not natural key) for URL uniformity. NOT idempotent.
- Stability classifier wired in for retrieval verdicts; volatile and non-world-fact scopes skip the cache write.
- Layer 5's decision matrix is keyed primarily on `verification_status` with `outcome` and `decision_confidence vs T` as secondary discriminators only for `verified` / `contradicted` / `user_asserted`.
- Vocabulary translation at corrector ledger time: `user_asserted` + REPLACE renders as `contradicted` for the LLM. The Intervention's verification_status field stays `user_asserted` in the audit trail.
- Tier W path_prior heuristic: derive from `stability_class` (immutable→0.99 python; else→0.85 retrieval). Revisit when new verifier types land in v0.15.

---

## Phase 9 — Cutover

### Scope

The behavioral and structural audit before v2 becomes the default.

- Final parity audit: legacy `tests/` and `tests/v2/` both pass.
- Smoke corpus dispatcher runs through both stacks (`/api/chat` and `/v2/api/chat`). Diff is zero, except the explicit improvements (cheetahs, Williamstown derivation) which v2 handles and v1 does not.
- Migrate operator UI default to /v2.
- Rename `src/` → `src/legacy/`, `src/aedos_v2/` → `src/`. Update imports.
- Delete `tests/` legacy, rename `tests/v2/` → `tests/`.
- Squash-merge v0.14 into main with a commit message detailing the architecture transition.
- Tag v0.14.0.

### Cutover criteria — all must hold

1. **Test parity.** v2 test suite ≥578 tests (matches or exceeds v1 count). All pass.
2. **Behavioral parity.** Smoke corpus dispatcher diff is zero except for the explicit improvements.
3. **Calibration.** Each oracle calibration corpus passes its floor (predicate_equivalence ≥0.90, others ≥0.85). Phase 7 derivation corpus ≥0.80.
4. **Performance.** Warm-cache turn latency on the v2 stack within 1.5× v1 stack on the smoke corpus.
5. **Observability.** Every pipeline_events stage from v1 has a v2 equivalent; trace UI parity check.
6. **Schema.** aedos_v2.db schema documented in CLAUDE.md; reset script exists.
7. **No silent kills.** The load-bearing-in-v1-but-silent-in-arch-doc list (SSE streaming, pipeline_events backbone, routing-anomaly path, verification_status enum, per-purpose model routing, microtheory session_id, inspector endpoints, static UI files, cache_invalidation_log, contradiction event surfacing, intervention types, UNIQUE_VALUE_SLOTS) — every item either is reproduced in v2 with file references or is explicitly marked deferred to v0.15+ in CLAUDE.md.

### Rollback criteria for cutover

If any of the seven criteria fail post-merge, revert the rename commit. The `src/legacy/` directory exists for one minor version (v0.14.x) before deletion in v0.15, so a hot rollback to v1 is possible by re-mounting `src/legacy/app.py` at `/`.

### Done when

All seven criteria pass, tag v0.14.0 is pushed, main reflects the new architecture.

---

## Cross-cutting notes for the remaining phases

**Calibration cadence.** Phase 7 is the only remaining phase with a calibration step. Phases 6, 8, and 9 have unit tests and parity checks but no LLM-classifier calibration. The frequentist counts on oracle rows continue to grow only via operator action (Phase 8 endpoints) and contradiction propagation (Phase 7+).

**Pipeline events parity.** Every event v1 emits, v2 emits with the same stage name. Remaining additions across Phases 6-8: `derivation_walk_attempt`, `derivation_walk_completed`, `derivation_walk_aborted_depth`, `derivation_walk_aborted_reliability`, plus operator-action events for substrate row affirmation/contradiction.

**Test count targets.** Phase 6 adds ~25 tests. Phase 7 adds ~70 tests (substrate consumer integration is the heaviest single addition). Phase 8 adds ~70 tests. Phase 9 is parity audit plus rename — no new test files. Final v2 suite ≈660 tests (v1's 578 plus ~80 substrate, ~80 derivation, ~15 session model).

**API spend.** Phase 7's calibration is the major remaining LLM expense. The 25-scenario derivation corpus pre-populates oracle rows from existing calibration data, but a derivation walk still consults multiple oracles per step. Project ~150-200 LLM calls for Phase 7 calibration. Phases 6, 8, and 9 are negligible API spend (test suites are mocked except for periodic re-runs of existing calibrations).

**The "phase plan, then build" discipline continues.** Every phase opens with Claude Code reading the architecture document, the relevant code from prior phases, and proposing a plan. The user reviews, surfaces ambiguities, signs off. Work proceeds to phase-end commit. The plan-then-build pattern has caught real architectural issues in every phase so far; do not skip it.

**The smoke corpus is documentation accreting toward a parity check.** Every phase appends entries demonstrating the phase's contribution. Phase 9 runs the corpus through both stacks. Sketching the dispatcher early (Phase 7 or 8) prevents Phase 9 surprises. The corpus is in a good shape now but its dispatcher does not yet exist — that's the thing to build.

**The architecture document is the source of truth for what persists.** When implementation reveals a tension between the architecture and what the code is doing, surface it for resolution before continuing. Architectural drift is the failure mode this discipline exists to prevent.

---

## Phase 8.6 — Extractor and storage-path bug fixes (interstitial)

*Tagged `v0.14-phase-8.6-complete`. Three bugs surfaced by real-world testing of the Phase 8.5 trace UI; all fixed before Phase 9 cutover.*

The bugs share an architectural theme: the extractor's projection of source text into structured claims diverges from what the source actually asserts, and downstream layers don't catch the divergence. Each fix restores an architectural commitment the implementation had quietly violated.

**Bug 1 — interrogative confabulation (the strawberry case).** A user asking "How many r's are in strawberry?" got extracted as `quantitative.has_count(value=2)` with a confabulated value, the python verifier corrected to 3, and the corrected fact got matched on subsequent turns and badged as "served from user_store". Two-layer fix shipped in 8.6a:

- Extractor prompts (v1 + v2) gained a discriminating user-side abstain few-shot for letter/word/character/digit-counting questions, plus a contrast example confirming declarative-with-value still extracts verbatim.
- `store_lookup_verify` (v1) gained an `asserted_by="user"` filter on `find_currently_valid` and `find_contradictions`. Tier 2 of the verification stack is the user microtheory — only user-asserted facts qualify. Pre-fix, python_verifier-asserted corrected values silently matched as if user-asserted; post-fix they're invisible at Tier 2 and the model claim falls through to fresh verification or Tier 3 (verification_cache) cleanly. The dual-write storage path (user's wrong version + python_verifier corrected version) is **untouched** — it preserves the audit trail of "user said X but verification produced Y". The lookup filter is the only storage-side change.

**Bug 2 — session-marker stripping (the Williamsburg case).** A user message "Let's say for this conversation I live in Williamsburg" got stored cross-session because the extractor's source_text projection stripped the marker phrase down to "I live in Williamsburg", and the v1 router's `is_session_scoped` check ran on the projection rather than the raw turn text. Fixed in 8.6b:

- `Router.route()` accepts a `raw_text` parameter that threads through `_route_user`, `_route_model`, `_route_user_world_claim`, `_dispatch_method`, and every per-method handler down to `_store`. `_store` uses `raw_text` when provided; falls back to `claim['source_text']` otherwise (backwards-compatible default). `Pipeline._stage_user_side` passes `user_message` as `raw_text` on every user-side `route()` call.
- v2's `tier_u.store_user_fact()` got the same `raw_text` parameter with `source_text` fallback. No production caller wires it yet (Phase 9's chat endpoint will), but the function is test-driven and the contract is now correct ahead of cutover.

**Bug 3 — tautological is_a (the waggle-dance case).** The extractor produced `is_a(entity="waggle-dance communication system", category="communication system")` — a vacuous tautology where the category is a suffix of the entity. The router routed it to `unverifiable` correctly, so no harm verdict-side, but the noise cluttered the trace. Fixed in 8.6c:

- Extractor prompts (v1 + v2) gained a "Tautological is_a guards" section with five contrast few-shots covering the canonical waggle-dance suffix case, the explicit-form "X is a [suffix-of-X]" case, the single-token equality case, and contrast cases that should still extract (real categorical, substring-not-suffix).
- v2 validator gained a fifth invariant — `categorical_tautology`. Catches `entity == category` OR `entity ends with " " + category` (case-insensitive, whitespace-collapsed). Pure substring matches that aren't suffixes ("President of the United States" / "President") do NOT flag — the leading-space requirement enforces "at least one modifier preceding category". v1 has no equivalent validator step; v1 is being retired in Phase 9 and the prompt fix is sufficient for v1's remaining lifetime.

### Test counts after Phase 8.6

- **v1 (legacy):** 589 passed (was 588) + 4 live-API gates (gated behind `RUN_API_TESTS=1`).
- **v2:** 844 passed (was 831) + 4 live-API gates.

### Live-API calibration discipline

Phase 8.6a established a discipline: extractor prompt changes that target real-LLM behavior must be calibration-gated on live API runs, not just mocked unit tests. The mocked tests prove the prompt parses correctly; only a live run proves the LLM follows the new few-shots. Each new extractor abstain rule shipped in 8.6 includes a paired live-API gate (`RUN_API_TESTS=1`) that must pass on first live run. If it doesn't, the prompt iterates until it does — same discipline as Phase 3-5's substrate calibration.

The 8.6c tautology rule required one prompt iteration before passing: the first version handled the implicit "noun phrase enables…" case but missed the explicit "X is a [suffix-of-X]" sentence form. Two contrast few-shots resolved it.

---

## Deferred to v0.15+

*Architectural conversations and known issues that are out of scope for v0.14.0 but worth capturing so they aren't lost.*

**Layer 1.5 faithfulness validator (architectural conversation, not work).** The three Phase 8.6 bugs share a structure: the extractor's projection of source text into structured claims diverges from what the source actually asserts, and downstream layers don't catch the divergence. Today the only post-extraction check is Layer 2's structural validator (required slots, mereological part≠whole, etc.), which doesn't compare the extracted claim against the source for representational faithfulness. A Layer 1.5 faithfulness validator would close that loop — but it requires real architectural thought about its abstention boundary (when does "the extractor projected something the source didn't assert" become a graded judgment vs a binary check?), where it sits in the layer model (between extraction and routing? folded into Layer 2?), and what its calibration corpus looks like. This is a v0.15+ conversation, not work; the Phase 8.6 fixes are the right v0.14 response to the bugs that surfaced.

**v1 user-world-claim dual-write storage path (preserved as-is).** When the user asserts a verifiable world claim and python verification contradicts it, v1's router stores both the user's wrong version (`asserted_by="user", verification_status="contradicted"`) and the corrected version (`asserted_by="python_verifier", verification_status="verified"`). This dual-write preserves the audit trail — "the user said X but verification produced Y" — that the chat-prompt builder, corrector, and trace UI rely on. Phase 8.6's `asserted_by="user"` lookup filter on `store_lookup_verify` makes the python_verifier row invisible at Tier 2, fixing the misleading "served from user_store" badge that was the surface bug. The dual-write itself is correct; future engineers should not "simplify" it away. v2 doesn't replicate this path because v2's storage layers (Tier U user microtheory + Tier W verification cache) separate the concerns architecturally — user assertions and verifier outputs go to different stores by design.

**v1 retirement timeline.** `src/legacy/` (the renamed `src/`) exists for one minor version (v0.14.x) after Phase 9's cutover. Hot rollback to v1 is possible during that window by re-mounting `src/legacy/app.py` at `/`. v0.15 deletes `src/legacy/` — at which point any v0.14 quirks that survived the cutover (notably v1's lack of a categorical_tautology validator) are removed by removal. Document anything in v1 that v2 deliberately doesn't replicate so the v0.15 deletion doesn't lose information.