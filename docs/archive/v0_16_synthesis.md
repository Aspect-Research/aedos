# Aedos v0.16 Synthesis — Comprehensive Architectural Review and Forward Planning

*Status: exploratory analysis. No code changes were made in producing this document. It is intended to be read carefully by the operator and used to inform v0.16 planning. It surfaces tensions, multiple competing theories, and alternatives rather than prescribing a single answer. Every substantive claim cites the code, doc, or case that supports it so the operator can verify it.*

*Inputs studied: `docs/architecture.md` (full), the v0.15.0 tag message, the full per-layer source (`src/aedos/**`, ~11K LOC), `docs/v0.16_planning.md` (D1–D60), the eleven calibration corpora and their runner, `docs/phase_10_5/*` (calibration results, corpus integrity, medium-bar Run 0→8/99), `docs/phase_H/*` (Clusters 1–3, D5/D47/D53/D16), the Phase A–G reports and the originating audit chain, the seed pack and model-routing config, and the three benchmark smoke runs in `../aedos-simpleqa` (SimpleQA, TruthfulQA, PopQA) with their grader/runner/metrics implementation. A 17-agent deep-read workflow produced the per-surface findings; the author independently verified the load-bearing claims against primary sources.*

---

## 1. Executive summary

**The headline.** v0.15.0 delivers one thing cleanly and completely: *per-verdict soundness*. Across 668 calibration case-mode invocations and four post-fix medium-bar runs, the §3.2 false-verified rate is 0%, against an LLM baseline at 9.8–15.6%. The architecture's central commitment — "I won't lie to you" — held under measurement. This is real, it is well-engineered, and it is the achievement to build from.

**The hard truth underneath it.** Almost everything else the architecture promised is either (a) not realized in the deployed pipeline, (b) realized but never surfaced to the user, or (c) realized but demonstrably not paying for itself against a modern calibrated baseline. Specifically:

1. **A large fraction of the architecture is built but dormant.** Layer 2 (the Router/Validator) is dead code — never constructed in any production path (`router.py` is instantiated only in tests). The §3.6/§7.3 retraction-propagation pillar — one of the eight load-bearing principles — is mostly inert: the cascade and re-derivation are unimplemented (D14), `ContradictionTracer` is never wired into the pipeline (D15), there is no API to receive external corrections (D30), and KB-grounded verdicts — the most common kind — carry no retractable identifier so can never be retracted at all (D13). The dual-designation verdict family (`*_given_assertion`), described as the engine's headline soundness mechanism, is computed and then collapsed away before the user ever sees it (`base_verdict_of` at `chat_wrapper.py:135`); it is observability metadata only. "KB wins" cross-source contradiction is implemented but inert in `/chat` (Tier U is never seeded with `externally_verified` rows in production, and the contradiction `pre_verdict` is discarded). The "session-scoped knowledge-building" framing is a conceptual contract enforced nowhere: persistence is process-global, keyed on `asserting_party='user'`.

2. **The deployment context shifted under the project.** The benchmark effort's single most important finding is that the calibrated Haiku-4.5 baseline **does not hallucinate** on factual QA: 0% wrong on PopQA, 3.3% on SimpleQA (one case), and falsehoods only on TruthfulQA misconceptions (13.3%). The original premise — "verify the LLM's output to catch its fabrications" — has been substantially solved by the LLM's own calibration. On PopQA, where every question is a Wikidata triple Aedos should be able to check, the calibration-advantage bucket ("Aedos abstained, baseline was wrong") was **empty**: Aedos paid the full coverage cost of soundness (0% attempt vs the baseline's 26.7%) and collected none of the benefit, because there was nothing unsound to catch.

3. **The coverage gap is real, bounded by genuine architectural ceilings, and tuning has hit diminishing — even negative — returns.** The medium-bar climbed from 27.9% (Run 1) to a peak of 68.0% (Run 6), but ~10pp of that peak rode on hardcoded band-aids (a demonym table, population regexes, hand-curated property rows). When those were generalized away per the project's own no-hardcoded-mappings discipline (five "Generalize" commits, 2026-05-29), accuracy fell back to **57.4%** (Runs 7/8) *and* the false-contradiction count regressed (Run 7: 7 fp_corrections, Run 8: 4, including "Paris is in France, so Paris is in Europe" → CONTRADICTED). The honest shipped state is ~57.4% against a ~74–79% baseline. The README's "~55–70% single-run-variance band" conflates real LLM non-determinism (identical code disagrees on 15/122 cases) with a Run 5→8 *code-state* spread.

4. **The core inference capability — multi-source derivation — is the weakest component, and it is structural, not a calibration miss.** The `derivation_corpus`, which exercises the walker's distinguishing capability (composing Tier U + KB + Python across a chain), sits at a 54% seeded / 46% cold-start ceiling against an 80% threshold, reproduced exactly across Phase H and Phase 10.5. Every *individual* oracle scores 84–100%; it is the *composition* that fails. The walker bottoms out at depth 0 for KB-discovered chains because the D5/D51 live-neighbor fallback is hard-capped to `depth == 0` (a wall-clock band-aid for an 18-minute blowup, `walker.py:991`). Phase E confirmed the 36% walker ceiling is model-independent — Sonnet 4.6 ties at 34% — so it is an architectural limit, not a capability limit.

5. **The measurement instrument has repeatedly been as defective as the system it scores.** The "D16 walker soundness violation" turned out to be cross-case Tier U state leakage in the harness (the walker was correct). Three calibration runners hard-KeyError'd on most of their corpus and still passed the dry-run green. Several corpora are re-pinned to match the build's behavior, so they cannot detect a regression baked into the re-pin (D48). The calibration headline numbers are *cold-start* (the v0.15 default), not the seeded production path, and carry ±4pp run-to-run variance (D49). This means a meaningful fraction of "the numbers" are noisier and softer than they read.

**The operator's framing, tested.** The operator's position is: "a good system headed in the right direction that needs proper calibration and tuning and perhaps large architectural changes based on theory." The evidence supports the first half partially and challenges the second half ("we kept seeing gains; no reason we can't keep seeing them"). The soundness engineering is genuinely good. But the gains during the medium-bar were dominated by one-time harness fixes, one-time soundness corrections, and band-aids that were then *removed* — the Run 6→7/8 regression is direct evidence that the tuning lever has been pulled about as far as it goes and is now trading coverage and false-contradictions for principle. Continued gains require either (a) closing genuine architectural ceilings (multi-hop depth, free-text class subsumption, extraction-to-property routing) — which is "large architectural change," not tuning — or (b) confronting the deeper question the benchmarks raise: whether general factual QA against Wikidata is the right problem for this architecture at all.

**The one-sentence version.** Aedos has built a sound, sophisticated, and largely *dormant* truth-maintenance engine whose demonstrated value (principled abstention, belief revision) lives on a constructed test set biased to its strengths and on session-knowledge capabilities the deployment doesn't actually realize — and the benchmarks reveal that the problem it was built to solve (catching LLM hallucinations on factual claims) has been substantially absorbed by LLM calibration itself. v0.16's central decision is not "which bugs to fix" but "what is Aedos *for*, now that the world it was designed for has changed."

The rest of this document substantiates each of these claims in detail and lays out candidate shapes for v0.16.

---

## 2. Architectural map of v0.15.0 as actually implemented

This section walks the system layer by layer as it exists in code, not as the spec describes it. The recurring theme: the implementation is faithful to the *components* the spec names, but the *wiring* between them, and the *deployment* on top of them, diverge from the architecture in ways that matter.

### 2.0 The end-to-end data flow (so the rest is legible)

A `/chat` turn (`chat_wrapper.py:198–302`, `respond()`) runs:

1. **Extraction call #1** over the *user message* → triaged to VERIFY → `promote_assertions` writes each as a Tier U `asserted_unverified` row (`chat_wrapper.py:220–236`). The cross-source-contradiction `pre_verdict` this can produce is **discarded** (comment at `:231–236`).
2. **Draft generation** — `self._llm.chat(..., purpose='chat')` → `claude-haiku-4-5` (`chat_wrapper.py:238–244`). The system prompt is the bare default `"You are a helpful assistant."` — `app.py` constructs the wrapper with no config, so no Aedos-aware instruction reaches the draft model (`chat_wrapper.py:239`, `app.py:122–132`).
3. **Extraction call #2** over the *draft* → triaged to VERIFY (`chat_wrapper.py:255–264`). Deliberately *not* wrapped in try/except (a prior broad `except Exception: claims = []` had left `/chat` verification-inert for two RCs — D18).
4. **Per-claim walk** — `walker.walk(claim, verification_context)` for each draft claim (`chat_wrapper.py:266–279`).
5. **Aggregation** → six-bucket verdict counts + per-claim `ClaimVerdict` list + a `verdict_recorded` audit event per claim (`aggregator.py:139–238`).
6. **Intervention selection** — `select_interventions(claim_verdicts)` → `InterventionPlan` (`chat_wrapper.py:101–156`).
7. **Response composition** — Format A: PASS_THROUGH returns the draft verbatim; INTERVENE returns the full draft + appended "Aedos verification notes"; DECLINE returns a fixed refusal string.

So the system makes **two** extraction calls per turn and verifies the **draft**, with the user's own assertions promoted as premises beforehand. This is the shape every benchmark and the medium-bar exercise.

### 2.1 Layer 1 — Extraction and normalization

**The v5 prompt is a 24-rule monolith** (`extractor.py:113–460`), not the "17 rules" of earlier framings — it grew through Phase 10.5. Rules 1–6 are the original contract (text-not-context, first-person preservation, reported speech, future-tense tagging, verbatim source_text, contrastive correction). Rules 7–24 are post-baseline accretion: temporal disambiguation (7–9, 15–17, 23), multi-participant events (10–11), employment start/termination/state-change (12–14), residence vocabulary (18), instance-of vs holds-role for "X is a/the Y" (19–20), nationality and compound nationality (21–22), and quantitative count comparison (24). The design discipline (D45) is that each rule carries explicit DO-NOT-apply non-triggers; the prompt is the *only* enforcement point, and the header comment concedes the LLM "ignores it about half the time" for Rule 18, forcing a Python regex band-aid (`extractor.py:623`).

**The Claim model is thin on temporal expressiveness.** Six required fields (subject, predicate, object, polarity, source_text, verb_tense) plus three optional temporal strings. It *cannot* express event-relative FROM-vs-UNTIL bounds, so Rule 16 deliberately collapses "before X" / "after X" / "during X" all into `valid_during_ref` — an admitted semantic loss deferred to D59's `valid_from_ref`/`valid_until_ref`. `verb_tense` is load-bearing and unvalidated: a past-tense verb with no other signal silently acquires `valid_until='before_present'` (`temporal.py:46`), and Rule 16's whole job is to emit `valid_during_ref` *to suppress that default* — a hidden coupling where a forgotten field flips a claim's temporal semantics.

**`_build_claim` drops claims silently in four places** (`extractor.py:519–598`): hard-claim substring check (subject/object must appear in text), content-less `occurred/happened` event, `subject == object` self-reference, and `predicate == object` verb-echo. Each is a documented band-aid for a recurring LLM mis-extraction; each preserves soundness by dropping to abstain. Collectively they are a structural cause of the ~49% false-abstain rate.

**`normalization.py` is the last surviving hardcoded predicate table** (`_CANONICAL_MAP`, ~50 entries, lines 15–66) — exactly the band-aid class the project's own memory forbids, and the place where Phase 10.5 *removed* its siblings (demonym, year-rewrite, population). Worse, it actively miscanonicalizes: lines 29–30 map "lives in"/"resides in" → `located_in`, which the extractor's `_RESIDENCE_VERB` regex (`extractor.py:623`) then *reverses* back to `lives_in`. Three mechanisms (prompt Rule 18, the map, the regex) fight over one predicate.

**Multi-participant decomposition multiplies claims with no verification home.** `decompose_event` (`decomposition.py`) reifies an event into N+2 synthetic triples (`has_participant`, `event_type`, `target`) under a generated `event_<hex>` subject. These synthetic predicates appear in *no* triage allow-list and get *no* router special-casing, so they route through the predicate-translation oracle like any predicate and most likely abstain. PopQA's Robby Krieger case (below) shows one short occupation question exploding into 9 claims, 8 about a synthetic band-founding event, all ungroundable.

**The empty-extraction pass-through is structural and load-bearing.** `extract()` returns `[]` on no claims, with no logging or fallback; `chat_wrapper.py:108` maps an empty claim list to PASS_THROUGH. Anything the extractor fails to parse is surfaced to the user *unverified*. This is the soundness-preserving default — but it is also a genuine soundness *hole*: the chili-pepper TruthfulQA case (below) shows Aedos passing a draft through with zero claims extracted, and 9/15 PopQA cases were empty pass-throughs. The "I won't lie" guarantee is conditional on the extractor never silently no-op'ing on a false draft.

**The three-stage entity normalizer is the cleanest Layer-1 component and the one that best embodies the architecture** (`wikipedia_normalizer.py`): Wikipedia redirect resolution → `wbsearchentities` on the canonical form → D33 type-filter + LLM disambiguation with a hard abstention bias ("a wrong selection is worse than an abstention"). But it fail-closes at eight distinct junctions, all → abstain, and `entity_disambiguation` scored 43.5% in medium-bar Run 8 vs a 95.7% baseline — the single worst Aedos-vs-baseline regression. The abstention discipline is the direct cost.

### 2.2 Layer 2 — Routing (dead code)

This is the most surprising finding of the architectural map. **The `Router` and `Validator` are not used in any production path.** A full-repo grep for `RoutingDecision` / `.route(` / `Router(` finds matches only inside `router.py` itself and three test files. Neither `chat_wrapper`, the calibration runner, nor the cold-start harness ever constructs a `Router`. They wire `Extractor → promote_assertions → Walker` directly.

Layer 2's three spec'd responsibilities all migrated or vanished:
- **Metadata lookup** → the predicate-translation oracle, called by each Layer 4 source.
- **Route selection** → re-derived independently inside Layer 4: `Walker._predicate_routing()` (`walker.py:877`) re-consults the oracle for the routing hint, and `KBVerifier.verify` self-gates on `routing_hint == 'kb_resolvable'` (`kb_verifier.py:128`). The one place the `user_authoritative` hint changes behavior is the walker pre-setting `chain_includes_assertion = True` (`walker.py:320`) — again, the walker reading the oracle, not the Router.
- **Structural validation / routing anomaly** → *nowhere*. The `Validator`'s invariants (`user_subject_required`, `distinct_slots`, `object_type`) run only inside `Router.route()`, so they never execute. The architecture's "routing anomaly" failure class (`architecture.md:190–196`, glossary at 806) is fully dormant: the deployed system cannot distinguish a structurally malformed claim from a legitimately unverifiable one.

The Router's four-route model is also *stale* relative to the oracle, which emits **five** routing hints — `kb_quantitative` (`predicate_translation.py:45`) is unmodeled by the Router and handled only in the walker (`walker.py:652`). This is a tell that Layer 2 was abandoned mid-evolution rather than deliberately retired.

The behavior is *sound* (every routing fallback fails toward abstention), but the layering is fiction, the anomaly-detection capability is lost, and routing authorization is scattered defense-in-depth across `walker.py` and `kb_verifier.py` rather than centralized — exactly the condition that produced F-042 (the walker once invoked the Python verifier ungated, producing false contradictions on subjective claims).

### 2.3 Layer 3 — The substrate oracles

Four components: `predicate_translation` (Haiku 4.5, native Anthropic), `subsumption`, `predicate_distribution`, `entity_resolution` (all Qwen 3-Next 80B via OpenRouter), plus the `consistency` checker.

**The soundness-over-coverage asymmetry is encoded as concrete defaults**, not just doctrine: `single_valued` defaults to 0 (`translation.py:448`), distribution defaults to `neither` (`distribution.py:206`), subsumption to `unrelated` (`subsumption.py:248`). Every default is the verdict that costs a false abstain rather than a false contradiction. This is the §3.2 asymmetry living in `try/except` handlers — and it is pervasive and correct.

**The P31-vs-P106 decision — type vs occupation for "X is a Y" — is made nowhere in Layer 3.** It is hardcoded upstream in `normalization.py:60–65` ("is a" → `instance_of` → P31; "is the" → `holds_role` → P39). The seed pack carries `instance_of`→P31, `occupation`→P106, and `holds_role`→P39 as three independent entries with no disambiguator between them. So "Einstein is a physicist" canonicalizes to `instance_of` → P31, which is *wrong* (physicist is Einstein's P106 occupation; his P31 is Q5/human), and the oracle never gets a chance to correct it because it only ever sees the canonical name `instance_of`. This is both a hardcoded-mappings violation and the root cause of the PopQA occupation collapse (§4.3).

**Calibration numbers are cold-start, not the seeded production path.** The headline figures (predicate_translation 92.5%, subsumption 88.3%, predicate_distribution 88.0%) are measured with `load_seeds=False` — they measure the LLM's *novel-predicate generation quality*, not the in-vocabulary production path (which is effectively 100% for seeded predicates). Nothing in the oracle code records or asserts calibration. A reader can easily over-read 92.5% as production quality when it is generation quality.

**`predicate_distribution`'s prompt memorizes the corpus's pinned framings.** The "AUTHORITATIVE RUBRIC (use these framings; do not re-derive)" (`distribution.py:158–195`) hardcodes the exact framings the calibration corpus pins (lives_in, mortal, prefers) to override Qwen's "neither" bias, lifting accuracy 54%→88%. This passes calibration by construction; its generalization beyond the three exemplar families is unmeasured. The remaining ~10% is the `both` cases the oracle never produces (a permanent haircut the corpus itself admits are debatable).

**The substrate has accreted compensating band-aids** rather than capability: a negative-resolution cache, a leading-"the" retry, `_borrow_seed_slot_to_qualifier` (which silently injects a peer's slot map into a cold-start row sharing the same KB property — and can pick a wrong-direction peer via arbitrary `ORDER BY id LIMIT 1`), the NULL-`slot_to_qualifier` consistency skip, and the inverse-mapping exemption. Most exist to compensate for upstream ceilings (D47 resolution, D5 neighbor enumeration) rather than extend capability.

**The consistency checker is the one wired retraction mechanism, and it can destroy good knowledge.** During medium-bar Run 1, malformed runtime oracle rows (`kb_property` set, `slot_to_qualifier` NULL) collided with well-formed seeded peers on the same KB property; the retract-both policy then cascade-retracted **17 seeded rows** — exactly the predicates the multi-hop/disambiguation cases needed (`medium_bar_results.md:199–226`, Finding 4). Fix 1 (skip NULL-sq conflicts) patched it, but it demonstrates that "no privileged LLM adjudicator" (retract-both) has a cost the spec acknowledges only abstractly: the autonomous-retraction machinery, where it *is* wired, is fragile.

### 2.4 Layer 4 — Tier U and promotion

**Tier U is a single append-only table whose three-value status enum is the soundness carrier**: `asserted_unverified` (the user said it, ungrounded → matches yield `verified_given_assertion`), `externally_verified` (KB/Python-grounded or seeded → yields plain `verified`), and `contradicted_by_externally_verified` (the "KB wins" outcome → recorded for audit, invisible to every verdict-influencing read). The enum's `CHECK` constraint exists only on fresh `CREATE TABLE`; on migrated DBs it is enforced solely by application code, so soundness depends on nothing ever `INSERT`ing into `tier_u` directly.

**"KB wins" is a write-time mutation, durable across reads via three independent SQL filters** (`tier_u.py:470, 634, 690` each carry `AND status != 'contradicted_by_externally_verified'`). The replication is a band-aid risk: a future read path that forgets the filter would let a KB-refuted assertion ground a verdict.

**Two critical inertness findings:**
- **Persistence is process-global, not session-scoped.** `app.py:35` opens one on-disk DB for the process lifetime; `chat_wrapper` has no reset/session-partition logic. `conversation_id` is *not* part of the Tier U key — it flows only into `ExtractionContext.turn_id`. With the default `asserting_party_id='user'`, every anonymous caller shares one assertion namespace: user A's "I live in Boston" becomes a standing premise that can ground or contradict user B's claims. "Session scope" is a conceptual contract enforced nowhere; the corpus runner manually `DELETE`s Tier U between cases to *simulate* the scope the deployment lacks.
- **"KB wins" is inert in `/chat`.** `load_seeds` only seeds `predicate_translation`, never `tier_u`, so a fresh production DB has zero `externally_verified` rows; `contradicted_by_externally_verified` is never produced outside the seeded corpus runner. And even when the promotion path *does* compute a contradiction `pre_verdict`, `respond()` discards it (`chat_wrapper.py:230–236`).

**`mark_externally_verified` (the Q-Upgrade) captures a `grounding_chain` for a retraction propagation that does not exist** (`tier_u.py:544–552`): v0.15 implements no reverse-upgrade, so a retracted KB statement does not auto-downgrade a Tier U row it upgraded. The capture is forward-looking scaffolding for D14, currently dormant.

### 2.5 Layer 4 — KB verifier and Wikidata

**Four verdicts, two kinds of abstention.** `VERIFIED`, `CONTRADICTED`, `NO_MATCH` ("route exists, evidence absent/unresolved" — polarity-invariant, "absence of evidence is not evidence"), `NO_KB_PATH` ("no route into the KB at all"). The two-tier abstention taxonomy is clean and architecturally meaningful.

**`CONTRADICTED` is gated on functional (`single_valued`) predicates only**, with two suppressors: an unresolved object → `NO_MATCH` (a mismatch is a resolution failure, not a falsehood), and a datatype-incompatibility guard. Multi-valued mismatches degrade to `NO_MATCH` except the narrow location-disjoint path. This is the soundness core and it is correct.

**Geographic knowledge is split across three hardcoded surfaces** — the worst architectural smell in this layer:
- `CONTINENT_QIDS` (a hand-validated 8-Q-id frozenset, `kb_verifier.py:31`) drives `_location_disjoint`.
- `_GEO_REGION_TYPES` (a 9-member exact-P31 set) + `_GEO_CONTAINER_TYPES` (`kb_wikidata.py:310–320`) drive the P361 bridge and object widening.
- Three *divergent* part_of property sets coexist: the transitive closure `(P131,P30,P17)`, the type-guarded P361 bridge, and the neighbor-enumeration default `(P31,P279,P361,P131,P17)`. P361 is trusted in enumeration and the bridge but distrusted in the closure.

**The P361 region-containment bridge (commit b25c657) is sound but narrow.** It admits a single P361 hop only when both endpoints have an exact P31 in the closed 9-member region set — a city (Q515) provably cannot enter via subclass, which keeps "Warsaw ⊄ Germany" closed while reopening "Massachusetts ⊂ New England." But it catches only *depth-1* region containment (a performance decision to avoid a WDQS timeout), and `establishing_property` returns None for any bridge-established verdict because the follow-up query uses the un-bridged property set — a trace-fidelity divergence.

**Two latent defects worth flagging.** (1) The reverse-neighbor `LIMIT` is read from a Config field that does not exist (`wikidata_neighbor_reverse_limit`, `kb_wikidata.py:1537`), so it is permanently the module default **20**; the docstring claiming "default 100" is dead text. (2) Live SPARQL synthesizes `valueType` only as entity|literal (`BIND(IF(isURI...))`), never date/quantity, so the date/quantity contradiction-type-safety guard is dormant against live data — and for the 10 seed entries that declare `object_type='date'` (vs the runtime enum's `'time'`), the guard is bypassed entirely.

**The empirical result is the soundness commitment rendered as a number**: medium-bar Run 8 is 0.0% false-verified vs the baseline's 10.7%, bought with 48.8% false-abstain and −20.5pp accuracy. The abstentions are dominated by *resolution* failures (`lookup_subject_unresolved` / `value_unresolved`), which suggests the highest-leverage coverage work is in Layer 3 resolution, not Layer 4 KB logic.

### 2.6 Layer 4 — Walker and Python verifier

**The §6.5 order is implemented but reordered.** The spec's discrete 5-step sequence is collapsed into a BFS frontier; within `_direct_lookup`, belief-revision conflict checks run *before* the Stage-1 literal Tier U match (a Cluster-3-step-7 fixup, `walker.py:440–530`), because the walker's own freshly-promoted row would otherwise mask the contradiction. KB-before-Python ordering and the Python routing gate are honored inside `_try_external_grounding`.

**`chain_includes_assertion` is a monotonic latch** (only ever set True): pre-set for `user_authoritative` predicates (`walker.py:320`), set on assertion-grounded belief revision and on the ungrounded-assertion fallthrough. External grounding "promotes out of" assertion-conditionality (no flag on `externally_verified` rows or successful upgrades). This is the dual-designation machinery, and it is genuinely exercised end-to-end — unlike retraction.

**Multi-hop bottoms out at depth 0 for KB-discovered chains.** `max_depth` defaults to 4, but the D5/D51 live-KB-neighbor fallback fires only when `depth == 0` (`walker.py:991`), an explicit wall-clock band-aid for the "18-minute per-case" D51 blowup. So the architecture's *flagship* example — "Asa lives in the United States" = Tier U(Williamstown) + KB(part_of chain) + distribution(lives_in distributes up) — cannot complete a 2+ hop KB chain. This is the dominant cause of `multi_hop_distribution` sitting at ~45% vs 95% baseline. And the deeper gating is in Layer 3: `predicate_distribution.consult("lives_in", 1, "part_of")` returns `neither`, *closing the expansion gate entirely* (D52) — so for the canonical case there are 0 expansion edges regardless of walker code. The multi-hop failure is primarily a substrate-oracle prompt problem, not a walker problem.

**Two hardcoded-shape band-aids in the walker** violate the no-hardcoded-mappings doctrine: `_is_persona_subject` (a raw SQL probe for `(party,'user','identity',X)` rows that skips KB entirely, papering over the resolver mapping "Asa" → Asa King of Judah, `walker.py:843–875`) and `_is_vague_class_object` (a regex on indefinite articles / relative clauses that skips object-conflict contradiction for "a town in the US"-style objects, `walker.py:127–154`). Both preserve soundness by string-matching rather than routing knowledge through a source.

**The Python verifier is a single-shot tri-state harness** (TRUE/FALSE/NONE → verified/contradicted/no_terminal_result), where every failure mode collapses to abstention. It is gated by `routing_hint == 'python'` (the F-042 fix, CI-enforced by an AST structural test), with fail-closed defaults. Model: Devstral Small via OpenRouter, chosen for soundness (0 false-verifieds where five other candidates each had 1).

### 2.7 Layer 5 — Result, aggregation, intervention, trace

**The six-verdict set is a single source of truth** (`aggregator.py:20–51`), authored in the walker and consumed by the aggregator. But **dual designation collapses to base before any user-facing decision** (`base_verdict_of`, `chat_wrapper.py:135`): a `verified_given_assertion` claim (grounded purely on the user's own assertion) produces exactly the same PASS_THROUGH contribution as a Wikidata-grounded `verified`. The entire `*_given_assertion` apparatus is observability-only at v0.15. This is the single largest gap between stated intent ("distinguish externally-grounded from user-asserted reasoning") and live behavior: the engine's headline soundness claim is invisible at the chat surface, exposed only in `/verification/{id}` and the audit log.

**The InterventionPlan redesign is a genuine improvement.** The 4-value enum became a 3-value top-level (`PASS_THROUGH`/`INTERVENE`/`DECLINE`) + per-claim `ClaimAction`s, fixing a real soundness-of-presentation bug where a mixed draft (one contradicted + one abstained) silently dropped the abstain. DECLINE now fires only on the genuinely-dominated case (zero verified AND ≥2 problematic). Note the spec's §4.6 "four moves" is now stale — the architecture doc was not updated.

**The retraction pillar is the most consequential dormancy:**
- **D15/D30**: `ContradictionTracer` is constructed only in tests, never in `pipeline.py`/`app.py`; there is no API endpoint to receive external corrections. §7.3 retraction source #2 is fully inert.
- **D14**: `propagate_retraction` is a single row→verdict hop whose return value is *discarded*; the `verdict_retracted` audit event has no consumer; there is no cascade and no re-derivation.
- **D13**: `_extract_source_rows` harvests only tier_u/predicate_translation/subsumption row ids; KB `premise_lookup` edges carry no retractable id and `entity_resolution_cache` is never referenced. A purely KB-grounded verdict records empty `source_rows` and is permanently unreachable by retraction.

Only the consistency-check→propagator hop is live (and it is the one that cascade-retracted 17 good seeds). The justification trace — the layer's richest artifact and the basis of the auditability value proposition — is **not durably persisted** (only `source_rows` go to the audit log; the rich trace lives in an in-memory dict lost on restart). `/verification/{id}` works only within a process lifetime.

### 2.8 Deployment surface, seeds, and configuration

**Model routing** (`client.py:57–84`): chat / extractor / `predicate_translation` / entity-normalization on `claude-haiku-4-5` (Anthropic native); `subsumption` / `predicate_distribution` / `entity_resolution` on `qwen/qwen3-next-80b-a3b-instruct` (OpenRouter); `python_verifier` on `mistralai/devstral-small` (OpenRouter). A single `/chat` turn fans out across two providers and silently requires both `ANTHROPIC_API_KEY` and `OPENROUTER_API_KEY`; a missing key surfaces as a RuntimeError deep inside the first substrate call, not at startup. `extractor:assistant` is dead config (the draft extraction hardcodes `purpose='extractor:user'`, so the spec's user/assistant asserting-party distinction is never realized).

**`RUN_LIVE_KB` is an env-only flag** read inside `WikidataAdapter.__init__` (`kb_wikidata.py:562`), not a Config field, not validated, not surfaced in `/health`. The deployed `/chat` will silently serve *fixture-backed* verdicts if it is unset, with no operator signal.

**The seed pack is 83 entries and is essentially a Wikidata-property lookup table**: 80 `kb_resolvable`, 3 `user_authoritative`, **0 `python`, 0 `abstain`**. The two routings that produce safe behavior under uncertainty have zero seeded priors. Only **1** entry carries entity-type filters and **0** populate `distinct_slots`, even though the oracle prompt prescribes them for born_in/educated_at/holds_role/nationality/has_capital — so D33 type-filtering is dormant for ~99% of seeded predicates. The normalization map emits **8 canonical targets with no seed** (`founded`, `is_capital_of`, `is_president_of`, `is_ceo_of`, …) — the system's own preferred surface forms route high-frequency relations straight to cold-start, including `is_capital_of` which is unseeded while `capital_of`/`has_capital` are seeded.

**The cold-start cliff is sharp, and the calibration default sits on the wrong side of it.** The v0.15 calibration runs cold-start (`open_memory_db(load_seeds=False)`), while the benchmark eval runs seeded (`build_wrapper(load_seeds=True)`). The two headline regimes measure opposite machines, and nothing in the config surface flags this for a reader. The measured value of seeding, on the corpus that exercises it most, is +8pp on derivation (54% seeded vs 46% cold-start).

---

## 3. v0.16 deferred-items synthesis

`docs/v0.16_planning.md` captures 60 D-numbers, but it is as much a *completed-work ledger* as a forward plan: roughly 26 are already resolved in-flight. The true open surface is ~34 items, which split cleanly into three buckets.

### 3.1 Bucket A — Bounded calibration / seed / runner fixes (~9 open)

These are one-commit-to-one-session changes that don't touch the architecture:

| ID | What | Note |
|---|---|---|
| D23 | `lives_in` single_valued rationale invalidated by D16 | empirically-gated; one-line seed change |
| D32 | qualifier coverage beyond P580/P582/P642 | data-driven; false-abstains only |
| D39 | seed-pack vs corpus alignment (born_in_year/prefers/status) | add 3 entries + CI test |
| D46-residual | 6 drift rows → unseeded properties (P576/P749/P800/P37) | one-line seed additions |
| D52 | `predicate_distribution` prompt opens the gate for locative+part_of | ~1hr prompt work; **gates multi-hop** |
| D55 | seed-pack semantic-correctness audit as a standing pass | found P276→P131, P585→P276 errors |
| D56-residual | cold-start oracle doesn't produce post-D19 slot shape; never emits `both` | bounded prompt-teaching |
| D59 | Claim model `valid_from_ref`/`valid_until_ref` | undo the Rule-16 workaround |
| D60 | runner scores `claims[0]` only | set-semantic matching across all claims |

### 3.2 Bucket B — Genuine architectural changes (~13 open)

These reshape behavior and carry design risk:

- **The retraction / over-time-soundness cluster — D13 + D14 + D15 + D30.** This is the single most consequential structural gap. D6 restored in-process replay, but: KB-grounded verdicts can't be retracted (D13), the cascade and re-derivation are unimplemented (D14), the tracer isn't wired (D15), and there's no ingress API (D30). The four are interdependent — wiring the tracer (D15) is pointless without an ingress (D30), and both are weakened if KB verdicts stay unreachable (D13) and there's no cascade (D14). The doc offers re-derivation as either "implement" or "explicitly scope out" — a genuine fork that decides whether Aedos is a self-healing knowledge graph or a one-shot verifier.
- **D5 + D51 + D52 — multi-hop KB derivation.** D5 (outgoing neighbor enumeration) shipped but enumerates the *wrong direction* for the locative cases that motivated it; D51 (reverse enumeration) is implemented in code but the planning doc still frames it as future work (a doc-vs-code drift); D52 (the distribution gate that closes for locative+part_of) is the real blocker. Plus the `depth==0` fanout cap. These jointly determine whether the flagship cross-source-unification capability works.
- **D57 — three-value cardinality** (`functional` / `temporal_functional` / `multi_valued`). "Asa works at Google" with a prior Microsoft should contradict (point-in-time), but `employed_by` is multi-valued (career history). This is a schema change rippling across 7 predicates (employed_by, member_of, political_party, occupation, religion, headquarters_in, nationality) and interacts with the conservative single_valued default and D23.
- **D58 — Tier U normalizer determinism**: the Wikipedia normalizer produces different canonical subjects for a seed write vs a promotion write despite matching source_text, creating two rows instead of one idempotent. A real non-determinism defect in the normalization layer.
- **D9 — `verification_context` plumbing** through `log_event` at every call site, so audit events can be grouped per verification run. Foundational for every other audit feature.
- **D10 — Tier U → Python composition** (a scope *decision*, not just an implementation gap): can a Tier-U-retrieved premise feed a Python computation?
- **D29 — periodic consistency-check scheduler** (`check_periodic()` exists but nothing invokes it).
- **D53 — replace Wikipedia disambig scraping with `wbsearchentities`** (largely landed in Phase H; residual).
- **D43 / D25 — provider robustness** (OpenRouter tool-call compliance; DeepSeek V4 Morph incompatibility) — partly production constraint, partly a client-layer content-fallback architecture change.

### 3.3 Bucket C — Research questions / methodology discipline (~12 open)

The late backlog is dominated by a meta-pattern: *every audit pass surfaced a class of audit v0.15 was missing.* The doc proposes institutionalizing these as a standing pre-release slate:

- **D24** — audit the measurement instrument (runner-vs-corpus keys); the static key audit "would have caught all four" mismatches and is "the discriminating mechanism, not optional."
- **D48** — corpus-vs-pipeline-shape audit: corpora exercise components in isolation on clean inputs; the deployed pipeline traverses differently on noisy drafts. This is the deepest measurement finding (see §4 and §5).
- **D49** — variance-bound reporting (±4pp run-to-run; promote ×3-median to per-corpus).
- **D50** — cross-case state-isolation audit (the source of the false D16 finding).
- **D45** — component-prompt discipline (positive trigger + explicit non-triggers). Highest documented ROI: extraction 84.9%→100% for ~$0.78; names the unaudited substrate/walker/python-verifier prompts as next targets.
- **D26 / D36 / D38 / D41 / D34** — deployment-readiness audit, companion structural tests, runbook-vs-code drift, adversarial fixtures, and the unresolved aspirational-vs-empirical fixture question.
- **D54** — hybrid mixed-vocabulary measurement (third curve as a function of seed coverage).

### 3.4 What the benchmarks did to the backlog's priorities

The benchmark findings sharpen which items matter most:
- The **instance_of-vs-P106 predicate-normalization gap** (not previously a numbered D-item, but the dominant PopQA failure) is now arguably the highest-leverage *bounded* coverage fix — it belongs in the extractor or the oracle, not a lookup table.
- The **empty-extraction pass-through** is reframed from a quiet default to a *soundness hole* (the chili-pepper case): an extraction that yields 0 claims from a confident draft should arguably trigger caution, not silent pass-through. This is a new, small, high-importance item.
- **D48** rises from a methodology nicety to *the* lens for interpreting every headline number, because the benchmarks are precisely the pipeline-shape inputs the corpora never test.
- The retraction cluster (D13–D15, D30) and dual-designation surfacing become more important *if* v0.16 pursues the session-knowledge / audit-trail value (Candidate C below), because those are the capabilities that machinery serves.

---

## 4. Benchmark findings synthesis

Three smoke runs in `../aedos-simpleqa`, all using the same model (`claude-haiku-4-5`) for draft, baseline, and grader, so the only variable is the verification layer. *Caveat on config:* the comparison reports' cost-note names "gpt-4.1-mini extraction/substrate calls," but that string is **stale hardcoded text** in `run_evaluation.py:227` (and the `.env` comment describing gpt-4.1-mini extractor defaults is likewise pre-Phase-E). The current `client.py` routes extraction to Haiku 4.5 + v5 prompt and substrate to Qwen, so the runs almost certainly used the calibrated production config — meaning the extraction-quality failures below are **real findings about the production extractor**, not artifacts of a wrong model. (Operator should confirm against the actual CallRecords/transcripts, since `bootstrap.py` still requires `OPENAI_API_KEY`, which is unexplained under the current routing.)

### 4.1 SimpleQA (30 cases) — a degenerate discriminator

29/30 baseline cases are NOT_ATTEMPTED and all 30 Aedos cases are NOT_ATTEMPTED; both score F=0.000. The only signal is a single −3.3pp false-positive delta from one baseline error (Big Brother). Haiku, told abstention is permitted, declines on SimpleQA's deliberately obscure long tail by default. The two cases where the draft *attempted* (Big Brother, Edmund Burke) are the only true end-to-end exercises: in both, Aedos DECLINEd after every extracted claim returned `no_grounding_found` with empty `source_breakdown` — i.e. it abstained by **KB-emptiness**, not by contradicting a Wikidata value. Edmund Burke is the cleanest single win: the draft asserted a false date (June 30, 1782 vs gold January 8, 1784) and Aedos suppressed it to NOT_ATTEMPTED — but again by failing to ground the surrounding entity claims, not by checking the date. **SimpleQA confirms the abstention mechanism works but cannot demonstrate value at scale, because the base model already abstains on 96.7% of cases.**

### 4.2 TruthfulQA (15 cases) — the value proposition inverts

The non-degenerate benchmark. Baseline is strong (truthful 86.7%, truthful+informative 73.3%, F=0.786) because Haiku is a competent misconception-debunker. Aedos semantic collapses to 6.7% informative, F=0.000 — it abstains on 14/15 cases. It grounded **zero** claims across the entire run (both pass-throughs were *empty*).

The mechanism is uniform: commonsense/causal/negative-existential propositions ("your digestive system breaks down watermelon seeds," "vampires are not real," "the Sun's position at birth has no impact on personality") have **no Wikidata representation in principle**, so every extracted claim returns `no_grounding_found` / `depth_exhausted`. Aedos cannot ground a non-existence claim at all. On the one case where Aedos's draft was *more* truthful than the baseline (vampires: baseline INCORRECT, Aedos draft CORRECT), the INTERVENE demoted it to semantic NOT_ATTEMPTED — its only truth-beat earned zero credit.

**The chili-pepper case is the single most important data point in either benchmark.** The draft correctly says the placenta is spiciest. The extractor pulled **zero claims** (`claim_count: 0`, `total_llm_calls: 0`), so Aedos PASS_THROUGH'd the draft *unverified* — and the semantic classifier, which grades a pass-through on the draft, recorded it as Aedos's lone INCORRECT (the grader itself erred, marking a gold-matching answer wrong). The architectural lesson stands regardless of the grader error: **Aedos's only non-abstention on TruthfulQA was a case where it verified nothing.** Had that ungroundable draft been a confidently-wrong misconception the extractor also failed to decompose, Aedos would have passed it through unflagged. The soundness guarantee is conditional on extraction recall.

### 4.3 PopQA (15 cases) — the grounding gap, cleanly

PopQA is the benchmark built *for* Aedos: every question is a Wikidata triple (occupation P106, place-of-birth P19, genre P136). Yet Aedos attempts 0/15 vs the baseline's 4/15. Two findings:

- **The predicate-normalization gap is the dominant failure.** "Robby Krieger is a guitarist" extracts as `instance_of / guitarist` → P31, never P106. The answer-bearing triple is *never queried*. All 8 occupation cases attempt 0/8. The fix belongs in extraction (emit `occupation` for "X is a <profession>" with a human subject) or the oracle (route an `instance_of` claim with a human subject and occupation-class object to P106) — not a lookup table.
- **Grounding works when the predicate maps.** In the Tadhg Dall Ó hUiginn case, `died_on / 1591` resolves to P570 and returns `verified` with `source_breakdown.kb: 1` — a real Wikidata hit — while the co-extracted `instance_of / Irish poet` from the same draft abstains. The grounding stack (entity resolution, property lookup, value comparison) is demonstrably functional; the gap is upstream in predicate routing.

PopQA also exposes **verbose-draft compounding**: the Robby Krieger draft produced 9 claims (8 about a synthetic band-founding event), all ungroundable, so one answerable fact is buried and the whole response is DECLINEd. Aedos's coverage is hostage to draft verbosity, and there is no notion of "the answer-bearing claim" vs "supporting color."

And critically: **both systems scored 0 wrong answers.** The "Aedos abstained, baseline hallucinated" bucket was empty. Aedos paid the full coverage cost of soundness and collected none of the benefit.

### 4.4 The cross-benchmark patterns

1. **The calibrated-baseline pattern.** Across all three, Haiku-4.5 with an abstention-permitting prompt does not hallucinate on factual questions — it either abstains or is right. PopQA hallucination 0%, SimpleQA 3.3% (n=1), TruthfulQA falsehoods only on misconceptions. The original "catch the LLM's fabrications" value proposition has been substantially absorbed by the LLM's own calibration.

2. **The grounding-gap pattern.** Aedos extracts claims that don't map to Wikidata's representation: instance_of vs P106 (PopQA), zero extraction from misconception-shaped prose (TruthfulQA chili pepper), refusal text minted as claims (`Paul Singer / born_in / <UNKNOWN>` on PopQA). These are extraction/routing failures, not KB failures.

3. **The verbose-draft pattern.** When the LLM knows an entity, it produces multi-claim descriptions; all-claims verification means one ungroundable elaboration forces an abstention. Invisible in the uniform medium-bar single-statement cases; dominant on real drafts.

4. **The structural-mismatch pattern.** TruthfulQA's question class (causal/commonsense/negative) has no Wikidata representation *in principle* — no coverage improvement fixes it.

### 4.5 What the benchmarks structurally cannot measure

Every case is a single-turn factoid scored on one CORRECT/INCORRECT/NOT_ATTEMPTED letter. The harness cannot measure three of Aedos's stated value propositions:
- **Session-knowledge accumulation** — every case is a fresh `respond()` with a unique `conversation_id`; there is no multi-turn sequence where a Tier U fact grounds a later claim. Aedos's core thesis is entirely outside the frame.
- **Grounding-source attribution** — `source_breakdown` (KB/Tier-U/Python) is captured but collapses to one letter; a KB-verified answer and a Tier-U-asserted answer grade identically.
- **Audit-trail value** — the per-claim trace and verification_id are recorded but worth nothing in the metric.

This is the crux: the benchmarks measure exactly the regime where Aedos is weakest (single-turn factoid QA against a calibrated baseline) and cannot touch the regime where its architecture was designed to add value.

---

## 5. Cross-cutting patterns and theories

### 5.1 What is working architecturally

- **Per-verdict soundness is real, measured, and well-engineered.** 0 false-verifieds across 668 calibration invocations and four medium-bar runs; the fail-closed defaults (`single_valued→0`, `routing→non-python`, `Python→None-on-uncertainty`) are disciplined and consistent. When the architecture says "I won't assert what I can't ground," it means it.
- **The wins are concentrated in two modes**: principled_abstention (Aedos 100% vs baseline 20%, +80pp) and belief_revision (50% vs 20%, +30pp). These are exactly the modes the benchmarks *don't* test. They generalize to the degree that the deployment surfaces (a) questions where the right answer is "I don't know" and (b) sessions where the user has stipulated facts that later claims contradict. They are *not* artifacts in the sense of being wrong — they are real — but they are demonstrated only on a constructed test set whose case mix was chosen to exercise Aedos's advantages (`evaluation_methodology.md` is explicit about this bias).
- **The entity-grounding stack works when inputs map cleanly** (died_on→P570, well-known entities verified against the KB). The machinery is sound; it is starved by upstream extraction/routing and by Wikidata's coverage shape.

### 5.2 What is not working architecturally

- **The composition layer (the walker) is the weakest link, and it is structural.** Individual oracles score 84–100%; derivation sits at 54%. Multi-hop bottoms out at depth 0; the distribution gate closes for the most common locative chains (D52); model choice doesn't move it (Sonnet ties at 34%). This is the gap between "I can check one fact" and "I can compose facts into a conclusion" — and the latter is the architecture's distinctive claim.
- **A large fraction of the architecture is dormant or unsurfaced**: dead Layer 2, inert retraction pillar (D13–D15, D30), collapsed dual-designation, inert KB-wins in chat, global (not session) Tier U, in-memory-only trace store, the chat draft extracted as if the user asserted it. The system *as deployed* is much less than the system *as designed*.
- **The extraction → Wikidata mapping is lossy in both directions** — predicates that should map don't (instance_of/P106), and drafts that should produce one checkable claim either produce nine ungroundable ones or zero.

### 5.3 Six theories (held in tension — the operator should weigh them)

**Theory 1 — Sound but under-calibrated.** The gaps are bugs, not misalignment: fix instance_of→P106, expand seeds, terser drafts, open the distribution gate (D52), tighten the Claim model (D59). The same architecture lifts substantially.
*Support:* died_on→P570 proves the stack works; many failures are bounded routing/seed bugs; D45 shows +15pp prompt wins are cheap. *Challenge:* the Run 6→7/8 regression shows the cheap tuning lever is exhausted and partly *anti-correlated* with soundness (band-aids → false-contradictions when removed); the derivation ceiling is model-independent and structural; the calibrated baseline gives Aedos little to catch even at full coverage. **Verdict: partially true for a bounded coverage recovery, false as the whole story.**

**Theory 2 — The value proposition is narrower than originally framed.** Aedos isn't "verify the world"; it's "accumulate session knowledge with explicit grounding-source attribution and an audit trail." The benchmarks don't fit because they don't exercise this; the medium-bar belief_revision/abstention wins are the real value.
*Support:* the wins are exactly in the session/abstention modes; the benchmarks structurally can't measure the claimed value (§4.5); the architecture's most sophisticated machinery serves session knowledge. *Challenge:* the deployment doesn't actually *realize* session knowledge — Tier U is global, dual-designation is collapsed, the chat extracts the draft (not the user's assertions in a way that persists meaningfully), and the audit trail isn't durably stored. **Verdict: this is the most defensible reframing of where value *could* live, but the value is currently unbuilt as much as unmeasured.**

**Theory 3 — Structural issues need rethinking.** The extract-then-verify pipeline produces ungroundable noise on verbose drafts; atomic per-claim verification conflicts with how facts and drafts are actually shaped; the retraction pillar is half-built; session Tier U doesn't compose with KB grounding.
*Support:* verbose-draft compounding, empty-pass-through hole, dead Layer 2, dormant retraction, the all-claims-no-answer-claim problem. *Challenge:* the *core* per-claim verification is sound and works; "rethink everything" risks discarding the one thing that works. **Verdict: true in specific, addressable places (verification-during-generation? answer-claim selection? wire-or-delete the dormant machinery?), not as a wholesale redesign.**

**Theory 4 — The deployment context shifted.** When Aedos was conceived, LLMs hallucinated more. Modern calibrated models abstain when uncertain. The "verify outputs" use case has been partly solved by calibration, leaving Aedos's value in a narrower band.
*Support:* the strongest single benchmark finding — the calibrated baseline doesn't hallucinate (PopQA 0%, SimpleQA 3.3%). The −20pp coverage cost buys a soundness margin the baseline mostly already has. *Challenge:* calibration is *prompt-conditional* (the baseline was explicitly told to abstain); in adversarial or forced-answer deployments, or on rarer/harder distributions, hallucination returns. **Verdict: strongly supported by the data; the most uncomfortable theory and the one most worth confronting.**

**Theory 5 — The value lives in untested domains.** Medical, legal, financial — domains with authoritative KBs and real audit needs. General-purpose benchmarks aren't where Aedos shines.
*Support:* §3.1 of the spec names exactly these as the high-cost-of-false-verification domains; the architecture's KB-agnostic protocol is built for swapping Wikidata for SNOMED/UniProt/an enterprise KG. *Challenge:* untested — there is no evidence yet, and domain KBs have the same "thin on causal/commonsense" shape Wikidata does. **Verdict: a plausible product direction, currently a hypothesis with zero data.**

**Theory 6 — Wikidata grounding is too thin.** Wikidata covers entity-attribute facts but not causal, temporal-relational, evaluative, or negative-existential claims people actually make. The architecture commits to Wikidata-shaped grounding, which limits scope to the subset of factual claims that fit.
*Support:* TruthfulQA is the proof — its entire question class is unrepresentable; Aedos can't ground a negation at all. *Challenge:* this is a property of *any* structured KB, not Aedos specifically; it argues for additional grounding sources (text/NLI), which is a large scope expansion. **Verdict: true and important; it bounds the addressable claim space more tightly than the spec's "out of scope" list admits.**

**The synthesis of theories.** The evidence most strongly supports a *blend of 4 + 2 + 6*: the deployment context has shifted (calibrated LLMs absorbed much of the original problem — 4), which pushes the real value into a narrower band (session knowledge + audit + grounding-source attribution — 2) that is *also* bounded by Wikidata's representational thinness (6). Theory 1 is true for a bounded, cheap coverage recovery that is worth doing but won't change the strategic picture. Theory 3 is true in specific, fixable places. Theory 5 is the most promising *forward* bet but is currently faith, not evidence. The uncomfortable common thread: **the system's sophistication is not currently matched by demonstrated value, because the value either isn't built (dormant machinery), isn't measured (no session/audit benchmark), or has been partly obviated (calibrated baselines).**

### 5.4 The architecture's internal tensions

- **Atomic-claim vs verbose-draft.** Aedos commits to atomic per-claim verification, but LLMs produce verbose multi-claim drafts. The atomization produces ungroundable noise and, with all-claims verification, lets one ungroundable elaboration sink an answerable question. A "verify the answer-bearing claim, note the rest" model would help — at the cost of the clean per-claim soundness story.
- **Soundness vs coverage, re-examined.** The spec frames this as a deliberate trade. The benchmark data adds a sharper edge: the soundness *benefit* is small (calibrated LLMs rarely fabricate) while the coverage *cost* is large (−20pp to −80pp). The trade is still correct in high-stakes domains where one false-verified is catastrophic; it is hard to justify on general QA against a calibrated baseline. The trade's value is entirely a function of deployment context.
- **Session-scoped vs global knowledge.** Tier U is conceptually per-session but physically global (keyed on `asserting_party='user'`). Factual knowledge is about the world (not per-session); user-asserted reasoning is per-session. The status enum conflates the axes, and the deployment enforces neither boundary.
- **Principled paths vs band-aids.** Phase 10.5 generalized hardcoded tables into principled prompt/oracle paths — and lost ~10pp and gained false-contradictions doing it (Run 6 → 7/8). The principled paths are slower, higher-variance, and (for high-precision tasks like demonym→country) may have a *structurally lower ceiling* than the lookup tables they replaced. This is a real, unresolved question: does the no-hardcoded-mappings doctrine cost more accuracy than it's worth for a system whose headline is accuracy?
- **Spec trails code by a phase** (D4/D12/D20/D21, and now §4.6's four moves, and D51's doc-vs-code drift). The architecture document is a *trailing record*, not a forward spec. This is a process tension worth naming.

### 5.5 The operator's two claims, tested directly

**"A good system headed in the right direction that needs calibration/tuning and perhaps large architectural changes."** Half-confirmed. The soundness engineering is genuinely good and the per-claim verification works. But "the right direction" presumes the destination (general factual verification) is still the right destination, which the benchmarks call into question. And "calibration/tuning" vs "large architectural changes" is a false menu: the cheap tuning is largely spent (§5.3 Theory 1), and the gains that remain require either real architectural work (multi-hop, free-text subsumption, extraction routing) *or* a scope decision — neither of which is "tuning."

**"We kept seeing gains; no reason we can't keep seeing them."** This is the claim the medium-bar trajectory most directly tests, and the honest answer is: the gains were not a repeatable trend. Decompose the Run 1→6 climb (27.9% → 68.0%):
- *Harness/config fixes* (Fix 5 empty-system block, the DB-path override, chain-flag stripping) — one-time measurement unblocking, not repeatable.
- *Soundness corrections* (subsumption upgrade, Python None, self-ref reject, NULL-sq skip) — one-time, durable, but bounded; they eliminated specific defects.
- *Band-aids* (demonym table, population regexes, year-aware verb table, hand-curated P50/P112 rows) — these carried ~10pp of the peak and were *removed* by the five "Generalize" commits, dropping Run 7/8 to 57.4% *and* reintroducing false-contradictions.

So the durable, principled, repeatable gains are the soundness corrections; the coverage gains were substantially one-time or band-aid. The honest shipped number is ~57.4% against a ~74–79% baseline, and the next increment requires closing architectural ceilings, not more of the same tuning. The trajectory is evidence *against* "more tuning keeps working," not for it.

---

## 6. Recommendations for v0.16

These are candidate shapes with trade-offs, not a prescription. The operator chooses. After each, the author's assessment.

### 6.1 Candidate Shape A — Calibration and tuning

Treat the gaps as bugs and grind them down: instance_of→P106 routing; expand the seed pack (and reconcile the 8 unseeded normalization targets); open the distribution gate (D52); fix the empty-pass-through hole and refusal-text extraction; tighten the Claim model (D59); fix normalizer determinism (D58); close the date/`time` object_type divergence; make the reverse-neighbor LIMIT a real Config field. Address the named medium-bar ceilings where bounded.

*Trade-off:* incremental, bounded, low-risk, recovers real coverage — plausibly recovers the ~10pp the generalization lost, *via principled paths this time*. But it leaves the strategic questions untouched, the architecture's dormant 60% unwired, and (per §5.3) it will not change the picture that the calibrated baseline gives Aedos little to catch on general QA.

*Author's assessment:* **necessary but not sufficient.** A subset of these are genuine bugs that should be fixed regardless of strategy (instance_of→P106, empty-pass-through→abstain, refusal-text→no-claim, D58). Do these as a fast first wave. But do not mistake them for a v0.16 thesis.

### 6.2 Candidate Shape B — Architectural redesign

Rethink core commitments: verification *during* draft generation rather than after (so the model is steered toward groundable claims); claim *clusters* / answer-claim selection rather than flat atomic verification (so verbose drafts don't self-sink); wire and complete the retraction pillar (D13–D15, D30); make Tier U a real persistence boundary; add a second grounding source (text/NLI) for the claim classes Wikidata can't represent.

*Trade-off:* potentially large lift and addresses Theories 3 and 6 directly, but speculative, expensive, and risks the one thing that works (clean per-claim soundness). Multiple of these are research projects.

*Author's assessment:* **two pieces are worth doing; the rest is premature.** The answer-claim-selection / verbose-draft problem is real and tractable (it's the difference between PopQA 0% and PopQA-could-attempt). Completing the retraction pillar matters *only if* Aedos commits to the session-knowledge / self-healing direction (Candidate C) — otherwise D14's re-derivation should be explicitly scoped *out* of the architecture rather than left half-built. The "verify during generation" and "add NLI grounding" ideas are large and should wait on a scope decision.

### 6.3 Candidate Shape C — Scope refinement

Stop chasing factuality-benchmark coverage. Reposition Aedos as a *session-knowledge-accumulation and grounding-attribution* system: build out Tier U (real session/conversation scoping, cross-session persistence as a first-class option, retraction handling, evidence linking); surface the dual-designation distinction the engine already computes; surface grounding-source attribution to the user; surface the "you just told me something the KB contradicts" warning that's currently discarded; build a multi-turn benchmark that actually exercises this.

*Trade-off:* commits to a narrower, more defensible scope that matches what the architecture is good at — but abandons the general-verifier framing and requires building measurement that doesn't exist.

*Author's assessment:* **the most intellectually honest direction.** The benchmarks show the general-verifier framing doesn't fit a calibrated-LLM world; the architecture's sophistication (TMS retraction, dual-designation, Tier U, audit trail) is *built for exactly this* and currently wasted. The catch: this requires *building* the value, not just measuring it — un-collapse dual designation, make Tier U session-real, wire the contradiction surface, persist the trace, and build the multi-turn benchmark. Much of Bucket B's retraction work becomes load-bearing here.

### 6.4 Candidate Shape D — Domain specialization

Pick a domain (medical/legal/financial) with an authoritative KB and a real audit need; build the KB-protocol adapter and seed pack for it; demonstrate value where soundness-over-coverage is unambiguously the right trade.

*Trade-off:* the most product-oriented and the cleanest fit for the soundness thesis (in a clinical or compliance setting, one false-verified *is* catastrophic and the audit trail *is* the product). But it's a big lift (domain KB integration, domain extraction, domain corpora) and validates the KB-agnostic claim the architecture was built for.

*Author's assessment:* **the most promising long-term bet, but premature as v0.16.** It depends on first proving (via Candidate C's measurement) that the session-knowledge/audit value is real and usable, and it requires a domain partner or dataset that doesn't exist yet. Hold it as the v0.17+ north star.

### 6.5 The author's recommendation

A sequenced combination, not a single shape:

1. **First wave (weeks, low risk): the genuine bugs from Candidate A.** instance_of→P106 (in extractor or oracle); empty-extraction → abstain/caution instead of silent pass-through (closes the soundness hole the chili-pepper case exposed); refusal-text → no claim; D58 normalizer determinism; D52 distribution gate; the date/`time` object_type fix. These are unambiguous improvements regardless of strategy and they recover coverage *and* close a soundness hole.

2. **Second wave (the strategic core): make a scope decision, and build the measurement for it (Candidate C).** The single highest-impact thing v0.16 can do is **answer "what is Aedos for?" and build a benchmark that tests that answer.** Right now the system's claimed value is literally unmeasured *and* partly unbuilt. If the answer is "session knowledge + audit + grounding attribution," then: un-collapse dual designation at the chat surface, make Tier U session-scoped for real, wire the discarded contradiction warning, persist the trace durably, and build a multi-turn Tier-U benchmark. This converts the dormant machinery into demonstrated value and is the prerequisite for any honest claim about Aedos's worth.

3. **Decide the fate of the dormant machinery explicitly.** For each of: Layer 2 (Router/Validator), the retraction cascade (D14), `ContradictionTracer` (D15/D30) — either *wire it and prove it* or *delete it and update the spec*. The current state (built, documented, dormant) is the worst option: it's the exact "unwired capability" the v0.15 audit chain spent 10 rounds eliminating, and it makes the architecture document a fiction.

**Highest-impact single thing:** define what Aedos is for and build the benchmark that measures it. Everything else is downstream of this.

**Lowest-risk single thing:** the instance_of→P106 fix plus empty-extraction→caution. Together they recover the cleanest coverage on the benchmark built for Aedos *and* close the one genuine soundness hole, with no architectural commitment.

**A harder recommendation the operator should sit with:** consider whether the architecture is *over-built relative to demonstrated value*. A great deal of sophisticated machinery — the TMS retraction graph, consistency checking with circuit breakers, dual-designation verdicts, the four-oracle substrate, the bounded-inference walker — is dormant, observability-only, or barely exercised. If the scope decision lands on "session knowledge + audit" (Candidate C), much of it becomes load-bearing. If it lands anywhere else, v0.16 should seriously consider *removing* complexity that isn't paying for itself, rather than continuing to maintain and document capabilities that never run.

---

## 7. Open questions for operator decision

These are genuinely the operator's to decide; the analysis can frame but not resolve them.

1. **What is Aedos for?** The benchmark effort showed "verify LLM outputs against the world" is not a clean fit in a calibrated-LLM era. The more honest framings are "accumulate session knowledge with explicit grounding-source attribution and a re-derivable audit trail" (Theory 2) or "high-stakes domain verification where one false-verified is catastrophic" (Theory 5). Which is the target? Everything in §6 branches on this.

2. **Who is Aedos for?** Session/organizational knowledge contexts, high-stakes professional domains, or research/education? The architecture fits the first two; the benchmark distribution fits none of them.

3. **Wire-or-delete the dormant machinery.** Is the retraction cascade (D14) implemented or scoped out? Is `ContradictionTracer` wired (D15/D30) or removed? Is Layer 2 reinstated as the routing authority or deleted (with its anomaly-detection capability formally retired)? "Built but dormant" should not survive v0.16.

4. **Is the empty-extraction pass-through a v0.16 blocker?** It is the structural soundness hole: an extractor that yields 0 claims from a confident draft silently endorses it. Should zero-claim extraction trigger caution/abstain rather than silent pass-through? (Author: yes — this is a soundness fix, not a coverage one.)

5. **The cardinality question (D57).** Should `single_valued` become three-valued (`functional` / `temporal_functional` / `multi_valued`)? This is the principled resolution of the `employed_by`/`prefers` belief-revision cases, but it ripples across 7 predicates and changes false-contradiction risk. How does it interact with the conservative-default soundness commitment?

6. **The no-hardcoded-mappings doctrine vs accuracy.** The Run 6→7/8 regression shows generalizing band-aids cost ~10pp and added false-contradictions. Is the doctrine worth that cost for a system whose headline is accuracy, or should some closed-set knowledge (continents, region types) be accepted as deliberate, audited, soundness-preserving enumeration rather than band-aids to be removed?

7. **Benchmark methodology.** (a) Should v0.16 add a false-CONTRADICTION gate to the acceptance criteria (it swung 0→7 across runs and the shipped state reintroduced it, yet only false-VERIFIED is gated)? (b) Should coverage be reported as median-of-N with confidence intervals rather than a "55–70% band" that hides a code-state confound? (c) Should the grader be a stronger, independent model than the drafting model (it marked a gold-correct answer wrong)? (d) Should v0.16 build the multi-turn / Tier-U / audit benchmark that would actually test Aedos's thesis? (e) Confirm the benchmark eval used the calibrated config (the stale gpt-4.1-mini cost-note and the `OPENAI_API_KEY` requirement warrant a check of the actual CallRecords).

8. **The corpus-as-spec vs corpus-as-snapshot problem (D48).** The calibration corpora are re-pinned to match the build and exercise components in isolation on clean inputs, so they can't detect a regression baked into a re-pin and don't predict pipeline behavior on noisy drafts. Should v0.16 split a frozen spec-conformance set from a regression-snapshot set, and add a pipeline-shape corpus tier? Until this is done, the headline calibration numbers should be read as component-competence-on-clean-input, not system accuracy.

9. **Internal release sequencing.** Should internal release happen *before* v0.16 to gather real-user feedback (which would, among other things, finally exercise the multi-turn/session path the benchmarks can't)? Or after, to ship something less dormant? Note: the deployed `/chat` currently runs with a bare "helpful assistant" prompt, global Tier U, collapsed dual designation, and discarded contradiction warnings — an internal release today would not showcase the architecture's intended value.

10. **What is the win condition for v0.16?** Benchmark scores? A working session-knowledge demo? Specific capabilities wired and proven (retraction, dual-designation surfacing)? Internal-release feedback? This should be decided *before* the work, because Candidates A–D have very different success criteria and timelines.

11. **What defers to v0.17?** Even a comprehensive v0.16 won't close everything. The principled basis for deferral should be explicit. Candidate D (domain specialization) is the natural v0.17 north star if v0.16 lands on Candidate C and proves the session-knowledge value.

---

*End of synthesis. The author's strongest single conviction, offered for the operator to push back on: the v0.15.0 soundness achievement is real and worth building on, but the project's central risk is not a coverage gap — it is that a great deal of sophisticated, sound architecture has been built and left dormant or unsurfaced while the world it was designed for (hallucinating LLMs that need external verification) has quietly changed underneath it. v0.16's first job is not to fix bugs or chase benchmark points; it is to decide what Aedos is for, and then to either wire the architecture to serve that purpose or remove the parts that don't.*
