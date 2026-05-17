"""Python sandbox for Aedos v0.15 — allow-list enforcement + subprocess execution.

The sandbox enforces an explicit import allow-list before executing any code.
Imports not in the list are rejected before execution; this is a correctness
sandbox, not a security sandbox (the code is LLM-generated).

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


def _check_imports(code: str) -> Optional[str]:
    """Return a violation message if the code imports a disallowed module, else None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"syntax_error: {e}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in ALLOWED_MODULES:
                    return f"disallowed_import: {alias.name!r}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top not in ALLOWED_MODULES:
                    return f"disallowed_import_from: {node.module!r}"
    return None


def run_code(
    code: str,
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    extra_allowed: frozenset[str] | None = None,
) -> SandboxResult:
    """Run code in a restricted subprocess after import scanning."""
    allowed = ALLOWED_MODULES if extra_allowed is None else ALLOWED_MODULES | extra_allowed

    # Inline the allowed-set into the import check so extra_allowed is respected
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return SandboxResult(
            success=False, stdout="", stderr="", exit_code=-1,
            duration_ms=0, timed_out=False,
            import_violation=f"syntax_error: {e}",
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in allowed:
                    return SandboxResult(
                        success=False, stdout="", stderr="", exit_code=-1,
                        duration_ms=0, timed_out=False,
                        import_violation=f"disallowed_import: {alias.name!r}",
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top not in allowed:
                    return SandboxResult(
                        success=False, stdout="", stderr="", exit_code=-1,
                        duration_ms=0, timed_out=False,
                        import_violation=f"disallowed_import_from: {node.module!r}",
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
