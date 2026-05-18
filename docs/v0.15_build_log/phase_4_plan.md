# Phase 4 Plan — KB Protocol + Wikidata Adapter

**Goal.** Three KB protocol operations (`resolve_entity`, `lookup_statements`, `subsumption`) abstracted as a Protocol and implemented against Wikidata via fixture-backed mocks (no live KB during overnight run). Entity resolver wired to entity_resolution_cache. KBVerifier handles the full KB-resolvable claim path: entity slot resolution → predicate translation → KB lookup → qualifier scope comparison → verdict.

**Dependencies.** Phase 0 (HTTP cache, DB, audit log), Phase 2 (predicate translation rows), Phase 3 (routing stubs, Tier U).

---

## What gets built

### 1. `src/aedos_v0_15/layer4_sources/kb_protocol.py`
- `KBEntityID = str` alias
- `KBPropertyID = str` alias
- Dataclasses: `LocalContext`, `ResolutionCandidate`, `Statement`, `SubsumptionResult`
- `KBProtocol` Protocol class (structural subtyping via `typing.Protocol`)

### 2. `src/aedos_v0_15/layer4_sources/kb_wikidata.py`
- `WikidataAdapter` implementing `KBProtocol`
- `resolve_entity`: when `RUN_LIVE_KB` not set, loads from fixture file; delegates to HTTP cache otherwise
- `lookup_statements`: fixture-backed SPARQL simulation
- `subsumption`: fixture-backed traversal
- `FixtureNotFoundError` for clean failure when fixture missing

### 3. `src/aedos_v0_15/layer3_substrate/resolver.py`
- `EntityResolver` with `resolve`, `select`, `retract_cache_entry`
- Consults `entity_resolution_cache`; on miss, calls `kb_protocol.resolve_entity`
- Writes cache on miss; increments `used_count` on hit

### 4. `src/aedos_v0_15/layer4_sources/kb_verifier.py`
- `KBVerdictType` enum: `verified | contradicted | no_match | no_kb_path`
- `KBVerdict` dataclass with `verdict`, `statement` (matched), `trace`
- `KBVerifier.verify(claim, current_time)` — full 6-step resolution pipeline

### 5. Wikidata fixture set — `tests/v0_15/fixtures/wikidata/`
- `search_asa.json` — wbsearchentities result for "Asa Shepard"
- `search_obama.json` — wbsearchentities result for "Obama"
- `search_williams_college.json` — wbsearchentities for "Williams College"
- `search_google.json` — wbsearchentities for "Google"
- `entity_Q76.json` — wbgetentities for Q76 (Barack Obama)
- `entity_Q49112.json` — wbgetentities for Williams College
- `sparql_P39_Q76.json` — P39 (position held) statements for Q76
- `sparql_P131_Q49112.json` — P131 (located in) statements for Q49112
- `sparql_subsumption_Q95.json` — P31/P279 chain for Q95 (Google)
- `sparql_no_match.json` — empty SPARQL result
- `README.md` — fixture inventory

### 6. Tests (~90 new)
- `tests/v0_15/unit/test_kb_protocol.py` — dataclass field tests
- `tests/v0_15/unit/test_wikidata_adapter.py` — fixture-backed adapter tests
- `tests/v0_15/unit/test_entity_resolver.py` — cache cold/warm, retraction
- `tests/v0_15/unit/test_kb_verifier.py` — verify/contradict/no_match/scope
- `tests/v0_15/integration/test_kb_path.py` — end-to-end KB-resolvable claim roundtrip

### 7. Calibration corpora (authored, not executed)
- `tests/v0_15/calibration/entity_resolution_corpus.jsonl` — 50 cases
- `tests/v0_15/calibration/kb_mapping_corpus.jsonl` — 40 cases

---

## Adversarial corpus strategy

**entity_resolution_corpus:** Includes name-collision cases (Paris=city vs Paris=person), slot-position disambiguation (subject vs object of holds_role changes expected type), no-match cases where no Wikidata Q-number should win, and cases where the top search rank is wrong type and should be filtered.

**kb_mapping_corpus:** Includes predicates that map to qualifier-constrained KB properties (e.g., holds_role → P39 with qualifier P580/P582 for date range), predicates that have multiple valid mappings, and predicates that should abstain from KB lookup.

---

## Ambiguities (pre-resolved)

1. **Fixture file naming:** Use `search_{term}.json` for wbsearchentities, `entity_{qid}.json` for wbgetentities, `sparql_{prop}_{qid}.json` for SPARQL. Keeps filenames predictable for fixture lookup logic.
2. **FixtureNotFoundError vs stub result:** Raise `FixtureNotFoundError` — let tests explicitly construct fixture-complete adapters; don't silently return empty results.
3. **Subsumption direction convention:** `a_subsumed_by_b` means A is a subtype/instance of B (A → B path exists via P31/P279). `b_subsumed_by_a` is reverse. `equivalent` means both directions exist.
4. **Scope comparison in KBVerifier:** If claim has `valid_from`/`valid_until`, check against P580/P582 qualifier on the KB statement. If no qualifier, statement is assumed always-valid → compatible with any scope.
5. **Multiple candidates in EntityResolver.select:** Return the first (highest-score) candidate without LLM call in tests; LLM call only when `score` of top ≤ 0.6 and second candidate within 0.15. Tests use candidates pre-sorted by score.
