"""Tests for src.verifiers.code_generation.sandbox (v0.4)."""

from __future__ import annotations

import pytest

from src.verifiers.code_generation.sandbox import ExecutionResult, run_code


def test_simple_print_succeeds():
    r = run_code("print(3)")
    assert r.success is True
    assert r.stdout.strip() == "3"
    assert r.stderr == ""
    assert r.exit_code == 0
    assert r.timed_out is False


def test_syntax_error_marks_failure():
    r = run_code("def broken(:")
    assert r.success is False
    assert r.exit_code != 0
    assert r.stderr  # python writes the SyntaxError to stderr


def test_timeout_returns_timed_out():
    r = run_code("import time; time.sleep(10)", timeout_seconds=1)
    assert r.success is False
    assert r.timed_out is True
    assert r.error and "timed out" in r.error.lower()


def test_stderr_is_captured_separately_from_stdout():
    code = (
        "import sys\n"
        "sys.stderr.write('warning: noisy\\n')\n"
        "print('answer')\n"
    )
    r = run_code(code)
    assert r.success is True
    assert r.stdout.strip() == "answer"
    assert "warning" in r.stderr


def test_runtime_error_marks_failure():
    r = run_code("raise RuntimeError('boom')")
    assert r.success is False
    assert "RuntimeError" in r.stderr or "boom" in r.stderr


def test_duration_is_measured():
    r = run_code("print(1)")
    assert isinstance(r.duration_ms, int)
    assert r.duration_ms >= 0


def test_slow_flag_for_long_runs():
    """A run >1s gets ``slow=True``."""
    r = run_code("import time; time.sleep(1.2)", timeout_seconds=5)
    assert r.success is True
    assert r.slow is True


def test_isolated_cwd_means_no_pwd_files():
    """Sandbox cwd is empty — listing it yields nothing."""
    code = "import os; print(len(os.listdir('.')))"
    r = run_code(code)
    assert r.success is True
    assert r.stdout.strip() == "0"


def test_to_dict_includes_all_fields():
    r = run_code("print(1)")
    d = r.to_dict()
    for k in ("success", "stdout", "stderr", "exit_code", "duration_ms", "timed_out", "slow"):
        assert k in d


def test_subprocess_oserror_returns_failed_result(monkeypatch):
    """If subprocess.run itself raises OSError (e.g. python interpreter
    missing, fork failure), the sandbox returns a failed
    ExecutionResult rather than crashing the verifier turn."""
    from src.verifiers.code_generation import sandbox as sb

    def fake_run(*a, **kw):
        raise OSError("fork failed: no resources")

    monkeypatch.setattr(sb.subprocess, "run", fake_run)

    r = sb.run_code("print(1)")
    assert r.success is False
    assert r.exit_code == -1
    assert r.timed_out is False
    assert r.error and "OSError" in r.error
    assert "fork failed" in r.error
