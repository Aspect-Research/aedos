"""v2 stack runner for the Phase 9 parity audit.

Direct-Python access to the v2 layers — no HTTP. The audit constructs
a fresh tmp_path-backed FactStore + the four oracles + a routing memo,
then walks corpus entries in file order. Each entry's runner is
shape-specific:

  * SUBSTRATE_DIRECT — pre-populate the named oracle row with
    ``expected_label``; assert the row is now retrievable. Verifies the
    oracle's record + lookup contracts.
  * TWO_TEXT_ORACLE — pre-populate predicate_equivalence with the
    ``expected_oracle_classification``; assert retrievable.
  * ROUTING_MEMO — drive Layer 2 ``Router.classify`` with a stub
    routing_fn that returns ``expected_routing``; inspect the memo
    row after to confirm n/a / write / hit.
  * ASSISTANT_LOOKUP — apply substrate fixture from corpus.py, then
    walk the claim and compare WalkerDecision.outcome /
    served_from_tier to ``expected_walker_outcome`` (or the legacy
    ``expected_tier_u_outcome``).
  * USER_STORAGE — call ``tier_u.store_user_fact`` per expected
    fact; compare the StoreUserFactResult to any expected
    ``is_session_local`` / ``session_ids_after`` /
    ``affirmed_count_after`` annotations.

Stack state is shared across the whole corpus run: one FactStore,
one set of oracles, one routing memo. Same as a real session would
look at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.fact_store import FactStore
from src.layer1_extraction.pattern_registry import (
    PatternRegistry,
    load_default_registry,
)
from src.layer2_routing.constants import KEY_SLOTS_BY_PATTERN
from src.layer2_routing.llm_router import RoutingDecision
from src.layer2_routing.router import Router as Layer2Router
from src.layer2_routing.routing_memo import RoutingMemo
from src.layer2_routing.types import RoutingOutcome
from src.layer3_substrate.entity_equivalence import EntityEquivalence
from src.layer3_substrate.entity_taxonomy import EntityTaxonomy
from src.layer3_substrate.predicate_distribution import (
    PredicateDistribution,
)
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
)
from src.layer4_lookup import tier_u as _tier_u
from src.layer4_lookup.types import LookupOutcome
from src.layer4_lookup.walker import walk_claim
from tests.parity_runners.corpus import (
    fixture_for,
    reconstruct_claim_slots,
    shape_of,
)
from tests.parity_runners.types import StackResult, StackVerdict
from tests.smoke_dispatcher import SmokeEntryShape


# ============================================================================
# Stack scaffold
# ============================================================================


@dataclass
class V2Stack:
    """All the v2 singletons the audit needs in one place."""

    store: FactStore
    registry: PatternRegistry
    memo: RoutingMemo
    predicate_oracle: PredicateEquivalence
    entity_oracle: EntityEquivalence
    taxonomy_oracle: EntityTaxonomy
    distribution_oracle: PredicateDistribution
    next_turn_id: int = 1

    @property
    def synthetic_turn_id(self) -> int:
        # Every entry needs a turn_id for pipeline_events. Synthetic
        # ids keep events scoped per entry.
        tid = self.next_turn_id
        self.next_turn_id += 1
        return tid


def build_v2_stack(db_path: Path) -> V2Stack:
    """Construct a V2Stack rooted at the given on-disk path. The
    caller manages the lifetime; tests typically pass a tmp_path."""
    store = FactStore(str(db_path))
    return V2Stack(
        store=store,
        registry=load_default_registry(),
        memo=RoutingMemo(store),
        predicate_oracle=PredicateEquivalence(store),
        entity_oracle=EntityEquivalence(store),
        taxonomy_oracle=EntityTaxonomy(store),
        distribution_oracle=PredicateDistribution(store),
    )


def populate_substrate(stack: V2Stack, entry_id: str) -> None:
    """Apply the substrate fixture for an entry, if any."""
    for row in fixture_for(entry_id):
        oracle = row["oracle"]
        if oracle == "predicate_equivalence":
            stack.predicate_oracle.record(
                pattern=row["pattern"],
                predicate_a=row["predicate_a"],
                predicate_b=row["predicate_b"],
                label=row["label"],
                slot_reversal=row.get("slot_reversal", "none"),
                reason=row.get("reason"),
            )
        elif oracle == "entity_equivalence":
            stack.entity_oracle.record(
                entity_a=row["entity_a"],
                entity_b=row["entity_b"],
                label=row["label"],
                reason=row.get("reason"),
            )
        elif oracle == "entity_taxonomy":
            stack.taxonomy_oracle.record(
                child=row["child"],
                parent=row["parent"],
                relation_type=row["relation_type"],
                label=row["label"],
                reason=row.get("reason"),
            )
        elif oracle == "predicate_distribution":
            stack.distribution_oracle.record(
                pattern=row["pattern"],
                predicate=row["predicate"],
                polarity=row["polarity"],
                taxonomy_relation_type=row["taxonomy_relation_type"],
                label=row["label"],
                reason=row.get("reason"),
            )
        else:
            raise ValueError(f"unknown oracle in fixture: {oracle!r}")


# ============================================================================
# Helpers
# ============================================================================


def _claim_from_expected(
    fact: dict, source_text: str, registry: PatternRegistry,
) -> dict:
    """Reconstruct the structured claim shape the v2 stack expects from
    a corpus ``expected_facts[i]`` entry. ``predicate_in`` is a list
    of acceptable predicates; we use the first as the canonical.

    Missing required slots get filled with a placeholder so v2's
    Layer 2 validator (invariant 1) accepts the claim. See
    ``corpus.reconstruct_claim_slots``.
    """
    predicates = fact.get("predicate_in") or []
    predicate = predicates[0] if predicates else ""
    pattern_name = fact["pattern"]
    required: list[str] = []
    if registry.has(pattern_name):
        required = list(registry.get(pattern_name).required_slot_names())
    return {
        "pattern": pattern_name,
        "predicate": predicate,
        "polarity": int(fact["polarity"]),
        "slots": reconstruct_claim_slots(
            pattern_name, dict(fact.get("slots_subset", {})),
            required_slots=required,
        ),
        "source_text": source_text,
    }


def _stub_routing_fn(method: str, reason: str) -> Any:
    """Return a routing_fn that always returns the given method.
    Used for routing_memo entries to bypass the LLM."""
    def _fn(_claim: dict) -> RoutingDecision:
        return RoutingDecision(
            method=method,
            reason=reason,
            python_inputs_self_contained=None,
            retrieval_query_hint=None,
            canonical_constants_needed=None,
        )
    return _fn


# ============================================================================
# Per-shape runners
# ============================================================================


def _run_substrate_direct(entry: dict, stack: V2Stack) -> StackVerdict:
    """Pre-populate the oracle row, then assert lookup returns it."""
    call = entry["oracle_call"]
    expected = entry["expected_label"]
    oracle = call["oracle"]
    try:
        if oracle == "predicate_equivalence":
            stack.predicate_oracle.record(
                pattern=call["pattern"],
                predicate_a=call["predicate_a"],
                predicate_b=call["predicate_b"],
                label=expected,
                slot_reversal="none",
                reason="audit fixture",
            )
            row = stack.predicate_oracle.lookup(
                call["pattern"], call["predicate_a"], call["predicate_b"],
            )
        elif oracle == "entity_equivalence":
            stack.entity_oracle.record(
                entity_a=call["entity_a"],
                entity_b=call["entity_b"],
                label=expected,
                reason="audit fixture",
            )
            row = stack.entity_oracle.lookup(call["entity_a"], call["entity_b"])
        elif oracle == "entity_taxonomy":
            stack.taxonomy_oracle.record(
                child=call["child"],
                parent=call["parent"],
                relation_type=call["relation_type"],
                label=expected,
                reason="audit fixture",
            )
            row = stack.taxonomy_oracle.lookup(
                call["child"], call["parent"], call["relation_type"],
            )
        elif oracle == "predicate_distribution":
            stack.distribution_oracle.record(
                pattern=call["pattern"],
                predicate=call["predicate"],
                polarity=call["polarity"],
                taxonomy_relation_type=call["taxonomy_relation_type"],
                label=expected,
                reason="audit fixture",
            )
            row = stack.distribution_oracle.lookup(
                call["pattern"], call["predicate"], call["polarity"],
                call["taxonomy_relation_type"],
            )
        else:
            return StackVerdict(
                StackResult.FAIL,
                detail=f"unknown oracle {oracle!r} in oracle_call",
            )
    except Exception as exc:
        return StackVerdict(
            StackResult.ERROR, detail=f"record/lookup raised",
            error_type=type(exc).__name__, error_message=str(exc),
        )
    if row is None:
        return StackVerdict(
            StackResult.FAIL,
            detail="record succeeded but lookup returned None",
        )
    if row.label != expected:
        return StackVerdict(
            StackResult.FAIL,
            detail=f"row.label={row.label!r}, expected {expected!r}",
        )
    return StackVerdict(
        StackResult.PASS,
        detail=f"oracle {oracle!r} row stored and retrieved with label={expected!r}",
    )


def _run_two_text_oracle(entry: dict, stack: V2Stack) -> StackVerdict:
    """Pre-populate predicate_equivalence; assert retrievable."""
    cls = entry["expected_oracle_classification"]
    try:
        stack.predicate_oracle.record(
            pattern=cls["pattern"],
            predicate_a=cls["predicate_a"],
            predicate_b=cls["predicate_b"],
            label=cls["label"],
            slot_reversal=cls["slot_reversal"],
            reason="audit fixture",
        )
        row = stack.predicate_oracle.lookup(
            cls["pattern"], cls["predicate_a"], cls["predicate_b"],
        )
    except Exception as exc:
        return StackVerdict(
            StackResult.ERROR, detail="predicate_equivalence raised",
            error_type=type(exc).__name__, error_message=str(exc),
        )
    if row is None or row.label != cls["label"]:
        return StackVerdict(
            StackResult.FAIL,
            detail=f"row missing or mismatched label "
                   f"(got {row.label if row else None!r})",
        )
    return StackVerdict(
        StackResult.PASS,
        detail=f"two-text classification stored: {cls['label']!r}, "
               f"slot_reversal={cls['slot_reversal']!r}",
    )


def _run_routing_memo(entry: dict, stack: V2Stack) -> StackVerdict:
    """Drive Layer 2 with a stub routing_fn matching expected_routing.

    For ``expected_memo_state == "n/a"``, the validator should reject
    the claim — the LLM router never runs and no memo row is written.

    For ``"write"``, the first invocation on this (pattern, predicate)
    triggers the LLM router (here stubbed to expected_routing) and
    writes the memo row.

    For ``"hit"``, a prior entry in the corpus should have written the
    memo row; this invocation should short-circuit on the memo lookup
    and the stub routing_fn should NOT fire.
    """
    expected_state = entry["expected_memo_state"]
    facts = entry["expected_facts"]
    fact = facts[0]
    expected_method = fact["expected_routing"]

    claim = _claim_from_expected(fact, source_text=entry["text"], registry=stack.registry)
    if claim["pattern"] in (None, ""):
        return StackVerdict(
            StackResult.ERROR,
            detail="expected_facts[0].pattern empty",
        )

    # Counter on a closure to detect whether the routing_fn fired.
    fn_call_count = {"n": 0}
    def _stub(_claim: dict) -> RoutingDecision:
        fn_call_count["n"] += 1
        return RoutingDecision(
            method=expected_method
            if expected_method != "routing_anomaly" else "unverifiable",
            reason="audit stub",
            python_inputs_self_contained=None,
            retrieval_query_hint=None,
            canonical_constants_needed=None,
        )

    router = Layer2Router(
        stack.store, stack.registry,
        memo=stack.memo, routing_fn=_stub,
    )
    try:
        decision = router.classify(claim, source_turn_id=stack.synthetic_turn_id)
    except Exception as exc:
        return StackVerdict(
            StackResult.ERROR, detail="Layer 2 classify raised",
            error_type=type(exc).__name__, error_message=str(exc),
        )

    if expected_state == "n/a":
        # Anomaly: validator caught it, router didn't run.
        if decision.outcome is not RoutingOutcome.ROUTING_ANOMALY:
            return StackVerdict(
                StackResult.FAIL,
                detail=f"expected ROUTING_ANOMALY, got {decision.outcome.value!r}",
            )
        if fn_call_count["n"] != 0:
            return StackVerdict(
                StackResult.FAIL,
                detail="routing_fn fired on a routing_anomaly claim",
            )
        return StackVerdict(
            StackResult.PASS,
            detail=f"validator anomaly: {decision.notes[0] if decision.notes else ''}",
        )

    if expected_state == "write":
        if fn_call_count["n"] != 1:
            return StackVerdict(
                StackResult.FAIL,
                detail=f"expected memo write to fire routing_fn once, "
                       f"got {fn_call_count['n']}",
            )
        if decision.method != expected_method:
            return StackVerdict(
                StackResult.FAIL,
                detail=f"expected method={expected_method!r}, got {decision.method!r}",
            )
        if decision.memo_hit:
            return StackVerdict(
                StackResult.FAIL,
                detail="memo_hit=True on a memo-write claim",
            )
        return StackVerdict(
            StackResult.PASS,
            detail=f"memo row written for ({claim['pattern']!r}, "
                   f"{claim['predicate']!r}) → {expected_method!r}",
        )

    if expected_state == "hit":
        if fn_call_count["n"] != 0:
            return StackVerdict(
                StackResult.FAIL,
                detail=f"expected memo hit, but routing_fn fired "
                       f"{fn_call_count['n']} time(s)",
            )
        if not decision.memo_hit:
            return StackVerdict(
                StackResult.FAIL,
                detail="memo_hit=False on a memo-hit claim",
            )
        if decision.method != expected_method:
            return StackVerdict(
                StackResult.FAIL,
                detail=f"memo hit returned method={decision.method!r}, "
                       f"expected {expected_method!r}",
            )
        return StackVerdict(
            StackResult.PASS,
            detail=f"memo hit for ({claim['pattern']!r}, "
                   f"{claim['predicate']!r}) → {expected_method!r}",
        )

    return StackVerdict(
        StackResult.ERROR,
        detail=f"unknown expected_memo_state={expected_state!r}",
    )


def _run_user_storage(entry: dict, stack: V2Stack) -> StackVerdict:
    """Store each expected fact via tier_u.store_user_fact, compare
    to per-fact session-aware expectations when present."""
    raw_text = entry["text"]
    current_session = entry.get("session")  # None when not session-aware
    stored: list[str] = []
    for fact in entry["expected_facts"]:
        claim = _claim_from_expected(fact, source_text=raw_text, registry=stack.registry)
        if not claim["predicate"]:
            continue
        key_slots = KEY_SLOTS_BY_PATTERN.get(claim["pattern"], [])
        try:
            result = _tier_u.store_user_fact(
                claim, stack.store,
                current_session=current_session,
                key_slot_names=key_slots,
                source_turn_id=stack.synthetic_turn_id,
                raw_text=raw_text,
            )
        except Exception as exc:
            return StackVerdict(
                StackResult.ERROR, detail="store_user_fact raised",
                error_type=type(exc).__name__, error_message=str(exc),
            )
        # Compare per-fact session-aware expectations when present.
        if "expected_is_session_local" in fact:
            if result.is_session_local != fact["expected_is_session_local"]:
                return StackVerdict(
                    StackResult.FAIL,
                    detail=f"is_session_local={result.is_session_local}, "
                           f"expected {fact['expected_is_session_local']}",
                )
        if "expected_session_ids_after" in fact:
            if list(result.session_ids_after) != list(fact["expected_session_ids_after"]):
                return StackVerdict(
                    StackResult.FAIL,
                    detail=f"session_ids_after={result.session_ids_after}, "
                           f"expected {fact['expected_session_ids_after']}",
                )
        if "expected_affirmed_count_after" in fact:
            if result.affirmed_count_after != fact["expected_affirmed_count_after"]:
                return StackVerdict(
                    StackResult.FAIL,
                    detail=f"affirmed_count_after={result.affirmed_count_after}, "
                           f"expected {fact['expected_affirmed_count_after']}",
                )
        stored.append(
            f"{claim['pattern']}.{claim['predicate']}"
            f"(session_ids={result.session_ids_after}, "
            f"affirmed={result.affirmed_count_after})"
        )
    return StackVerdict(
        StackResult.PASS,
        detail=f"stored {len(stored)} fact(s): {'; '.join(stored)}",
    )


def _run_assistant_lookup(entry: dict, stack: V2Stack) -> StackVerdict:
    """Apply substrate fixture, then walk each expected fact and
    compare to the entry's expected_walker_outcome."""
    populate_substrate(stack, entry["id"])
    current_session = entry.get("session")
    raw_text = entry["text"]

    for fact in entry["expected_facts"]:
        claim = _claim_from_expected(fact, source_text=raw_text, registry=stack.registry)
        if not claim["predicate"]:
            return StackVerdict(
                StackResult.FAIL,
                detail="expected_facts[0].predicate_in empty",
            )
        # Walker needs a Layer 2 decision. Use a stub routing_fn that
        # picks user_authoritative for self-attribute claims and
        # retrieval otherwise — we don't actually dispatch to verifiers,
        # only walk the tiers.
        method = "user_authoritative"  # safe default for the corpus
        router = Layer2Router(
            stack.store, stack.registry,
            memo=stack.memo,
            routing_fn=_stub_routing_fn(method, "audit stub"),
        )
        try:
            layer2_decision = router.classify(
                claim, source_turn_id=stack.synthetic_turn_id,
            )
            decision = walk_claim(
                claim, layer2_decision, stack.store,
                registry=stack.registry,
                predicate_oracle=stack.predicate_oracle,
                entity_oracle=stack.entity_oracle,
                taxonomy_oracle=stack.taxonomy_oracle,
                distribution_oracle=stack.distribution_oracle,
                llm=None,
                source_turn_id=stack.synthetic_turn_id,
                current_session=current_session,
                fresh_dispatch=None,
            )
        except Exception as exc:
            return StackVerdict(
                StackResult.ERROR,
                detail="walk_claim raised",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

        # Phase 8g: prefer expected_walker_outcome; fall back to legacy.
        expected_outcome = (
            fact.get("expected_walker_outcome")
            or fact.get("expected_tier_u_outcome")
        )
        if expected_outcome is None:
            return StackVerdict(
                StackResult.FAIL,
                detail="entry has neither expected_walker_outcome nor "
                       "expected_tier_u_outcome",
            )
        if decision.outcome.value != expected_outcome:
            return StackVerdict(
                StackResult.FAIL,
                detail=f"walker outcome={decision.outcome.value!r} "
                       f"served_from={decision.served_from_tier!r}, "
                       f"expected outcome={expected_outcome!r}",
            )
        # When expected_served_from_tier is provided, check it too.
        expected_tier = fact.get("expected_served_from_tier")
        if expected_tier is not None and decision.served_from_tier != expected_tier:
            return StackVerdict(
                StackResult.FAIL,
                detail=f"served_from_tier={decision.served_from_tier!r}, "
                       f"expected {expected_tier!r}",
            )
    return StackVerdict(
        StackResult.PASS,
        detail=f"walker matched expected outcome on "
               f"{len(entry['expected_facts'])} fact(s)",
    )


# ============================================================================
# Public dispatch
# ============================================================================


def run_entry(entry: dict, stack: V2Stack) -> StackVerdict:
    """Dispatch a corpus entry to the right per-shape runner."""
    shape = shape_of(entry)
    if shape is SmokeEntryShape.SUBSTRATE_DIRECT:
        return _run_substrate_direct(entry, stack)
    if shape is SmokeEntryShape.TWO_TEXT_ORACLE:
        return _run_two_text_oracle(entry, stack)
    if shape is SmokeEntryShape.ROUTING_MEMO:
        return _run_routing_memo(entry, stack)
    if shape is SmokeEntryShape.USER_STORAGE:
        return _run_user_storage(entry, stack)
    if shape is SmokeEntryShape.ASSISTANT_LOOKUP:
        return _run_assistant_lookup(entry, stack)
    return StackVerdict(
        StackResult.ERROR,
        detail=f"unknown shape {shape!r}",
    )
