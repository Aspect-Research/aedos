# Phase F2 — Wikidata Live Implementation Design

*Design document. F2 implementation works from this. The operator
confirms the design (especially the four open questions in §12) before
any implementation commit lands.*

---

## 1. Frame

F2 implements the three `WikidataAdapter._live_*` methods (F-001..003)
against the real Wikidata API and wires the adapter into
`build_pipeline` (F-004..008) so the deployed pipeline actually
exercises the methods with the configuration it requires.

F2 also lands two F1-surfaced items that pair naturally with the
Wikidata work:

- **F-009 alignment** — fix the four purpose-string mismatches so the
  deployed pipeline routes substrate / verifier calls to the model the
  documentation promises.
- **F-022 + runbook hygiene** — implement `AEDOS_KB_REQUEST_DELAY_MS`
  and remove the dead `AEDOS_LLM_TEMPERATURE` runbook reference.

F2's discipline (per F1's wiring-correctness acceptance criterion): every
capability F2 implements must be reachable from the deployed pipeline
path, verified by at least one live test.

---

## 2. Settled by architecture or inputs

Re-stated from the F1 audit (§6) and the plan (§F2) so the design has
its inputs in one place.

| Question | Decision | Source |
|---|---|---|
| `_live_lookup` endpoint | SPARQL via WDQS (`query.wikidata.org/sparql`) | Architecture §9.1 + fixture shape (`sparql_P39_Q76.json`) |
| `_live_resolve` endpoint | `wbsearchentities` via `wikidata.org/w/api.php` | Architecture §9.1 + §9.3 |
| Subsumption depth | 6 hops, configurable | `Config.wikidata_subsumption_depth = 6` |
| Subsumption properties | `is_a` → P31|P279; `part_of` → P131|P361 | Architecture §9.1 |
| Rank handling | preferred preferred; normal fallback; deprecated excluded | Architecture §9.1; existing fixture-mode behavior (`kb_wikidata.py:141-142`) |
| Caching layer | HTTP-level LRU + ETag | Architecture §9.1; `CachingHTTPClient` exists |
| Polarity handling | KB lookups return positive statements only; `KBVerifier._apply_polarity` handles polarity flipping | `kb_verifier.py:281-295` |
| Slot-to-qualifier direction | `KBVerifier._lookup_targets` already swaps for inverse predicates; `_live_lookup` is direction-neutral | `kb_verifier.py:238-278` (D19 resolved fix-up 3) |

---

## 3. Operator-confirmed ambiguities (recap)

From the plan check-in:

- **A** Type filtering at resolution → defer to v0.16; live `_live_resolve` returns search-ranked candidates without per-candidate P31 fetching.
- **B** Rate limiting → 5/s SPARQL, 50/s search; configurable via `AEDOS_KB_REQUEST_DELAY_MS`.
- **C** User-Agent → `Aedos/0.15 (https://github.com/Aspect-Research/aedos; asa@aspectresearch.org)` (GitHub URL verified). Privacy caveat captured for future commercial-deployment revisit.
- **D** Persistent statement cache → defer to v0.16.
- **F** Implement `AEDOS_KB_REQUEST_DELAY_MS`; remove `AEDOS_LLM_TEMPERATURE` from runbook.
- **G** D13 (KB-grounded retraction) deferred to v0.16; F2 does not touch trace-edge cache row ids.

Plus F1 surfaced:

- **F-009 placement** → F2 (operator confirmed).
- **Wiring correctness** → F2 acceptance includes "the capability is reachable from the deployed pipeline path."

---

## 4. Design — `_live_resolve(reference, local_context)`

### Endpoint and parameters

- URL: `Config.wikidata_search_endpoint` (default `https://www.wikidata.org/w/api.php`)
- Method: GET
- Query parameters:
  - `action=wbsearchentities`
  - `search=<reference>`
  - `language=en`
  - `type=item` (entities only; not properties)
  - `limit=<Config.wikidata_candidate_pool_size>` (default 10)
  - `format=json`

### Request execution

Via `CachingHTTPClient.get(url, params, ttl_seconds=Config.http_cache_entity_ttl_seconds)` — the resolution result is cached at the HTTP layer with the configured TTL (default 3600s).

The User-Agent header is set at `CachingHTTPClient` construction (see §7.3).

Rate limit: search limiter (50/s default), see §7.4.

### Response parsing

Response shape (per `search_obama.json`):
```json
{
  "search": [
    {"id": "Q76", "label": "Barack Obama", "description": "44th president...", "match": {...}},
    ...
  ]
}
```

Map to `ResolutionCandidate`:
- `kb_identifier` = `item["id"]`
- `score` = `1.0 / (rank + 1)` where `rank` is the zero-indexed position in the result array — same scoring as fixture mode
- `provenance` = `{"search_rank": rank, "label": item.get("label"), "description": item.get("description")}`

This matches `_fixture_resolve` exactly so consumer code (`EntityResolver.select`) sees identical candidate shape between fixture and live modes.

### Error handling

Per architecture §9.4 ("Entity not found → empty candidates → abstention"):

- httpx exceptions (`httpx.TimeoutException`, `httpx.NetworkError`, `httpx.HTTPStatusError`): single retry with 1s backoff, then return `[]`.
- 404 / empty `search` array: return `[]` immediately (no retry).
- Malformed JSON / missing `search` key: log to audit log; return `[]`.

The method *never* raises. The walker treats `[]` as a resolution failure and abstains.

### Audit logging

One audit event per live call, regardless of success / failure:

```python
log_event(
    self._db,
    event_type="kb_live_resolve",
    event_subject=reference,
    event_data={
        "candidate_count": len(candidates),
        "duration_ms": ...,
        "cache_hit": cached_at_http_layer,
        "retry_count": retries,
    },
)
```

This is new (not in fixture mode) — gives F4 traces a clear signal that live calls happened. Routes through the existing audit log infrastructure (no new tables).

### Tests

Live tests (gated by `RUN_LIVE_KB=1`):

1. `test_live_resolve_obama_returns_q76` — exact match against the known case Phase E surfaced.
2. `test_live_resolve_williams_college_returns_q49112` — known location case.
3. `test_live_resolve_disambiguation_obama_returns_multiple` — sanity check that multiple candidates come back (operator decisions disambiguate downstream).
4. `test_live_resolve_unknown_entity_returns_empty` — sentinel string not in Wikidata.

Mocked-transport tests (no live API):

5. `test_resolve_retries_on_timeout_then_succeeds`
6. `test_resolve_retries_on_timeout_then_gives_up_with_empty`
7. `test_resolve_malformed_response_returns_empty`

---

## 5. Design — `_live_lookup(entity, predicate)`

### Endpoint and query shape

- URL: `Config.wikidata_sparql_endpoint` (default `https://query.wikidata.org/sparql`)
- Method: GET (WDQS supports both; GET caches better via ETag)
- Headers: `Accept: application/sparql-results+json`

SPARQL template (parametrized on `{entity}`, `{predicate}`, `{qualifier_props}`):

```sparql
SELECT ?value ?valueType ?rank
       ?qual_P580 ?qual_P582 ?qual_P642
WHERE {
  wd:{entity} p:{predicate} ?statement .
  ?statement ps:{predicate} ?value .
  ?statement wikibase:rank ?rank .
  FILTER (?rank != wikibase:DeprecatedRank)
  OPTIONAL { ?statement pq:P580 ?qual_P580 . }
  OPTIONAL { ?statement pq:P582 ?qual_P582 . }
  OPTIONAL { ?statement pq:P642 ?qual_P642 . }
  BIND(IF(isURI(?value), "entity", "literal") AS ?valueType)
}
```

### Qualifier strategy

Default qualifier set (always queried): **P580 (start time), P582 (end time), P642 (of)**. Rationale:

- P580/P582 are required for `KBVerifier._scope_compatible` (universal scope check).
- P642 ("of") appears in the most common seed predicate (`holds_role` → P39 with P642 for the organization). It's the only non-temporal qualifier the seed pack references heavily.

For predicates whose `slot_to_qualifier` references other qualifier P-codes, the live query *does not* return those qualifiers in F2 scope. The KB verifier's scope check still works (P580/P582 are always there); slot-to-qualifier-driven fields just won't be populated. This is acceptable because:

- The seed pack's `slot_to_qualifier` qualifiers beyond P642 are infrequent (P580/P582/P642 cover the common cases).
- A missing qualifier causes a *false abstain* (the §3.2 acceptable cost), not a false verdict.
- v0.16 can extend to dynamic qualifier discovery via `slot_to_qualifier`.

### Response parsing

Bindings format (per `sparql_P39_Q76.json`):

```json
{"results": {"bindings": [
  {
    "value": {"type": "uri", "value": "http://www.wikidata.org/entity/Q11696"},
    "valueType": {"value": "entity"},
    "rank": {"value": "http://wikiba.se/ontology#NormalRank"},
    "qual_P580": {"value": "+2009-01-20T00:00:00Z", "datatype": "...#dateTime"},
    "qual_P582": {"value": "+2017-01-20T00:00:00Z", "datatype": "...#dateTime"}
  }
]}}
```

Use existing `_fixture_lookup` parsing logic (`kb_wikidata.py:129-176`) — it already handles this shape correctly. Extract into a shared `_parse_statement_bindings(bindings) → list[Statement]` helper called by both fixture and live paths.

### Error handling

Per architecture §9.4 ("SPARQL timeout → retry once with backoff → escalate to abstain with explicit error trace"):

- Timeout: single retry with 1s backoff; on second timeout, return `[]` (no statements ≠ false; just no grounding).
- HTTP 500/503 (WDQS overload): single retry with 2s backoff.
- HTTP 429 (rate limit): respect the `Retry-After` header if present; single retry.
- Non-recoverable errors: return `[]` and log to audit log.

### Audit logging

```python
log_event(
    self._db,
    event_type="kb_live_lookup",
    event_subject=f"{entity}:{predicate}",
    event_data={
        "statement_count": len(statements),
        "duration_ms": ...,
        "retry_count": retries,
    },
)
```

### Tests

Live:

1. `test_live_lookup_p39_q76_returns_president_role` — known case with date qualifiers.
2. `test_live_lookup_p36_q30_returns_washington_dc` — inverse-predicate case (capital_of); confirms D19 lookup direction still works under live data.
3. `test_live_lookup_p131_q49112_returns_williamstown` — known located_in case.
4. `test_live_lookup_unknown_entity_returns_empty` — Q-id that doesn't exist.

Mocked:

5. `test_lookup_filters_deprecated_rank`
6. `test_lookup_timeout_retries_then_gives_up`

---

## 6. Design — `_live_subsumption(entity_a, entity_b, relation_type)`

### Strategy: two ASK queries

For each direction, fire an ASK query. Combine results into verdict.

```sparql
# Direction 1: is entity_a subsumed by entity_b?
ASK {
  wd:{entity_a} (wdt:P31|wdt:P279)+ wd:{entity_b} .
}

# Direction 2: is entity_b subsumed by entity_a?
ASK {
  wd:{entity_b} (wdt:P31|wdt:P279)+ wd:{entity_a} .
}
```

Property mapping by relation_type:
- `is_a` → `(wdt:P31|wdt:P279)`
- `part_of` → `(wdt:P131|wdt:P361)`

### Verdict logic

| direction_1 (a→b) | direction_2 (b→a) | verdict |
|---|---|---|
| true | false | `a_subsumed_by_b` |
| false | true | `b_subsumed_by_a` |
| true | true | `equivalent` (cycle; rare) |
| false | false | `unrelated` |

### Why ASK, not SELECT for the path

WDQS supports unbounded `+` property paths but times out on broad-fanout queries. ASK is much faster than SELECT for path-existence queries because the engine can short-circuit on first match.

The `traversal_chain` returned in `SubsumptionResult` is **populated minimally** for F2 — set to `[entity_a, entity_b]` when the relation holds, `[]` otherwise. Architecture §9.1 doesn't require full chains; existing consumers (`subsumption.py:108-111`) only store the chain on the verdict, not consult it. v0.16 can extend to full intermediate-chain population.

### Depth bound

`Config.wikidata_subsumption_depth = 6` (existing). Pure `+` doesn't enforce a bound; the WDQS timeout (60s server-side) and our 30s client timeout enforce one practically. For F2, accept this as the bound and document. If Phase 10.5 surfaces cases where deeper chains are needed (unlikely; >6 hops is rarely load-bearing), revisit in v0.16.

### Establishing property

ASK returns just true/false. To return `establishing_property` for the verdict, run a tiny follow-up SELECT *only when the ASK was true*:

```sparql
SELECT ?prop WHERE {
  VALUES ?prop { wdt:P31 wdt:P279 }
  wd:{entity_a} ?prop ?intermediate .
  ?intermediate (wdt:P31|wdt:P279)* wd:{entity_b} .
}
LIMIT 1
```

This adds one query per non-`unrelated` verdict. Acceptable cost; gives the trace useful information.

### Error handling

Same pattern as `_live_lookup`: timeout → single retry → return `unrelated` verdict (architecture treats it as a non-finding, not an error). Audit log:

```python
log_event(
    self._db,
    event_type="kb_live_subsumption",
    event_subject=f"{entity_a}<>{entity_b}:{relation_type}",
    event_data={
        "verdict": verdict_str,
        "duration_ms": ...,
        "queries_run": 2 if unrelated else 3,
        "retry_count": retries,
    },
)
```

### Tests

Live:

1. `test_live_subsumption_q76_is_a_q5_yes` — Obama is a human; direct.
2. `test_live_subsumption_q49112_part_of_q1384_transitive` — Williams College → Williamstown → MA (multi-hop part_of).
3. `test_live_subsumption_q76_unrelated_q95` — Obama unrelated to Google.
4. `test_live_subsumption_equivalent_case` — pick a known equivalence case from Wikidata for the `equivalent` verdict.

Mocked:

5. `test_subsumption_both_directions_yields_equivalent`
6. `test_subsumption_timeout_returns_unrelated`

---

## 7. Wiring design

### 7.1 `build_pipeline` signature extension

Change:

```python
def build_pipeline(
    db,
    llm_client: Optional[LLMClient] = None,
    kb=None,
    config: Optional[Config] = None,  # NEW
) -> Pipeline:
```

When `config is None`, instantiate `Config.from_env()`. When `kb is None`, construct `WikidataAdapter` with the config-derived HTTP cache, LLM client, and config object.

### 7.2 HTTP cache construction

```python
if kb is None:
    lru = LRUHTTPCache(
        max_size=config.http_cache_lru_size,
        default_ttl_seconds=config.http_cache_entity_ttl_seconds,
    )
    http_client = CachingHTTPClient(
        cache=lru,
        default_ttl_seconds=config.http_cache_entity_ttl_seconds,
        timeout_seconds=30.0,  # F2 scope: not Config-driven; v0.16 candidate
        headers={"User-Agent": config.user_agent},
    )
    kb = WikidataAdapter(
        http_cache=http_client,
        llm_client=client,
        db=db,
        config=config,
    )
```

### 7.3 User-Agent

New `Config` field:

```python
user_agent: str = field(
    default_factory=lambda: os.getenv(
        "AEDOS_USER_AGENT",
        "Aedos/0.15 (https://github.com/Aspect-Research/aedos; asa@aspectresearch.org)",
    )
)
```

Threaded through `CachingHTTPClient(headers={"User-Agent": ...})`. Privacy caveat noted in `Config` docstring: contact email appears in HTTP headers; revisit for commercial deployment.

### 7.4 Rate limiter

New module `src/aedos/utils/rate_limit.py`:

```python
class RateLimiter:
    """Simple per-instance token-bucket-ish limiter — enforces minimum
    interval between acquires. Single-thread (the deployed pipeline is
    single-threaded; if a Phase 10.5-era harness adds concurrency, the
    limiter needs upgrading to threading.Lock — captured as v0.16
    candidate D32 if needed)."""

    def __init__(self, max_per_second: float, override_delay_ms: Optional[int] = None):
        if override_delay_ms is not None:
            self._interval = override_delay_ms / 1000.0
        else:
            self._interval = 1.0 / max_per_second
        self._last_call = 0.0

    def acquire(self):
        now = time.monotonic()
        wait = self._interval - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
```

In `WikidataAdapter.__init__`:

```python
override_ms = int(os.getenv("AEDOS_KB_REQUEST_DELAY_MS")) if os.getenv("AEDOS_KB_REQUEST_DELAY_MS") else None
self._sparql_limiter = RateLimiter(
    max_per_second=config.wikidata_sparql_rate_per_second,  # new Config field, default 5
    override_delay_ms=override_ms,
)
self._search_limiter = RateLimiter(
    max_per_second=config.wikidata_search_rate_per_second,  # new Config field, default 50
    override_delay_ms=override_ms,
)
```

`AEDOS_KB_REQUEST_DELAY_MS` overrides both limiters with the same explicit delay — matches the runbook's existing knob semantics.

Acquires happen at the top of each live method.

### 7.5 New `Config` fields summary

```python
user_agent: str = ...  # see 7.3
wikidata_sparql_rate_per_second: float = 5.0
wikidata_search_rate_per_second: float = 50.0
```

`Config.wikidata_subsumption_depth` already exists; `Config.wikidata_*_endpoint` already exist. F2 wires what exists + adds three new fields.

---

## 8. F-009 alignment design

### Choice: rename call sites, not the table

Two options:

- **A. Rename call sites to match table keys.** `subsumption.py:243`
  changes from `purpose="subsumption_generation"` to
  `purpose="substrate:subsumption"`. Four call sites change.
- **B. Add new keys to the table.** Add `subsumption_generation`,
  `distribution_generation`, etc. to `DEFAULT_MODEL_BY_PURPOSE`.

**Choose A.** Rationale:

- The table's namespace convention (`substrate:*`, `extractor:*`,
  `python_verifier`, `walker`) is informative — it groups purposes by
  pipeline component. The call-site names (`subsumption_generation`,
  etc.) are call-action-style and don't group.
- The `.env.example` documentation lists the table-key namespace as the
  per-purpose override convention (`AEDOS_MODEL_substrate:subsumption`).
  Aligning call sites preserves the documented override surface.
- Phase E's `_CANDIDATES` results don't reference the per-purpose
  configuration by name — they used the `"*"` wildcard — so the rename
  doesn't break existing data.

### Concrete changes

| File | Line | Before | After |
|---|---|---|---|
| `subsumption.py` | 243 | `purpose="subsumption_generation"` | `purpose="substrate:subsumption"` |
| `predicate_distribution.py` | 162 | `purpose="distribution_generation"` | `purpose="substrate:predicate_distribution"` |
| `resolver.py` | 102 | `purpose="entity_selection"` | `purpose="substrate:entity_resolution"` |
| `python_verifier.py` | 90 | `purpose="python_code_generation"` | `purpose="python_verifier"` |

### Dead-key cleanup

`extractor:assistant` and `walker` are dead keys (no call site).
Disposition:

- `walker` — remove. The walker doesn't call the LLM directly; its
  consumers (substrate oracles) have their own purposes. The key was
  speculative.
- `extractor:assistant` — keep but document as reserved. Architecture
  may want it for a future chat-assistant extraction path; no harm in
  leaving it. Add a comment marking it as reserved.

### Verification test

New unit test `tests/unit/test_purpose_table_completeness.py`:

```python
def test_every_call_site_purpose_is_in_default_table():
    """Verifies F-009 doesn't regress. Greps src/aedos/ for purpose=
    parameters and confirms each is a key in DEFAULT_MODEL_BY_PURPOSE."""
    import re
    purposes_used = set()
    for path in glob.glob("src/aedos/**/*.py", recursive=True):
        if "llm/client.py" in path:
            continue  # the client itself doesn't have call-site purposes
        with open(path) as f:
            for match in re.finditer(r'purpose=["\'](.+?)["\']', f.read()):
                purposes_used.add(match.group(1))
    table_keys = set(DEFAULT_MODEL_BY_PURPOSE.keys())
    # purpose="chat" is the implicit fallback, always allowed
    purposes_used.discard("chat")
    missing = purposes_used - table_keys
    assert not missing, (
        f"Purposes used in src/aedos/ but not in DEFAULT_MODEL_BY_PURPOSE: "
        f"{missing} — F-009 regression"
    )
```

This guards against F-009 recurring. Direct response to D26's "the audit
chain measured behavior that didn't match the documented configuration"
finding: a CI-runnable check that the documentation and the code agree.

### `.env.example` documentation update

Update the model-routing table to remove `walker`. The other four
purposes' rows already reference the table-key names; no change needed
there.

---

## 9. Runbook hygiene (F-022, F-023)

### F-022 implement

`AEDOS_KB_REQUEST_DELAY_MS` is read in `WikidataAdapter.__init__` (see
§7.4). No further changes needed; the runbook's existing reference becomes
accurate.

### F-023 remove

`phase_10_5_runbook.md:370-372` currently reads:

```
**LLM returns malformed tool output:** Increase temperature slightly
(`AEDOS_LLM_TEMPERATURE=0.1`) for the predicate translation oracle; the default
is 0.0.
```

Replace with:

```
**LLM returns malformed tool output:** Capture the raw response from the
audit log (the `LLMClient._attach_raw_response` path preserves the SDK
response on failed parses). If a specific model produces persistent
malformed tool output, the model is likely incompatible with the tool
schema — see `docs/v0.16_planning.md` D25 for the DeepSeek precedent.
Tuning options (temperature, retry, prompt restructuring) are model-
specific; v0.16 may add a per-purpose temperature knob if calibration
data shows it's needed.
```

This replaces a knob the code doesn't support with an actionable
diagnostic path the code does support.

---

## 10. Test strategy

### Live tests

Directory: `tests/integration/live/test_wikidata_live.py` — new directory
matching the existing `tests/integration/` convention.

- Gated by `RUN_LIVE_KB=1` (same convention as the existing cold-start test).
- Each test does one live request (or two for subsumption). Total ~12 live tests; runtime under 30s.
- Live tests use a fresh `WikidataAdapter` instance (no shared state between tests).

### Mocked tests

Existing `tests/unit/test_wikidata_adapter.py` covers fixture mode and
stays green (F2 doesn't change fixture-mode behavior).

New `tests/unit/test_wikidata_live_failure_modes.py` covers live-method
error handling via mocked HTTP transport:

- Timeout retry / give-up
- Malformed response
- 429 retry with Retry-After
- Rate-limiter engagement

### Wiring tests

New `tests/integration/test_build_pipeline_config.py`:

- `build_pipeline(db, config=Config(...))` constructs a `WikidataAdapter` with the expected http_cache, llm_client, db, config.
- The configured User-Agent reaches the HTTP request headers (via a mocked transport that records headers).
- Setting `AEDOS_KB_REQUEST_DELAY_MS` enforces the configured delay (verified by timing two consecutive `_live_*` calls under mocked transport).

### F-009 test

Already specified in §8 — `tests/unit/test_purpose_table_completeness.py`.

### Validation run (the F2 acceptance commit)

The F2 acceptance commit re-runs the derivation corpus under `RUN_LIVE_KB=1` with the current `DEFAULT_MODEL_BY_PURPOSE` (gpt-4.1-mini for substrate, claude-haiku-4-5 for chat). Acceptance:

- 0 `NotImplementedError`s.
- Performance: derivation corpus completes in ≤ 30 minutes (50 cases × ~30s/case average, with HTTP caching reducing repeat-resolution cost).
- Spot check 3 traces: confirm KB calls happened (audit log has `kb_live_*` events), confirm purpose strings in traces match `DEFAULT_MODEL_BY_PURPOSE` keys exactly (F-009 verification), confirm HTTP cache hit count > 0.
- Rate-limit engagement: not required to fire during the corpus run (50 cases × ~3 KB calls = 150 requests; at 5/s SPARQL limit that's 30s minimum, easily within budget); confirm via a deliberate stress test that the limiter does engage when load exceeds 5/s.

---

## 11. Acceptance criteria

F2 lands when:

1. The four implementation commits (`_live_resolve`, `_live_lookup`, `_live_subsumption`, wiring) land green with their tests.
2. The two paired-housekeeping commits (purpose alignment, runbook hygiene) land green.
3. The validation commit re-runs the derivation corpus with no `NotImplementedError`s, confirms wiring via spot-checked traces, and confirms F-009 by purpose-strings-in-trace.
4. All existing tests stay green (~720 + new).
5. The F2 budget consumed (Wikidata is free; LLM cost for the validation run only): ≤ $15.

If any of the wiring-correctness verifications fail (item 3), F2 is not done.

---

## 12. Open questions for operator review

These are F2-scope decisions the design surfaces but operator may want to weigh in on before implementation begins. The plan recommends a decision per item; the operator confirms or pushes back.

### Q1 — Qualifier set scope

The design (§5) collects P580, P582, P642 in the SPARQL query and skips
other qualifier P-codes that some predicates' `slot_to_qualifier` may
reference. Skipped qualifiers cause false abstains (architecture §3.2
acceptable), not false verdicts.

**Operator decision:** confirmed — ship P580/P582/P642 only for F2.

**v0.16 follow-up (captured as D32):** qualifier coverage may need to
expand based on Phase 10.5 false-abstain rate analysis — specifically
watch which predicates abstain frequently in `kb_mapping_corpus` and
`derivation_corpus`. If a predicate-specific qualifier pattern shows up
in the abstain mode, v0.16 extends to dynamic per-predicate qualifier
discovery via `slot_to_qualifier`. Phase 10.5's measurement data is the
trigger.

### Q2 — Subsumption establishing-property follow-up query

The design (§6) runs ASK queries for direction detection and a follow-up
SELECT to identify the establishing property. The follow-up adds one
query per non-unrelated subsumption call.

**Recommendation:** ship the follow-up. The information is useful for
traces and the cost is small (one extra fast query per positive
verdict).

**Alternative:** skip the follow-up; set `establishing_property = None`
when ASK is true. Faster but loses information in traces.

### Q3 — Rate-limiter scope (threading)

The design (§7.4) ships a single-threaded rate limiter. The deployed
pipeline is single-threaded; the calibration runner runs sequentially.

**Operator decision:** confirmed — single-threaded for F2. **Refinement:**
limiter state lives as an instance attribute on the `WikidataAdapter`
(`self._sparql_limiter`, `self._search_limiter`), not as a
module-level global. This is the design as written in §7.4 — confirming
explicitly so the choice is durable: future lock-protection becomes a
small change (add a `threading.Lock` to `RateLimiter.__init__`, wrap
`acquire`'s `_last_call` mutation in it) rather than a refactor of where
the state lives.

### Q4 — Dead-key disposition

The design (§8) removes `walker` from `DEFAULT_MODEL_BY_PURPOSE` and
keeps `extractor:assistant` as "reserved" with a comment.

**Operator decision:** confirmed. **Refinement on the comment text:**
the reservation rationale must be durable beyond a future operator
wondering "why is this dead key still here." The comment in
`DEFAULT_MODEL_BY_PURPOSE` reads:

```python
# extractor:assistant is architecturally distinct from extractor:user
# for asserting-party reasons (the asserting party for an
# assistant-extracted claim is the assistant / deployment, not the
# user). Pinned here even though no call site currently uses it, so
# the architectural distinction is preserved in configuration. See
# architecture §4.1 for the asserting-party rationale.
"extractor:assistant": {"model": "gpt-4.1", **_OPENAI},
```

The text makes the reservation reasoning explicit and architecturally
grounded, not "TODO: maybe someday."

---

## 13. Implementation order

Per F1 §3, with refined per-commit scope:

1. **`Phase F2: _live_resolve implementation`** (3-4h)
   - Implement method per §4.
   - Live tests #1-4, mocked tests #5-7.
   - Does *not* depend on §7 wiring (the WikidataAdapter constructor
     still accepts http_cache=None; the method works against a directly-
     constructed CachingHTTPClient in tests).

2. **`Phase F2: _live_lookup implementation`** (4-6h)
   - Implement method per §5.
   - Live tests #1-4, mocked tests #5-6.
   - Extract `_parse_statement_bindings` helper shared with fixture path.

3. **`Phase F2: _live_subsumption implementation`** (3-4h)
   - Implement method per §6.
   - Live tests #1-4, mocked tests #5-6.

4. **`Phase F2: WikidataAdapter wiring through build_pipeline`** (2-3h)
   - Implement §7 (Config threading, HTTP cache construction, User-Agent,
     rate limiter).
   - Wiring tests per §10.
   - First commit where the deployed pipeline (`app.py /chat`,
     `benchmark.py`) reaches live Wikidata.

5. **`Phase F2: purpose-string alignment`** (1-2h)
   - Implement §8.
   - F-009 verification test.

6. **`Phase F2: runbook hygiene`** (<0.5h)
   - F-022 (no code change; the §7.4 limiter already reads the env var).
   - F-023 (one-paragraph edit).

7. **`Phase F2: live KB integration validated against derivation corpus`** (2-3h)
   - Run derivation corpus under `RUN_LIVE_KB=1`.
   - Verify F-009 via trace inspection.
   - Verify HTTP cache hit count.
   - Document the run in a brief `docs/phase_F/f2_validation_log.md`.

Total F2: 16-23 hours (within F1's 17-26 estimate).

After F2: F3 design doc → F3 implementation → F4.

---

## 14. Out of F2 scope (recap)

For clarity, items the audit might assume F2 touches but it does not:

- **D9 verification_context plumbing** — v0.16
- **D10 Tier U → Python composition** — v0.16
- **D13 KB-grounded retraction via cache-row trace edges** — v0.16
- **D14 retraction cascade + re-derivation** — v0.16
- **D15 ContradictionTracer wired into build_pipeline** — v0.16
- **D29 periodic consistency-check scheduler** — v0.16
- **D30 external-correction ingress API** — v0.16
- **D31 resolution-cache audit endpoint** — v0.16
- **F-015 Python sandbox hardening** — F3 (operator-elevated unconditionally)
- **F-024/F-025/F-026/F-027 Config threading for non-KB fields** — F3
- **F-013 app.py `.env` loading** — F3 if scope permits

F2 stays focused.

---

*End of Phase F2 design document.*
