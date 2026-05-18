# Phase 5 Plan — Subsumption + Predicate Distribution Oracles

**Goal.** Two remaining substrate oracles. SubsumptionOracle: KB-mediated (both entities KB-resolved → delegate to kb_protocol), substrate-row (mixed/Aedos-only → DB lookup or LLM generation), retraction. PredicateDistributionOracle: four-verdict enum (distributes_up/down/both/neither), lookup-first, LLM cold-cache. Substrate facade wires all four components.

**Dependencies.** Phase 0 (DB schema: subsumption + predicate_distribution tables), Phase 2 (oracle pattern: cold-cache LLM generation), Phase 4 (KB protocol subsumption).

---

## What gets built

### 1. `src/aedos_v0_15/layer3_substrate/subsumption.py`
- `EntityRef` dataclass: `namespace: str`, `identifier: str`
- `SubsumptionVerdictType` enum: `a_subsumed_by_b | b_subsumed_by_a | equivalent | unrelated`
- `SubsumptionVerdict` dataclass: `verdict`, `source` (kb|substrate|llm_generated), `row_id`, `reason`, `traversal_chain`
- `SubsumptionOracle.consult(entity_a, entity_b, relation_type)` — three-path resolution priority
- `SubsumptionOracle.retract(row_id, reason)` — soft-delete with audit log
- `SubsumptionOracle.query_neighbors(entity_a, relation_type)` — return rows involving entity_a

### 2. `src/aedos_v0_15/layer3_substrate/predicate_distribution.py`
- `DistributionVerdictType` enum: `distributes_up | distributes_down | both | neither`
- `DistributionVerdict` dataclass: `verdict`, `reason`, `row_id`, `was_cached`
- `PREDICATE_DISTRIBUTION_TOOL` tool schema for LLM generation
- `PredicateDistributionOracle.consult(predicate, polarity, relation_type)` — lookup-first, LLM cold-cache
- `PredicateDistributionOracle.retract(row_id, reason)`
- `PredicateDistributionOracle.query_neighbors(predicate, relation_type)` — return rows for same predicate

### 3. `src/aedos_v0_15/layer3_substrate/__init__.py` — Substrate facade
- `Substrate` dataclass wiring all four components: `resolver`, `predicate_translation`, `subsumption`, `predicate_distribution`
- Uniform access pattern for the walker (Phase 6)

### 4. Tests (~60 new)
- `tests/v0_15/unit/test_subsumption_oracle.py` — KB-mediated, substrate-row, cold-cache, retraction, query_neighbors
- `tests/v0_15/unit/test_predicate_distribution_oracle.py` — cold/warm cache, all four verdicts, retraction
- `tests/v0_15/integration/test_substrate_complete.py` — Substrate facade, cross-oracle consistency

### 5. Calibration corpora (authored, not executed)
- `tests/v0_15/calibration/subsumption_corpus.jsonl` — 60 cases
- `tests/v0_15/calibration/predicate_distribution_corpus.jsonl` — 50 cases

---

## Ambiguities (pre-resolved)

1. **KB-mediated vs substrate-row priority:** KB-mediated wins unconditionally when both entities have KB identifiers. The substrate row is never consulted in the KB-mediated case — the KB is the source of truth. Substrate rows are only for non-KB entities.
2. **Verdict storage for KB-mediated:** KB-mediated subsumptions are NOT stored as substrate rows (KB is the source; caching here would duplicate the entity_resolution_cache concern). Return result with `source="kb"` and no row_id.
3. **LLM tool for distribution oracle:** Uses `extract_with_tool` with a structured JSON tool (same pattern as predicate_translation). Returns `verdict` + `reason` fields.
4. **EntityRef namespace convention:** Wikidata entities use `namespace="wikidata"`, Aedos-only entities use `namespace="aedos"`.
5. **Substrate facade shape:** `Substrate` is a plain dataclass, not a class with methods. Walker access: `substrate.predicate_translation.consult(...)` etc.
