# Aedos v0.15 — Architecture Document (Draft 2)

*This document is the spec v0.15 is built to. It is for the project's authors and for the Claude Code instances implementing phases against it. The paper is downstream of this document; the chat-wrapper deployment is one configuration of what this document describes.*

*Draft 2 supersedes Draft 1. Major changes from Draft 1: the 9-pattern claim taxonomy collapses to a single binary-relational pattern with predicate-level metadata; operator review is removed as an architectural mechanism (autonomous correctness mechanisms replace it); the consistency-check policy is specified as retract-both-with-circuit-breaker; seeds are explicitly optional; the Python cross-check is dropped entirely; walker resource budgets are added.*

---

## 1. What v0.15 is

Aedos is a claim-verification engine for natural-language text. Given a piece of text and a verification context, Aedos extracts the factual claims the text makes, routes each claim to one of three typed sources of belief — context-stipulated premises (Tier U), a curated structured knowledge base (the KB, via a small protocol), or deterministic computation (Python) — and returns a per-claim verdict with a complete justification trace. Claims that cannot be grounded in any of the three sources are abstained on. The system commits to soundness: every verified verdict is traceable to its premises; false abstains are accepted as the price of refusing false verifieds.

Claim structure is universal: every claim is a binary relation (subject, predicate, object), with optional temporal scope (`valid_from`, `valid_until`) attached as claim metadata. The semantic distinctions that earlier versions of the architecture handled via a multi-pattern decomposition (preference, propositional_attitude, spatial_temporal, categorical, role_assignment, relational, quantitative, event, mereological) are now carried by predicate metadata — every predicate carries its own type, validation invariants, routing hints, and KB mapping. Multi-participant claims (events with several participants) decompose into multiple binary claims linked by a reified event identifier.

Aedos is a truth-maintenance system in the Doyle/Kleer lineage, with specific design choices for the natural-language verification setting: three-typed premises rather than uniform assumptions, lazy derivation rather than eager propagation, automatic contradiction-tracing and retraction-propagation rather than human-mediated revision, bounded inference (equivalence and subsumption, gated by predicate distribution) rather than general logical inference. The KB is a parameter, not a commitment; v0.15 ships with Wikidata as the reference implementation of the KB protocol, but the architecture is defined by the protocol and Wikidata is one instance of it. The chat-wrapper deployment, in which Aedos sits between a chat LLM and the KB and intervenes on the LLM's response, is one configuration of the verification engine — the architecture also accommodates document verification, generated-content verification, and other applications where natural-language text is checked against grounded premises.

The system operates autonomously. There is no operator-in-the-loop; no human review gates any decision; correctness mechanisms (consistency checking, downstream contradiction tracing, retraction propagation) all run without human intervention.

---

## 2. What changed from v0.14

v0.14 was an architectural rebuild on a four-oracle substrate (predicate equivalence, entity equivalence, entity taxonomy, predicate distribution), with two storage tiers (Tier U for user microtheory, Tier W for verified world facts), a retrieval verifier using Wikipedia search, an eight-state verification status enum, nine claim patterns, and confidence numbers (frequentist Beta posteriors over operator-audit counts) gating decisions via chain-reliability thresholds. v0.15 makes the following changes.

**Wikidata replaces Tier W.** The world cache is no longer an internal Aedos table populated by the system's own verifications. It is Wikidata, queried via the KB protocol. Aedos does not curate world knowledge; it consults curated world knowledge. There is no override layer, no snapshotting tier, no Aedos-asserted world-fact storage.

**The retrieval verifier is deleted.** No claim is grounded by web search. The three authorized sources of belief are exhaustive: Tier U, the KB, Python. Claims that fall outside all three abstain.

**The 9-pattern claim taxonomy collapses to one pattern.** All claims are binary-relational: (subject, predicate, object). Pattern-level metadata (USER_SUBJECT_PATTERNS, taxonomy_relevant_slots, distinct_slots, default_routing_method) moves to the predicate. Multi-participant claims decompose into multiple binary claims with a reified event identifier. Typed value verification (formerly the quantitative pattern) becomes a property of predicates whose object slot type is `quantity`. Temporal scope (formerly the spatial_temporal pattern's time slot) becomes universal claim metadata.

**The substrate is one resolver and three oracles.** Entity equivalence becomes the entity resolver (per-mention disambiguation against the KB, returning ranked candidates rather than yes/no verdicts). Predicate equivalence becomes predicate translation (now cross-namespace, mapping Aedos predicates to KB properties with slot-to-qualifier correspondence and carrying predicate metadata). Entity taxonomy becomes subsumption resolution (mostly KB-native, with substrate rows for meta-judgments the KB doesn't encode). Predicate distribution survives unchanged in role.

**No confidence numbers in decision logic.** The frequentist Beta posteriors and chain-reliability floors are gone. Substrate rows exist or they don't; if they exist, they are usable; if they are shown wrong, they are retracted. A single `used_count` per row tracks how often the row has been consulted (observability metadata only; not a decision input).

**No operator-in-the-loop.** Affirmed/contradicted counts and operator-affirm endpoints are removed. Correctness is preserved by automatic mechanisms: substrate-internal consistency checks (with retract-both-and-regenerate resolution and a circuit breaker), downstream contradiction tracing through justification graphs, and retraction propagation. Audit log remains for observability and post-hoc analysis but is not architecturally required.

**The KB protocol is formalized.** Three operations — `resolve_entity`, `lookup_statements`, `subsumption` — abstracted over any structured knowledge base meeting the protocol's requirements. Wikidata is v0.15's reference implementation; other implementations are valid targets.

**The Python cross-check is dropped.** v0.14's roadmap planned a two-code-generation cross-check for v0.16. v0.15 commits to single-generation Python verification. Errors in Python verification are caught by downstream contradiction tracing.

**Aedos is framed as a general claim-verification engine.** The pipeline takes (text, context) and produces a structured verification result. The chat-wrapper deployment is one configuration; the architecture is the engine.

**Walker resource budgets are explicit.** Each verification has a per-claim wall-clock budget and LLM-call budget; exceedance produces an abstention with a budget-exceeded marker in the trace.

---

## 3. Principles

The eight commitments that the rest of the document fills in. Each is load-bearing; together they are the contract.

### 3.1. Three typed sources of belief, abstain otherwise

Aedos believes a claim if and only if it can be grounded in one of three sources: a context-stipulated premise stored in Tier U, a statement returned by the KB protocol, or a deterministic computation. Claims that cannot be grounded in any of these abstain. There is no fourth source. There is no web retrieval, no LLM self-assessment, no reasoning from training-data priors. The system's honesty story is that every verified claim has a citable source, and every abstention is a refusal to manufacture one.

This principle rules out a class of failure mode: confident assertion absent grounding. It imposes a real cost: the system abstains on a wider class of inputs than systems allowing LLM priors or web retrieval. The cost is intentional. In domains where the cost of a false verification exceeds the cost of an abstention — medical, legal, scientific, regulatory, enterprise factual chat — the trade is correct.

### 3.2. Soundness over completeness

Every verified verdict carries a justification trace traceable to its premises. False abstains are accepted as the price of refusing false verifieds. The system commits to never returning *verified* for a claim it cannot justify; it does not commit to always returning *verified* for a claim that is true.

Three failure modes produce false abstains: resolution failure (the entity exists in the KB but the resolver did not find it under the available context), translation failure (the predicate is expressible in the KB but the translation oracle does not yet have a row for it and inline generation did not produce a confident verdict), and walk-depth-or-budget failure (a valid derivation chain exists at depth or LLM-call count beyond what the walker explored). Each is recoverable — better resolution, more translation rows, larger budgets — but none cause the system to lie.

This principle has operational consequences. The chat-wrapper deployment's intervention set does not include "hedge" or "soft assertion"; the moves are pass-through, abstain, correct, decline. There is no "I think but I'm not sure." The confidence-numbers-in-decision-logic mechanism of v0.14 is replaced by categorical existence-or-absence of substrate rows: the system either has a usable row or it does not.

### 3.3. Bounded inference

Aedos performs equivalence and subsumption over typed premises, gated by predicate distribution, with cycle detection and polarity tracking. It does not perform further logical inference. It does not do modal reasoning, counterfactual reasoning, causal inference (unless the KB explicitly encodes causation), planning, or unbounded chaining beyond the configured depth.

The choice is deliberate and inherits from Cyc's microtheory tradition. Most neuro-symbolic systems fail because they attempt unbounded inference and either explode in complexity or produce results no one can audit. The bounded scope makes Aedos's reasoning checkable: every derivation step is one of three kinds (equivalence substitution, subsumption traversal with distribution gate, premise lookup), and every step produces a traceable justification.

### 3.4. Three premise types are structurally co-equal

Tier U (context-stipulated premises), the KB (curated world premises), and Python (computed premises) are the three sources of grounded belief. None is auxiliary; each handles a category of fact the others structurally cannot.

Tier U holds what the asserting party (the chat user, the document's stated premises, the deployment configuration) has stipulated as ground truth for this verification context. The KB cannot store this. Python cannot compute it. Tier U is the only source.

The KB holds what curators have collectively asserted about the world. Tier U cannot duplicate this scope; Python cannot compute encyclopedic facts. The KB is the only source.

Python computes what is deterministically derivable from typed inputs. Tier U cannot store every possible computation; the KB does not perform arithmetic. Python is the only source.

The substrate's job is to make these three sources interoperable: to resolve entities consistently across them, to translate predicates between them, to compose claims across them in derivation walks.

### 3.5. The substrate is the translation layer

The substrate exists to translate between the language a claim is made in (Aedos's predicate vocabulary plus the natural-language entity references in slot values) and the languages of the premise sources. Resolution, translation, subsumption-judgment, and distribution-judgment are all instances of this translation task.

The substrate's contribution is *connective*. Each oracle and the resolver answer a specific translation question; the derivation walker composes their answers into a justification chain that crosses sources. Naive tool-use, in which an LLM calls SPARQL directly and reasons over the results in its forward pass, lacks this connective layer — it cannot compose across sources with the traceability the substrate provides.

### 3.6. Every belief traces to a justification graph; retraction propagates autonomously

Every verified verdict carries a complete justification trace: which Tier U rows contributed, which KB statements were consulted, which Python executions produced which intermediate values, which substrate rows were used at which composition steps. The trace is sufficient for re-derivation and for audit.

Retraction is the operation that removes a substrate row, a Tier U row, or a cached KB lookup from the system's belief set. Retraction propagates through the justification graph: every verdict whose trace includes the retracted item is itself retracted, and may be re-derived from remaining premises if those would still support it.

Retraction sources are automatic: substrate-internal consistency checks detect inconsistent rows and retract them under the circuit-breaker policy; downstream contradiction tracing identifies rows whose dependent verdicts are contradicted by later premises and retracts them; deployment-injected external corrections (when the deployment surfaces a user-level contradiction) feed back into the same propagation mechanism. The architectural commitment is that no human is in the verification loop; no row's retraction requires manual review.

### 3.7. Claim structure is universally binary-relational

All claims are of the form (subject, predicate, object), with polarity, optional temporal scope (`valid_from`, `valid_until`), and source provenance. The semantic distinctions that previous versions of the architecture made via multiple pattern types are carried by predicate metadata — every predicate carries its object type, validation invariants, routing hints, and KB mapping. Multi-participant claims (events with several participants, or claims with structured side information) decompose into multiple binary claims linked by reified entity identifiers.

This is the architectural commitment that simplifies the pipeline: extraction produces relational claims; routing decides per-predicate; the substrate translates predicates regardless of "type"; the walker walks a uniform graph of relational claims. The complexity that pattern decomposition previously carried moves to the predicate metadata, where it lives alongside the predicate translation oracle's KB-mapping work.

### 3.8. The KB is a parameter, not a commitment

Aedos is defined by the KB protocol — `resolve_entity`, `lookup_statements`, `subsumption` — abstracted over any structured knowledge base meeting the protocol's requirements. Wikidata is v0.15's reference implementation. The architecture does not commit to Wikidata; future deployments may implement the protocol over domain-specific KBs.

The paper's contribution is the protocol and the architecture above it. The Wikidata implementation demonstrates the protocol on a broad-coverage curated KB. Subsequent demonstrations on domain-specific KBs strengthen the architectural claim without requiring architectural change.

---

## 4. The system, end to end

The five layers, each with inputs, outputs, responsibilities, and explicit non-responsibilities. A sixth, optional layer is the deployment-specific consumer of Layer 5's result.

### 4.1. Layer 1 — extraction

**Input.** A tuple `(text, context)`. `text` is natural-language text to be checked. `context` is a structured object carrying whatever information surrounds the text — for the chat-wrapper case, the current turn id, the prior conversation, and the asserting party (the user); for document verification, the document identifier, the document's stated premises, and the asserting party (the author); for generated-content verification, the generation configuration, the generation context, and the asserting party (the deployment). The context object's shape is configured per deployment; Layer 1 reads only the fields it needs.

**Output.** A list of structured claims. Each claim has:

- `subject` — an entity reference (natural-language slot value plus, if resolved, a cached KB identifier).
- `predicate` — a normalized canonical predicate name (snake_case, tense-neutral, voice-neutral).
- `object` — a typed slot value; the type is constrained by the predicate's metadata (entity reference, quantity, time, proposition, entity list).
- `polarity` — asserted or negated.
- `valid_from`, `valid_until` — optional temporal scope.
- `source_text` — the verbatim assertion span.
- `source_provenance` — the fields of `context` identifying the asserting party and location.

**Predicate normalization at extraction.** The extractor produces predicates in a canonical form. "Asa lives in Williamstown" and "Asa is located in Williamstown" both extract to `relational(Asa, lives_in, Williamstown)` (or both to `located_in`, whichever the extractor's canonical map produces — consistency across the corpus is what matters). Semantically equivalent predicates that the extractor fails to unify are linked downstream by the predicate translation oracle (both map to the same KB property). Normalization at extraction reduces the substrate's translation burden.

**Responsibilities.**
- Identify claims in the text using LLM-mediated structured extraction.
- Normalize predicates to canonical form.
- Decompose multi-participant claims and events into multiple binary claims with a reified event identifier (see 4.1.1).
- Extract temporal scope when stated (explicit dates, durations) and when inferable from tense (see 4.1.2).
- Enforce hard-claim discipline: do not fabricate claims about entities merely mentioned in `text`'s surrounding context; only extract what `text` itself asserts.
- Apply universal first-person canonicalization: any first-person reference in `text` resolves to the asserting party (per `context`).
- Enforce source-text discipline: the `source_text` field is the verbatim assertion span, not a paraphrase.
- Apply verifiability triage to determine which claims are worth routing onward (claims that pass triage are sent to Layer 2; claims that fail triage are recorded in the audit log and dropped as inert prose).
- Handle contrastive corrections (e.g., "Actually, X, not Y") by extracting both polarities in parallel.

**What Layer 1 does not do.**
- Does not assume a chat context. Reads only the fields of `context` it needs; for document verification, lacks turn ids and does not require them.
- Does not classify claims by route; that is Layer 2's job.
- Does not consult the substrate, the KB, or Python.
- Does not fabricate asserting parties; reads from `context`.

#### 4.1.1. Multi-participant decomposition

A claim like "Asa and Mike co-founded Acme in 2020" decomposes at extraction into a set of binary claims linked by a reified event entity:

- `relational(event_<id>, has_participant, Asa)`
- `relational(event_<id>, has_participant, Mike)`
- `relational(event_<id>, event_type, company_founding)`
- `relational(event_<id>, target, Acme)`
- `relational(event_<id>, occurred_in, 2020)`

The `event_<id>` is a fresh identifier generated for this extraction. Subsequent mentions of the same event in the same conversation produce independent identifiers; if linkage matters downstream, the entity resolver may link reified events across mentions when the extractor's signal is strong enough (matching participants, matching event_type, matching time), but the architecture does not commit to deterministic event re-identification across mentions. The cost is occasional duplication; the benefit is simpler extraction logic and avoiding the failure mode where an incorrect deterministic-hash collides two distinct events.

#### 4.1.2. Temporal scope handling

Temporal scope is universal claim metadata. Three cases:

- **Explicit scope.** "Obama was President from 2008 to 2016." Extract `valid_from=2008-01-20`, `valid_until=2017-01-20` (resolved to specific dates when contextually clear, otherwise to year boundaries). The claim's predicate is tense-neutral: `holds_role`.
- **Implicit past tense without dates.** "Obama was President." Extract with `valid_until=before_present`, a sentinel value meaning "the claim's validity ended at some unspecified point before the current verification time." The claim is not currently in force; it may appear in derivation chains as a historical premise (e.g., to ground a claim about Obama's past role) but cannot ground a present-tense claim like "Obama is President."
- **No temporal markers.** "Williams College is in Massachusetts." Unscoped; `valid_from` and `valid_until` are both null. Treated as currently valid.

**Relative temporal scope.** Claims of the form "X was Y when Z was W" — where one claim's scope is anchored to another claim's scope rather than to absolute time — are extracted with a `valid_during_ref` field on each claim, pointing to the other claim's identifier. At verification time, the system resolves the reference: if absolute times for the referenced claim's scope are known (from Tier U, the KB, or Python computation), the system substitutes them; if not, the system can still verify *the relative claim* against premises with matching relative scope (a Tier U row or KB statement asserting the same relative timing). The walker treats relative-scope references as a special edge type.

**Future tense.** "Asa will be President." Rejected as out-of-scope. Aedos does not verify predictive claims.

### 4.2. Layer 2 — routing

**Input.** A claim from Layer 1.

**Output.** A routing decision selecting one of four routes (or a routing anomaly indicating the claim is structurally invalid).

**The four routes.**

- **User-authoritative** (more precisely, *context-authoritative* in the general framing). The claim's predicate has `routing_hint = user_authoritative` in its predicate metadata. The asserting party is by definition the ground truth for these predicates. The claim is checked against Tier U: if Tier U has a matching prior assertion, the claim is verified or contradicted accordingly; if Tier U has nothing, the claim is *stored* in Tier U and treated as verified. User-authoritative claims never go to the KB or to Python.

- **Python.** The claim's predicate has `routing_hint = python`. Reducible to deterministic computation over typed values: arithmetic, date and time arithmetic, string operations, structured comparisons, list/set operations. Python verification produces a justification consisting of the code that was run, the inputs, the output.

- **KB-resolvable.** The claim's predicate has `routing_hint = kb_resolvable`. Entity slots can be resolved to KB identifiers, and the predicate has a translation row mapping it to a KB property with slot-to-qualifier correspondence. KB-resolvable claims may also enter derivation walks composing KB statements with Tier U premises and Python results.

- **Abstain.** The claim's predicate has no useful routing hint, or routing-determined-late returns no usable route (e.g., the KB-resolvable route was attempted but the predicate translation oracle could not produce a confident row). The deployment configuration determines what happens to the original text.

**Routing decisions are predicate-driven.** The router consults the predicate's metadata in the predicate translation oracle (if a row exists for the predicate). If no row exists, the router triggers inline predicate metadata generation (an LLM call producing the metadata fields and, when applicable, the KB mapping). The generated metadata is stored in the predicate translation row. Once the metadata exists, the routing decision is deterministic.

**Routing anomalies.** Structural invariants are checked before the route is finalized. The invariants are predicate-level metadata fields:

- `user_subject_required = true` predicates require the subject slot to canonicalize to the asserting party. Violation → routing anomaly.
- `distinct_slots` predicates require specified slot pairs to differ (e.g., `subject != object` for part_of, contains, located_in). Violation → routing anomaly.
- Claims with object slot type mismatched against the predicate's declared object type (e.g., predicate requires `quantity` but slot holds a string) → routing anomaly.

A routing anomaly indicates an extraction error, not an Aedos verification failure. The claim is not processed; an audit-log entry is created; the deployment is informed.

> **Implementation note (v0.16.1).** Layer 2 is no longer a standalone module (the former `layer2_routing/` `Router`/`Validator` were deleted). Routing is predicate-driven directly off the predicate translation oracle's `routing_hint` (consulted by the walker via `_predicate_routing`), and the three structural-invariant checks are now enforced in the live path: `user_subject_required` is a **fail-closed walk-entry guard** in the walker (a user_subject_required predicate asserted about a subject that is neither the asserting party nor a stipulated user persona short-circuits to an abstain before any source lookup — it can only ever abstain, never produce a verdict); `distinct_slots` (subject == object) is **superseded** by the extractor's `self_referential` abstention reason (stamped at extraction, short-circuited pre-lookup); and the object-type check is **superseded** by the kb_verifier value-type gate (`_object_satisfies_value_type`), which fails open (abstains on a type mismatch, never false-contradicts). A persona-subject claim (subject is a stipulated `user identity` Tier U row) routes `user_authoritative` so the KB is structurally unreachable and the entity resolver can never misresolve the persona name and false-contradict.

**Responsibilities.**
- Look up or trigger generation of the predicate's metadata.
- Validate structural invariants.
- Select the route.

**What Layer 2 does not do.**
- Does not execute verification; selects the route.
- Does not consult the KB or run Python; determines whether those routes are available.
- Has no `retrieval` route; web search is not a source of belief.

### 4.3. Layer 3 — the substrate

Layer 3 is the entity resolver, the three oracles, and the substrate-internal consistency checker. Detailed in Section 5. Layer 2 and Layer 4 consult Layer 3 throughout their operation.

### 4.4. Layer 4 — sources and derivation

**Input.** A claim and its route from Layer 2.

**Output.** A premise-grounded verdict: verified, contradicted, or no-grounding-found.

**The three sources.** Tier U, the KB (via the KB protocol), Python.

**The derivation walker.** Composes premises across the three sources via substrate operations. Walks BFS at default depth 4 over the composite graph. Emits a complete justification trace. Detailed in Section 6.4.

**Responsibilities.**
- Direct lookup against Tier U or the KB per the route.
- Derivation walk over composite sources when direct lookup is non-terminal or fails.
- Justification-trace emission.
- Polarity tracking, cycle detection, predicate-distribution gating.
- Inline substrate-row generation when a needed row does not exist.
- Resource-budget enforcement (wall-clock and LLM-call ceilings per claim).

**What Layer 4 does not do.**
- Does not perform interventions on text; produces verdicts and traces.
- Does not assume a chat context.
- Does not write substrate rows for the purpose of remembering verdicts.

### 4.5. Layer 5 — verification result

**Input.** The collection of per-claim verdicts from Layer 4.

**Output.** A structured verification result object: per-claim verdicts, per-claim justification traces, aggregate metadata, source-of-belief breakdown. Detailed in Section 7.

**Responsibilities.**
- Aggregate per-claim verdicts into a single result for the text.
- Ensure every verdict has a complete justification trace.
- Compute aggregate metadata.
- Provide the structured object to the deployment.

**What Layer 5 does not do.**
- Does not perform text interventions.
- Does not surface user-facing language.

### 4.6. Deployment layer (optional)

The deployment layer turns the verification result into the deployment's output. The chat-wrapper deployment consumes the verification result and produces interventions on the LLM's response. The deployment layer is not part of the architecture; it is the architecture's interface to applications.

**Chat-wrapper intervention model.** Four moves, categorical, no hedging:

- **Pass-through.** Response contains only verified or out-of-scope content. Shown unmodified.
- **Abstain.** Some claims could not be grounded. Deployment removes or annotates them per policy. No "I think but I'm not sure."
- **Correct.** Some claims are contradicted by the substrate. Deployment rewrites with citation to the justification trace.
- **Decline.** Response is dominated by ungrounded/contradicted content. Deployment refuses to answer.

---

## 5. The substrate, in detail

The substrate is the entity resolver, the three oracles, and the substrate-internal consistency checker. All four components share one architectural pattern: rows that exist are usable; retraction is the only state change that affects usability; a single `used_count` per row tracks observability metadata only.

### 5.1. The entity resolver

**Operation signature.** `resolve_entity(reference, local_context) → ranked_candidates_with_provenance`.

- `reference` is the natural-language entity reference (a slot value from a claim, a Tier U row's entity reference, or an entity-shaped string in a derivation step).
- `local_context` is the immediate surroundings: the claim being resolved (predicate, slot position, asserting party), and the position in the verification.
- Returns a ranked list of candidates. Each candidate is `(kb_identifier, provenance)` where provenance records why this candidate was ranked where (entity-type filter applied, prior resolution in this verification context, name match strength, KB search-API ranking).

**Per-mention disambiguation.** The resolver is called per mention. "I went to Paris with Paris" produces two resolver calls, each disambiguated from local context.

**The result cache.** The KB adapter may maintain a result cache keyed on (reference, local_context_signature). Invisible at the protocol level. Cached resolutions are subject to retraction: if downstream contradiction tracing identifies a cached resolution as wrong, the cache entry is invalidated and any verdict depending on it is retracted.

**Tier U entity references.** When Tier U stores a claim, entity slots store the natural-language reference plus, if available, a cached KB identifier from prior resolution. Composition of Tier U facts with KB statements goes through the resolver to obtain the identifier; cached resolutions are checked first.

**What the resolver is not.** Not an oracle in the categorical-verdict sense. Returns ranked candidates; selection happens at the call site. No row-level usage statistics other than result-cache `used_count`.

### 5.2. The three oracles

The three oracles:

- **Predicate translation.** Given an Aedos predicate, what is the predicate's metadata (object type, validation invariants, routing hint, KB mapping when applicable)? This oracle now does the work that pattern-level metadata did in v0.14, plus the KB-mapping work that v0.14's predicate equivalence did.
- **Subsumption resolution.** Given two entities and a relation type, do they stand in the subsumption relation? Most subsumption queries resolve directly through the KB protocol's `subsumption` operation; substrate rows exist for meta-judgments the KB does not encode.
- **Predicate distribution.** Given a predicate, does it distribute up or down a subsumption relation? The Aedos-mediated meta-knowledge oracle — necessary for the predicates Aedos uses that have no KB analog (preference, propositional_attitude, most relational predicates).

Each oracle has a row schema, a row-creation path, a retraction mechanism, and a consistency-check participation.

**Row schema — predicate translation.**

```
predicate_translation (
  id INTEGER PRIMARY KEY,
  aedos_predicate TEXT NOT NULL,            -- canonical predicate name
  object_type TEXT NOT NULL,                -- entity | quantity | time | proposition | entity_list
  user_subject_required INTEGER DEFAULT 0,  -- 1 if subject must be asserting party
  distinct_slots TEXT,                      -- JSON list of slot pairs that must differ
  routing_hint TEXT NOT NULL,               -- user_authoritative | python | kb_resolvable | abstain
  kb_namespace TEXT,                        -- nullable; e.g., "wikidata"
  kb_property TEXT,                         -- nullable; e.g., "P39"
  slot_to_qualifier TEXT,                   -- nullable; JSON; subject/object keys govern KB lookup direction
  single_valued INTEGER NOT NULL DEFAULT 0, -- 1 = functional / single-valued, 0 = multi-valued
  reason TEXT NOT NULL,                     -- LLM-generated justification for metadata + mapping
  created_at TEXT NOT NULL,
  last_consulted_at TEXT,                   -- observability
  used_count INTEGER DEFAULT 0,             -- observability; no decision use
  retracted_at TEXT,                        -- null when usable; non-null after retraction
  retraction_reason TEXT,
  UNIQUE(aedos_predicate, kb_namespace)
)
```

The predicate translation row carries every piece of metadata the system needs to route, validate, and translate the predicate. Inline generation produces all fields in a single LLM call when a predicate is first encountered. Predicates with no KB mapping leave the KB fields null.

**`single_valued`.** The `single_valued` field marks whether the predicate is functional — whether a subject has at most one value for it. `1` means single-valued (e.g., `born_in`, `head_of_state`): a KB value that differs from the claimed value *contradicts* the claim. `0` means multi-valued (e.g., `holds_role`, `received_award`): the KB simply holds *other* values and the claimed value may also be true, so a value mismatch yields `no_match`, not `contradicted`. The default is `0`, which is conservative by design: a wrong `1` produces false *contradictions*, whereas a wrong `0` produces only false *abstains* — the accepted §3.2 cost. The seed-file format (§9.2) carries `single_valued` as a per-entry field. The per-predicate functional/multi-valued classification for the reference seed pack — including borderline cases such as `country_of` and `mother_of`, which are multi-valued despite a superficially functional reading — is seed-pack content, not architecture; see `docs/v0.15_build_log/fixup2_report.md` for the rationale.

**`slot_to_qualifier` and lookup direction.** When the predicate is KB-mappable, `slot_to_qualifier` is a JSON map whose `subject` and `object` keys indicate which Aedos slot corresponds to the KB statement's subject and which to the KB statement's value. For most predicates the Aedos subject maps to the KB statement subject; for inverse predicates (e.g., `capital_of` against P36, where the KB stores the statement on the country, not the city) the Aedos subject maps to the KB statement *value* and the Aedos object maps to the KB statement subject. The KB lookup is keyed accordingly — see §6.2.

**Row schema — subsumption resolution.**

```
subsumption (
  id INTEGER PRIMARY KEY,
  entity_a_namespace TEXT NOT NULL,         -- aedos | wikidata | other_kb
  entity_a_identifier TEXT NOT NULL,
  entity_b_namespace TEXT NOT NULL,
  entity_b_identifier TEXT NOT NULL,
  relation_type TEXT NOT NULL,              -- is_a | part_of
  verdict TEXT NOT NULL,                    -- a_subsumed_by_b | b_subsumed_by_a | equivalent | unrelated
  source TEXT NOT NULL,                     -- the KB property that established this, or "llm_generated"
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_consulted_at TEXT,
  used_count INTEGER DEFAULT 0,
  retracted_at TEXT,
  retraction_reason TEXT,
  UNIQUE(entity_a_namespace, entity_a_identifier, entity_b_namespace, entity_b_identifier, relation_type)
)
```

**Row schema — predicate distribution.**

```
predicate_distribution (
  id INTEGER PRIMARY KEY,
  aedos_predicate TEXT NOT NULL,
  polarity INTEGER NOT NULL CHECK(polarity IN (0, 1)),
  relation_type TEXT NOT NULL,              -- is_a | part_of
  verdict TEXT NOT NULL,                    -- distributes_up | distributes_down | both | neither
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_consulted_at TEXT,
  used_count INTEGER DEFAULT 0,
  retracted_at TEXT,
  retraction_reason TEXT,
  UNIQUE(aedos_predicate, polarity, relation_type)
)
```

**Row creation.** Rows are created inline by the LLM when the system encounters a substrate question without an existing row. The walker (or any other consumer) calls the oracle; the oracle issues an LLM call structured by the oracle's prompt template; the LLM produces a verdict plus reason (and, for predicate translation, the full metadata); the row is stored; the verdict is returned. No budget on inline row creation (the walker's resource budget covers per-claim totals). The deployment may pre-populate common rows at install time.

**Row retraction.** Rows are retracted automatically by one of three mechanisms:

- *Substrate-internal consistency check* (5.4) detects an inconsistency between rows and retracts under the circuit-breaker policy.
- *Downstream contradiction tracing* identifies a row whose dependent verdicts are contradicted by later premises and retracts the row.
- *Deployment-injected external correction* surfaces a contradiction (the deployment's user feedback contradicts a stored verdict) and feeds it into the contradiction tracer.

Retracted rows are excluded from future consultations but remain in the table for trace integrity. Verdicts whose justification traces included the retracted row are themselves marked for re-derivation or retraction.

**The `used_count` field.** Tracks how many times the row has been consulted in the decision pipeline. Updated automatically. Observability metadata only; not a decision input. Useful for: identifying load-bearing rows in retrospective audits, identifying never-used rows that can be garbage-collected, debugging which rows contributed to a verdict.

### 5.3. The "no row quality gate" rule

The decision pipeline does not consult `used_count`, `last_consulted_at`, or any other quality-indicator field of a substrate row. A row's existence in the table (with `retracted_at` null) is the condition for its use.

The system's correctness depends on the retraction mechanism, which is automatic. There is no operator-in-the-loop; no human reviews rows; no human approves the LLM's substrate judgments before they are used. The architecture commits to the LLM producing correct substrate judgments most of the time, and to the automatic mechanisms (consistency check + downstream contradiction tracing + retraction propagation) catching the rest.

This commitment carries operational weight. The architectural correctness story is: substrate rows are generated by LLM calls with focused context (single substrate question, immediate entities/predicates, no broad-corpus reasoning); errors are caught at two levels (within-substrate by consistency check; cross-substrate-and-premise by contradiction tracing); retraction propagates through justification graphs preserving soundness over time.

What the architecture does not commit to: catching every error immediately. A wrong row may produce wrong verdicts before being detected. The soundness commitment is *over time* (no incorrect verdicts persist) not *per verdict* (every verdict is correct on first production).

### 5.4. Substrate-internal consistency checks

The substrate may contain inconsistent rows. Three classes of inconsistency are detectable:

- **Transitive equivalence violation.** Predicate translation rows imply incompatible KB mappings (predicate A maps to wikidata:P1 and to wikidata:P2 in different rows; or two predicates A and B are mapped to the same KB property but to incompatible slot-to-qualifier structures). One form of slot-to-qualifier divergence is *exempt*: two predicates mapping to the same KB property with `slot_to_qualifier` maps that are exact subject/object inversions of each other (the `capital_of` / `has_capital` pattern, both on P36) are *not* a conflict — they are inverse predicates of the same KB relation, and the divergence is the inversion itself. Any other form of `slot_to_qualifier` divergence on the same KB property remains a conflict.
- **Contradicting subsumption verdicts.** Two subsumption rows give different verdicts for the same entity pair and relation type. Should be precluded by the UNIQUE constraint, but possible if rows are migrated or operator-edited; included for completeness.
- **Conflicting distribution judgments.** Two predicate distribution rows for the same (predicate, polarity, relation_type) tuple give different verdicts. Again precluded by UNIQUE; defensive.

**Detection.** Consistency checks run on a periodic schedule (deployment-configurable; default once daily) and on-write (a newly-created or modified row is checked against neighbors before commit; conflicts are detected at this point).

**Resolution policy: retract-both with circuit breaker.** When the consistency check detects an inconsistency between two rows:

1. Both rows are immediately retracted (`retracted_at` set to now, `retraction_reason` set to "consistency_check:<inconsistency_class>"). The audit log records the retraction event.
2. Verdicts whose justification traces include either retracted row are marked for re-derivation.
3. On next consultation, if a query would have used one of the retracted rows, the LLM is invoked to regenerate the relevant row. The regenerated row is committed; if it remains consistent with the substrate, normal operation continues.
4. The circuit breaker tracks regeneration cycles per (substrate question, entity-pair-or-predicate-pair). If the same conflict re-occurs N times (default N=3, configurable), the system stops regenerating and marks the substrate question as *unresolvable*. Any subsequent verification that requires resolving this question abstains, with a trace marker `circuit_breaker_triggered`.

**Why retract-both.** The architecture does not have a privileged adjudicator (no operator, no LLM judge — the LLM that judges between conflicting rows is structurally the same kind of LLM that produced the conflict). Retracting both removes the bad row alongside the good one; subsequent regeneration in isolation, with the conflict's existence in the audit log, gives the system a chance to converge on a consistent answer. The cost is occasional re-work; the benefit is that the architecture has no implicit hierarchy of LLM authority.

**Why the circuit breaker.** Without it, the system could loop: generate row, conflict detected, retract, regenerate, conflict detected again, retract, regenerate, ad infinitum. The circuit breaker bounds compute and admits that some substrate questions may be unresolvable from the LLM's available context. Unresolvable questions produce abstention, preserving soundness.

**Audit log.** All retractions, regeneration cycles, and circuit-breaker triggerings are recorded. The audit log is queryable for post-hoc analysis (which substrate questions are most often contentious, which predicates trigger consistency conflicts) but is not consulted by the decision pipeline.

---

## 6. Sources and the derivation walker

### 6.1. Tier U

**Schema.**

```
tier_u (
  id INTEGER PRIMARY KEY,
  asserting_party TEXT NOT NULL,            -- user_id, document_id, deployment_config_id
  subject TEXT NOT NULL,                    -- natural-language reference; KB id cached in resolved_subject_id when available
  predicate TEXT NOT NULL,                  -- canonical predicate name
  object TEXT NOT NULL,                     -- typed per predicate.object_type; JSON if structured
  polarity INTEGER NOT NULL CHECK(polarity IN (0, 1)),
  resolved_subject_id TEXT,                 -- KB identifier if resolved
  resolved_object_id TEXT,                  -- KB identifier if applicable and resolved
  valid_from TEXT,                          -- ISO 8601, or 'before_present' sentinel, null = unscoped
  valid_until TEXT,                         -- ISO 8601, or 'before_present' sentinel, null = currently valid
  valid_during_ref TEXT,                    -- nullable; references another tier_u claim id for relative scope
  source_text TEXT NOT NULL,
  source_context TEXT,                      -- JSON, structure depends on deployment
  asserted_at TEXT NOT NULL,
  retracted_at TEXT,
  retraction_reason TEXT,
  UNIQUE(asserting_party, subject, predicate, object, polarity, asserted_at)
)
```

The `pattern` field present in v0.14's facts table is gone; all claims are relational.

**Asserting party.** Tier U distinguishes premises by asserting party. The chat-wrapper deployment has one asserting party per verification context. Document verification may have one per document. Deployment configuration may inject deployment-level premises with `asserting_party = "deployment_config:<id>"`. The asserting party is treated as an identifier; the architecture does not impose a hierarchy among asserting parties in v0.15. Conflicts across asserting parties are reported in the verification result; the deployment decides resolution.

**Write path.** Claims classified by Layer 2 as user-authoritative are written to Tier U with the asserting party from `context`. The write is idempotent on an exact match of `(asserting_party, subject, predicate, object, polarity)` against a non-retracted row: the existing row is returned and no new row is written.

A new claim *closes* a prior row — sets its `valid_until = now()` — only when it genuinely contradicts that row. Closure has two cases:

- **Direct negation.** A prior row asserts the *same* `object` at the *opposite* `polarity`. The prior is closed regardless of the predicate's cardinality: a claim and its direct negation cannot both currently hold.
- **Functional object revision.** A positive claim names a *different* `object` than a prior positive row for the same `(asserting_party, subject, predicate)`, and the predicate is functional (`single_valued = 1`). A functional predicate admits at most one object per subject, so the asserting party has revised the slot; the prior is closed.

Every other difference is a *parallel assertion* — the new row is written and the prior stays open. A different `object` on a *multi-valued* predicate (two occupations, two hobbies) is not a contradiction: the architecture lets the asserting party hold several values for the slot at once. A different `object` at the opposite polarity — the contrastive-correction shape "I live in X, not Y", which §4.1 extracts as `(X, positive)` and `(Y, negated)` — is likewise compatible: negating Y does not contradict asserting X.

`single_valued` is read through the predicate-translation oracle. When no oracle is wired, or the consultation fails, the predicate is treated as multi-valued — the §5.2 conservative default — so a missing classification produces a parallel write, never a false closure.

**Read path.** Three stages.

1. *Literal match.* Exact match on (asserting_party, subject, predicate, object, polarity) with current temporal scope. Direct return.
2. *Entity-resolution broadening.* For each entity slot in the lookup, consult the entity resolver to obtain the KB identifier; check Tier U rows whose corresponding slot's cached `resolved_*_id` matches.
3. *Predicate-translation broadening.* Consult the predicate translation oracle to determine whether the lookup's predicate is equivalent to a different Aedos predicate (the same KB property would be the target of both translations); retry stages 1 and 2 with the equivalent predicate.

**Temporal scope at read.** The walker compares the claim's temporal scope against Tier U rows' scopes. Rows with `valid_until` before the claim's `valid_from` are historical and may appear in chains as historical premises but cannot ground a present-tense claim. Rows with `valid_during_ref` are resolved by retrieving the referenced claim's scope and substituting at lookup.

**Cross-context Tier U.** v0.15 does not persist Tier U across verification contexts unless the deployment explicitly configures it. Each context (each chat, each document, each verification batch) has an independent Tier U. The chat-wrapper deployment may persist Tier U across the same user's conversations as a deployment choice.

### 6.2. The KB protocol

Three operations.

**Operation 1: `resolve_entity(reference, local_context) → ranked_candidates_with_provenance`.** Described in 5.1.

**Operation 2: `lookup_statements(entity, predicate) → statements`.** Given a KB entity identifier and a KB property identifier, return the statements the KB holds for the pair.

The `entity` passed to this operation is *not* unconditionally the claim's subject. It is whichever Aedos slot maps to the KB statement's subject under the predicate's `slot_to_qualifier` (§5.2) — the claim's subject for a normal predicate, the claim's *object* for an inverse predicate (e.g., `capital_of`, where P36 keys the statement on the country). The verifier resolves that slot to a KB identifier, keys the lookup on it, and compares the returned statement value against the other slot. An uninterpretable `slot_to_qualifier` makes the KB route unavailable rather than guessing a direction.

Each statement contains:
- Statement value (entity identifier, literal, date, quantity, etc.).
- Qualifiers returned by default (start_time, end_time, of, location, others). The walker compares qualifier scope against claim scope.
- Rank metadata (preferred / normal / deprecated). Deprecated excluded; preferred preferred; normal as fallback.
- Provenance: which source within the KB asserted this (if applicable).

**Operation 3: `subsumption(entity_a, entity_b, relation_type) → relation_verdict_with_provenance`.** Returns one of: `a_subsumed_by_b`, `b_subsumed_by_a`, `equivalent`, `unrelated`, plus the KB property and chain of identifiers establishing the relation.

**Failure modes.**
- Entity not found → resolver returns empty; calling code escalates to abstention.
- Predicate has no translation row and inline generation fails → KB-resolvable route unavailable.
- KB query timeout → explicit timeout; retry or escalate.
- Statement's qualifier scope conflicts with claim's scope → walker handles.

**The protocol is KB-agnostic.** Any implementation meeting the three signatures and semantics is valid.

### 6.3. Python verification

The third source of belief.

**Sandbox.** Restricted Python with standard library plus an allow-list of stable deterministic packages: `datetime`, `math`, `decimal`, `fractions`, `statistics`, `re`, `unicodedata`, `string`, plus a small per-deployment-approved set. No file I/O, no network, no subprocess.

**Sandbox threat model (v0.15).** The sandbox is designed against
LLM-generated wrong code — code the LLM produces honestly but that does the wrong thing (subprocess attempts, file I/O, broken `__import__` calls, class-hierarchy traversal as a stand-in for "look up Python internals"). It is **not** designed against an active attacker crafting input to escape the sandbox. The v0.15 implementation (`aedos.utils.sandbox`) AST-blocks static imports outside the allow-list, direct references to `__import__` / `eval` / `exec` / `open` / `compile` / `__builtins__`, and attribute access on the common-escape dunders (`__class__`, `__subclasses__`, `__globals__`, `__bases__`, `__mro__`, `__dict__`). It does **not** block encoded-string bypasses (e.g. `eval(base64.b64decode(...))`) or runtime-constructed attribute lookups (e.g. `getattr(obj, chr(95)*2 + 'class' + chr(95)*2)`).

For deployments handling adversarial input (public-facing chat endpoints, scenarios where prompts are unconstrained, any case where verifier output drives security-relevant decisions), upgrade to RestrictedPython or containerized execution — see `docs/phase_F/f3_design.md` §4 Options B and C.

**Generation.** Single-generation per claim. The LLM is given the claim and produces Python code; the code runs in the sandbox with the claim's typed slot values as inputs.

**Justification.** Each Python verification produces:
- The generated code.
- The typed inputs.
- The captured output.
- The execution metadata (runtime, exceptions if any).

The code is part of the trace; audit can re-run with the same inputs.

**No persistence of Python rules.** Each claim regenerates its rule. v0.15 does not cache.

**No cross-check.** v0.15 commits to single-generation. Errors in generated code are caught by downstream contradiction tracing: when a Python-produced verdict is later contradicted by Tier U or a KB statement, the verdict is retracted, and the Python execution that produced it is logged as discredited (a future deployment may use this log to identify failure modes, but v0.15 does not act on it programmatically).

### 6.4. The derivation walker

The walker is Aedos's inference engine.

**Algorithm.** BFS over a composite premise graph.

- **Nodes** are claim-shaped: (subject, predicate, object, polarity).
- **Edges** are derivation steps. Three kinds:
  - *Equivalence substitution* — substitute the predicate via predicate translation, or substitute an entity via entity resolution.
  - *Subsumption traversal* — substitute an entity via a subsumption relation (up or down a taxonomy), gated by predicate distribution.
  - *Premise lookup* — match against a Tier U row, a KB statement, or a Python verification result.
- **Start node:** the input claim.
- **Goal:** any premise-lookup-edge producing a definite verdict.

**Default depth.** 4. Configurable. A performance knob — soundness does not depend on depth.

**Cycle detection.** Canonical state key (subject + predicate + object + polarity, normalized). Visited keys are tracked per walk and re-visits are pruned.

**Polarity tracking.** Each edge carries a polarity transformation. Equivalence substitutions preserve polarity. Contradictory predicate translations flip polarity. Subsumption traversals preserve polarity (the walker's gating decision incorporates polarity).

**Belief revision.** A premise lookup against Tier U can return `contradicted`, not only `verified`, when the asserting party's own stored premises contradict the claim. There are two paths, both confined to Tier U — the asserting party is authoritative over its own premises:

- *Polarity belief revision.* The claim's exact negation — the same `(subject, predicate, object)` at the opposite polarity — is a currently-valid, non-retracted Tier U row. The claim is `contradicted`; the trace edge records `belief_revision: polarity_conflict`. This fires for either polarity: a positive claim against a stored negation, or a negated claim against a stored positive assertion.
- *Object-conflict belief revision.* The claim is positive, its predicate is functional (`single_valued = 1`), and Tier U holds a currently-valid positive row for the same `(asserting_party, subject, predicate)` with a *different* `object`. A functional predicate admits at most one object per subject, so the stored value contradicts the claimed one: the claim is `contradicted` and the trace edge records `belief_revision: object_conflict`. This path is new in v0.15.

Object-conflict belief revision fires *only* for functional predicates. A multi-valued predicate holding a different Tier U value is not a contradiction — the asserting party may hold several parallel beliefs — so that case falls through to abstention rather than `contradicted`. The path is also asymmetric: a *negated* claim against a Tier U positive assertion with a different value does not produce a verified verdict by `single_valued` entailment in v0.15. A functional prior `S P O′` does logically imply `¬(S P O)` for every `O ≠ O′`, but deriving a verified negation that way was deliberately left out — the conservative abstention is the accepted §3.2 false-abstain cost, and the negation-entailment direction is a v0.16 candidate.

**Predicate distribution gating.** Every subsumption traversal consults the predicate distribution oracle. The traversal is allowed only if the oracle's verdict permits the implied distribution.

**Inline row generation.** When the walker needs a substrate row that does not exist, the walker calls the LLM to generate it, stores it, and continues. No per-row generation budget. The per-claim resource budget (see below) covers the total cost.

**Resource budgets.** Each claim has two budgets:

- *Wall-clock budget.* Default 30 seconds. Configurable per deployment.
- *LLM-call budget.* Default 10 calls per claim. Configurable per deployment.

When a walk exceeds either budget, the walker abstains with a trace marker indicating which budget was exceeded. The architecture treats budget-exceedance as a form of abstention, not a soundness compromise: the system is honestly declining to commit to a verdict it could not produce within bounded compute.

Budgets are *advisory* not *adversarial-hardening* — they bound honest-input cost. Adversarial inputs that deliberately trigger deep walks abstain; this is acceptable behavior.

**Justification trace.** Every verdict carries a complete trace: each edge taken, the substrate row or premise consulted, the polarity at each step, the inline-generation events, the budget consumption.

**Termination.** The walker terminates when (a) a definite verdict is produced, (b) the depth bound is reached without a verdict, (c) the frontier is empty without a verdict, (d) the resource budget is exceeded. (b), (c), (d) all produce no-grounding-found verdicts, which Layer 5 turns into abstention with the trace's termination reason recorded.

**Multiple successful chains.** If the walker finds multiple chains producing the same verdict, the trace records all of them. If multiple chains produce *different* verdicts (contradiction), the result is a contradiction; the substrate likely contains inconsistency, which the next consistency-check run will detect and resolve.

### 6.5. Lookup order

For each claim, Layer 4's procedure:

1. Direct lookup in Tier U.
2. Direct lookup in the KB.
3. Python verification if the route is Python.
4. Derivation walk over Tier U + KB + Python.
5. Abstain.

Walk may run after a direct match if the match is non-terminal (e.g., temporal scope conflict). Within the walk, source ordering is heuristic, not constraining.

---

## 7. Verification result and justification traces

### 7.1. The verification result object

A single object per `(text, context)` input, containing:

- `claims_extracted` — list of claims from Layer 1.
- `per_claim_verdicts` — verdict per claim (verified | contradicted | abstained).
- `per_claim_traces` — justification trace per claim.
- `aggregate_metadata` — counts of verified/contradicted/abstained, walk depths observed, LLM calls per claim, budget exceedances, oracle rows consulted, source breakdown per claim.
- `audit_log_entries` — references to audit-log entries created during this verification.
- `text_input` — reference to original text and context, for re-derivability.
- `consistency_warnings` — any inconsistency markers encountered (e.g., a verdict's trace included a row that was retracted between extraction and verification).

### 7.2. Justification trace structure

Trees rooted at the verdict.

**Trace node types:**

- **Tier U premise** — `(asserting_party, subject, predicate, object, polarity, asserted_at, source_text, scope)`.
- **KB statement** — `(kb_namespace, entity, predicate, statement_value, qualifiers, rank, provenance)`.
- **Python verification** — `(generated_code, typed_inputs, output, runtime_metadata)`.
- **Substrate row consultation** — `(oracle_name, row_id, verdict, reason)`.
- **Derivation chain** — `(chain_id, steps, polarity_trace, source_breakdown)`.

Every trace is sufficient for re-derivation and audit.

### 7.3. The retraction mechanism

Retraction is the operation that removes belief in a Tier U row, a substrate row, or a cached KB resolution. Retraction propagates.

**Retraction sources (all automatic).**

- **Substrate-internal consistency check.** Section 5.4 mechanism. Detects inconsistencies, retracts both rows, applies circuit breaker on repeat conflicts.
- **Downstream contradiction tracing.** When a verdict is shown wrong by a contradicting premise (a new KB statement contradicts a derivation result, a later Tier U assertion contradicts a stored row, a deployment-injected external correction surfaces), the system walks the verdict's justification trace, identifies contributing rows, and retracts them.
- **Deployment-injected external correction.** The deployment (typically the chat-wrapper or a feedback-collection interface) may surface a user-visible contradiction (the user reports the system's verdict was wrong). This feedback enters the contradiction tracer the same as any other contradicting premise.

There is no operator-driven retraction as part of the architecture. The audit log persists retractions for observability; nothing in the architecture waits for a human to act.

**Retraction propagation.** When a row is retracted, the system identifies all verdicts whose traces include it. Each is marked for re-derivation. On re-derivation, remaining premises may support the verdict (restoration with new trace) or may not (the verdict is retracted, becoming abstained).

**The operational guarantee.** Soundness is preserved over time: every verdict the system currently asserts is supported by currently-usable premises.

---

## 8. Scope: what Aedos addresses and what it does not

### 8.1. Failure modes Aedos addresses

The six break-downs of naive LLM-plus-tool-use:

- **Multi-hop reasoning with predicate distribution.** Naive tool-use composes lookup results in the LLM's forward pass without tracking which step is grounded. The walker composes explicitly, gated by predicate distribution, with a full trace.
- **Cross-source unification.** Tier U + KB + Python composed in one chain. "Asa lives in the United States" composes Tier U (Asa lives in Williamstown), KB (Williamstown part_of Massachusetts part_of US), and predicate distribution (lives_in distributes up part_of).
- **Contextual entity disambiguation.** Per-mention resolver with local context; cached resolutions retractable.
- **Structural predicate translation including slot-to-qualifier mapping.** Predicate translation oracle records the mapping once with full metadata, reuses it, scope-checks against claims.
- **Cross-context belief revision via Tier U.** Tier U persists context-stipulated premises with temporal scope; contradictions across context detected via lookup.
- **Principled abstention.** First-class outcome. The architecture commits to never manufacturing grounding.

### 8.2. Partial addressing

- **Quantitative claims.** Addressed when the value is in the KB or computable by Python.
- **Temporal claims.** Addressed when explicit scope exists or when relative scope can be resolved.
- **Comparison claims.** Addressed when reducible to lookup + Python comparison.

### 8.3. Does not address

- Subjective and evaluative claims (no source of belief).
- Counterfactual reasoning.
- Causal claims (unless the KB encodes causation).
- Modal claims and possibility (out of scope; the existential-walk extension is a v0.16 candidate).
- Aesthetic, emotional, experiential claims.
- Live-state claims (no live-API premise source in v0.15).
- Self-knowledge claims about the LLM.
- Procedural and how-to chat (response passes through; embedded factual claims are verified).
- Meta-conversation reasoning.
- Reasoning about the LLM's own outputs.
- Source-attribution claims (verifying that a named source actually said X) — likely out of scope architecturally; the inner proposition may still be extracted and verified separately.
- Predictive / future-tense claims (extraction rejects these).

### 8.4. Implications for deployment

Aedos's value scales with deployment scope. Deployments with a high-quality KB covering most claims, where factual correctness has high cost, and where users seek factual answers, benefit most. Deployments where users want speculative or evaluative response benefit less. The deployment's policy for non-factual content is configurable.

The architecture commits to being correct within scope and being honest about being out of scope. It does not commit to handling out-of-scope content well; that is the deployment's responsibility.

---

## 9. Wikidata as v0.15's reference implementation

### 9.1. The Wikidata adapter

Implements the three protocol operations.

**SPARQL endpoint usage.** WDQS at `query.wikidata.org/sparql`. Standard HTTP caching (ETag, conditional requests, in-process LRU) with deployment-configurable TTLs. Not snapshotting in the Tier W sense — invalidated by retraction and TTL, invisible at the protocol level.

**The search API.** `wbsearchentities` for entity resolution, with language filter and type filtering driven by predicate metadata (the predicate's `object_type` constraint plus any pattern-specific class filter from predicate metadata's extension fields).

**Subsumption traversal.** SPARQL over P31, P279, P131, P361 with bounded depth (5-6 hops, configurable).

**Rank handling.** Preferred / normal / deprecated. Deprecated excluded.

**Qualifier extraction.** Default-returned with each statement; walker uses for scope comparison.

### 9.2. Predicate translation seeds (optional)

The architecture supports zero-seed deployments. A fresh v0.15 deployment with no seeds is fully functional; the LLM generates predicate translation rows inline as predicates are first encountered. There is no architectural requirement to seed any rows at install.

**Optional seed set for Wikidata implementations.** Deployments may pre-populate predicate translation rows with hand-curated mappings for common predicates. The seed set is a deployment convenience — it reduces first-use LLM cost by amortizing the metadata-and-mapping generation work upfront — but is *not* architecturally required and *does not* enable any system behavior that is otherwise blocked.

**Seed file format (when used).** A versioned JSON artifact (`seeds/predicate_translation.json`) shipping with the deployment, each entry matching the predicate_translation schema in 5.2:

```json
{
  "aedos_predicate": "holds_role",
  "object_type": "entity",
  "user_subject_required": 0,
  "distinct_slots": null,
  "routing_hint": "kb_resolvable",
  "kb_namespace": "wikidata",
  "kb_property": "P39",
  "slot_to_qualifier": {
    "subject": "statement_subject",
    "object": "statement_value",
    "org": "qualifier:P642",
    "valid_from": "qualifier:P580",
    "valid_until": "qualifier:P582"
  },
  "reason": "Seed mapping. Wikidata's P39 (position held) is the canonical property for role-organization claims about persons; P642 (of) qualifies the organization."
}
```

**Coverage of an optional seed set.** A representative seed pack would cover approximately 50-100 common predicates spanning roles, locations, kinship, categorical membership, mereological relations, quantitative properties, events, and the more common preference and propositional_attitude predicates. The exact coverage is a deployment decision.

**Architectural commitment: zero-seed must work.** v0.15 is tested in a zero-seed configuration; the test suite includes a cold-start run that processes a representative claim set against an empty substrate. Performance under zero-seed is naturally slower (every predicate triggers inline generation) but correctness is unchanged.

### 9.3. Entity resolver implementation against Wikidata

**Search.** `wbsearchentities` with the reference string, language filter, candidate-pool size 10.

**Type filtering.** Predicate metadata's `object_type` constrains the candidate type (e.g., a predicate with `object_type=entity` and a subject-slot expectation of "location" filters to entities whose P31 chain includes a geographic-class). Additional predicate-specific class filters live in predicate metadata's extension fields.

**Local-context disambiguation.** Remaining candidates scored by type-filter match strength, Wikidata search rank, local-context match (neighboring slots and prior resolutions in the same verification context as soft evidence), and LLM-mediated selection when multiple candidates pass filtering.

**Result cache.** Adapter-level, keyed on (reference, local_context_signature). Invalidated by retraction and TTL.

### 9.4. Failure modes

- Entity not found in Wikidata → empty candidates → abstention.
- Predicate has no translation row and inline generation fails → KB-resolvable route unavailable → falls to derivation or abstain.
- SPARQL timeout → retry once with backoff → escalate to abstain with explicit error trace.
- Qualifier scope conflicts with claim scope → walker handles via abstention or alternate-chain search.

### 9.5. What v0.15 commits to building

- Three protocol operations implemented against Wikidata (SPARQL + search API).
- Optional predicate translation seed set covering common predicates (50-100 entries).
- Entity resolver with pattern-type-aware filtering and LLM-mediated selection.
- Audit-log endpoints (query-only; no operator-driven mutation): inspect substrate rows, inspect resolution cache entries, view audit history, view consistency-check reports.
- Cold-start documentation: zero-seed deployment guide, seed-pack usage guide, recommended verification of cold-start correctness.

### 9.6. What v0.15 does not commit to

- A second KB implementation.
- Live-state APIs as belief sources.
- Procedural retrieval.
- Multi-hop search optimization beyond BFS at configured depth.
- Causation predicates beyond what Wikidata happens to encode.
- The two-code-generation cross-check.
- Cross-context Tier U persistence (deployment-configurable, not architectural).
- Python rule caching.
- Operator-driven row mutation.
- Deterministic event re-identification across mentions.

---

## 10. Open questions and v0.16 candidates

The known unknowns.

**Humean causation.** When (if ever) should Aedos verify causal claims? Position: abstain unless the KB encodes causation. Future deployments against medical KBs with causal relations may bring it into scope.

**Possibility-claim handling.** "Is X possible" as an existential search. v0.16 may add an existential walker, separate from the verification walker, for possibility claims.

**Cross-context Tier U.** When and how to persist Tier U across contexts.

**Consistency-check circuit-breaker tuning.** The N parameter, the regeneration behavior under circuit-break, whether the circuit breaker should distinguish substrate-question classes (predicate translation vs. subsumption vs. distribution).

**Second KB implementation.** SNOMED, UniProt, GeoNames, enterprise KGs. Required for paper experiments to demonstrate KB-agnosticism.

**Synthetic-data-verification as a deployment.** Tier U as generation-context constraints; output as filtered training-data candidates.

**Philosophical foundations companion document.** TMS lineage, Humean causation, foundationalist epistemology, abstention-as-honesty.

**Python rule caching.** Claim-shape-keyed cache for reusable rules.

**Deterministic event re-identification.** When the cost of duplicate event entities becomes operationally significant.

**Source-attribution claims.** Verifying that a named source actually said X. Likely out of scope for Aedos architecturally; flagged as a deferred question.

**Predicate normalization edge cases.** When the extractor's canonical-form predicate doesn't match other predicates that should be semantically equivalent — relying on the predicate translation oracle to link them is correct in principle but may produce sub-optimal latency. v0.16 may add an explicit predicate-canonicalization layer between extraction and the substrate.

---

## 11. Glossary

- **Aedos.** The system this document specifies. A natural-language claim-verification engine.
- **Asserting party.** The party whose assertions populate Tier U for a given verification context: the user in a chat, the author in a document, the configuration in a deployment-stipulated premise.
- **Audit log.** Persistent record of substrate operations: row creations, retractions, consistency-check reports. Observability only; not consulted by the decision pipeline.
- **Bounded inference.** The commitment to reasoning via equivalence and subsumption only, gated by predicate distribution.
- **Chat-wrapper deployment.** The configuration in which Aedos sits between a chat LLM and the KB and produces interventions on the LLM's response.
- **Circuit breaker.** The mechanism in the substrate-internal consistency check that bounds retract-regenerate cycles. After N cycles for the same conflict, the substrate question is marked unresolvable and dependent verifications abstain.
- **Claim.** A structured binary-relational proposition: subject, predicate, object, polarity, optional temporal scope, source provenance.
- **Cold-start.** A freshly-deployed v0.15 system: substrate empty (optionally seeded), Tier U empty, no cached resolutions. Functions immediately; substrate grows through use.
- **Context.** The structured object accompanying input text. Carries deployment-specific info about asserting party, conversation or document, verification scope.
- **Derivation walker.** Layer 4's component composing premises across Tier U, the KB, and Python via substrate operations.
- **Inline row generation.** The walker's (or oracle's) procedure for generating a needed substrate row via LLM call when the row does not exist.
- **Justification trace.** The complete record of which premises and substrate operations grounded a verdict. Makes Aedos a TMS.
- **KB.** Knowledge base. v0.15's reference KB is Wikidata; the architecture is KB-agnostic.
- **KB protocol.** Three-operation interface (`resolve_entity`, `lookup_statements`, `subsumption`).
- **Oracle.** Substrate component that answers a translation question and stores rows. v0.15 oracles: predicate translation, subsumption resolution, predicate distribution.
- **Predicate.** A canonical, normalized name for a binary relation in the relational claim shape. Carries metadata including object type, validation invariants, routing hint, and KB mapping (when applicable).
- **Predicate distribution.** The substrate question of whether a predicate distributes up or down a subsumption relation. The Aedos-mediated oracle (not KB-derived).
- **Predicate translation.** The substrate question of how an Aedos predicate maps to a KB property (with slot-to-qualifier correspondence), plus the carrier of predicate metadata.
- **Premise type.** One of three: Tier U, KB, Python.
- **Reified event.** A constructed entity identifier used to express a multi-participant claim (event, multi-subject relation) as a set of binary claims.
- **Relational claim.** The universal claim shape: (subject, predicate, object). With polarity, temporal scope, source provenance.
- **Resolver.** Entity resolution component. Returns ranked candidates; not an oracle in the categorical-verdict sense.
- **Resource budget.** Per-claim wall-clock and LLM-call ceilings on the walker. Exceedance produces abstention.
- **Retraction.** The operation that removes belief in a row, cached lookup, or verdict. Propagates through justification traces. All sources are automatic.
- **Routing anomaly.** A claim failing Layer 2's structural-invariant validation. Indicates extraction error.
- **Soundness.** Every verified verdict is supported by traceable premises. Aedos's correctness commitment.
- **Substrate.** Layer 3. The translation layer. Comprises entity resolver, three oracles, consistency checker.
- **Substrate-internal consistency check.** Automatic detection of inconsistent substrate rows. Resolves via retract-both and circuit breaker.
- **Subsumption.** The taxonomic relation: is_a or part_of. Resolved primarily via KB protocol; substrate rows for meta-judgments.
- **Temporal scope.** `valid_from` and `valid_until` metadata on claims, expressing when the claim was/is in force. Includes the `before_present` sentinel for unspecified past validity, and `valid_during_ref` for relative scope.
- **Tier U.** Context-stipulated premise store. Per verification context unless deployment configures cross-context persistence.
- **TMS (Truth-Maintenance System).** Doyle/Kleer architectural tradition Aedos descends from.
- **`used_count`.** Per-row observability metadata tracking consultations. Not a decision input.
- **Verification result.** Structured output of Layer 5.
- **Wikidata.** v0.15's reference KB implementation.

---

*End of architecture document, v0.15 Draft 2.*