# v0.16.2 — Resolved-entity name-match (the famous-entity QID tangle) §3.2

## Symptom
`Tokyo capital_of Japan` (or `Japan capital Tokyo`) returned **CONTRADICTED**, with
the contradicting `source_value=Q1490` — Tokyo's own QID, the *correct* answer. The
KB found the right capital and the system still called the claim false. Paris
verified on the same predicate.

## Root cause (entity ambiguity on the value slot)
Confirmed against live Wikidata:
- Japan (Q17) `P36` (capital) = **Q1490** (Tokyo).
- Q1490's `P31` types are *prefecture of Japan / megacity / metropolis / global
  city / capital of Japan …* — **not** `city` (Q515).
- `wbsearchentities("Tokyo")` returns **Q7473516 (the special-wards "Tokyo") first,
  Q1490 second**.

So when resolving the claim's value "Tokyo" with the `capital`/P36 value-slot
**type filter** (which expects a city-like type), Q1490 is excluded and a different
same-named QID (e.g. Q7473516) is selected. The verifier then compared
`Q7473516 ≠ Q1490`, and since `capital` is functional (single-valued), the mismatch
escalated to CONTRADICTED — a §3.2 false-contradict (and, via `_apply_polarity`, a
false-verify on the negation "X's capital is not Tokyo"). Paris is clean (Q90 is the
primary, city-typed "Paris"), which is why it verified and Tokyo didn't.

## Fix (general — a resolved-entity mismatch that names the KB value is not a conflict)
In `KBVerifier.verify`, immediately after `_compare_positive`:

```python
if (pos_verdict == CONTRADICTED and meta.object_type == "entity" and value_resolved
        and statement is not None
        and self._value_surface_names_kb_entity(expected_ref, statement.value)):
    pos_verdict = VERIFIED          # the KB value IS what the claim names
    entity_name_match = True
```

`_value_surface_names_kb_entity(surface, kb_value)` returns True iff the KB value is
an entity Q-id whose **canonical label** (via the adapter's `fetch_label`) equals the
claim's value **surface form** (trimmed, case-folded). `expected_ref` is the value
slot's surface — the claim object for a standard predicate, the claim subject for an
inverse predicate (`capital_of`).

Rationale (de-dicto): the claim asserts "&lt;subject&gt; &lt;predicate&gt; &lt;name&gt;", and the KB
confirms the single-valued answer is the entity *named* &lt;name&gt;. The resolver merely
selected a different same-named QID; the KB value is the very entity the claim names,
so the claim — as stated, by name — is true → VERIFIED.

### Critical discriminator (caught by adversarial review)
The first cut fired on **every** `_compare_positive` CONTRADICTED return — including
the **E4 "present-tense role ended"** path, where `statement = matched_ended` is a
statement whose value ALREADY value-matched the claim (the contradiction is about
temporal *currency*, not value). Its label equals the surface, so the name-match
flipped the correct CONTRADICTED → VERIFIED — re-opening the wrong-pope/ended-role
hole ("Obama is the President of the United States" → VERIFIED in 2026). The fix:
only rescue a **value-MISMATCH** contradiction, never a matched-value one —

```python
and not _value_matches(getattr(statement, "value", None), expected_value)
```

placed BEFORE the label fetch (so matched-value contradictions short-circuit). For
the Tokyo QID tangle the values differ (Q1490 ≠ Q7473516) → discriminator passes →
rescued. For the E4 ended role the value matched (Q11696 == Q11696) → discriminator
blocks → CONTRADICTED stands.

## Why it's sound
- Fires only on an **exact normalized label match** between the KB value and the
  claim's value surface form, so a genuine name mismatch (Honolulu vs New York City;
  Kyoto vs Tokyo) never matches — the functional contradiction stands.
- The disjoint and no-statements fallback CONTRADICTED paths **return early**, before
  this guard, so it only ever sees `_compare_positive`'s value-match `scope_mismatch`;
  for geographically disjoint entities the labels differ, so it won't fire there.
- Polarity: a negated claim's positive content VERIFIES, then inverts to CONTRADICTED
  ("...is NOT Tokyo" → false) — closing the paired negation false-verify too.
- **Fail-closed**: a non-entity KB value, an adapter without `fetch_label`, an empty
  label, or any fetch error leaves the verdict untouched. Offline-safe — `fetch_label`
  uses `_fixture_label` when not live (no network), so the gated suite's verdicts are
  unchanged (the guard fails closed without a fixture label).

## Tests / verification
- `tests/unit/test_kb_verifier.py::TestEntityNameMatch`:
  the QID-tangle case (Tokyo → VERIFIED + `entity_name_match` trace flag); a genuine
  name mismatch (Kyoto → still CONTRADICTED); the negation (→ CONTRADICTED); the
  fail-closed no-label case (→ CONTRADICTED); and **the ended-role-with-matching-label
  case (→ stays CONTRADICTED)** — the §3.2 regression guard for the discriminator. The
  test `MockKB` gained a `labels`/`fetch_label` surface.
- Full offline gated suite: **1728 passed**, 1 xfail, 1 xpass (pre-existing sandbox
  boundaries).
- Live Wikidata confirmed Q1490's label is "Tokyo" and it is Japan's P36 — the data
  path the fix relies on.

## Scope note
The match is on the canonical label, so a claim using a non-canonical surface
("Tokyo, Japan", or an alias) still won't match and may remain contradicted — a
conservative choice that bounds the (de-dicto) VERIFY to high-confidence name
identity. Broadening to aliases/normalized variants is a possible later extension.
