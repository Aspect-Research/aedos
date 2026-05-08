# AEDOS: Architecture

*Draft 4. Post-Phase-5: substrate built, derivation pending.*

## Thesis

Correctness is a property of the system, not the model. AEDOS sits between a user and an LLM, extracts each factual claim the model makes into a structured form, and routes that claim through a tiered verification stack backed by a verified world model that grows monotonically more correct over the course of a conversation. The system performs bounded inference — equivalence, subsumption, and derivation — over its verified store, but does not attempt open-ended reasoning. Verification, not memoization, is the source of truth; memoization compounds verification's value over time.

## Principles

The architecture rests on seven principles. Every design decision below is downstream of these.

**1. Verification is upstream of memoization.** Nothing enters the verified store unless it cleared a verifier (Python execution, retrieval, or user authority on user-authoritative claim classes). A wrong oracle call, a wrong cache lookup, or a wrong canonicalization can cost a cache hit or produce a soft conversational misstep, but cannot admit a falsehood into the world model.

**2. Bounded-domain classification, not open-ended reasoning.** Every place AEDOS uses an LLM to make a semantic judgment, the LLM's task is reduced to a small label set with a fixed schema. Pattern selection (9 labels), routing (5 labels), predicate equivalence (3 labels + slot reversal kind), entity equivalence (2 labels), entity taxonomy (4 labels) with relation_type tagging, predicate distribution (4 labels) keyed on the (pattern, predicate, polarity, relation_type) tuple. Open-ended reasoning is delegated to the chat model, where it belongs.

**3. Frequentist confidence from independent external evidence only.** Every reusable artifact in the system — stored facts, cache entries, oracle rows — carries `affirmed_count` and `contradicted_count`. Reliability is a Beta-posterior over those counts. Counts increment only on independent external evidence: a user reasserting a fact, a verifier producing a fresh positive verdict, an operator-driven re-judgment of an oracle row. Cache hits, oracle-mediated equivalence resolutions, and subsumption-derived matches do not increment counts on the rows they consult or on the rows they resolve to. Reads are not writes. Inference is bookkeeping; only fresh evidence is signal. Without this discipline, the Beta posterior degrades into a usage counter and the empirical-reliability principle quietly fails.

**4. Bounded inference over a verified store.** AEDOS performs three and only three inference operations: equivalence (this claim is the same as a stored fact), subsumption (this claim follows from a stored fact about a broader entity, where the predicate distributes), and derivation (this claim follows from a chain of equivalence and subsumption steps over U and W). All three are performed by the four-oracle substrate, all three are memoized at the substrate level, and none of them persist new facts — derived results are ephemeral and re-walked on demand. Causation, modality, counterfactual reasoning, and commonsense reasoning are out of scope as inference operations.

**5. Disciplined abstention, sharpened to distinguish representation from reasoning.** The system distinguishes *representing* a class of claims from *reasoning over* that class. Causal claims, modal claims, and (some) counterfactual structures can be represented as propositions and routed to verification; AEDOS does not infer over their internal structure.

- *Causal claims* ("smoking causes cancer") are represented as `relational` rows with predicates like `causes`, routed to retrieval, and verified the same way any other proposition is verified. This is a Humean stance: the system commits only to whether the proposition (a claim about regular association) is well-supported, not to a model of causal mechanism. The system never derives effects from causes or propagates consequences through causal chains.
- *Modal claims with an attributed agent* ("the user thinks X must be true") are stored as `propositional_attitude` rows with the proposition opaque. The system does not reason over modal structure.
- *Aesthetic and evaluative claims* are non-propositional under expressivist semantics and are abstained on at extraction. They may be stored as `propositional_attitude` rows attributed to an agent (the user finds sunsets beautiful), but never enter W.
- *Bare counterfactuals* ("if Biden had run, he would have won") are abstained on at extraction. Possible-worlds claims have no truth-conditions in retrieval.

The boundary is not a limitation to be eventually removed. It is the architectural commitment that makes the system defensible.

**6. Auditability and revisability through a single event log.** Every stored fact, cache entry, and oracle row carries provenance, counts, and a reason. Every layer emits structured events to a single pipeline_events log, which is the authoritative source for the trace UI and for downstream auditing. Operators can dispute rows; downstream lookups propagate less confidence through disputed paths. Auditability is not a feature; it is a property the architecture guarantees by construction.

**7. Validate before classifying, route before reasoning.** Each layer that uses an LLM is preceded by a rule-based validation step. Layer 2's routing-anomaly check (claims that violate per-pattern subject-slot invariants are flagged before the LLM router runs) is the canonical case. Layer 4's tier precedence is another: U and W are consulted before fresh verification, and direct lookup before derivation. This discipline ensures cheap rule-based work catches structural problems before expensive LLM work, and ensures bookkeeping correctness regardless of LLM behavior.

## The five layers

AEDOS is organized as five layers with single-job interfaces between them.

**Layer 1 — Extraction.** A single LLM call per turn extracts structured claims from text. Each claim has a pattern, a predicate, a polarity, slots, and a provenance tag (`user` | `assistant`). The extractor abstains when no pattern fits. Output is the system's structural commitment to what was said.

**Layer 2 — Routing.** Two-step. First, a rule-based validation step: each claim is checked against per-pattern structural invariants (e.g. `preference` and `propositional_attitude` claims must have `agent` ∈ {user, me, i}; `event` claims must have non-empty `participants`; `mereological` claims must have `part != whole`; all required slots must be present and non-empty). Failed validation produces a `routing_anomaly` outcome that bypasses the LLM router entirely and stores the claim at `verification_status="routing_anomaly"`. Second, for valid claims, an LLM call classifies the claim along two axes: trust class (user-authoritative for self-attributes; verifiable otherwise) and verification path (Python, retrieval, store-only, or unverifiable). Routing classifications are memoized in a `routing_memo` table keyed by (pattern, predicate); after warm-up, this is mostly free. The (pattern, predicate) memoization key invariance is enforced by the discipline that the extractor uses distinct predicate labels for semantically distinct claim subtypes (e.g. `has_letter_count` vs `has_population` rather than overloading `has_count`).

**Layer 3 — The four-oracle semantic substrate.** Four memoized LLM classifiers form a 2×2 over (predicate, entity) × (equivalence, subsumption):

|              | Equivalence              | Subsumption              |
|--------------|--------------------------|--------------------------|
| **Predicate**| `predicate_equivalence`  | `predicate_distribution` |
| **Entity**   | `entity_equivalence`     | `entity_taxonomy`        |

The four oracles do not share a uniform shape. They fall into three documented patterns, all coordinated by a thin `classifier_base.py` shell:

- **Symmetric-pair pattern** (`predicate_equivalence`, `entity_equivalence`). Two-argument key, canonical lex ordering enforced at the SQL layer with `CHECK (a < b)`. Caller passes `(query, stored)` to `consult()`; the oracle handles canonical ordering internally and returns a verdict in the caller's frame. Self-pairs rejected at the SQL layer.

- **Directional-pair pattern** (`entity_taxonomy`). Two-argument key, no canonical swap — the column order is meaningful (the `child` column carries the more-specific entity by convention; the label vocabulary `{child_subsumed_by_parent, parent_subsumed_by_child, equivalent, neither}` disambiguates when the caller passes them in non-natural order). A caller asking the same pair in both directions writes two rows; the cost-correctness trade is bounded and explicit.

- **Singleton-key pattern** (`predicate_distribution`). Four-column key `(pattern, predicate, polarity, taxonomy_relation_type)` with no pairing and no canonical swap. The label `{distributes_up, distributes_down, both, neither}` is direction-encoded in the label itself. Pattern-keyed because predicates are pattern-scoped; polarity-keyed because distribution behavior can differ across polarities; relation-type-keyed because the same predicate can distribute differently under `is_a` vs `part_of` chains.

`predicate_equivalence` is pattern-keyed; `entity_equivalence` and `entity_taxonomy` are pattern-independent (entities have stable identity across patterns); `predicate_distribution` is pattern-keyed. Predicate strings are normalized by `strip().lower()`; entity strings are normalized by `strip()` only — case carries entity-disambiguation signal (`apple` ≠ `Apple`). Each oracle has a tiny label set, a schema, frequentist counts, and an audit trail. Each emits three pipeline events: `{oracle_name}_hit`, `{oracle_name}_write`, `{oracle_name}_classification_failed`, plus the shared `oracle_consulted` event. After warm-up, lookup cost approaches zero. The substrate is consulted by Layer 4. It is not a pipeline stage; it is a substrate.

**Layer 4 — Tiered lookup with derivation.** Two storage tiers, in precedence order:

- **Tier U (user microtheory)** — facts the user has asserted, scoped by user_id. Each row carries an `is_session_local` flag and a `session_ids` set. *Cross-session facts* (`is_session_local=false`) are global to the user; the session_ids set records which sessions reaffirmed them, drives the Beta-posterior reinforcement (one increment per new session, never on same-session repetition), and informs recency for tone. *Session-local facts* (`is_session_local=true`) — the "let's say for this conversation" hypotheticals — exist only within a single originating session; the session_ids set is enforced by SQL CHECK to be a single-element list. Both kinds live in the same table; the flag determines visibility.

- **Tier W (world cache)** — verification results for world facts, with TTL based on stability class.

Layer 4 attempts to resolve each claim in the following order:

1. **Direct lookup in U** with semantic matching via the oracles, filtered by session-locality. The lookup walks: SQL exact match on identity slots → if no candidates, consult `entity_equivalence` to find alias-identity candidates → for any candidates, literal predicate match → on miss, consult `predicate_equivalence` to find equivalent or contradictory predicates. The result is `MATCH` (under any combination of entity-alias, predicate-equivalence, polarity-flip), `CONTRADICTION`, or `MISS`.

2. **Direct lookup in W** with semantic matching via the oracles. Same shape as Tier U.

3. **Derivation walk** — combine facts from U and W via equivalence and subsumption chains. Example: claim "user lives in Massachusetts" doesn't match U or W directly, but U has `lives_in(user, Williamstown)`, `entity_taxonomy` has `Williamstown part_of Massachusetts`, and `predicate_distribution` says `lives_in` distributes up `part_of` chains. The claim is VERIFIED via derivation, with chain reliability propagated through every oracle row consulted. The walk is bounded by both a hard depth limit (`MAX_DEPTH = 4` hops) and a chain-reliability floor (`MIN_CHAIN_RELIABILITY = 0.4` — admits fresh oracle rows above the cold-start prior of 0.5 effective, but rejects chains with actively-contradicted rows). The floor is set low because derivation is cheap and a low floor lets new, unproven oracle rows still produce useful (advisory) verdicts that strengthen with use.

4. **Fresh verification** if all of the above miss or fall below threshold.

Derived results are *never persisted*. The system re-walks the chain on demand. This has two consequences: every derivation is always current (if U updates, the answer updates with no cache invalidation needed), and derivations cost a substrate walk per query (cheap after oracle warm-up: SQL lookups plus zero LLM calls when the rows are memoized).

Each step returns a verdict carrying its own confidence (Beta posterior over the matched row's counts), the chain reliability accumulated across oracle calls, and a `via` list ordered by consultation sequence (`[]` for pure literal match, `["entity_equivalence"]` for alias-only, `["entity_equivalence", "predicate_equivalence"]` for both-oracle paths, longer chains for derivation). Below threshold, fall through.

Derivation operates over the substrate with an optional active-classification budget (default 20 per walk). When the budget is zero (passive mode), the walker is purely read-only over substrate and skips cold cells. When nonzero, the walker may consult `predicate_distribution` on cold (pattern, predicate, polarity, relation_type) cells up to the budget per walk, populating the substrate as a side effect of the walk. Budget exhaustion produces graceful fall-through — branches that needed cold classification are skipped, and the walk completes on whatever paths the warm substrate supports. The cost contract is bounded by memoization: warm-cache walks pay no LLM cost regardless of budget; cold-start walks pay at most budget-many LLM calls. The same lookup-first convention applies to Tier U and Tier W stages 2 and 3 — when an LLM is provided, oracle cold-rows are classified on the fly (unbounded, since candidate sets are O(N facts under pattern)); when llm is None, those stages return lookup-first results without classification, no crashes.

**Layer 5 — Decision and response.** Takes the lookup verdict, the chain reliability, the trust class, and the verification status. Produces one of five interventions per claim: pass-through, replace, hedge, soften, or noop. Decision confidence is the three-factor product `path_prior × chain_reliability × evidence_strength` against a configurable threshold T (default 0.5); above T produces a hard verdict, below T produces a soft verdict. Operator-driven affirm/contradict endpoints on each oracle row are the only paths (alongside cold-start classification) that mutate the substrate's frequentist counts. Streams to UI; writes verifier output back to W as appropriate (Tier U writes are scoped to user-authoritative claim classes via Layer 2's routing decision, not Layer 5).

## How U and W interact

U and W are separate stores but not isolated. They interact in three modes:

**Consultation during routing.** Self-attribute claims route to U; world claims route to W. The two stores normally do not cross during direct lookup.

**Cross-tier contradiction detection.** When the user asserts a verifiable world claim, W is consulted to detect contradiction with established facts. The disagreement triggers a soft conversational intervention rather than overriding either store. Conversely, when the model asserts a fact about a topic the user has expressed beliefs on (stored in U as `propositional_attitude`), Layer 5 may consult U to inform tone — the user being wrong about a checkable fact does not change W's verdict, but the system can frame the correction with awareness of the user's belief.

**Query-time lifting via derivation.** The derivation walk in Layer 4 is the canonical place where U and W combine. A derivation from `lives_in(user, Williamstown) ∈ U` and `Williamstown part_of Massachusetts ∈ W` produces "user lives in Massachusetts" as an answer to a query, but persists it in neither tier. This corresponds to the standard McCarthy/Guha treatment of microtheories: facts can be lifted between contexts at query time, but the lifting is an inference operation, not a storage event.

There is no persisted derivation between U and W. The system never writes a derived fact into either tier. This discipline keeps the storage model clean and avoids the cache-invalidation cascade that would otherwise be required when source facts change.

## The nine patterns

Each claim extracts into one of nine patterns. Predicates within a pattern are free-form; the extractor invents specific labels (e.g. `is_obsessed_with` under `preference`) when the example list doesn't capture the relation precisely. The pattern set is closed; predicates are open. The extractor is held to the discipline that semantically distinct claim subtypes get distinct predicate labels — this is what makes the (pattern, predicate) routing memo key invariance hold.

| Pattern | Identity slots | Key examples |
|---|---|---|
| `role_assignment` | agent, role, org | holds_role, served_as |
| `preference` | agent, object | likes, dislikes, loves, hates |
| `quantitative` | subject, property | has_count, weighs, born_in_year |
| `spatial_temporal` | entity, location | lives_in, located_in, visited |
| `categorical` | entity, category | is_a, instance_of |
| `relational` | subject, object | married_to, founded_by, causes |
| `event` | event_type, occurred_at | won_election, was_inaugurated |
| `propositional_attitude` | agent, proposition | believes, knows, hopes |
| `mereological` | part, whole | part_of, member_of, composed_of |

`mereological` is a distinct pattern because parthood is not subsumption: inferences that distribute down `is_a` chains do not in general distribute down `part_of` chains, and conflating them produces systematic errors (Brachman 1983; Winston, Chaffin & Herrmann 1987). Keeping them separate at the pattern level lets `predicate_distribution` learn distinct policies cleanly. The substrate is unified — `entity_taxonomy` stores both `is_a` and `part_of` chains in a single table with a `relation_type` column. The mereological scope is constitutive parthood only; locational containment ("Tokyo is in Japan", "the engine is in the car") stays in `spatial_temporal`.

`relational` is the home for causal predicates (`causes`, `caused_by`, `enables`, `prevents`). These extract and route to retrieval like any other relational claim. The system stores the propositions but does not reason over the causal structure they describe.

Polarity is binary {0, 1}. Tense is captured in `valid_from`/`valid_until` for the store and as a derived cache-key dimension for the world cache.

## Verification status

Each stored fact and cache entry carries a verification status. The architecture commits to the following enumeration; the distinctions matter for downstream behavior and must not be collapsed.

| Status | Meaning | Layer 5 behavior |
|---|---|---|
| `verified` | Verifier ran and confirmed the claim. | Pass-through. |
| `contradicted` | Verifier ran and contradicted the claim. | Replace (correction). |
| `user_asserted` | User asserted a self-attribute claim; no verifier ran. | Pass-through. |
| `unverifiable_in_principle` | Routing identified the claim as unverifiable by design (e.g. preference, attitude). | Soften. |
| `retrieval_inconclusive` | Verifier ran; evidence insufficient for a verdict. | Hedge. |
| `retrieval_failed` | Verifier broke (network, parse error, etc.). No evidence about the claim. | Noop. |
| `unverifiable_pending_implementation` | Routing identified the claim as verifiable in principle but no verifier handles it yet. | Hedge with implementation flag. |
| `routing_anomaly` | Layer 2 validation failed; the claim has a structural problem. | Noop, flag for operator. |

The `retrieval_inconclusive` vs `retrieval_failed` distinction is load-bearing: hedging on verifier failure (treating absence of evidence as weak evidence of the claim) is a known failure mode that this enumeration prevents.

## What AEDOS does and does not do

**Does:**
- Extract structured claims from conversation.
- Validate claims against per-pattern structural invariants before routing.
- Route each valid claim to the cheapest adequate verifier.
- Verify against Python, against retrieval, or against user authority on user-authoritative claims.
- Store verified facts with provenance, polarity, temporal scope, and frequentist counts.
- Perform equivalence inference: recognize that two claims express the same fact under predicate or entity canonicalization.
- Perform subsumption inference: recognize that a claim follows from a stored fact about a broader entity, when the predicate distributes.
- Perform derivation: combine facts from U and W via equivalence and subsumption chains to answer queries that match no stored fact directly.
- Represent causal propositions and route them to retrieval.
- Cascade-invalidate semantically adjacent stored facts when a contradiction arises (deferred to v0.15).
- Populate the substrate lazily through ordinary use, with a per-walk `predicate_distribution` classification budget that bounds cold-start cost on derivation walks. Tier U / Tier W stages tolerate cold cells under `llm=None` by returning lookup-first results without crashing.
- Emit structured events from every layer to a single pipeline_events log.
- Surface its reasoning, its confidence, and its uncertainty to the operator through a trace UI with per-oracle row inspectors.

**Does not:**
- Reason over modal structure beyond what `propositional_attitude` records as opaque belief content.
- Reason over causal structure. Causal propositions are stored and verified; effects are not derived from causes.
- Reason counterfactually. Bare counterfactuals are abstained at extraction.
- Verify aesthetic, evaluative, or expressive content as world facts. These can be stored as user attitudes; they cannot enter W.
- Persist derived facts. Derivation is a query operation; storage is reserved for independently-evidenced facts.
- Increment counts on cache hits, oracle-mediated resolutions, or subsumption-derived matches. Inference is bookkeeping; only fresh evidence reinforces.
- Mutate substrate rows from any path other than oracle classification (cold-start writes during walks/lookups) and operator-driven affirm/contradict endpoints. Lookup hits never increment counts; only independent external evidence does.
- Collapse the 8-state verification status enumeration. Each state encodes a distinct downstream behavior.
- Lowercase entity strings during canonicalization. Case is semantic for entities; it is presentational for predicates.
- Perform open-ended commonsense reasoning. The chat model handles this; AEDOS does not.

## Confidence

Three sources combine into a single Decision confidence:

`path_prior` × `chain_reliability` × `evidence_strength`

`path_prior` is the routing layer's prior on the verifier (Python ≈ 0.99; retrieval ≈ 0.85; user-authoritative ≈ 1.0 for the right claim classes; store lookup inherits from the matched fact).

`chain_reliability` is the minimum reliability across all oracle rows consulted in the lookup or derivation, where each row's reliability is a Beta posterior over (`affirmed_count`, `contradicted_count`) with a uniform prior. A fresh row has reliability ~0.5 and produces an advisory verdict until it earns trust through use. Min-link is conservative and fails gracefully on cold-start rows. For derivation walks, every oracle row consulted contributes; the longer the chain, the lower the floor.

`evidence_strength` inherits from the verifier; for retrieval it reflects passage support, for Python it is 1.0 on success, for store hits it is 1.0 (the fact is verified by definition of being in U or W).

A single threshold T (default 0.5, configurable via `AEDOS_DECISION_THRESHOLD`), applied to Decision confidence, controls how aggressive the system is. Above T: hard verdict. Below T: soft verdict (hedge, advisory, fall through to next tier or to fresh verification).

## Substrate calibration as of v0.14-phase-5

The architectural commitment that bounded LLM classifications compose into a reliable system rests on per-oracle calibration evidence. As of phase 5, the four oracles calibrate against hand-labeled corpora as follows:

| Oracle | Aggregate | Floor | Worst category |
|---|---|---|---|
| `predicate_equivalence` | 0.967 | 0.90 | antonym_polarity_flip 0.867 |
| `entity_equivalence` | 0.978 | 0.85 | hard_cases (exempt) and person_vs_place 0.800 |
| `entity_taxonomy` | 0.966 | 0.85 | over_subsumption_tempting 0.750 |
| `predicate_distribution` | 0.977 | 0.85 | polarity_sensitive 0.875 |

Every non-exempt category cleared its per-category floor on first calibration runs without prompt iteration. Misclassifications cluster around genuinely-hard semantic territory (botanical-vs-culinary tomato, ambiguous Lincoln referent, conservative-bias on soft over-merge cases) rather than systematic failure modes. Calibration is reread before any oracle's prompt is altered.

## Operational guarantees

The architecture is evaluated by two standards:

**Operational closure.** Does the representation support the operations the system performs (extraction, validation, routing, verification, equivalence inference, subsumption inference, derivation, cascade invalidation, event emission)? Every claim that enters the system must be representable; every pair of representations the oracles judge equivalent must in fact be equivalent under the slot-mapping; every subsumption must follow the predicate distribution policy; every derivation must compose correctly across the oracle chain; every layer must emit traceable events.

**Abstention discipline.** Does the system know what it doesn't represent and refuse rather than confabulate? Out-of-scope content (bare counterfactuals, aesthetic claims, modal-without-attribution) is abstained on at extraction. Structurally invalid content (e.g. preference claims with non-user agents) is flagged at Layer 2 validation. In-scope content that exceeds the system's reasoning capabilities (causal mechanism, modal structure, counterfactual evaluation) is represented as proposition-level claims and verified accordingly, without committing the system to inference over the structures those propositions describe.

These are the standards. Empirical accuracy on any specific claim distribution is downstream of them.

## What persists, what doesn't

This document specifies the architecture that persists. The implementation will continue to evolve — verifier backends will change, the routing-oracle prompt will be tuned, the trace UI will gain features. But the seven principles, the five layers, the four oracles in their three documented shapes, the two storage tiers (U and W) with session-local and cross-session distinction, the eight-state verification status enumeration, the discipline that derivation does not persist, the discipline that only independent external evidence increments counts, the principle of verification-upstream-of-memoization, and the disciplined abstention boundary do not change. They are the system.