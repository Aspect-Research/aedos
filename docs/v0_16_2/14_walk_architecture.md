# v0.16.2 — Walk architecture fix: bound total work + directed-over-enumerate

## Symptom
"Obama born_in Kenya" (FALSE) STILL hits `budget_wall_clock` (30s) after the
`max_kb_neighbor_probes=48` cap — trace shows ~22 `kb_neighbor_enumeration` steps
(3× P31 is_a/parent, 19× P17 part_of/child), `Sources {kb:0}`.

## Root cause (4-investigator consensus, high confidence)
1. **The probe cap counts the wrong thing.** `max_kb_neighbor_probes` counts only
   the per-candidate `_verify_chain` transitive ASKs *inside* `_expand_via_kb_neighbors`
   (deduped by QID, memoized). On this walk that's a small, sub-48 fraction.
2. **The dominant cost is uncounted per-node `KBVerifier.verify` work.** Each of the
   ~22 admitted neighbors becomes a depth-1 frontier node whose own
   `_direct_lookup → KBVerifier.verify` runs entity resolution + `lookup_statements`
   + — the killer — **`_subsumption_upgrades`, which calls the UN-memoized
   `_kb.subsumption` (2 ASKs + an establishing-property SELECT each)**. `subsumption`
   has no memo (only `verify_transitive_path` does), so it re-pays the network cost on
   every node. ~22 nodes × ~3–5 SPARQL at 5/s ≈ 30s.
3. **The wall-clock is sampled only at the depth-loop top** (`walk()` line 568),
   never inside `for node in frontier`, so one fanned-out depth overruns by an
   unbounded margin before the next check → `budget_wall_clock`. `{kb:0}` because the
   kb source counter only increments on a kb-VERIFIED/CONTRADICTED, never on the
   dozens of abstaining lookups doing the real work.
4. **The fanout is provably futile.** `born_in` is functional (P19); the direct verify
   already FETCHED Obama's full P19 value set {Kapiolani, Honolulu} and the **directed**
   `_subsumption_upgrades` already asked "is the birthplace ⊆ Kenya?" → No. Enumerating
   Kenya's children ("is the birthplace one of these?") re-derives the *same* question
   by generate-and-test. Subject-side is_a enumeration can't ground a specific
   functional fact either. The directed check is the right primitive and it already ran.

## Fix architecture — three composing, abstain-only components

### Change 1 — unified per-walk KB-WORK budget, sampled inside the node loop (the general bound)
- `WalkerBudget.max_kb_work_units` (default 60) + a per-walk `_KBWork` counter
  (mirrors `_KBNeighborProbes`: created in `walk()`, threaded as a parameter — never
  an instance attr — so concurrent walks on the shared walker each get their own).
- Charge a unit at every **walker-side** KB round-trip: the `KBVerifier.verify` call
  in `_try_external_grounding` (a flat weight, since verify bundles several SPARQL),
  each `_verify_chain` transitive ASK + `subsumption.consult`, each
  `_expand_via_kb_neighbors` `enumerate_neighbors`, each `_resolve_qid` resolve.
  (Walker-side only — the adapter is shared across parallel walks, so per-walk state
  can't live there.)
- Sample `work.exhausted` **inside** the node loop → fast deterministic
  `no_grounding_found` / `abstention_reason="budget_kb_work"`, guarded by
  `current_verdict is None` (an earlier-grounded verdict wins, same guard as the probe
  arm). Plus a cheap within-node wall-clock re-sample as a backstop.
- **Sound:** abstain-only — never emits/flips a verdict; the `current_verdict is None`
  guard preserves a verdict found earlier in the frontier. Turns an
  already-timing-out `budget_wall_clock` into a fast, deterministic `budget_kb_work`.
  Cap is generous (a genuine grounding needs a few units) and config-tunable.

### Change 2 — directed-over-enumerate: skip the futile fanout (the speedup)
- Surface from `KBVerifier.verify`'s trace: `statements_found` (subject's value known)
  and that the directed subsumption-upgrade ran-and-failed (it's implicit on the
  NO_MATCH path: if it had succeeded the verdict would be VERIFIED).
- Thread the signal to the walker; gate `_discover_chains` to **skip neighbor
  enumeration** (both slots) when: predicate is functional (`single_valued`) AND
  `object_type == entity` AND `statements_found` (a known value the directed check
  already tested against the claim). Premise-forward (`_expand_from_premises`, bounded)
  stays enabled.
- **Sound:** skipping removes only candidates the verify-time gate / directed upgrade
  would reject anyway (admitted-edge set unchanged → identical OUTCOME, reached without
  the wasted SPARQL). The only legitimate grounding (a container claim, "born_in USA")
  is owned by the directed upgrade, not enumeration. Strictly safer (also avoids the
  C2-FC1 substituted-contradiction hazard). `_predicate_is_functional` fails open to
  False (a consult miss leaves the old fanout in place — never a new abstain).

### Change 2-NR — non-regression: directed upgrade over ALL held values
- The in-statements subsumption-upgrade currently checks only the FIRST `scope_mismatch`
  value. For a multi-valued subject whose true container chain sits on a SIBLING value
  (Obama's P19 = {hospital, Honolulu}; the hospital's P131 chain may be incomplete), it
  can miss. Loop `_subsumption_upgrades` over ALL distinct mismatch entity values,
  VERIFYING on the first hit, so "born_in USA" verifies regardless of iteration order.
- **Sound:** each check is the same positive-subsumption ASK; adding more held values
  only finds MORE genuine containment (a coverage win), never fabricates. This makes
  the Change-2 skip strictly non-regressive (the directed path covers every true case).
- **Adversarial-review fix (part_of-only):** the first cut of 2-NR called the upgrade
  with BOTH `part_of` and `is_a`. The review found a §3.2 **false-verify**: on a
  non-location single-valued entity predicate holding multiple values (occupation,
  employer, member_of), a sibling value that is_a the claim object (e.g. an occupation
  value is_a "river") promoted the claim to VERIFIED, *bypassing* the
  `multi_valued_single_valued_predicate` abstain guard — and a single-value variant was
  a *pre-existing* hole (the in-statements upgrade was never relation-gated, unlike the
  no-statements arm). The claim-object subsuming a held VALUE entails the claim only via
  **geographic containment** (born in a place within O ⇒ born in O), not via `is_a`. Fix:
  the in-statements upgrade now passes `relations=("part_of",)` only. This is a *strict
  subset* of the prior relations, so it can only make the upgrade fire LESS — it cannot
  introduce a new false-verify (monotone-safe); it preserves "born_in USA" (part_of) and
  closes both the multi-value and single-value is_a false-verifies. (Gating on
  `_is_location_property` — the reviewer's first suggestion — was rejected: P19/born_in
  is NOT in `_LOCATION_KB_PROPERTIES`, so that gate would have wrongly disabled
  "born_in USA".)

### Change 2-FOLLOWUP — decouple the part_of skip from single_valued (the live miss)
Live, "Obama born_in Kenya" STILL fanned out (~20 P17 part_of steps) despite Change 2.

**SUPERSEDED — the stated diagnosis was WRONG.** This followup claimed the extractor's
"was born in" doesn't match the seed key `born_in`, so the predicate cold-started without
`single_valued`. That is FALSE: `normalize_predicate` (extractor.py, v0.16 WS1) strips a
leading auxiliary in CORE — `"was born in"` → `born_in` — so it hits the seed
(`P19, single_valued=1, object_type=entity`, confirmed in the live DB, used 162×). The
decoupling code below (the `value_known_entity` signal + cross-binding aggregation) was
committed (cb3adf6) and is *sound but INEFFECTIVE* for the real cause: `value_known_entity`
is also `bool(statements) and ...`, so it is still False on the path that actually fired.
Kept for the non-functional-entity-with-statements case it does help; see Change 2-FINAL.

~~Fix: **decouple**. The P17 part_of fanout (the bulk, ~19/22 steps) is owned by the
directed all-values **part_of** upgrade regardless of single_valued, so gate the
part_of-enumeration skip on a new `value_known_entity` signal (`bool(statements) and
object_type=="entity"`, NO single_valued). Keep the is_a-enumeration skip gated on
`functional_value_known`. Also aggregate both signals across bindings in `verify()`'s
arbitration (OR), so a later no-match binding can't clobber the P19 binding's signal.~~

### Change 2-FINAL — the metadata signal (the actual fix for the live fanout)
**True cause (pinned with a repro against the real `KBVerifier`):** the directed-over-
enumerate signals are emitted ONLY on the statements-FOUND path (the post-`_compare_positive`
trace dict). The two NO_MATCH paths the live walk actually took carry NO signal:
`subject_resolution_failed` (kb_verifier.py ~278) and `no_statements_found` (~412). Both
committed signals (`functional_value_known`, `value_known_entity`) are `bool(statements)
and ...`, so on those paths they are False → the walker fanned out. The smoking gun: born_in
is single_valued, so a KNOWN mismatching value yields a fast **CONTRADICTED** at the direct
lookup (no discovery). The live walk produced an *abstain that fanned out* — which can only
happen if **statements were not found** (e.g. "Obama" resolving ambiguously — Barack Obama
vs *Obama, Japan* — or the looked-up entity carrying no P19). Repro:
`fvk=vke=False` on both no-signal paths; `verdict=contradicted, fvk=vke=True` on the
statements-found control.

**Fix:** emit a METADATA-derived signal `functional_entity_predicate` from `verify()`,
independent of `bool(statements)` — `meta.object_type == "entity" AND had_kb_path AND
all(b.single_valued for b in meta.bindings if b.kb_property)` — set on every NO_MATCH
return. Thread it to the walker (new thread-local `_functional_entity_predicate`, mirroring
the two siblings) and add it as a new OR term to `skip_enum`, gating BOTH enumeration arms.
For a functional entity predicate the directed subsumption upgrade is the only KB grounding
path, so neighbor enumeration is futile whether or not a value was found. `all` (not `any`)
keeps enumeration for a mixed/non-functional binding set — over-abstention is the disease to
cure. Net: "Obama born_in Kenya" now skips the fanout regardless of whether Obama resolved /
had a P19, and abstains fast.

**Sound — abstain-only, structurally (§3.2):** `_discover_chains` runs ONLY after the direct
verify returned None (the walk loop `continue`s on any non-None verdict), so skipping
enumeration can never create or alter a VERIFIED/CONTRADICTED — it only removes discovery
from an already-abstaining walk. The premise-forward arm and the substrate `find_neighbors`
substitution stay OUTSIDE `skip_enum` (still run). Adversarial review found no false-verify/
false-contradict and the change **net safety-positive** (it closes a pre-existing un-gated
subject-side part_of substitution path). Residual over-abstention is theoretical only
(subject-side is_a substitution on the statements-empty branch) with no constructible
real-data trigger, and is covered by the still-running premise-forward / substrate arms.

## Net behavior
- "Obama born_in Kenya" (statements found) → fast **CONTRADICTED** at the direct lookup.
- "Obama born_in Kenya" (subject unresolved / no P19) → `functional_entity_predicate` skips
  the fanout → **fast sound abstain** (no 22-step P17 enumeration).
- "Obama born_in USA" → directed upgrade (Honolulu ⊆ USA), strengthened by 2-NR →
  **VERIFIED**.
- "Williams College in the US" (located_in, NOT single_valued) → unaffected by Change 2;
  grounds via part_of/premise_forward as before.
- Any remaining non-grounding walk → bounded by Change 1's work budget → fast
  `budget_kb_work` abstain instead of a 30s timeout.

## Soundness summary (§3.2)
No component admits a new grounding edge or relaxes a verify-time gate. Changes 1, 2, and
2-FINAL are abstain-only (can't false-verify or false-contradict; `_discover_chains` runs
only when the direct verify already abstained; `current_verdict is None` guards preserve
found verdicts). Change 2-NR only extends an existing positive-subsumption VERIFY to more
held values (coverage-only, sound). Over-abstention is the only risk and is bounded:
Change 1's cap is generous and tunable; Change 2's statements-based gate and 2-FINAL's
metadata gate (`functional + entity + ALL-bindings-single_valued`) are confined to the
provably-futile class, with the premise-forward / substrate-substitution arms left running;
2-NR *reduces* over-abstention. Offline-testable with the mock-KB harness AND a real-
`KBVerifier` regression (the mock that hard-coded the signal is exactly what masked the
2-FOLLOWUP miss); adversarial-reviewed (no §3.2 hole; net safety-positive).
