"""Tests for PythonVerifier — mocked LLM, real sandbox execution."""

from __future__ import annotations

import pytest

from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer4_sources.python_verifier import PythonVerdict, PythonVerifier
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockTransport:
    def __init__(self, code: str = "def verify(s, p, o): return True", raises: bool = False):
        self._code = code
        self._raises = raises

    def extract_with_tool(self, *a, purpose=None, **kw):
        if self._raises:
            raise RuntimeError("mock LLM error")
        return {"code": self._code, "reasoning": "mock"}

    def chat(self, *a, **kw):
        return ""


def _make_verifier(code: str = "def verify(s, p, o): return True", raises: bool = False) -> PythonVerifier:
    client = LLMClient(_transport=MockTransport(code=code, raises=raises))
    return PythonVerifier(llm_client=client)


def _claim(subject: str = "4", predicate: str = "less_than", object_val: str = "7") -> Claim:
    return Claim(
        claim_id="c1",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=1,
        source_text="test",
        asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
    )


# ---------------------------------------------------------------------------
# PythonVerdict dataclass
# ---------------------------------------------------------------------------

class TestPythonVerdictDataclass:
    def test_fields_present(self):
        v = PythonVerdict(verdict="verified")
        assert v.verdict == "verified"
        assert v.generated_code == ""
        assert v.inputs == {}
        assert v.output is None
        assert v.runtime_metadata == {}

    def test_verdict_values(self):
        for val in ("verified", "contradicted", "no_terminal_result"):
            v = PythonVerdict(verdict=val)
            assert v.verdict == val


# ---------------------------------------------------------------------------
# No LLM client
# ---------------------------------------------------------------------------

class TestPythonVerifierNoClient:
    def test_no_client_returns_no_terminal_result(self):
        pv = PythonVerifier()
        result = pv.verify(_claim())
        assert result.verdict == "no_terminal_result"

    def test_inputs_populated_even_without_client(self):
        pv = PythonVerifier()
        result = pv.verify(_claim(subject="hello", predicate="startswith", object_val="he"))
        assert result.inputs["subject"] == "hello"


# ---------------------------------------------------------------------------
# Numerical comparison
# ---------------------------------------------------------------------------

class TestNumericalComparison:
    def test_true_comparison_returns_verified(self):
        code = "def verify(s, p, o): return int(s) < int(o)"
        pv = _make_verifier(code)
        result = pv.verify(_claim("4", "less_than", "7"))
        assert result.verdict == "verified"

    def test_false_comparison_returns_contradicted(self):
        code = "def verify(s, p, o): return int(s) < int(o)"
        pv = _make_verifier(code)
        result = pv.verify(_claim("10", "less_than", "3"))
        assert result.verdict == "contradicted"

    def test_equal_comparison(self):
        code = "def verify(s, p, o): return int(s) == int(o)"
        pv = _make_verifier(code)
        result = pv.verify(_claim("5", "equals", "5"))
        assert result.verdict == "verified"

    def test_not_equal_returns_contradicted(self):
        code = "def verify(s, p, o): return int(s) == int(o)"
        pv = _make_verifier(code)
        result = pv.verify(_claim("5", "equals", "6"))
        assert result.verdict == "contradicted"


# ---------------------------------------------------------------------------
# Date arithmetic
# ---------------------------------------------------------------------------

class TestDateArithmetic:
    def test_days_after_verified(self):
        code = (
            "from datetime import date\n"
            "def verify(s, p, o):\n"
            "    start = date(2026, 2, 6)\n"
            "    end = date(2026, 5, 17)\n"
            "    return (end - start).days == int(o)\n"
        )
        pv = _make_verifier(code)
        result = pv.verify(_claim("2026-05-17", "days_after_2026-02-06", "100"))
        assert result.verdict == "verified"

    def test_days_after_contradicted(self):
        code = (
            "from datetime import date\n"
            "def verify(s, p, o):\n"
            "    start = date(2026, 2, 6)\n"
            "    end = date(2026, 5, 17)\n"
            "    return (end - start).days == int(o)\n"
        )
        pv = _make_verifier(code)
        result = pv.verify(_claim("2026-05-17", "days_after_2026-02-06", "200"))
        assert result.verdict == "contradicted"

    def test_birth_year_arithmetic(self):
        code = (
            "def verify(s, p, o):\n"
            "    birth_year = int(s)\n"
            "    return birth_year + 30 == int(o)\n"
        )
        pv = _make_verifier(code)
        result = pv.verify(_claim("1973", "plus_30_equals", "2003"))
        assert result.verdict == "verified"


# ---------------------------------------------------------------------------
# String operations
# ---------------------------------------------------------------------------

class TestStringOperations:
    def test_character_count_verified(self):
        code = (
            "def verify(s, p, o):\n"
            "    return s.count('r') == int(o)\n"
        )
        pv = _make_verifier(code)
        result = pv.verify(_claim("strawberry", "count_r_equals", "3"))
        assert result.verdict == "verified"

    def test_character_count_contradicted(self):
        code = (
            "def verify(s, p, o):\n"
            "    return s.count('r') == int(o)\n"
        )
        pv = _make_verifier(code)
        result = pv.verify(_claim("strawberry", "count_r_equals", "2"))
        assert result.verdict == "contradicted"

    def test_startswith_verified(self):
        code = "def verify(s, p, o): return s.startswith(o)"
        pv = _make_verifier(code)
        result = pv.verify(_claim("hello", "startswith", "he"))
        assert result.verdict == "verified"

    def test_string_length_verified(self):
        code = "def verify(s, p, o): return len(s) == int(o)"
        pv = _make_verifier(code)
        result = pv.verify(_claim("aedos", "length_equals", "5"))
        assert result.verdict == "verified"


# ---------------------------------------------------------------------------
# Exception / error cases
# ---------------------------------------------------------------------------

class TestExceptionCases:
    def test_disallowed_import_returns_no_terminal_result(self):
        code = "import os\ndef verify(s, p, o): return os.path.exists(o)"
        pv = _make_verifier(code)
        result = pv.verify(_claim())
        assert result.verdict == "no_terminal_result"
        assert "import_violation" in result.runtime_metadata

    def test_runtime_exception_returns_no_terminal_result(self):
        code = "def verify(s, p, o): raise ValueError('boom')"
        pv = _make_verifier(code)
        result = pv.verify(_claim())
        assert result.verdict == "no_terminal_result"
        assert "exception_info" in result.runtime_metadata

    def test_syntax_error_returns_no_terminal_result(self):
        code = "def verify(s, p, o):\n  return @@invalid"
        pv = _make_verifier(code)
        result = pv.verify(_claim())
        assert result.verdict == "no_terminal_result"

    def test_llm_error_returns_no_terminal_result(self):
        pv = _make_verifier(raises=True)
        result = pv.verify(_claim())
        assert result.verdict == "no_terminal_result"
        assert "exception_info" in result.runtime_metadata

    def test_llm_returns_empty_code_no_terminal_result(self):
        client = LLMClient(_transport=type("T", (), {
            "extract_with_tool": lambda *a, **kw: {"code": "", "reasoning": ""},
            "chat": lambda *a, **kw: "",
        })())
        pv = PythonVerifier(llm_client=client)
        result = pv.verify(_claim())
        assert result.verdict == "no_terminal_result"


# ---------------------------------------------------------------------------
# Trace/metadata fields
# ---------------------------------------------------------------------------

class TestVerdictFields:
    def test_generated_code_populated(self):
        code = "def verify(s, p, o): return True"
        pv = _make_verifier(code)
        result = pv.verify(_claim())
        assert "verify" in result.generated_code

    def test_inputs_dict_populated(self):
        pv = _make_verifier()
        result = pv.verify(_claim("4", "less_than", "7"))
        assert result.inputs == {"subject": "4", "predicate": "less_than", "object": "7"}

    def test_output_populated_on_success(self):
        pv = _make_verifier("def verify(s, p, o): return True")
        result = pv.verify(_claim())
        assert result.output is not None

    def test_runtime_ms_non_negative(self):
        pv = _make_verifier()
        result = pv.verify(_claim())
        assert result.runtime_metadata.get("runtime_ms", 0) >= 0

    def test_truthy_non_bool_counts_as_verified(self):
        code = "def verify(s, p, o): return 1"
        pv = _make_verifier(code)
        result = pv.verify(_claim())
        assert result.verdict == "verified"

    def test_falsy_non_bool_counts_as_contradicted(self):
        code = "def verify(s, p, o): return 0"
        pv = _make_verifier(code)
        result = pv.verify(_claim())
        assert result.verdict == "contradicted"
