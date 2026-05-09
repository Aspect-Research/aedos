"""Tier W — world cache (v0.14 Phase 7).

Successor to v1's ``src/cache/verification_cache.py``. Same shape
in the broad strokes (canonical_key indexed table, TTL-aware
expiration, refresh/contradiction counts driving Beta posterior),
with two architectural changes:

1. **The 8-state verification_status enum.** v1 wrote one of three
   values to the ``verdict`` column ("verified", "contradicted",
   "inconclusive"). v2 widens that column to carry the full 8-state
   architectural enum: verified, contradicted, user_asserted (rare
   in W; see below), unverifiable_in_principle, retrieval_inconclusive,
   retrieval_failed, unverifiable_pending_implementation,
   routing_anomaly. The schema is unchanged; the column accepts the
   richer values without a CHECK constraint. Legacy v1 values are
   a strict subset, so any v1 cache file is forward-compatible.

2. **Pattern-registry-driven canonical key normalization.** v1
   case-folded all string slot values; v2's substrate is case-
   sensitive on entities (``apple`` ≠ ``Apple``). The Phase 7 plan
   commits to per-slot-type normalization: entity-typed slots get
   strip-only (case preserved); string/date slots get strip + lower
   + whitespace collapse; numeric values are coerced so 5 and 5.0
   collide. Slot types come from the ``PatternRegistry`` (the
   architectural source of truth).

The oracle resolution chain
==========================

Tier W lookups parallel Tier U's three-stage resolution:

  1. **Literal match.** Compute the claim's canonical_key; SELECT
     against the indexed column. Matching key + non-expired row →
     MATCH (under same polarity) or CONTRADICTION (opposite polarity
     under the same predicate / slots / tense; this only applies to
     polarity-equivalence — the polarity is encoded in the key, so
     opposite-polarity is a separate row).

  2. **Predicate equivalence on same-pattern, same-key-slot rows.**
     Walk cached rows under (pattern, key_slot_values), consult the
     ``predicate_equivalence`` oracle on each row's predicate vs
     the claim's. Same resolution table as Tier U: equivalent →
     match-if-same-polarity / contradiction-if-different;
     contradictory → match-if-different-polarity (cheetahs case) /
     contradiction-if-same. ``slot_reversal != 'none'`` falls
     through (Phase 7 doesn't consume slot transformations at the
     cache layer; future phase territory).

  3. **Alias-identity broadening.** Walk cached rows under (pattern)
     with no key-slot filter. For each candidate, walk identity slots
     through ``entity_equivalence``. Qualifying candidates run
     through literal + predicate-equivalence against the claim's
     predicate.

Each stage's outcome emits a ``tier_w_lookup`` event. On MATCH /
CONTRADICTION the literal stage emits ``tier_w_hit``. On a write
from ``write_verifier_result``, ``tier_w_write`` fires.

What the v2 cache table inherits unchanged
==========================================

The schema's ``verification_cache`` table is byte-identical to v1's:
canonical_key, pattern, predicate, verdict, evidence, stability_class,
cached_at, expires_at, hit_count, evidence_hash, source_urls,
last_refreshed_at, refresh_count, contradiction_count. The column
``verdict`` carries the 8-state status; everything else is shared.

Note: the column names ``refresh_count`` / ``contradiction_count``
are v1 vocabulary; the v0.14 architecture's frequentist counts
correspond to ``affirmed_count`` / ``contradicted_count`` (per
principle 3). The math is identical — same Beta(1,1) posterior over
the same counts. The schema diverges in name only because changing
the cache table's column names mid-stack would force a schema
migration this phase isn't taking on. Tier W's docstrings refer to
"refresh_count" when speaking about the cache row and to
"affirmed_count" when speaking about the architectural concept.

Population rule (Ambiguity #8 of the Phase 7 plan)
==================================================

Tier W is populated **only by verifier output** (via
``write_verifier_result``). User-asserted world claims live in Tier
U with provenance; they never get copied into W. The architecture's
user-vs-world split is preserved at the storage layer. If Phase 8's
cross-tier contradiction work needs to consult U while evaluating W,
it reads the two tiers separately rather than denormalizing.

Migration sketch (for documentation only)
=========================================

Legacy v1 cache rows wrote one of three values to the ``verdict``
column. v2 widens this to 8 states. v2 always resets the DB on
schema changes, so no real migration is required. The
``_document_migration_pattern`` function below sketches what a
migration would look like if we ever needed one — purely for
documentation. It is NOT called from production code paths.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.fact_store import FactStore
from src.layer1_extraction.pattern_registry import (
    Pattern,
    PatternRegistry,
)
from src.layer3_substrate.classifier_base import _safe_emit_event
from src.layer3_substrate.entity_equivalence import (
    EntityEquivalence,
    EntityEquivalenceVerdict,
)
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
    PredicateEquivalenceVerdict,
)
from src.layer4_lookup.types import (
    LookupOutcome,
    TierWResult,
)
from src.llm_client import LLMClient


# ============================================================================
# Lookup-first helpers (v0.14 Phase 8d — duplicated from tier_u.py)
# ============================================================================
#
# Same lookup-first contract as tier_u: with llm=None, cold cells return
# None instead of raising. Duplicated rather than imported to keep each
# tier module self-contained — these helpers have no shared state and
# the bodies are small.


def _resolve_predicate_equivalence(
    oracle: PredicateEquivalence,
    pattern: str,
    predicate_query: str,
    predicate_stored: str,
    *,
    llm: Optional[LLMClient],
    source_turn_id: Optional[int],
) -> Optional[PredicateEquivalenceVerdict]:
    """Lookup-first predicate_equivalence resolution. See tier_u
    docstring for the contract."""
    if llm is None:
        existing = oracle.lookup(pattern, predicate_query, predicate_stored)
        if existing is None:
            return None
        return PredicateEquivalenceVerdict(
            label=existing.label,
            slot_reversal=existing.slot_reversal,
            reason=existing.reason,
            row_id=existing.id,
            served_from_cache=True,
            confidence=existing.confidence(),
            classification_failed=False,
        )
    return oracle.consult(
        pattern, predicate_query, predicate_stored,
        llm=llm, source_turn_id=source_turn_id,
    )


def _resolve_entity_equivalence(
    oracle: EntityEquivalence,
    entity_query: str,
    entity_stored: str,
    *,
    llm: Optional[LLMClient],
    source_turn_id: Optional[int],
) -> Optional[EntityEquivalenceVerdict]:
    """Lookup-first entity_equivalence resolution. See tier_u docstring."""
    if llm is None:
        existing = oracle.lookup(entity_query, entity_stored)
        if existing is None:
            return None
        return EntityEquivalenceVerdict(
            label=existing.label,
            reason=existing.reason,
            row_id=existing.id,
            served_from_cache=True,
            confidence=existing.confidence(),
            classification_failed=False,
        )
    return oracle.consult(
        entity_query, entity_stored,
        llm=llm, source_turn_id=source_turn_id,
    )


# ============================================================================
# The 8-state verification_status enum (subset relevant to Tier W)
# ============================================================================

# Tier W never writes user_asserted (Tier U's domain) or routing_anomaly
# (Layer 2's terminal state — never reaches a verifier). The other 6 are
# all valid Tier W ``verdict`` values.
TIER_W_VERIFICATION_STATUSES: tuple[str, ...] = (
    "verified",
    "contradicted",
    "unverifiable_in_principle",
    "retrieval_inconclusive",
    "retrieval_failed",
    "unverifiable_pending_implementation",
)

# v0.14.1 — Tier W's storage policy is now strictly informational. Only
# verdicts that carry actionable knowledge get persisted. The other
# statuses go through ``write_verifier_result`` (so the audit trail
# still records them via a ``skipped_no_information`` event) but do
# not insert into ``verification_cache``. Rationale: a cache hit on
# ``retrieval_inconclusive`` carries no information AND suppresses
# the retry that might land a real verdict next time. See architecture
# principle 3 (frequentist confidence from independent external
# evidence) — non-evidence shouldn't accrete in the cache.
_CACHEABLE_VERIFICATION_STATUSES: frozenset[str] = frozenset({
    "verified",
    "contradicted",
})


# ============================================================================
# Canonical-key construction (pattern-registry-driven)
# ============================================================================


# Past-tense markers ported verbatim from v1. Same conservative bias:
# false positives split a key that could've collided (small hit-rate
# loss); false negatives keep current behavior (safe).
_PAST_TENSE_MARKERS = re.compile(
    r"\b(was|were|had been|used to|formerly|previously|once|originally)\b",
    re.IGNORECASE,
)


def claim_tense(claim: dict) -> str:
    """Return 'past' or 'present' based on source_text markers."""
    source_text = claim.get("source_text") or ""
    if _PAST_TENSE_MARKERS.search(source_text):
        return "past"
    return "present"


def _normalize_predicate(p: str) -> str:
    """Strip + lowercase; predicates are case-presentational.

    Mirrors ``predicate_equivalence._canonical_pair``'s normalization.
    No stem stripping (a v1 hack that conflated semantically-distinct
    predicates the substrate now disambiguates correctly).
    """
    return (p or "").strip().lower()


def _normalize_slot_value(value: Any, slot_type: str) -> str:
    """Per-type slot-value normalization (Ambiguity #3 of the Phase 7
    plan: pattern-registry-driven, not blanket lowercasing).

    Two contracts pinned by tests:
      * ``Apple`` ≠ ``apple`` for entity-typed slots (case carries
        entity-disambiguation signal — the substrate's contract).
      * ``5`` and ``5.0`` collide for numeric values (number
        normalization).

    Slot type rules:
      * ``entity``, ``entity_or_string``  → strip-only, case-preserved
      * ``string``, ``date``              → strip + lowercase + whitespace
                                            collapse (case isn't semantic
                                            for free-form labels / dates)
      * ``list``                          → JSON-encoded, sort_keys (each
                                            element recursively normalized
                                            as a string)
      * ``any`` (or unknown)              → conservative: strip-only when
                                            string (treat as entity-like);
                                            JSON when not

    Numeric coercion runs BEFORE per-type rules: bool/int/float values
    map to a stable string form regardless of slot type, so the
    "5 == 5.0" contract holds even when the slot is declared ``string``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Preserve the int-equals-float collision: 5.0 → "5" so a claim
        # carrying value=5 and a verifier emitting 5.0 cache-collide.
        if value.is_integer():
            return str(int(value))
        return str(value)

    if isinstance(value, list):
        return json.dumps(
            [_normalize_slot_value(v, "any") for v in value],
            ensure_ascii=False,
        )
    if isinstance(value, dict):
        # Recursively normalize values, sort keys.
        normalized = {
            k: _normalize_slot_value(v, "any")
            for k, v in value.items()
        }
        return json.dumps(normalized, sort_keys=True, ensure_ascii=False)

    if not isinstance(value, str):
        return json.dumps(value, default=str, ensure_ascii=False)

    s = " ".join(value.split())  # collapse internal whitespace
    if slot_type in ("entity", "entity_or_string"):
        return s.strip()
    # string / date / any → lowercase + strip
    return s.strip().lower()


def _slot_type(pattern: Pattern, slot_name: str) -> str:
    """Return the slot's declared type, or 'any' if the pattern doesn't
    name the slot (extracted-but-not-declared slots fall through to the
    conservative 'any' rule)."""
    slot = pattern.slot(slot_name)
    if slot is None:
        return "any"
    return slot.type


def canonicalize_claim_key(claim: dict, registry: PatternRegistry) -> str:
    """Produce the stable canonical key for cache lookup.

    Shape: ``{pattern}|{predicate}|p={polarity}|t={tense}|{slots_block}``

    where ``slots_block`` is ``key1=val1&key2=val2&...`` with
    alphabetically-sorted keys, each value normalized per its
    declared slot type via ``_normalize_slot_value``.

    The pattern is required to validate against the registry. An
    unknown pattern raises ``ValueError``. Unknown slot keys (those
    not declared on the pattern) get the 'any' normalization.

    Raises ``KeyError`` if the claim is missing pattern or predicate.
    """
    pattern_name = claim.get("pattern")
    if not pattern_name or not isinstance(pattern_name, str):
        raise KeyError(
            f"claim missing 'pattern' field; got {pattern_name!r}"
        )
    pattern = registry.get(pattern_name)

    predicate = _normalize_predicate(str(claim.get("predicate", "")))
    if not predicate:
        raise KeyError(
            f"claim missing 'predicate' field after normalization; "
            f"got {claim.get('predicate')!r}"
        )

    polarity = int(claim.get("polarity", 1))
    tense = claim_tense(claim)
    slots = claim.get("slots") or {}

    parts: list[str] = []
    for k in sorted(slots.keys()):
        normalized = _normalize_slot_value(slots[k], _slot_type(pattern, k))
        parts.append(f"{k}={normalized}")
    slots_block = "&".join(parts)
    # The leading lowercase pattern name and the encoded polarity/tense
    # match v1's shape so trace UIs that grep keys for substrings
    # ("|p=1|") keep working; the ``predicate`` and slot-value layers
    # are where v2 diverges.
    return (
        f"{pattern_name.strip().lower()}|{predicate}"
        f"|p={polarity}|t={tense}|{slots_block}"
    )


# ============================================================================
# Canonical-key parsing
# ============================================================================


@dataclass(frozen=True)
class _ParsedKey:
    pattern: str
    predicate: str
    polarity: int
    tense: str
    slots: dict[str, str]   # values are post-normalization strings


def _parse_canonical_key(key: str) -> Optional[_ParsedKey]:
    """Reverse the canonical-key encoding far enough to read
    polarity/tense/slots back out for substrate-mediated comparison.

    Returns None on a malformed key (e.g. v1 keys that lack the
    ``t=`` segment). Callers treat None as "skip this row" rather
    than crashing the lookup.

    Note: this parses normalized values, not original slot values. The
    case-preservation contract on entity slots means parsed values are
    a faithful read of what's stored; for string slots they're already
    lowercased.
    """
    parts = key.split("|")
    if len(parts) < 4:
        return None
    pattern, predicate = parts[0], parts[1]
    pol_part = parts[2]
    tense_part = parts[3]
    if not pol_part.startswith("p="):
        return None
    if not tense_part.startswith("t="):
        return None
    try:
        polarity = int(pol_part[2:])
    except ValueError:
        return None
    tense = tense_part[2:]
    slots_block = parts[4] if len(parts) >= 5 else ""

    slots: dict[str, str] = {}
    if slots_block:
        for pair in slots_block.split("&"):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            slots[k] = v
    return _ParsedKey(
        pattern=pattern, predicate=predicate, polarity=polarity,
        tense=tense, slots=slots,
    )


# ============================================================================
# Cache row dataclass + write outcomes
# ============================================================================


@dataclass(frozen=True)
class TierWRow:
    """A row from the verification_cache table.

    ``verdict`` carries the 8-state ``verification_status`` per the
    Phase 7 architectural commitment. ``refresh_count`` and
    ``contradiction_count`` are the v1 column names; architecturally
    these are ``affirmed_count`` and ``contradicted_count``.
    """

    id: int
    canonical_key: str
    pattern: str
    predicate: str
    verdict: str               # 8-state verification_status value
    evidence: Optional[dict[str, Any]]
    stability_class: str
    cached_at: str
    expires_at: Optional[str]
    hit_count: int
    evidence_hash: Optional[str]
    source_urls: list[str]
    last_refreshed_at: Optional[str]
    refresh_count: int
    contradiction_count: int

    def confidence(self) -> float:
        from src.layer2_routing.constants import confidence_from_counts
        return confidence_from_counts(
            self.refresh_count, self.contradiction_count,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "canonical_key": self.canonical_key,
            "pattern": self.pattern,
            "predicate": self.predicate,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "stability_class": self.stability_class,
            "cached_at": self.cached_at,
            "expires_at": self.expires_at,
            "hit_count": self.hit_count,
            "evidence_hash": self.evidence_hash,
            "source_urls": list(self.source_urls),
            "last_refreshed_at": self.last_refreshed_at,
            "refresh_count": self.refresh_count,
            "contradiction_count": self.contradiction_count,
            "confidence": self.confidence(),
        }

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False  # immutable
        try:
            expiry = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False
        check = now or datetime.now(timezone.utc)
        return expiry < check


@dataclass(frozen=True)
class TierWWriteOutcome:
    """Outcome of a ``write_verifier_result`` call.

    ``action`` values:
      * ``inserted`` — new row, no prior verdict at this key
      * ``refreshed`` — same verdict as before; refresh_count bumped
      * ``contradicted_and_replaced`` — verdict differs from prior;
        contradiction_count bumped, row's verdict overwritten
      * ``skipped_volatile`` — ttl_seconds == 0 (volatile, no cache)
    """

    action: str
    canonical_key: str
    prior_verdict: Optional[str] = None
    row_id: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "canonical_key": self.canonical_key,
            "prior_verdict": self.prior_verdict,
            "row_id": self.row_id,
        }


# ============================================================================
# Lookup
# ============================================================================


def lookup(
    claim: dict,
    store: FactStore,
    predicate_oracle: PredicateEquivalence,
    *,
    key_slot_names: list[str],
    registry: PatternRegistry,
    llm: Optional[LLMClient] = None,
    source_turn_id: Optional[int] = None,
    entity_oracle: Optional[EntityEquivalence] = None,
) -> TierWResult:
    """Look for a matching or contradicting cached verifier verdict.

    Three resolution stages, in cost-ascending order:
      1. Literal match on canonical_key (SQL only).
      2. Predicate-equivalence broadening on same-pattern same-key-slot rows.
      3. Alias-identity broadening via entity_equivalence on same-pattern rows.

    Mirrors ``tier_u.lookup``'s shape so the walker (Phase 7d) can
    consume the two results uniformly. Polarity-flip semantics for
    Tier W are tighter than Tier U's: cache rows record specific
    polarities; an opposite-polarity literal row is a CONTRADICTION
    of the lookup, not a flippable MATCH (Tier U's contradictory-
    verdict-flips-to-match logic is unique to user-asserted facts
    where the user's assertion is authoritative; for cached verifier
    output, opposite polarity at the same key means the verifier
    decided the opposite-polarity proposition was the verdict).
    """
    canonical_key = canonicalize_claim_key(claim, registry)
    pattern = claim.get("pattern", "")
    predicate = claim.get("predicate", "")
    polarity = int(claim.get("polarity", 1))
    slots = claim.get("slots") or {}
    key_slots = {k: slots[k] for k in key_slot_names if k in slots}

    # ---- Stage 1: literal match -----------------------------------
    literal = _literal_match(store, canonical_key, polarity, claim, registry)
    if literal is not None:
        outcome, row = literal
        _safe_emit_event(
            store, source_turn_id, "tier_w_lookup",
            {
                "stage": "literal",
                "tier": "W",
                "outcome": outcome.value,
                "canonical_key": row.canonical_key,
                "row_id": row.id,
                "verification_status": row.verdict,
            },
        )
        _safe_emit_event(
            store, source_turn_id, "tier_w_hit",
            {
                "row_id": row.id,
                "canonical_key": row.canonical_key,
                "verdict": row.verdict,
                "outcome": outcome.value,
                "expires_at": row.expires_at,
            },
        )
        if outcome is LookupOutcome.MATCH:
            return TierWResult(
                outcome=LookupOutcome.MATCH,
                matching_row_id=row.id,
                matching_canonical_key=row.canonical_key,
                verification_status=row.verdict,
                evidence=row.evidence,
                expires_at=row.expires_at,
                via=[],
                notes=[
                    "literal match: canonical_key + same polarity"
                ],
            )
        return TierWResult(
            outcome=LookupOutcome.CONTRADICTION,
            contradicting_row_id=row.id,
            contradicting_canonical_key=row.canonical_key,
            verification_status=row.verdict,
            evidence=row.evidence,
            expires_at=row.expires_at,
            via=[],
            notes=[
                "literal contradiction: same predicate / slots / tense, "
                "opposite polarity"
            ],
        )

    # ---- Stage 2: predicate-equivalence broadening ----------------
    exact_candidates = _gather_exact_identity_candidates(
        store, pattern, key_slots, predicate, claim, registry,
    )
    for row, parsed in exact_candidates:
        verdict = _resolve_predicate_equivalence(
            predicate_oracle, pattern, predicate, parsed.predicate,
            llm=llm, source_turn_id=source_turn_id,
        )
        if verdict is None:
            continue  # cold cell, no LLM — skip per lookup-first contract
        outcome_kind = _resolve_via_predicate_verdict(
            verdict,
            claim_polarity=polarity,
            candidate_polarity=parsed.polarity,
        )
        if outcome_kind is None:
            continue
        kind, polarity_flipped = outcome_kind
        notes = [
            f"oracle resolved ({predicate!r}, {parsed.predicate!r}) "
            f"-> {verdict.label} + {verdict.slot_reversal}; "
            f"polarity_flipped={polarity_flipped}"
        ]
        if kind == "match":
            _safe_emit_event(
                store, source_turn_id, "tier_w_lookup",
                {
                    "stage": "predicate_equivalence",
                    "tier": "W",
                    "outcome": "match",
                    "row_id": row.id,
                    "verification_status": row.verdict,
                },
            )
            return TierWResult(
                outcome=LookupOutcome.MATCH,
                matching_row_id=row.id,
                matching_canonical_key=row.canonical_key,
                verification_status=row.verdict,
                evidence=row.evidence,
                expires_at=row.expires_at,
                via=["predicate_equivalence"],
                predicate_equivalence_row_id=verdict.row_id,
                polarity_flipped=polarity_flipped,
                notes=notes,
            )
        _safe_emit_event(
            store, source_turn_id, "tier_w_lookup",
            {
                "stage": "predicate_equivalence",
                "tier": "W",
                "outcome": "contradiction",
                "row_id": row.id,
                "verification_status": row.verdict,
            },
        )
        return TierWResult(
            outcome=LookupOutcome.CONTRADICTION,
            contradicting_row_id=row.id,
            contradicting_canonical_key=row.canonical_key,
            verification_status=row.verdict,
            evidence=row.evidence,
            expires_at=row.expires_at,
            via=["predicate_equivalence"],
            predicate_equivalence_row_id=verdict.row_id,
            polarity_flipped=polarity_flipped,
            notes=notes,
        )

    # ---- Stage 3: alias-identity broadening -----------------------
    if entity_oracle is not None and key_slots:
        alias_results = _gather_alias_identity_candidates(
            store, pattern, key_slots, key_slot_names, claim, registry,
            entity_oracle, llm, source_turn_id,
        )
        for row, parsed, entity_row_ids in alias_results:
            # Literal-predicate path against the alias candidate.
            if parsed.predicate == _normalize_predicate(predicate):
                if parsed.polarity == polarity:
                    _safe_emit_event(
                        store, source_turn_id, "tier_w_lookup",
                        {
                            "stage": "entity_equivalence",
                            "tier": "W",
                            "outcome": "match",
                            "row_id": row.id,
                            "verification_status": row.verdict,
                        },
                    )
                    return TierWResult(
                        outcome=LookupOutcome.MATCH,
                        matching_row_id=row.id,
                        matching_canonical_key=row.canonical_key,
                        verification_status=row.verdict,
                        evidence=row.evidence,
                        expires_at=row.expires_at,
                        via=["entity_equivalence"],
                        entity_equivalence_row_ids=list(entity_row_ids),
                        notes=[
                            f"alias-identity match via entity_equivalence "
                            f"rows {entity_row_ids!r}; literal predicate match"
                        ],
                    )
                _safe_emit_event(
                    store, source_turn_id, "tier_w_lookup",
                    {
                        "stage": "entity_equivalence",
                        "tier": "W",
                        "outcome": "contradiction",
                        "row_id": row.id,
                        "verification_status": row.verdict,
                    },
                )
                return TierWResult(
                    outcome=LookupOutcome.CONTRADICTION,
                    contradicting_row_id=row.id,
                    contradicting_canonical_key=row.canonical_key,
                    verification_status=row.verdict,
                    evidence=row.evidence,
                    expires_at=row.expires_at,
                    via=["entity_equivalence"],
                    entity_equivalence_row_ids=list(entity_row_ids),
                    notes=[
                        f"alias-identity contradiction via entity_equivalence "
                        f"rows {entity_row_ids!r}; literal predicate, "
                        f"opposite polarity"
                    ],
                )

            # Different predicate after alias-identity broadening:
            # consult predicate_equivalence on top (lookup-first).
            verdict = _resolve_predicate_equivalence(
                predicate_oracle, pattern, predicate, parsed.predicate,
                llm=llm, source_turn_id=source_turn_id,
            )
            if verdict is None:
                continue
            outcome_kind = _resolve_via_predicate_verdict(
                verdict,
                claim_polarity=polarity,
                candidate_polarity=parsed.polarity,
            )
            if outcome_kind is None:
                continue
            kind, polarity_flipped = outcome_kind
            notes = [
                f"alias-identity + predicate equivalence: "
                f"entity rows={entity_row_ids!r}, "
                f"predicate verdict=({verdict.label}, {verdict.slot_reversal}), "
                f"polarity_flipped={polarity_flipped}"
            ]
            if kind == "match":
                _safe_emit_event(
                    store, source_turn_id, "tier_w_lookup",
                    {
                        "stage": "entity_equivalence+predicate_equivalence",
                        "tier": "W",
                        "outcome": "match",
                        "row_id": row.id,
                        "verification_status": row.verdict,
                    },
                )
                return TierWResult(
                    outcome=LookupOutcome.MATCH,
                    matching_row_id=row.id,
                    matching_canonical_key=row.canonical_key,
                    verification_status=row.verdict,
                    evidence=row.evidence,
                    expires_at=row.expires_at,
                    via=["entity_equivalence", "predicate_equivalence"],
                    predicate_equivalence_row_id=verdict.row_id,
                    entity_equivalence_row_ids=list(entity_row_ids),
                    polarity_flipped=polarity_flipped,
                    notes=notes,
                )
            _safe_emit_event(
                store, source_turn_id, "tier_w_lookup",
                {
                    "stage": "entity_equivalence+predicate_equivalence",
                    "tier": "W",
                    "outcome": "contradiction",
                    "row_id": row.id,
                    "verification_status": row.verdict,
                },
            )
            return TierWResult(
                outcome=LookupOutcome.CONTRADICTION,
                contradicting_row_id=row.id,
                contradicting_canonical_key=row.canonical_key,
                verification_status=row.verdict,
                evidence=row.evidence,
                expires_at=row.expires_at,
                via=["entity_equivalence", "predicate_equivalence"],
                predicate_equivalence_row_id=verdict.row_id,
                entity_equivalence_row_ids=list(entity_row_ids),
                polarity_flipped=polarity_flipped,
                notes=notes,
            )

    # ---- Miss --------------------------------------------------------
    _safe_emit_event(
        store, source_turn_id, "tier_w_lookup",
        {
            "stage": "miss",
            "tier": "W",
            "outcome": "miss",
            "canonical_key": canonical_key,
            "candidates_considered": len(exact_candidates),
        },
    )
    return TierWResult(outcome=LookupOutcome.MISS)


# ---- stage 1 helper -------------------------------------------------------


def _literal_match(
    store: FactStore,
    canonical_key: str,
    polarity: int,
    claim: dict,
    registry: PatternRegistry,
) -> Optional[tuple[LookupOutcome, TierWRow]]:
    """SELECT the canonical_key. If found and non-expired, MATCH on
    polarity equality; if the polarity differs (i.e. the cache has the
    SAME claim with opposite polarity), that's CONTRADICTION.

    Implementation note: same-claim opposite-polarity lookups have
    DIFFERENT canonical keys (polarity is encoded in the key). So the
    "literal contradiction" path here checks for a row with the
    polarity-flipped key.
    """
    row = _select_by_key(store, canonical_key)
    if row is not None and not row.is_expired():
        # Hit count bookkeeping (best-effort).
        _bump_hit_count(store, row.id)
        return (LookupOutcome.MATCH, row)

    # Polarity-flipped key for the contradiction case.
    flipped_polarity = 1 - polarity
    flipped_claim = dict(claim)
    flipped_claim["polarity"] = flipped_polarity
    flipped_key = canonicalize_claim_key(flipped_claim, registry)
    flipped_row = _select_by_key(store, flipped_key)
    if flipped_row is not None and not flipped_row.is_expired():
        _bump_hit_count(store, flipped_row.id)
        return (LookupOutcome.CONTRADICTION, flipped_row)
    return None


def _select_by_key(
    store: FactStore, canonical_key: str,
) -> Optional[TierWRow]:
    row = store._conn.execute(
        "SELECT * FROM verification_cache WHERE canonical_key = ?",
        (canonical_key,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_dataclass(row)


def _bump_hit_count(store: FactStore, row_id: int) -> None:
    """Increment hit_count. Best-effort; never crashes the lookup."""
    try:
        store._conn.execute(
            "UPDATE verification_cache SET hit_count = hit_count + 1 "
            "WHERE id = ?",
            (row_id,),
        )
        store._conn.commit()
    except Exception:
        pass


# ---- stage 2 helper -------------------------------------------------------


def _gather_exact_identity_candidates(
    store: FactStore,
    pattern: str,
    key_slots: dict[str, Any],
    claim_predicate: str,
    claim: dict,
    registry: PatternRegistry,
) -> list[tuple[TierWRow, _ParsedKey]]:
    """Cache rows under (pattern, identity slots) whose predicate
    differs from the claim's. The literal-match step already handled
    same-predicate-same-polarity and same-predicate-opposite-polarity
    cases, so they're excluded here.

    Slot matching against parsed keys uses the same per-slot
    normalization as canonical_key construction so a row keyed under
    ``location=NYC`` matches a query at ``location=NYC`` exactly. This
    means alias resolution (NYC vs New York City) is the *next*
    stage's job; this stage only catches predicate paraphrase under
    same identity.

    Tense is included in the SQL filter implicitly: the canonical
    key has the tense segment, and rows under different tenses get
    different keys, so a query at present-tense never sees a past-
    tense cached row's predicate via this stage.
    """
    rows = store._conn.execute(
        "SELECT * FROM verification_cache WHERE pattern = ?",
        (pattern,),
    ).fetchall()
    out: list[tuple[TierWRow, _ParsedKey]] = []
    pattern_obj = registry.get(pattern) if registry.has(pattern) else None
    claim_tense_value = claim_tense(claim)
    norm_claim_predicate = _normalize_predicate(claim_predicate)

    for r in rows:
        row = _row_to_dataclass(r)
        if row.is_expired():
            continue
        parsed = _parse_canonical_key(row.canonical_key)
        if parsed is None:
            continue
        if parsed.predicate == norm_claim_predicate:
            # Literal stage already handled this path.
            continue
        if parsed.tense != claim_tense_value:
            # Different tense versions cache independently; no
            # broadening across the tense divide.
            continue
        if not _key_slots_match(parsed.slots, key_slots, pattern_obj):
            continue
        out.append((row, parsed))
    return out


def _key_slots_match(
    parsed_slots: dict[str, str],
    claim_key_slots: dict[str, Any],
    pattern: Optional[Pattern],
) -> bool:
    """Check whether every claim key slot has a literal-equal value
    in the parsed key. Uses the same per-slot normalization as
    canonicalize so 'NYC' query matches a row keyed at 'NYC'.
    """
    for k, v in claim_key_slots.items():
        if k not in parsed_slots:
            return False
        slot_type_name = (
            _slot_type(pattern, k) if pattern is not None else "any"
        )
        normalized_query = _normalize_slot_value(v, slot_type_name)
        if parsed_slots[k] != normalized_query:
            return False
    return True


# ---- stage 3 helper -------------------------------------------------------


def _gather_alias_identity_candidates(
    store: FactStore,
    pattern: str,
    claim_key_slots: dict[str, Any],
    key_slot_names: list[str],
    claim: dict,
    registry: PatternRegistry,
    entity_oracle: EntityEquivalence,
    llm: Optional[LLMClient],
    source_turn_id: Optional[int],
) -> list[tuple[TierWRow, _ParsedKey, list[int]]]:
    """Cache rows under (pattern) whose identity slots are alias-
    equivalent to the claim's per the entity_equivalence oracle.
    Returns (row, parsed_key, entity_row_ids) tuples.

    A candidate qualifies only if EVERY identity slot is either
    literally equal (post-normalization) or alias-equivalent per the
    oracle, AND at least one slot needed the oracle (otherwise the
    candidate would already be in the exact-identity set).
    """
    rows = store._conn.execute(
        "SELECT * FROM verification_cache WHERE pattern = ?",
        (pattern,),
    ).fetchall()
    out: list[tuple[TierWRow, _ParsedKey, list[int]]] = []
    pattern_obj = registry.get(pattern) if registry.has(pattern) else None
    claim_tense_value = claim_tense(claim)

    for r in rows:
        row = _row_to_dataclass(r)
        if row.is_expired():
            continue
        parsed = _parse_canonical_key(row.canonical_key)
        if parsed is None:
            continue
        if parsed.tense != claim_tense_value:
            continue

        entity_row_ids: list[int] = []
        all_qualify = True
        any_via_oracle = False

        for slot_name in key_slot_names:
            cv = claim_key_slots.get(slot_name)
            if cv is None:
                # Claim doesn't provide this slot — can't form an
                # identity comparison. Conservative miss.
                all_qualify = False
                break
            if slot_name not in parsed.slots:
                all_qualify = False
                break
            slot_type_name = (
                _slot_type(pattern_obj, slot_name)
                if pattern_obj is not None else "any"
            )
            cv_normalized = _normalize_slot_value(cv, slot_type_name)
            sv = parsed.slots[slot_name]
            if cv_normalized == sv:
                continue  # literal slot match (post-normalization)
            # Non-string parsed values shouldn't reach the oracle —
            # entity_equivalence is for entity strings only. Skip.
            if not isinstance(cv, str):
                all_qualify = False
                break
            cv_for_oracle = (cv or "").strip()
            sv_for_oracle = sv  # parsed values are post-normalization strings
            if not cv_for_oracle or not sv_for_oracle:
                all_qualify = False
                break
            if cv_for_oracle == sv_for_oracle:
                continue
            verdict = _resolve_entity_equivalence(
                entity_oracle, cv_for_oracle, sv_for_oracle,
                llm=llm, source_turn_id=source_turn_id,
            )
            if verdict is None:
                # Cold cell, no LLM → conservative: candidate doesn't
                # qualify under lookup-first contract.
                all_qualify = False
                break
            if verdict.classification_failed or verdict.label != "same":
                all_qualify = False
                break
            if verdict.row_id is not None:
                entity_row_ids.append(verdict.row_id)
            any_via_oracle = True

        if all_qualify and any_via_oracle:
            out.append((row, parsed, entity_row_ids))
    return out


# ---- predicate-equivalence verdict resolution -----------------------------


def _resolve_via_predicate_verdict(
    verdict: PredicateEquivalenceVerdict,
    *,
    claim_polarity: int,
    candidate_polarity: int,
) -> Optional[tuple[str, bool]]:
    """Map a predicate_equivalence verdict to a tier-W outcome kind.

    Returns ``(kind, polarity_flipped)`` or None on no-signal.

    Same logic as Tier U's resolution (see tier_u._resolve_via_
    predicate_verdict). Cached verifier verdicts get the same
    interpretation as user-asserted facts under predicate-pair
    semantics: equivalent → polarity-equality determines match;
    contradictory → polarity-difference determines match.
    """
    if verdict.classification_failed or verdict.label is None:
        return None
    if verdict.label == "distinct":
        return None
    if verdict.slot_reversal != "none":
        return None  # Phase 7 doesn't consume slot transformations

    if verdict.label == "equivalent":
        if claim_polarity == candidate_polarity:
            return ("match", False)
        return ("contradiction", False)

    if verdict.label == "contradictory":
        if claim_polarity != candidate_polarity:
            return ("match", True)
        return ("contradiction", True)

    return None


# ============================================================================
# Write path
# ============================================================================


def write_verifier_result(
    claim: dict,
    store: FactStore,
    *,
    verification_status: str,
    registry: PatternRegistry,
    evidence: Optional[dict[str, Any]] = None,
    stability_class: str = "decade_stable",
    ttl_seconds: Optional[int] = None,
    source_turn_id: Optional[int] = None,
) -> TierWWriteOutcome:
    """Insert or update a Tier W cache row after a verifier ran.

    **This is the only writer to Tier W.** User-asserted facts live
    in Tier U; Tier U never copies into W (Ambiguity #8 of the Phase
    7 plan). The architectural user-vs-world split is preserved at
    the storage layer.

    ``verification_status`` must be one of the 6 statuses Tier W
    can carry (see ``TIER_W_VERIFICATION_STATUSES``). Passing
    ``user_asserted`` or ``routing_anomaly`` raises ValueError —
    those statuses are out-of-domain for Tier W.

    ``stability_class`` and ``ttl_seconds`` together determine the
    row's expiry. ``ttl_seconds=None`` means the row is immutable
    (no expiry). ``ttl_seconds=0`` means volatile — the write is
    skipped (caller's verifier output is too time-sensitive to
    cache). All other values produce ``expires_at = now +
    ttl_seconds`` ISO-8601.

    Refresh / contradiction count semantics on conflict (when the
    canonical_key already exists):
      * If the new verdict equals the prior verdict: ``refresh_count``
        bumps by 1, ``contradiction_count`` unchanged.
      * If the new verdict differs: ``refresh_count`` unchanged,
        ``contradiction_count`` bumps by 1, ``verdict`` overwritten
        with the new value.
    Both reuse v1's bookkeeping convention.

    Pipeline events emitted:
      * ``tier_w_write`` (action + key + verdict)
      * ``cache_contradiction_replaced`` (only on contradicted_and_
        replaced; mirrors v1's vocabulary so existing trace UI
        consumers keep working)
    """
    if verification_status not in TIER_W_VERIFICATION_STATUSES:
        raise ValueError(
            f"verification_status {verification_status!r} not in "
            f"{TIER_W_VERIFICATION_STATUSES}; Tier W never writes "
            f"user_asserted or routing_anomaly"
        )

    canonical_key = canonicalize_claim_key(claim, registry)

    # v0.14.1 — only verified/contradicted carry information worth
    # caching. The other four statuses (retrieval_inconclusive,
    # retrieval_failed, unverifiable_pending_implementation,
    # unverifiable_in_principle) are "we don't know" verdicts; caching
    # them poisons the cache with non-knowledge AND suppresses retry
    # — the next attempt at the same canonical key short-circuits
    # to the inconclusive cache hit instead of trying again with
    # potentially-better query reformulation or a Wikipedia article
    # that's been edited since. Skip the write; emit an audit event.
    if verification_status not in _CACHEABLE_VERIFICATION_STATUSES:
        _safe_emit_event(
            store, source_turn_id, "tier_w_write",
            {
                "action": "skipped_no_information",
                "canonical_key": canonical_key,
                "verdict": verification_status,
                "stability_class": stability_class,
                "reason": (
                    "non-actionable verdict — re-attempt instead of "
                    "caching the uncertainty"
                ),
            },
        )
        return TierWWriteOutcome(
            action="skipped_no_information",
            canonical_key=canonical_key,
            prior_verdict=None,
            row_id=None,
        )

    if ttl_seconds == 0:
        # Volatile — caller's verifier emits time-sensitive output
        # that shouldn't be cached. Log + skip.
        _safe_emit_event(
            store, source_turn_id, "tier_w_write",
            {
                "action": "skipped_volatile",
                "canonical_key": canonical_key,
                "verdict": verification_status,
                "stability_class": stability_class,
            },
        )
        return TierWWriteOutcome(
            action="skipped_volatile",
            canonical_key=canonical_key,
            prior_verdict=None,
            row_id=None,
        )

    pattern = claim.get("pattern", "")
    predicate = _normalize_predicate(str(claim.get("predicate", "")))

    now = datetime.now(timezone.utc)
    cached_at = now.isoformat()
    last_refreshed_at = cached_at
    expires_at: Optional[str]
    if ttl_seconds is None:
        expires_at = None
    else:
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()

    evidence_json = (
        json.dumps(evidence, default=str) if evidence else None
    )
    evidence_hash = (
        hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        if evidence_json else None
    )
    source_urls_json = (
        json.dumps(_extract_source_urls(evidence), default=str)
        if evidence else None
    )

    prior_row = store._conn.execute(
        "SELECT verdict, refresh_count, contradiction_count "
        "FROM verification_cache WHERE canonical_key = ?",
        (canonical_key,),
    ).fetchone()
    prior_verdict = prior_row["verdict"] if prior_row else None

    if prior_row is None:
        new_refresh = 0
        new_contradictions = 0
        action = "inserted"
    elif prior_verdict == verification_status:
        new_refresh = int(prior_row["refresh_count"] or 0) + 1
        new_contradictions = int(prior_row["contradiction_count"] or 0)
        action = "refreshed"
    else:
        new_refresh = int(prior_row["refresh_count"] or 0)
        new_contradictions = int(prior_row["contradiction_count"] or 0) + 1
        action = "contradicted_and_replaced"

    store._conn.execute(
        """
        INSERT INTO verification_cache (
            canonical_key, pattern, predicate, verdict, evidence,
            stability_class, cached_at, expires_at, hit_count, created_at,
            evidence_hash, source_urls,
            last_refreshed_at, refresh_count, contradiction_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_key) DO UPDATE SET
            verdict = excluded.verdict,
            evidence = excluded.evidence,
            stability_class = excluded.stability_class,
            cached_at = COALESCE(verification_cache.cached_at, excluded.cached_at),
            expires_at = excluded.expires_at,
            evidence_hash = excluded.evidence_hash,
            source_urls = excluded.source_urls,
            last_refreshed_at = excluded.last_refreshed_at,
            refresh_count = excluded.refresh_count,
            contradiction_count = excluded.contradiction_count
        """,
        (
            canonical_key, pattern, predicate, verification_status,
            evidence_json, stability_class, cached_at, expires_at, cached_at,
            evidence_hash, source_urls_json,
            last_refreshed_at, new_refresh, new_contradictions,
        ),
    )
    store._conn.commit()

    row_id = store._conn.execute(
        "SELECT id FROM verification_cache WHERE canonical_key = ?",
        (canonical_key,),
    ).fetchone()["id"]

    _safe_emit_event(
        store, source_turn_id, "tier_w_write",
        {
            "action": action,
            "canonical_key": canonical_key,
            "verdict": verification_status,
            "prior_verdict": prior_verdict,
            "row_id": row_id,
            "stability_class": stability_class,
            "ttl_seconds": ttl_seconds,
        },
    )

    if action == "contradicted_and_replaced":
        _safe_emit_event(
            store, source_turn_id, "cache_contradiction_replaced",
            {
                "canonical_key": canonical_key,
                "prior_verdict": prior_verdict,
                "new_verdict": verification_status,
                "row_id": row_id,
            },
        )

    return TierWWriteOutcome(
        action=action,
        canonical_key=canonical_key,
        prior_verdict=prior_verdict,
        row_id=row_id,
    )


def _extract_source_urls(evidence: Optional[dict]) -> list[str]:
    """Pull unique URLs from the evidence dict's snippets list. Same
    convention as v1's verification_cache helper. Returns [] when the
    evidence has no snippets."""
    if not evidence:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    snippets = evidence.get("snippets") if isinstance(evidence, dict) else None
    if isinstance(snippets, list):
        for s in snippets:
            url = (s or {}).get("url") if isinstance(s, dict) else None
            if isinstance(url, str) and url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


# ============================================================================
# Row → dataclass
# ============================================================================


def _row_to_dataclass(row: Any) -> TierWRow:
    src_urls_raw: Optional[str]
    try:
        src_urls_raw = row["source_urls"]
    except (IndexError, KeyError):
        src_urls_raw = None
    try:
        source_urls = json.loads(src_urls_raw) if src_urls_raw else []
    except (json.JSONDecodeError, TypeError):
        source_urls = []
    return TierWRow(
        id=int(row["id"]),
        canonical_key=row["canonical_key"],
        pattern=row["pattern"],
        predicate=row["predicate"],
        verdict=row["verdict"],
        evidence=(
            json.loads(row["evidence"]) if row["evidence"] else None
        ),
        stability_class=row["stability_class"],
        cached_at=row["cached_at"],
        expires_at=row["expires_at"],
        hit_count=int(row["hit_count"] or 0),
        evidence_hash=row["evidence_hash"] if "evidence_hash" in row.keys() else None,
        source_urls=source_urls,
        last_refreshed_at=row["last_refreshed_at"] if "last_refreshed_at" in row.keys() else None,
        refresh_count=int(row["refresh_count"] or 0) if "refresh_count" in row.keys() else 0,
        contradiction_count=int(row["contradiction_count"] or 0) if "contradiction_count" in row.keys() else 0,
    )


# ============================================================================
# Migration sketch (documentation only)
# ============================================================================


def _document_migration_pattern() -> str:
    """Documentation-only sketch of how a v1 → v2 cache migration would
    look. Not called from production code paths.

    Per Ambiguity #2 of the Phase 7 plan: the v2 DB resets cleanly,
    so no actual migration is required. Legacy v1 cache rows wrote
    "verified" / "contradicted" / "inconclusive" to the ``verdict``
    column; these are a strict subset of v2's 8-state enum, so a
    forward-compatible read just works. This function documents the
    semantic mapping for a future operator who hits the migration
    case (e.g. someone preserving a v1 cache file across a schema
    change post-v0.14).

    Returns the migration playbook as a string for inspection by
    tests / docs builds.
    """
    return """\
v1 → v2 verification_cache.verdict migration

  Legacy value     -> v2 8-state status
  ----------------   ----------------------------
  verified         -> verified                  (no change)
  contradicted     -> contradicted              (no change)
  inconclusive     -> retrieval_inconclusive    (most common case)
                  OR retrieval_failed           (when the legacy
                                                 row was written from
                                                 a verifier-error path
                                                 — distinguishable in
                                                 evidence.error_flag)

The split between retrieval_inconclusive and retrieval_failed is
load-bearing under the v0.14 architecture (the latter is a no-op at
Layer 5; the former is a hedge). A migration would inspect each
inconclusive row's evidence.error_flag field and route accordingly:

  evidence.error_flag in {"retrieval_error", "no_results",
                          "judge_parse_error", "judge_error"}
    -> retrieval_failed
  otherwise (judge ran cleanly, said insufficient_evidence)
    -> retrieval_inconclusive

This function is NOT a migration script. The v2 DB resets cleanly
and v1 row counts in production are negligible. The mapping is
preserved here for documentation purposes only.
"""
