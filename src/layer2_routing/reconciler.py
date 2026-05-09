"""Layer 2.5 — routing reconciler (v0.14.3).

After the LLM router picks a verifier method, the reconciler checks
that the picked method is COMPATIBLE with the claim's pattern shape.
The Cairo timezone case motivated this:

  * Extractor produced ``spatial_temporal.located_in`` and self-attested
    ``expected_verifier="python"``.
  * Router followed the extractor's hint and picked ``python``.
  * Python verifier ran cleanly and computed the right answer, but the
    comparison step needed a ``value`` slot to compare against — and
    ``spatial_temporal`` doesn't have one.
  * Result: ``unverifiable_pending_implementation`` → hedge.

The architectural fix: each verifier method has a known input shape
(which slots it expects to find on the claim). When the router picks
a method incompatible with the claim's pattern shape, the reconciler
overrides the pick using the pattern's ``default_routing_method`` from
the schema. The reconciler is rule-based, deterministic, and emits a
``routing_reconciled`` event so the trace UI can surface the override.

Architectural fit
=================

  * Principle 1 (verification upstream of memoization): unchanged.
    The reconciler doesn't touch the store; it only adjusts the
    routing decision before the walker dispatches.
  * Principle 7 (validate before classifying, route before reasoning):
    extended. The "route" step now has a schema-driven post-check.
  * The Cairo case becomes a structural fix, not a per-claim patch.
    Any future "static-relation pattern routed to python" claim gets
    the same override.

Verifier slot requirements
==========================

Declared as a constant here rather than per-pattern in the schema —
the requirement is a property of the VERIFIER (its comparison logic
needs a value to compare against), not of the pattern. The schema
declares the pattern's default routing method; the reconciler checks
that the picked method's shape requirements are met by the pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.layer1_extraction.pattern_registry import PatternRegistry
from src.layer2_routing.types import Decision, RoutingOutcome


# Verifier methods that require a comparable ``value`` slot on the
# claim — they compute an actual_value and need an expected_value to
# compare against. Pattern shapes without a value slot
# (spatial_temporal, mereological, categorical, role_assignment,
# preference, propositional_attitude, event) are incompatible.
#
# Patterns WITH a value slot (quantitative) are compatible.
_VERIFIERS_REQUIRING_VALUE_SLOT: frozenset[str] = frozenset({
    "python",
    "python_with_canonical_constants",
})


# v0.14.5 — verifier method families. The multi-signal reconciler
# operates at FAMILY granularity (python and python_with_canonical_
# constants are functionally interchangeable for routing-correction
# purposes; either one suffices to verify a counting / clock claim).
# Methods outside the named families (user_authoritative,
# unverifiable) are never auto-overridden by the multi-signal check —
# those routings carry per-claim semantic judgment ("the user is
# authoritative on this kind of claim"; "no method applies") that
# the schema can't capture in advance, and the LLM router owns
# those decisions.
_PYTHON_FAMILY: frozenset[str] = frozenset({
    "python",
    "python_with_canonical_constants",
})
_RETRIEVAL_FAMILY: frozenset[str] = frozenset({
    "retrieval",
})
_NEVER_OVERRIDE_METHODS: frozenset[str] = frozenset({
    "user_authoritative",
    "unverifiable",
})


def _method_family(method: Optional[str]) -> Optional[str]:
    """Map a routing method to its family. Returns ``'python'`` /
    ``'retrieval'`` for the two reconcilable families, ``None`` for
    anything else (the never-override methods + unknown values)."""
    if method in _PYTHON_FAMILY:
        return "python"
    if method in _RETRIEVAL_FAMILY:
        return "retrieval"
    return None


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of reconciliation. ``reconciled`` is True iff the
    routing decision was changed. ``override_method`` is the new
    method (None when no override). ``reason`` explains why."""

    reconciled: bool
    override_method: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciled": self.reconciled,
            "override_method": self.override_method,
            "reason": self.reason,
        }


def reconcile_routing(
    claim: dict, layer2: Decision, registry: PatternRegistry,
) -> tuple[Decision, ReconcileResult]:
    """Check the routing decision against the claim's pattern shape.
    Returns (possibly_overridden_decision, reconcile_result).

    Pure function. No store access, no LLM call. The decision is
    deterministic from the claim + decision + schema.

    Override conditions (first match wins):

      1. **Verifier shape mismatch.** Router picked ``python`` /
         ``python_with_canonical_constants`` but the pattern has no
         ``value`` slot. Override to the pattern's
         ``default_routing_method`` (or ``retrieval`` as ultimate
         fallback).

    Anomaly + memo-hit decisions are passed through untouched —
    anomalies have no method to reconcile, and memo hits are by
    construction already-reconciled (the memo only stores reconciled
    methods).
    """
    if layer2.outcome is RoutingOutcome.ROUTING_ANOMALY:
        return layer2, ReconcileResult(reconciled=False)

    method = layer2.method
    if method is None:
        return layer2, ReconcileResult(reconciled=False)

    pattern_name = claim.get("pattern", "")
    if not pattern_name or not registry.has(pattern_name):
        return layer2, ReconcileResult(reconciled=False)
    pattern = registry.get(pattern_name)

    # Check 1: verifier needs a value slot but pattern doesn't have one.
    if method in _VERIFIERS_REQUIRING_VALUE_SLOT:
        slot_names = {s.name for s in pattern.slots}
        if "value" not in slot_names:
            override = pattern.default_routing_method or "retrieval"
            if override == method:
                # Defensive: schema disagrees with itself (default_routing
                # _method is python on a pattern with no value slot).
                # Fall back to retrieval as the only general-purpose
                # verifier that works on any pattern shape.
                override = "retrieval"
            return _apply_override(claim, layer2, override, reason=(
                f"verifier {method!r} requires a `value` slot but pattern "
                f"{pattern_name!r} has none (slots: "
                f"{sorted(slot_names)}); overriding to schema default "
                f"{override!r}"
            ))

    # Check 2 (v0.14.5): multi-signal agreement override. When at
    # least 2 of 3 cheaply-available signals agree on a verifier
    # FAMILY (python vs retrieval) different from the LLM router's
    # pick, override to the schema's default for that family. The
    # vowel-count case motivated this: schema says python, predicate
    # is in the verify-allow-list, extractor self-attested python —
    # but the LLM router picked retrieval on the surface form of the
    # claim ("how many vowels in <long sentence>" looks Wikipedia-
    # shaped). 3-of-3 schema/predicate/extractor agreement → override.
    #
    # Never overrides into or out of user_authoritative / unverifiable;
    # those routings carry per-claim semantic judgment the schema
    # can't capture, and the LLM router owns those decisions.
    if method in _NEVER_OVERRIDE_METHODS:
        return layer2, ReconcileResult(reconciled=False)

    router_family = _method_family(method)
    if router_family is None:
        return layer2, ReconcileResult(reconciled=False)

    signals = _collect_routing_signals(claim, pattern)
    consensus = _signal_consensus(signals)
    if consensus is None:
        return layer2, ReconcileResult(reconciled=False)

    consensus_family, consensus_method = consensus
    if consensus_family == router_family:
        # Already in agreement at the family level — no override needed
        # even if the specific method differs (python vs
        # python_with_canonical_constants are functionally similar).
        return layer2, ReconcileResult(reconciled=False)

    signal_summary = ", ".join(
        f"{src}={mthd}" for src, mthd in signals
    )
    return _apply_override(claim, layer2, consensus_method, reason=(
        f"multi-signal consensus ({consensus_family!r} family) "
        f"disagrees with router pick {method!r}; signals: "
        f"[{signal_summary}]; overriding to {consensus_method!r}"
    ))


def _collect_routing_signals(
    claim: dict, pattern,
) -> list[tuple[str, str]]:
    """Gather the three routing signals available pre-execution.

    Returns a list of ``(source_name, method)`` tuples. Sources:

      * ``schema_default`` — pattern's ``default_routing_method``
        (always contributes when set; the architectural default for
        the pattern).
      * ``predicate_allow_list`` — fires when the claim's predicate
        appears in the pattern's ``triage_verify_predicates``. This
        is a STRONGER signal than schema default alone — the schema
        explicitly named this predicate as having a known verifier
        path. Its value is the pattern's default_routing_method
        (the schema's inferred verifier for any predicate in its
        allow-list).
      * ``extractor`` — the ``expected_verifier`` field the extractor
        self-attested on the claim (v0.14.3). Skipped when absent.

    These are deliberately overlapping (signal 2 is "predicate-
    specific confirmation" of signal 1). The 2-of-3 consensus rule
    means either (schema + extractor) OR (predicate-allow-list +
    extractor) suffices — both shapes encode "the schema knows the
    answer here AND the extractor agrees."
    """
    signals: list[tuple[str, str]] = []
    default_method = pattern.default_routing_method
    if default_method:
        signals.append(("schema_default", default_method))
    predicate = (claim.get("predicate") or "").strip().lower()
    if predicate and predicate in pattern.triage_verify_predicates:
        if default_method:
            signals.append(("predicate_allow_list", default_method))
    expected = claim.get("expected_verifier")
    if isinstance(expected, str) and expected.strip():
        signals.append(("extractor", expected.strip()))
    return signals


def _signal_consensus(
    signals: list[tuple[str, str]],
) -> Optional[tuple[str, str]]:
    """Return ``(family, method)`` when 2+ signals agree on a
    reconcilable family (python or retrieval). Returns ``None`` when
    no family reaches 2 votes.

    ``method`` is the first method seen for the consensus family —
    used as the override target. When the schema_default (the most
    architecturally specific signal) agrees with the consensus, its
    method is preferred; the dict insertion order in
    ``_collect_routing_signals`` guarantees schema_default appears
    first.
    """
    family_counts: dict[str, int] = {}
    family_first_method: dict[str, str] = {}
    for _source, method in signals:
        family = _method_family(method)
        if family is None:
            continue  # user_authoritative / unverifiable / unknown
        family_counts[family] = family_counts.get(family, 0) + 1
        if family not in family_first_method:
            family_first_method[family] = method
    for family, count in family_counts.items():
        if count >= 2:
            return family, family_first_method[family]
    return None


def _apply_override(
    claim: dict, layer2: Decision, new_method: str, *, reason: str,
) -> tuple[Decision, ReconcileResult]:
    """Build a new Decision with the overridden method. Preserves
    the validation + memo_hit + claim fields; replaces method, reason
    (the routing reason), routing_decision payload, and prepends a
    note about the override."""
    new_routing_payload = dict(layer2.routing_decision or {})
    new_routing_payload["method"] = new_method
    new_routing_payload["reason"] = reason
    new_routing_payload["original_method"] = layer2.method
    new_routing_payload["reconciled_by"] = "routing_reconciler"

    new_notes = [
        f"routing reconciled: original method {layer2.method!r} → "
        f"{new_method!r}; reason: {reason}"
    ] + list(layer2.notes or [])

    new_decision = Decision(
        claim=layer2.claim,
        outcome=layer2.outcome,
        method=new_method,
        reason=reason,
        memo_hit=layer2.memo_hit,
        validation=layer2.validation,
        routing_decision=new_routing_payload,
        notes=new_notes,
    )
    return new_decision, ReconcileResult(
        reconciled=True,
        override_method=new_method,
        reason=reason,
    )
