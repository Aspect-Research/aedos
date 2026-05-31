# Phase F3 — Validation Log

*Brief validation log for F3 closure (per `f3_design.md` §8 acceptance).
F2's validation log (`f2_validation_log.md`) covers Wikidata live
methods + wiring + F-042 routing gate; this log covers F3's
sandbox hardening, Config threading, `.env` loader, and the D40
structural test.*

---

## Environment

- Date: 2026-05-20
- Python: 3.11.9
- F3 build: post-`5b7b81e` (F3 commit #4, `.env` loader)
- Wikidata: live (`RUN_LIVE_KB=1`)

## What F3 verified directly

### F-015 sandbox hardening (16 tests, all green)

`tests/unit/test_sandbox.py`:

- 13 `TestF015BypassPatterns` tests covering each blocked pattern
  (`__import__`, `eval`, `exec`, `open`, `compile`, `__builtins__`,
  `__class__`, `__subclasses__`, `__bases__`, `__mro__`, `__dict__`,
  `__globals__`, `__import__` attribute) plus literal-class-traversal
  blocking and legitimate-verifier-code preservation.
- 2 `TestF015KnownBypasses` tests documenting the security boundary
  via `@pytest.xfail`. One xfailed (the fully-encoded-dunder-chain
  bypass — the documented v0.15 boundary); one xpassed (a phrasing
  the `__builtins__` Name block happens to catch). The xfail/xpass
  signal is durable: a future RestrictedPython / containerized
  upgrade would xpass both.

### Config threading + validation (28 tests, all green)

`tests/unit/test_config_validation.py` (17 tests) — every field's
validation rule rejects invalid values with a `ValueError` that
names the field. Default `Config()` constructs cleanly.

`tests/integration/test_build_pipeline_config.py` `TestF3ConfigThreading`
(5 tests) — `Config.walker_*` and `Config.circuit_breaker_threshold`
reach `Walker._max_depth`, `Walker._default_wall_clock_seconds`,
`Walker._default_max_llm_calls`, and `ConsistencyChecker._threshold`.

### `.env` loader (6 tests, all green)

`tests/unit/test_env.py` — load behavior, idempotency, explicit-path
bypass, no-override-by-default precedence, parent-directory search.

`tests/unit/test_app.py` — confirms the `app.py` lifespan invokes the
loader (the client fixture pre-sets `RUN_LIVE_KB=""` to prevent the
load from polluting downstream tests).

### D40 structural test (3 tests, all green)

`tests/unit/test_layer4_routing_invariants.py` — the F-042 companion
gate. Stash-and-verify done during the test landing: temporarily
removed the F-042 routing gate from `walker.py`, ran the test,
confirmed it failed with the prescribed error message and
`file:line` of the violation; restored the gate; test passes. The
test discriminates correctly.

### Mocked regression

```
$ py -m pytest tests/ -q --ignore=tests/cold_start --ignore=tests/calibration --ignore=tests/integration/live
834 passed, 1 xfailed, 1 xpassed
```

No regressions. The xfailed test is the documented v0.15 sandbox
boundary (fully-encoded-dunder-chain bypass); the xpassed is a
specific phrasing the AST blocks. Both are runtime-visible signals
encoded in the test suite per F3 §4.7's "security boundary in writing"
discipline.

## What F3 verified live (single-case validation)

Single `der_cross_001` case with a deliberately-non-default `Config`
to confirm threading flows end-to-end:

```
Config: walker_max_depth=5, walker_wall_clock_seconds=45.0,
        circuit_breaker_threshold=4

Config threading: VERIFIED
  - pipeline.walker._max_depth == 5
  - pipeline.walker._default_wall_clock_seconds == 45.0
  - pipeline.consistency._threshold == 4

Live KB activity:
  - kb_live_resolve:     2 events
  - kb_live_lookup:      1 event
  - kb_live_subsumption: 0 events

F-042 routing gate: VERIFIED
  - Python verifier edges in trace: 0 (predicate routed kb_resolvable;
    Python verifier correctly did not fire)

Verdict: no_grounding_found
  (Architecturally correct post-F-042: KB returned NO_MATCH because
  "Obama" resolved to Q41773 per D33; Python verifier no longer
  rescues. Pre-F-042 this case produced `verified` via Python `return
  True`. The corpus accuracy threshold is a Phase 10.5 question; F3
  validation is execution-correctness.)

Elapsed: 8.2s
```

All F3 acceptance criteria (§8 of `f3_design.md`) met.

## F3 closure

F3 implementation complete:

| Commit | What |
|---|---|
| `b1d801a` | D40 structural test for Layer 4 routing authorization |
| `06a0e2e` | F-015 sandbox hardening + security boundary docs |
| `9d875bf` | Config threading + validation |
| `5b7b81e` | `.env` loader utility (F-013) |

Phase F4 begins next: end-to-end validation of a single case
through the full pipeline (extractor → walker → KB verifier →
aggregator → audit log → trace inspection) against real services,
producing `docs/phase_F/end_to_end_validation.md` as the F4 artifact
that proves Phase 10.5 can run honestly. Per F1, F4 closes with the
`v0.15.0-rc.8` tag.

---

*End of Phase F3 validation log.*
