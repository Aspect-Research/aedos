# Aedos v0.16.1 — Forward Planning

Status of v0.16: released and tagged (`v0.16.0`, merged to `main`). It delivered the structural
rebuild (multi-property substrate, discover/verify composition, partial-TMS provenance,
verify-every-claim, per-claim corrections, temporal T1), held the soundness invariant (0%
false-**verified** across both live Medium-Bar runs and the PopQA smoke), modestly beat v0.15
(57.4% → 60.7% accuracy, multi-hop +10pp), and runs ~3.5× slower.

This document plans **v0.16.1** — the set of gaps the v0.16 review deferred, plus what the
Medium Bar and the sister-repo PopQA smoke newly exposed. It is a planning document for your
review; per the v0.16 flow, detailed per-file change-specs and a confirmation gate follow approval.

**Scope-status tags** used below: *discovered-after-v16* (surfaced by review/benchmarks, not on
the v0.16 plan), *not-implemented-in-v16* (planned, deliberately deferred), *partially-implemented*
(some landed, the rest deferred), *architectural-deferral* (a larger redesign intentionally postponed).

**Standing rule (unchanged):** every change preserves §3.2 — never false-verify, never
false-contradict; abstention is the safe outcome; replacements land *before* the hardcodes they
replace are deleted, with regression pins re-proven green.

---

## Overview of the gap

v0.16's posture is *sound but over-abstaining*, with one newly-found soundness crack and a pile of
deliberately-deferred architectural debt. The work splits into four bands:

- **A. Soundness (highest priority).** A real false-**contradict** exists and is currently
  *unmeasured* — the harnesses count only false-verifies. (Item 1.)
- **B. Coverage — the core promise (answer ordinary factual claims).** Two concrete drivers of the
  ~46% false-abstain rate: profession/occupation copulas never ground, and there is no genuine
  cross-source/compound derivation. (Items 2, 3.)
- **C. Architectural debt — "knowledge in code."** Remaining Wikidata hardcodes, dormant wired-but-
  inert mechanisms, and deferred simplifications. (Items 4, 5, 6.)
- **D. Depth — larger postponed mechanisms.** Event-relative temporal bounds, the discovery-cost/
  latency budget, and the full retraction cascade. (Items 7, 8, 9.)

A suggested sequencing is at the end.

---

## A. Soundness

### 1. Close the false-contradict blind spot  — *discovered-after-v16*

**Problem.** §3.2 forbids false-*contradict* as much as false-*verify*, but neither benchmark
harness counts false-contradicts — so the "0% false-verified" headline hides them. The PopQA smoke
contains a confirmed one: Tadhg Dall Ó hUiginn's birth "c. 1550" is graded **contradicted** ("the
source indicates 1550-01-01 instead") even though the KB *agrees* (1550). Root cause:
`kb_verifier._value_matches` only runs the year-aware compare when the claim string matches
`_BARE_YEAR_RE` (`^[+-]?\d{4}$`); "c. 1550" fails the regex, the literal compare fails, and because
`born_on` is a single-valued date predicate with a KB value present, the verdict flips to
contradicted. The Medium Bar also has a few stable `gt=verified → contradicted` cases (`csu_003`;
`pt_004`/`pt_006` have softer gold) that the v0.16 review reclassified as "noise" without a
per-case determinism split or a false-contradict counter.

**Fix.** (1) Add a symmetric false-contradict counter to both harnesses (count `gt≠contradicted &
verdict=contradicted`, broken out by `gt=verified` vs `gt=abstain`) — pure measurement, closes the
blind spot. (2) Fix approximate-date handling: strip circa/approx prefixes (`c.`, `circa`, `ca.`,
`~`) on the *claim* side before the year compare, or — strictly safer — when the claim value is an
approximation and the KB year matches, downgrade contradicted → abstain. This only ever loosens a
contradiction toward abstain (tightens §3.2) and cannot create a false-verify; preserve the
precise-ISO strict compare. (3) Once the counter exists, triage the stable Medium-Bar contradicts
case-by-case and fix only the confirmed-unsound ones (do **not** code to a debatable gold label).

**Relationships.** Shares the date/value-type path with the WS6 temporal work and with item 7; the
counter is naturally part of the standing harness (item 6c). This is the only item touching the
paramount invariant, so it leads.

---

## B. Coverage — the core promise

### 2. Ground ordinary factual claims — occupation copulas  — *discovered-after-v16*

**Problem.** The PopQA smoke's "0% attempt" is partly the *drafting* LLM abstaining (12/15 drafts
were "I don't have reliable information…" — Aedos faithfully passed through a non-answer, not a
bug). The real Aedos gap is the 3 cases where the draft *did* assert a profession ("Robby Krieger
is a guitarist"): the occupation claim was extracted and the entity resolved (his `member_of` The
Doors verified), but the occupation never grounded. Cause: profession copulas extract as
`instance_of` → P31, and a person's P31 is `Q5` (human), never the occupation; meanwhile the WS1
candidate-binding mechanism is only *half*-active — the verification-side copula arbitration landed
(it blocks a wrong contradiction) but the discovery side never emits P106 (occupation) as a
candidate binding for `instance_of`, so there is no positive P106 path at all.

**Fix.** Have the predicate-metadata oracle emit `candidate_kb_properties=[P106]` (plus occupation
`object_entity_types`) for the `instance_of` predicate, so `meta.bindings = [P31, P106]`; the
existing binding loop then *verifies* the asserted occupation against the subject's actual P106
set (no new verify code). Gate the positive P106 match on the object being a confirmed
occupation/profession class (extend `_object_satisfies_value_type`'s subsumption check to the
positive path, **fail-closed**) so a non-profession copula ("X is a river") cannot false-verify.
Keep P106 `single_valued=0` so a wrong occupation abstains, never contradicts. Add a copula-
occupation regression case — the Medium Bar has none, which is why this shipped unseen.

**Relationships.** Extends the WS1 multi-property substrate (not a new mechanism). An alternative
discovery route is activating SLING (item 5). The `depth_exhausted` abstention here is a *symptom*
this fix removes, so it sits upstream of the latency budget (item 8). Orthogonal to the 12
draft-abstention empties (a drafting-model limit, separate item if pursued).

### 3. Multi-source derivation / cross-source unification  — *partially-implemented* (least mature)

**Problem.** Aedos has no genuine cross-source *unification*. Compound "X and Y" statements are
split into independent claims and recombined only by a boolean AND in the benchmark runner (not a
traced aggregator operation), so a compound abstains whenever any conjunct does. Worse, the Python
tier cannot take *retrieved* premise values as inputs — `PythonVerifier.verify` sees only the
claim's own three slots — so any derivation of the form "fetch value V, then compute over V"
("founded before 1800", "10² is 100 and 100 > 50", "born before") is structurally unreachable. The
`ProvenanceTerm` AND-op that would represent a joined derivation exists but is never constructed
(every premise is OR-appended). `cross_source_unification` is the only mode below v0.15 (57.1%).

**Fix.** Three independently-shippable steps. **Step 0 (S):** convert the vague-class abstentions
(`csu_007` "a town in the US", `csu_013` "a state that borders NY") to sound verifies via a
class-instance check through `verify_transitive_path`/P31. **Step 1 (S–M):** move the compound
conjunction into the aggregator as a *traced* AND op (same monotone semantics — contradicted-wins,
verified iff all-verified — so verdicts don't change, but it gains observability + a retraction
footprint, and stabilizes the noisy metric). **Step 2 (M–L, the D10 decision):** give the walker a
premise-input channel — resolve referenced premise values via Tier-U/KB, thread them into
`PythonVerifier.verify(premises=…)`, and record each as an AND-child in the `ProvenanceTerm`. Gate:
an `asserted_unverified` premise must carry the chain-flag (so the verdict surfaces as
`verified_given_assertion`, never laundered to a plain verify), and fail-closed on any premise miss.

**Relationships.** Depends on the WS2 discover/verify machinery and the WS3 AND-term (both exist).
D10 is the canonical open decision. Step 0 shares primitives with multi-hop/geo; Step 2 reuses the
resolver path used by `kb_quantitative`/`kb_interval` and must respect the assertion-conditionality
duals. Step 1's deterministic rollup also de-noises the cross_source metric for everything else.

---

## C. Architectural debt — "knowledge in code"

### 4. Remove the remaining hardcodes  — *partially-implemented* (deferred deletions)

**Problem.** Four Wikidata-specific hardcodes still sit in CORE (above-the-`kb_protocol`-seam)
control flow: the geo cluster in `kb_verifier` (`CONTINENT_QIDS`, `_LOCATION_KB_PROPERTIES`,
`_location_disjoint` — D2; and the P361 part_of bridge — D3); the persona-subject raw-SQL guard in
`walker` (D5); the walker's neighbor/qualifier P-id tables (`_D5_NEIGHBOR_PROPS_BY_RELATION`,
P580/P582 keys); and `wikipedia_normalizer` reaching around `KBProtocol` into adapter-private
methods. None is unsound today — each is a conservative guard or fail-open — but each is Wikidata
vocabulary in code the architecture says belongs behind the seam or in prompt/KB/oracle.

**Fix** (each: replacement *before* deletion, pins re-proven). **Geo (D2/D3):** activate a
*discovered* disjointness mechanism first — give the dormant `substrate_exceptions` nogood cache a
production writer keyed on the resolved subject Q-id, or push `_location_disjoint` behind a
`KBProtocol.geographic_disjoint`/containment op implemented in `WikidataAdapter` (where the closed
7-continent set legitimately lives as a Wikidata fact); only then delete the in-CORE constants,
once Warsaw/Rome/Thames/Vatican/Monaco stay green. **Persona (D5):** first implement the WS4
persona-subject → `user_authoritative` routing (the deletion's blocked precondition), then drop the
guard and its raw SQL (replace with a `tier_u.has_identity(party, subject)` method so no module
reaches into `_db`); re-prove the persona-abstain pin. **Neighbor/qualifier + normalizer:** pure
behavior-neutral plumbing — move the P-id table into the adapter's `enumerate_neighbors`, route
P580/P582 behind a protocol accessor, and add `KBProtocol.search`/`fetch_types` ops so the
normalizer stops reaching into adapter internals (+ move the Wikipedia endpoint to config).

**Relationships.** Geo depends on activating the dormant nogood cache (item 5). Persona
**hard-depends** on the un-implemented WS4 routing — and that same `user_subject_required` notion
overlaps the Validator anomaly check (item 6). All gated by the contract's SS1–SS5
replacement-before-deletion ordering. The normalizer/neighbor leaks are the documented
core-vs-Wikidata seam residuals.

### 5. Resolve the dormant mechanisms  — *partially-implemented*

**Problem.** Five spec-named capabilities are wired but inert (all coverage-only; none can
currently mis-verdict): SLING distant-supervision (`propose_bindings` always returns `[]` — the
oracle tool schema emits no sample Q-ids); the binding-NOGOOD veto (`_binding_vetoed` → `vetoes` —
no production writer creates the `subsumption`-kind row it reads, and the reader keys on the NL
subject string while the only writer keys on resolved Q-ids); the three-valued `_interval_holds_at`
(unit-tested, no verdict-path caller); and the qualifier-keyed temporal lookup + `status_started/
ended` KB arm (data-model-inert — P580/P582 are qualifiers, P571/P576 are statement values).

**Fix** (honoring "no hand-seeded guards"). **SLING:** activate *gated* — add a `sample_subject_qids`
oracle field + prompt line, behind a config flag, `single_valued=False` (verify-only, never
contradict), with review + a regression pin before it can drive a verdict. **Binding-NOGOOD:**
**remove** the inert veto surface — a veto that *suppresses contradictions* is the dangerous
direction; do not add a writer. **holds-at-T:** keep as a tested primitive, or wire it into the
event-relative resolver (item 7). **Qualifier/status arms:** keep inert (the `founded_in_year`/
Tier-U paths already cover them) or drop the dead `status_*` seed rows; do **not** extend the
generic lookup to interpret qualifier slots (that would fork a second, unreviewed verdict path).

**Relationships.** SLING is an alternative discovery route for item 2's occupation grounding;
binding-NOGOOD relates to item 4's discovered-disjointness; `_interval_holds_at` is exactly the
consumer item 7 needs.

### 6. Deferred simplifications  — *partially-implemented*

**Problem.** (a) **Layer-2 Router/Validator** are vestigial — no production verdict flows through
them (the walker routes inline via `meta.routing_hint`); only tests import them — *but* the
Validator's three anomaly checks (`user_subject_required`, `distinct_slots`, `object_type`) are the
only implementation of the §4.2 routing-anomaly contract. (b) The **Python tier** is one-shot
LLM-codegen, flaky run-to-run on the exact arithmetic/date comparisons it exists for. (c) There is
**no standing eval harness** — the Medium Bar runs via ad-hoc `scripts/` wrappers and the PopQA path
lives in a sibling repo, with stale v0.15 pass/fail thresholds.

**Fix.** (a) Relocate the three anomaly checks into the live path as *fail-closed* guards (or
document them superseded by the existing object-type gate + persona routing), then delete the
classes and retarget the 3 test files. (b) Add a **deterministic front-end** to the Python tier for
numeric comparison, date/year ordering, and simple arithmetic — return a verdict only on an exact
parse, else `None` → LLM-codegen fallback → abstain (byte-safe for §3.2; optionally majority-vote
codegen). (c) Promote `benchmark.py` into a committed CLI: fold in the per-instance watchdog +
live-FV counter + the new false-**contradict** gate (item 1), add a pinned offline regression set
(`mhd_018`, the circa-date case, a copula-occupation case) runnable mocked in CI, and one live
entry point that subsumes the PopQA reader; thin the 21 one-off scripts.

**Relationships.** The Validator's `user_subject_required` overlaps the persona guard (item 4 — do
them together). The Python deterministic front-end de-noises both the false-contradict triage (item
1) and cross_source (item 3). The **standing harness is a prerequisite** for safely calibrating the
latency budget (item 8) and every data-driven deferral.

---

## D. Depth — larger postponed mechanisms

### 7. Temporal: event-relative bounds (`valid_from_ref`/`valid_until_ref`)  — *partially-implemented* (D59)

**Problem.** Event-relative bounds ("before the acquisition", "after the election") can't be
represented with directional semantics. `valid_from_ref`/`valid_until_ref` don't exist; extractor
Rule 16 collapses before/after/during onto the single `valid_during_ref`, which is itself inert
(persisted, never read by any grounding path). v0.16 shipped T1 (absolute date-in-object endpoints)
but not this.

**Fix.** **Stage 1 (low risk):** add the two Optional fields to `Claim`/`TemporalScope` (+ tool
schema, triage, Tier-U, an additive DB column); split Rule 16 (before → `valid_until_ref`,
after/since → `valid_from_ref`, during stays `valid_during_ref`); tighten the corpus runner to
check them. Write-only metadata → zero verdict risk, restores the lost directional info.
**Stage 2 (deferrable):** an event-relative resolver — require a *backing* event claim (align Rule
16 with Rule 9's "A when B"), resolve it to an absolute date via the existing KB date predicates,
convert before/after into an effective absolute bound, and feed the dormant `_interval_holds_at`.
Fail-closed: missing/ambiguous/imprecise event → abstain, never a terminal verdict.

**Relationships.** Stage 2 is the consumer that activates `_interval_holds_at` (item 5); needs the
backing-event-claim mechanism; shares the date path with item 1. Wikidata has no native
event-relative bounds, so the resolution must happen in Aedos core (ground the event, then compare).

### 8. Latency / discovery-cost budget  — *not-implemented-in-v16*

**Problem.** ~3.5× slower than v0.15 (median 40s/claim, 138 min/run) because PATCH-A — the SS3
soundness fix — routes every KB-enumerated discovery neighbor through one rate-limited (5/s) SPARQL
ASK. The only cost gate is wall-clock; nothing meters the ASKs, and there is no positive-result
memo (only the negative nogood cache + query-string HTTP cache).

**Fix** (increasing risk). **(A)** A process-scoped positive-result memo for `verify_transitive_path`
keyed by `(relation, source_qid, target_qid)` — zero soundness risk (memoizes only definite,
error-None answers; bounded TTL) and collapses the repeat-ASK cost across a fan-out and a run.
**(B)** Batch the per-neighbor confirmation into one SPARQL `VALUES`/`SELECT` instead of N serial
ASKs (identical semantics). **(C)** An explicit per-walk discovery KB-ASK budget on `WalkerBudget`
(config-tunable), calibrated against per-case ASK counts so it sits above legitimate-discovery
depth. Ship A+B first (they shrink the fan-out so any cap rarely binds), C as the hard ceiling.

**Relationships.** Must not reopen the PATCH-A SS3 hole — a budget can only *prevent* discovery
(false-abstain), never admit an unentailed edge. Reuses the nogood-cache infra. Calibrating C
**needs the standing harness** (item 6c); measure after item 2 + the Step-2 disjoint gain so the
cap isn't set against already-fixed fan-out.

### 9. Retraction cascade + re-derivation  — *architectural-deferral* (D14)

**Problem.** The verdict → dependent-verdict *cascade* and re-derivation "from remaining premises"
aren't implemented. v0.16 has lazy, scoped stale-marking plus a *full blind re-walk* (not
premise-scoped restoration); a verdict is never recorded as a premise of another, so nothing
re-derives B when A flips; the circuit-breaker's step-3 regeneration is absent.

**Fix** (all §3.2-safe — re-derivation can only downgrade-to-abstain or restore under a freshly
walked trace). **(1) Cascade:** after a re-walk, diff old vs new per-claim verdict; for each flipped
claim, mark stale any other claim whose provenance referenced its grounding rows (a bounded
fixed-point) — without persisting verdict-as-row. **(2) Premise-scoped re-derivation:** before the
full re-walk, check whether the `ProvenanceTerm` has a surviving OR-alternative (all its rows still
un-retracted, re-checked at restore time) and restore under it; full re-walk only as fallback.
**(3) Circuit-breaker regeneration (most deferrable):** an opt-in regenerate hook, fail-closed to
the existing breaker on re-conflict.

**Relationships.** Depends on the `ProvenanceTerm` populating multi-alternative groundings; the
regeneration piece shares the "no unreviewed verdict source mid-flight" caution with SLING/NOGOOD;
largely independent of the geo/persona items. This is the largest, most-deferrable piece.

---

## Suggested sequencing

- **Tier 1 — soundness + measurement (do first):** item 1 (false-contradict counter + circa-date fix)
  together with the standing-harness false-contradict gate (item 6c). These protect §3.2 and make
  every later change measurable.
- **Tier 2 — highest coverage per effort, low risk:** item 2 (occupation grounding) and item 3
  Steps 0–1 (vague-class + traced rollup). The largest PopQA/cross_source wins for the least risk.
- **Tier 3 — architectural debt:** item 4 (hardcode removals, each gated on its replacement),
  item 5 (resolve dormant mechanisms), item 6a (Router/Validator removal).
- **Tier 4 — depth:** item 3 Step 2 (premise→Python), item 7 (event-relative bounds), item 8
  (latency budget), item 9 (retraction cascade).

Per the v0.16 process, on approval this becomes a detailed per-file change-spec set with a
confirmation gate before implementation, separate code- and test-agents, a green suite after each
phase, and replacements landing before their deletions.
