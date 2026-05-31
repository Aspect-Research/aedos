# Phase 3 Plan — Routing (Layer 2) + Tier U

## Summary

Phase 3 produces: (1) the router that maps claims to routes based on predicate metadata, (2) the validator that checks structural invariants, (3) Tier U with write path, three-stage lookup, and temporal scope handling. KB-resolvable and Python routes are stubbed — they route correctly but do not execute verification.

## File list

### Modified
- `src/aedos_v0_15/layer2_routing/router.py` — `Router`, `RoutingDecision`
- `src/aedos_v0_15/layer2_routing/validator.py` — `Validator`, `ValidationResult`
- `src/aedos_v0_15/layer4_sources/tier_u.py` — `TierU`, `WriteResult`, `LookupResult`

### New tests
- `tests/v0_15/unit/test_router.py`
- `tests/v0_15/unit/test_tier_u.py`
- `tests/v0_15/integration/test_routing_to_tier_u.py`

### New calibration corpus
- `tests/v0_15/calibration/temporal_scope_corpus.jsonl` — ~40 cases

## Test plan

Target: ~70 new tests (cumulative ~309).

| Module | Coverage | Count |
|---|---|---|
| test_router.py | All 4 routes, anomaly cases, cold-cache trigger | ~20 |
| test_tier_u.py | Write (insert, idempotent, contradiction), 3-stage lookup, temporal, retraction | ~35 |
| test_routing_to_tier_u.py | End-to-end claim → route → Tier U write → lookup | ~15 |

## Ambiguities (to be resolved)

See `docs/v0_15/phase_3_ambiguities.md`.

## Architecture decisions this phase

- KB-resolvable and Python routes return a `RoutingDecision` with route set but `stub=True`; no verification is performed.
- Entity resolver is a stub in Phase 3: `resolve_entity` is not called; stage 2 broadening is a no-op.
- Contradiction is defined narrowly: same (asserting_party, subject, predicate, polarity) with different object. Subject-predicate-different-polarity pairs are treated as separate rows (one asserted, one negated).
- `TierU.write()` is idempotent: exact same content returns existing row without INSERT.
- Temporal scope at read: rows with `valid_until='before_present'` or `valid_until < current_time` are marked historical.
