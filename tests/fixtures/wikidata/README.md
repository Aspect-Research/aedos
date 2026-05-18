# Wikidata Fixture Set — v0.15 Phase 4

These fixtures back the WikidataAdapter when `RUN_LIVE_KB` is not set.
Each fixture mirrors the response shape from the real Wikidata API endpoint.

## Search fixtures (`wbsearchentities`)

| File | Search term | Top result |
|---|---|---|
| `search_obama.json` | "Obama" | Q76 Barack Obama |
| `search_williams_college.json` | "Williams College" | Q49112 Williams College |
| `search_google.json` | "Google" | Q95 Google |
| `search_no_match.json` | "xyzzy_nonexistent_entity_42" | empty |

## Entity fetch fixtures (`wbgetentities`)

| File | Entity | Key claims |
|---|---|---|
| `entity_Q76.json` | Q76 Barack Obama | P31=Q5 (human), P39=Q11696 (President) with P580/P582 qualifiers |
| `entity_Q49112.json` | Q49112 Williams College | P31=Q189004 (liberal arts college), P131=Q771397 (Williamstown) |

## SPARQL fixtures

| File | Query shape | Source entity / property |
|---|---|---|
| `sparql_P39_Q76.json` | lookup_statements P39 on Q76 | holds_role for Barack Obama |
| `sparql_P131_Q49112.json` | lookup_statements P131 on Q49112 | located_in for Williams College |
| `sparql_subsumption_Q95.json` | subsumption traversal from Q95 | Google → business org chain |
| `sparql_no_match.json` | any query with empty result | generic empty SPARQL response |

## Fixture lookup convention

The `WikidataAdapter` resolves fixtures by:
- Search: `search_{normalized_term}.json` where term is lowercased with spaces→underscores
- Entity fetch: `entity_{qid}.json`
- SPARQL statements: `sparql_{prop}_{entity}.json`
- SPARQL subsumption: `sparql_subsumption_{entity_a}.json`

When `RUN_LIVE_KB=1`, the adapter bypasses fixtures and calls the live Wikidata API.
Phase 10.5 validates that the fixture shapes match current live Wikidata responses.
