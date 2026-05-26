# Phase H Cluster 3 — ceiling diagnostic

Per operator directive after the dual-measurement probe data
(seeded 48%, cold-start 44%, +4pp gap; Cluster 2 baseline 44%
cold-start), the +2pp seeded lift over baseline is smaller than the
operator brief's conservative +10-20pp prediction. This document
classifies the remaining ceiling cases by tunability before
deciding what additional v0.15 work is appropriate vs. genuinely
v0.16 architectural deferrals.

Source data: `cluster_3_validation_run_20260526T182317Z.json`
(seeded run 1). Audit-event totals: 6 `tier_u_status_upgraded`, 1
`cross_source_contradiction`, 1 `walker_skipped_due_to_pre_verdict`.

## Per-category classification

### Category 1 — Walker upgrade policy (4 R2 misses)

Cases: `der_disambiguation_002` (Paris capital France),
`der_disambiguation_008` (Cambridge in Massachusetts),
`der_predicate_translation_005` (Marie Curie Nobel),
`der_predicate_translation_006` (France has_capital Paris).

**Failure mechanism.** Walker reaches Q-Lookup α path
(`_try_external_grounding`) after matching its own promoted
asserted_unverified row. The KB verifier is invoked but returns a
non-verified verdict, so the row's status is not upgraded and the
walker returns `verified_given_assertion`. Audit-event delta:
upgrade=0 for each.

The 6 cases where Q-Lookup α DID fire (audit count 6) share a
shape: standard-direction predicates with unambiguous subject Q-ids
(Obama → Q76, Honolulu → Q18094). The 4 misses each have a feature
that complicates KB resolution:

- `der_disambiguation_002` / `der_predicate_translation_006`:
  inverted direction (`capital_of` has `{subject: statement_value,
  object: statement_subject}`); the KB verifier must look up France's
  P36 and check if the value is Paris, not the other way around.
- `der_disambiguation_008`: subject "Cambridge" is ambiguous between
  Cambridge (Massachusetts city), Cambridge (Cambridgeshire, UK
  city), and Cambridge University (institution). The D33 type
  filter handles this if entity_types are populated; the corpus
  case might not have the right slot context for the resolver.
- `der_predicate_translation_005`: "Marie Curie received the Nobel
  Prize in 1903" — year is in qualifier P585; if the KB verifier
  needs the qualifier to match, the prompt-extracted year might
  format differently (1903 vs +1903-00-00T00:00:00Z).

**Classification: mostly code-tunable but partly architectural.**
The inverted-direction case is bounded code work in the KB
verifier (verify the slot-inversion logic against both `capital_of`
and `has_capital`). The Cambridge case is a corpus-side question
(better entity_types hint) or KB-verifier-side fallback (try
multiple candidate Q-ids and rank by predicate compatibility). The
Nobel-year case is qualifier-shape matching in the KB verifier.

These are bounded fixes if pursued — each is a focused KB-verifier
or entity-resolver path improvement, costing 2-4 hours of work
each plus an extraction_corpus re-validation ($1-3) and a
derivation_corpus re-validation ($2-3).

### Category 2 — Promotion-shadow-prior in walker (3 R4 misses)

Cases: `der_revision_003` (polarity_conflict),
`der_revision_004` (idempotent), `der_revision_006` (scope conflict).

**Failure mechanism — common root.** The Cluster 2 promote-then-walk
pattern means by the time `walker.walk` runs on a claim, that
claim's Tier U row already exists (`asserted_unverified`, just
written). Walker.Stage-1 matches this row first. The pre-existing
belief-revision paths (`polarity_conflict` at line 401-424,
`object_conflict` at line 426-456 of walker.py) are reachable ONLY
when Stage 1 misses — but the promotion guarantees Stage 1 hits
for the just-promoted claim.

For `der_revision_003` ("Asa is not a student", polarity=0, prior
holds_role=student polarity=1):
- Promotion writes (Asa, holds_role, student, polarity=0).
- Walker.Stage-1 with polarity=0 → matches own promotion.
- Returns `verified_given_assertion`.
- The `flipped` lookup (polarity=1) that would find the prior is
  in the Stage-1-miss branch — unreachable.

For `der_revision_004` ("Asa is still a student", polarity=1, prior
holds_role=student polarity=1):
- Prior seeded first with status=externally_verified.
- Promotion's write is idempotent (same key) → returns prior's id;
  row status remains externally_verified.
- BUT the audit data shows first_tier_u_premise_status =
  asserted_unverified, suggesting either the normalizer produced
  inconsistent canonical subjects between the seed write
  (source_text="seed") and the promotion write (source_text="Asa
  is still a student"), creating two rows instead of one.
- Walker matches the promoted row (asserted_unverified), not the
  prior. Returns `verified_given_assertion` instead of `verified`.

For `der_revision_006` ("Asa joined Google in 2020", prior
employed_by Google valid_from=2019):
- Step 3 Rule 12 produces predicate=employed_by, object=Google,
  valid_from=2020.
- Promotion's write hits the prior on key match — but with a
  different valid_from. TierU.write's idempotency uses only
  (asserting_party, subject, predicate, object, polarity); valid_from
  is not part of the key. So write returns prior's id idempotently;
  the valid_from delta is silently ignored.
- Walker matches the (externally_verified) prior. Returns
  `verified`. Scope conflict not detected.

**Classification: code-tunable.**

Three bounded fixes:

a. **Walker change: filter own promotion from Stage 1 lookup.** The
   promotion step returns the row_id it wrote (or matched
   idempotently). The walker accepts this row_id and excludes it
   from Stage 1. This makes the polarity_conflict path reachable
   for `der_revision_003`.

b. **TierU normalizer consistency.** The normalizer is invoked with
   `source_text` from the claim. For seed writes (source_text="seed")
   vs promotion writes (real source text), the same surface form
   could normalize differently. Either skip the normalizer for seed
   writes (the seed values are already canonical by convention) or
   make the normalizer source-text-insensitive for short bare
   references.

c. **TierU.write scope-aware idempotency / scope_conflict
   detection.** Include valid_from / valid_until in the idempotency
   key, OR add a `scope_conflict` write outcome when the new claim's
   scope differs from a prior. Walker reads the outcome and emits
   `contradicted` for `der_revision_006`.

(a) and (b) together unblock `der_revision_003` and `der_revision_004`.
(c) unblocks `der_revision_006`. All three are bounded code changes
to walker.py / tier_u.py. Each comes with new unit tests in
test_walker_cluster_2.py / test_tier_u.py.

### Category 3 — Subject normalization for common nouns (1 R4 miss)

Case: `der_revision_005` ("The project ended in 2024", prior
"project" status=ongoing).

**Failure mechanism.** Extractor produces subject="The project"
(article preserved); seed has subject="project". TierU._normalize_slot
calls the Wikipedia normalizer; for non-named-entity subjects like
"The project" the normalizer doesn't transform them. Walker Stage
1 misses (literal subject mismatch). The walker abstains with
`abstained_given_assertion`.

**Classification: code-tunable.** Two options:

a. **Strip leading articles in TierU._normalize_slot** before the
   Wikipedia normalizer is consulted. Keeps the rule simple
   ("project" and "The project" are the same).
b. **Add an extractor rule to strip articles from subjects.**
   Cheaper at extraction time; less consistent across the pipeline.

Option (a) is preferred because it's symmetric (prior write and
promotion write both strip articles; literal lookup matches).

### Category 4 — Semantic single_valued question (1 R4 miss)

Case: `der_revision_002` ("Asa works at Google", prior employed_by
Microsoft).

**Failure mechanism.** Step 2's alias `works_at` → P108
(employed_by) routing works. Extractor produces `employed_by Google`.
Both the new claim and the prior have predicate=employed_by but
different objects. Walker's object_conflict path requires
`_predicate_is_functional(employed_by)` to fire — but employed_by's
seed has `single_valued=0` (multi-valued: many employers over a
career). Object_conflict doesn't fire. Walker returns
`verified_given_assertion`.

**Classification: semantic call needed, not purely code-tunable.**

The corpus expects employed_by to be functional at a point in time
(one current employer). The seed treats it as a career history
field (multi-valued). Both are legitimate semantics. The fix
options:

a. **Treat employed_by as functional at a point in time.** Set
   `single_valued=1`. Belief-revision then works as the corpus
   expects. But: a person who held jobs at IBM (1990-1995) and
   then Microsoft (1996-2005) and then Google (2006-) currently has
   ONE employer (Google). The career-history semantic needs to be
   captured via valid_from/valid_until, not multi-valued rows.

b. **Leave employed_by multi-valued; flag the corpus expectation
   as a semantic mismatch.** Corpus author intended point-in-time;
   the system treats it as history. The corpus case is wrong.

c. **Add a "functional-at-a-point-in-time" cardinality concept.**
   This is genuinely v0.16 architectural — a third cardinality
   alongside functional/multi-valued.

Option (a) is the operator's call on the semantic model. If chosen,
it interacts with several other seeded predicates (member_of,
political_party, religion, occupation — all have time-changing
character) that the operator may want to similarly reclassify. v0.16
candidate IF the operator wants the temporal-functional distinction
formalized; otherwise option (a) bounded to employed_by is also
defensible for v0.15 if the operator confirms.

## Summary table

| Case | Category | Classification | Cost | v0.15 work? |
|---|---|---|---|---|
| der_disambiguation_002 | 1 | code-tunable (inverted slot in KB verifier) | 2-4h + $3 | maybe |
| der_disambiguation_008 | 1 | code-tunable (entity_types fallback) | 2-4h + $3 | maybe |
| der_predicate_translation_005 | 1 | code-tunable (year-qualifier match) | 2-4h + $3 | maybe |
| der_predicate_translation_006 | 1 | code-tunable (inverted slot) | (shared with 002) | maybe |
| der_revision_003 | 2 | code-tunable (walker filter own promotion) | 4-6h + $3 | **yes** |
| der_revision_004 | 2 | code-tunable (normalizer consistency) | 4-6h + $3 | **yes** |
| der_revision_006 | 2 | code-tunable (scope_conflict detection) | 4-6h + $3 | **yes** |
| der_revision_005 | 3 | code-tunable (strip articles) | 1-2h + $3 | **yes** |
| der_revision_002 | 4 | semantic call (single_valued question) | 1h + operator decision | operator call |

**Recommended Cluster 3 closure path:**

- **Category 2 fixes (yes):** Cluster 3 step 7 lands a walker change
  to exclude own-promotion from Stage 1, a normalizer consistency
  fix for seed writes, and scope_conflict detection. Three R4
  cases (003, 004, 006) lift to passing. Estimated +6pp seeded
  (54%) and similar or larger cold-start uplift since these are
  walker mechanics independent of substrate.
- **Category 3 fix (yes):** Cluster 3 step 8 lands article-stripping
  in TierU._normalize_slot. One R4 case (005) lifts. +2pp.
- **Category 4 (operator call):** Bring to the operator: change
  employed_by to single_valued=1, OR explicitly defer to v0.16
  with the temporal-functional concept.
- **Category 1 (deferred):** Tag as v0.16 candidates. The four cases
  are bounded but split across KB verifier, entity resolver, and
  qualifier-shape areas — better to surface as discrete v0.16
  D-items rather than land Cluster 3 with mixed-area changes.

**If Categories 2 and 3 land:** projected seeded accuracy 54-56%
(+10-12pp over baseline) — at the lower bound of the operator
brief's conservative prediction. Phase 10.5's full 3x2 variance
runs would confirm the median.

**Estimated v0.15 work to close:** 1.5-2 days for steps 7-8 + ~$6-12
in additional API for re-validation. Within the session budget.

## Investigation methodology pattern

This diagnostic used:
- The validation script's per-case audit-event deltas (`upgrade`,
  `cross_source`, `walker_skipped`) to classify R2 misses by
  whether Q-Lookup α fired vs failed.
- `first_tier_u_premise_status` and `walk_edges_count` from the
  walk capture to distinguish "walker matched own promotion" from
  "walker matched prior" from "walker abstained with no Tier U
  match".
- Code reading of `walker.py` (lines 311-467) and `tier_u.py`
  (lines 92-200, 516-590) to identify the failure mechanism per
  case.
- Cross-reference against the corpus JSON to verify expected
  semantics.

The pattern is reusable for future Phase H or Phase 10.5
investigations: the validation script's audit-delta capture is
already structured for this kind of diagnostic. Worth capturing as
a v0.16 reference (D55-adjacent — semantic correctness audit pattern).
