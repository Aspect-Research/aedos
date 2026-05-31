# Phase 6 Plan — Derivation Walker

**Goal.** BFS inference engine: depth-4, cycle detection, polarity tracking, predicate-distribution gating, resource budgets, full justification trace emission. PythonVerifier is stubbed. Walker produces `verified | contradicted | no_grounding_found` verdicts with complete traces.

**Dependencies.** Phases 0-5 (all substrate oracles), Phase 3 (Tier U lookup), Phase 4 (KBVerifier).

---

## What gets built

### 1. `src/aedos_v0_15/layer5_result/trace.py`
- `TraceNode(node_type, content)` dataclass
- `TraceEdge(edge_type, source, target, metadata)` dataclass
- `JustificationTrace(root, edges, polarity_trace, source_breakdown, walk_metadata)` dataclass
- `trace_to_json(trace)` → dict for serialization

### 2. `src/aedos_v0_15/layer4_sources/walker.py`
- `VerificationContext(current_time, asserting_party)` dataclass
- `WalkerBudget(wall_clock_seconds=30.0, max_llm_calls=10)` dataclass
- `BudgetConsumption(wall_clock_ms, llm_calls)` dataclass
- `WalkResult(verdict, trace, abstention_reason, budget_consumption)` dataclass
- `BudgetExceeded(reason)` exception
- `Walker.walk(claim, context, budget=None)` — full BFS
- `Walker._expand_via_substrate(node)` — equivalence substitution + distribution-gated subsumption traversal
- `Walker._direct_lookup(node, context)` — Tier U + KB + Python stub
- Cycle detection via canonical key set
- Polarity tracking: negation flips verdict interpretation

### 3. PythonVerifier stub update
- `src/aedos_v0_15/layer4_sources/python_verifier.py` — stub that returns `PythonVerdict(terminal=False, verdict=None)`

### 4. Tests (~80 new)
- `tests/v0_15/unit/test_trace.py` — trace dataclass tests, serialization
- `tests/v0_15/unit/test_walker.py` — BFS depth, cycle detection, polarity tracking, distribution gating, budget enforcement, trace emission
- `tests/v0_15/integration/test_walker_with_substrate.py` — multi-source chains, Tier U + KB composition, budget exceedance

### 5. Calibration corpus
- `tests/v0_15/calibration/derivation_corpus.jsonl` — 50 cases

---

## Walker design decisions

1. **Claim node canonical key**: `f"{asserting_party}|{subject}|{predicate}|{object}|{polarity}"` — prevents revisiting the same claim in a walk.
2. **Python verifier stub**: Returns `terminal=False` always. Walker skips it cleanly. Real implementation in Phase 7.
3. **Distribution gating**: Only expand subsumption edges if `verdict != "neither"`. `distributes_up` → expand subject upward; `distributes_down` → expand subject downward; `both` → expand both.
4. **Multi-chain behavior**: First `verified` or `contradicted` result from any direct lookup terminates the walk and returns. Conflicting verdicts within the same walk (one source verified, another contradicted) → return `contradicted` (contradiction takes precedence) and flag in trace metadata.
5. **Trace completeness**: Every edge added to `next_frontier` creates a TraceEdge. The trace records the full walk graph, not just the winning path.
6. **Budget LLM call counting**: Each substrate oracle cold-cache call (predicate_translation, subsumption, predicate_distribution) increments `llm_call_count`. KB lookups that invoke LLM entity selection also count. Tier U lookups do not count (no LLM).
