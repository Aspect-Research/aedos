# Phase H Cluster 2 — design

**Status:** design draft, pending operator review.

Cluster 2 shifts Aedos from a strict per-claim verifier to a
**knowledge-building verifier within a session**: user-asserted
claims accumulate as Tier U premises with an explicit
`asserted_unverified` provenance flag, and the walker may chain
those premises with external grounding to derive subsequent
verdicts. Verdicts whose chain includes any asserted-unverified
premise are flagged with a dual designation
(`verified_given_assertion` / `contradicted_given_assertion` /
`abstained_given_assertion`) so the chain's grounding source stays
explicit in the audit trail.

This is the largest pending architectural commitment in Phase H.
After it lands, the soundness story expands: there are two verdict
families in the "system says true" sense — one purely externally
grounded, one conditionally grounded on user assertion — and the
dual designation preserves §3.2 by making the grounding kind
explicit on every result.

## Motivation

Derivation corpus cases `der_multihop_001`…`_012` (12 of 50)
demonstrate the pattern. Example `der_multihop_001`:

```json
{
  "input": {
    "text": "Asa lives in Williamstown",
    "context_premises": [
      {"subject": "Williamstown", "predicate": "part_of", "object": "Massachusetts"}
    ]
  },
  "expected_output": {"verdict": "verified",
                      "chain": ["lives_in + part_of → lives_in"]}
}
```

The corpus author seeded `Williamstown part_of Massachusetts` as a
substrate fact but seeded **no Tier U row about Asa**. Today's walker:

1. Stage 1 / 2 / 3 Tier U lookup misses (Tier U empty)
2. KB lookup misses (Asa is not in Wikidata)
3. Substrate expansion has nothing to chain off — no premise to start the
   distribution traversal from
4. Abstains

The corpus author's expectation is that the *extracted user claim*
("Asa lives_in Williamstown") itself becomes a Tier U premise the
walker can use. Without that promotion, the multihop cases cannot
verify; with it, they verify cleanly via Stage 1 literal match (or
via distribution chain when the input claim is the *derived* form
"Asa lives_in Massachusetts" and the premise "Asa lives_in
Williamstown" came from a prior turn).

The same shape recurs in the phase 10.5 runbook (Step 3 seeds Asa
assertions directly into Tier U by hand for benchmark cases). Cluster
2 generalizes that pattern: the system itself does what the runbook
asks the operator to do.

## Operator-confirmed contract

Five decisions are settled. The design implements them faithfully;
implementation-design questions outside the contract are surfaced
in §"Surfaced design questions" below.

1. **Promotion timing — Sub-C.** Every extracted user claim writes
   a Tier U row with an `asserted_unverified` status. The walker
   subsequently sees these rows as premises and propagates the flag
   through derivation chains.

2. **Persistence — per-session.** Asserted-unverified rows persist
   within a verification session but not across sessions. The corpus
   runner already enforces this via per-case `DELETE FROM tier_u`
   (D16); the chat-wrapper's session is the lifetime of its database
   handle. Cross-session persistence is deployment policy (architecture
   §6.1 cross-context Tier U), not a Cluster 2 commitment.

3. **Verdict designation — Naming X (dual).** Two families:
   - `verified` / `contradicted` / `abstained` — fully
     externally-grounded chains (KB statements, Python computations,
     and *previously externally-verified* Tier U rows).
   - `verified_given_assertion` /
     `contradicted_given_assertion` /
     `abstained_given_assertion` — chains containing at least one
     asserted-unverified premise.

   The flag propagates transitively. If asserted-unverified premise
   A grounds derivation B, and B grounds C, then C is also
   `*_given_assertion`.

4. **Belief revision among asserted claims — per cardinality.** The
   existing §6.1 cardinality logic is reused for asserted-unverified
   conflicts:
   - Functional (single_valued) predicate: new assertion overrides
     the prior via standard belief revision; the prior row is closed.
   - Non-functional predicate: both assertions coexist as parallel
     rows.

5. **User-vs-KB contradiction — KB wins.** When a new user assertion
   would close a Tier U row whose status is *externally verified*
   (not merely asserted_unverified), the KB-verified row instead
   stays open and the new assertion gets a `contradicted` verdict.
   Asymmetric on purpose: user assertion cannot override external
   grounding.

## Codebase orientation

Findings from reading the current state, used as the starting point
for the implementation.

**Tier U** (`src/aedos/layer4_sources/tier_u.py`, `database.py:11`):
- Schema has no status / provenance flag. Adding one is bounded.
- `write` is already idempotent on
  `(asserting_party, subject, predicate, object, polarity)`; the
  cardinality-aware closure logic from D16 is exactly what the
  contract's decision 4 needs.
- `lookup` and `lookup_object_conflict` are status-blind. Both
  need to surface the matched row's status so the walker can decide
  what to tag the verdict with.

**Walker** (`src/aedos/layer4_sources/walker.py`):
- `_direct_lookup` already does Tier U → KB → Python in the §6.5
  order, returning `verified` or `contradicted`. The verdict string
  is what flows into the aggregator.
- The walker has no concept of premise-source provenance yet — every
  trace edge carries a `source` (`tier_u` | `kb` | `python`) but no
  flag distinguishing externally-verified Tier U rows from
  asserted-unverified ones.
- The §6.5 lookup order means a self-promoted row will satisfy
  Stage 1 *before* the walker reaches KB. That short-circuits
  external verification of asserted claims (see Q-Upgrade below).

**Aggregator** (`src/aedos/layer5_result/aggregator.py`):
- Counts three verdict types. Has no dual designation logic.
- `VerificationResult.aggregate_metadata`'s
  `verified`/`contradicted`/`abstained` counts feed
  `select_intervention` in the chat-wrapper.

**Router** (`src/aedos/layer2_routing/router.py`):
- The `user_authoritative` route label exists but **no caller acts
  on it** — the walker treats user_authoritative claims the same as
  kb_resolvable ones. Cluster 2 generalizes the intent (every claim
  promotes to Tier U), making the label informational. The route
  still carries useful metadata (predicate is fundamentally
  user-stipulated → walker should not attempt KB / Python after
  finding the asserted row); see Q-UserAuth below.

**Chat-wrapper** (`src/aedos/deployment/chat_wrapper.py:80-110`):
- Extracts claims from the **LLM draft**, not the **user message**.
  The user_message itself is never extracted from today, so no
  "user-asserted claim" path exists in the deployed pipeline at all.
- Cluster 2 implies adding a user-message extraction step in the
  chat-wrapper. The corpus runner's `text` field naturally serves
  both roles (user assertion + verification target), so the
  calibration path doesn't need this work.

**Corpus runner** (`tests/calibration/test_corpus_runner.py:540`):
- Already clears Tier U per case (D16). Cluster 2 promotion just
  adds rows during the existing run; isolation is intact.
- Existing `tier_u` / `tier_u_prior` keys still pre-seed rows for
  cases that want a specific prior state; those rows should be
  marked as `externally_verified` so the §6.1 cross-source rule
  fires correctly when later assertions conflict (see Q-Seed below).

## Architecture

Three components change; two are new.

```
  text + context
       │
       ▼
┌──────────────────────────────────────────────────────────────┐
│ Layer 1 — extraction                                         │
│ (unchanged — produces claims with asserting_party)           │
└──────────────────────────────────────────────────────────────┘
       │
       │ claims: list[Claim]
       ▼
┌──────────────────────────────────────────────────────────────┐  ← NEW
│ Promotion step (new pipeline stage)                          │
│ For every extracted claim:                                   │
│   tier_u.write(claim, status='asserted_unverified')          │
│ Applies §6.1 cardinality belief revision (D16) +             │
│ §"KB wins" cross-source rule if a conflicting prior row      │
│ is `externally_verified`.                                    │
│ Runs BEFORE any walker call so all claims see each other     │
│ as premises.                                                 │
└──────────────────────────────────────────────────────────────┘
       │
       │ claims (still the same list)
       ▼
┌──────────────────────────────────────────────────────────────┐
│ Layer 4 — derivation walker                                  │
│   _direct_lookup reads the matched row's status flag.        │
│   The walker tracks chain composition:                       │
│     - 'externally_grounded' if every premise hit is KB,      │
│       Python, or externally_verified Tier U                  │
│     - 'asserted' if any premise hit is asserted_unverified   │
│       Tier U                                                 │
│   On successful external grounding of a previously asserted  │
│   claim, the walker calls tier_u.mark_externally_verified()  │
│   to upgrade the row's status (see Q-Upgrade below).         │
└──────────────────────────────────────────────────────────────┘
       │
       │ WalkResult.verdict ∈ {verified, verified_given_assertion,
       │                       contradicted, contradicted_given_assertion,
       │                       no_grounding_found, abstained_given_assertion}
       ▼
┌──────────────────────────────────────────────────────────────┐
│ Layer 5 — aggregator                                         │
│ Counts both verdict families. base verdict (verified vs.     │
│ contradicted vs. abstained) drives intervention selection;   │
│ the assertion flag is metadata for audit / Phase 10.5.       │
└──────────────────────────────────────────────────────────────┘
```

### The status field

Schema addition on `tier_u`:

```sql
ALTER TABLE tier_u ADD COLUMN status TEXT NOT NULL
  DEFAULT 'asserted_unverified';
```

Values:
- `asserted_unverified` — row entered the table via user assertion
  promotion and has not been externally grounded.
- `externally_verified` — row was either (a) promoted from a user
  assertion and subsequently verified by a KB / Python derivation
  chain, or (b) pre-seeded by the deployment / corpus runner as a
  known external fact.

The default is `asserted_unverified`: existing harness paths that
write Tier U via the promotion route get the asserted flag for free.
Pre-seed paths (corpus runner's `tier_u_prior`, runbook Step 3) pass
`status='externally_verified'` explicitly — those represent prior
external knowledge, not in-session user assertions. (See Q-Seed.)

### Walker chain-composition tracking

The walker tracks per-trace whether the chain includes any
asserted-unverified premise. Implementation:

- `JustificationTrace` gains a `chain_includes_assertion: bool`
  field (default False).
- Every `_direct_lookup` Tier U hit reads `row.status`; if
  `asserted_unverified`, sets `chain_includes_assertion = True`.
- Every subsumption / equivalence traversal preserves the flag (the
  flag is monotonic — once True, stays True for the rest of the
  walk).
- Final verdict assembly: `verified` + flag → `verified_given_assertion`,
  `contradicted` + flag → `contradicted_given_assertion`,
  `no_grounding_found` + flag → `abstained_given_assertion`.

The base verdict still drives intervention; the flag is read at
aggregation time to compute the dual designation.

### Verdict-upgrade write-back (proposed; see Q-Upgrade)

When `_direct_lookup` finds an `asserted_unverified` Tier U row
that subsequently also verifies via KB or Python within the same
walk, the walker calls `tier_u.mark_externally_verified(row_id)`
to upgrade the row's status. The verdict for *this* walk is plain
`verified` (the chain was externally grounded); future walker calls
that touch the row see it as `externally_verified` and skip the
flag set. This both keeps the chain composition correct and lets
external grounding propagate across in-session derivations.

### Cross-source contradiction (§"KB wins")

Fires inside the promotion step's belief-revision check:
- New user assertion's `(asserting_party, subject, predicate)`
  conflicts with a prior Tier U row.
- If the prior is `asserted_unverified` → §6.1 cardinality rules
  apply unchanged. Prior closes (single_valued) or coexists
  (multi-valued).
- If the prior is `externally_verified` → **KB wins**. The prior
  stays open; the new assertion is still written (preserving the
  audit trail of what the user said) but its row is marked
  `contradicted_by_externally_verified` and a `contradicted` verdict
  is recorded for the claim. Subsequent walker calls treat the
  contradicted-row as non-grounding (skipped on lookup, like
  retracted rows).

Status enum thus becomes three-valued:
`asserted_unverified | externally_verified | contradicted_by_externally_verified`.
(See Q-Status below — a smaller two-flag schema may be cleaner.)

### Architectural asymmetry (intentional)

The system trusts external sources to validate user assertions
(upgrade path: `asserted_unverified` → `externally_verified`) but
does not let user assertions invalidate external sources (KB-wins
path: user assertion → `contradicted_by_externally_verified`).
External grounding is one-way authoritative over user assertion.
Future operators reading the code should understand this asymmetry
is intentional architectural intent, not implementation oversight —
it is the mechanism by which §3.2 soundness extends across the
expanded verdict family: user assertion can never override what an
external source has established, so a chain that touches an
externally-verified premise inherits external grounding, while a
chain that touches an asserted-unverified premise inherits the
weaker conditional grounding.

### Verdict point-in-time semantics

Verdicts are point-in-time. If `walk_1` at time T1 produces
`verified_given_assertion` because the chain hit an
asserted_unverified Tier U row, and that row subsequently upgrades
to `externally_verified` at time T2 (via a later walk that grounded
the claim externally), `walk_1`'s recorded verdict remains
`verified_given_assertion` — verdicts do not retroactively rewrite.
Future walks (after T2) that consult the upgraded row produce plain
`verified`. The audit log preserves the history of both walks; the
status transition is its own audit event with its own timestamp.

The same applies to retraction: a verdict's correctness at issue
time is not invalidated by later substrate change. Retraction
propagation (architecture §7.3) handles re-derivation; verdicts
themselves are immutable records of what the system concluded with
the premises it had then.

### User_authoritative verdict semantics

For predicates whose route is `user_authoritative` (preferences,
beliefs, first-person experience — see Q-UserAuth), the verdict is
**always** one of the `*_given_assertion` family. There is no path
to plain `verified` because external grounding does not apply to
these predicates — no KB property maps to `prefers` or `believes`,
and Python cannot compute first-person facts. The verdict family
is `verified_given_assertion` / `contradicted_given_assertion` /
`abstained_given_assertion`, and the upgrade path is structurally
unreachable.

This is correct architectural behavior — user-stipulated facts are
verified-given-the-user-said-so, never verified-by-external-source.
Cluster 2 makes that distinction explicit in the verdict, where
prior versions would have returned plain `verified` and concealed
the asymmetry.

## Schema changes

```sql
-- step 1: status column
ALTER TABLE tier_u ADD COLUMN status TEXT NOT NULL
  DEFAULT 'asserted_unverified';

-- step 1: migration of any existing rows (production paths only —
-- corpus runner starts fresh per case; chat-wrapper deployments
-- starting fresh after the migration get the default). Rows written
-- by the pre-Cluster-2 path were de facto external (operator-seeded
-- or test-fixture-seeded): promote them.
UPDATE tier_u SET status='externally_verified'
  WHERE status='asserted_unverified' AND asserted_at < '<migration_cutoff>';
```

No new tables. No new indices needed (status appears in WHERE
clauses with the existing `asserting_party + subject + predicate`
key prefix — the existing indices cover it).

`audit_log` gains three new event types (no schema change — the
`event_type` field is a free string):
- `tier_u_asserted_promotion` — emitted by the promotion step
- `tier_u_status_upgraded` — emitted by `mark_externally_verified`
- `cross_source_contradiction` — emitted by promotion's KB-wins path

`JustificationTrace` (`src/aedos/layer5_result/trace.py`) gains:
- `chain_includes_assertion: bool` — final flag
- per-edge `metadata['premise_status']` for tier_u-source edges
  (one of the status values, for audit reconstruction)

`VerificationResult.aggregate_metadata` gains:
- `verified_given_assertion: int`, `contradicted_given_assertion: int`,
  `abstained_given_assertion: int` counts alongside the existing three

`select_intervention` collapses the dual designations to their base
verdict (`verified_given_assertion` counts as verified for the
intervention decision, etc.).

## Implementation steps

Match the operator brief's six-step plan, with concrete file-level
shape.

### Step 1 — Schema, verdict types, trace metadata (~1 day)

- `src/aedos/database.py`: add `status` column to `tier_u`
- `src/aedos/layer4_sources/tier_u.py`: `WriteResult` gains
  `was_cross_source_contradicted`; `write` accepts `status` kwarg
  (default `'asserted_unverified'`); `LookupResult.rows` rows expose
  `status` (already do, via `SELECT *`); add
  `mark_externally_verified(row_id)`.
- `src/aedos/layer5_result/trace.py`: `JustificationTrace` gains
  `chain_includes_assertion: bool`; `TraceEdge.metadata` is already a
  free dict so per-edge `premise_status` requires no schema change.
- `src/aedos/layer5_result/aggregator.py`: `aggregate_metadata`
  includes the three new counts.
- Verdict enum: keep verdicts as strings (current convention); add
  the three new strings as accepted values where verdict equality is
  checked.

Tests:
- `tests/unit/test_tier_u.py`: status persistence, `mark_externally_verified`,
  status-aware lookup.
- `tests/unit/test_aggregator.py`: dual designation counts.
- Schema round-trip / migration test.
- **Structural test (D36 pattern):** assert the six-way verdict set
  is handled consistently across all sites verdict types appear —
  serialization in trace / VerificationResult JSON, audit log
  `verdict_recorded` events, aggregator's `aggregate_metadata`
  counts, `select_intervention`'s collapse logic, and the corpus
  runner's expected-verdict comparison. The test enumerates the
  six verdicts and asserts each is recognized (not rejected as
  unknown, not silently mapped to the wrong base) at every site.
  Mitigates the risk that the dual designation drifts as new code
  touches verdict handling.

Commit: `Phase H Cluster 2 step 1: schema for asserted-unverified status and dual-designation verdicts`

### Step 2 — Assertion promotion in the pipeline (~1 day)

- New `src/aedos/layer4_sources/promotion.py`:
  `promote_assertions(claims, tier_u) -> list[PromotionResult]`.
  For each claim, calls `tier_u.write(claim, status='asserted_unverified')`,
  catching the cross-source contradiction case and emitting
  `cross_source_contradiction` audit events.
- `src/aedos/pipeline.py`: assembly unchanged (promotion is invoked
  by callers, not built into the pipeline dataclass).
- `src/aedos/deployment/chat_wrapper.py`: call `promote_assertions`
  on the extracted claims before the walker loop. (Note: chat_wrapper
  today extracts from `draft`, not user_message; see Q-ChatWrapperSource.)
- `tests/calibration/test_corpus_runner.py`: `_run_derivation` calls
  `promote_assertions` on extracted claims before `walker.walk`.

Tests:
- `tests/unit/test_promotion.py`: claims write to Tier U with the
  correct status; cross-source contradiction returns the correct
  WriteResult.
- Integration: end-to-end through `chat_wrapper.respond` confirming
  promotion happens before walker.

Commit: `Phase H Cluster 2 step 2: assertion promotion in extraction pipeline`

### Step 3 — Walker chain-composition tracking (~1.5 days)

- `src/aedos/layer4_sources/walker.py`:
  - `_direct_lookup` reads `result.rows[0]['status']` for Tier U
    matches; if `asserted_unverified`, sets
    `trace.chain_includes_assertion = True` and records
    `premise_status` on the edge.
  - On a Tier U match with `asserted_unverified` status, the walker
    continues to KB / Python lookup (instead of short-circuiting on
    the Tier U hit) to attempt external grounding. If KB / Python
    confirms, the walker calls `tier_u.mark_externally_verified(row_id)`
    and the chain-composition flag is *not* set
    (chain is externally grounded after upgrade). If KB / Python
    doesn't confirm, the chain flag stays set and the verdict is
    `verified_given_assertion`.
  - Subsumption traversal edges propagate the flag forward
    monotonically (any premise on the chain sets it for the whole
    chain).
  - Final verdict computation: if `chain_includes_assertion`,
    convert base verdict to its `_given_assertion` variant.
- `src/aedos/layer5_result/aggregator.py`: bucket the six verdict
  types into the metadata counts; `select_intervention` collapses
  to the three base verdicts.

Tests:
- `tests/unit/test_walker.py`: mixed-source chain produces
  `verified_given_assertion`; pure-KB chain produces `verified`;
  upgrade path verifies and flips the row status.
- `tests/unit/test_aggregator.py`: intervention decisions are
  unchanged for collapsed verdicts.
- Integration: derivation_corpus multihop cases verify (with
  whichever designation is appropriate).

Commit: `Phase H Cluster 2 step 3: walker premise-source tracking and dual-designation verdict aggregation`

### Step 4 — Cross-source contradiction (~0.5 day)

Most of this lands in step 2 (promotion-time check). Step 4 covers
the walker-side reciprocal:
- `_direct_lookup`'s belief-revision paths skip Tier U rows whose
  status is `contradicted_by_externally_verified`.
- `lookup_object_conflict` likewise.
- Audit log captures the asymmetry at decision time.

Tests:
- `tests/unit/test_tier_u.py`: cross-source contradiction case
  (user-vs-externally-verified) leaves the prior open, writes the
  new row with `contradicted_by_externally_verified`.
- `tests/unit/test_walker.py`: a contradicted-by-KB user assertion
  produces `contradicted` (not `contradicted_given_assertion` —
  the contradiction is externally grounded).

Commit: `Phase H Cluster 2 step 4: cross-source contradiction handling with KB priority`

### Step 5 — Corpus alignment (~0.5 day)

Per the operator brief, two options. **Recommended: Option A** —
update corpus expected verdicts to use the dual designations
explicitly. Reasoning: the corpus is the contract the system is
measured against; equivocating between `verified` and
`verified_given_assertion` at scoring time hides the architectural
distinction. Option A is also the more disciplined choice; matches
the prior cluster review style.

- `tests/calibration/derivation_corpus.jsonl`: update the 12
  multihop cases and any cross-source cases whose chain includes an
  in-session assertion to expect `verified_given_assertion` instead
  of `verified`.
- `tests/calibration/test_corpus_runner.py`: `_run_derivation`
  accepts the new verdict strings in its expected-verdict comparison.
- The corpus author's `tier_u_prior` entries get
  `status='externally_verified'` at seed time (they represent
  established external knowledge, not in-session assertions).

The split between "cases whose chain *requires* an asserted
premise" vs. "cases whose chain *could* externally verify" is the
disciplined audit work for this step — the corpus is small enough
(50 cases) that a one-pass review settles it.

Tests:
- Corpus runner passes the updated expectations.
- Per D49: 2-3 runs per validation step.

Commit: `Phase H Cluster 2 step 5: corpus alignment for dual-designation verdicts`

### Step 6 — Validation (~0.5–1 day)

- Run `derivation_corpus` against the post-Cluster-2 build under
  RUN_CALIBRATION + RUN_LIVE_KB + RUN_LIVE_TESTS.
- Compare to post-D53 baseline (22/50). Expected lift: the 12
  multihop cases should verify under the new pipeline (chain through
  the self-promoted Tier U row), plus a small handful of
  cross-source cases whose chain was missing the user-assertion
  premise.
- Conservative prediction per the brief: +12-18 percentage points.
  Optimistic: all 12 multihop cases verify cleanly → 34/50 (68%) →
  +24 pp.
- D49 discipline: 2-3 runs, report a range.
- Document in `docs/phase_H/cluster_2_validation.md`.

Commit: `Phase H Cluster 2 step 6: validation`

## Surfaced design questions

Implementation-design questions the contract doesn't pre-answer.
Each carries a recommended answer; surface for operator confirmation
before step 1 lands.

### Q-Status — Status enum shape: three values or two-flag?

Three-value enum
(`asserted_unverified | externally_verified | contradicted_by_externally_verified`)
vs. two boolean flags
(`is_externally_verified BOOL, is_contradicted_by_kb BOOL`).

**Recommendation: three-value enum.** Simpler to query, one column,
one CHECK constraint. The states are mutually exclusive in practice
(a row cannot be both `externally_verified` and `contradicted_by_kb`).
The boolean form would invite invalid combinations.

### Q-Upgrade — Does the assertion flag flip when the claim externally verifies?

When the walker finds a Tier U row at `asserted_unverified` and
*also* grounds the claim via KB / Python in the same walk, does the
row's status transition to `externally_verified`?

**Recommendation: yes, upgrade.** The contract's §"KB wins" rule
requires distinguishing externally-verified rows; the upgrade
mechanism is how rows acquire that status during normal operation.
Walker calls `tier_u.mark_externally_verified(row_id, grounding_chain)`
on successful external grounding; this is the only write-back path
the walker takes.

The verdict for the walk that performed the upgrade is plain
`verified` (the chain that produced *this* verdict was externally
grounded — the Tier U match was redundant). Subsequent walks
benefit from the upgraded status without re-doing the external
verification work.

**Upgrade-event audit capture.** The `tier_u_status_upgraded`
event captures the *triggering verification chain* — which KB
statements, Python verifications, or substrate rows grounded the
upgrade — in its `event_data`. Concretely: the same trace-edge
references the aggregator records into the retraction propagator
(architecture §7.3) get serialized into the upgrade event.

This is for v0.16 retraction-propagation work (v0.16 D14 territory):
when a KB row that triggered an upgrade is later retracted, the
upgrade arguably ought to reverse (the externally_verified status
was contingent on that KB row's truth). v0.15 does not yet
implement the reverse-upgrade propagation, but capturing the chain
now means v0.16 doesn't need archaeological reconstruction from
trace-log replay. Adding the field later would require either a
schema migration or accepting that pre-v0.16 upgrades are
unrecoverable; capturing now is cheap.

Alternative considered: never upgrade. Rejected because it leaves
the §"KB wins" rule unenforceable — no Tier U row can ever become
`externally_verified` if upgrade is the only path.

### Q-Lookup — Should the walker try KB after a successful asserted-unverified Tier U hit?

Today §6.5 lookup order is Tier U → KB → Python; the walker
short-circuits on the first definite verdict. Under Cluster 2, a
self-promoted asserted_unverified row triggers Stage 1 → trivially
"verifies" the claim that produced it.

Three options:
- α. Always check KB even after a Tier U hit (when the hit is
  asserted_unverified). Upgrade on success; mark
  `verified_given_assertion` otherwise.
- β. Skip KB when the Tier U hit is asserted_unverified. Always
  return `verified_given_assertion`.
- γ. KB-first lookup order (reverse §6.5) for kb_resolvable claims.

**Recommendation: α.** Honors the soundness story (we prefer
externally-grounded verdicts when available) and is the only
option that enables Q-Upgrade's transition path. Cost: one extra
KB call per asserted-unverified Tier U hit; bounded by the existing
LLM-call budget (default 10 per claim).

### Q-UserAuth — Does the `user_authoritative` route still need a separate code path?

Today the router emits `user_authoritative` as a route label but
no caller acts on it. Under Cluster 2, every claim promotes to
Tier U regardless of route, so the label is informational.

**Recommendation: keep the label, skip KB / Python for
user_authoritative claims.** For predicates the system has
classified as fundamentally user-stipulated (preferences, beliefs,
first-person experience), there is no external source to consult —
the walker should match the self-promoted row and return
`verified_given_assertion` immediately, without spending a KB call
that will not produce grounding. This collapses to Q-Lookup option
β for user_authoritative claims, option α for everything else.

### Q-ChatWrapperSource — Where does the chat-wrapper get user assertions from?

`chat_wrapper.respond` today extracts claims only from the LLM
draft. For Cluster 2 to deliver "user-asserted claims accumulate as
premises" in the deployed pipeline, the wrapper needs to also
extract claims from `user_message` and promote those.

**Recommendation: add a second extractor invocation on
`user_message` and promote the resulting claims, distinct from the
draft-extraction-and-walk loop.** Concretely:

```
1. extract(user_message) → user_claims
2. promote(user_claims) → Tier U asserted_unverified rows
3. draft = llm.chat(user_message)
4. extract(draft) → draft_claims
5. for each draft_claim: walker.walk(draft_claim, ctx)
6. aggregate + intervene
```

This is bounded extra extraction cost (one extra LLM call per turn)
and matches the architectural intent. The corpus runner is unaffected
(its `text` field is both user input and verification target).

If the operator prefers to defer the chat-wrapper change to a
follow-up commit, step 2's promotion can be wired only into the
corpus runner first; the chat-wrapper change becomes a separate
commit that depends on it.

### Q-Seed — How does the corpus runner mark pre-seeded `tier_u_prior` rows?

The runner's `_run_derivation` seeds `tier_u_prior` entries via
`tier_u.write(...)` today. Under Cluster 2's default
(`status='asserted_unverified'`), those seeds would be flagged as
in-session assertions, which mis-describes their role: the corpus
author seeded them to represent prior external knowledge for the
case.

**Recommendation: `tier_u_prior` writes pass
`status='externally_verified'`; the runner's per-case extracted
claims go through the promotion path (status='asserted_unverified').**
This preserves the runner's existing semantics (prior rows are
"established") and exercises the cross-source contradiction path
for cases where a new assertion conflicts with a seeded prior.

`tier_u` entries (without the `_prior` suffix) are a third shape —
the corpus uses them inconsistently; sample inspection of der_cross_*
suggests they're also pre-seed externals. Recommendation: same
treatment as `tier_u_prior`.

### Q-Aggregation — Does intervention selection see the dual designations?

`select_intervention` today branches on
`verified` / `contradicted` / `abstained` counts.

**Recommendation: it sees only base verdicts.** The aggregator
collapses dual designations to base verdicts before computing
intervention. Audit (and Phase 10.5 measurement) sees the full
six-way breakdown via `aggregate_metadata`. Intervention selection
is about user-facing behavior; the user does not need to know
whether the system trusted their own assertion or got it from a KB.

If the operator prefers to surface the distinction to users (e.g.,
a different intervention copy for `verified_given_assertion`), that
is a deployment-policy change separable from the architecture work
here.

### Q-MultiClaim — Promotion ordering for multi-claim inputs

A single text may extract multiple claims. Does promotion happen for
all claims before any walker call, or per-claim (promote one, walk
one, promote next, walk next)?

**Recommendation: promote-all then walk-all.** Matches the
knowledge-building model: all asserted knowledge is established
before verification consults it. Per-claim ordering would create
order-dependent verdicts (claim 2's walker might or might not see
claim 1 as a premise depending on order).

## Risks

**Soundness boundary.** The dual designation is the soundness
preservation mechanism — every verdict makes its grounding source
explicit. A bug in chain-composition tracking (a `_given_assertion`
chain mis-tagged as plain `verified`) would re-introduce the false
verifies §3.2 forbids. Walker tests need to cover the boundary
cases carefully.

**Corpus expectation drift.** Step 5 updates 12+ corpus cases'
expected verdicts. Per D49, re-runs of derivation_corpus after the
update validate the change; pre-/post- comparison documents the
delta. The post-update baseline becomes the new measurement floor.

**Verdict-upgrade write-back.** Q-Upgrade's `mark_externally_verified`
introduces a new write path. Race-condition risk in concurrent
sessions is irrelevant for v0.15 (single-process SQLite), but
the audit log needs to capture the transition for retraction
propagation (a retracted KB row that triggered an upgrade should
arguably re-flag the Tier U row back to `asserted_unverified`,
though this is a propagation question for follow-up — see
v0.16 D14's retraction cascade work).

**Chat-wrapper extraction cost.** Q-ChatWrapperSource adds an
extra LLM call per turn. Acceptable v0.15 cost; if Phase 10.5
benchmarks show this is dominant, the user-message extractor can
be a smaller / cheaper model than the draft extractor.

## What this design does not commit to

- Cross-session Tier U persistence. Per-session is the v0.15
  commitment; broader persistence is v0.16 deployment policy.
- A `derived` Tier U status (rows written by the walker as
  derivation products rather than by promotion). The current design
  only writes Tier U via the promotion path; derived rows would be
  a Cluster-3 / v0.16 extension.
- Retraction propagation through verdict-upgrade. A retracted KB
  row that previously triggered a Tier U status upgrade arguably
  ought to re-flag the row back — explicit follow-up, not in scope.
- Updates to Cluster 3 (predicate canonicalization). Next session.
- Rc.11 tag or Phase H closure.

## After operator confirmation

Implementation proceeds in the six steps above. Each step's commit
lands green (full test suite passes). Open implementation-design
questions surface as they emerge; resolve with operator at the
natural decision points.

The cumulative API cost is bounded: design work is free, step 1-4
implementation has minimal API spend, validation runs at $1-3 per
derivation_corpus pass per D49 budget for 2-3 runs total. Total
session cost estimate: $2-5.
