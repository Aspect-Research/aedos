# v0.16.2 — Entity-vs-literal contradiction guard (§3.2)

## Symptom
`Pope Francis birth_name Jorge Mario Bergoglio` returned **CONTRADICTED**, with the
note "the source indicates Jorge Mario Bergoglio instead" — the contradicting value
was the *exact same string* as the claim.

## Root cause
`birth_name` was classified `object_type="entity"`. In `KBVerifier.verify`, object
resolution runs only for entity-typed predicates (kb_verifier.py:278), so the object
"Jorge Mario Bergoglio" was resolved to the **person entity** (a Q-id) — that name
*is* a Wikidata entity (Pope Francis himself). The KB's P1477 birth-name value is the
**literal** string "Jorge Mario Bergoglio" (the adapter tags non-URI values
`"literal"`, kb_wikidata.py:815). The verifier then compared **Q-id vs literal
string** → no match → and because the predicate is functional (single-valued), the
mismatch escalated to CONTRADICTED. Both sides print as the same text.

The existing S3 guard (`_contradiction_value_type_ok`) **permits** a `literal` KB
value to contradict an `entity` predicate — that allowance exists for
literal-vs-literal external-id compares (e.g. ISBN), where the object did NOT resolve.
It did not exclude the **resolved-entity-vs-literal** cross-kind case.

This was a two-sided §3.2 violation: a false-**contradict** on the positive claim,
and (via `_apply_polarity`) a false-**verify** on the negation ("X's birth name is
NOT Jorge…" would have inverted CONTRADICTED→VERIFIED).

## Fix (general, across the project)
A CONTRADICTED requires **same-kind operands**. In `_compare_positive`'s
single-valued block, after S3:

```python
if value_resolved and not _is_entity_value(scope_mismatch):
    return KBVerdictType.NO_MATCH, None, "entity_claim_vs_literal_value"
```

`value_resolved` is True only when `object_type=="entity"` **and** the object
resolved to a Q-id, so `expected_value` is an entity. If the contradicting KB
statement's value is not itself an entity, the comparison is resolved-entity-vs-
literal — never a sound contradiction — so abstain.

```python
def _is_entity_value(stmt) -> bool:
    vt = getattr(stmt, "value_type", None)
    if vt == "entity": return True
    if vt: return False                     # literal/date/quantity/… = non-entity
    v = getattr(stmt, "value", None)         # untagged → fall back to Q-id surface
    return isinstance(v, str) and _QID_RE.fullmatch(v.strip()) is not None
```

It's placed **after** S3 so S3 keeps its informative reason for datatype mismatches
(date/quantity vs entity); this guard catches only the case S3 *allows*
(literal vs resolved-entity).

## Why it's sound
- Only ever turns a would-be CONTRADICTED into NO_MATCH on the positive path — it
  **cannot** introduce a false-verify or a new false-contradict; via `_apply_polarity`
  the negated claim becomes NO_MATCH too (closing the negation false-verify).
- Fires only on the genuine mis-mapping (object resolved to a Q-id, KB value literal).
  Correctly-literal predicates (ISBN, names with `object_type` ≠ entity) have
  `value_resolved=False`, so the guard never fires and literal-vs-literal comparison
  is unchanged. Mis-mapped-but-unresolved objects already abstain via the N1
  value-unresolved guard.
- The untagged-value_type fallback (Q-id surface pattern) keeps a real entity value
  that the adapter left untagged from being misread as a literal — so a genuine
  entity-vs-entity contradiction is preserved (control test).

## Tests / verification
- `tests/unit/test_kb_verifier.py::TestKBVerifierValueTypeGuard`:
  `test_resolved_entity_vs_literal_abstains_not_contradicts` (the birth_name case →
  NO_MATCH) + `test_resolved_entity_vs_untagged_qid_value_still_contradicts`
  (untagged Q-id still contradicts — no over-abstention).
- Existing `test_type_mismatched_statement_abstains_not_contradicts` /
  `test_compatible_type_still_contradicts` unchanged (S3 still owns the
  datatype-mismatch reason; entity-vs-entity still contradicts).
- Full offline gated suite: **1723 passed**, 1 xfail, 1 xpass (pre-existing
  sandbox boundaries).
