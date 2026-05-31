"""v0.16.1 WS3 Step 0: vague-class instance check (`_verify_vague_class_instance`).

When a claim's OBJECT is a vague descriptive class ("a town in the United
States", "a state that borders New York") the walker cannot match it as a
literal entity, so historically it abstained. WS3 Step 0 adds a SOUND positive
grounding path that fires ONLY on a confirmed class membership, using the same
subsumption AUTHORITY the walker already trusts — `verify_transitive_path`
over `is_a` (= P31|P279+). It NEVER admits a cold LLM positive; on any
non-resolution, definite negative, fail-open KB error, nogood veto, or no-KB
it returns None and the walk keeps abstaining (§3.2 — abstain is safe).

These tests drive the check end-to-end through `walker.walk` with mocked
Substrate + KB doubles (the same shapes test_walker_kb_neighbors.py uses):

  * subject IS subsumption-confirmed an instance of the resolved class -> VERIFIED,
    grounded via the KB `is_a` transitive path (source == "kb").
  * the vague class can't be resolved to a Q-id            -> abstain (no guess).
  * the subject can't be resolved to a Q-id                -> abstain.
  * the subsumption path does NOT hold (definite negative) -> abstain (never contradict).
  * the KB ASK fails open (error set)                      -> abstain (never false-verify).
  * a nogood veto on the edge                              -> abstain.
  * no KB attached to the walker                           -> abstain.
  * the head extractor cannot isolate a class noun         -> abstain.

The unit-level helper tests pin `_vague_class_head` / `_is_vague_class_object`
directly so the head-extraction contract stays sound.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer3_substrate import Substrate
from aedos.layer4_sources.kb_protocol import (
    ResolutionCandidate,
    SubsumptionResult,
    TransitivePathResult,
)
from aedos.layer4_sources.kb_verifier import KBVerdictType, KBVerdict
from aedos.layer4_sources.tier_u import LookupResult
from aedos.layer4_sources.walker import (
    VerificationContext,
    Walker,
    _is_vague_class_object,
    _vague_class_head,
)


# ---------------------------------------------------------------------------
# Helpers — mirror the doubles used by test_walker_kb_neighbors.py
# ---------------------------------------------------------------------------

# Subject "Williamstown" and class head "town" resolve to distinct Q-ids so we
# can assert verify_transitive_path is asked the SUBJECT is_a CLASS question
# with the right operands.
_SUBJECT_QID = "Q49237"   # Williamstown, Massachusetts
_CLASS_QID = "Q3957"      # town (the class)


def _claim(
    subject="Williamstown",
    predicate="is",
    object_val="a town in the United States",
    polarity=1,
):
    return Claim(
        claim_id="c1",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=polarity,
        source_text="test",
        asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
    )


def _ctx():
    return VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(),
        asserting_party="user_test",
    )


class _MockTierU:
    """Tier U with no priors — the vague-class path is the only grounding
    candidate, so the walk's verdict is decided solely by the WS3 Step 0 check
    (when it fires) or abstains (when it does not)."""

    def lookup(self, claim, current_time=None, exclude_row_ids=None):
        return LookupResult(found=False)

    def lookup_object_conflict(self, claim, current_time=None):
        return LookupResult(found=False)


class _NoMatchKBVerifier:
    def verify(self, claim, current_time=None, source_text=None):
        return KBVerdict(verdict=KBVerdictType.NO_MATCH)


def _resolver_by_surface(mapping: dict[str, str | None]):
    """A resolver that maps a surface form (case-insensitive substring of the
    class head / subject) to a Q-id, so the subject and the class head can
    resolve to DIFFERENT Q-ids. A surface absent from the map resolves to []."""
    resolver = MagicMock()

    def _resolve(surface, local_context):
        qid = mapping.get(surface)
        if qid is None:
            return []
        return [ResolutionCandidate(kb_identifier=qid, score=1.0)]

    resolver.resolve.side_effect = _resolve
    return resolver


def _make_substrate(resolver):
    """A Substrate whose subsumption oracle returns nothing (so the only
    positive grounding for a vague-class object is the WS3 Step 0 KB
    transitive path), with the given resolver."""
    pd = MagicMock()
    dv = MagicMock()
    dv.verdict.value = "neither"
    dv.was_cached = True
    pd.consult.return_value = dv
    sub = MagicMock()
    sub.find_neighbors.return_value = []
    sub.consult.return_value = SubsumptionResult(verdict="unrelated")
    pt = MagicMock()
    return Substrate(
        resolver=resolver,
        predicate_translation=pt,
        subsumption=sub,
        predicate_distribution=pd,
    )


def _make_kb(*, holds: bool = True, error: str | None = None):
    """A KB double whose `verify_transitive_path` is the sole authority the
    Step 0 check consults. enumerate_neighbors returns nothing so the only
    positive path is the transitive ASK."""
    kb = MagicMock()
    kb.verify_transitive_path.return_value = TransitivePathResult(
        holds=holds, error=error, establishing_property="P31"
    )

    def _enum(entity, properties, direction="outgoing"):
        return {p: [] for p in properties}

    kb.enumerate_neighbors.side_effect = _enum
    return kb


def _make_walker(substrate, kb):
    return Walker(
        tier_u=_MockTierU(),
        kb_verifier=_NoMatchKBVerifier(),
        python_verifier=None,
        substrate=substrate,
        kb=kb,
    )


# ---------------------------------------------------------------------------
# Head-extraction contract (pure helpers)
# ---------------------------------------------------------------------------

class TestVagueClassHelpers:
    def test_recognizes_indefinite_article_objects(self):
        assert _is_vague_class_object("a town in the United States")
        assert _is_vague_class_object("an institution founded before 1800")
        assert _is_vague_class_object("some river")
        assert _is_vague_class_object("a state that borders New York")

    def test_non_vague_objects_are_not_flagged(self):
        assert not _is_vague_class_object("Williamstown")
        assert not _is_vague_class_object("Massachusetts")
        assert not _is_vague_class_object("")

    def test_head_strips_article_and_cuts_at_modifier(self):
        assert _vague_class_head("a town in the United States") == "town"
        assert _vague_class_head("a state that borders New York") == "state"
        assert _vague_class_head("an institution founded before 1800") == "institution"
        assert _vague_class_head("some river") == "river"

    def test_head_none_when_no_class_noun(self):
        # No indefinite-article prefix -> no head isolated.
        assert _vague_class_head("Williamstown") is None
        # Article only, nothing after it.
        assert _vague_class_head("a ") is None
        assert _vague_class_head("") is None


# ---------------------------------------------------------------------------
# Step 0: positive grounding via subsumption authority
# ---------------------------------------------------------------------------

class TestVagueClassVerified:
    def test_confirmed_instance_verifies_via_kb_is_a(self):
        """Subject IS a subsumption-confirmed instance of the resolved class:
        the KB `is_a` transitive path HOLDS (holds=True, error=None) -> VERIFIED.
        The grounding is the KB subsumption authority, never a cold LLM
        positive."""
        resolver = _resolver_by_surface(
            {"town": _CLASS_QID, "Williamstown": _SUBJECT_QID}
        )
        substrate = _make_substrate(resolver)
        kb = _make_kb(holds=True)
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(), _ctx())

        assert result.verdict == "verified"
        # The transitive-path ASK was asked the SUBJECT is_a CLASS question.
        kb.verify_transitive_path.assert_called()
        call = kb.verify_transitive_path.call_args
        assert call.args[0] == _SUBJECT_QID
        assert call.args[1] == _CLASS_QID
        assert call.kwargs.get("relation_type") == "is_a"
        # The grounding edge is a KB premise_lookup with the vague_class_instance
        # marker — the verdict rests on the KB subsumption authority.
        vague_edges = [
            e for e in result.trace.edges
            if e.metadata.get("grounding") == "vague_class_instance"
        ]
        assert vague_edges, (
            "expected a vague_class_instance KB grounding edge; got "
            f"{[e.metadata for e in result.trace.edges]}"
        )
        edge = vague_edges[0]
        assert edge.metadata["source"] == "kb"
        assert edge.metadata["verdict"] == "verified"
        assert edge.metadata["relation_type"] == "is_a"
        assert edge.metadata["subject_qid"] == _SUBJECT_QID
        assert edge.metadata["class_qid"] == _CLASS_QID
        assert result.trace.source_breakdown.get("kb", 0) >= 1


# ---------------------------------------------------------------------------
# Step 0: every uncertainty abstains (no false-verify, no guess, no contradict)
# ---------------------------------------------------------------------------

class TestVagueClassAbstains:
    def test_unresolvable_class_abstains(self):
        """The vague class does not resolve to a Q-id -> abstain. No guess:
        the KB transitive ASK is never even reached (no class operand)."""
        # Subject resolves; class head "town" does NOT.
        resolver = _resolver_by_surface({"Williamstown": _SUBJECT_QID})
        substrate = _make_substrate(resolver)
        kb = _make_kb(holds=True)  # would verify IF asked — proving the abstain
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(), _ctx())

        assert result.verdict == "no_grounding_found"
        # No vague-class grounding edge survives.
        assert not [
            e for e in result.trace.edges
            if e.metadata.get("grounding") == "vague_class_instance"
        ]
        # The transitive ASK was never asked the is_a question for the class —
        # an unresolved class cannot be a transitive-path operand.
        for c in kb.verify_transitive_path.call_args_list:
            assert c.args[1] != _CLASS_QID

    def test_unresolvable_subject_abstains(self):
        """The subject does not resolve to a Q-id -> abstain (no operand to
        ask the is_a question about)."""
        resolver = _resolver_by_surface({"town": _CLASS_QID})  # subject absent
        substrate = _make_substrate(resolver)
        kb = _make_kb(holds=True)
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(), _ctx())

        assert result.verdict == "no_grounding_found"
        assert not [
            e for e in result.trace.edges
            if e.metadata.get("grounding") == "vague_class_instance"
        ]

    def test_subsumption_unrelated_abstains_not_verifies(self):
        """Both endpoints resolve, but the is_a path does NOT hold (definite
        negative: holds=False, error=None). The subject is NOT an instance of
        the resolved class -> abstain. Never a false-verify, and never a
        contradiction (a non-membership is abstention, not refutation)."""
        resolver = _resolver_by_surface(
            {"town": _CLASS_QID, "Williamstown": _SUBJECT_QID}
        )
        substrate = _make_substrate(resolver)
        kb = _make_kb(holds=False, error=None)  # DEFINITE negative
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(), _ctx())

        assert result.verdict == "no_grounding_found"
        assert result.verdict != "contradicted"
        assert not [
            e for e in result.trace.edges
            if e.metadata.get("grounding") == "vague_class_instance"
        ]

    def test_kb_fail_open_error_abstains(self):
        """The KB transitive ASK fails open (error set) -> abstain. A
        fail-open answer is NOT authoritative; it must never false-verify
        (§3.2 fail-closed on uncertainty)."""
        resolver = _resolver_by_surface(
            {"town": _CLASS_QID, "Williamstown": _SUBJECT_QID}
        )
        substrate = _make_substrate(resolver)
        kb = _make_kb(holds=False, error="simulated SPARQL timeout")
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(), _ctx())

        assert result.verdict == "no_grounding_found"
        assert not [
            e for e in result.trace.edges
            if e.metadata.get("grounding") == "vague_class_instance"
        ]

    def test_no_kb_abstains(self):
        """A walker with no KB attached cannot run the check -> abstain."""
        resolver = _resolver_by_surface(
            {"town": _CLASS_QID, "Williamstown": _SUBJECT_QID}
        )
        substrate = _make_substrate(resolver)
        walker = Walker(
            tier_u=_MockTierU(),
            kb_verifier=_NoMatchKBVerifier(),
            python_verifier=None,
            substrate=substrate,
            kb=None,
        )

        result = walker.walk(_claim(), _ctx())

        assert result.verdict == "no_grounding_found"
        assert not [
            e for e in result.trace.edges
            if e.metadata.get("grounding") == "vague_class_instance"
        ]

    def test_negative_polarity_does_not_fire(self):
        """The Step 0 positive grounding is gated on polarity == 1. A negated
        claim ("X is NOT a town") does not take the vague-class verify path —
        the KB transitive ASK for the is_a membership is not used to verify a
        negation."""
        resolver = _resolver_by_surface(
            {"town": _CLASS_QID, "Williamstown": _SUBJECT_QID}
        )
        substrate = _make_substrate(resolver)
        kb = _make_kb(holds=True)
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(polarity=0), _ctx())

        # No vague-class verified grounding for a negated claim.
        assert not [
            e for e in result.trace.edges
            if e.metadata.get("grounding") == "vague_class_instance"
        ]


# ---------------------------------------------------------------------------
# Step 0: nogood veto forecloses the edge (entailment-safety)
# ---------------------------------------------------------------------------

class TestVagueClassNogoodVeto:
    def test_nogood_veto_abstains_without_network(self):
        """A cached nogood for (is_a, subject -> class) forecloses the edge: the
        check returns None (abstain) WITHOUT a transitive ASK round-trip."""
        resolver = _resolver_by_surface(
            {"town": _CLASS_QID, "Williamstown": _SUBJECT_QID}
        )
        substrate = _make_substrate(resolver)
        kb = _make_kb(holds=True)  # would verify if asked
        walker = _make_walker(substrate, kb)

        # Attach an exception cache that vetoes exactly this edge.
        cache = MagicMock()

        def _is_nogood(relation_type, source_identifier, target_identifier):
            return (
                relation_type == "is_a"
                and source_identifier == _SUBJECT_QID
                and target_identifier == _CLASS_QID
            )

        cache.is_nogood.side_effect = _is_nogood
        walker._exception_cache = cache

        result = walker.walk(_claim(), _ctx())

        assert result.verdict == "no_grounding_found"
        assert not [
            e for e in result.trace.edges
            if e.metadata.get("grounding") == "vague_class_instance"
        ]
        # The veto fired BEFORE the transitive ASK — the is_a question for this
        # exact (subject, class) pair was never asked.
        for c in kb.verify_transitive_path.call_args_list:
            assert not (
                c.args[0] == _SUBJECT_QID
                and c.args[1] == _CLASS_QID
                and c.kwargs.get("relation_type") == "is_a"
            )
