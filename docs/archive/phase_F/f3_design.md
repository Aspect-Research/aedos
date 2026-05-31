# Phase F3 — Design Document

*Design document for Phase F3 implementation. F3 covers the items F1
named as F3-scope (or operator-elevated to F3 from v0.16 deferral):
sandbox hardening, broader Config threading, and `.env` loading for
the deployed app. Operator confirms the design — particularly the
four open questions in §9 — before any implementation commit lands.*

---

## 1. Frame

F3 closes the F1 inventory items the F2 phase deferred:

- **F-015** — Python verifier sandbox hardening. Operator-elevated to
  F3 unconditionally at the F1 review (the chat wrapper's untrusted
  input flows through extraction and influences what code the verifier
  generates; defense-in-depth matters even at v0.15 scale).
- **F-024..F-026** — Config threading for non-KB fields. `Walker` takes
  a `config` dict it never receives; `ConsistencyChecker` likewise. All
  `Config.walker_*` and `Config.circuit_breaker_threshold` fields are
  currently dead in the deployed pipeline.
- **F-013** — `app.py` lacks `.env` loading. Operators must
  pre-export env vars or use `uvicorn --env-file`; a deployment that
  forgets either gets a chat endpoint that can't reach Anthropic
  / OpenAI.

F3 also lands the structural-test follow-ups that the F2 follow-ups
named as v0.16 candidates but that fit F3's scope naturally:

- **D40** structural test for routing-source matching (the F-042
  companion check).

F3 does **not** cover:
- v0.16 deltas D9, D10, D13, D14, D15, D17 (architectural-completeness;
  scoped to v0.16 by Phase F1).
- D33, D34, D39 (corpus/fixture alignment; data-driven, awaits Phase 10.5).
- D5 (no KB-sourced neighbor enumeration; architectural).
- Phase F4's end-to-end single-case validation (separate phase).

F3 acceptance, per the F1 wiring-correctness discipline: every
capability F3 implements must be reachable from the deployed pipeline
path and verified by at least one live or live-shaped test.

---

## 2. Settled by architecture or inputs

| Question | Decision | Source |
|---|---|---|
| Sandbox is required at the Python verifier boundary | Yes | Architecture §6.3 ("Restricted Python with standard library plus an allow-list... No file I/O, no network, no subprocess.") |
| Current sandbox: subprocess + AST import allow-list | Implemented | `src/aedos/utils/sandbox.py` |
| Walker resource budgets are deployment-configurable | Yes | Architecture §6.4 ("Default 30 seconds... Default 10 calls per claim. Configurable per deployment.") |
| Circuit-breaker threshold is deployment-configurable | Yes | Architecture §5.4 ("default N=3, configurable") |
| `app.py` is the FastAPI deployment entry point | Yes | `src/aedos/app.py` is the only FastAPI surface |
| `.env` is the project's standard credentials store | Yes | `.env.example` documents the layout |

---

## 3. Operator-confirmed prior decisions

Carryover from F2 review (the items that affect F3):

- **F-015 elevated to F3 unconditionally.** Sandbox hardening is not
  conditional on F1 finding adversarial cases empirically — the chat
  wrapper's untrusted input surface makes defense-in-depth a baseline.
- **The honest framing matters.** v0.15's sandbox commits to catching
  common LLM-generated wrong-code patterns; adversarial defense
  remains v0.16 territory. The security boundary is documented
  explicitly, not implied.

New for F3 from the operator's F2-acceptance message:

- **F-042 changes the framing of the sandbox decision slightly.** The
  v0.15 pipeline can produce wrong verdicts on routine input due to
  upstream bugs (F-042 was one); sandbox quality matters for "LLM
  accidentally generates code that does the wrong thing" failures,
  not only adversarial-user attacks. The choice between options
  weights this category alongside adversarial defense.

---

## 4. Design — F-015 Python sandbox hardening

The load-bearing F3 decision. Three options, explicit cost-benefit,
recommendation with the honest security boundary in writing.

### 4.1 What the current sandbox catches and doesn't

Current implementation (`src/aedos/utils/sandbox.py`):

- **AST static-import scan** before execution. Walks `ast.Import` and
  `ast.ImportFrom` nodes; rejects any module not in the allow-list
  (`datetime`, `math`, `decimal`, `fractions`, `statistics`, `re`,
  `unicodedata`, `string`).
- **Subprocess isolation** for execution. Spawns a fresh Python
  process with a minimal environment dict and stdin closed.
  Captures stdout/stderr; enforces a wall-clock timeout
  (`_DEFAULT_TIMEOUT_SECONDS = 10`).
- **CWD scoped to a tempdir** that's cleaned up after execution.

What this catches:
- `import os` → blocked at AST scan
- `from os import path` → blocked at AST scan
- Infinite loops → timeout
- Persistent state across executions → subprocess isolation prevents

What this does **not** catch:
- `__import__("os")` — direct builtin invocation; not visible as
  `ast.Import`.
- `getattr(__builtins__, "__import__")("os")` — dunder-traversal
  builtin lookup.
- `__import__ = __builtins__.__import__; __import__("os")` — assigning
  to local then using.
- `eval("import os")` / `exec("import os")` — string-encoded code.
- `open("/etc/passwd")` — `open` is a builtin; not subject to the
  import allow-list; reads files.
- `__class__.__base__.__subclasses__()` — Python class hierarchy
  enumeration; finds subprocess-like classes; classic CPython escape.
- Network: a code path through `socket` is blocked by the import
  scan, but if any bypass works, network access is available
  because the subprocess is a normal CPython interpreter.

For LLM-generated code under non-adversarial input, the static import
scan catches >90% of unsafe patterns. For LLM-generated code under
**adversarial input** (where the user influences what code the LLM
writes via the claim's predicate or object slot), the scan is
bypassable.

### 4.2 Option A — AST-walk hardening

Extend `_check_imports` (rename to `_check_sandbox_violations`) to
walk additional AST patterns:

- `Name(id="__import__")` references as either subscript or call
  target.
- `Name(id="eval")` / `Name(id="exec")` references.
- `Name(id="open")` references (block file I/O at the AST layer).
- `Name(id="__builtins__")` references.
- `Attribute(attr="__subclasses__")` and `Attribute(attr="__class__")`
  on user expressions.
- `Attribute(attr="__import__")`, `__globals__`, `__bases__`, dunder
  attributes generally on user expressions (allow on safe stdlib
  modules; reject on user variables).
- `Subscript` access where the subscript key is a dunder name.

Implementation: extend the `ast.walk` loop with the new node-type
checks. Each violation is a separate rejection reason
(`disallowed_builtin: __import__`, `disallowed_attribute: __class__`,
etc.). Add a unit test per bypass pattern.

**Cost.** ~80 lines in `sandbox.py`. ~150 lines of tests (one per
bypass pattern, parameterized). Two-hour implementation + test.

**Benefit.** Closes the *common* bypass patterns. An LLM that
accidentally writes `__import__("os")` is now caught at parse time.
The vast majority of LLM-generated-wrong-code patterns are covered.

**What this doesn't catch (the honest list):**
- Encoded strings: `getattr(object, chr(95)*2 + 'subclasses' + ...)()`
  bypasses any AST name check by constructing the dunder name from
  literal expressions.
- `eval` / `exec` on dynamically-built strings: even if `eval` / `exec`
  literal references are blocked, code can build up to the same
  result through other functions (e.g., `compile` is in the import
  allow-list as part of `types` ... actually it isn't, but the
  pattern generalizes).
- `__class__` traversal *via* an allowed stdlib type: `''.__class__.__base__.__subclasses__()`
  is the canonical sandbox-escape; the empty-string literal isn't a
  user variable, so a blanket `Attribute(attr="__class__")` block on
  user expressions either also blocks legitimate uses or misses this
  pattern.
- Anything that requires runtime evaluation rather than AST inspection.

**Documentation honesty.** Production deployments handling
adversarial input must understand the AST-walk catches the common
case but is **not** a security boundary against an active attacker.
v0.15 documents the limitation in the verifier's docstring and in
the architecture doc; v0.16 (or earlier, if Phase 10.5 surfaces
specific adversarial scenarios) upgrades to Option B or C.

### 4.3 Option B — RestrictedPython

Adopt the `zope.restrictedpython` library (or
`restrictedpython` standalone). It compiles Python source using a
restricted set of opcodes that block:
- Direct attribute access on dunder names (the `__class__` traversal).
- Print statements / certain builtins by default.
- Configurable via policy.

Integration: `python_verifier.py` calls
`RestrictedPython.compile_restricted(code, '<sandbox>', 'exec')`
instead of `compile`; runs the result with a guarded `globals` dict
that only exposes safe builtins.

**Cost.** New library dependency. ~50 lines in `python_verifier.py`
+ sandbox.py to integrate. Tests for legitimate verification code
(must still work) + adversarial bypass tests (must be blocked).
Four-to-six-hour implementation + test.

Library considerations:
- `RestrictedPython` is maintained by the Plone/Zope community.
  Active but not as widely used as it once was.
- Python version compatibility: needs RestrictedPython that supports
  Python 3.11 (the project's minimum per `pyproject.toml`).
- Some Python features (f-strings, walrus operator, certain
  comprehensions) need newer RestrictedPython versions or
  configuration.

**Benefit.** Closes the `__class__` traversal pattern. Closes the
encoded-string-to-dunder pattern. Stronger guarantee against the
canonical CPython sandbox escapes.

**What this doesn't catch:**
- Algorithmic bugs in generated code (Option B is about escape, not
  about whether the code does the right verification logic).
- Side channels: timing attacks, memory exhaustion via deep
  recursion (the subprocess timeout helps but doesn't fully bound).
- Future bypasses found in RestrictedPython itself (Python evolves;
  RestrictedPython must keep pace).

**Library risk.** Adopting a dependency makes us responsible for
tracking its CVE history and maintenance status. For v0.15 (a
research release), this is a modest cost; for v0.16's broader
deployment ambitions, it becomes load-bearing.

### 4.4 Option C — Containerized execution

Run the generated code in a Docker container with:
- No network access.
- Read-only filesystem (except a tempdir for stdout/stderr).
- Memory and CPU limits.
- Non-privileged user.

Integration: `sandbox.run_code` orchestrates a `docker run` (or a
podman / firecracker / gVisor equivalent) with the generated code
piped via stdin. Captures stdout/stderr/exit code; enforces timeout.

**Cost.** Docker (or equivalent) becomes a deployment dependency —
not bundled with the Aedos package. ~150 lines of container
orchestration code. Tests against the real container runtime
(adds CI infrastructure complexity). Per-call cold-start overhead
(measured in tens to hundreds of ms per verification; nontrivial
for the calibration corpus's hundreds of verifier calls).

Four-to-eight-hour implementation, plus container-image build and
CI integration. Probably one to two days end-to-end.

**Benefit.** Strongest isolation. Even a successful sandbox escape
inside the container can't reach the host. Standard industry
practice for executing untrusted code.

**What this doesn't catch:**
- Algorithmic bugs in generated code (same as Options A/B).
- Side channels that survive container isolation (rare in practice
  for typical sandboxing concerns).

**Deployment cost.** Requires Docker (or chosen runtime) on every
deployment. The Phase 10.5 runbook would need a "Docker installed?"
prereq. Local dev gets a per-test container spin-up overhead. The
chat-wrapper deployment in production would need container
infrastructure.

### 4.5 Recommendation: Option A, with the security boundary in writing

Adopt Option A for v0.15. Rationale:

1. **Common-case coverage at proportionate cost.** AST-walk
   hardening catches the LLM-generates-wrong-code class that F-042
   surfaced as a real failure mode. The cost (~2 hours) is
   proportionate to the v0.15-scale benefit.

2. **Honest security framing.** The AST-walk catches non-adversarial
   bugs but is not a security boundary. v0.15 documents this
   limitation explicitly:
   - In `python_verifier.py`'s module docstring.
   - In `sandbox.py`'s module docstring.
   - In `architecture.md` §6.3 (alongside the existing sandbox
     description).
   - In `docs/phase_10_5_runbook.md` if relevant for operator
     awareness.

   The text: "v0.15's sandbox blocks common LLM-generated unsafe
   patterns but is bypassable by adversarially-crafted input.
   Production deployments handling adversarial input should upgrade
   to RestrictedPython (Option B) or containerized execution
   (Option C) per the F3 design document."

3. **v0.16 upgrade path is clear.** Either Option B or C is a
   future-proof upgrade; the choice between them can be informed by
   Phase 10.5 data (does the Python corpus surface any adversarial
   patterns? does the deployment surface adversarial input?) and by
   v0.16's deployment-environment decisions.

4. **No new dependencies for v0.15.** AST walking is a pure-stdlib
   extension of existing code.

**The argument I'm not making:** "v0.15 is research-only so security
doesn't matter." That argument was defensible before F-042 (which
showed the deployed pipeline can produce wrong verdicts on routine
input). It's weaker now. Option A is recommended not because v0.15
is exempt from security concerns but because Option A's coverage of
the *non-adversarial* failure class is what v0.15 most benefits from.

### 4.6 Test strategy for Option A

For each bypass pattern that Option A blocks, a unit test:

```python
def test_sandbox_blocks_dunder_import():
    code = '__import__("os").system("ls")'
    result = run_code(code)
    assert not result.success
    assert "disallowed_builtin" in (result.import_violation or "")
```

Patterns to test (~12-15 tests):
- `__import__("os")` — builtin call
- `getattr(__builtins__, "__import__")` — attribute lookup
- `eval("...")` — eval call
- `exec("...")` — exec call
- `open("/some/path")` — file open
- `__class__.__base__.__subclasses__()` — class traversal (this one
  may not be catchable by Option A; document as out-of-scope here
  and tested in v0.16's Option B/C work).
- `compile(...)` — bytecode compilation
- Legitimate verification code that uses allowed modules (must still
  work — test against canned `def verify(...)` examples).

The legitimate-code tests confirm Option A doesn't over-block. The
bypass tests confirm Option A catches what it claims to catch. The
not-caught patterns are documented in test docstrings with a note
that the pattern is deferred to v0.16's stronger sandbox.

### 4.7 Sandbox docstring (the security boundary in writing)

Drafted text for `utils/sandbox.py`'s module docstring (replacing the
current "correctness sandbox, not a security sandbox" line):

```python
"""Python sandbox for Aedos v0.15.

Threat model
------------
Aedos verifies natural-language claims by generating Python code via
an LLM and executing it. The sandbox bounds what that code can do.

This sandbox is designed against **LLM-generated wrong code** — code
that the LLM produces honestly but that does the wrong thing (writes
False for subjective claims, attempts file I/O for unbounded computations,
imports modules outside the allow-list). It is **not** designed
against an active attacker crafting input to escape the sandbox.

What the sandbox blocks
-----------------------
- Static imports outside the allow-list (datetime, math, decimal,
  fractions, statistics, re, unicodedata, string).
- Direct invocations of __import__, eval, exec, open, compile in the
  AST.
- Attribute access patterns commonly used in CPython sandbox escapes
  (__class__, __subclasses__, __builtins__, __globals__) where the
  AST shape is visible.
- Subprocess isolation (each verification runs in a fresh Python
  process with a minimal environment; CWD is a clean tempdir).
- Wall-clock timeout (default 10s).

What the sandbox does NOT block (production deployments handling
adversarial input must upgrade — see docs/phase_F/f3_design.md §4 for
options B and C):
- Encoded-string attacks (`chr(95)*2 + 'class' + chr(95)*2` etc.).
- Class-hierarchy traversal via literal expressions (e.g.,
  ``''.__class__.__base__.__subclasses__()``).
- Anything that requires runtime evaluation rather than AST
  inspection.

The current implementation is suitable for v0.15's research-release
scale where LLM-generated wrong code is the dominant concern.
"""
```

---

## 5. Design — F-024..F-027 Config threading

Mostly mechanical. One open question on validation.

### 5.1 Walker config (F-025)

Current state (`walker.py`):
- `Walker.__init__` accepts `config: Optional[dict] = None`; stores it.
- `_DEFAULT_MAX_DEPTH = 4` is module-level.
- `WalkerBudget(wall_clock_seconds=30.0, max_llm_calls=10)` is dataclass default.
- `Walker.walk` accepts `budget: Optional[WalkerBudget] = None` and
  defaults to `WalkerBudget()` if None.

Threading plan:
- `build_pipeline` constructs `Walker(... config=config.__dict__)` or
  passes the relevant fields explicitly.
- Cleaner: change `Walker.__init__` to accept `walker_wall_clock_seconds`,
  `walker_max_llm_calls`, `walker_max_depth` as optional kwargs;
  `build_pipeline` passes them from Config.
- Walker uses the kwargs as the defaults when `walk()` is called
  without an explicit `budget`.

Implementation: ~20 lines in walker.py, ~5 lines in pipeline.py.

### 5.2 ConsistencyChecker config (F-026)

Current state (`consistency.py`):
- `ConsistencyChecker.__init__` accepts `config: Optional[dict] = None`;
  reads `cfg.get("circuit_breaker_threshold", _DEFAULT_CIRCUIT_BREAKER_THRESHOLD)`.

Threading plan:
- `build_pipeline` passes `config={"circuit_breaker_threshold":
  config.circuit_breaker_threshold}` to ConsistencyChecker.
- Or cleaner: change ConsistencyChecker to accept the threshold as a
  direct kwarg.

Implementation: ~5 lines in consistency.py, ~3 lines in pipeline.py.

### 5.3 HTTP cache statement TTL (F-027)

Already wired in F2 commit #4 via `Config.http_cache_statement_ttl_seconds`
(read by `WikidataAdapter._live_lookup`). No additional work; just
confirm the field is documented as having distinct semantics from
`http_cache_entity_ttl_seconds`.

### 5.4 Config validation — the open question

Should `Config.__post_init__` validate field values?

**Option A — Validate on construction.**

```python
def __post_init__(self):
    if self.walker_wall_clock_seconds <= 0:
        raise ValueError("walker_wall_clock_seconds must be positive")
    if self.walker_max_llm_calls <= 0:
        raise ValueError("walker_max_llm_calls must be positive")
    if self.walker_max_depth <= 0:
        raise ValueError("walker_max_depth must be positive")
    if self.circuit_breaker_threshold <= 0:
        raise ValueError("circuit_breaker_threshold must be positive")
    if not self.wikidata_sparql_endpoint.startswith(("http://", "https://")):
        raise ValueError("wikidata_sparql_endpoint must be a URL")
    # ... etc.
```

**Cost.** ~30 lines of validation code in `config.py`. Test: confirm
each validation triggers; confirm Config() with defaults doesn't.

**Benefit.** Catches typos and misconfigurations at deployment-init
time, not after a 30-minute calibration run. The operator gets a
clear error like "AEDOS_WALKER_MAX_DEPTH must be positive integer" at
process startup rather than confusing downstream behavior.

**Option B — Trust the operator.**

No validation. Config values flow through; if they're nonsensical,
downstream code crashes or behaves weirdly.

**Cost.** Zero implementation.

**Benefit.** Simpler. Avoids validation rules drifting from actual
constraints.

**Cost (hidden).** Phase 10.5 operators get less-helpful errors.

### 5.5 Recommendation for §5.4: Option A (validate)

Phase 10.5's runbook has specific configuration that an operator
might typo. Catching at Config construction is a small cost (one-time
per process) that prevents long debug cycles later. The validation
rules are straightforward (positive integers, valid URLs); rule drift
is bounded because the fields themselves are stable.

Tests confirm each validation: ~10 unit tests of `Config(field=bad_value)`
raising the expected ValueError; one test of `Config()` succeeding
with defaults; one test of valid customization succeeding.

---

## 6. Design — F-013 `.env` loader

Three candidate placements; one recommendation.

### 6.1 Option A — `conftest.py`

Pros:
- All tests automatically see `.env` values.
- Calibration runner gets API keys without extra setup.

Cons:
- Tests should be deterministic and isolated; loading `.env` couples
  test outcomes to whatever's in the operator's env file.
- A typo in `.env` (e.g., wrong key name) can silently affect tests.
- Risk: a developer's `.env` containing experimental config could
  break the test suite in ways that don't reproduce in CI.

### 6.2 Option B — `app.py` startup

Pros:
- Affects only the FastAPI deployment, not tests.
- Operator running `uvicorn aedos.app:app` gets `.env` auto-loaded.
- Matches the convention where `.env` is a deployment artifact, not
  a test artifact.

Cons:
- Calibration runner / benchmark still don't get `.env` loaded
  automatically.
- The Phase 10.5 runbook still requires the operator to source
  `.env` into the shell explicitly.

### 6.3 Option C — Shared utility `aedos.utils.env`

Provide `aedos.utils.env.load_dotenv_if_present()` as a small,
explicit, opt-in helper. Callers that want `.env` loading invoke it;
callers that don't (tests by default) ignore it.

```python
# aedos/utils/env.py
def load_dotenv_if_present(path: str | Path | None = None) -> bool:
    """Load environment variables from .env if it exists. Returns True
    if loaded, False otherwise. Idempotent: safe to call multiple times
    (subsequent loads are no-ops at the dotenv level)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    if path is None:
        path = _find_env_in_cwd_or_parents()
    if path and Path(path).exists():
        load_dotenv(path)
        return True
    return False
```

Callers:
- `app.py` startup → calls it. Closes F-013.
- `tests/calibration/test_corpus_runner.py` could optionally call it
  at module import (closes F-040's residual: the calibration runner
  no longer requires shell-sourced env vars).
- `tests/evaluation/benchmark.py` likewise (optional).
- Phase E's `phase_e_comparison.py` `_load_env` function gets
  replaced by this shared utility.

Pros:
- Explicit. Each caller decides whether to load.
- Easy to test (utility itself is small).
- Avoids coupling tests to operator's `.env`.
- Generalizes — calibration runner closes F-040 in the same change.

Cons:
- Slightly more code than Option B alone.

### 6.4 Recommendation: Option C

The shared utility avoids the test-coupling risk of Option A while
giving every entry-point caller an explicit, easy way to load `.env`.
Closes F-013 (app.py) and F-040's residual (calibration runner) in
one small unit of work.

**Implementation:**
- `src/aedos/utils/env.py` — the utility (~20 lines).
- `src/aedos/app.py` — calls `load_dotenv_if_present()` at startup
  before `Config.from_env()`.
- `tests/calibration/test_corpus_runner.py` — calls at module import.
- `tests/evaluation/benchmark.py` — calls in the live-mode entry.
- `tests/evaluation/phase_e_comparison.py` — replace `_load_env` with
  the shared utility.

**Tests:**
- `test_env.py` — utility loads when `.env` is present; returns False
  when not; doesn't crash if dotenv isn't installed.

---

## 7. Test strategy

### 7.1 F-015 sandbox

Per §4.6: ~15 unit tests covering blocked patterns and legitimate
patterns. Adversarial tests document what's NOT caught with
xfail-style markers.

### 7.2 Config threading

- `test_walker.py` — confirm Walker honors config-passed budget
  defaults.
- `test_consistency_checker.py` — confirm config-passed threshold
  drives circuit-breaker behavior.
- `test_config.py` (new) — validation tests per §5.4-§5.5.

### 7.3 `.env` loader

- `test_env.py` (new) — the utility's behavior.
- `test_app.py` — confirm app startup invokes the loader.

### 7.4 Structural test (D40)

A new `test_walker_source_routing.py` (or extension to
`test_purpose_table_completeness.py`) that grep-walks `walker.py`
for `verify(` calls into sources and asserts each is guarded by a
routing check. Implementation: ~30 lines of regex + assertion.

This is the F-042 companion check landed in F3 (the architecture
invariant "Layer 4 sources are invoked only per routing
authorization" now has a CI gate).

### 7.5 Mocked regression

After all F3 changes, the full mocked suite must stay green. Expected
post-F3: 787 + ~30 new tests = ~815, no regressions.

### 7.6 F3 validation (the wiring-correctness gate)

Single live re-run of `der_cross_001` (the F2 single-case sanity
check) with F3 changes applied. Cost: ~$0.02. Confirms:
- Walker honors the config-passed budget (verify `walk_metadata`
  reports the configured wall-clock seconds).
- ConsistencyChecker honors the config-passed threshold (if any
  conflict surfaces in the run; otherwise check `consistency_circuit_breaker`
  table's threshold field).
- `app.py` startup loads `.env` (confirm `_config` has populated keys
  by invoking `/chat` after process start).

---

## 8. Acceptance criteria

F3 lands when:

1. The four implementation commits (sandbox, config threading,
   `.env` loader, D40 structural test) land green with their tests.
2. The structural test (D40) confirms the F-042 routing invariant is
   enforced in CI.
3. The mocked suite passes (~815 tests, no regressions).
4. The F3 validation live re-run produces a clean verdict for
   `der_cross_001` and shows config-driven behavior in the trace.
5. The security-boundary documentation lands in `sandbox.py`,
   `python_verifier.py`, and a §6.3 cross-reference in
   `architecture.md`.

---

## 9. Open questions for operator review

These are F3-scope decisions the design surfaces. The plan makes a
recommendation per item; the operator confirms or pushes back.

### Q1 — Sandbox option

The design recommends **Option A** (AST-walk hardening) with the
security boundary documented in writing. The honest framing the
operator named: "v0.15 ships with AST-walk hardening that catches
the common case but is bypassable; production deployments handling
adversarial input should upgrade to RestrictedPython or
containerized execution."

**Alternatives:**
- Option B (RestrictedPython) — stronger guarantee, library
  dependency, ~4-6h.
- Option C (containerized) — strongest, requires Docker, 1-2 days.

**Recommendation:** A, with explicit boundary documentation.

### Q2 — Config validation

The design recommends **Option A** (validate on construction): catch
typos early at deployment-init time.

**Alternative:** trust the operator (Option B); errors surface
downstream.

**Recommendation:** A.

### Q3 — `.env` loader placement

The design recommends **Option C** (shared utility, opt-in by each
entry point). Closes F-013 (app.py) and F-040's residual (calibration
runner) in one change.

**Alternatives:**
- A (conftest.py) — auto-loads in tests; risks coupling.
- B (app.py only) — simpler but doesn't close F-040's residual.

**Recommendation:** C.

### Q4 — D40 structural test scope

The D40 structural test catches "every Layer 4 source is invoked
only per routing authorization." Scope question: does the test
cover only `walker.py` (the current site), or does it generalize
to any future Layer 4 entry point?

**Options:**
- **Narrow:** assert specifically that Walker invokes Python verifier
  only when routing is `python`. Doesn't generalize to future
  consumers (the chat wrapper, the benchmark) but covers F-042's
  exact site.
- **General:** define the invariant ("any caller of Python verifier
  must check routing") and grep-walk the codebase for `python_verifier.verify(`
  calls, asserting each is guarded.

**Recommendation:** General. The grep-walk pattern (analogous to
`test_purpose_table_completeness.py`) catches F-042-class drift
across all consumers, including future ones added by Phase F4 or
v0.16. Net cost: ~5 additional lines vs the narrow version.

---

## 10. Implementation order

Per F2's commit-boundary discipline. Discrete commits, stash-and-verify
where applicable (especially for sandbox hardening — pre-fix tests
should fail, post-fix should pass).

1. **`Phase F3: AST-walk sandbox hardening (F-015)`** (~3h)
   - Extend `_check_imports` → `_check_sandbox_violations` per §4.2.
   - Update `python_verifier.py` and `sandbox.py` docstrings with the
     threat-model text from §4.7.
   - Architecture §6.3 cross-reference (minor).
   - Tests per §7.1.

2. **`Phase F3: Config threading + validation (F-024..F-027)`** (~2h)
   - `config.py`: add `__post_init__` validators per §5.4-§5.5.
   - `walker.py`: accept budget defaults via constructor kwargs.
   - `consistency.py`: accept threshold via constructor kwarg.
   - `pipeline.py`: thread Config through to Walker and ConsistencyChecker.
   - Tests per §7.2.

3. **`Phase F3: .env loader utility (F-013)`** (~1h)
   - `src/aedos/utils/env.py` — the utility.
   - `app.py` — call at startup.
   - `test_corpus_runner.py` + `benchmark.py` + `phase_e_comparison.py`
     — opt-in usage (close F-040's residual).
   - Tests per §7.3.

4. **`Phase F3: D40 structural test for Layer 4 routing authorization`** (~1h)
   - New test or extension to `test_purpose_table_completeness.py`
     per §7.4.
   - General scope per Q4.

5. **`Phase F3: live validation (single case)`** (~0.5h, ~$0.02)
   - Re-run `der_cross_001` with F3 applied; confirm config-driven
     behavior in trace; capture in a brief addendum to
     `f2_validation_log.md` or a new `f3_validation_log.md`.

Total F3: ~7-8 hours, ~$0.02 API cost. Within the F1 estimate (9-15
hours).

---

## 11. Out of F3 scope (recap)

For clarity, items the audit might assume F3 touches but it does not:

- **D9 verification_context plumbing** — v0.16
- **D10 Tier U → Python composition** — v0.16
- **D13 KB-grounded retraction** — v0.16
- **D14 retraction cascade + re-derivation** — v0.16
- **D15 ContradictionTracer wired into build_pipeline** — v0.16
- **D29 periodic consistency-check scheduler** — v0.16
- **D30 external-correction ingress API** — v0.16
- **D31 resolution-cache audit endpoint** — v0.16
- **D33 / D34 / D39 fixture and corpus alignment** — Phase 10.5 data-driven
- **D37 calibration runner honors AEDOS_DB_PATH** — v0.16
- **D38 runbook-vs-code audit standing discipline** — v0.16 (Phase F surfaced 4 instances; v0.16 establishes the gate)
- **D41 adversarial mock fixtures discipline** — v0.16 (F-042 landed the first worked example)
- **RestrictedPython or containerized sandbox** — v0.16, if Phase 10.5 or production deployment surfaces specific adversarial scenarios
- **Phase F4 end-to-end validation** — separate phase

F3 stays focused.

---

*End of Phase F3 design document.*
