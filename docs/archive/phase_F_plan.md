# Phase F — Plan (deployment readiness audit and live-integration implementation)

A discrete phase between Phase E's open-weight model comparison and Phase 10.5's
calibration pass. Phase E surfaced that `WikidataAdapter._live_resolve`,
`_live_lookup`, and `_live_subsumption` raise `NotImplementedError` — every
derivation-corpus case fails immediately when `RUN_LIVE_KB=1` is set, which the
Phase 10.5 runbook *requires* in Step 1.

The headline item is the Wikidata live implementation. The broader concern is
the audit-chain pattern: ten rounds of audit and fix-up verified the
*verification* pipeline's correctness in detail but never asked "does the
deployed pipeline actually reach its external services?" Phase E surfaced the
live-KB stub because it tried to exercise the system end-to-end; the same kind
of gap may exist elsewhere. Phase F's discipline is to find them all before
Phase 10.5 begins, not after.

This plan is about **F1 (the audit) and the ambiguities the audit will need to
resolve**. F2 (Wikidata implementation) starts with its own design document
written after F1 produces its inventory. F3's scope is determined by F1's
findings. F4 is end-to-end validation against real services.

---

## Why F1 is the gate

The audit chain (audit → fix-up → re-audit, Phases A–D) treated deployment
readiness as out of scope. D24 already named the pattern: "audit the
measurement instrument, not just the production code." Phase F's audit extends
the same discipline to the external-service integration surface — the substrate
between Aedos's internal correctness (which is well-audited) and the Phase 10.5
runbook's real-world execution (which has never been exercised end-to-end).

Three concrete pieces of evidence that an audit is warranted *before* writing
implementation code:

1. **The known stub.** `kb_wikidata.py:208-221` — three `_live_*` methods that
   raise `NotImplementedError`. Phase E's derivation runs produced 20 cases of
   exactly this error when `RUN_LIVE_KB=1` was set.
2. **The constructor mismatch.** `WikidataAdapter.__init__` (`kb_wikidata.py:62`)
   accepts `http_cache`, `llm_client`, `db`, `config`, `fixture_dir`. The
   pipeline assembler (`pipeline.py:71`) constructs it with *zero* arguments:
   `kb = kb if kb is not None else WikidataAdapter()`. Even if the live methods
   existed, they would not receive an HTTP cache, an LLM client, or a config.
   `Config` defines `wikidata_sparql_endpoint`, `wikidata_search_endpoint`,
   `wikidata_subsumption_depth`, and `wikidata_candidate_pool_size`
   (`config.py:39-43`), none of which currently reach the adapter.
3. **The pattern that produced item 2.** Items 1 and 2 are not isolated. The
   audit chain verified *invariants* about pipeline behavior (extraction
   correctness, oracle row consistency, retraction propagation) but never
   verified that the wiring it audited reaches its external services with the
   configuration it requires. The same wiring-level oversight that produced
   item 2 may exist elsewhere (the LLM-client/Anthropic path, the audit log,
   the chat wrapper).

F1 turns this from suspicion into an inventory.

---

## F1 methodology

For each integration point, three questions:

1. **Is it implemented?** A method that raises `NotImplementedError` is not
   implemented. A method whose body works only under mock conditions
   (e.g., reads fixture files and there is no live path) is not implemented
   for production. A method whose body works but is never *called* by the
   deployed pipeline (e.g., a constructor accepts a config that nothing
   provides) is half-implemented and is the same class of defect.

2. **Is it tested against the real service?** Search the test suite for
   tests that exercise the real external service. Mocked unit tests are not
   sufficient for this question — the *whole reason* Phase F is needed is
   that mocked tests missed the live-KB stub. A live test gated by
   `RUN_LIVE_KB=1` / `RUN_LIVE_TESTS=1` counts only if it actually exercises
   the live path (not a `pytest.skipif` that hides a stub).

3. **What does the architecture specify?** If the implementation differs
   from architecture §6.2 (KB protocol), §6.3 (Python verifier sandbox),
   §6.4 (walker resource budgets), §7.3 (retraction propagation), §9
   (Wikidata reference implementation) — that is a deployment-readiness
   gap. Categorize the divergence: capability (the implementation lacks a
   specified capability), behavior (the implementation does the wrong
   thing under some condition), or wiring (the capability exists in code
   but is not reachable from the deployed pipeline).

The methodology is **exhaustive enumeration**, not pattern-matching. Walk
every file in `src/aedos/` once; for every external-service touch point and
every configuration field, ask the three questions. Initial impressions from
reading the inputs are below; the audit produces the canonical list.

### What I have already seen (initial impressions, not the audit)

These are noted in this plan so they're not lost during the formal audit
walk. The audit produces the canonical inventory; this list seeds it.

**Wikidata (the known item).**
- `_live_resolve`, `_live_lookup`, `_live_subsumption` raise
  `NotImplementedError` (`kb_wikidata.py:208-221`).
- `build_pipeline` constructs `WikidataAdapter()` with no arguments
  (`pipeline.py:71`). The constructor's `http_cache`, `llm_client`, `db`,
  `config`, and `fixture_dir` parameters are unused in deployment.
- `Config.wikidata_sparql_endpoint`, `wikidata_search_endpoint`,
  `wikidata_subsumption_depth`, `wikidata_candidate_pool_size` are defined
  but never read by anything that matters in the production path.
- `CachingHTTPClient` exists (`utils/http_cache.py`) and has working
  ETag-conditional logic, but the Wikidata adapter never receives one.

**LLM client (Anthropic path).**
- `purpose="chat"` routes to the Anthropic SDK (Haiku 4.5). Mocked unit
  tests exercise this via `_transport`. **No live test exercises the
  Anthropic path against the real API in the chat-wrapper flow.** The
  Phase E comparison harness ran calibration corpora directly (not
  through `/chat`), so the chat-draft → extract → verify → intervene
  sequence has never executed end-to-end against real Anthropic +
  real Wikidata.
- `purpose=` overrides (`extractor:user`, `extractor:assistant`, the
  five `substrate:*`, `python_verifier`, `walker`) route to OpenAI by
  default. The Phase E migration to open-weight models is *operator
  work* (E3) and has not landed; if Phase E5 lands before Phase F2, the
  `DEFAULT_MODEL_BY_PURPOSE` table will be different by then.

**Python verifier sandbox.**
- Architecture §6.3: "Restricted Python with standard library plus an
  allow-list of stable deterministic packages: datetime, math, decimal,
  fractions, statistics, re, unicodedata, string, plus a small
  per-deployment-approved set. **No file I/O, no network, no subprocess.**"
- Implementation (`utils/sandbox.py`): AST scan for `import` / `import
  from` against an allow-list, then subprocess execution with a
  minimal env. The AST scan does **not** catch dynamic-import patterns
  (`__import__("os")`, `getattr(__builtins__, "__import__")`,
  `importlib.import_module`). Once the code is past the AST scan, the
  subprocess inherits the system Python and can do anything Python can do
  — including read files, open sockets, and spawn further subprocesses.
- The architecture's "no file I/O, no network, no subprocess" reads as a
  *capability* constraint on the generated code, not on the sandbox
  itself. The current implementation satisfies it only for code that
  follows the static-import convention. LLM-generated code that
  bypasses static import (deliberately or accidentally) is unrestricted.

**Audit log persistence.**
- `audit_log` table is created (`database.py:84`).
- `log_event` writes into it from `tier_u.py`, `predicate_translation.py`,
  `subsumption.py`, `predicate_distribution.py`, `consistency.py`,
  `aggregator.py`, `retraction.py`, `contradiction_tracer.py`. Phase A's
  D8 cleanup (`cefe65f`) and Phase C's D22 cleanup (`65833f0`) unified
  the logging interface; the events fire in the deployed pipeline.
- `app.py` exposes `/audit/*` endpoints reading from this table.
- I have not verified end-to-end that a `/chat` call produces the audit
  events that the architecture says it should. Test coverage is per-module
  unit tests plus `tests/integration/test_oracle_audit_logging.py`. No
  test exercises the full `/chat` → audit-log-row path against the real
  database file used by `app.py`.

**Caching and persistence.**
- `entity_resolution_cache` table exists and is read/written by
  `resolver.py`. Trace edges do not yet reference cache rows (D13, still
  deferred), so cache invalidation via downstream contradiction tracing
  cannot reach a cached resolution. This was flagged as v0.16 work but is
  worth re-classifying under Phase F's lens: if the architecture's
  §7.3 over-time-soundness guarantee requires reaching cached resolutions,
  D13 is a deployment-readiness item, not a future-improvement item.
- There is no statement cache (no `kb_statement_cache` table). Architecture
  §9.1 specifies "Standard HTTP caching (ETag, conditional requests,
  in-process LRU)" — which `CachingHTTPClient` provides — so caching at
  the HTTP layer is the architecture's answer. But that cache is
  per-process (LRU in memory) and not wired to the adapter (item 1
  above). The architecture is silent on whether *parsed* statements
  should also be cached.

**Pipeline assembly.**
- `build_pipeline` (`pipeline.py:57`) is the canonical assembler. Used by
  `app.py` (for `/chat`) and by `tests/evaluation/benchmark.py` (for
  the medium-bar evaluation). Both call it with `db` and let it default
  `llm_client` and `kb`. **Neither passes a `Config`.**
- `ChatWrapper.respond` is correct as of `e2c8d45` (D18 resolution).
  Mocked end-to-end test exists (`tests/integration/test_chat_wrapper.py`).
  No live end-to-end test.

**Configuration / environment.**
- `RUN_LIVE_KB` is read in three places: `kb_wikidata.py` (sets the
  `_live` flag, then raises `NotImplementedError`), the cold-start test
  (`tests/cold_start/test_zero_seed_correctness.py:27`), and the
  benchmark live-mode gate (`benchmark.py:533`).
- `RUN_LIVE_TESTS` gates the cold-start test and the benchmark live mode.
- `.env.example` documents both flags. The `.env` loader is conftest-level
  in pytest; `app.py` does not load `.env` (it reads env vars directly).
  Whether the operator's deployment loads `.env` automatically is a
  deployment question.
- `AEDOS_KB_REQUEST_DELAY_MS` is mentioned in the Phase 10.5 troubleshooting
  guide (`phase_10_5_runbook.md:367`) but no code reads it. Either implement
  it for Phase F or remove the troubleshooting reference.
- `AEDOS_LLM_TEMPERATURE` is similarly mentioned in troubleshooting
  (`phase_10_5_runbook.md:371`) and no code reads it. Same disposition
  question.

These nine clusters are the seed. The audit walks `src/aedos/` and produces
the canonical inventory.

---

## F1 output format

`docs/phase_F/deployment_readiness_audit.md`:

### Section 1: Integration inventory table

| Integration point | File / call site | Status | Live-test coverage | Architecture-spec gap |
|---|---|---|---|---|
| (one row per touch point) | | implemented / stubbed / mock-only / wiring-gap / unknown | yes / no / partial | (description if any) |

Status definitions:
- **implemented** — body works against the real service, exercised by at
  least one live test.
- **stubbed** — body raises `NotImplementedError` or returns a placeholder.
- **mock-only** — body works against mock/fixture inputs but the live path
  does not exist or has never been exercised.
- **wiring-gap** — body works but the deployed pipeline does not invoke it
  with the necessary configuration (the `WikidataAdapter()` pattern).
- **unknown** — the audit could not determine the answer in the time
  allotted; flagged for follow-up.

### Section 2: Categorization

- **Must-implement for Phase 10.5.** Without these, Phase 10.5's runbook
  cannot execute as written.
- **Should-implement for Phase 10.5.** These would improve Phase 10.5's
  results or make the runbook honest but are not strict blockers.
- **Deferred to v0.16.** Items where the audit's verdict is "this is a
  real gap but Phase F is not the right place to address it." Each
  deferred item gets a paragraph explaining why deferral is the
  disciplined choice, not a stub.

### Section 3: Proposed implementation order

The must-implement list, ordered. Wikidata is the obvious starting point.
Subsequent items depend on what the audit surfaces.

### Section 4: Scope estimate

Per-item estimate of design + implementation + test + integration-validation
hours, with totals. The estimate informs the operator's check-in decision
between F1 and F2 (proceed at this scope vs. trim the must-implement list).

### Section 5: Discipline notes

Any audit findings about the audit itself — patterns the audit chain might
have missed, methodology improvements for v0.16's pre-release deployment
audit (the D24-companion item already named in the prompt).

---

## F2 — Wikidata live implementation (sketch)

After F1's audit lands and the operator confirms scope, F2 starts with a
design document at `docs/phase_F/wikidata_implementation_design.md`.

The architecture has decided several questions already. I am surfacing
these as *settled* in the plan so they don't get re-litigated in the design
doc; the design doc decides the open ones.

**Settled by architecture or by the existing fixture shape:**

- **Endpoint for `_live_lookup`:** SPARQL via `query.wikidata.org/sparql`.
  Architecture §9.1 names "WDQS at query.wikidata.org/sparql." The fixture
  shape (`sparql_P39_Q76.json` → `results.bindings[].value.value` /
  `qual_P580` / `qual_P582`) is the SPARQL JSON response format with
  qualifier projection; `_fixture_lookup` already parses this. The live
  implementation produces the same shape.
- **Endpoint for `_live_resolve`:** `wbsearchentities` via
  `wikidata.org/w/api.php`. Architecture §9.1 and §9.3 specify the search
  API with language filter and candidate pool size (10, in
  `Config.wikidata_candidate_pool_size`).
- **Subsumption depth:** 5–6 hops, configurable. `Config.wikidata_subsumption_depth = 6`.
- **Subsumption properties:** P31 (instance of), P279 (subclass of), P131
  (located in administrative entity), P361 (part of). Architecture §9.1.
- **Rank handling:** preferred preferred; normal as fallback; deprecated
  excluded. Architecture §9.1 and the existing fixture path
  (`kb_wikidata.py:141-142` filters deprecated).
- **Caching:** HTTP-level via `CachingHTTPClient`. Architecture §9.1.
  `LRUHTTPCache` already implements ETag-conditional GETs and TTL.

**Open for the design document (operator may want to weigh in):**

- **Type filtering at resolution.** Architecture §9.3 says "type filtering
  driven by predicate metadata." `wbsearchentities` does not accept a type
  filter natively; post-filtering means fetching each candidate's P31
  chain (extra HTTP call per candidate) or relying on the resolver's
  ambiguity logic (`EntityResolver.select`'s `_AMBIGUITY_GAP` LLM-mediated
  selection). The design doc picks one. Both are defensible. **Recommended:
  no per-candidate P31 fetch in F2 — rely on candidate ordering by search
  relevance and let `EntityResolver.select` invoke its LLM disambiguation
  for the ambiguous-gap case. Type filtering becomes a v0.16 candidate if
  empirical data shows it helps.** (Discipline: don't add a feature whose
  benefit cannot be measured at v0.15 calibration time.)

- **Rate limiting.** Wikidata's User Agent policy and SPARQL endpoint
  guidelines suggest ~5 req/s for SPARQL, ~50 req/s for the search API,
  with retry-after for 429s. The Phase 10.5 troubleshooting guide
  mentions `AEDOS_KB_REQUEST_DELAY_MS` but no code reads it. The design
  doc decides whether to add client-side rate limiting (likely yes,
  conservative ~5 req/s for SPARQL with a configurable knob; the
  troubleshooting reference becomes accurate).

- **User-Agent.** `CachingHTTPClient` sets
  `User-Agent: Aedos/0.15 (claim-verification research)`. Wikidata's
  User-Agent policy requires contact info (URL or email). The design doc
  decides what to put — likely a deployment-configurable string with a
  sensible default. Phase F's identity here is a deployment question
  (whose contact info?); the operator should weigh in.

- **Subsumption two-direction check.** Given `entity_a` and `entity_b`,
  the verdict is one of `a_subsumed_by_b`, `b_subsumed_by_a`, `equivalent`,
  `unrelated`. A single SPARQL query keyed on `entity_a` only checks the
  first direction. The design doc decides: two queries (one per direction),
  one query with `UNION` (efficient but harder to parse), or accept
  asymmetry and have callers swap arguments. **Recommended: one SPARQL
  query with `UNION` returning both directions; equivalent is the case
  where both paths exist at depth ≤ 1.** Discipline reason: the caller
  should not have to know which direction to swap.

- **`slot_to_qualifier` for inverse-direction predicates in the live
  path.** D19 was resolved in fixup-3 — `KBVerifier` consults
  `slot_to_qualifier` and swaps lookup direction for inverse predicates.
  The live `_live_lookup` is *direction-neutral* (it looks up whatever
  entity it is given against whatever property it is given). So this is
  not an F2 issue — but the live integration test for `capital_of` /
  `mother_of` should exercise the inverse-direction code path to confirm
  D19's fix continues to hold under live data.

- **Polarity handling for KB lookups.** Wikidata stores positive
  statements. The architecture and `KBVerifier._apply_polarity` already
  do the right thing (polarity flipping at the verifier level). No F2
  decision required.

- **Persistent statement cache.** The architecture is silent. A per-process
  LRU HTTP cache survives only the current process; if Phase 10.5 runs
  multiple test sessions, each pays full Wikidata cost on the first
  request. **Recommended: stay with HTTP-cache only for F2; defer
  persistent statement cache to v0.16.** Discipline reason: architecture
  did not require it, and Phase 10.5's expected runtime (the runbook's
  total estimate of 6–9 hours) is dominated by LLM calls, not by Wikidata
  calls. The persistent cache adds complexity for a non-bottleneck.

- **Error handling philosophy.** Architecture §9.4 specifies failure modes:
  "Entity not found → empty candidates → abstention." / "SPARQL timeout
  → retry once with backoff → escalate to abstain with explicit error
  trace." So the live methods *never* propagate exceptions to the
  walker — they return empty candidates / empty statement list /
  `unrelated` subsumption with a provenance note explaining why. The
  design doc spells out the retry/backoff policy (single retry, 1s
  initial backoff, then abstain).

### F2 implementation order

Per the prompt: three discrete commits, one validation commit.

1. `Phase F2: _live_resolve implementation` — `wbsearchentities` against
   the live API with the polite User-Agent, rate-limited, returning
   `ResolutionCandidate`s. Tests against the live API for at least:
   - the Obama → Q76 case (known disambiguation; Phase E surfaced
     `der_disambiguation_006` here).
   - a not-found case.
   - a network-failure case (simulated via mocked HTTP transport).
2. `Phase F2: _live_lookup implementation` — SPARQL via WDQS with
   qualifier projection (P580/P582 explicitly; default qualifiers
   collected). Tests for: P39 on Q76 (Obama's positions with date
   qualifiers), P36 on Q30 (capital of US, with the inverse-direction
   D19 case), a deprecated-statement case (verify exclusion), a
   not-found case, a timeout case (mocked).
3. `Phase F2: _live_subsumption implementation` — SPARQL property-path
   query over P31|P279|P131|P361 up to configured depth, two directions.
   Tests for: direct subclass (Q5 human ⊃ Q76 Obama), transitive multi-hop
   (Williams College → Williamstown → MA → US), cycle case (any pair
   known to have a cycle in Wikidata; verify termination), unrelated case.
4. `Phase F2: live KB integration validated against derivation corpus` —
   re-run derivation corpus under `RUN_LIVE_KB=1`, confirm no
   `NotImplementedError`s, spot-check three traces, performance sanity
   (< 30 minutes for the whole corpus, with caching).

The validation commit is the F2 acceptance gate. If the derivation corpus
takes longer than ~30 minutes or surfaces a structural-error-from-live
bug, F2 is not done.

### F2 testing discipline

Every implementation commit lands with at least one test that exercises
the real Wikidata API. These tests are gated by `RUN_LIVE_KB=1` (existing
convention). Mocked unit tests stay green (the fixture-mode path is
untouched by F2). The new live tests live under
`tests/integration/live/test_wikidata_live.py` (a new directory because
the existing `tests/integration/` does not have a live subdirectory and
the cold-start tests are organized separately). The directory name
matches `audit_report.md:290`'s reference to
`tests/v0_15/live/test_wikidata_live.py` from the original plan.

---

## F3 — sketch (scope determined by F1)

The structure is identical to F2: design doc → discrete implementation
commits with live-service tests → integration validation.

Likely candidates based on the pattern that produced the live-KB miss
(reading from the initial-impression list above):

- **`WikidataAdapter` wiring.** Even after F2's live methods exist, the
  `build_pipeline` call site (`pipeline.py:71`) must pass `http_cache` +
  `config` + `llm_client` to the adapter, and `Config` must be threaded
  to `build_pipeline`. This is the "wiring gap" status from F1's
  inventory — it's strictly part of making F2 reachable from the deployed
  pipeline, and may belong inside F2 rather than F3. The audit decides.
- **Python verifier sandbox hardening.** If F1 classifies the
  `__import__("os")` gap as a deployment-readiness concern (LLM-generated
  code that bypasses static import is a real failure mode, distinct from
  a *security* threat), F3 addresses it. Options: AST-walk that catches
  dynamic-import patterns (`__import__`, `importlib`, `getattr` on
  builtins) and rejects them; or switch to RestrictedPython; or accept
  current behavior and document the limitation. **Discipline call:**
  classify as F3 only if F1's evidence shows live Phase 10.5 cases that
  generate problem code; otherwise defer to v0.16.
- **Configuration loader.** `app.py` reads `Config.from_env()` but
  doesn't pass it to `build_pipeline`. `build_pipeline` doesn't accept a
  config parameter. So every configurable knob (`Config.walker_*`,
  `Config.wikidata_*`, `Config.circuit_breaker_threshold`,
  `Config.http_cache_*`) is dead in the deployed pipeline. F3 wires
  `Config` through `build_pipeline` into the components that need it.
- **`AEDOS_KB_REQUEST_DELAY_MS` / `AEDOS_LLM_TEMPERATURE`.** The Phase
  10.5 troubleshooting guide mentions both; no code reads either.
  Implement, or remove the troubleshooting references. F1 chooses.
- **Chat wrapper end-to-end live test.** D18 fixed the wrapper's
  extract call signature, but there is no test that exercises the real
  `/chat` flow against real Anthropic + real Wikidata. F3 adds one
  (single case, manually validated) and adds it to the live-test gate.
- **D13 (KB-grounded retraction).** Architecture §7.3 says cached
  resolutions are subject to retraction. The code can soft-delete cache
  rows (`resolver.retract_cache_entry`) but trace edges don't reference
  cache row ids, so contradiction tracing cannot reach them. v0.16
  candidate as currently classified, but Phase F may reclassify if F1
  surfaces this as a deployment-readiness gap.

### F3 scope decision

The operator confirms the F3 scope after F1's audit lands and before any
F3 implementation begins. If F1 surfaces work beyond what is sketched
above, surface it before starting — don't unilaterally expand scope. The
prompt's discipline note: "No 'I'll come back to this' implementations.
Stubs are how we got here."

---

## F4 — End-to-end validation against real services

Per the prompt:

- Fresh database, 61 seeds loaded, Tier U Asa-rows seeded per Phase 10.5
  Step 3.
- `RUN_LIVE_TESTS=1`, `RUN_LIVE_KB=1`. Real `ANTHROPIC_API_KEY` and either
  Phase E5's `DEFAULT_MODEL_BY_PURPOSE` or the OpenAI default (depending
  on whether E5 has landed by F4 time).
- One case, manually traced. The case selection criteria are explicit:
  must exercise `_live_resolve`, `_live_lookup`, and `_live_subsumption`,
  and must have a known expected verdict.

Candidate cases (the design doc may pick one; this is a sketch):
- `der_disambiguation_006` — surfaces the Obama → Q76 vs Q842926
  disambiguation; exercises `_live_resolve`. Picked by Phase E as
  illustrating wrong-entity selection; useful for F4 because the
  resolution-walker-verdict chain is short enough to trace by hand.
- A multi-hop `der_geographic_*` case — exercises subsumption traversal.
- A `kb_mapping_*` case — exercises `_live_lookup` with qualifier scope.

The validation deliverable is `docs/phase_F/end_to_end_validation.md`
containing the captured trace and the verification narrative. The commit
that lands it tags `v0.15.0-rc.8`. Phase 10.5 begins from rc.8.

---

## Check-ins and operator decisions

Per the prompt's "four major check-in points":

1. **After F1, before F2.** The audit's inventory and proposed scope.
   The operator confirms which items to schedule, which to defer, and
   approves the LLM API budget for F2 (Wikidata is free, but live
   integration testing against open-weight models via OpenRouter or
   against Anthropic for the chat-wrapper test costs money).
2. **After each F2 implementation commit.** The next implementation
   waits for the previous to land with its live tests passing.
3. **After F3 implementation.** Confirm the implementation matches what
   the audit specified.
4. **At F4.** The end-to-end validation case selection, the trace, and
   the rc.8 tag.

Phase E5's status at the start of F2 affects which models drive the F2
integration tests. The plan does not assume E5 has landed; the F2 design
doc explicitly states the model configuration F2 uses.

---

## Budget estimate (the prompt asked for $20–50 across all phases)

| Phase | API consumer | Approximate budget |
|---|---|---|
| F1 | (audit only, no code) | $0 |
| F2 _live_resolve tests | Wikidata only (free) | $0 |
| F2 _live_lookup tests | Wikidata only (free) | $0 |
| F2 _live_subsumption tests | Wikidata only (free) | $0 |
| F2 derivation-corpus validation | LLM × Wikidata, full corpus (≈ 50 cases) | $5–15 |
| F3 (depends on scope) | Chat-wrapper live test if scoped in | $1–3 |
| F4 single case end-to-end | Anthropic chat draft + OpenAI/OpenRouter substrate + Wikidata | $0.50–2 |
| | **Total** | **$7–20** |

Below the prompt's $20–50 estimate. The bulk is F2's validation run; F4's
single case is cheap. The operator authorizes once at the start of F2
rather than per-test.

The budget excludes Phase 10.5's Step 6 (medium-bar evaluation, 122 cases
× 3 runs) — that is Phase 10.5 cost, not Phase F cost.

---

## Discipline pattern shifts (the prompt called these out)

- **Design before implementation.** F2 starts with a design doc. F3
  starts with a design doc. F1's inventory is the design input for both.
- **Test against real services.** Mocked tests are necessary but not
  sufficient for Phase F. Every implementation commit has a live test.
- **Surface scope changes.** If F1 finds work beyond the sketches above,
  it goes into F1's report and the operator approves scope before F3
  starts.
- **Cumulative cost matters.** Authorized once at F2 start, tracked but
  not re-litigated per-test.
- **No "I'll come back to this."** Stubs are how we got here. Surface
  the work or defer it formally; do not slip a `# TODO` into the build.

---

## v0.16 plan delta candidate (the prompt asked for one)

To be recorded in `docs/v0.16_planning.md` after F1 lands. Working title:
**D26 — Pre-release deployment-readiness audit as a standard discipline
pattern.** The audit chain's nine rounds verified the verification pipeline
extensively but never asked whether the wired pipeline reached its
external services with the configuration each service requires. Phase E
surfaced the live-KB stub by exercising the system end-to-end, and Phase F
extended the audit to the full integration surface. v0.16's plan should
treat "the deployed pipeline reaches every external service it depends on,
with tests against each" as a standard pre-release gate alongside D24's
runner-vs-corpus audit. The two are companion gates: D24 audits the
measurement instrument, D26 audits the deployed system; together they
turn end-to-end correctness from a hope into a checked invariant.

---

## Ambiguities surfaced

These are the explicit decisions the operator may want to weigh in on,
either at the F1 check-in or at the F2 design-doc check-in. The plan
makes a recommendation for each but is not committing without operator
sign-off.

- **Type filtering at resolution.** Recommended off (rely on
  `EntityResolver.select`'s LLM-mediated disambiguation). Trade-off:
  more LLM calls for ambiguous cases vs. additional per-candidate
  HTTP cost.
- **Rate limiting at the Wikidata adapter level.** Recommended on
  (~5 req/s SPARQL, ~50 req/s search) with `AEDOS_KB_REQUEST_DELAY_MS`
  honored. Trade-off: slightly slower live runs vs. risk of being
  rate-limited mid-corpus.
- **User-Agent contact info.** Recommended deployment-configurable with
  a stub default; operator may want to set a specific contact for
  Aedos's research use.
- **Persistent statement cache.** Recommended deferred to v0.16
  (architecture did not require it; not Phase 10.5's bottleneck).
- **Python sandbox hardening.** Recommended deferred to v0.16 unless F1
  surfaces concrete generated-code failure modes from Phase E that
  bypass the AST scan. Trade-off: deploying with a known limitation
  vs. expanding F3 scope.
- **`AEDOS_KB_REQUEST_DELAY_MS` / `AEDOS_LLM_TEMPERATURE`.** Either
  implement (small) or remove the runbook references. Recommended:
  implement the KB one (it's tied to rate limiting and is small);
  remove the LLM-temperature reference (temperature is not a per-purpose
  knob in the current client and adding one is out of scope).
- **D13 reclassification.** Recommended: keep deferred to v0.16 — Phase
  10.5 does not exercise downstream contradiction tracing against
  cached resolutions at the scale that would surface the gap. F4's
  single-case validation will not exercise it either. The gap is real
  but Phase F is not the right phase to address it.

---

*End of Phase F plan.*
