"""Guard the CLOSED abstain bucket-set <-> template mapping (v0.16.2 observability).

Every reason code the engine can emit must have a human template. These tests
fail CI if a new abstention reason is added to the extraction enum, the walker, or
the KB verifier without a matching template — keeping GET /verification/{id}'s
`abstention_line` complete. §3.2-neutral (presentation only)."""
from __future__ import annotations

import re
from pathlib import Path

import aedos
from aedos.layer1_extraction.triage import AbstentionReason
from aedos.layer5_result.abstention_templates import (
    ABSTENTION_TEMPLATES,
    abstention_line,
)

_SRC = Path(aedos.__file__).resolve().parent


def _scan(relpath: str, *patterns: str) -> set[str]:
    text = (_SRC / relpath).read_text(encoding="utf-8")
    found: set[str] = set()
    for pat in patterns:
        found.update(re.findall(pat, text))
    return found


# The authoritative closed set, enumerated from source (extraction enum + walker
# bare strings + KB-verifier bare strings + aggregator). Pinned explicitly so the
# rarer assignment forms (e.g. a variable in _compare_positive) are covered even
# when the source scans below miss them.
_KNOWN_REASONS = {
    # extraction (also covered programmatically by the enum test)
    "self_referential", "predicate_eq_object", "content_less_event",
    "subject_absent_from_source", "not_checkworthy",
    # walker
    "user_subject_required", "vague_subject_existential", "depth_exhausted",
    "budget_wall_clock", "budget_llm_calls", "budget_kb_work",
    "budget_kb_neighbor_probes", "budget_fanout",
    # aggregator
    "circuit_breaker_triggered",
    # KB verifier
    "unsupported_slot_to_qualifier", "lookup_subject_unresolved", "no_statements",
    "value_type_incompatible_binding", "value_type_unconfirmed_positive_gate",
    "value_unresolved", "no_matching_statement", "multi_valued_single_valued_predicate",
    "value_type_object_type_mismatch", "entity_claim_vs_literal_value",
    "approximate_date_no_year_match", "date_not_a_clean_mismatch",
}


class TestClosedSetCoverage:
    def test_every_extraction_enum_value_has_a_template(self):
        for member in AbstentionReason:
            assert member.value in ABSTENTION_TEMPLATES, (
                f"AbstentionReason.{member.name} ({member.value!r}) has no template"
            )

    def test_every_walker_reason_has_a_template(self):
        reasons = _scan("layer4_sources/walker.py", r'abstention_reason="([a-z_]+)"')
        assert reasons, "scan found no walker abstention reasons — pattern drift?"
        missing = reasons - set(ABSTENTION_TEMPLATES)
        assert not missing, f"walker reasons without a template: {sorted(missing)}"

    def test_every_kb_verifier_reason_has_a_template(self):
        reasons = _scan(
            "layer4_sources/kb_verifier.py",
            r'"abstention_reason":\s*"([a-z_]+)"',         # dict-literal form
            r'NO_MATCH,\s*None,\s*"([a-z_]+)"',            # _compare_positive tuple returns
        )
        assert reasons, "scan found no KB-verifier abstention reasons — pattern drift?"
        missing = reasons - set(ABSTENTION_TEMPLATES)
        assert not missing, f"KB-verifier reasons without a template: {sorted(missing)}"

    def test_explicit_known_closed_set_is_covered(self):
        missing = _KNOWN_REASONS - set(ABSTENTION_TEMPLATES)
        assert not missing, f"known closed-set reasons without a template: {sorted(missing)}"


class TestRenderer:
    def test_none_reason_renders_none(self):
        assert abstention_line(None) is None

    def test_known_reason_renders_template(self):
        line = abstention_line("no_statements")
        assert line == ABSTENTION_TEMPLATES["no_statements"]

    def test_subject_interpolation(self):
        line = abstention_line("lookup_subject_unresolved", subject="Obama")
        assert "Obama" in line

    def test_subject_placeholder_when_absent(self):
        line = abstention_line("subject_absent_from_source")
        assert line and "the subject" in line  # graceful placeholder

    def test_enum_member_is_normalized(self):
        line = abstention_line(AbstentionReason.NOT_CHECKWORTHY)
        assert line == ABSTENTION_TEMPLATES["not_checkworthy"]

    def test_unknown_reason_is_forward_safe(self):
        line = abstention_line("some_future_reason_code")
        assert line == "Could not verify (reason: some_future_reason_code)."
