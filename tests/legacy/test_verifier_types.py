"""Tests for the small verifier-types helpers (VerificationOutcome /
VerificationResult). These are dataclass + enum helpers but they
participate in branching downstream — worth locking the contract."""

from __future__ import annotations

from src.legacy.verifiers.types import VerificationOutcome, VerificationResult


def test_verified_property():
    r = VerificationResult(outcome=VerificationOutcome.VERIFIED)
    assert r.verified
    assert not r.contradicted
    assert not r.inconclusive


def test_contradicted_property():
    r = VerificationResult(outcome=VerificationOutcome.CONTRADICTED)
    assert not r.verified
    assert r.contradicted
    assert not r.inconclusive


def test_inconclusive_property():
    r = VerificationResult(outcome=VerificationOutcome.INCONCLUSIVE)
    assert not r.verified
    assert not r.contradicted
    assert r.inconclusive


def test_outcome_str_value():
    """Outcomes serialize to lowercase strings — the router and the
    pipeline_events JSON depend on this."""
    assert VerificationOutcome.VERIFIED.value == "verified"
    assert VerificationOutcome.CONTRADICTED.value == "contradicted"
    assert VerificationOutcome.INCONCLUSIVE.value == "inconclusive"


def test_to_dict_shape():
    r = VerificationResult(
        outcome=VerificationOutcome.VERIFIED,
        actual_value=42,
        explanation="counted 42",
    )
    d = r.to_dict()
    assert d == {
        "outcome": "verified",
        "actual_value": 42,
        "explanation": "counted 42",
    }


def test_to_dict_with_none_values():
    r = VerificationResult(outcome=VerificationOutcome.INCONCLUSIVE)
    d = r.to_dict()
    assert d["outcome"] == "inconclusive"
    assert d["actual_value"] is None
    assert d["explanation"] == ""
