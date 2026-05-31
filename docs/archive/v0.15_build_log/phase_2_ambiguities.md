# Phase 2 Ambiguities — Predicate Translation Oracle

## Ambiguity 1: UNIQUE constraint is (aedos_predicate, kb_namespace) — what about null kb_namespace?

**Resolution:** SQLite treats NULL as distinct from every other NULL in UNIQUE constraints by default. So (predicate, NULL) and (predicate, NULL) would both insert without violating UNIQUE. To avoid duplicate null-namespace rows, the `consult()` method checks for an existing row with `WHERE aedos_predicate=? AND kb_namespace IS NULL` before inserting when `kb_namespace` is None. If a row exists (regardless of NULL behavior), it is returned.

## Ambiguity 2: What is the fallback when the LLM returns a malformed response?

**Resolution:** `consult()` raises `PredicateTranslationError(predicate, "generation_failed", details)`. The caller (router, Phase 3) catches this and routes the claim to abstain. No partial row is stored. The failure is logged to the audit log with event_type="row_generation_failed".

## Ambiguity 3: Does `retract()` propagate to downstream verdicts in Phase 2?

**Resolution:** No. Downstream retraction propagation (through justification traces) requires the walker (Phase 6) and retraction engine (Phase 8). In Phase 2, `retract()` only sets `retracted_at` on the row and logs the event. Phase 8 will wire up the propagation.

## Ambiguity 4: Should `used_count` and `last_consulted_at` be updated on every warm-cache hit?

**Resolution:** Yes. Every call to `consult()` that finds a usable row (non-retracted) increments `used_count` and sets `last_consulted_at`. This is observability metadata only (per architecture §5.2) and does not affect decisions.

## Ambiguity 5: What routing_hint should the oracle assign to unknown predicates with no KB coverage?

**Resolution:** "abstain". The conservative default: if the oracle cannot confidently identify a route, abstain is safer than a wrong route. The LLM prompt explicitly instructs to choose "abstain" over speculative "kb_resolvable" when the predicate has no clear KB mapping.
