# Aedos v0.16.1 — Implementation Plan

Branch `v0.16.1` (from `main`). Authoritative scope for this session, derived from
`docs/v0_16_1_planning.md` and the operator's review of it. Detailed change spec + ordering;
implemented in a build-verify-build loop with per-workstream code/test agents, a small-subset
test gate after each step, and **one** full Medium Bar at the very end. **Commits yes; no tag, no
push.** §3.2 soundness is paramount; replacements land before deletions; abstain is the safe outcome.

## Operator scope decisions (locked)
- **Order of values:** soundness > coverage > simplicity > **latency** (latency is *not* a goal).
  Over-abstention is the disease to cure.
- **Item 1:** confirmed a real Aedos defect (not just grading) — fix the approximate-date
  false-contradict. The false-contradict counter is welcome (observability), but the date fix is
  the real fix.
- **Items 2, 3, 4, 6:** full, as planned. 6a (Router/Validator) done **in conjunction with** 4
  (persona / `user_subject_required`).
- **Item 5 (dormant):** high priority — **activate or remove** each; leave nothing inert.
- **Item 7:** **Stage 1 only** (schema + extraction); Stage 2 (resolver) deferred — report Stage 1.
- **Item 8:** **lever A only** (positive-result memo), with isolated unit tests + a small live
  smoke proving it changes no verdict. Levers B/C deferred.
- **Item 9 (retraction cascade):** **out of scope** this session; kept in mind.

## Workstreams (build-verify order)

### WS1 — item 1: approximate-date false-contradict fix + false-contradict counter  [Tier 1, S]
- **Defect (confirmed):** `kb_verifier._value_matches` (kb_verifier.py:914) runs the year-aware
  compare only when the claim matches `_BARE_YEAR_RE` (`^\d{4}$`). "c. 1550" fails it → returns
  False → a single-valued date predicate (born_on/P569) with a KB value present emits CONTRADICTED,
  falsely contradicting a true claim whose year (1550) matches the KB.
- **Fix:** in `_value_matches`, strip a leading approximation marker on the **claim** side
  (`c.`/`circa`/`ca.`/`approx`/`approximately`/`about`/`around`/`~`) before `_BARE_YEAR_RE`, so an
  approximate-year claim **matches on year-equality** (→ verified). Preserve the precise-ISO strict
  compare (two precise dates still compare strictly). Additionally, an **approximate** claim that
  does NOT year-match the KB must **not CONTRADICT** — downgrade to NO_MATCH/abstain (an
  approximation cannot soundly contradict a nearby exact date); plumb an "is-approximate" signal to
  the single-valued contradiction promotion so it abstains instead. Never introduces a false-verify
  (matches only on exact year equality).
- **Counter:** add a symmetric false-CONTRADICT metric to `tests/evaluation/benchmark.py`
  (`gt≠contradicted & verdict=contradicted`, broken out by gt), alongside `false_verified`.
- **Tests:** unit — "c. 1550" vs KB "1550-01-01" → match (verify), not contradict; "c. 1550" vs
  KB "1600" → abstain (not contradict); two precise dates still compare strictly; bare-year path
  unchanged. Soundness: no new false-verify.

### WS2 — item 2: occupation-copula grounding  [Tier 2, M]
- **Gap:** profession copulas extract as `instance_of`→P31; a person's P31 is Q5 (human), so the
  occupation never grounds (PopQA: Krieger "guitarist" → no_grounding_found). The WS1
  `candidate_kb_properties` arbitration is verification-side only; the discovery side never emits
  P106 for `instance_of`, so there is no positive P106 path.
- **Fix:** the predicate-metadata oracle emits `candidate_kb_properties=[P106]` (+ occupation
  `object_entity_types`) for `instance_of` so `meta.bindings = [P31, P106]`; the existing binding
  loop verifies the asserted occupation against the subject's P106 set (no new verify code). Knowledge
  stays in the oracle/prompt, not a Python table.
- **Soundness gate:** the positive P106 match fires only when the resolved object is a confirmed
  occupation/profession class — extend `_object_satisfies_value_type`'s subsumption confirmation to
  gate the positive path (currently only the CONTRADICTED side), **fail-closed** on type
  uncertainty, so "X is a river" can't match P106. Keep P106 `single_valued=0` (wrong occupation
  abstains, never contradicts).
- **Tests:** "Robby Krieger is a guitarist" (fixture) → verified via P106; "X is a river" → not
  P106-verified; wrong occupation → abstain.

### WS3 — item 3: multi-source / cross-source derivation  [Tier 2, L]
- **Step 0 (S):** convert vague-class abstentions (`_is_vague_class_object`, walker.py) to sound
  verifies via a class-instance check through `verify_transitive_path`/P31 (subsumption authority,
  never a cold LLM positive).
- **Step 1 (S–M):** move the compound-statement conjunction out of the benchmark runner into the
  **aggregator** as a traced AND op (same monotone semantics: contradicted-wins, verified iff
  all-verified, else abstain), composing per-claim sub-traces and recording the shared source-text.
  No verdict change; adds observability + retraction footprint; de-noises the metric.
- **Step 2 (M–L, D10):** premise→Python channel — before invoking Python, resolve referenced
  premise values via Tier-U/KB; extend `PythonVerifier.verify(claim, premises: dict)` to thread
  them into the codegen prompt + `def verify(...)`; record each premise as an AND-child in the
  `ProvenanceTerm` (the op="and" path that exists but is never constructed). **Soundness gates:** an
  `asserted_unverified` premise forces the chain-flag (`verified_given_assertion`, never laundered);
  fail-closed (abstain) on any premise-resolution miss; Python may CONFIRM a comparison only over
  grounded premises.
- **Tests:** vague-class verify; compound rollup trace; premise→Python ("founded before 1800",
  "10² is 100 and 100>50") with the assertion-flag + fail-closed pins.

### WS4 — item 5: resolve dormant mechanisms (no inert)  [Tier 3, M, high priority]
- **SLING — ACTIVATE (gated):** add optional `sample_subject_qids` to `PREDICATE_METADATA_TOOL`
  input schema + a prompt line populating it for long-tail edges; behind a config flag; SLING
  bindings stay `single_valued=False` (verify-only, never contradict); regression-pin with a
  positive control before it can drive a verdict.
- **Binding-NOGOOD veto — REMOVE:** delete `_binding_vetoed` (kb_verifier) + the `vetoes()` reader
  (substrate_exceptions) — a veto that *suppresses contradictions* is the dangerous direction and it
  has no producer. (Keep the `transitive_path` nogood cache used by the walker — that arm is live.)
- **holds-at-T:** wire a safe, coverage-positive base-relation-scope consumer if low-risk
  (a unique-interval-gated `_interval_holds_at` check returning a verdict only on true/false, abstain
  on unknown), **else remove** the unused primitive. Decide during implementation on the soundness
  evidence; default to remove if wiring risks a false-contradict.
- **Dead status seeds — REMOVE:** drop the `status_started`/`status_ended` (P571/P576) seed rows —
  they can never fire (the KB arm reads P580/P582 qualifiers; org subjects already route to
  `founded_in_year`/`dissolved_in_year`).
- **Qualifier-keyed `_lookup_targets`:** leave returning None (correct routing to the resolver — not
  inert code, a deliberate route); no change.

### WS5 — items 4 + 6a: hardcode removals + Router/Validator  [Tier 3, L, riskiest]
Strict replacement-before-deletion; re-prove pins (Warsaw/Rome/Thames/Vatican/Monaco; persona abstain).
- **Geo (D2/D3):** activate discovered-disjointness — give the `substrate_exceptions` nogood cache a
  production writer keyed on the resolved subject Q-id, OR push `_location_disjoint`/`_subsumption_
  upgrades` behind a `KBProtocol.geographic_disjoint`/containment op implemented in `WikidataAdapter`
  (where `CONTINENT_QIDS`, P30/P131/P17, Q5107 legitimately live). Then delete the in-CORE geo
  constants once the geo pins stay green. The closed 7-continent set stays inside the adapter.
- **Persona (D5) — build the WS4 routing first:** when a claim's subject matches a stipulated user
  identity Tier-U row, route to `user_authoritative` (KB structurally unreachable). Add
  `tier_u.has_identity(party, subject)` (no module reaches `_db`). Then delete `_is_persona_subject`
  + its raw SQL. Re-prove the persona-abstain pin.
- **Neighbor/qualifier tables:** move `_D5_NEIGHBOR_PROPS_BY_RELATION` into the adapter's
  `enumerate_neighbors`; route P580/P582 behind a protocol interval-qualifier accessor.
  Behavior-neutral.
- **Normalizer bypass:** add `KBProtocol.search(query)` + `fetch_types(qids)` ops, implement in
  `WikidataAdapter`, replace the `wbsearchentities`/`_fetch_p31_for_candidates` reach-arounds, move
  the Wikipedia endpoint to config. Behavior-neutral.
- **Router/Validator (6a, w/ persona):** relocate the Validator's three anomaly checks
  (`user_subject_required`, `distinct_slots`, `object_type`) into the live path as **fail-closed**
  guards (or document superseded by the kb_verifier object-type gate + persona routing), then delete
  `router.py`/`validator.py` and retarget the 3 test files. `user_subject_required` overlaps the
  persona work — reconcile together.

### WS6 — item 6b: Python-tier deterministic front-end  [Tier 3, M]
- Add a deterministic parser+evaluator to `PythonVerifier` for numeric comparison ("X greater/less
  than Y"), date/year ordering ("A before/after B"), and simple arithmetic ("N squared/plus/times M
  is K"): return a verdict **only** on a full exact parse, else `None` → existing LLM-codegen
  fallback → abstain. Byte-safe for §3.2 (ambiguity → None → fail-open). Optionally majority-vote 3
  codegen samples and abstain on disagreement (tightens, never loosens).
- **Tests:** "10 squared is 100" → deterministic verified; "100 > 50" → verified; "5 > 9" →
  contradicted; unparseable → None→fallback.

### WS7 — item 6c: standing eval harness  [Tier 3, M]
- Promote `tests/evaluation/benchmark.py` into a committed CLI: `false_verified==0` hard-fail gate +
  the new **false-contradict** gate (WS1); fold in the per-instance watchdog + live-FV counter +
  incremental JSONL currently duplicated across `scripts/medium_bar_step1_run.py`; a pinned **offline
  regression set** (`mhd_018`, the circa-date case, a copula-occupation case) runnable mocked in CI;
  one documented live entry point that subsumes the SimpleQA/PopQA reader. Thin the one-off scripts.
- Update the stale "v0.15 / +15pp-vs-baseline" framing to v0.16.x-appropriate gates.

### WS8 — item 7 Stage 1 only: event-relative temporal fields  [Tier 4, M]
- Add Optional `valid_from_ref` / `valid_until_ref` to `Claim` (extractor.py) and `TemporalScope`
  (temporal.py), mirroring `valid_during_ref`; thread through `extract_temporal_scope`,
  `_build_claim`, the JSON tool schema, triage, the walker claim-template copy, the Tier-U INSERT,
  and an **additive** DB column (non-destructive). Split Rule 16: before→`valid_until_ref`,
  after/since→`valid_from_ref`, during stays `valid_during_ref`. Tighten the corpus runner to check
  the `*_ref` fields. **Write-only metadata — no verdict path reads them — zero verdict risk.**
  Stage 2 (the resolver) is **deferred**.

### WS9 — item 8 lever A only: verify_transitive_path positive-result memo  [Tier 4, M]
- Add a process-scoped positive-result memo for `verify_transitive_path` keyed by
  `(relation_type, source_qid, target_qid)` → holds/holds-not, populated **only on definite
  (error-None) answers**, bounded LRU + TTL, consulted before the live ASK. Zero soundness risk
  (returns exactly what a fresh definite ASK would). **Isolated unit tests** (memo hit/miss, never
  caches error/fail-open results) **+ a small live smoke** (a few cases) confirming verdicts are
  unchanged and nothing breaks. Levers B (batch) / C (KB-ASK budget) deferred.

## Global ordering & process
1. WS1 (soundness) → WS2, WS3 (coverage) → WS4 (dormant) → WS5 (hardcodes+router, riskiest) →
   WS6 (python) → WS7 (harness) → WS8 (temporal Stage 1) → WS9 (memo + smoke).
   (WS7 depends on WS1's counter; WS5 persona/router are interdependent and done together; WS9's
   smoke is the only mid-session live call besides WS2/WS3 fixture-driven checks.)
2. Each WS: a code agent then a separate test agent (no context conflation), then I verify a
   **small relevant test subset** green and commit. No full Medium Bar mid-session.
3. After all WS: adversarial review rounds (per v0.16) → patch what's found (build-verify-build) →
   keep the suite green.
4. **Final:** one full Medium Bar evaluation; report. No tag, no push.

## Soundness invariants (every WS)
- Never false-verify, never false-contradict; abstain is safe. New positive-grounding paths
  (WS2 P106, WS3 premise→Python) fail-closed and gate on confirmed value/type. Deletions (WS4/WS5)
  land only after their replacements with pins re-proven. Activations (SLING) are verify-only +
  reviewed + pinned. The memo (WS9) caches only definite answers.
