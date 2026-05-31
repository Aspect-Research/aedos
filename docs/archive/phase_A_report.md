# Phase A Cleanup — Report

Session between `v0.15.0-rc.1` and Phase 10.5. Six contained items from the
v0.16 deltas: three architecture-document edits (D4, D12, D20-part-2) and three
code changes (D7, D8, D18). Hygiene only — no capability extension. The deferred
deltas (D5, D9, D10, D13, D14, D15) stayed deferred; D6 and D16 are Phase B /
operator-decision items and were not touched.

Four cluster commits, run in order A1 → A2 → A3 → A4:

```
9b276bd Cluster A1: architecture wording aligned with code (D4, D12, D20-part-2)
e2c8d45 Cluster A2: ChatWrapper passes ExtractionContext (D18)
8031d04 Cluster A3: remove redundant predicate_equivalence walker edge (D7)
cefe65f Cluster A4: unify audit-logging on log_event function form (D8)
```

---

## Cluster summaries

### A1 — architecture wording (D4, D12, D20-part-2)

Three edits to `docs/architecture.md`, zero code risk:

- **D4.** §5.2's `predicate_translation` schema block now includes
  `single_valued INTEGER NOT NULL DEFAULT 0`, with a prose paragraph documenting
  semantics (1 = functional, a differing KB value contradicts; 0 = multi-valued,
  a mismatch is `no_match`; default 0 is conservative — a wrong 1 produces false
  contradictions, a wrong 0 only false abstains) and a note that the seed-file
  format carries the field. The paragraph references `fixup2_report.md` for the
  per-predicate classification rather than transcribing the functional list
  (seed-pack content, not architecture).
- **D12.** §5.4's `transitive_equivalence_violation` bullet now states the
  inverse-predicate exemption: two predicates on the same KB property whose
  `slot_to_qualifier` maps are exact subject/object inversions (the
  `capital_of`/`has_capital` pattern) are not a conflict; any other divergence
  remains one. This aligns the document with `consistency.py`'s
  `_is_inverse_mapping` (fixup-2).
- **D20 part 2.** §5.2 documents that `slot_to_qualifier`'s `subject`/`object`
  keys govern which Aedos slot maps to the KB statement subject vs. value; §6.2
  states the `lookup_statements` call is keyed on whichever Aedos slot maps to
  the KB statement subject — the claim's subject *or* object depending on
  `slot_to_qualifier`. This aligns the document with fixup-3's D19 work.

The three edited sections were re-read for internal consistency: §5.2, §5.4 and
§6.2 all use the `capital_of` / P36 inverse-predicate example coherently. No
deeper wording issue surfaced — §6.2's qualifier prose did not need broader
revision than the D20-part-2 sentence.

### A2 — ChatWrapper ExtractionContext (D18)

`ChatWrapper.respond` called `extract(draft, asserting_party=...)` against the
`extract(text, context: ExtractionContext)` signature. The resulting `TypeError`
was swallowed by a broad `except Exception: claims = []`, so `/chat` extracted
zero claims and every response was pass-through.

Fix: `respond` now builds an `ExtractionContext(asserting_party=…,
context_type="chat_user", turn_id=…)` and passes it positionally. The broad
`except` was **removed entirely** rather than narrowed — it was the exact
construct that hid this bug for two release candidates; letting an unexpected
extraction failure propagate is the honest behaviour and surfaces the next
defect immediately. (The narrow-and-log alternative was considered and rejected
on that basis; the choice is documented in the commit message.)

New file `tests/integration/test_chat_wrapper.py`: an end-to-end extraction test
(load-bearing for D18) and a response-shape test.

### A3 — remove predicate_equivalence walker edge (D7)

`Walker._expand_via_substrate` emitted `predicate_equivalence` edges via
`predicate_translation.query_neighbors`. An equivalent predicate shares the same
`kb_property` so its KB lookup is identical to the original's, and
`TierU.lookup` stage 3 already broadens by the same oracle — the edge was
redundant. The block was deleted; the method's remaining work is
distribution-gated subsumption traversal. `query_neighbors` was kept because
`TierU._stage3` still uses it.

### A4 — unify audit-logging on log_event (D8)

`consistency.py`, `retraction.py` and `contradiction_tracer.py` called
`self._audit.log(...)` as a method, but `audit/log.py` exposes no object with a
`.log` method — `audit_log` defaulted to `None` in every constructor and no
caller anywhere passed it, so the branches were dead. All three were
standardized on the module-level `log_event(conn, …)` function (the form
`tier_u.py` and the oracles already use); the unused `audit_log` constructor
parameter was removed from all three. `event_data` is now passed as a dict
(`log_event` does its own `json.dumps`).

---

## Per-delta status

| Delta | Before | After | Commit |
|-------|--------|-------|--------|
| D4  | §5.2 schema omits `single_valued` (code has it) | §5.2 prints column + semantics | `9b276bd` |
| D7  | Walker emits a redundant `predicate_equivalence` edge | edge removed | `8031d04` |
| D8  | 3 classes call dead `self._audit.log(...)` | unified on `log_event` | `cefe65f` |
| D12 | §5.4 rule described as direction-blind (code is direction-aware) | §5.4 states inverse exemption | `9b276bd` |
| D18 | `/chat` extracts zero claims (stale `extract` signature) | `ExtractionContext` passed; `/chat` verifies | `e2c8d45` |
| D20-1 | `KBVerdict.trace` field names | addressed in release-prep R2 (not revisited) | — |
| D20-2 | §5.2/§6.2 silent on lookup direction | §5.2/§6.2 state `slot_to_qualifier` governs direction | `9b276bd` |

All six are marked **Resolved (Phase A cleanup)** in `docs/v0.16_planning.md`
with commit references; the original entries are kept as historical record.

---

## Verification

### Stash-and-verify — A2 (D18)

`chat_wrapper.py` was stashed (reverting it to `v0.15.0-rc.1`) and
`tests/integration/test_chat_wrapper.py` run against it:

```
FAILED test_chat_extracts_claims  — assert []   (claims empty)
FAILED test_chat_response_shape   — assert []   (claims empty)
```

Both fail because the rc.1 stale signature raises `TypeError`, swallowed by the
broad `except`, leaving `claims` empty. After unstashing, both pass. This
demonstrates the fix actually wires extraction through `/chat` — not merely that
the endpoint returns 200. (The prompt anticipated test 1 failing; test 2 also
depends on a non-empty claim list, so it fails too — consistent and expected.)

### Stash-and-verify — A3 (D7)

After removing the edge, the full suite was run with the edge **restored**
(`walker.py` stashed to rc.1):

| state | result |
|-------|--------|
| edge removed   | 701 passed, 1 skipped, 11 deselected |
| edge restored  | 701 passed, 1 skipped, 11 deselected |

The suite passes identically either way — no test requires the edge to exist,
confirming it was genuinely redundant. `test_predicate_equivalence_substitution`
still passes after removal: it exercises predicate-translation broadening
through `TierU` stage 3 (a retained capability), not the removed walker edge, so
it was correctly left in place. Net test count change from A3 is therefore 0 —
no test exercised the edge exclusively.

### A4 — per-call-site classification (additive vs no-op cleanup)

| call site | event | classification |
|-----------|-------|----------------|
| `consistency.resolve_conflict` | `consistency_violation` | **additive** — verified: a `resolve_conflict` run on a real db now writes 1 entry |
| `consistency._increment_circuit_breaker` | `circuit_breaker_triggered` | **additive** — code path live; fires when the breaker reaches threshold |
| `retraction.propagate_retraction` | `verdict_retracted` | **additive** — verified: a `propagate_retraction` run on a real db now writes 1 entry |
| `contradiction_tracer.trace_contradiction` | `contradiction_traced` | **additive when a db is supplied**; **no behaviour change in the deployed pipeline** — `ContradictionTracer` is not wired into `build_pipeline` (D15, deferred) |

The dead branches were targeting events that architecture §5.4 says the audit
log records ("All retractions, regeneration cycles, and circuit-breaker
triggerings are recorded"), so the fix makes architecturally-required logging
real rather than merely deleting it. `log_event` already supports these event
types (free-form `event_type`, arbitrary `event_data` dict) — **no scope
expansion and no schema change were needed**, so the A4 caveat's
extend-vs-simplify decision did not arise. See "Deferred decisions" below.

### Final verification

```
pytest tests/ -q                                  701 passed, 1 skipped, 11 deselected
python -m tests.evaluation.benchmark --validate-harness   Harness validation: PASS
pytest --run-calibration -q                        701 passed, 12 skipped
git log --oneline v0.15.0-rc.1..HEAD               4 cluster commits
```

The calibration run collects the 11-corpus runner as harness dry-runs (each
corpus's cases load and parse OK); live evaluation remains gated on
`RUN_CALIBRATION` for Phase 10.5.

---

## Test count delta

| | passed | collected |
|--|--------|-----------|
| `v0.15.0-rc.1` baseline | 699 | 711 |
| after Phase A | 701 | 713 |

**+2**, both from D18 (`test_chat_extracts_claims`, `test_chat_response_shape`
in the new `tests/integration/test_chat_wrapper.py`). D7 removed no tests (no
test exercised the removed edge exclusively). D8 changed no test count (the dead
paths were untested). D4/D12/D20 are documentation-only.

---

## Deferred decisions surfaced during A4

The A4 caveat asked whether the dead `self._audit.log(...)` branches were
attempting to log events that *should* be logged — in which case the fix is to
make the logging work, not just delete it. They were: `consistency_violation`,
`circuit_breaker_triggered`, `verdict_retracted` and `contradiction_traced` are
precisely the events architecture §5.4 names as audit-log content. The
unification direction (standardize on `log_event`) makes that logging real, and
`log_event` supports all four event shapes with no new event type and no schema
change. **No extend-vs-simplify decision was forced** — the fix resolves the
question rather than deferring it.

One adjacent item, **not** folded in: `ContradictionTracer` is still unwired in
`build_pipeline` (D15). A4 corrected its `contradiction_traced` call site, but
the event will not fire in the deployed pipeline until D15 wires the tracer in.
D15 stays deferred — this is recorded as context for Phase B / v0.16, not acted
on here.

No other deferred delta was found to be "actually small enough to fold in"; the
discipline boundary held.
