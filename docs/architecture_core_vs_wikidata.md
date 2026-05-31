# Aedos architecture — core vs. Wikidata-calibrated

A map of the codebase into two conceptual groups:

- **CORE** — backend-independent. Would survive swapping the knowledge base for a
  non-Wikidata source. Holds the verification *architecture* (extraction, routing,
  the discover/verify walker, the TMS/provenance, aggregation, the deployment surface)
  and treats knowledge-base identifiers as opaque strings.
- **WIKIDATA-CALIBRATED** — contains Wikidata-specific knowledge: SPARQL/WDQS, P-property
  and Q-entity identifiers, the property-constraint (P2302) ontology vocabulary,
  continent Q-id sets, and the Wikipedia search API.

The two groups meet at one **boundary**: `layer4_sources/kb_protocol.py` (the `KBProtocol`
abstraction + its DTOs), wired by the composition root `pipeline.py`.

This document is an architectural-hygiene reference, not a build instruction. The canonical
system specification remains `docs/architecture.md`.

## The seam

- `layer4_sources/kb_protocol.py` — the `KBProtocol` Protocol (`resolve_entity`,
  `lookup_statements`, `subsumption`, `enumerate_neighbors`, `verify_transitive_path`,
  `fetch_property_ontology`, `fetch_label`) plus opaque DTOs (`Statement`,
  `ResolutionCandidate`, `SubsumptionResult`, `TransitivePathResult`, `LocalContext`).
  `KBEntityID` / `KBPropertyID` are opaque string aliases.
- `pipeline.py` — the **composition root**: the only module that imports the concrete
  `WikidataAdapter` and injects it (as a `KBProtocol`) into the resolver, verifier, walker,
  and property-relations. This single concrete import is by design.

The import graph respects the seam: **no CORE module imports `kb_wikidata`** — they consume
the backend through the injected protocol.

## Group membership

### Wikidata-calibrated
- `layer4_sources/kb_wikidata.py` — the adapter; the overwhelming concentration of Wikidata
  knowledge (SPARQL/WDQS endpoints, `^Q\d+$`/`^P\d+$` id regexes, `_SUBSUMPTION_PROPERTIES`,
  `_DEFAULT_NEIGHBOR_PROPERTIES`, `_DEFAULT_QUALIFIER_PROPS`, the P361 part-of bridge, and the
  full P2302 property-constraint ontology vocabulary).
- `layer1_extraction/wikipedia_normalizer.py` — entity-surface normalization built on the
  Wikipedia/Wikidata search APIs (`wbsearchentities`, P31 type fetch).

### Boundary
- `layer4_sources/kb_protocol.py` (the abstraction), `pipeline.py` (composition root).

### Core (backend-independent)
- **layer1 extraction:** `extractor.py`, `triage.py`, `normalization.py`, `decomposition.py`,
  `temporal.py`. (Wikidata property ids appear only inside the LLM extraction prompt — taught
  to the model at request time, never branched on in Python.)
- **layer2 routing:** `router.py`, `validator.py`.
- **layer3 substrate:** `resolver.py`, `subsumption.py`, `predicate_translation.py`,
  `predicate_distribution.py`, `property_relations.py`, `consistency.py`, `sling_fallback.py`,
  `substrate_exceptions.py`, `__init__.py`. (The substrate oracles delegate all P/Q knowledge
  behind `KBProtocol`; `predicate_translation.py`'s P-ids live only in the oracle prompt.)
- **layer4 sources:** `tier_u.py`, `promotion.py`, `python_verifier.py`.
- **layer5 result:** `aggregator.py`, `trace.py`, `retraction.py`, `contradiction_tracer.py`.
- **top-level / infra:** `app.py`, `config.py`, `database.py` (schema is namespace-generic —
  `kb_namespace`/`kb_property` store opaque strings), `seed_loader.py`,
  `deployment/chat_wrapper.py`, `llm/client.py`, `audit/log.py`, `utils/*`.

## The "knowledge lives in the prompt/oracle" principle

Wikidata vocabulary enters CORE modules only by **generation**, not by hardcoding:
`predicate_translation.py`'s `_GENERATION_SYSTEM_PROMPT` and `extractor.py`'s extraction
prompt teach the LLM to emit Wikidata property ids; the Python logic stores them as opaque
strings. This is the intended pattern (knowledge in prompt/KB/oracle, not Python lookup tables)
and is why those modules classify as core despite mentioning P-ids.

## Residual calibration above the seam (future-refactor candidates — NOT changed here)

The separation is clean in the substrate/result core but imperfect at the verification surface
and the normalizer. These are documented as roadmap items, deliberately **not** modified in the
cleanliness pass (which makes no functional changes):

1. `layer1_extraction/wikipedia_normalizer.py` reaches **around** the protocol into
   adapter-private methods (`self._kb_adapter.wbsearchentities(...)`,
   `self._kb_adapter._fetch_p31_for_candidates(...)`) and hardcodes the Wikipedia endpoint.
   A future `KBProtocol.search` / type-fetch operation would close this.
2. `layer4_sources/walker.py` hardcodes a relation→P-id table (`_D5_NEIGHBOR_PROPS_BY_RELATION`
   = P31/P279/P131/P361/P17) and the P580/P582 temporal-qualifier keys. The base property is
   routed through the oracle, but the qualifier semantics are baked in.
3. `layer4_sources/kb_verifier.py` holds Wikidata constants in control flow: `CONTINENT_QIDS`,
   `_LOCATION_KB_PROPERTIES` (P131/P17/P30/P361/P206/P276), `_GEO_CONTAINER_TYPES` (Q5107), and
   direct P580/P582 qualifier reads. This geographic/temporal calibration sits just above the
   adapter seam rather than inside it.

Pushing (2) and (3) behind the protocol, and giving the normalizer a protocol-level search,
would make the core/Wikidata cut crisp: *everything is core except the adapter and the
normalizer.* That refactor is out of scope for the cleanliness pass and would be its own
functional change with its own tests.
