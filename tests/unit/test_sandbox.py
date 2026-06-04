"""Tests for the v0.15 Python sandbox."""

from __future__ import annotations

import pytest

from aedos.utils.sandbox import ALLOWED_MODULES, run_code


class TestAllowedImports:
    def test_datetime_allowed(self):
        result = run_code("import datetime\nprint(datetime.date(2026, 1, 1).isoformat())")
        assert result.success
        assert "2026-01-01" in result.stdout

    def test_math_allowed(self):
        result = run_code("import math\nprint(math.floor(3.7))")
        assert result.success
        assert "3" in result.stdout

    def test_decimal_allowed(self):
        result = run_code("from decimal import Decimal\nprint(Decimal('1.1') + Decimal('2.2'))")
        assert result.success

    def test_fractions_allowed(self):
        result = run_code("from fractions import Fraction\nprint(Fraction(1, 3))")
        assert result.success

    def test_statistics_allowed(self):
        result = run_code("import statistics\nprint(statistics.mean([1,2,3]))")
        assert result.success

    def test_re_allowed(self):
        result = run_code("import re\nprint(len(re.findall('r', 'strawberry')))")
        assert result.success

    def test_string_allowed(self):
        result = run_code("import string\nprint(string.ascii_lowercase[:3])")
        assert result.success


class TestDisallowedImports:
    def test_os_rejected(self):
        result = run_code("import os\nprint(os.getcwd())")
        assert not result.success
        assert result.import_violation is not None
        assert "os" in result.import_violation

    def test_subprocess_rejected(self):
        result = run_code("import subprocess\nsubprocess.run(['echo', 'hi'])")
        assert not result.success
        assert result.import_violation is not None

    def test_sys_rejected(self):
        result = run_code("import sys\nprint(sys.version)")
        assert not result.success
        assert result.import_violation is not None

    def test_requests_rejected(self):
        result = run_code("import requests\nrequests.get('http://example.com')")
        assert not result.success
        assert result.import_violation is not None

    def test_from_os_path_rejected(self):
        result = run_code("from os.path import join\nprint(join('a', 'b'))")
        assert not result.success
        assert result.import_violation is not None


class TestExecution:
    def test_simple_arithmetic(self):
        result = run_code("print(2 + 2)")
        assert result.success
        assert "4" in result.stdout

    def test_exception_captured(self):
        result = run_code("raise ValueError('test error')")
        assert not result.success
        assert result.exit_code != 0

    def test_syntax_error_rejected(self):
        result = run_code("def foo(\nprint('oops')")
        assert not result.success
        assert result.import_violation is not None
        assert "syntax_error" in result.import_violation

    def test_stdout_captured(self):
        result = run_code("print('hello sandbox')")
        assert "hello sandbox" in result.stdout

    def test_duration_ms_populated(self):
        result = run_code("x = 1 + 1")
        assert result.duration_ms >= 0


class TestF015BypassPatterns:
    """F-015 hardening — the sandbox blocks the common bypass patterns
    that LLM-generated code might produce when prompted for verification
    tasks. See `aedos.utils.sandbox`'s module docstring for the threat
    model; see `docs/phase_F/f3_design.md` §4 for the design choice
    and Options B/C for upgrade paths against adversarial input."""

    def test_blocks_dunder_import_call(self):
        """``__import__("os")`` — direct builtin call. Pre-F-015 this
        bypassed the static-import check entirely."""
        result = run_code('__import__("os").system("ls")')
        assert not result.success
        assert result.import_violation is not None
        assert "__import__" in result.import_violation

    def test_blocks_eval(self):
        """``eval(...)`` allows runtime code construction."""
        result = run_code('eval("1 + 1")')
        assert not result.success
        assert "eval" in (result.import_violation or "")

    def test_blocks_exec(self):
        """``exec(...)`` likewise."""
        result = run_code('exec("x = 1")')
        assert not result.success
        assert "exec" in (result.import_violation or "")

    def test_blocks_open_call(self):
        """``open(...)`` for file I/O — architecture §6.3 says
        "no file I/O", which the AST-level block enforces for the
        common pattern (builtin `open`). Bypasses via dynamic
        attribute access are not caught — see the module docstring
        for the boundary."""
        result = run_code('open("/etc/passwd").read()')
        assert not result.success
        assert "open" in (result.import_violation or "")

    def test_blocks_compile_call(self):
        """``compile(...)`` constructs bytecode at runtime."""
        result = run_code('compile("1+1", "<src>", "eval")')
        assert not result.success
        assert "compile" in (result.import_violation or "")

    def test_blocks_builtins_name_reference(self):
        """``__builtins__`` as a direct reference (e.g.,
        ``getattr(__builtins__, "__import__")``)."""
        result = run_code('getattr(__builtins__, "__import__")("os")')
        assert not result.success
        assert "__builtins__" in (result.import_violation or "")

    def test_blocks_class_attribute(self):
        """``some_var.__class__`` — used in class-hierarchy traversal."""
        result = run_code('x = "hello"\nprint(x.__class__)')
        assert not result.success
        assert "__class__" in (result.import_violation or "")

    def test_blocks_subclasses_attribute(self):
        """``some_var.__subclasses__()`` — the canonical CPython
        sandbox-escape attribute. Blocked at the attribute layer."""
        result = run_code('object.__subclasses__()')
        assert not result.success
        assert "__subclasses__" in (result.import_violation or "")

    def test_blocks_globals_attribute(self):
        """``func.__globals__`` — extracts the module-level namespace."""
        result = run_code('def f(): pass\nprint(f.__globals__)')
        assert not result.success
        assert "__globals__" in (result.import_violation or "")

    def test_blocks_bases_attribute(self):
        """``cls.__bases__`` / ``cls.__base__`` for hierarchy walking."""
        result = run_code('class X: pass\nprint(X.__bases__)')
        assert not result.success
        assert "__bases__" in (result.import_violation or "")

    def test_blocks_mro_attribute(self):
        """``cls.__mro__`` for hierarchy walking."""
        result = run_code('print(int.__mro__)')
        assert not result.success
        assert "__mro__" in (result.import_violation or "")

    def test_blocks_dict_attribute(self):
        """``obj.__dict__`` reveals object internals."""
        result = run_code('class X: pass\nprint(X.__dict__)')
        assert not result.success
        assert "__dict__" in (result.import_violation or "")

    def test_blocks_dunder_import_attribute(self):
        """``obj.__import__`` attribute access (rare but covers the
        ``builtins.__import__`` form)."""
        result = run_code('import datetime\ndatetime.__import__')
        assert not result.success
        assert "__import__" in (result.import_violation or "")

    def test_legitimate_verifier_code_still_works(self):
        """A realistic verifier function using allowed modules. The
        F-015 hardening must not over-block this — verify() functions
        like this are the normal Python-route success case."""
        code = """
import re
import datetime

def verify(subject, predicate, obj):
    if predicate == "is_year_in_range":
        try:
            year = int(obj)
            return 1900 <= year <= 2100
        except ValueError:
            return False
    return False

print('TRUE' if verify('x', 'is_year_in_range', '2020') else 'FALSE')
"""
        result = run_code(code)
        assert result.success, (
            f"Legitimate verifier code rejected: {result.import_violation or result.stderr}"
        )
        assert "TRUE" in result.stdout

    def test_legitimate_string_manipulation_still_works(self):
        """String / regex / fractions verification — common patterns
        in the Python verification corpus."""
        code = """
import re
from fractions import Fraction

def verify(subject, predicate, obj):
    if predicate == "matches_pattern":
        return bool(re.match(r'\\d{4}', obj))
    return False

print('TRUE' if verify('x', 'matches_pattern', '2026') else 'FALSE')
"""
        result = run_code(code)
        assert result.success
        assert "TRUE" in result.stdout


    def test_blocks_literal_class_traversal(self):
        """``''.__class__.__base__.__subclasses__()`` — the canonical
        CPython sandbox escape, starting from a string literal. The
        F-015 attribute check catches each dunder attribute even when
        the base of the chain is a literal expression rather than a
        named variable. (Initial design analysis suggested this might
        not be catchable, but the Attribute AST node is found by
        ``ast.walk`` regardless of where the base sits.)"""
        code = '"".__class__.__base__.__subclasses__()'
        result = run_code(code)
        assert not result.success
        # ast.walk visits attributes in tree order (outer first).
        # Whichever dunder is hit first is enough; the cascade is moot.
        violation = result.import_violation or ""
        assert any(
            d in violation
            for d in ("__class__", "__base__", "__subclasses__")
        ), f"Expected a dunder violation; got {violation!r}"


class TestF015KnownBypasses:
    """These tests document patterns the AST-walk hardening does NOT
    catch — the v0.15 sandbox's security boundary in writing. They use
    ``pytest.xfail(strict=False)`` so a future Option-B or Option-C
    upgrade (RestrictedPython, containerized) can run the same test
    suite and report xpass when the bypass is closed.

    See `aedos/utils/sandbox.py`'s docstring for the complete boundary
    statement; see `docs/phase_F/f3_design.md` §4 for the upgrade path.
    """

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "v0.15 known boundary — full encoded-string bypass not "
            "caught. Every dunder attribute must be built at runtime "
            "from chr literals to evade the AST attribute check. "
            "Upgrade to RestrictedPython (F3 §4 Option B) or "
            "containerized execution (Option C) for adversarial input."
        ),
    )
    def test_fully_encoded_dunder_chain_bypass(self):
        """``getattr(obj, chr(95)*2 + name + chr(95)*2)`` constructs
        each dunder name at runtime from `chr` literals. The AST sees
        only `getattr` calls and arithmetic; no `__class__`,
        `__base__`, or `__subclasses__` literal attribute appears in
        the source. F3 Option A's AST-walk has no signal to block.

        This is the documented v0.15 boundary — production deployments
        handling adversarial input must upgrade. The test asserts that
        a future stronger sandbox closes this bypass."""
        code = '''
def make_dunder(name):
    return chr(95) * 2 + name + chr(95) * 2

cls = getattr("", make_dunder("class"))
base = getattr(cls, make_dunder("base"))
subs_fn = getattr(base, make_dunder("subclasses"))
result = subs_fn()
print(len(result))
'''
        result = run_code(code)
        # If `not result.success`, the bypass is closed → xpass.
        # If `result.success`, the bypass is open → xfail (the
        # documented v0.15 boundary holds).
        assert not result.success, (
            "Future sandbox upgrade should close encoded-dunder bypass"
        )

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "v0.15 known boundary — eval-with-runtime-constructed-payload "
            "is not in the AST as `eval`. Bypass via, e.g., a captured "
            "reference assigned from a non-blocked builtin. Upgrade per "
            "F3 §4."
        ),
    )
    def test_indirect_eval_via_globals(self):
        """``vars()['eval']`` / ``locals()['eval']`` aren't direct
        ``eval`` Name references; the AST sees `vars`/`locals` calls
        plus subscript access. Neither is blocked by F-015 (we don't
        block all builtins, only the dangerous ones; ``vars`` is not
        on the block list because legitimate code uses it benignly)."""
        code = '''
e = vars(__builtins__)["eval"] if hasattr(__builtins__, "eval") else None
print(e("1 + 1") if e else "blocked")
'''
        result = run_code(code)
        # `__builtins__` Name is blocked by F-015's name check, so this
        # particular form IS caught. A future, more clever bypass would
        # avoid `__builtins__` entirely. The xfail documents the class
        # of attack, not this specific phrasing.
        assert not result.success


# ===========================================================================
# v0.16.2 — environment scrubbing / API-key non-leakage (the load-bearing
# property for a key-holding network deployment). The sandbox child is spawned
# with an explicitly built, secret-free env, so model-generated code cannot read
# the process's API keys via os.environ — even if it escapes the AST scan, the
# env channel carries no secret to leak.
# ===========================================================================

from aedos.utils.sandbox import _build_child_env, _SECRET_NAME_RE  # noqa: E402


class TestEnvScrubbing:
    def test_secret_name_regex_matches_credentials(self):
        for name in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GH_TOKEN",
                     "DB_PASSWORD", "AWS_SECRET_ACCESS_KEY", "SOME_CREDENTIAL"):
            assert _SECRET_NAME_RE.search(name), name
        for name in ("PATH", "SYSTEMROOT", "TEMP", "LANG", "PATHEXT"):
            assert not _SECRET_NAME_RE.search(name), name

    def test_build_child_env_excludes_api_keys(self, monkeypatch):
        # Plant secrets in the PARENT env; the constructed child env must omit
        # them (they are not in the non-secret passthrough allow-list).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-FAKE-must-not-leak")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-FAKE-must-not-leak")
        child_env = _build_child_env()
        assert "ANTHROPIC_API_KEY" not in child_env
        assert "OPENROUTER_API_KEY" not in child_env
        assert all("FAKE" not in v for v in child_env.values())

    def test_api_keys_unreadable_from_inside_sandbox(self, monkeypatch):
        # END-TO-END proof: plant keys in the parent, then run REAL sandbox code
        # that reads its own os.environ (os temporarily allowed so this tests the
        # ENV SCRUB itself, not the AST import block). The child must see None.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-FAKE-must-not-leak")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-FAKE-must-not-leak")
        code = (
            "import os\n"
            "print('ANTHROPIC=' + repr(os.environ.get('ANTHROPIC_API_KEY')))\n"
            "print('OPENROUTER=' + repr(os.environ.get('OPENROUTER_API_KEY')))\n"
        )
        result = run_code(code, extra_allowed=frozenset({"os"}))
        assert result.success, result.stderr
        assert "ANTHROPIC=None" in result.stdout
        assert "OPENROUTER=None" in result.stdout
        assert "FAKE" not in result.stdout
        assert "sk-ant" not in result.stdout and "sk-or" not in result.stdout

    def test_isolated_mode_does_not_break_allowed_stdlib(self):
        # `-I` must not block the allow-listed stdlib the verifier relies on.
        result = run_code(
            "import datetime, math, statistics\n"
            "print(datetime.date(2026, 1, 1).year + math.floor(math.pi))\n"
        )
        assert result.success, result.stderr
        assert "2029" in result.stdout


# ===========================================================================
# v0.16.2 deployment hardening — per-process resource limits (RLIMIT_AS /
# RLIMIT_CPU). WS-A scrubbed the env / added `-I`; it did NOT bound memory or
# CPU, so an escaped or runaway child could still OOM/peg the host. These
# exercise the REAL subprocess path (no mock): run_code forks `python -I -c
# <payload>` with the production preexec_fn that sets the rlimits before exec.
# ===========================================================================

import signal  # noqa: E402
import sys  # noqa: E402

from aedos.utils.sandbox import _DEFAULT_MEMORY_LIMIT_BYTES  # noqa: E402


@pytest.mark.skipif(
    sys.platform != "linux",
    reason=(
        "RLIMIT_AS / RLIMIT_CPU kill behavior is asserted against the real "
        "subprocess on Linux (the deploy target). The limits are applied on any "
        "POSIX platform; the import guard keeps the suite green on non-Linux."
    ),
)
class TestResourceLimits:
    def test_memory_bomb_dies_clean_not_consuming_host(self):
        """A 700MB allocation under the production 512MB RLIMIT_AS must fail
        with MemoryError (clean nonzero exit), NOT consume host memory.

        700MB is the discriminator: small enough that any host with ~1GB free
        could satisfy it, so a MemoryError here can ONLY be the 512MB cap —
        proving the deployed bound is active, not the host's RAM ceiling. (A
        naive multi-GB bomb would MemoryError even with the cap removed, proving
        nothing.) Asserting MemoryError specifically also distinguishes the
        clean in-process rlimit death from an OOM-killer SIGKILL."""
        assert _DEFAULT_MEMORY_LIMIT_BYTES == 512 * 1024 * 1024
        result = run_code("b = bytearray(700 * 1024 * 1024)\nprint(len(b))")
        assert not result.success
        assert result.exit_code != 0
        assert not result.timed_out, (
            "memory bomb should die on the allocation (MemoryError), not by "
            "wall-clock timeout"
        )
        assert "MemoryError" in result.stderr, (
            f"expected a clean MemoryError from the rlimit; got "
            f"stderr={result.stderr!r}"
        )

    def test_memory_cap_is_the_active_mechanism(self):
        """Host-independent proof that RLIMIT_AS — not host RAM — kills the
        over-budget allocation. With an explicit 256MB cap, 128MB SUCCEEDS while
        400MB fails with MemoryError; the boundary sits at the cap on ANY host.
        Passing memory_limit_bytes exercises the same real preexec path as
        production — only the budget changes, nothing is weakened."""
        cap = 256 * 1024 * 1024
        under = run_code("bytearray(128 * 1024 * 1024)", memory_limit_bytes=cap)
        assert under.success, (
            f"128MB under a 256MB cap must succeed; got stderr={under.stderr!r}"
        )
        over = run_code("bytearray(400 * 1024 * 1024)", memory_limit_bytes=cap)
        assert not over.success
        assert "MemoryError" in over.stderr, (
            f"400MB over a 256MB cap must MemoryError; got stderr={over.stderr!r}"
        )

    def test_cpu_spin_killed_by_rlimit_cpu(self):
        """A pure CPU spin must be killed by RLIMIT_CPU, demonstrably BEFORE the
        wall-clock timeout. A small cpu_seconds (1s) with a large wall-clock
        (20s) leaves the CPU rlimit as the only thing that can stop the spin in
        time — isolating it from the parent timer. Same production preexec_fn;
        only the CPU budget is small so SIGXCPU fires fast and deterministically."""
        result = run_code("while True:\n    pass\n", timeout_seconds=20, cpu_seconds=1)
        assert not result.success
        assert not result.timed_out, (
            "RLIMIT_CPU should kill the spin well before the 20s wall-clock; "
            f"got duration_ms={result.duration_ms}"
        )
        assert result.exit_code in (-signal.SIGXCPU, -signal.SIGKILL), (
            f"expected SIGXCPU/SIGKILL; got exit_code={result.exit_code}, "
            f"stderr={result.stderr!r}"
        )
        assert result.duration_ms < 10_000

    def test_normal_payload_unaffected_by_limits(self):
        """The production 512MB / CPU limits (with `-I` + scrubbed env) must not
        break legitimate verifier code — a normal computation still succeeds."""
        result = run_code(
            "from fractions import Fraction\n"
            "print(sum(Fraction(1, n) for n in range(1, 50)))"
        )
        assert result.success, f"normal payload broke under rlimits: {result.stderr!r}"
        assert result.stdout.strip()

    def test_default_cpu_budget_derives_from_timeout(self, monkeypatch):
        """When cpu_seconds is not given, the CPU budget defaults to the
        wall-clock timeout (max(1, int(timeout_seconds))). The spin test
        overrides cpu_seconds for determinism, so this asserts the DEFAULT
        derivation wiring directly (spy on _build_preexec_fn) — catching a
        regression in run_code's default path without a slow >timeout spin."""
        import aedos.utils.sandbox as sandbox_mod

        captured: dict[str, int] = {}
        real = sandbox_mod._build_preexec_fn

        def _spy(memory_limit_bytes, cpu_seconds):
            captured["cpu_seconds"] = cpu_seconds
            captured["memory_limit_bytes"] = memory_limit_bytes
            return real(memory_limit_bytes, cpu_seconds)

        monkeypatch.setattr(sandbox_mod, "_build_preexec_fn", _spy)
        run_code("print('ok')", timeout_seconds=7)
        assert captured["cpu_seconds"] == 7  # max(1, int(7))
        assert captured["memory_limit_bytes"] == _DEFAULT_MEMORY_LIMIT_BYTES
