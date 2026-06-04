"""Tests for the v0.16 WS6 T1 interval-from-events resolver in the walker.

Covers (spec docs/v0_16/06_temporal.md §B):
  * `_iso_or_none` — BEFORE_PRESENT and empty → None (open end).
  * (`_interval_holds_at` and its three-valued holds-at-T suite were removed in
        v0.16.1 WS4 — the primitive had no verdict-path consumer; see the walker
        note where it lived.)
  * `_interval_from_statements` — P580/P582 gathering, preferred-rank
        disambiguation, conflicting-start abstention, open-end dominance.
  * `_verify_interval_endpoint` — verified / contradicted / abstain, driven by
        a P580/P582 fixture (Einstein P108 → IAS preferred).
  * FAIL-CLOSED: `_gather_interval` returns None on a KB error / resolution
        failure / ambiguous multiple-start; `_verify_interval_endpoint` returns
        None (abstain) on an open / unknown endpoint and a non-endpoint
        predicate.

The interval methods are unit-level — they are exercised through directly
constructed stubs rather than a full pipeline, mirroring the discipline in
test_walker.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.temporal import BEFORE_PRESENT
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer4_sources.kb_protocol import Statement
from aedos.layer5_result.trace import JustificationTrace, TraceNode
from aedos.layer4_sources.walker import (
    Interval,
    VerificationContext,
    Walker,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _StubResolver:
    """Resolves a fixed subject reference to a Q-id; everything else → None.
    Optionally raises to simulate a resolver-layer KB error."""

    def __init__(self, resolutions=None, raise_on_resolve=False, cache_row_id=None):
        self._resolutions = resolutions or {"Einstein": "Q937"}
        self._raise = raise_on_resolve
        self._cache_row_id = cache_row_id

    def resolve(self, reference, local_context):
        if self._raise:
            raise RuntimeError("simulated resolver error")
        qid = self._resolutions.get(reference)
        return [qid] if qid else []

    def select(self, candidates, local_context):
        return candidates[0] if candidates else None

    def last_cache_row_id(self):
        return self._cache_row_id


class _StubKBVerifier:
    """Carries a `_resolver` attribute, as the walker expects."""

    def __init__(self, resolver):
        self._resolver = resolver


class _StubKB:
    """lookup_statements keyed by (entity, property). Optionally raises.

    v0.16.1 WS5c: when `qualifier_keys` is provided the stub exposes the
    KBProtocol `interval_qualifier_keys` accessor returning it, so a test can
    confirm CORE reads the start/end qualifier keys from the adapter rather than
    a hardcoded P-id. When omitted, the stub has NO accessor (mirroring an
    adapter predating WS5c) and the walker's fail-safe (P580, P582) default
    applies — the behavior-neutral path the bulk of the suite exercises.
    """

    def __init__(self, statements_by_key=None, raise_on_lookup=False,
                 qualifier_keys=None):
        self._by_key = statements_by_key or {}
        self._raise = raise_on_lookup
        if qualifier_keys is not None:
            self.interval_qualifier_keys = lambda: tuple(qualifier_keys)

    def lookup_statements(self, entity, prop):
        if self._raise:
            raise RuntimeError("simulated KB lookup error")
        return list(self._by_key.get((entity, prop), []))


class _StubTierU:
    """A Tier U whose lookup returns a configurable endpoint row (or nothing)."""

    def __init__(self, rows=None, found=False):
        self._rows = rows or []
        self._found = found

    def lookup(self, claim, current_time=None, exclude_row_ids=None):
        from aedos.layer4_sources.tier_u import LookupResult
        if self._found:
            return LookupResult(found=True, rows=list(self._rows))
        return LookupResult(found=False)


class _StubMeta:
    def __init__(self, kb_property, single_valued=False, routing_hint="kb_interval"):
        self.kb_property = kb_property
        self.single_valued = single_valued
        self.routing_hint = routing_hint


class _StubPT:
    """predicate_translation.consult → fixed meta keyed by predicate."""

    def __init__(self, meta_by_pred):
        self._by_pred = meta_by_pred

    def consult(self, predicate, kb_namespace=None):
        meta = self._by_pred.get(predicate)
        if meta is None:
            raise KeyError(predicate)
        return meta


class _StubSubstrate:
    def __init__(self, pt):
        self.predicate_translation = pt


def _make_walker(*, kb=None, resolver=None, tier_u=None, meta_by_pred=None):
    resolver = resolver if resolver is not None else _StubResolver()
    kb_verifier = _StubKBVerifier(resolver)
    kb = kb if kb is not None else _StubKB()
    tier_u = tier_u if tier_u is not None else _StubTierU()
    substrate = _StubSubstrate(_StubPT(meta_by_pred or {}))
    return Walker(
        tier_u=tier_u,
        kb_verifier=kb_verifier,
        python_verifier=None,
        substrate=substrate,
        kb=kb,
    )


def _claim(subject="Einstein", predicate="employment_started",
           object_val="1933", polarity=1):
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


def _ctx(current_time=None):
    return VerificationContext(
        current_time=current_time or datetime.now(timezone.utc).isoformat(),
        asserting_party="user_test",
    )


def _trace():
    return JustificationTrace(
        root=TraceNode(node_type="claim", content={}),
        source_breakdown={"tier_u": 0, "kb": 0, "python": 0},
    )


# ---------------------------------------------------------------------------
# _iso_or_none
# ---------------------------------------------------------------------------

class TestIsoOrNone:
    def test_none_value_is_none(self):
        assert Walker._iso_or_none(None) is None

    def test_before_present_maps_to_none(self):
        # BEFORE_PRESENT is the implicit-past end sentinel → open end (never
        # forces a false holds_at).
        assert Walker._iso_or_none(BEFORE_PRESENT) is None

    def test_empty_string_is_none(self):
        assert Walker._iso_or_none("") is None
        assert Walker._iso_or_none("   ") is None

    def test_iso_date_passes_through(self):
        assert Walker._iso_or_none("1933-10-01") == "1933-10-01"
        assert Walker._iso_or_none("1912") == "1912"


# ---------------------------------------------------------------------------
# v0.16.1 WS4: the _interval_holds_at three-valued primitive (and its
# TestIntervalHoldsAt suite) were REMOVED — the primitive had no verdict-path
# consumer (its only intended caller is the deferred event-relative resolver,
# item 7 Stage 2) and wiring it to a present-day check risked a false-contradict.
# The BEFORE_PRESENT → end_known=False behavior it relied on is still covered by
# the _iso_or_none tests above and the _interval_from_statements tests below.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _interval_from_statements
# ---------------------------------------------------------------------------

class TestIntervalFromStatements:
    def setup_method(self):
        self.w = _make_walker()

    def test_empty_statements_is_none(self):
        assert self.w._interval_from_statements([]) is None

    def test_single_closed_statement(self):
        iv = self.w._interval_from_statements([
            Statement(value="Q1", value_type="entity",
                      qualifiers={"P580": "1933-10-01", "P582": "1955-04-18"})
        ])
        assert iv.start == "1933-10-01"
        assert iv.end == "1955-04-18"
        assert iv.start_known is True
        assert iv.end_known is True

    def test_single_open_end_statement(self):
        iv = self.w._interval_from_statements([
            Statement(value="Q1", value_type="entity",
                      qualifiers={"P580": "1912-01-01"})
        ])
        assert iv.start == "1912-01-01"
        assert iv.start_known is True
        assert iv.end_known is False

    def test_preferred_statement_disambiguates_conflicting_starts(self):
        # Two statements with DIFFERENT starts; one is preferred → use it.
        iv = self.w._interval_from_statements([
            Statement(value="Q1", value_type="entity", rank="preferred",
                      qualifiers={"P580": "1933-10-01", "P582": "1955-04-18"}),
            Statement(value="Q1", value_type="entity", rank="normal",
                      qualifiers={"P580": "1912-01-01"}),
        ])
        assert iv is not None
        assert iv.start == "1933-10-01"

    def test_conflicting_starts_no_preferred_abstains(self):
        # Conflicting starts with no preferred discriminator → abstain (None);
        # we do NOT max/min dates.
        iv = self.w._interval_from_statements([
            Statement(value="Q1", value_type="entity",
                      qualifiers={"P580": "1933-10-01"}),
            Statement(value="Q1", value_type="entity",
                      qualifiers={"P580": "1912-01-01"}),
        ])
        assert iv is None

    def test_open_end_dominates_a_closed_end(self):
        # Among the chosen statements, a genuine open end (ongoing) keeps the
        # interval open even when another records an end.
        iv = self.w._interval_from_statements([
            Statement(value="Q1", value_type="entity",
                      qualifiers={"P580": "1933-10-01", "P582": "1955-04-18"}),
            Statement(value="Q1", value_type="entity",
                      qualifiers={"P580": "1933-10-01"}),  # no P582 → open
        ])
        assert iv is not None
        assert iv.start == "1933-10-01"
        assert iv.end_known is False


# ---------------------------------------------------------------------------
# _tier_u_endpoint
# ---------------------------------------------------------------------------

class TestTierUEndpoint:
    def test_started_row_yields_start_interval(self):
        tu = _StubTierU(rows=[{"object": "2020"}], found=True)
        w = _make_walker(tier_u=tu)
        iv = w._tier_u_endpoint(_claim(predicate="employment_started"), _ctx())
        assert iv is not None
        assert iv.start == "2020"
        assert iv.start_known is True
        assert iv.end_known is False

    def test_ended_row_yields_end_interval(self):
        tu = _StubTierU(rows=[{"object": "2024"}], found=True)
        w = _make_walker(tier_u=tu)
        iv = w._tier_u_endpoint(_claim(predicate="employment_ended"), _ctx())
        assert iv is not None
        assert iv.end == "2024"
        assert iv.end_known is True

    def test_no_tier_u_row_is_none(self):
        w = _make_walker(tier_u=_StubTierU(found=False))
        iv = w._tier_u_endpoint(_claim(predicate="employment_started"), _ctx())
        assert iv is None

    def test_agreeing_rows_yield_the_shared_date(self):
        # Round-1 robustness follow-up (WS6): multiple Tier U rows that AGREE on
        # the endpoint date collapse to that single distinct date — not an
        # arbitrary rows[0] pick, but a deterministic one-distinct-date result.
        tu = _StubTierU(rows=[{"object": "2020"}, {"object": "2020"}], found=True)
        w = _make_walker(tier_u=tu)
        iv = w._tier_u_endpoint(_claim(predicate="employment_started"), _ctx())
        assert iv is not None
        assert iv.start == "2020"
        assert iv.start_known is True

    def test_conflicting_rows_abstain(self):
        # Two Tier U rows DISAGREE on the endpoint date → more than one distinct
        # non-None date → abstain (None), mirroring the KB conflicting-start
        # abstention. We must NOT pick a row by accident (§3.2 soundness).
        tu = _StubTierU(rows=[{"object": "2020"}, {"object": "2021"}], found=True)
        w = _make_walker(tier_u=tu)
        iv = w._tier_u_endpoint(_claim(predicate="employment_started"), _ctx())
        assert iv is None

    def test_conflicting_rows_abstain_on_ended_too(self):
        # The conflicting-date abstention is endpoint-agnostic: an _ended claim
        # with disagreeing Tier U end dates also abstains.
        tu = _StubTierU(rows=[{"object": "2023"}, {"object": "2024"}], found=True)
        w = _make_walker(tier_u=tu)
        iv = w._tier_u_endpoint(_claim(predicate="employment_ended"), _ctx())
        assert iv is None

    def test_all_none_dates_abstain(self):
        # Rows present but every endpoint date is None (empty/sentinel) → zero
        # distinct dates → nothing to ground → abstain (None).
        tu = _StubTierU(rows=[{"object": None}, {"object": ""}], found=True)
        w = _make_walker(tier_u=tu)
        iv = w._tier_u_endpoint(_claim(predicate="employment_started"), _ctx())
        assert iv is None

    def test_one_real_date_among_nones_grounds(self):
        # A single distinct non-None date alongside None rows still grounds —
        # the Nones are discarded, leaving exactly one distinct date.
        tu = _StubTierU(rows=[{"object": None}, {"object": "2020"}], found=True)
        w = _make_walker(tier_u=tu)
        iv = w._tier_u_endpoint(_claim(predicate="employment_started"), _ctx())
        assert iv is not None
        assert iv.start == "2020"
        assert iv.start_known is True

    def test_uniquely_valued_started_endpoint_is_unique(self):
        # PATCH-C r2c-1: the single-distinct-date invariant (len(dates)==1)
        # GUARANTEES the endpoint is uniquely valued, so a Tier-U _started
        # endpoint carries unique=True — enabling it to drive the
        # _verify_interval_endpoint contradiction branch (which gates on
        # interval.unique) symmetrically with the KB grounding path.
        tu = _StubTierU(rows=[{"object": "2020"}], found=True)
        w = _make_walker(tier_u=tu)
        iv = w._tier_u_endpoint(_claim(predicate="employment_started"), _ctx())
        assert iv is not None
        assert iv.unique is True

    def test_uniquely_valued_ended_endpoint_is_unique(self):
        # Symmetric for the _ended endpoint.
        tu = _StubTierU(rows=[{"object": "2024"}], found=True)
        w = _make_walker(tier_u=tu)
        iv = w._tier_u_endpoint(_claim(predicate="employment_ended"), _ctx())
        assert iv is not None
        assert iv.unique is True

    def test_agreeing_rows_collapse_to_unique(self):
        # Multiple rows that AGREE collapse to one distinct date → still a
        # uniquely-valued endpoint (unique=True). Agreement is not a conflict.
        tu = _StubTierU(rows=[{"object": "2020"}, {"object": "2020"}], found=True)
        w = _make_walker(tier_u=tu)
        iv = w._tier_u_endpoint(_claim(predicate="employment_started"), _ctx())
        assert iv is not None
        assert iv.start == "2020"
        assert iv.unique is True

    def test_conflicting_rows_never_set_unique(self):
        # On a CONFLICT (more than one distinct date) the method abstains (None)
        # — so a non-unique interval is never produced and unique is never set
        # on a conflict. This is the soundness guarantee behind the contradiction
        # gate: a conflicting Tier-U endpoint can never license a contradiction.
        tu = _StubTierU(rows=[{"object": "2020"}, {"object": "2021"}], found=True)
        w = _make_walker(tier_u=tu)
        iv = w._tier_u_endpoint(_claim(predicate="employment_started"), _ctx())
        assert iv is None

    def test_unique_endpoint_satisfies_the_contradiction_gate(self):
        # PATCH-C r2c-1, behavioral: the contradiction branch in
        # _verify_interval_endpoint abstains when `not interval.unique`. A
        # uniquely-valued Tier-U endpoint now carries unique=True, so it SATISFIES
        # that gate — feeding such an interval straight into the contradiction
        # branch no longer over-abstains on the uniqueness check. (We assert the
        # gate condition directly: a unique Tier-U interval clears `not unique`.)
        tu = _StubTierU(rows=[{"object": "2020"}], found=True)
        w = _make_walker(tier_u=tu)
        iv = w._tier_u_endpoint(_claim(predicate="employment_started"), _ctx())
        assert iv is not None and iv.unique is True
        # The gate at _verify_interval_endpoint (`if not interval.unique`) would
        # NOT short-circuit this interval — symmetric with the KB grounding path,
        # whose preferred-unique interval is also unique=True.
        assert not (not iv.unique)


# ---------------------------------------------------------------------------
# _verify_interval_endpoint — verified / contradicted / abstain
# ---------------------------------------------------------------------------

# Einstein P108 → IAS (Q11942, preferred, P580=1933, P582=1955). A single
# preferred statement gives an unambiguous interval.
def _ias_statements():
    return [
        Statement(value="Q11942", value_type="entity", rank="preferred",
                  qualifiers={"P580": "1933-10-01", "P582": "1955-04-18"}),
        Statement(value="Q11920", value_type="entity", rank="normal",
                  qualifiers={"P580": "1912-01-01"}),  # open end
    ]


def _interval_walker(single_valued=False, statements=None, resolver=None,
                     kb=None, base_property="P108"):
    statements = _ias_statements() if statements is None else statements
    kb = kb if kb is not None else _StubKB(
        statements_by_key={("Q937", base_property): statements}
    )
    meta = _StubMeta(kb_property=base_property, single_valued=single_valued)
    return _make_walker(
        kb=kb,
        resolver=resolver,
        meta_by_pred={"employment_started": meta, "employment_ended": meta},
    )


class TestVerifyIntervalEndpoint:
    def test_started_matches_kb_p580_is_verified(self):
        w = _interval_walker()
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1933"), _ctx(), _trace()
        )
        assert out is not None
        verdict, grounding = out
        assert verdict == "verified"
        assert grounding["qualifier"] == "P580"
        assert grounding["endpoint_value"] == "1933-10-01"

    def test_ended_matches_kb_p582_is_verified(self):
        w = _interval_walker()
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_ended", object_val="1955"), _ctx(), _trace()
        )
        assert out is not None
        verdict, _ = out
        assert verdict == "verified"

    def test_functional_mismatch_is_contradicted_with_value(self):
        # single_valued=True endpoint, KB P580 is 1933 but the claim says 1999
        # → contradicted, and the KB date is the contradicting value (WS5).
        w = _interval_walker(single_valued=True)
        trace = _trace()
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1999"), _ctx(), trace
        )
        assert out is not None
        verdict, grounding = out
        assert verdict == "contradicted"
        assert grounding["contradicting_value"] == "1933-10-01"
        assert grounding["contradicting_value_type"] == "time"
        # The trace edge carries the WS5 contradicting value for the aggregator.
        edge = trace.edges[-1]
        assert edge.metadata["source"] == "kb_interval"
        assert edge.metadata["contradicting_value"] == "1933-10-01"

    def test_multivalued_mismatch_abstains(self):
        # A non-functional (single_valued=False) endpoint mismatch must NOT
        # contradict — other values may legitimately hold. Abstain (None).
        w = _interval_walker(single_valued=False)
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1999"), _ctx(), _trace()
        )
        assert out is None

    def test_polarity_inversion_on_negated_claim(self):
        # A negated *_started claim whose positive form matches the KB → the
        # negation is contradicted (polarity inverts the positive 'verified').
        w = _interval_walker()
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1933", polarity=0),
            _ctx(), _trace(),
        )
        assert out is not None
        verdict, _ = out
        assert verdict == "contradicted"

    def test_non_endpoint_predicate_is_none(self):
        # A predicate that is neither *_started nor *_ended → None (abstain).
        meta = _StubMeta(kb_property="P108")
        w = _make_walker(meta_by_pred={"employed_by": meta})
        out = w._verify_interval_endpoint(
            _claim(predicate="employed_by", object_val="1933"), _ctx(), _trace()
        )
        assert out is None

    def test_empty_object_is_none(self):
        w = _interval_walker()
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val=""), _ctx(), _trace()
        )
        assert out is None

    def test_open_endpoint_abstains(self):
        # The claim asks about the END (P582) but the only matching statement
        # has an OPEN end (ETH Zurich, no P582). Absence is not evidence →
        # abstain, never contradict.
        stmts = [
            Statement(value="Q11920", value_type="entity",
                      qualifiers={"P580": "1912-01-01"}),  # no P582
        ]
        w = _interval_walker(statements=stmts)
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_ended", object_val="2000"), _ctx(), _trace()
        )
        assert out is None


# ---------------------------------------------------------------------------
# Non-unique-interval contradiction guard (WS6 round-1, forward-defensive) +
# dead object_org-branch-removal regression coverage
# ---------------------------------------------------------------------------

class TestNonUniqueContradictionGuard:
    """Round-1 robustness follow-up (WS6, defense-in-depth): a *_started/_ended
    CONTRADICTED verdict is licensed ONLY when the interval was built from a
    UNIQUELY-identified statement (a single candidate, or a single
    preferred-ranked one). An interval built by COLLAPSING several statements
    that merely AGREE on a start is NOT unique and must never contradict an
    asserted endpoint — otherwise a future single_valued endpoint binding could
    false-contradict against a value that is just one of several the KB holds.

    All 8 endpoint seeds are single_valued=0 today, so this branch is
    unreachable in production; the guard is forward-defensive. We exercise it by
    pinning single_valued=True on a stub meta.
    """

    @staticmethod
    def _agreeing_nonunique_statements():
        # Two NORMAL-rank statements that AGREE on the start (no preferred
        # discriminator) → _interval_from_statements collapses them into an
        # interval with unique=False.
        return [
            Statement(value="Q11942", value_type="entity", rank="normal",
                      qualifiers={"P580": "1933-10-01"}),
            Statement(value="Q11920", value_type="entity", rank="normal",
                      qualifiers={"P580": "1933-10-01"}),
        ]

    def test_collapsed_interval_is_not_unique(self):
        w = _make_walker()
        iv = w._interval_from_statements(self._agreeing_nonunique_statements())
        assert iv is not None
        assert iv.start == "1933-10-01"
        # Collapsed from two agreeing normal statements → NOT unique.
        assert iv.unique is False

    def test_single_statement_interval_is_unique(self):
        w = _make_walker()
        iv = w._interval_from_statements([
            Statement(value="Q11942", value_type="entity", rank="normal",
                      qualifiers={"P580": "1933-10-01"}),
        ])
        assert iv is not None
        assert iv.unique is True

    def test_preferred_unique_interval_is_unique(self):
        # A single preferred-ranked statement among normals is uniquely
        # identified even though more than one statement is present.
        w = _make_walker()
        iv = w._interval_from_statements(_ias_statements())
        assert iv is not None
        assert iv.unique is True

    def test_nonunique_mismatch_does_not_contradict(self):
        # single_valued=True, the collapsed (non-unique) interval start is
        # 1933 but the claim asserts 1999 → the guard forces ABSTAIN (None),
        # NOT a contradiction.
        w = _interval_walker(
            single_valued=True,
            statements=self._agreeing_nonunique_statements(),
        )
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1999"),
            _ctx(), _trace(),
        )
        assert out is None

    def test_unique_mismatch_still_contradicts(self):
        # Control: the SAME single_valued mismatch against a UNIQUE
        # (single-statement) interval DOES contradict — proving the abstention
        # above is the non-unique guard firing, not some unrelated abstention.
        w = _interval_walker(
            single_valued=True,
            statements=[
                Statement(value="Q11942", value_type="entity", rank="normal",
                          qualifiers={"P580": "1933-10-01"}),
            ],
        )
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1999"),
            _ctx(), _trace(),
        )
        assert out is not None
        verdict, grounding = out
        assert verdict == "contradicted"
        assert grounding["contradicting_value"] == "1933-10-01"

    def test_nonunique_match_still_verifies(self):
        # A non-unique interval is only barred from CONTRADICTING — a MATCH is
        # still a verification (the guard sits in the contradiction branch only).
        w = _interval_walker(
            single_valued=True,
            statements=self._agreeing_nonunique_statements(),
        )
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1933"),
            _ctx(), _trace(),
        )
        assert out is not None
        verdict, _ = out
        assert verdict == "verified"


class TestObjectOrgBranchRemoval:
    """Round-1 robustness follow-up: the dead `getattr(claim, 'object_org', None)`
    candidate-narrowing branch in _gather_interval was removed (Claim has no
    `object_org` field, so it was always None / dead). These tests pin that the
    removal did NOT change the verified / abstain behavior on the existing
    fixtures — _gather_interval still builds the interval straight from the
    subject's base statements via _interval_from_statements.
    """

    def test_claim_has_no_object_org_attribute(self):
        # The premise of the removal: Claim never carried `object_org`.
        c = _claim(predicate="employment_started")
        assert getattr(c, "object_org", None) is None

    def test_verified_behavior_unchanged(self):
        # The canonical IAS fixture still verifies a matching P580 endpoint.
        w = _interval_walker()
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1933"),
            _ctx(), _trace(),
        )
        assert out is not None
        verdict, grounding = out
        assert verdict == "verified"
        assert grounding["endpoint_value"] == "1933-10-01"

    def test_abstain_behavior_unchanged(self):
        # A multi-valued (single_valued=False) mismatch still abstains, exactly
        # as before — no object_org narrowing ever altered this path.
        w = _interval_walker(single_valued=False)
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1999"),
            _ctx(), _trace(),
        )
        assert out is None

    def test_gather_interval_builds_from_full_statement_set(self):
        # _gather_interval grounds against the subject's full base-statement set
        # (preferred-or-agreeing), with no object-side narrowing.
        w = _interval_walker()
        iv = w._gather_interval(
            _claim(predicate="employment_started"), "P108", _ctx()
        )
        assert iv is not None
        assert iv.start == "1933-10-01"  # IAS preferred statement


# ---------------------------------------------------------------------------
# FAIL-CLOSED: resolution / KB error / ambiguity → None
# ---------------------------------------------------------------------------

class TestVerifyIntervalEndpointFailClosed:
    def test_resolver_error_returns_none(self):
        w = _interval_walker(resolver=_StubResolver(raise_on_resolve=True))
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1933"), _ctx(), _trace()
        )
        assert out is None

    def test_unresolved_subject_returns_none(self):
        # Subject not in the resolver's table → resolve returns [] → None.
        w = _interval_walker(resolver=_StubResolver(resolutions={}))
        out = w._verify_interval_endpoint(
            _claim(subject="Nobody", predicate="employment_started", object_val="1933"),
            _ctx(), _trace(),
        )
        assert out is None

    def test_kb_lookup_error_returns_none(self):
        w = _interval_walker(kb=_StubKB(raise_on_lookup=True))
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1933"), _ctx(), _trace()
        )
        assert out is None

    def test_ambiguous_multiple_start_returns_none(self):
        # Two normal-rank statements with CONFLICTING starts and no preferred
        # discriminator → _gather_interval abstains → endpoint verdict None.
        stmts = [
            Statement(value="Q11942", value_type="entity",
                      qualifiers={"P580": "1933-10-01"}),
            Statement(value="Q11920", value_type="entity",
                      qualifiers={"P580": "1912-01-01"}),
        ]
        w = _interval_walker(statements=stmts)
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1933"), _ctx(), _trace()
        )
        assert out is None

    def test_no_kb_property_returns_none(self):
        # The predicate metadata carries no base kb_property → abstain.
        meta = _StubMeta(kb_property=None)
        w = _make_walker(meta_by_pred={"employment_started": meta})
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1933"), _ctx(), _trace()
        )
        assert out is None

    def test_consult_failure_returns_none(self):
        # predicate_translation.consult raising (unknown predicate) → abstain.
        w = _make_walker(meta_by_pred={})
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1933"), _ctx(), _trace()
        )
        assert out is None


# ---------------------------------------------------------------------------
# _gather_interval — KB + Tier U merge, fail-closed
# ---------------------------------------------------------------------------

class TestGatherInterval:
    def test_kb_interval_gathered(self):
        w = _interval_walker()
        iv = w._gather_interval(
            _claim(predicate="employment_started"), "P108", _ctx()
        )
        assert iv is not None
        # IAS preferred statement → 1933 start, 1955 end.
        assert iv.start == "1933-10-01"

    def test_tier_u_fills_open_kb_end(self):
        # KB statement is open-ended; a Tier U *_ended fact fills the end.
        stmts = [Statement(value="Q11942", value_type="entity",
                           qualifiers={"P580": "1933-10-01"})]  # open end
        kb = _StubKB(statements_by_key={("Q937", "P108"): stmts})
        tu = _StubTierU(rows=[{"object": "1950"}], found=True)
        w = _make_walker(
            kb=kb, tier_u=tu,
            meta_by_pred={"employment_ended": _StubMeta("P108")},
        )
        iv = w._gather_interval(
            _claim(predicate="employment_ended"), "P108", _ctx()
        )
        assert iv is not None
        assert iv.start == "1933-10-01"
        # The Tier U endpoint filled the otherwise-open end.
        assert iv.end == "1950"
        assert iv.end_known is True

    def test_kb_error_returns_none(self):
        w = _interval_walker(kb=_StubKB(raise_on_lookup=True))
        iv = w._gather_interval(
            _claim(predicate="employment_started"), "P108", _ctx()
        )
        assert iv is None

    def test_no_kb_no_tier_u_returns_none(self):
        # No statements and no Tier U endpoint → None (nothing to ground).
        kb = _StubKB(statements_by_key={})
        w = _make_walker(kb=kb, tier_u=_StubTierU(found=False),
                         meta_by_pred={"employment_started": _StubMeta("P108")})
        iv = w._gather_interval(
            _claim(predicate="employment_started"), "P108", _ctx()
        )
        assert iv is None


# ---------------------------------------------------------------------------
# v0.16.1 WS5c — interval qualifier keys sourced from the KB adapter
# ---------------------------------------------------------------------------

class TestWS5cIntervalQualifierKeys:
    """CORE reads the (start, end) interval-qualifier keys from the KB adapter's
    `interval_qualifier_keys` accessor, not a hardcoded walker P-id. With a stub
    returning ALTERNATE keys, the endpoint grounds against statements carrying
    those keys and the trace records them — proving the relocation is wired
    (and behavior-neutral when the adapter returns the canonical P580/P582)."""

    def test_accessor_keys_drive_interval_gathering(self):
        # Stub returns alternate qualifier keys; statements carry them. The
        # endpoint must ground against the start key the accessor reported.
        alt_statements = [
            Statement(value="Q11942", value_type="entity", rank="preferred",
                      qualifiers={"START": "1933-10-01", "END": "1955-04-18"}),
        ]
        kb = _StubKB(
            statements_by_key={("Q937", "P108"): alt_statements},
            qualifier_keys=("START", "END"),
        )
        w = _make_walker(
            kb=kb,
            meta_by_pred={"employment_started": _StubMeta("P108"),
                          "employment_ended": _StubMeta("P108")},
        )
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_started", object_val="1933"),
            _ctx(), _trace(),
        )
        assert out is not None
        verdict, grounding = out
        assert verdict == "verified"
        assert grounding["qualifier"] == "START"
        assert grounding["endpoint_value"] == "1933-10-01"

    def test_canonical_p580_p582_when_accessor_returns_them(self):
        # An adapter returning the canonical keys is byte-identical to the
        # historical hardcoded path.
        kb = _StubKB(
            statements_by_key={("Q937", "P108"): _ias_statements()},
            qualifier_keys=("P580", "P582"),
        )
        w = _make_walker(
            kb=kb,
            meta_by_pred={"employment_started": _StubMeta("P108"),
                          "employment_ended": _StubMeta("P108")},
        )
        out = w._verify_interval_endpoint(
            _claim(predicate="employment_ended", object_val="1955"),
            _ctx(), _trace(),
        )
        assert out is not None
        verdict, grounding = out
        assert verdict == "verified"
        assert grounding["qualifier"] == "P582"

    def test_adapter_exposes_interval_qualifier_keys(self):
        # The live WikidataAdapter returns the canonical (P580, P582).
        from aedos.layer4_sources.kb_wikidata import WikidataAdapter
        assert WikidataAdapter().interval_qualifier_keys() == ("P580", "P582")
