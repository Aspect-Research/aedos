# Phase F1 — Deployment Readiness Audit

*Output of Phase F1. No implementation; this document drives F2 and F3.
Per `docs/phase_F_plan.md`, this audit walks the external-service
integration surface of v0.15 and produces an inventory the operator
confirms before any implementation begins.*

---

## Methodology

For each integration point, three questions:

1. **Is it implemented?** A method that raises `NotImplementedError` is
   not implemented. A method that works only against mock inputs is
   mock-only. A method whose body works but the deployed pipeline does
   not invoke it with the configuration it needs is a wiring-gap.
2. **Is it tested against the real service?** Mocked unit tests do not
   count. A live test gated by `RUN_LIVE_KB=1` / `RUN_LIVE_TESTS=1`
   counts only if it actually exercises the live path and does not
   `pytest.skip` over a stub.
3. **What does the architecture specify?** Capability gaps, behavior
   gaps, and wiring gaps are categorized separately.

Status categories:
- **implemented** — body works against the real service, live test exists
- **stubbed** — body raises `NotImplementedError`
- **mock-only** — body works against mock/fixture inputs, no live path
- **wiring-gap** — body works but the deployed pipeline does not reach
  it with the configuration it requires
- **partial** — implementation exists but does not cover the full
  surface architecture specifies
- **out-of-policy** — implementation is in the deployed pipeline but
  violates an external policy (e.g. Wikimedia User-Agent)

The audit walked `src/aedos/` (15 files plus subdirectories), the
testing configuration (`tests/conftest.py`, `tests/calibration/`,
`tests/integration/`, `tests/cold_start/`), the deployment-relevant
docs (`.env.example`, `docs/phase_10_5_runbook.md`), and every reference
to `os.environ` / `os.getenv` in `src/`. Architecture cross-checks against
`docs/architecture.md` §§4.6, 5.1, 5.4, 6.2, 6.3, 6.4, 7.3, 9.

**Scope-distinction note.** The audit ran in two modes:

- **Mode 1: existing code surface.** Every integration point the existing
  code references — `WikidataAdapter`, `LLMClient`, `Python` sandbox,
  audit log, `Config`, env vars, pipeline wiring. The first pass walked
  this mode and produced Section 1.1 through Section 1.7.
- **Mode 2: architecture-required surface.** Integration points the
  architecture specifies that may not exist in code at all. The second
  pass walked architecture §§4.6, 5.4, 7.3, 9.5 looking for specified-
  but-not-implemented surfaces. Three found; recorded in Section 1.8.

Both passes are necessary for a complete deployment-readiness audit. The
operator surfaced the distinction at the F1 review check-in; Mode 2 is
captured as part of F1's record so the audit's coverage is honest about
what it did and did not check.

---

## Section 1 — Integration inventory

### 1.1 Wikidata KB protocol

| ID | Integration point | File / call site | Status | Live-test | Architecture-gap |
|---|---|---|---|---|---|
| F-001 | `_live_resolve(reference, local_context)` | `kb_wikidata.py:208-211` | stubbed | no | §6.2 / §9.3 capability missing |
| F-002 | `_live_lookup(entity, predicate)` | `kb_wikidata.py:213-216` | stubbed | no | §6.2 / §9.1 capability missing |
| F-003 | `_live_subsumption(entity_a, entity_b, relation_type)` | `kb_wikidata.py:218-221` | stubbed | no | §6.2 / §9.1 capability missing |
| F-004 | `WikidataAdapter` constructed without args in deployed pipeline | `pipeline.py:71` | wiring-gap | no | Constructor's `http_cache`, `llm_client`, `db`, `config` parameters unused — even if F-001..003 were implemented, they would not receive HTTP caching or configurable endpoints |
| F-005 | `Config.wikidata_sparql_endpoint`, `wikidata_search_endpoint`, `wikidata_subsumption_depth`, `wikidata_candidate_pool_size` | `config.py:39-43` | wiring-gap | no | Defined but never read in deployment |
| F-006 | `CachingHTTPClient` for KB requests | `utils/http_cache.py` | wiring-gap | no | §9.1 specifies ETag / LRU caching; class exists, never instantiated for KB |
| F-007 | Wikimedia User-Agent policy compliance | `utils/http_cache.py:91-93` | out-of-policy | n/a | UA is `Aedos/0.15 (claim-verification research)` — no contact info. Wikimedia's policy [meta.wikimedia.org/wiki/User-Agent_policy] requires URL or email |
| F-008 | Client-side rate limiting | (does not exist) | not implemented | n/a | No knob; runbook references `AEDOS_KB_REQUEST_DELAY_MS` (F-022) which no code reads |

### 1.2 LLM client (Anthropic + OpenAI + OpenRouter)

| ID | Integration point | File / call site | Status | Live-test | Architecture-gap |
|---|---|---|---|---|---|
| F-009 | **Purpose-string mismatch — 4 of 9 routes broken** | (see narrative below) | wiring-gap | no | Call sites use purpose strings that do not match `DEFAULT_MODEL_BY_PURPOSE` keys; calls fall through to the chat-default fallback, not the gpt-4.1-mini default the table documents |
| F-010 | `chat_wrapper.py:90` Anthropic chat path | `purpose="chat"` → `claude-haiku-4-5` | implemented | no | Untested against real Anthropic in any deployed-pipeline flow |
| F-011 | `extract_with_tool` Anthropic branch | `llm/client.py:447-465` | implemented | no | Anthropic SDK tool-use path; reachable via `AEDOS_MODEL_<purpose>` for Anthropic-routed purposes; never exercised live |
| F-012 | `extract_with_tool` OpenAI-compatible branch | `llm/client.py:407-446` | implemented | partial | Exercised by Phase E comparison via OpenRouter; not by any deployed-pipeline live test |
| F-013 | `app.py` does not load `.env` | `app.py:27` (`Config.from_env()` reads `os.getenv` directly) | partial | n/a | Operator must export env vars in shell or use `--env-file` on the uvicorn invocation. `.env.example` instructs both options but no in-process loader exists |
| F-014 | `_TEMPERATURE_DEPRECATED_PREFIXES` completeness | `llm/client.py:56` | partial | n/a | Only `claude-opus-4-7`; Sonnet 4.6+ also deprecated temperature. Minor; only fires if `rewrite(temperature=...)` is called explicitly, which the deployed pipeline does not |

**F-009 narrative.** Four of the nine purposes in `DEFAULT_MODEL_BY_PURPOSE`
have no matching call site, and four call sites use purpose strings that
are not in `DEFAULT_MODEL_BY_PURPOSE`:

| Call site purpose | File | Table key | Match? |
|---|---|---|---|
| `extractor:user` | `extractor.py:115` | `extractor:user` | ✓ |
| `substrate:predicate_translation` | `predicate_translation.py:214` | `substrate:predicate_translation` | ✓ |
| `chat` | `chat_wrapper.py:90` | `chat` | ✓ |
| `subsumption_generation` | `subsumption.py:243` | `substrate:subsumption` | ✗ |
| `distribution_generation` | `predicate_distribution.py:162` | `substrate:predicate_distribution` | ✗ |
| `entity_selection` | `resolver.py:102` | `substrate:entity_resolution` | ✗ |
| `python_code_generation` | `python_verifier.py:90` | `python_verifier` | ✗ |
| (no call site) | n/a | `extractor:assistant` | (dead key) |
| (no call site) | n/a | `walker` | (dead key) |

Consequence under deployment: when a call uses an unmatched purpose
(`subsumption_generation`, `distribution_generation`, `entity_selection`,
`python_code_generation`), `_resolve_purpose_config` falls through to
`_config_for_model(fallback_model)` where `fallback_model` is
`self.model` (defaults to `claude-haiku-4-5`). So **four substrate /
verifier call types route to Haiku 4.5 by default, not to `gpt-4.1-mini`
as the table documents and the `.env.example` claims.**

This affects what Phase 10.5's `RUN_CALIBRATION=1` runs actually measure:
the runbook's expectation is the table's gpt-4.1-mini default; the actual
behavior is Haiku 4.5 for half the substrate. The Phase E comparison
harness side-steps this via `AEDOS_OVERRIDE_MODEL_BY_PURPOSE: {"*": ...}`
because the `*` wildcard catches all non-`chat` purposes regardless of
name — so the comparison data is honest. But Phase 10.5's *default* run
(no override) measures something different from what the documentation
promises.

This is the canonical wiring-gap pattern Phase F exists to surface. It
is a direct deployment-readiness defect; the prior audit chain caught
the *symptoms* (Phase E surfaced anomalies in the comparison data, which
were absorbed as model-behavior questions) but not the *cause*.

### 1.3 Python verifier sandbox

| ID | Integration point | File / call site | Status | Live-test | Architecture-gap |
|---|---|---|---|---|---|
| F-015 | AST import scan + subprocess isolation | `utils/sandbox.py:65-83`, `86-172` | partial | n/a | §6.3 specifies "no file I/O, no network, no subprocess." AST scan catches `import os` but not `__import__("os")`, `importlib.import_module("os")`, or `getattr(__builtins__, "__import__")`. Subprocess execution provides isolation from the parent process but not from system resources. Operator-elevated to F3 unconditionally |
| F-016 | Allowed-module set | `utils/sandbox.py:23-32` | implemented | n/a | Matches §6.3's stdlib list exactly |
| F-017 | Generated-code path: LLM → claim runner → harness → subprocess | `python_verifier.py:80-160` | implemented | no | Live LLM call gated only by `purpose="python_code_generation"` (which falls through per F-009); subprocess execution is deterministic given inputs |

### 1.4 Audit log persistence

| ID | Integration point | File / call site | Status | Live-test | Architecture-gap |
|---|---|---|---|---|---|
| F-018 | `audit_log` table schema | `database.py:84-91` | implemented | n/a | Schema correct |
| F-019 | `log_event` write path | `audit/log.py:15-31` | implemented | yes (integration) | 8 source-side modules write events; `tests/integration/test_oracle_audit_logging.py` exercises the path |
| F-020 | `/audit/*` query endpoints | `app.py:48-87` | implemented | yes (integration) | `tests/integration/test_audit_endpoints.py` |
| F-021 | `verification_context` parameter of `log_event` | (most call sites pass `None`) | partial | n/a | D9 plumbing — `verification_context` is rarely populated at call sites. Architecture §7.3 wants every audit event during a verification to reference its verification id. Existing v0.16 candidate; not elevated by F |

### 1.5 Configuration / environment

| ID | Integration point | File / call site | Status | Live-test | Architecture-gap |
|---|---|---|---|---|---|
| F-022 | `AEDOS_KB_REQUEST_DELAY_MS` referenced by runbook | `phase_10_5_runbook.md:367` | not implemented | n/a | No code reads the env var. Operator confirmed: implement (ties to rate-limit decision B) |
| F-023 | `AEDOS_LLM_TEMPERATURE` referenced by runbook | `phase_10_5_runbook.md:371` | not implemented | n/a | No code reads the env var; `LLMClient` has no per-purpose temperature knob. Operator confirmed: remove from runbook |
| F-024 | `Config.from_env()` instantiated in `app.py`, never passed to `build_pipeline` | `app.py:27`, `pipeline.py:57` | wiring-gap | n/a | `build_pipeline` signature does not accept a `Config`; every field of `Config` is dead in the deployed pipeline (see F-025..F-029) |
| F-025 | `Config.walker_wall_clock_seconds`, `walker_max_llm_calls`, `walker_max_depth` | `config.py:31-33` | wiring-gap | n/a | `Walker.__init__` accepts `config: dict`, never gets one; uses hardcoded `WalkerBudget()` and `_DEFAULT_MAX_DEPTH=4` |
| F-026 | `Config.circuit_breaker_threshold` | `config.py:36` | wiring-gap | n/a | `ConsistencyChecker.__init__` accepts `config: dict`, never gets one; uses `_DEFAULT_CIRCUIT_BREAKER_THRESHOLD=3` |
| F-027 | `Config.http_cache_*` | `config.py:26-28` | wiring-gap | n/a | No `CachingHTTPClient` constructed in the deployed pipeline (see F-006) |
| F-028 | `Config.wikidata_*` | `config.py:39-43` | wiring-gap | n/a | See F-005 |
| F-029 | `Config.seed_file` | `config.py:45` | wiring-gap | n/a | Seed loading is done by `seeds/load_seeds.py` as a standalone script (per Phase 10.5 runbook Step 2); the deployed pipeline does not consult `seed_file` |

### 1.6 Pipeline / chat-wrapper integration

| ID | Integration point | File / call site | Status | Live-test | Architecture-gap |
|---|---|---|---|---|---|
| F-030 | `ChatWrapper.respond` extract → walk → aggregate | `chat_wrapper.py:80-144` | implemented | no | D18 resolved; mocked end-to-end test exists; no live test against real Anthropic + real Wikidata + real LLM substrate |
| F-031 | `build_pipeline` assembly | `pipeline.py:57-123` | implemented | yes (mocked) | `kb` and `llm_client` default args mean the deployed pipeline cannot inject a Config-driven adapter (see F-004, F-024) |
| F-032 | `ContradictionTracer` wired into deployed pipeline | (not wired) | not wired | n/a | D15 — `ContradictionTracer` exists (`layer5_result/contradiction_tracer.py`) but `build_pipeline` does not construct it. Architecture §7.3 retraction source #2 is inert in deployment. Existing v0.16 candidate |

### 1.7 Caching (cross-cutting)

| ID | Integration point | File / call site | Status | Live-test | Architecture-gap |
|---|---|---|---|---|---|
| F-033 | `entity_resolution_cache` table reads/writes | `resolver.py:31-77` | implemented | yes (mocked) | Cache works; retraction is supported (`retract_cache_entry`) |
| F-034 | `entity_resolution_cache` row ids referenced by trace edges | (not referenced) | partial | n/a | D13 — trace edges don't carry cache row ids, so retraction propagation cannot reach cached resolutions. Operator confirmed: defer to v0.16 |
| F-035 | Persistent KB statement cache | (does not exist) | n/a | n/a | Architecture §9.1 specifies HTTP-level caching only (LRU + ETag). Operator confirmed: defer persistent statement cache to v0.16 |

### 1.8 Architecture-specified surfaces not in code (Mode 2 findings)

The Mode 2 pass walked architecture §§4.6, 5.4, 7.3, 9.5 looking for
integration surfaces the architecture specifies but the code does not
implement. Three found.

| ID | Integration point | Architecture cite | Status | Disposition |
|---|---|---|---|---|
| F-036 | Periodic consistency check scheduler | §5.4: "Consistency checks run on a periodic schedule (deployment-configurable; default once daily) and on-write" | partial (method exists, no caller) | `ConsistencyChecker.check_periodic()` exists at `consistency.py:84`; no scheduler, background task, or deployment hook invokes it. On-write checking is wired via `consistency_checker` parameter to each oracle. Periodic re-scanning — which would catch conflicts that became stale after subsequent writes — has no execution path. Defer to v0.16 (Phase 10.5 runs corpora sequentially in a single session; the on-write path covers the measurement window) |
| F-037 | Deployment-injected external correction ingress | §7.3: "deployment-injected external correction surfaces a contradiction (the user reports the system's verdict was wrong). This feedback enters the contradiction tracer" | not implemented | `ContradictionTracer.trace_contradiction()` exists at `contradiction_tracer.py:43`; no HTTP/API surface receives user corrections to forward to it. `app.py`'s only POST endpoint is `/chat`. Architecture §7.3 names this as retraction source #3 and the v0.16 plan's D15 already captures "ContradictionTracer not wired into deployed pipeline" — F-037 is the upstream half of D15 (no API to receive corrections, distinct from no tracer to receive them in the pipeline). Defer to v0.16 as a D15 companion |
| F-038 | Resolution-cache audit endpoint | §9.5: "Audit-log endpoints (query-only; no operator-driven mutation): inspect substrate rows, **inspect resolution cache entries**, view audit history, view consistency-check reports" | not implemented | `app.py` exposes four `/audit/*` endpoints (substrate-rows, consistency-checks, circuit-breakers, retractions). The architecture-specified "inspect resolution cache entries" endpoint is absent. Small (~30 LOC); defer to v0.16 (does not block Phase 10.5; Phase 10.5 acceptance does not reference the endpoint set) |

**Mode 2 disposition.** All three findings are real architectural-
completeness gaps, none blocks Phase 10.5, all defer to v0.16. They join
the existing v0.16 candidates rather than expanding Phase F's scope. The
architecture-surface walk took ~30 minutes and is captured for D26's
v0.16 standing-pass methodology.

---

## Section 2 — Categorization

### Must-implement (blocks Phase 10.5 runbook as written)

The runbook depends on these. Without them, Phase 10.5 Step 1 (`RUN_LIVE_KB=1`)
produces 20+ `NotImplementedError`s on the derivation corpus (Phase E
already saw this), Step 4 measures something different from what the
documentation claims (F-009), and the runbook's troubleshooting
references (F-022, F-023) reference behavior the code does not support.

| ID | Item | F-phase | Rationale |
|---|---|---|---|
| F-001 | `_live_resolve` | F2 | Headline blocker — required by every live KB call path |
| F-002 | `_live_lookup` | F2 | Headline blocker |
| F-003 | `_live_subsumption` | F2 | Headline blocker |
| F-004 | `WikidataAdapter` wiring in `build_pipeline` | F2 | Required for F-001..003 to be reachable from deployed pipeline |
| F-005 | `Config.wikidata_*` reaching `WikidataAdapter` | F2 | Required for F-008/F-007 to be configurable |
| F-006 | `CachingHTTPClient` instantiated for KB | F2 | Architecture §9.1; without it every KB call is uncached |
| F-007 | Wikimedia User-Agent compliance | F2 | Out-of-policy risk; operator-decided value is `Aedos/0.15 (https://github.com/Aspect-Research/aedos; asa@aspectresearch.org)` |
| F-008 | Client-side rate limiting | F2 | 5/s SPARQL, 50/s search (operator-decided B); ties to F-022 |
| F-009 | Purpose-string mismatch | F2 or F3 | Directly affects what Phase 10.5 measures. F2 if scope permits because it changes the meaning of Phase 10.5 results; F3 acceptable since the fix is small (~1-2h) and isolated |
| F-022 | Implement `AEDOS_KB_REQUEST_DELAY_MS` | F2 | Operator confirmed; ties to F-008 |
| F-023 | Remove `AEDOS_LLM_TEMPERATURE` line from runbook | F2 or F3 | Trivial; the runbook documents behavior the code does not support — that itself is a deployment-readiness defect under D26's framing |
| F-015 | Python sandbox hardening | F3 | Operator-elevated unconditionally |
| F-024 | `Config` threaded through `build_pipeline` | F3 | Required for F-005, F-025..F-028 to be reachable. Decomposed naturally: F2 threads what F2 needs (KB-related Config fields); F3 threads what F3 needs (walker / consistency / http_cache fields) |
| F-025..F-028 | `Config.walker_*`, `circuit_breaker_threshold`, `http_cache_*`, `wikidata_*` wired | F3 (F-028 in F2) | Same as F-024 |

### Should-implement (improves Phase 10.5 without blocking)

| ID | Item | F-phase | Rationale |
|---|---|---|---|
| F-010 | Live test against real Anthropic for chat | F4 | Folds naturally into F4's end-to-end validation; one live case covers it |
| F-030 | Chat-wrapper end-to-end live | F4 | Same |
| F-013 | `app.py` `.env` loading | F3 if scope permits | Improves operator ergonomics; current state is workable via `uvicorn --env-file` |

### Deferred to v0.16

These are not deployment-readiness items by the strict definition (Phase F's
question: "does the deployed pipeline reach its external services with the
configuration each service requires?"). They are correctness-mechanism or
architectural-completeness items that the audit surfaced but Phase F
deliberately scopes out — pulling them in would expand Phase F from
deployment-readiness into doing v0.16's plan.

| ID | Item | v0.16 delta | Operator-confirmed |
|---|---|---|---|
| F-014 | `_TEMPERATURE_DEPRECATED_PREFIXES` completeness | (new minor, capture as D27) | n/a |
| F-021 | `verification_context` plumbing | D9 | n/a (already deferred) |
| F-034 | KB-grounded retraction (cache row ids on trace edges) | D13 | Confirmed defer |
| F-032 | `ContradictionTracer` wired into deployed pipeline | D15 | n/a (already deferred) |
| (audit) | Tier U → Python composition | D10 | n/a (already deferred) |
| (audit) | Retraction cascade and re-derivation | D14 | n/a (already deferred) |
| F-029 | `Config.seed_file` consulted by deployed pipeline | D28 | n/a |
| F-036 | Periodic consistency-check scheduler (§5.4) | D29 | n/a |
| F-037 | External-correction ingress (§7.3 retraction source #3; D15 companion) | D30 | n/a |
| F-038 | Resolution-cache audit endpoint (§9.5) | D31 | n/a |
| (F-009 cleanup) | Dead `extractor:assistant` / `walker` keys in `DEFAULT_MODEL_BY_PURPOSE` | Resolve as part of F-009 if F2 schedules it; otherwise v0.16 cleanup | n/a |
| (architectural) | Type filtering at entity resolution | Ambiguity A — confirmed defer | Confirmed defer |
| (architectural) | Persistent KB statement cache | Ambiguity D — confirmed defer | Confirmed defer |

**Operator-confirmed explicit-deferral note.** Phase F's proactive audit
pass also uncovered known deferred-architecture gaps (D9, D10, D13, D14,
D15). Phase F scoped these out deliberately as not-deployment-readiness
work; they remain v0.16-scope per their original deferral reasoning. The
principle ("proactive audits should be comprehensive") has force but
expanding Phase F beyond deployment-readiness conflates two different
audit types. D26 captures the discipline pattern; the v0.16 plan picks up
the architectural-completeness work under its own scope.

---

## Section 3 — Proposed implementation order

F2 (Wikidata live implementation) — four commits per the original plan
plus two additional wiring commits surfaced by F1:

1. `Phase F2: _live_resolve implementation` — `wbsearchentities` against
   live WDQS, polite User-Agent (F-007), rate-limited (F-008), config-
   driven (F-005). Live tests cover Obama → Q76, not-found, network
   failure (mocked transport).
2. `Phase F2: _live_lookup implementation` — SPARQL with qualifier
   projection. Live tests cover P39 on Q76, P36 on Q30 (D19 inverse
   direction), deprecated-statement exclusion, not-found, timeout
   (mocked).
3. `Phase F2: _live_subsumption implementation` — SPARQL property-path
   over P31|P279|P131|P361 with `UNION` for two-directional check.
   Live tests cover direct, transitive, cycle termination, unrelated.
4. `Phase F2: WikidataAdapter wiring through build_pipeline` — F-004,
   F-005, F-006, F-008, F-022, F-028. Threads `Config` through
   `build_pipeline` for the Wikidata-related fields only; the broader
   Config threading (F-024, F-025..F-027) lands in F3 to keep F2
   focused on Wikidata.
5. `Phase F2: purpose-string alignment` — F-009. Renames the four
   misaligned call-site purposes to match `DEFAULT_MODEL_BY_PURPOSE`
   keys, or alternately adds the call-site purposes to the table; the
   F2 design doc picks one. Removes dead `extractor:assistant` and
   `walker` entries if `walker` is genuinely unused. Updates
   `.env.example` documentation to match.
6. `Phase F2: remove AEDOS_LLM_TEMPERATURE from runbook` — F-023.
   One-line edit; included with F2 to keep the runbook honest about
   what the code supports.
7. `Phase F2: live KB integration validated against derivation corpus` —
   re-run derivation corpus under `RUN_LIVE_KB=1`, confirm no
   `NotImplementedError`s, spot-check three traces, verify F-009
   correction produces expected model routing in trace metadata,
   performance sanity.

**F2 acceptance criterion — wiring correctness as first-class concern.**

F1's 4:1 ratio of wiring-gaps to stubs (8 wiring defects vs. 3 known
stubs in Mode 1, plus 3 architecture-required-not-implemented in Mode 2)
suggests the deployment surface has systematic wiring gaps, not just
isolated missing-method gaps. F2's design discipline must treat
**wiring correctness as part of the work, not separate from it**:

- Every method F2 implements must be reachable from the deployed
  pipeline path, not just unit-test-callable.
- Every config knob F2 introduces (rate-limit, UA, endpoint URL) must
  flow from `Config` through `build_pipeline` to the consumer.
- F2 acceptance requires at least one live test that exercises the
  full deployed-pipeline path for each new capability — confirming
  the capability is reachable, not just that the code-path-in-isolation
  works.

Concretely, the F2 validation commit (#7 above) verifies:
- A live derivation run produces traces that confirm the configured
  endpoint URL was used (verifiable via trace metadata or call records).
- The trace metadata records `model` (and `purpose`) for every LLM
  call; after F-009 correction, the purposes match
  `DEFAULT_MODEL_BY_PURPOSE` keys exactly.
- The HTTP cache shows non-zero hit count after the corpus run (proves
  the cache was wired and consulted).
- The rate limiter shows non-zero deferred-request count if the corpus
  was large enough to engage it, OR a deliberate stress-test confirms
  the limiter engages.

If any of these "the capability is reachable" verifications fail, F2 is
not done.

F3 — scope per F1:

8. `Phase F3: Python verifier sandbox hardening (design doc first)` —
   F-015. Design doc surfaces the choice between AST-walk hardening,
   RestrictedPython, or containerized execution; operator approves
   before implementation.
9. `Phase F3: Python verifier sandbox hardening (implementation)` —
   chosen design from #8. Tests: every known dynamic-import bypass
   pattern (`__import__("os")`, `importlib.import_module("os")`,
   `getattr(__builtins__, "__import__")`, `exec` of import strings).
10. `Phase F3: Config threaded through build_pipeline (non-Wikidata)` —
    F-024, F-025, F-026, F-027. `build_pipeline` accepts `Config`;
    `Walker` receives `walker_*`; `ConsistencyChecker` receives
    `circuit_breaker_threshold`; HTTP cache constructed and reused.
11. `Phase F3: app.py loads .env` — F-013 (if scope permits).
    Optional based on F3 budget.

F4:

12. `Phase F4: end-to-end validation against real services (single case)` —
    F-010, F-030. Single case manually traced through real Anthropic +
    real Wikidata + (E5-pending) gpt-4.1-mini substrate.

Tag at the end of F4: `v0.15.0-rc.8`.

---

## Section 4 — Scope estimate

Hours of careful work per item. Includes design, implementation, tests,
and integration validation. Ranges reflect known unknowns surfaced during
the audit walk.

### F2 — Wikidata + wiring

| Item | Hours |
|---|---|
| Design doc (covers all of F2) | 2-3 |
| `_live_resolve` impl + live tests | 3-4 |
| `_live_lookup` impl + live tests | 4-6 |
| `_live_subsumption` impl + live tests | 3-4 |
| Wiring (`build_pipeline`, `Config` threading for KB) | 2-3 |
| Purpose-string alignment + tests | 1-2 |
| Runbook edit + minor cleanup | < 0.5 |
| Derivation-corpus validation run | 2-3 |
| **F2 subtotal** | **17-26 hours** |

### F3 — sandbox + remaining config + .env

| Item | Hours |
|---|---|
| Sandbox-hardening design doc | 1-2 |
| Sandbox hardening implementation | 4-8 |
| Config threading (non-Wikidata fields) | 3-4 |
| `.env` loader for `app.py` | 1 |
| **F3 subtotal** | **9-15 hours** |

### F4 — end-to-end

| Item | Hours |
|---|---|
| Case selection + setup | 1 |
| Trace capture + verification | 1-2 |
| Document the trace | 1 |
| **F4 subtotal** | **3-4 hours** |

### Total

**29-45 hours of careful work** — roughly 4-6 working days. Below the
prompt's "2-3 days, possibly more" estimate at the lower bound; at the
upper bound, modestly longer. The variance is primarily in F2's KB
implementation work and F3's sandbox hardening, where the design choice
materially affects implementation cost.

### Budget for live API consumption

The plan estimated $7-20 total. F1 surfaces no items that change this
materially. F2's derivation-corpus validation remains the largest
consumer (~$5-15); F4's single case is cheap (~$0.50-2). F3's sandbox
tests do not consume API budget (sandbox execution is deterministic
given inputs; the LLM-code-generation tests can use mocked transport for
the sandbox's bypass cases).

Cap: $20. Authorize at F2 start, track but do not re-litigate.

---

## Section 5 — Discipline notes (D26 companion)

### F-009 elevation — the most significant finding of Phase F so far

The live-KB stubs (F-001..003) were the entry-point finding — Phase E
surfaced them as 20 cases of `NotImplementedError`, which is what
motivated Phase F's existence. They are real and they block Phase 10.5
as written. But the headline finding of Phase F to date is **F-009**:
the months-old purpose-string mismatch that has been silently routing
half the substrate to the wrong model.

**What F-009 means at the project level.**

Phase A through Phase D ran their audits against a deployed
configuration that nobody deliberately chose. The intended configuration
(documented in `DEFAULT_MODEL_BY_PURPOSE` and in `.env.example`) is
`gpt-4.1-mini` for all internal LLM purposes; the actual configuration
(what `_resolve_purpose_config` produces when the call-site purpose
strings don't match the table) is `gpt-4.1-mini` for two of nine
purposes and `claude-haiku-4-5` (the chat-default fallback) for the
other four. The remaining three are dead keys in the table.

The audit chain did not catch this because **the test suite uses mocked
LLM clients that don't care which model name routes where**. `MockTransport`
in `tests/conftest.py:55-89` records the `purpose` parameter for
assertions but returns a canned response regardless of routing — the
routing is invisible to the test outcome. So Phases A, B, C, and D ran
their audits against a system whose deployed behavior they had no way to
observe; the mocked test surface is structurally blind to model-routing
correctness.

Phase E's comparison data is honest because the harness's
`AEDOS_OVERRIDE_MODEL_BY_PURPOSE: {"*": ...}` wildcard catches every
non-`chat` purpose regardless of name — the override path side-stepped
the broken routing entirely. So the open-weight comparison results are
real measurements of the candidate models; they just happened to
side-step a defect that affects the default configuration.

**Implication.** F-001..003 prevent Phase 10.5 from running at all under
`RUN_LIVE_KB=1`. F-009 lets Phase 10.5 run but measures something
different from what the operator believes is being measured. The first
is a louder failure mode; the second is the more consequential one,
because the louder failure would be debugged before publication while
the silent one would simply produce numbers nobody recognized as wrong.

In the D26 framing: F-001..003 are the kind of defect a *single* live
run would surface. F-009 is the kind that would survive multiple live
runs unnoticed — exactly what the proactive-audit discipline is for.

**Why the broader audit chain didn't catch this.** The architecture
chain audited the verification pipeline's *semantics* (does it produce
the right verdicts given the right inputs?), not its *deployment*
(does it actually use the inputs the documentation says it uses?). The
mocked-test convention that made A-D possible at all is the same
convention that hides routing defects. D24 named the closely-related
pattern for measurement instruments; F-009 confirms it generalizes —
*the harness that proves the system correct does not necessarily prove
the system is configured the way you think it is*.

### The wiring-gap pattern (eight findings)

Phase F's audit walk surfaced the live-KB stubs (the headline) but also
surfaced **eight wiring-gap defects** the prior audit chain missed. The
pattern across them is consistent: the architecture specified the
capability, the implementation provided it as a code path, and the
deployed pipeline does not invoke the code path with the configuration
the capability requires. The class includes:

- F-004 (WikidataAdapter constructed with no arguments)
- F-005 (Config wikidata_* fields defined but never read)
- F-006 (CachingHTTPClient never instantiated for KB)
- F-008 (rate-limit knob referenced by runbook, no code reads it)
- F-009 (4 of 9 LLM purpose-strings broken — silently routes to wrong model)
- F-022 (KB delay knob referenced, no code reads it)
- F-023 (LLM temperature knob referenced, no code reads it)
- F-024..F-028 (entire `Config` class dead in deployed pipeline)

**Common root cause.** Each was introduced by a forward-looking change
(adding a config field, refactoring purpose strings, adding a runbook
troubleshooting note) without a corresponding follow-up wiring change.
The audit chain's check was "does the change work in isolation?" — each
PR individually passed its tests. The deployment-readiness check —
"does the deployed pipeline reach this change end-to-end?" — was never
the next-PR responsibility.

This is exactly D26's pattern: previous audits were reactive (catching
gaps when measurement produced anomalies); this proactive audit walked
the surface before measurement began. The eight wiring-gap defects all
existed in the v0.15.0-rc.7-prep code; Phase E surfaced one of them as
a 20-case `NotImplementedError` blast; the other seven would have
surfaced during Phase 10.5 as inexplicably-low calibration accuracy or
silent model-routing surprises. None were necessary to surface in
expensive measurement runs.

**Methodology for v0.16's D26 implementation.** What worked in F1:

1. **Configuration audit** — every `Config` field; every `os.getenv`;
   every env-var the runbook documents. Check each reaches the code
   that should consume it.
2. **Purpose / routing audit** — every `purpose=` parameter; compare
   against the routing table that documents the supported set.
3. **External-service constructor audit** — every external-service
   adapter (`WikidataAdapter`, `LLMClient`); verify the deployed
   pipeline constructs it with the configuration its constructor
   accepts.
4. **Runbook-vs-code audit** — every env-var, file path, or knob the
   runbook mentions; confirm the code reads it.

These four passes caught all eight wiring-gap defects above. The pattern
generalizes: D26's standing pre-release pass should run them. The
implementation cost is small (a few hours per pass, totally offline,
zero API consumption) — the kind of standing check that earns its place
in CI rather than as an ad-hoc audit.

---

## Section 6 — Operator decisions captured

The seven ambiguities resolved before F1 began. Restated here so the
F2 design doc has them at hand without cross-referencing the plan.

| Item | Decision | F-phase | Notes |
|---|---|---|---|
| **A** Type filtering at resolution | Defer to v0.16 | n/a | Capture as "known v0.16 work, deferred pending Phase 10.5 disambiguation-error rate data" |
| **B** Rate limiting | Implement | F2 | 5/s SPARQL, 50/s search, configurable via `AEDOS_KB_REQUEST_DELAY_MS` (F-008/F-022 paired) |
| **C** User-Agent | `Aedos/0.15 (https://github.com/Aspect-Research/aedos; asa@aspectresearch.org)` (GitHub URL verified against `git remote -v`) | F2 | Privacy caveat: email appears in HTTP headers to Wikimedia and intermediaries; acceptable for research-scale; flag for commercial-deployment revisit |
| **D** Persistent KB statement cache | Defer to v0.16 | n/a | Architecture §9.1 specifies HTTP cache only; revisiting architecture is v0.16 work |
| **E** Python sandbox hardening | **Elevated to F3 unconditionally** | F3 | Operator pushback: defense-in-depth for chat-wrapper attack surface. Surface design choice (AST-walk vs RestrictedPython vs container) before implementing |
| **F** `AEDOS_KB_REQUEST_DELAY_MS` / `AEDOS_LLM_TEMPERATURE` | Implement KB knob, remove LLM-temp runbook line | F2 | Removing the dead runbook line is itself deployment-readiness per D26 framing |
| **G** D13 KB-grounded retraction | Defer to v0.16 | n/a | Phase F scopes out D9, D10, D13, D14, D15 as "not deployment readiness" — they remain v0.16-scope |

---

## Section 7 — F2 inputs

The F2 design document, when written, has these inputs ready:

- Architecture-settled questions (endpoints, depth, properties, rank
  handling, caching layer, polarity): see `docs/phase_F_plan.md` §F2.
- Operator-decided ambiguities (A-G above): this document §6.
- The F1 inventory items in scope for F2: F-001..008 (Wikidata + UA +
  rate-limit), F-009 (purpose alignment), F-022 (KB delay knob), F-023
  (runbook edit), F-028 (Config wikidata_* threading), partial F-024
  (Config threading for KB-related fields).
- Out-of-F2-scope items deferred to F3: F-013 (.env loader), F-015
  (sandbox hardening), F-024..F-027 (broader Config threading),
  F-025..F-026 (walker / circuit-breaker config).

---

## Section 8 — Acceptance

F1 acceptance criteria:

**Mode 1 (existing code surface):**
- ✓ Every external-service touch point in `src/aedos/` walked
- ✓ Every `os.getenv` and `os.environ` reference in `src/` audited
- ✓ Every `Config` field traced from definition to consumer
- ✓ Every `purpose=` call site checked against `DEFAULT_MODEL_BY_PURPOSE`
- ✓ Every runbook env-var reference checked against code
- ✓ Architecture cross-checks against §§4.6, 5.1, 5.4, 6.2, 6.3, 6.4, 7.3, 9

**Mode 2 (architecture-required surface, added at F1 review):**
- ✓ Architecture §4.6 chat-wrapper intervention model surfaces walked
- ✓ Architecture §5.4 consistency-check schedule walked
- ✓ Architecture §7.3 retraction sources (substrate, contradiction, external) walked
- ✓ Architecture §9.5 audit-log endpoint set walked

**Output:**
- ✓ Inventory table, categorization, implementation order, scope estimate
- ✓ F-009 elevation as the most significant Phase F finding to date
- ✓ Wiring-correctness acceptance criterion specified for F2
- ✓ Mode 2 findings recorded as v0.16 deltas (D29-D31)

Operator decision point: confirm F2 may begin against the order and scope
above, or request scope adjustments first.

The commit that lands this audit is tagged `Phase F1: deployment readiness audit (no implementation)` per the plan's specification.

---

*End of Phase F1 deployment readiness audit.*
