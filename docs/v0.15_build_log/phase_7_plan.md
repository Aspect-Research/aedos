# Phase 7 Plan — Python Verification Path

## Goal

Full PythonVerifier: LLM generates a `verify(subject, predicate, obj) -> bool` function, sandbox
executes it, walker integrates the terminal verdict into the trace.

## What's built

### `src/aedos_v0_15/layer4_sources/python_verifier.py` (replace stub)

**PythonVerdict** — updated dataclass:
- `verdict: str` — "verified" | "contradicted" | "no_terminal_result"
- `generated_code: str` — code returned by LLM
- `inputs: dict` — {"subject": ..., "predicate": ..., "object": ...}
- `output: Any` — raw stdout
- `runtime_metadata: dict` — runtime_ms, exception_info, import_violation

**PythonVerifier.verify(claim)** flow:
1. Call LLM via `extract_with_tool(purpose="python_code_generation")` → `{"code": ..., "reasoning": ...}`
2. Assemble harness: `{generated_code}\nresult = verify(...)\nprint('TRUE' if result else 'FALSE')`
3. Execute via `sandbox.run_code()`
4. Interpret: stdout "TRUE" → "verified", "FALSE" → "contradicted", else → "no_terminal_result"
5. Non-zero exit / import violation / timeout → "no_terminal_result"

**Walker update** — `walker.py` line ~252: change `getattr(py_result, "terminal", False)` to
`py_result.verdict != "no_terminal_result"`.

### `tests/v0_15/unit/test_python_verifier.py`

- Date arithmetic claim → verified
- String operation claim → verified
- Numerical comparison → verified
- Numerical comparison → contradicted
- Disallowed import → no_terminal_result
- Exception in generated code → no_terminal_result
- LLM client stub returning code that raises → no_terminal_result
- Timeout case (mocked with very short timeout)
- Correct trace fields populated in verdict

### `tests/v0_15/integration/test_python_path.py`

- Walker with python_verifier (mocked LLM → code returning True) → verified
- Walker with python_verifier (mocked LLM → code returning False) → contradicted
- Walker with python_verifier (mocked LLM → code raising) → no_grounding_found
- Python trace node emitted in trace.source_breakdown

### `tests/v0_15/calibration/python_verification_corpus.jsonl`

30 cases (authored, execution deferred to Phase 10.5):
- 10 date arithmetic
- 8 string operations
- 6 numerical comparison
- 6 list/set operations

## Adversarial coverage

- Cases where the "obvious" code has an off-by-one (date ranges, string counts with duplicates)
- Cases where the predicate's English meaning is subtly different from the trivial comparison
- Import that looks allowed (e.g., `datetime.timezone`) but imports a disallowed module — should NOT be blocked (datetime is allowed)
- Code that returns a truthy non-bool (1, "yes") — harness coerces via bool()

## Ambiguities resolved

1. **No_terminal_result vs abstain**: PythonVerifier returns "no_terminal_result" as the verdict;
   the walker treats this as "not a terminal answer" and continues to depth exhaustion rather than
   returning "no_grounding_found" immediately. This lets Tier U or KB still ground the claim.
2. **String inputs**: All claim slots (subject/predicate/object) are strings; generated code receives
   str arguments and must handle any conversions internally.
3. **Bool coercion**: The harness wraps `print('TRUE' if result else 'FALSE')` — any truthy value
   (1, non-empty string, etc.) counts as verified. This is intentional: generated code may return int.
