# Phase 2 Plan — Predicate Translation Oracle

## Summary

Phase 2 produces the predicate translation oracle: `PredicateTranslation.consult(aedos_predicate)` returns `PredicateMetadata` from DB cache, or generates it via a single LLM call and stores it. Retraction sets `retracted_at`; retracted rows are excluded from future consultations. `query_neighbors` returns rows that could conflict with the given predicate (same `kb_property`).

The oracle does NOT route claims (that is Phase 3). The oracle only answers "what is this predicate's metadata?".

## File list

### Modified
- `src/aedos_v0_15/layer3_substrate/predicate_translation.py` — `PredicateTranslation` class, `PredicateMetadata` dataclass, `PREDICATE_METADATA_TOOL` schema

### New tests
- `tests/v0_15/unit/test_predicate_translation.py`

### New calibration corpus
- `tests/v0_15/calibration/predicate_metadata_corpus.jsonl` — ~80 cases

## Test plan

Target: ~50 new tests (cumulative ~250 including Phase 1's 200).

| Class | Coverage | Count |
|---|---|---|
| TestPredicateMetadataDataclass | Fields, types, optional fields | ~5 |
| TestConsultColdCache | LLM call triggered, row stored, metadata returned | ~8 |
| TestConsultWarmCache | No LLM call on second consult, same result | ~5 |
| TestRetraction | retracted_at set, excluded from future consult | ~8 |
| TestQueryNeighbors | Returns conflict candidates, empty when no conflicts | ~5 |
| TestAuditLog | Creation event, retraction event logged | ~5 |
| TestErrorHandling | Malformed LLM response, missing required fields, graceful fallback | ~8 |
| TestToolSchema | Tool schema has correct fields and enums | ~3 |
| TestRouting | Each routing_hint value (user_authoritative, python, kb_resolvable, abstain) | ~5 |

## Calibration corpus adversarial-coverage strategy

~80 cases across 5 sub-categories:

**User-authoritative predicates (20 cases):** Include 5 adversarial cases: predicates like "prefers" that sound factual but are authoritative, predicates that vary by deployment ("rating" could be kb_resolvable in one deployment but user_authoritative in another), and predicates with `user_subject_required=1` that should be enforced.

**Python-routed predicates (15 cases):** Include 3 adversarial: predicates that look quantitative but aren't (e.g., "is similar to" should not be python), and predicates where the Python computation depends on the object_type being `quantity` vs `entity`.

**KB-resolvable predicates (30 cases):** Include 8 adversarial: predicates with non-obvious Wikidata P-numbers, predicates that map to the same KB property but different slot_to_qualifier structures (these are the consistency-check trigger cases), and predicates with temporal qualifiers (P580, P582 for start/end time).

**Abstain-routed predicates (10 cases):** Include 3 adversarial: predicates that sound kb_resolvable but lack KB coverage ("metaphorically represents", "symbolizes"), modal predicates ("could be", "might have"), causal predicates where causation isn't KB-encoded.

**Ambiguous/deployment-dependent (5 cases):** Predicates that could route differently depending on context. These test oracle behavior under underspecified input.

## Architecture decisions this phase

- `consult()` always uses `kb_namespace=None` as the primary key. If a row exists for (predicate, None), it is returned even when a specific kb_namespace is requested — the namespace is for future specialization.
- On LLM generation failure, `consult()` raises `PredicateTranslationError` rather than returning a fake row. The router in Phase 3 will catch this and route to abstain.
- `retract()` does not delete the row; it sets `retracted_at`. The row remains for audit purposes and `query_neighbors` may still return it.
- `query_neighbors` returns ALL rows with matching `kb_property` including retracted ones; the caller decides whether to act on retracted neighbors.
