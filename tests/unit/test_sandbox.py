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
