# Phase B Plan — D16 (object-conflict belief revision) + D6 (persistent retraction) + DB consistency

*Session between Phase A cleanup (`v0.15.0-rc.2`) and Phase 10.5 calibration.
Unlike Phase A (hygiene only), Phase B is genuine capability extension: the
walker gains a new contradiction-detection path, Tier U's write-path closure
starts consulting `single_valued`, and the retraction propagator gains a
persistence layer. The audit chain has not covered this work; stash-and-verify
and the surfaced design questions below are the load-bearing substitutes.*

Five clusters, run in order: **B1 → B2 → B3 → B4 → B5**. Each is its own commit
so any cluster can be rolled back independently.

---

## Repo layout (post-Phase-A refresher)

`src/aedos/` is the five-layer pipeline plus support modules. Paths relevant to
Phase B (the session prompt's path hints are corrected here against the actual
tree):

- `database.py` — *top-level*, not under `layer3_substrate/`. Schema + migration.
- `layer4_sources/tier_u.py` — Tier U source. *Not* `layer3_substrate/tier_u.py`.
- `layer4_sources/walker.py` — the derivation walker.
- `layer3_substrate/predicate_translation.py` — the oracle carrying `single_valued`.
- `layer5_result/{retraction,aggregator,contradiction_tracer}.py` — D6 surface.
- `audit/log.py` — `log_event(conn, …)` function form (post-D8/A4).
- `pipeline.py` — `build_pipeline`, the single wiring definition.

Baseline at session start: `pytest tests/ -q` → **701 passed, 1 skipped, 11
deselected**.

---

## Cluster B1 — database schema consistency

**Defect.** `database.py`'s `CREATE TABLE predicate_translation` writes
`single_valued INTEGER DEFAULT 0` (line 41); the N6 migration `ALTER TABLE`
(line 133) and architecture §5.2 both specify `INTEGER NOT NULL DEFAULT 0`. A
fresh DB gets the nullable form, a migrated DB the non-nullable form —
internally inconsistent though functionally equivalent for non-NULL inserts.

**Fix.** One line: `single_valued INTEGER NOT NULL DEFAULT 0` in the `CREATE
TABLE` block. The `ALTER TABLE` and architecture §5.2 already match (verified:
architecture line 307 prints `NOT NULL`; `database.py:133` has `NOT NULL`).

**Risk.** None expected. `_generate_and_store` always inserts an int
(`int(raw.get("single_valued", 0) or 0)`); no code inserts explicit NULL.
Inserts omitting the column get the default `0`. Confirm with `pytest tests/ -q`.

**Commit.** `Cluster B1: database.py CREATE TABLE matches ALTER TABLE for single_valued`

---

## Cluster B2 — walker object-conflict belief revision (D16, walker half)

### Today

`walker._direct_lookup` (in `walker.py`) does, for the Tier U read path:
1. exact-match lookup → `verified`;
2. polarity-flipped lookup (belief revision, fixup-3) → `contradicted`.

It does **not** detect object conflicts: `(Asa, lives_in, Boston, pol=1)`
against a Tier U row `(Asa, lives_in, NYC, pol=1)` abstains, even when `lives_in`
is functional.

### The unified belief-revision rule

The session prompt's B2-Step-1 and B3 four-case specs are imprecise for
**negated** claims. The precise rule, derived from the meaning of a functional
predicate ("a subject has at most one value"):

Two assertions about the same `(party, subject, predicate)`, `(O₁,pol₁)` and
`(O₂,pol₂)`, are **mutually contradictory iff**

> `(O₁ == O₂ ∧ pol₁ ≠ pol₂)`  — direct negation, any predicate
> `(O₁ ≠ O₂ ∧ pol₁ == pol₂ == 1 ∧ functional)`  — functional positive object conflict

and a positive functional assertion `S P O′` additionally **entails the
negation** of `S P O` for any `O ≠ O′`.

Applied to the walker's Tier U read path for a claim `(O_C, pol_C)` against a
currently-valid Tier U row `(O_R, pol_R)`:

| Tier U row | claim | functional? | verdict | trace marker |
|---|---|---|---|---|
| `(O, 1)` | `(O, 1)` | — | verified | (exact match) |
| `(O, p)` | `(O, 1−p)` | — | contradicted | `polarity_conflict` |
| `(O′, 1)`, O′≠O | `(O, 1)` | yes | **contradicted** | `object_conflict` |
| `(O′, 1)`, O′≠O | `(O, 0)` | yes | *abstain* (Decision 1 — conservative) | — |
| anything else | | | no Tier U grounding (→ KB/Python/abstain) |

**Decision 1 (resolved at the post-plan check-in).** The negated-claim
direction — verifying `(O,0)` because a functional `(O′,1)` entails it — is
**not** implemented. Negated claims against a different functional value
abstain. B2 implements only the `object_conflict → contradicted` row (positive
claims). The `negation_implied` marker is dropped.

Only **positive** Tier U rows (`pol_R == 1`) of a *different* object bear on the
claim — a negative Tier U row about a different object (`¬(S P O′)`) is
uninformative. So B2 needs a single new Tier U query: positive, currently-valid,
same `(party, subject, predicate)`, `object ≠ claim.object`.

**Why the polarity guard matters (a finding, not a guess).** The literal
B2-Step-1 ("same `(party, subject, predicate, polarity)`, different `object`,
functional → CONTRADICTED") would also fire for `pol == 0`: claim
`(lives_in, Boston, 0)` vs Tier U `(lives_in, NYC, 0)` — "Asa doesn't live in
Boston" vs "Asa doesn't live in NYC" — which are perfectly **consistent** (Asa
lives in a third place). The guarded rule fires the contradiction only when
**both** assertions are positive. The unguarded rule would also surface in B3,
where it self-closes contrastive corrections (see B3 below). The guard is a
correctness fix, not optional — shipping the unguarded rule ships a bug.

### Implementation

- **`TierU.lookup_object_conflict(claim, current_time) → LookupResult`** — a new
  public method returning currently-valid, non-retracted, **positive** Tier U
  rows for the same `(asserting_party, subject, predicate)` whose `object`
  differs from the claim's. Literal-match style (mirrors `_query_current` with
  `object != ?` and `polarity = 1`); no stage-2/3 broadening (cross-predicate
  object conflict is a possible v0.16 refinement, flagged not built).
- **`walker._direct_lookup`** — after the polarity-flip block, before KB
  verification, **for positive claims only**: call `lookup_object_conflict`; if
  rows found, `consult` the predicate translation oracle for `single_valued`;
  if functional, return `contradicted`. Negated claims do not enter this path
  (Decision 1). The existing polarity-flip edge also gets the
  `belief_revision: polarity_conflict` marker so both revision kinds are
  distinguishable in the trace.
- **LLM-call accounting.** The `consult` call is a cache hit in the assembled
  pipeline (Layer 2 routing consults every predicate before the claim reaches
  Layer 4), so `_direct_lookup` keeps returning `llm_delta = 0` — consistent
  with the existing convention there (the KB verifier also consults the oracle
  and the path reports 0). Budgets are advisory (§6.4).

### Tests (`test_walker_with_substrate.py`, new class `TestWalkerObjectConflictVerdicts`)

The tests use the existing `_make_full_system(single_valued=…)` helper —
`MockTransport` returns the chosen `single_valued` for any inline-generated
predicate, so the tests are self-contained and do **not** modify the seed pack.

1. Functional object-conflict → `contradicted`, trace marker `object_conflict`.
2. Multi-valued object-difference → `no_grounding_found`; trace does **not**
   contain `object_conflict` (the stronger assertion the prompt asks for).
3. Polarity belief revision still works (regression).
4. Negated claim vs different-valued functional prior → `no_grounding_found`
   (Decision 1 — conservative; no `object_conflict` marker in the trace).
5. Negated-claim object difference, both negative, functional → *not*
   `contradicted` (the polarity-guard regression).

**Stash-and-verify.** Stash `walker.py` (reverts to rc.2): tests 1 and 2's
marker assertion fail; unstashed, all pass.

**Commit.** `Cluster B2: walker detects object conflicts on functional predicates (D16, walker half)`

---

## Cluster B3 — Tier U write-path respects single_valued (D16, Tier U half)

### Today

`TierU.write`'s conflict query is `… AND (object != ? OR polarity != ?)` — it
closes a prior row on **any** object difference. Correct for functional
predicates, data loss for multi-valued ones ("I like pizza" then "I like sushi"
should be two parallel rows).

### Write-path closure rule

Close a prior row **iff it contradicts the new claim**, by the same unified rule
as B2:

- **Same object, opposite polarity** → close (direct negation, any cardinality).
- **Different object, both polarity 1, functional** → close (functional revision).
- **Different object, both polarity 1, multi-valued** → *parallel write*, leave prior open.
- **Different object, different polarity** → *parallel write*, leave prior open.
- **Different object, both polarity 0** → *parallel write*, leave prior open.
- Same object, same polarity → idempotent (unchanged).

**Architecture check.** §6.1's write-path prose covers only the polarity case
("asserted X, now asserts not-X"); it is **silent** on object-conflict closure.
The current code (close on any object diff) is already *broader* than §6.1, and
D16's functional object-conflict closure is broader still. The architecture
wording lags the code — flagged as a v0.16 follow-up in B5 (not edited here, per
the session's "don't touch the architecture for capability changes" rule).

**The contrastive-correction finding.** §4.1 says contrastive corrections ("I
live in NYC, not Boston") extract *both polarities in parallel* — here
`(lives_in, NYC, 1)` and `(lives_in, Boston, 0)`. The literal four-bullet spec
("different polarity → close") would make writing the second half **close the
first half**: the system would believe "Asa lived in NYC until now, and doesn't
live in Boston" — dropping the current "lives in NYC". The rule above closes
"different object, different polarity" as a *parallel* write, which is correct.
This is why B3 implements the refined rule, not the literal bullets.

### Implementation

- Idempotency check — unchanged.
- Replace the single conflict query with two: (a) same-object opposite-polarity
  rows (always close); (b) different-object positive rows when the new claim is
  positive — close only if `single_valued == 1`, else parallel.
- `single_valued` is read via the `predicate_translation` oracle the `TierU`
  already holds (`self._oracle`). The `consult` is a cache hit in the pipeline
  (routing precedes the write). When `self._oracle is None` (the bare
  `_tier_u()` test helper), default to multi-valued (the §5.2 conservative
  default — never a false closure).
- `WriteResult` may need `closed_row_ids` (plural). In well-behaved pipeline
  data ≤1 row closes per write; the plural is for stale-data robustness. Final
  shape decided at implementation after grepping consumers — back-compat
  `closed_row_id`/`contradiction_closed` retained if any non-test consumer reads
  them.
- Audit events `tier_u_row_closed` / `tier_u_parallel_assertion`, added in the
  file's idiom (gated on the `audit_log` constructor flag, like the existing
  `row_created`). **Finding:** `build_pipeline` does not pass `audit_log` to
  `TierU`, so these events — like the pre-existing `row_created` — are inert in
  the deployed pipeline. Resolving that (wire `audit_log`, or finish the D8
  cleanup by gating `TierU` logging on `db`) is raised at the B3 check-in and
  flagged for v0.16; it is not silently changed here (it would alter the
  pipeline's audit-log contents right before calibration).

### Tests (`test_tier_u.py`)

`test_write_different_object_closes_prior` currently uses `holds_role`
(multi-valued) and asserts `contradiction_closed is True` — it asserts the **old
"always close" behavior** and *must be updated*, not preserved (per the
discipline note on test-weakening). It is split into the functional case (test
1) and the multi-valued case (test 2).

1. Functional, different object, both positive → prior closed (regression of
   the *intended* closure; uses a seeded functional predicate row).
2. Multi-valued, different object, both positive → both rows open (new).
3. Functional idempotent write → no new row.
4. Multi-valued idempotent write → no duplicate row.
5. Polarity flip (same object) closes prior, functional and multi-valued.
6. Contrastive correction — different object, different polarity → both open.
7. Both-negative different object → both open.

**Stash-and-verify.** Stash `tier_u.py` (reverts to post-B2): test 2 fails (old
write closes the multi-valued prior). Tests 1, 3, 4, 5 pass both ways.

**Interaction with B2.** B2 test 1 only *writes* the prior (NYC) and *walks* the
conflicting claim (Boston) — `walk` does not write Tier U, so B3 does not affect
it. Re-run after B3 to confirm.

**Commit.** `Cluster B3: Tier U write-path respects single_valued (D16, Tier U half)`

---

## Cluster B4 — persistent retraction propagation (D6)

### Today

`RetractionPropagator._trace_index` is in-memory, rebuilt per process. Verdicts
recorded in process N are invisible to process N+1's retraction propagation, so
§7.3's over-time soundness breaks across restarts.

### The architectural decision: Option β (audit-log replay)

**Decision: Option β.** The decision criterion is "what is already in the audit
log." Evidence: `aggregator.py:108-114` already logs a `verdict_recorded` event
per verdict with `event_data = {"verdict": …, "source_rows": [[table, id], …]}`
and `event_subject = "claim:{cid}"`. **The audit log already contains
`source_rows`** — every datum a replay needs. Per the session prompt's own
criterion, β (audit-log replay) is therefore viable and preferred: it adds no
new table to keep consistent with the audit log, and the persistence *already
happens* — β only adds a consumer.

Reasoning recorded here as required before implementation:

- **Option α** (new `verdict_traces` table) would duplicate what
  `verdict_recorded` already persists, creating a table that must be kept
  consistent with the audit log. Rejected as redundant infrastructure.
- **Option β** reuses the existing `verdict_recorded` events. Startup cost
  scales with audit-log size — fine for v0.15 (the performance smoke test
  confirms 1000 verdicts replay in well under a second).

### Implementation (β is small — no table, no aggregator change)

- **`RetractionPropagator.replay() → int`** — new method. Reads
  `verdict_recorded` events in `id` order, rebuilds `_trace_index` and
  `_verdict_index` exactly as the in-process `record_verdict_trace` calls would
  (last-wins per `claim_id`; `source_rows` lists from JSON converted back to
  tuples; the `claim:` prefix stripped from `event_subject`). Idempotent.
  Returns the count hydrated.
- **`propagate_retraction` is unchanged.** It walks the in-memory index; `replay`
  merely hydrates that index from disk. This is the key consistency property: a
  replayed propagator's state is *identical* to what an in-process propagator
  would hold, so the replay path and the live path never diverge. Replay
  processes only `verdict_recorded` (not `verdict_retracted`) — because the
  in-process model does not mutate the index on retraction either, so mirroring
  it keeps the two paths consistent. Re-retracting an already-retracted verdict
  is harmless (it only emits another audit record) and is identical to the
  existing in-process behavior of calling `propagate_retraction` twice.
- **`build_pipeline`** calls `propagator.replay()` right after constructing the
  propagator — `build_pipeline` is the process's startup (`app.py` builds it
  once, lazily; the benchmark builds it per process).
- **`ContradictionTracer`** — `trace_contradiction` already reads
  `self._propagator._trace_index`; with the propagator replayed it reads
  persisted verdicts for free. The only change: the tracer replays the fallback
  propagator it constructs itself when given a `db`. Its `retracted_at` UPDATE
  is unchanged.
- `aggregator.py` needs **no change** — the `verdict_recorded` persistence
  already exists.

### Tests (`test_retraction_propagator.py`)

1. **Cross-process persistence.** A file-backed DB (`tmp_path`, not `:memory:` —
   `:memory:` is per-connection). "Process 1": open DB, aggregate claims so
   `verdict_recorded` events are logged, close the connection. "Process 2":
   reopen the same file, construct a fresh `RetractionPropagator` (empty index).
   The discriminator is **built into the test**: assert
   `propagate_retraction(...) == []` *before* `replay()` (process-1 verdicts
   invisible), then `replay()`, then assert `propagate_retraction` now reaches
   them. This in-test before/after assertion is the stash-and-verify, made
   permanent — more robust than git-stashing a method the test must call.
2. **Replay consistency.** After `replay()` the index matches what was recorded
   — no verdict missing, none hallucinated.
3. **Existing in-process tests unchanged** (regression — they construct
   `RetractionPropagator()` with no db and never call `replay`).
4. **Performance smoke.** 1000 verdicts; `replay()` + `propagate_retraction`
   complete well under a second.

**Honest note on the test mechanism.** A single test process cannot fork a real
OS process. The test simulates the process boundary as: a *new SQLite connection
to the same file* + a *new `RetractionPropagator` object with an empty index*.
That is the architecturally meaningful boundary for β (the persistence medium is
the file; the volatile state is the propagator's dict). What it does not cover —
two concurrent OS processes writing the log — is out of D6's scope. This
limitation is stated in the Phase B report rather than papered over.

**Commit.** `Cluster B4: persistent verdict traces (D6, option β)`

---

## Cluster B5 — planning update + Phase B report

- `docs/v0.16_planning.md`: mark D6, D16, and the DB-consistency fix **Resolved
  (Phase B)** with commit refs (D17-style notes; original entries kept).
  D5/D9/D10/D13/D14/D15 stay deferred.
- v0.16 follow-ups to record: architecture §6.1/§6.4 wording lag for
  object-conflict closure; `TierU`/oracle `audit_log` unwired in
  `build_pipeline`; the stale `lives_in` seed `reason` (see below);
  cross-predicate object-conflict broadening.
- `docs/phase_B_report.md`: cluster summaries, per-delta before/after, the B4
  α/β decision, the B3 design resolution, stash-and-verify results, test-count
  delta, follow-ups.
- Final verification: `pytest tests/ -q`; `benchmark --validate-harness`;
  `pytest --run-calibration`. Tag `v0.15.0-rc.3`.

**Commit.** `Cluster B5: planning update + Phase B report`

---

## Surfaced design questions and findings

**Question 1 — RESOLVED: Abstain (conservative).** Should the walker **verify**
a negated claim that a functional Tier U assertion implies (Tier U
`(Asa, lives_in, NYC, 1)`; claim `(Asa, lives_in, Boston, 0)`)? The logically
sound verdict is `verified`, but §8.1 does not explicitly settle it. **Decided
at the post-plan check-in: abstain.** The walker implements `object_conflict →
contradicted` for positive claims only; the negated-claim `negation_implied →
verified` path is *not* built. A negated claim against a different functional
value falls through to abstain — the accepted §3.2 false-abstain cost. This
keeps Phase B's belief-revision surface minimal.

**Finding A (not a question — correctness).** The polarity guard: B2's
object-conflict path and B3's closure rule fire only when **both** assertions
are positive. The literal specs would produce false contradictions on negated
claims (and self-close contrastive corrections, B3). Implemented as the guarded
rule; surfaced here and at the B3 check-in for visibility.

**Finding B (B4 — surfaced, decided).** Option β chosen on the evidence that
`verdict_recorded` already carries `source_rows`. Re-confirmed at the B4
check-in before implementation.

**Finding C (architecture wording lag).** §6.1 (Tier U write path) and §6.4
(walker) describe only polarity-based revision; D16 adds object-conflict
revision. Per the session rule, the architecture is **not** edited here —
flagged as a v0.16 follow-up.

**Finding D (`TierU` audit logging unwired).** `build_pipeline` does not pass
`audit_log` to `TierU` or the oracles, so their audit events are inert in the
deployed pipeline. B3's new events are added idiomatically (same gating as the
adjacent `row_created`); the wiring gap is flagged for v0.16 / raised at the B3
check-in, not silently changed.

**Finding E (stale `lives_in` seed rationale).** `seeds/predicate_translation.json`
classifies `lives_in` as `single_valued: 0` with the rationale *"single_valued
is not consulted on the user-authoritative route."* D16 makes that rationale
**false** — `single_valued` is now consulted on the Tier U (user-authoritative)
route. Whether `lives_in` should be reclassified is a borderline seed-pack
judgment (multiple residences are plausible; `0` stays defensible as the
conservative default). The seed pack is **not** touched in Phase B — changing a
seed `single_valued` alters calibration inputs and is a deployment decision, not
walker/Tier-U code. Flagged as a v0.16 follow-up. Consequence to note: D16's
headline `lives_in` example does not fire in the *seeded* deployment until/unless
`lives_in` is reclassified — but the *capability* is correctly implemented and
fires for every `single_valued = 1` predicate.

---

## Discipline / non-goals

- **Deferred deltas stay deferred.** D5 (KB-sourced neighbor enumeration — the
  natural extension of D16's belief revision) and D14 (retraction cascade — the
  natural extension of D6) are **not** pulled forward. D9/D10/D13/D15 likewise.
- **The seed pack is not modified** (Finding E).
- **The architecture document is not modified** for capability changes
  (Findings C, and the §6.1/§6.4 lag).
- **No test weakening.** Where a regression test asserts old behavior
  (`test_write_different_object_closes_prior`), the test is *updated to the new
  semantics*, never the new behavior weakened to the old test.
- **Stop-and-surface trigger.** If B4's persistence touches more than
  `retraction.py` + `pipeline.py` + `contradiction_tracer.py`, or B2/B3 reveal
  deeper substrate interactions, stop and surface rather than push through.
