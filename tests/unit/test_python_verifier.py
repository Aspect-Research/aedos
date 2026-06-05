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


class CountingTransport:
    """Records how many times the codegen transport is invoked. Used to PROVE
    (not merely infer from a raise) that the WS6 deterministic front-end never
    asks the LLM on a deterministic hit, while a fall-through claim does invoke
    it exactly once."""

    def __init__(self, code: str = "def verify(s, p, o): return True"):
        self._code = code
        self.calls = 0

    def extract_with_tool(self, *a, purpose=None, **kw):
        self.calls += 1
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


def _nd_claim() -> Claim:
    """A claim the v0.16.1 WS6 deterministic front-end does NOT recognize
    (string-operation predicate, non-numeric object) so it returns None and the
    LLM-codegen / harness path under test still runs. Use this in tests that
    exercise the codegen path itself (exception handling, empty code, no client,
    generated_code population) rather than the comparison semantics."""
    return _claim(subject="strawberry", predicate="count_char_r", object_val="three")


# ---------------------------------------------------------------------------
# v0.16.1 WS6 — deterministic front-end (item 6b)
#
# The deterministic path is tried FIRST inside verify() and grounds the common
# self-contained comparison / arithmetic / date-ordering claims by EXACT
# computation, BEFORE (and instead of) the flaky LLM codegen. To prove the
# deterministic verdict is what is returned — and that the codegen was not even
# asked — these use a transport whose codegen RAISES: a deterministic hit must
# still yield a real verdict (the raising codegen is never reached), while an
# unparseable claim must fall through to that raising codegen and abstain.
# ---------------------------------------------------------------------------

class TestDeterministicFrontEnd:
    def _pv_codegen_raises(self) -> PythonVerifier:
        # Codegen raises if reached; a deterministic hit returns before it.
        return _make_verifier(raises=True)

    def test_squared_is_verified(self):
        # Spec example: "10 squared is 100".
        result = self._pv_codegen_raises().verify(_claim("10", "squared", "100"))
        assert result.verdict == "verified"
        assert result.runtime_metadata.get("deterministic") is True

    def test_squared_is_contradicted(self):
        result = self._pv_codegen_raises().verify(_claim("10", "squared", "99"))
        assert result.verdict == "contradicted"

    def test_greater_than_verified(self):
        # Spec example: "100 > 50".
        result = self._pv_codegen_raises().verify(_claim("100", "greater_than", "50"))
        assert result.verdict == "verified"

    def test_greater_than_contradicted(self):
        # Spec example: "5 > 9".
        result = self._pv_codegen_raises().verify(_claim("5", "greater_than", "9"))
        assert result.verdict == "contradicted"

    def test_at_least_boundary_verified(self):
        result = self._pv_codegen_raises().verify(_claim("50", "at_least", "50"))
        assert result.verdict == "verified"

    def test_million_suffix_reused_from_quant_parser(self):
        # Reuses the walker's _parse_quantity suffix handling ("2 million").
        result = self._pv_codegen_raises().verify(
            _claim("60 million", "greater_than", "2 million"))
        assert result.verdict == "verified"

    def test_year_before_ordering_verified(self):
        result = self._pv_codegen_raises().verify(_claim("1643", "born_before", "1879"))
        assert result.verdict == "verified"

    def test_year_after_ordering_contradicted(self):
        result = self._pv_codegen_raises().verify(_claim("1643", "born_after", "1879"))
        assert result.verdict == "contradicted"

    def test_polarity_inverts_deterministic_verdict(self):
        # Negated comparison ("NOT 100 > 50") inverts to contradicted.
        result = self._pv_codegen_raises().verify(
            _claim("100", "greater_than", "50"))
        assert result.verdict == "verified"
        c = _claim("100", "greater_than", "50")
        c.polarity = 0
        result_neg = self._pv_codegen_raises().verify(c)
        assert result_neg.verdict == "contradicted"

    def test_works_without_llm_client(self):
        # The deterministic path needs no LLM, so it grounds even with no client.
        pv = PythonVerifier()  # no llm_client
        result = pv.verify(_claim("100", "greater_than", "50"))
        assert result.verdict == "verified"

    def test_premise_values_used_for_comparison(self):
        # WS3b: fetched premise values are used in place of literal slots.
        pv = PythonVerifier()
        premises = {
            "subject": {"value": "1643", "kb_property": "P569"},
            "object": {"value": "1879", "kb_property": "P569"},
        }
        result = pv.verify(_claim("Newton", "born_before", "Einstein"), premises=premises)
        assert result.verdict == "verified"

    # ---- None -> fallback (must reach codegen) ----

    def test_non_numeric_operand_falls_through(self):
        # "Q123 > Q50": operands are not strict numbers -> deterministic None ->
        # codegen runs (mock returns True here, so we observe a codegen verdict).
        pv = _make_verifier("def verify(s, p, o): return True")
        result = pv.verify(_claim("Q123", "greater_than", "Q50"))
        assert result.verdict == "verified"
        assert result.runtime_metadata.get("deterministic") is None

    def test_unsupported_predicate_falls_through(self):
        # is_prime is not a comparator/arith/order predicate -> None -> codegen.
        pv = _make_verifier(raises=True)
        result = pv.verify(_claim("7", "is_prime", "true"))
        assert result.verdict == "no_terminal_result"  # codegen raised -> abstain

    def test_comma_list_is_not_parsed_as_number(self):
        # "1,2,3,4,5 sum_equals 15": the comma-list must NOT parse as a number
        # (soundness) -> deterministic None -> codegen.
        pv = _make_verifier("def verify(s, p, o): return True")
        result = pv.verify(_claim("1,2,3,4,5", "sum_equals", "15"))
        # 'equals' substring present, but subject is a list, not a number:
        # deterministic abstains, codegen (mock True) is what answers.
        assert result.runtime_metadata.get("deterministic") is None

    def test_embedded_operand_arithmetic_falls_through(self):
        # "1973 plus_30_equals 2003": the 'plus' op token routes away from the
        # equals comparator, and the unary-arith path can't parse the embedded
        # 30, so deterministic abstains -> codegen.
        pv = _make_verifier(raises=True)
        result = pv.verify(_claim("1973", "plus_30_equals", "2003"))
        assert result.verdict == "no_terminal_result"


class TestDeterministicStringCount:
    """v0.16.4: exact vowel/consonant/letter/character/word counting over the
    subject literal — the deterministic counterpart of LLM codegen for
    'the word superstrawberry has 4 vowels'. Codegen RAISES, so a verdict here
    proves the count was computed deterministically, no LLM."""

    def _pv(self) -> PythonVerifier:
        return _make_verifier(raises=True)

    def test_vowel_count_verified(self):
        # superstrawberry: u,e,a,e = 4 vowels.
        r = self._pv().verify(_claim("superstrawberry", "vowel_count", "4"))
        assert r.verdict == "verified"
        assert r.runtime_metadata.get("deterministic") is True

    def test_vowel_count_contradicted(self):
        r = self._pv().verify(_claim("superstrawberry", "vowel_count", "9"))
        assert r.verdict == "contradicted"

    def test_subject_wrapper_and_object_unit_are_normalized(self):
        # The wrapped subject and a "N vowels" object still compute over the word.
        r = self._pv().verify(_claim("the word 'superstrawberry'", "vowel_count", "4 vowels"))
        assert r.verdict == "verified"

    def test_letter_and_character_and_word_counts(self):
        pv = self._pv()
        assert pv.verify(_claim("cat", "letter_count", "3")).verdict == "verified"
        assert pv.verify(_claim("cat", "letter_count", "5")).verdict == "contradicted"
        assert pv.verify(_claim("hello", "character_count", "5")).verdict == "verified"
        assert pv.verify(_claim("hello world", "word_count", "2")).verdict == "verified"

    def test_y_ambiguity_absorbed_never_false_contradicts(self):
        # 'rhythm' has 0 aeiou vowels and 1 if y counts. BOTH 0 and 1 verify;
        # only a count matching NEITHER interpretation contradicts (soundness).
        pv = self._pv()
        assert pv.verify(_claim("rhythm", "vowel_count", "0")).verdict == "verified"
        assert pv.verify(_claim("rhythm", "vowel_count", "1")).verdict == "verified"
        assert pv.verify(_claim("rhythm", "vowel_count", "3")).verdict == "contradicted"

    def test_polarity_inverts_count_verdict(self):
        c = _claim("cat", "vowel_count", "1")  # cat has 1 vowel -> verified
        assert self._pv().verify(c).verdict == "verified"
        c.polarity = 0                          # "cat does NOT have 1 vowel" -> contradicted
        assert self._pv().verify(c).verdict == "contradicted"

    def test_syllable_count_is_not_deterministic_falls_through(self):
        # Syllable counting is heuristic, so it is NOT in the exact front-end;
        # it falls through to codegen (here the raising stub -> no_terminal_result).
        r = self._pv().verify(_claim("banana", "syllable_count", "3"))
        assert r.verdict == "no_terminal_result"

    def test_non_count_predicate_with_letter_token_falls_through(self):
        # 'wrote_letter' merely contains 'letter' — it is NOT a count predicate.
        r = self._pv().verify(_claim("Lincoln", "wrote_letter", "3"))
        assert r.verdict == "no_terminal_result"

    def test_non_integer_object_falls_through(self):
        r = self._pv().verify(_claim("cat", "letter_count", "three"))
        assert r.verdict == "no_terminal_result"


# ---------------------------------------------------------------------------
# v0.16.1 WS6 — deterministic front-end SKIPS the LLM; fallback INVOKES it.
#
# These use a call-COUNTING codegen transport (rather than a raising one) to
# prove the LLM transport interaction directly: a deterministic hit must return
# a verdict with ZERO codegen calls; an unparseable claim must fall through and
# invoke codegen EXACTLY ONCE, preserving today's behavior. This is the explicit
# "did the deterministic path skip the LLM?" evidence the WS6 spec calls for.
# ---------------------------------------------------------------------------

class TestDeterministicSkipsLLM:
    def _pv(self, code: str = "def verify(s, p, o): return True"):
        transport = CountingTransport(code=code)
        pv = PythonVerifier(llm_client=LLMClient(_transport=transport))
        return pv, transport

    def test_greater_than_verified_without_llm_call(self):
        # "100 greater_than 50" -> verified deterministically, codegen NOT invoked.
        pv, transport = self._pv()
        result = pv.verify(_claim("100", "greater_than", "50"))
        assert result.verdict == "verified"
        assert result.runtime_metadata.get("deterministic") is True
        assert transport.calls == 0

    def test_greater_than_contradicted_without_llm_call(self):
        # "50 greater_than 100" -> contradicted deterministically, no LLM call.
        pv, transport = self._pv()
        result = pv.verify(_claim("50", "greater_than", "100"))
        assert result.verdict == "contradicted"
        assert result.runtime_metadata.get("deterministic") is True
        assert transport.calls == 0

    def test_squared_verified_without_llm_call(self):
        # "10 squared 100" -> verified deterministically, no LLM call.
        pv, transport = self._pv()
        result = pv.verify(_claim("10", "squared", "100"))
        assert result.verdict == "verified"
        assert result.runtime_metadata.get("deterministic") is True
        assert transport.calls == 0

    def test_wrong_arithmetic_contradicted_without_llm_call(self):
        # "10 squared 99" -> contradicted deterministically (real arithmetic),
        # no LLM call.
        pv, transport = self._pv()
        result = pv.verify(_claim("10", "squared", "99"))
        assert result.verdict == "contradicted"
        assert result.runtime_metadata.get("deterministic") is True
        assert transport.calls == 0

    def test_quantity_suffix_operand_verified_without_llm_call(self):
        # "60 million greater_than 2 million" -> verified via the reused
        # _parse_quantity suffix handling, no LLM call.
        pv, transport = self._pv()
        result = pv.verify(
            _claim("60 million", "population_greater_than", "2 million"))
        assert result.verdict == "verified"
        assert result.runtime_metadata.get("deterministic") is True
        assert transport.calls == 0

    def test_ambiguous_claim_falls_through_to_codegen(self):
        # AMBIGUOUS / unparseable (non-numeric operands) -> deterministic None ->
        # the codegen FALLBACK IS reached (transport invoked exactly once) and
        # answers as before.
        pv, transport = self._pv("def verify(s, p, o): return True")
        result = pv.verify(_claim("Q123", "greater_than", "Q50"))
        assert transport.calls == 1
        assert result.verdict == "verified"
        assert result.runtime_metadata.get("deterministic") is None

    def test_codegen_none_falls_through_to_abstain(self):
        # Fallback reached, and a None from codegen -> abstain (no_terminal_result).
        pv, transport = self._pv("def verify(s, p, o): return None")
        result = pv.verify(_claim("Q123", "greater_than", "Q50"))
        assert transport.calls == 1
        assert result.verdict == "no_terminal_result"

    def test_backcompat_unparseable_claim_unchanged(self):
        # Back-compat: a claim that previously went to codegen and is not
        # deterministically parseable behaves EXACTLY as before — codegen runs
        # and its verdict is returned, with no deterministic marker.
        code = "def verify(s, p, o):\n    return s.count('r') == int(o)"
        pv, transport = self._pv(code)
        result = pv.verify(_claim("strawberry", "count_r_equals", "3"))
        assert transport.calls == 1
        assert result.verdict == "verified"
        assert result.runtime_metadata.get("deterministic") is None
        assert "verify" in result.generated_code


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
        # A claim the deterministic front-end ignores, so the no-client path
        # (not a deterministic verdict) is what is exercised here.
        result = pv.verify(_nd_claim())
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
        result = pv.verify(_nd_claim())
        assert result.verdict == "no_terminal_result"
        assert "import_violation" in result.runtime_metadata

    def test_runtime_exception_returns_no_terminal_result(self):
        code = "def verify(s, p, o): raise ValueError('boom')"
        pv = _make_verifier(code)
        result = pv.verify(_nd_claim())
        assert result.verdict == "no_terminal_result"
        assert "exception_info" in result.runtime_metadata

    def test_syntax_error_returns_no_terminal_result(self):
        code = "def verify(s, p, o):\n  return @@invalid"
        pv = _make_verifier(code)
        result = pv.verify(_nd_claim())
        assert result.verdict == "no_terminal_result"

    def test_llm_error_returns_no_terminal_result(self):
        pv = _make_verifier(raises=True)
        result = pv.verify(_nd_claim())
        assert result.verdict == "no_terminal_result"
        assert "exception_info" in result.runtime_metadata

    def test_llm_returns_empty_code_no_terminal_result(self):
        client = LLMClient(_transport=type("T", (), {
            "extract_with_tool": lambda *a, **kw: {"code": "", "reasoning": ""},
            "chat": lambda *a, **kw: "",
        })())
        pv = PythonVerifier(llm_client=client)
        result = pv.verify(_nd_claim())
        assert result.verdict == "no_terminal_result"


# ---------------------------------------------------------------------------
# Trace/metadata fields
# ---------------------------------------------------------------------------

class TestVerdictFields:
    def test_generated_code_populated(self):
        # Use a claim the deterministic front-end ignores so the codegen path
        # (which populates generated_code) actually runs.
        code = "def verify(s, p, o): return True"
        pv = _make_verifier(code)
        result = pv.verify(_nd_claim())
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
        # _nd_claim: the deterministic front-end abstains, so the harness's
        # truthy-non-bool -> verified mapping (the behavior under test) runs.
        code = "def verify(s, p, o): return 1"
        pv = _make_verifier(code)
        result = pv.verify(_nd_claim())
        assert result.verdict == "verified"

    def test_falsy_non_bool_counts_as_contradicted(self):
        code = "def verify(s, p, o): return 0"
        pv = _make_verifier(code)
        result = pv.verify(_nd_claim())
        assert result.verdict == "contradicted"
