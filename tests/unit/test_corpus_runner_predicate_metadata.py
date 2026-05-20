"""Phase E follow-up — `_run_predicate_metadata` handles the two corpus shapes
that don't map 1:1 to `PredicateMetadata` attributes.

The Qwen × predicate_metadata run surfaced 5 `AttributeError: 'PredicateMetadata'
object has no attribute 'routing_hint_options'` on the `pred_ambig_*` cases —
the corpus uses `routing_hint_options: [list]` to express "this ambiguous
predicate has multiple acceptable routings", not as a missing attribute.
`pred_kb_008` carries the same shape under a different key
(`distinct_slots_required: bool`) — latent because it requires a candidate
whose earlier-field matches force the iteration to reach it.

Pre-fix the runner did `getattr(meta, field)` unconditionally; the fix
dispatches on the two known special keys.
"""

from __future__ import annotations

from tests.calibration.test_corpus_runner import _run_predicate_metadata


class _Meta:
    """Minimal stand-in for `PredicateMetadata` — carries only the attributes
    the test needs. The dataclass has no `routing_hint_options` /
    `distinct_slots_required`; that's the whole point."""

    def __init__(self, routing_hint=None, distinct_slots=None,
                 object_type=None, kb_property=None, user_subject_required=False):
        self.routing_hint = routing_hint
        self.distinct_slots = distinct_slots
        self.object_type = object_type
        self.kb_property = kb_property
        self.user_subject_required = user_subject_required


class _Harness:
    """`h.predicate_translation.consult(predicate)` → a fixed `_Meta`."""

    def __init__(self, meta):
        outer = self
        outer._meta = meta

        class _PT:
            def consult(self, predicate, kb_namespace=None):
                return outer._meta
        self.predicate_translation = _PT()


class TestRoutingHintOptions:
    """`pred_ambig_*` shape: `routing_hint_options: [list]` of acceptable hints."""

    def test_produced_hint_in_options_passes(self):
        h = _Harness(_Meta(routing_hint="user_authoritative"))
        case = {"aedos_predicate": "x",
                "expected_metadata": {
                    "routing_hint_options": ["user_authoritative", "kb_resolvable"]}}
        assert _run_predicate_metadata(h, case) is True

    def test_produced_hint_not_in_options_fails(self):
        h = _Harness(_Meta(routing_hint="python"))
        case = {"aedos_predicate": "x",
                "expected_metadata": {
                    "routing_hint_options": ["user_authoritative", "kb_resolvable"]}}
        assert _run_predicate_metadata(h, case) is False


class TestDistinctSlotsRequired:
    """`pred_kb_008` shape: `distinct_slots_required: bool` — produced
    `distinct_slots` should be populated (truthy) or not."""

    def test_required_and_populated_passes(self):
        h = _Harness(_Meta(distinct_slots=["subject", "object"]))
        case = {"aedos_predicate": "x",
                "expected_metadata": {"distinct_slots_required": True}}
        assert _run_predicate_metadata(h, case) is True

    def test_required_but_missing_fails(self):
        h = _Harness(_Meta(distinct_slots=None))
        case = {"aedos_predicate": "x",
                "expected_metadata": {"distinct_slots_required": True}}
        assert _run_predicate_metadata(h, case) is False

    def test_not_required_and_missing_passes(self):
        h = _Harness(_Meta(distinct_slots=None))
        case = {"aedos_predicate": "x",
                "expected_metadata": {"distinct_slots_required": False}}
        assert _run_predicate_metadata(h, case) is True


class TestDirectFieldComparisonStillWorks:
    """The fix must not regress the 75/80 cases that use direct-attribute keys."""

    def test_routing_hint_object_type_match(self):
        h = _Harness(routing_hint_or_meta := _Meta(
            routing_hint="kb_resolvable", object_type="entity",
            kb_property="P361"))
        case = {"aedos_predicate": "x",
                "expected_metadata": {
                    "routing_hint": "kb_resolvable", "object_type": "entity",
                    "kb_property": "P361"}}
        assert _run_predicate_metadata(h, case) is True

    def test_user_subject_required_bool_coercion(self):
        # The pre-existing `bool(produced) == bool(value)` shim survives the fix.
        h = _Harness(_Meta(user_subject_required=1))
        case = {"aedos_predicate": "x",
                "expected_metadata": {"user_subject_required": True}}
        assert _run_predicate_metadata(h, case) is True

    def test_mismatch_returns_false_early(self):
        h = _Harness(_Meta(routing_hint="python", object_type="quantity"))
        case = {"aedos_predicate": "x",
                "expected_metadata": {
                    "routing_hint": "kb_resolvable", "object_type": "entity"}}
        assert _run_predicate_metadata(h, case) is False
