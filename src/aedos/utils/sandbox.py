"""Python sandbox for Aedos v0.15.

Threat model
------------
Aedos verifies natural-language claims by generating Python code via an
LLM and executing it. The sandbox bounds what that code can do.

This sandbox is designed against **LLM-generated wrong code** — code
that the LLM produces honestly but that does the wrong thing (writes
False for subjective claims, attempts file I/O for unbounded
computations, imports modules outside the allow-list). It is **not**
designed against an active attacker crafting input to escape the
sandbox.

What the sandbox blocks
-----------------------
v0.15's AST-walk hardening blocks the common patterns that
LLM-generated code might produce when prompted for verification tasks:

- Static imports outside the allow-list (datetime, math, decimal,
  fractions, statistics, re, unicodedata, string).
- Direct invocations of `__import__`, `eval`, `exec`, `open`,
  `compile` in the AST.
- Direct references to `__builtins__` as a Name or attribute target.
- Class-hierarchy traversal via `__class__` / `__subclasses__` /
  `__globals__` / `__bases__` attribute access.
- Subprocess isolation (each verification runs in a fresh Python
  process with a minimal environment; CWD is a clean tempdir).
- Wall-clock timeout (default 10s).

What the sandbox does NOT block
-------------------------------
- Encoded-string bypass patterns (e.g.,
  ``eval(base64.b64decode(...))``). The AST sees `eval`'s usage but
  the dangerous payload is constructed at runtime from non-literal
  sources.
- Dynamic attribute access via ``getattr`` with computed strings
  (``getattr(obj, chr(95)*2 + 'class' + chr(95)*2)``). The AST sees
  the `getattr` call but not which attribute it ultimately resolves.
- Any pattern that constructs the bypass string at runtime from
  non-literal sources.
- Class-hierarchy traversal via a literal value's `__class__`
  (``''.__class__.__base__.__subclasses__()``) — the attribute chain
  starts on a literal, not on a user-named variable; a blanket block
  would either also block legitimate uses or miss this pattern.

When v0.15 is appropriate
-------------------------
- Research deployments.
- Internal tools where user input is bounded.
- Calibration and evaluation workflows (the Phase 10.5 corpora).
- Development and testing.

When v0.15 is NOT appropriate
------------------------------
- Public-facing chat endpoints where user prompts are unconstrained.
- Deployments where Aedos's verifier output is used to make
  security-relevant decisions.
- Any production scenario where an attacker can craft input that
  influences what code the LLM generates.

For those scenarios, upgrade to RestrictedPython (Option B in
``docs/phase_F/f3_design.md`` §4) or containerized execution
(Option C). v0.16 may ship one of these as the default.

Allowed modules per architecture Section 6.3:
    datetime, math, decimal, fractions, statistics, re, unicodedata, string
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Optional


ALLOWED_MODULES: frozenset[str] = frozenset([
    "datetime",
    "math",
    "decimal",
    "fractions",
    "statistics",
    "re",
    "unicodedata",
    "string",
    # typing is purely a type-annotation helper used
    # by the Python verifier prompt that emits
    # `-> Optional[bool]` return-type signatures. typing has no runtime
    # capability that violates the sandbox's threat model — Optional,
    # Union, etc. are passive annotations evaluated to typing class
    # objects, never invoked, never escape the function. Allowing it
    # unblocks Python verifier code generation that uses the canonical
    # type signature.
    "typing",
])

# Builtin names that are unsafe in the verifier's threat model — see the
# module docstring's "What the sandbox blocks" section. A reference to
# any of these (as a Name, an Attribute, or a subscript on
# `__builtins__`) is treated as a violation. The block is AST-level only;
# runtime-constructed bypass strings are out of scope (see "What the
# sandbox does NOT block").
_BLOCKED_BUILTIN_NAMES: frozenset[str] = frozenset([
    "__import__",
    "eval",
    "exec",
    "open",
    "compile",
    "__builtins__",
])

# Dunder attribute names whose presence in user expressions indicates an
# escape attempt. Blocked on Attribute access regardless of the owning
# expression. Note that legitimate verification code never needs these.
_BLOCKED_DUNDER_ATTRS: frozenset[str] = frozenset([
    "__class__",
    "__subclasses__",
    "__bases__",
    "__base__",
    "__mro__",
    "__globals__",
    "__builtins__",
    "__import__",
    "__dict__",
])

_DEFAULT_TIMEOUT_SECONDS = 10


@dataclass
class SandboxResult:
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool
    import_violation: Optional[str] = None
    error: Optional[str] = None

    @property
    def slow(self) -> bool:
        return self.duration_ms >= 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "import_violation": self.import_violation,
            "error": self.error,
        }


def _check_sandbox_violations(
    code: str, allowed_modules: frozenset[str] = ALLOWED_MODULES
) -> Optional[str]:
    """Return a violation message if the code violates any sandbox rule, else None.

    Walks the AST and rejects:
      - imports outside `allowed_modules` (the allow-list);
      - direct references to blocked builtin names (``__import__``,
        ``eval``, ``exec``, ``open``, ``compile``, ``__builtins__``);
      - attribute access on dunder names commonly used in CPython
        sandbox escapes (``__class__``, ``__subclasses__``,
        ``__globals__``, ``__bases__``, ``__mro__``, ``__dict__``,
        ``__import__``, ``__builtins__``).

    See the module docstring's threat-model section for what this does
    and does not catch. F-015 in `docs/phase_F/f3_design.md` records
    the design choice.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"syntax_error: {e}"

    for node in ast.walk(tree):
        # Static imports outside the allow-list
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in allowed_modules:
                    return f"disallowed_import: {alias.name!r}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top not in allowed_modules:
                    return f"disallowed_import_from: {node.module!r}"
        # Direct references to blocked builtins (Name nodes used as
        # expressions, function calls, etc.)
        elif isinstance(node, ast.Name):
            if node.id in _BLOCKED_BUILTIN_NAMES:
                return f"disallowed_builtin: {node.id!r}"
        # Attribute access on dunder names (class-hierarchy traversal,
        # __globals__, __import__ off __builtins__, etc.)
        elif isinstance(node, ast.Attribute):
            if node.attr in _BLOCKED_DUNDER_ATTRS:
                return f"disallowed_dunder_attribute: {node.attr!r}"

    return None


# Backwards-compatible alias — `_check_imports` was the original name;
# callers may import it. Behavior is now the broader sandbox check.
_check_imports = _check_sandbox_violations


def run_code(
    code: str,
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    extra_allowed: frozenset[str] | None = None,
) -> SandboxResult:
    """Run code in a restricted subprocess after sandbox-violation scanning.

    See the module docstring for the threat model and the explicit list
    of what this sandbox does and does not block. F-015 in
    `docs/phase_F/f3_design.md` §4 records the design choice and the
    upgrade path for deployments handling adversarial input.
    """
    allowed = ALLOWED_MODULES if extra_allowed is None else ALLOWED_MODULES | extra_allowed

    violation = _check_sandbox_violations(code, allowed_modules=allowed)
    if violation is not None:
        return SandboxResult(
            success=False, stdout="", stderr="", exit_code=-1,
            duration_ms=0, timed_out=False,
            import_violation=violation,
        )

    started = time.monotonic()
    with tempfile.TemporaryDirectory() as workdir:
        minimal_env: dict[str, str] = {}
        if sys.platform == "win32":
            for k in ("SYSTEMROOT", "PATH", "PATHEXT", "TEMP", "TMP"):
                if k in os.environ:
                    minimal_env[k] = os.environ[k]

        try:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workdir,
                env=minimal_env,
                timeout=timeout_seconds,
                text=True,
            )
        except subprocess.TimeoutExpired as e:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return SandboxResult(
                success=False,
                stdout=(e.stdout or "") if isinstance(e.stdout, str) else "",
                stderr=(e.stderr or "") if isinstance(e.stderr, str) else "",
                exit_code=-1,
                duration_ms=elapsed_ms,
                timed_out=True,
                error=f"timed out after {timeout_seconds}s",
            )
        except (OSError, ValueError) as e:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return SandboxResult(
                success=False, stdout="", stderr="",
                exit_code=-1, duration_ms=elapsed_ms,
                timed_out=False,
                error=f"{type(e).__name__}: {e}",
            )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return SandboxResult(
        success=completed.returncode == 0,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        exit_code=int(completed.returncode),
        duration_ms=elapsed_ms,
        timed_out=False,
    )
