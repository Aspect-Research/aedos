# Aedos Forward Planning, Part 2 — Review & Response

*This is a review of your review, not a new plan. It records where we're aligned, where I push back, the deeper designs on the three things you wanted to explore (beating the selection bottleneck, best-of-both-worlds multi-hop, the "organic" substrate), and the decisions your review effectively settled. It is grounded in specific code (re-read firsthand) and specific literature (the new threads your musings raised). It is shorter than Part 1 by design. Respond to it the same way and we'll move to a concrete v0.16 work breakdown.*

---

## 1. Where we're now aligned (so I don't belabor it)

These are settled by your review; I state them once and move on.

- **What Aedos is for** — failure-mode transformation over the two structural floors (long-tail facts → KB/abstention; math → Python). You noted the three escapes I named *are* Aedos's three sources; that's the point, and it's the literature-backed justification to lead with. Your "Python could expand to address more symptoms" is real and I've parked it as a future item (see §8).
- **The keystone cut** — single-property → multi-property bindings (B1) and deferring property selection past extraction (B2). Committed.
- **Discover/verify split** (B3) as the composition spine. Committed.
- **Temporal: triples only** (Option T1 — start/end as separate date-in-object claims, endpoint arithmetic, no Allen algebra).
- **Kill `normalization.py`'s hardcoded map.**
- **Premise-retraction scoped to user-asserted-basis claims** (i.e. the `*_given_assertion` family — see §3.3).
- **Surface conditional verdicts** ("true *if* your premise holds").
- **"Not-checkworthy" is a quiet designation, not a loud abstain.**
- **Dead-code cleanup is deferred** — document it well, clean later. (One exception: the *retraction pillar* is architecture, not cleanup, so it's in scope below as the "partial TMS" question.)

The rest of this document is the parts that needed more than agreement.

---

## 2. The sharpest thing you said: over-abstention is the disease, and verify-every-claim is not its cure

You put it precisely: *"it abstains a heck ton and a disturbing amount, and it doesn't actually verify anything."* That is the disease. But I want to separate two things that are easy to conflate, because the fix for each is different — and you flagged that you didn't fully follow the verify-every-claim section (§6 clarifies the mechanics).

- **Disease A — the abstention rate is too high on claims that *should* verify.** This is caused by the composition engine failing to ground chains (§3) and the predicate map mis-routing (§4). Fixing those *lowers* abstention. This is the lever you care about.
- **Disease B — some claims never get a verdict at all** (the extractor silently drops them). Fixing this — "verify every claim" — does *not* lower abstention; if anything it surfaces a few *more* abstentions (the previously-dropped claims). Its purpose is **ledger honesty**, not coverage.

The reconciliation is your own "quiet designation" instinct: every extracted unit gets a verdict, but malformed/non-checkworthy ones are *quiet* designations, not loud "Aedos could not verify" notes. So verify-every-claim makes the ledger complete without flooding the UI — which is exactly the failure mode that drove the pre-v15 "only verify relevant claims" design you described. SAFE (https://arxiv.org/abs/2403.18802) is the cautionary mirror here: its relevance step *deletes* irrelevant facts; for Aedos the same step should *designate* them, never delete. Net: **§3 and §4 are the cure for the over-abstention you're worried about; §6 is an orthogonal honesty fix that must be kept quiet so it doesn't worsen the very symptom you're chasing.**

---

## 3. Your three musings, engaged

### 3.1 "Is Marie-Curie really a symptom of Aedos being bad?" — No, and the literature says exactly why

You're right to resist that framing. The leak (Warsaw → P206 Vistula → P17 → "Germany" producing a false `born_in`) was a **coverage-driven over-reach** — P361/P206 were in the transitive closure — that produced a false-verified, then was fixed by trimming the property set and adding the exact-P31 type guard (`kb_wikidata.py:300-365`, whose own comments narrate this). The bug was *trusting a property path that isn't entailment-preserving for `born_in`*, not Aedos being "bad."

The literature reframes it even more favorably to your instinct. Ferranti et al. (ISWC 2025, *Formalizing Repairs for Wikidata Constraint Violations*, https://aic.ai.wu.ac.at/~polleres/publications/ferr-etal-2025ISWC.pdf) studied how the Wikidata community actually repairs constraint violations and found that the **rule** is fixed far more often than the **data**: Conflicts-with violations are repaired by *deleting the constraint* 74% of the time, Single-value 73%, (Value-)Type ~35% via class-hierarchy edits. In their A-box (fix the world) vs T-box (fix the rule) taxonomy, "my rule/path was wrong here" is the *common* correct repair. The Marie-Curie leak is a textbook T-box error: the path was wrong for that predicate. So the right mental model is not "Aedos made a mistake" but "Aedos needs a place to record *which paths are entailment-preserving for which predicate*, and to record the exceptions when they aren't." That place is the substrate — and recording the exception is the seed of the "organic" idea you raised (§3.3). This is the single irreducible bit of trusted-schema knowledge I flagged in Part 1, and it's smaller and more principled than it looked.

### 3.2 Beating the 74% selection bottleneck — your instinct is the convergent answer; one gentle pushback on GraphRAG

You were excited about surpassing StructGPT's finding that 74% of KGQA errors are *relation/entity selection*, not hallucination. The literature is unusually unanimous, and it confirms your "propose many, let evidence decide, never commit to one" instinct *is* the answer the field converged on:

- **RoG** (https://arxiv.org/abs/2310.01061): emit the **top-3 relation paths** (beam), then let constrained BFS instantiate them against the KG — ungroundable paths die automatically. Ablating this propose-and-ground step costs ~21 F1 on WebQSP — i.e. *that step is where the selection gain lives.*
- **ToG** (https://arxiv.org/abs/2307.07697): keep a **beam of N≈3 relations**, never prune to one; raw-LLM WebQSP Hits@1 ~63 → ~83 with the beam. The jump *is* "beating the selection bottleneck."
- **GenRL** (https://arxiv.org/abs/2108.07337): generate **top-50** candidate relation bindings, then **validate against the KB and stop at the first that actually exists** — the KB, not the model's argmax, picks the winner. +9 to +59 F1 on relation linking specifically.
- **PoG** (https://arxiv.org/abs/2410.23875): add reflection + **backtracking** so a wrong selection is *recoverable*, not fatal.

The deep principle, and the one invariant you must preserve: **the arbiter has to be independent of the proposer.** Self-consistency / LLM-reranking-its-own-proposals gives little gain because the verifier shares the generator's biases. Aedos's *live Wikidata evidence* is exactly such an independent arbiter — which makes Aedos's planned design (propose candidate properties liberally, let a live SPARQL check decide, demote the distribution oracle from gate to ranker) **strictly stronger** than the self-consistency methods. Your discover/verify split is literally ToG's "keep a beam" + GenRL's "KB validates" generalized to *property selection*.

**The realistic gain, stated honestly:** the published deltas for this move are ~+13 to +24 Hits@1 on *answering* tasks. For Aedos-as-*verifier*, the translation is: **convert most selection errors into honest abstentions, and recover a double-digit slice of the answerable ones** (where a proposed property actually grounds). ToG-2 (https://arxiv.org/abs/2407.10805) even lists "overcautiousness — refuses to answer when info isn't explicit" as a *failure* for an answering system — but for Aedos that's the *desired* behavior. So you don't eliminate the 74%; you make it safe and recover part of it. Two practical notes from the evidence: the sweet spot is **N≈3-5 candidates, not "all properties"** (more injects noise), and **all gains are capped by entity-resolution quality** (GenRL 0.60→0.68 with gold entities) — so the `entity_resolution` oracle is the highest-leverage place to invest alongside this.

**The gentle pushback you asked for (you raised GraphRAG):** GraphRAG (Microsoft, https://arxiv.org/abs/2404.16130) is the *least* relevant import here. It builds a graph *from a text corpus*, does community detection, and LLM-summarizes communities for *sensemaking* over private documents. Independent evals (https://arxiv.org/html/2502.11371v3) find vanilla RAG *beats* it on single-hop/factoid, and its global mode loses fine-grained detail. It doesn't address property selection over a *curated* KG — which is Aedos's actual problem. The right family is the beam/path methods above (RoG/ToG/PoG), not community summarization. The one borrowable GraphRAG-adjacent idea is narrow: **precompute the candidate structure offline** (a property's neighbors, its subproperty/constraint closure) so selection is over precomputed candidates — which dovetails with §4. And ToG-2's concrete trick worth stealing: it moved *entity* selection from the LLM to a dense cross-encoder reranker (BGE) — cheaper and more accurate than asking the LLM to pick.

### 3.3 The partial-TMS pushback — you're right, and here's the precise thing it should be

You pushed back that the TMS failure modes (label explosion, odd-loops, order-sensitivity) are surpassable — "just have a certain amount of labels and stop adding more" — and that you want "something like a partial TMS." **You're right on both counts, and the literature is emphatic about it.**

- **On bounding the labels:** CDCL SAT solvers are the most mature nogood-learning systems on earth, and the hard-won lesson (*Too much information: Why CDCL solvers need to forget learned clauses*, PLOS ONE 2022, https://pmc.ncbi.nlm.nih.gov/articles/PMC9417043/) is that you **must** bound and forget learned clauses — keeping all of them slows propagation and actively *misleads* search in ~11% of cases. Modern solvers (Glucose's LBD, Kissat) flush aggressively. So "stop adding more labels" isn't a hack; it's mandatory practice in the field that *descends from TMS*. And critically: bounding a *sound* store only ever costs **re-work, never correctness** — if Aedos evicts a cached fact, it just re-checks against Wikidata next time.
- **On the failure modes:** odd-loops and order-sensitivity are properties of maintaining a single, persistent, mutually-dependent, *non-monotonic* belief set under retraction. Aedos is per-claim, session-scoped, and acyclic — *no claim's truth is justified by the negation of another claim's truth within a session*. So those modes don't bite by construction, not merely by bounding. Label explosion (the ATMS 2^n problem) comes from eagerly maintaining *all* derivations of *all* facts; Aedos computes derivations lazily for the claim at hand and discards per session. So the "death of TMS" was the death of the *eager, global, persistent* version — exactly the version that has no job in a session-scoped verifier.

**So here is the "partial TMS" precisely.** Keep: (1) the **dependency/justification data structure** — a semiring provenance term per derivation (`⊗` = conjunction of hops/sources a derivation needed, `⊕` = alternative derivations; Green/Tannen, https://homepages.inf.ed.ac.uk/jcheney/publications/provdbsurvey.pdf), computed lazily and discarded per session; (2) **premise-retraction** scoped to the user store — when the user corrects a Tier-U premise, re-derive the claims whose provenance term mentions it (which are exactly the `*_given_assertion` verdicts — answering your "that is user store, right?": yes, only verdicts that depend on a user assertion need re-derivation, and the dual-designation flag already tells us which those are); (3) a **bounded nogood/exception cache** (CDCL-style, evict-and-recheck). Drop: eager global relabeling, the persistent cross-claim contradiction cascade, the "every verdict stays globally consistent forever" ambition. That dropped part is what was dormant — and it should *stay* dropped. The partial TMS you want is the lazy-provenance + premise-retraction + bounded-nogoods version; that's both buildable and exactly the right size.

The remaining honest question for you is cost: mid-session re-derivation on a premise change could be eager (re-run affected walks immediately) or lazy (mark dependent `*_given_assertion` verdicts stale, re-derive only when next referenced). Lazy is much cheaper and fits the session model; I'd lean lazy. (Open question 1.)

### 3.4 Best-of-both-worlds multi-hop — the design you wanted to spend more time on

You wanted to think about whether we can get SPARQL's power *and* the cross-source case. We can, and the code shape is already most of the way there. Three moves, layered:

1. **KB-internal hops → one SPARQL property-path ASK.** This already exists and works: `_build_subsumption_ask_query` (`kb_wikidata.py:324`) fires `ASK { wd:source (wdt:P131|wdt:P30|wdt:P17)+ wd:target }` and verifies a whole geographic chain in one round-trip. The move is to **generalize it past geographic subsumption** to any predicate whose KB property is transitive (discovered from Wikidata's own metadata — some properties carry a "transitive" constraint), with the per-predicate entailment-safety recorded as a nogood/exception when it leaks (§3.3, the Marie-Curie lesson).
2. **Cross-source joins → the walker orchestrates, the KB step stays a SPARQL path.** The irreducible fact (Part 1, B3): a chain with a Tier-U or Python link *cannot* be one SPARQL query — Wikidata doesn't know "Asa." So the walker remains the orchestrator above SPARQL, but each *KB-internal* segment of a cross-source chain collapses to a single property-path ASK. Concretely, `_try_external_grounding` (`walker.py:662`) already calls `kb_verifier.verify` per node and the verifier already does the SPARQL path internally — so "best of both worlds" is: the walker joins Tier U ↔ KB ↔ Python at the *seams*, and each KB seam is a one-shot path query, not a hop-by-hop BFS.
3. **Search forward from premises, not backward from the goal.** The Williamstown/Asa case fails today because the walker *descends* from USA's millions of incoming edges (blind 20-sample, the `depth==0` cap). Seed a **premise frontier** from the session's Tier-U facts and expand it *forward* via bounded *outgoing* edges (`_build_neighbors_query`'s outgoing path is already un-LIMIT'd, `kb_wikidata.py:246`), meeting the goal in the middle. This defuses the cap and fixes the cross-source case by ascending Williamstown → Berkshire County → Massachusetts → USA (a handful of bounded P131 hops).

Net: SPARQL paths do the heavy KB-internal lifting; the walker does only the cross-source joins; the search runs premise-forward so it stays bounded. The distribution oracle becomes a *ranker* (which property to try first), never a *gate*. This is the design I'd put on the table as the starting point — and I agree it's worth more of your time before we commit the details (open question 2).

---

## 4. The substrate as a predicate map — deepened, because you wanted to go deeper here

You loved the "Wikidata encodes its own property relationships — cache for the cache" framing and the SLING result that the map can be built cold. Let me make the design concrete and confirm your preference.

**The cut, concretely.** `PredicateMetadata.kb_property` is a scalar `Optional[str]` today (`predicate_translation.py:258`), and `consult()` takes only the predicate string (`:295`). The change is: `kb_property` → a small *ranked set* of candidate bindings `[{property, slot_to_qualifier, single_valued, subject_types, value_types}]`, and `consult()` gains **object-type context** (so a copula's property is chosen *with the object's type in hand*). The verifier becomes a candidate loop with **evidence arbitration**: look up each candidate, the one whose value matches (and whose P2302 type-constraints the resolved entities satisfy) wins; VERIFIED only on a positive match; CONTRADICTED gated to the single-valued, constraint-matching property so fanning out never fabricates a contradiction.

**Where the candidates and the map come from — your preference is the right one.** You said: lean on Wikidata's own relations rather than expensive/unreliable cold building. Agreed, and the literature backs the ordering:
- **Primary: Wikidata's own property ontology.** P2302 constraints (subject-type Q21503250, value-type Q21510865, inverse, conflicts-with), P1647 subproperty-of, P1696 inverse, P1659 related-property. Ferranti et al. (2024, https://www.semantic-web-journal.net/system/files/swj3378.pdf) formalize P2302 as SPARQL/SHACL "witness patterns" — i.e. the constraint graph is *queryable live as an arbitration signal*, not a hand table. This is the "cache for the cache": a `property_relations` table that mirrors how `predicate_translation` already caches, but its source is Wikidata's self-description, built cold via the existing cache-then-generate pattern. P2302's value-type constraint on P106 ("value must be an occupation") is what resolves "X is a physicist → P106 not P31" by *lookup*. This is currently used **nowhere** in the codebase — it's pure upside.
- **Fallback only: SLING-style distant supervision** (https://arxiv.org/abs/2009.07726) for the edges Wikidata's own metadata doesn't encode. You're right it's the expensive/unreliable path; keep it as a fallback, not the spine.
- **Not the answer: relation embeddings** (RotatE) — they conflate co-occurrence with equivalence (P31 and P106 sit *near* each other precisely because they co-apply — the wrong signal) and give garbage vectors for new properties. Use at most as a weak retrieval prior.

This is "no hardcoding" *realized*, not abandoned: the predicate map is mined from the connected data's own schema and validated against live evidence. The ~50 synonym alias seed rows (a hidden hardcoded table) collapse because every surface form resolves to a P-id by the same path; inverse slot maps auto-derive from P1696. Seeds become optional overrides, empty by default — your cold-start ideal.

### 5. The "organic Aedos" musing — taken seriously, with the one safety rule that makes it sound

You mused that Aedos, "if it's a little bit organic — not just translating Wikidata — could adapt." This is a real, precedented direction, and it's the natural home for the Marie-Curie exception. But the literature draws a bright line you must hold:

- **Path A — refine the *schema/predicate-map/exceptions*** (cache that "born_in does NOT flow over P361/P206 here"): **safe, bounded, precedented.** Wikidata has a *native primitive* for exactly this — `exception_to_constraint` (P2303) — and Ferranti et al. show editors use exceptions as a first-class repair. Aedos mirroring P2303 in its substrate (a per-property nogood: "this path/translation does not hold for these subjects") is the no-hardcoding answer to the leak.
- **Path B — accumulate new *world facts* / correct Wikidata content**: this is the NELL path (https://www.cs.cmu.edu/~tom/pubs/NELL_aaai15.pdf) and it imports NELL's full **semantic-drift** risk — "one early false positive contaminates all later iterations," and NELL's own warning that *"an agent can perceive consistency but not correctness."* Keep B out of scope; it conflicts with your accepted stance that Wikidata correctness is a bounding factor.

The rule that makes Path A sound is **asymmetric trust by sign**: cache **nogoods/exceptions eagerly** (a nogood only ever makes Aedos *more* conservative — abstain/reject — which is always safe under soundness-over-coverage), but cache **positive corrections only as re-verifiable hypotheses** that must pass a live Wikidata check before they're trusted (a wrong cached *positive* is a grounded-looking false-accept, the one thing worse than a transient hallucination). Bound the store (CDCL lesson), re-validate on eviction or Wikidata version change. With that rule, your "organic" Aedos is coherent and the TMS-bounding intuition holds: a bounded, sound, acyclic, lazy cache trades only re-work, never correctness. I'd treat the full nogood/exception cache as a *follow-on* to the multi-property + discover/verify work (it's the riskiest novel engineering), but the *hooks* for it fall out of the provenance structure for free (open question 3).

---

## 6. Verify-every-claim, in plain terms (since you said you didn't follow it)

Today, the extractor's `_build_claim` (`extractor.py`) *returns `None`* — silently deletes the claim — in four cases: the subject/object aren't substrings of the source text; a content-less "occurred/happened" event; `subject == object`; `predicate == object`. Plus a fifth: the `INERT_PROSE` triage drop (`triage.py` — a hardcoded allow-list + a capital-letter heuristic). A deleted claim never gets a verdict; it just vanishes.

"Verify every claim" means: **never delete — every extracted unit terminates in a verdict.** The four malformed shapes become a *quiet* `no_grounding_found` with a reason (`self_referential`, `predicate_eq_object`, etc.), short-circuited *before* any KB lookup (this matters — if `(Einstein, born_in, Einstein)` reached the KB it would produce a false *contradiction*; the short-circuit preserves the soundness the drop currently buys). The hard-claim substring check stays as *detection* but emits a quiet abstain rather than a silent delete. And "not-checkworthy" (the INERT cases) becomes a quiet designation. This is bounded — a new `abstention_reason` enum, no new top-level verdict — and it's the *ledger-honesty* half of the picture, distinct from the abstention-rate fix (§2). It's also the principled replacement for the prompt-compensation band-aids these filters are.

**On your elegant-short-prompt point:** you're right that the 24-rule prompt "is kind of like hardcoding" and "can never contain enough rules," and that we may be "tuning the wrong thing." The architecture above is what lets the prompt *shrink*: the mapping knowledge (which property a predicate maps to, which surface forms are synonyms) moves out of accreting prompt rules and into **Wikidata's own ontology + live evidence**, where it scales without prompt growth. The prompt's job contracts to "produce a well-formed (s,p,o) triple with enough disambiguation to resolve the entities" — Molecular Facts' decontextuality+minimality (https://arxiv.org/abs/2406.20079). That *is* the elegant, short-prompt direction you want; the four drop-filters and the per-predicate extraction rules are exactly the prompt-compensation that the ontology-driven substrate makes unnecessary.

---

## 7. Decisions your review settled, and what stays deferred

**Settled (architecture, in scope for v0.16):**
1. Multi-property substrate via Wikidata's own ontology discovery (B1/B2) — the keystone; you said go for it.
2. Multi-hop = best-of-both-worlds (SPARQL paths + walker orchestration + premise-forward search), distribution oracle demoted to ranker (B3). Design in §3.4; details to refine.
3. Temporal = triples only (T1).
4. Premise-retraction (the partial TMS) scoped to `*_given_assertion` claims; lazy re-derivation preferred.
5. Verify every claim (quiet designations); surface conditional verdicts; emit corrected values (Part 1 §3.5 — the value is already computed and dropped at `walker.py:698-704`).

**Deferred (documented, handled later):**
- Dead-code cleanup (Layer 2 Router/Validator removal, dual-designation plumbing) — *except* the retraction pillar, which is the partial-TMS architecture above.
- The organic nogood/exception cache (Path A) — hooks fall out of the provenance work; the full cache is a follow-on.
- Phase E model re-selection; evaluation-harness building (incl. popularity-stratified measurement); bounded calibration/seed tuning.
- Python-tier expansion to more structural-hallucination symptoms (your musing).

---

## 8. Open questions back to you (just the genuine forks)

1. **Premise-retraction: eager or lazy?** When a user corrects a Tier-U premise, re-derive dependent `*_given_assertion` verdicts immediately, or mark them stale and re-derive on next reference? I lean lazy (cheaper, fits the session model). Does mid-conversation correctness justify the eager cost?
2. **Multi-hop sequencing:** generalize the SPARQL property-path to all transitive predicates first (fastest KB-internal win), or build the premise-forward frontier first (fixes the genuinely-hard cross-source case)? Both land eventually; which failure shape do you want closed first?
3. **The organic cache: v0.16 or v0.17?** The multi-property substrate and discover/verify split are the keystone and stand alone. The nogood/exception cache (Path A) is the riskiest novel engineering (NELL's drift risk, even scoped to schema). Commit it now, or land the keystone first and add the adaptive layer once the provenance structure exists?
4. **Multi-property arbitration precedence:** when two candidate properties both produce a positive value-match for an ambiguous claim, the constraint-matching property should win — but is there a case where you'd want both surfaced (e.g. a genuinely dual-typed claim)? Worth pinning the rule before implementation.

---

*Net of this review: you were right on the substance of every pushback. Marie-Curie is a T-box (rule) error, the common and expected kind, not Aedos being bad. The 74% selection bottleneck is beaten by exactly your "propose many, let evidence arbitrate, never commit to one" — and Aedos's live-Wikidata arbiter makes that move strictly stronger than the LLM-only methods, provided we keep the arbiter independent. The partial TMS you want is the right size and the literature proves bounding is sound. And the "organic" substrate is coherent and precedented — Wikidata even has the native P2303 exception primitive — as long as it caches nogoods eagerly and positives only as re-verifiable hypotheses. The architecture we need is this architecture with two real cuts and a disciplined adaptive layer, not a different one. Next, when you've responded, I'd turn this into a concrete v0.16 work breakdown.*
