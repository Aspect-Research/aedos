# v0.16.2 — Constraint-validation layer + the direction/ordering problem

## Part A — Implemented: constraint-validation layer (the priority)

**What it does.** The contradiction value-type guard (`_object_satisfies_value_type`)
now sources its value-type constraint from the **KB property's own Wikidata
constraints (P2302)** — via the existing `fetch_property_ontology(prop)` →
`value_type_qids` — **when the oracle/seed left the binding's `object_entity_types`
undeclared**. So a single-valued entity predicate whose claim object provably
violates the property's value-type constraint **abstains** instead of
false-contradicting, even for predicates the oracle never typed (e.g.
`authored_by → P50`, value-type "human"). The constraint "falls out of the data"
rather than the oracle's guess.

**Where.** `kb_verifier.py`: new `_property_value_type_constraint(binding)` (fetches
`value_type_qids`, fail-open `[]`), consulted inside `_object_satisfies_value_type`
when `binding.object_entity_types` is empty. The rest of that function's fail-open
subsumption logic is unchanged.

**Why it's sound (structurally safety-only).** `_object_satisfies_value_type` is
consulted *only* in the copula CONTRADICTED gate; a `False` there only downgrades
CONTRADICTED → NO_MATCH (`value_type_incompatible_binding`). So the change can
**only turn a contradiction into an abstain** — it cannot create a false-verify or
a new false-contradict. It fails OPEN: it blocks only when the object is provably
`unrelated` to **every** constraint class; any missing constraint, absent
`fetch_property_ontology`, fetch error, or uncertain subsumption permits as before.
The verify path (`_object_confirms_value_type`) is untouched, so it never
over-abstains a legitimate VERIFY (e.g. the Tokyo case, where the true value Q1490
isn't typed as "city").

**Scope intentionally narrow.** Only the **value role on the contradict path**.
The subject role and the verify path are *not* validated against constraints —
validating a VERIFY against aspirational Wikidata constraints would over-abstain
true claims whose KB value doesn't fit the (often incomplete) constraint, and the
contradict-side subject already has statements for the property so it's a valid
domain. Value-on-contradict is the high-value, no-over-abstain piece.

**Tests.** `TestPropertyConstraintValidation` (4): blocked-on-violation;
proceeds-when-satisfied; fail-open when no constraint; fallback-fires-when-untyped.
Full offline suite **1733 passed**.

## Part B — The direction/ordering problem, examined more generally

The recurring failure ("Mark Twain wrote Huckleberry Finn" abstains while
"Huckleberry Finn was written by Mark Twain" verifies; the reversed "Huckleberry
Finn wrote Mark Twain" must NOT verify) is the **inverse-relation direction
problem**, shared by `capital_of`, `parent/child`, `employer/employee`,
`wrote/written_by`, etc.

**The key decomposition (why "direction from data" is half-true):**
- **Role typing** *does* fall out of the data: the property's domain/range
  constraints (P2302 subject-type / value-type) say which entity is the *work* and
  which is the *author*. Part A uses exactly this.
- **Voice** does *not* fall out of the data: "A wrote B" (active, A=author) and
  "B was written by A" (passive, B=work) describe the **same** P50 edge between the
  same two entities. Types can't distinguish which entity the *claim* names as the
  agent. That information lives in the **predicate/verb**, not the KB. This is why a
  pure type-orientation false-verifies "Huckleberry Finn wrote Mark Twain" (it finds
  the type-valid authorship edge and ignores that "wrote" makes the subject the
  author).

So a complete, sound solution needs **data for roles + language for voice**.
Candidate approaches:

1. **Extraction canonicalization (voice at the NLU layer) — recommended primary.**
   Normalize authorship/relational verbs to one canonical direction at extraction,
   preserving voice: "X wrote Y" and "Y was written by Y" → `Y authored_by X`;
   "Y wrote X" → `X authored_by Y`. The verifier then sees one canonical direction
   (already seeded: `authored_by → P50`), and Part A's constraints validate it.
   *Pro:* voice resolved where language is understood; general; sound.
   *Con:* prompt change, probabilistic, real blast radius (could mis-canonicalize),
   not offline-testable; also fixes the current Rule-3 bug that frames "wrote" as
   reported speech.

2. **Constraint-validated orientation at verify (Part A + a flip extension).**
   Use domain/range to *validate* the oracle's direction (abstain on type-violation,
   shipped) and *flip* to the unique type-valid orientation when the oracle's is
   type-impossible. *Pro:* sound, data-driven, catches mis-mappings/mis-directions.
   *Con:* cannot supply voice, so it can't orient symmetric-edge asymmetric-voice
   verbs on its own; the flip only helps when one orientation is type-*impossible*.

3. **Oracle direction improvement.** Feed the property's domain/range constraints
   into the predicate-translation oracle and prompt it to infer `slot_to_qualifier`
   voice-aware. The oracle already emits `slot_to_qualifier`; this makes it
   reliable. *Pro:* generalizes to unseen verbs; knowledge stays in the oracle.
   *Con:* probabilistic.

4. **Bidirectional edge check with a voice guard.** Resolve both entities; check the
   property edge in both directions; accept the one consistent with the claim's
   voice. *Pro:* robust to resolution/direction errors. *Con:* still needs voice;
   needs both entities to resolve.

5. **Seed the canonical relational predicates.** Pin `wrote`/`authored`/`directed`/…
   → P-id + correct `slot_to_qualifier` + type constraints. *Pro:* deterministic,
   immediate, sound. *Con:* per-verb (the "hardcoded mapping" we avoid); no
   generalization to unseen verbs.

**Recommendation.** Layer them: **(1)** extraction canonicalizes voice → a canonical
relational predicate; **(3)** the oracle maps it to property + direction, *informed
by the property's domain/range*; **(2/Part A)** the verifier validates roles against
the constraints. Voice lives at NLU, direction at the oracle+data, validation at
verify — each layer doing what it can soundly do. The single highest-leverage next
step for the `wrote` case specifically is **(1)** (it's also the current Rule-3
reported-speech bug), but it's the riskiest layer (prompt, live-only iteration), so
it should be its own scoped change with live evaluation.
