"""D40 — structural invariants for Layer 4 source authorization.

Architecture §6.5 specifies that each Layer 4 source (Tier U, KB
verifier, Python verifier) is invoked only by the route authorized
to call it. The Python verifier is the most-policed: §6.5 step 3
("Python verification *if the route is Python*") is the gate
F-042 surfaced as missing — the walker invoked the Python verifier
unconditionally, producing false-`contradicted` for subjective and
preference claims that should have abstained.

This test walks `src/aedos/` for every call into the Python verifier
and asserts each is guarded by a routing-hint check in the enclosing
function (or carries an explicit `# noqa: PYVERIFIER-UNGATED`
opt-out). The structural-test pattern is D26's "audit the invariant
in CI, not just in review" discipline; D40 is the F-042-specific
worked example.

If this test fires, the failure message tells the maintainer
exactly what to do — either add the gate or document the opt-out.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable, NamedTuple

import pytest


_SRC_DIR = Path(__file__).parent.parent.parent / "src" / "aedos"
_NOQA_MARKER = "noqa: PYVERIFIER-UNGATED"

# Names that indicate the enclosing function consults the predicate's
# routing before invoking the Python verifier. Expand as new gate idioms
# are introduced (an entry here documents that the new idiom counts as
# routing-gated for the invariant's purpose).
_ROUTING_CHECK_NAMES = frozenset({
    "_predicate_routing",
    "routing_hint",
    "predicate_routing",
})

# AST attribute names that, when chained at the verify-call site,
# identify the call as a Python verifier invocation. We match any
# attribute access ending in `.verify(` whose chain contains one of
# these names — covers `self._python_verifier.verify(...)`,
# `pipeline.python_verifier.verify(...)`, etc.
_VERIFIER_CHAIN_NAMES = frozenset({
    "python_verifier",
    "_python_verifier",
})


class UngatedCall(NamedTuple):
    file: str
    line: int
    function_name: str


def _verify_call_chain_names(call_node: ast.Call) -> list[str]:
    """Return the attribute names along a `.verify(` call's chain
    (e.g. `self._python_verifier.verify(...)` → ['verify',
    '_python_verifier'])."""
    chain: list[str] = []
    node = call_node.func
    while isinstance(node, ast.Attribute):
        chain.append(node.attr)
        node = node.value
    return chain


def _function_has_routing_check(func_node: ast.AST) -> bool:
    """True if any `_predicate_routing` / `routing_hint` reference
    appears in the function's body."""
    for child in ast.walk(func_node):
        if isinstance(child, ast.Name) and child.id in _ROUTING_CHECK_NAMES:
            return True
        if isinstance(child, ast.Attribute) and child.attr in _ROUTING_CHECK_NAMES:
            return True
    return False


def _find_python_verifier_calls(
    tree: ast.AST,
) -> list[tuple[ast.Call, ast.AST]]:
    """Walk the AST and return (call_node, enclosing_function_node)
    for each Python verifier `.verify(...)` invocation. The enclosing
    function is the innermost FunctionDef / AsyncFunctionDef ancestor
    (or None if the call is at module level)."""
    results: list[tuple[ast.Call, ast.AST]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._func_stack: list[ast.AST] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._func_stack.append(node)
            self.generic_visit(node)
            self._func_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._func_stack.append(node)
            self.generic_visit(node)
            self._func_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute) and node.func.attr == "verify":
                chain = _verify_call_chain_names(node)
                # chain[0] is "verify"; subsequent entries are the
                # owning object's attribute name(s). Match if any
                # link in the chain identifies the python verifier.
                if any(name in _VERIFIER_CHAIN_NAMES for name in chain[1:]):
                    if self._func_stack:
                        results.append((node, self._func_stack[-1]))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return results


def _has_noqa_marker(file_lines: list[str], call_line: int) -> bool:
    """The marker is honored on the call line itself or any of the
    five lines immediately above the call (covers multi-line call
    expressions and a preceding comment line)."""
    start = max(0, call_line - 6)
    end = call_line  # call_line is 1-indexed; slice is exclusive
    return any(_NOQA_MARKER in line for line in file_lines[start:end])


def _collect_ungated() -> list[UngatedCall]:
    ungated: list[UngatedCall] = []
    for py_file in sorted(_SRC_DIR.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for call_node, func_node in _find_python_verifier_calls(tree):
            if _has_noqa_marker(lines, call_node.lineno):
                continue
            if _function_has_routing_check(func_node):
                continue
            ungated.append(
                UngatedCall(
                    file=str(py_file.relative_to(_SRC_DIR.parent.parent)),
                    line=call_node.lineno,
                    function_name=getattr(func_node, "name", "<module>"),
                )
            )
    return ungated


def test_python_verifier_calls_are_routing_gated():
    """D40 invariant: every Python verifier invocation must be
    routing-gated per architecture §6.5 step 3.

    See `docs/v0.16_planning.md` D40 for the discipline rationale
    and `docs/phase_F/f3_design.md` §4.4 (D40 paragraph) for the F3
    landing context.
    """
    ungated = _collect_ungated()
    assert not ungated, (
        "D40 invariant violation — Python verifier called without a "
        "routing-hint check.\n\n"
        + "\n".join(
            f"  {call.file}:{call.line} in function {call.function_name!r}"
            for call in ungated
        )
        + "\n\n"
        "Each violation must either:\n"
        "  (a) add a `routing_hint == 'python'` gate via "
        "`_predicate_routing()` (architecture §6.5 step 3), or\n"
        "  (b) document the intentional exception with a "
        "`# noqa: PYVERIFIER-UNGATED` comment on or just above the "
        "call line.\n\n"
        "The unconditional-Python-invocation pattern produced F-042: a "
        "§3.2 soundness violation where subjective/preference/opinion "
        "claims received false `contradicted` verdicts. The gate keeps "
        "the architectural promise — Python is the source of belief "
        "only for the python route."
    )


def test_find_python_verifier_calls_discriminates():
    """Sanity check: the AST walker actually identifies the
    walker's call site. If this test fails, the discovery logic is
    broken and the main test could give a false PASS."""
    walker_file = _SRC_DIR / "layer4_sources" / "walker.py"
    tree = ast.parse(walker_file.read_text(encoding="utf-8"))
    calls = _find_python_verifier_calls(tree)
    assert len(calls) >= 1, (
        "Expected at least one python_verifier.verify call in "
        "layer4_sources/walker.py — the discovery logic is broken or "
        "the walker has been refactored in a way the test doesn't see."
    )
    # The walker's verify call lives in _direct_lookup
    func_names = {
        getattr(func_node, "name", "<module>")
        for _, func_node in calls
    }
    assert "_direct_lookup" in func_names, (
        f"Expected verify call inside _direct_lookup; found in: {func_names}"
    )


def test_routing_check_detection_works():
    """Sanity check: `_function_has_routing_check` recognizes the
    real gate. If the walker's `_direct_lookup` is detected as
    ungated, the detection logic is too strict."""
    walker_file = _SRC_DIR / "layer4_sources" / "walker.py"
    tree = ast.parse(walker_file.read_text(encoding="utf-8"))
    for call_node, func_node in _find_python_verifier_calls(tree):
        assert _function_has_routing_check(func_node), (
            f"Detection failed: {getattr(func_node, 'name', '?')} should "
            f"register as routing-gated (F-042 fix landed in walker.py)."
        )
