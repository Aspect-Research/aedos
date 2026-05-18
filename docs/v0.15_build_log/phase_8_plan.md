# Phase 8 Plan — Layer 5 + Substrate-Internal Consistency Check

## Goal

Verification result aggregation (VerificationResult dataclass). Substrate-internal consistency
check (three inconsistency classes, retract-both, circuit breaker). Retraction propagation.
Downstream contradiction tracing. End-to-end pipeline complete.

## What's built

### `src/aedos_v0_15/layer5_result/aggregator.py`

`VerificationResult` dataclass:
- `claims_extracted: list[Claim]`
- `per_claim_verdicts: dict[str, str]` — claim_id → verdict
- `per_claim_traces: dict[str, JustificationTrace]` — claim_id → trace
- `aggregate_metadata: dict` — counts of verified/contradicted/abstained, walk depths, llm_calls, source breakdown
- `audit_log_entries: list[int]`
- `text_input: dict`
- `consistency_warnings: list[dict]`

`Aggregator.aggregate(claims, per_claim_results) -> VerificationResult`:
- Builds per_claim_verdicts/traces maps
- Computes aggregate_metadata summary
- Collects consistency_warnings if any walk result has abstention_reason == "circuit_breaker_triggered"

### `src/aedos_v0_15/layer3_substrate/consistency.py`

`ConsistencyResult` dataclass:
- `status: str` — "pass" | "conflict"
- `inconsistency_class: Optional[str]` — "transitive_equivalence_violation" | "contradicting_subsumption" | "conflicting_distribution"
- `row_a_id: Optional[int]`, `row_b_id: Optional[int]`
- `table: Optional[str]`
- `details: dict`

`ConsistencyChecker(db, audit_log=None, config=None)`:
- `check_on_write(table, row_id) -> ConsistencyResult`:
  - predicate_translation: check for same aedos_predicate mapping to same kb_property but different slot_to_qualifier; or same (aedos_predicate, kb_namespace) pair implying conflict
  - subsumption/predicate_distribution: UNIQUE constraints prevent most conflicts; check_on_write mainly detects transitive equivalence violations
- `check_periodic() -> list[ConsistencyResult]`:
  - Scan all active (non-retracted) rows across predicate_translation for same (kb_property, kb_namespace) mapped by different aedos_predicates with incompatible slot_to_qualifier
  - Scan subsumption for same entity-pair with conflicting verdicts
  - Scan predicate_distribution for same (predicate, polarity, relation_type) with conflicting verdicts
- `resolve_conflict(conflict) -> None`:
  - Retract both rows (set retracted_at, retraction_reason="consistency_check:{class}")
  - Log to audit_log
  - Upsert consistency_circuit_breaker: increment cycle_count
  - If cycle_count >= threshold (default 3): set unresolvable=1, log circuit_breaker_triggered

### `src/aedos_v0_15/layer5_result/retraction.py`

`VerdictRetraction` dataclass:
- `claim_id: str`, `verdict: str`, `retracted_row_id: int`, `retracted_table: str`, `retracted_at: str`

`RetractionPropagator(db, audit_log=None)`:
- `propagate_retraction(table, row_id) -> list[VerdictRetraction]`:
  - Scans verdict_traces table (lightweight: stores claim_id + json-serialized set of row references)
  - Returns list of VerdictRetraction objects
  - In-memory implementation for Phase 8 (verdict_traces populated during aggregation)
- `record_verdict_trace(claim_id, trace) -> None`:
  - Serialize trace's contributing source IDs into a lookup structure for retraction scanning

### `src/aedos_v0_15/layer5_result/contradiction_tracer.py`

`ContradictionTracer(db, audit_log=None, retraction_propagator=None)`:
- `trace_contradiction(contradicted_verdict_id, contradicting_premise) -> list[VerdictRetraction]`:
  - Look up verdict's trace
  - Identify contributing substrate rows
  - Retract those rows via retraction_propagator
  - Return list of retracted verdicts

## Ambiguities resolved

1. **verdict_traces storage**: No separate DB table; RetractionPropagator maintains an in-memory
   dict mapping claim_id → set of (table, row_id) pairs during a session. Between sessions this
   is empty (retraction propagation is session-local in Phase 8). Full persistence via audit_log
   is Phase 10.
2. **circuit breaker question_signature**: `"{table}:{predicate_or_entity_pair}"` — deterministic
   key identifying the substrate question that keeps conflicting.
3. **aggregate_metadata "abstained" key**: "abstained" = claims with verdict "no_grounding_found".
