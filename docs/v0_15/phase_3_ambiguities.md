# Phase 3 Ambiguities — Routing + Tier U

## Ambiguity 1: How does Router handle PredicateTranslationError?

**Resolution:** If the oracle raises `PredicateTranslationError`, the router returns `RoutingDecision(route="abstain", anomaly_reason="predicate_translation_failed")`. This is logged to the audit log. The claim is not processed further.

## Ambiguity 2: What counts as a contradiction in Tier U?

**Resolution:** Two rows contradict when they have the same (asserting_party, subject, predicate) but different object values, and neither is retracted, and at least one is currently valid. Opposite-polarity rows (polarity=1 and polarity=0 for the same subject/predicate/object) are also contradictions — one asserts, the other denies the same fact. When a contradiction is detected on write, the old row's `valid_until` is set to `asserted_at` of the new row, and the new row is inserted.

## Ambiguity 3: For temporal scope at read, what is the "current time" parameter?

**Resolution:** `current_time` is passed in as an ISO8601 string by the caller. TierU does not call `datetime.now()` internally — all time-dependent behavior is externally controlled via the parameter. Default is `datetime.now(UTC).isoformat()` at the call site. This makes the lookup fully deterministic in tests.

## Ambiguity 4: Does predicate-translation broadening (stage 3) call the LLM?

**Resolution:** Stage 3 calls `oracle.consult(predicate)` which may trigger an LLM call on a cold cache. In tests, the oracle is always pre-seeded (or uses a mock LLM). If the oracle raises PredicateTranslationError, stage 3 is skipped (no broadening) and the lookup returns whatever stage 1 found.

## Ambiguity 5: What is the `source_context` field in tier_u?

**Resolution:** `source_context` stores the ExtractionContext fields as a JSON string (asserting_party, turn_id, document_id). It is for observability only; it does not affect lookup logic.
