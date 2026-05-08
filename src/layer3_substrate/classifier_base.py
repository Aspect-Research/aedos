"""Shared shell for Layer 3 substrate oracles.

Phase 3 introduced ``predicate_equivalence``. Phase 4 added
``entity_equivalence``. Phase 5 adds the two subsumption oracles —
``entity_taxonomy`` and ``predicate_distribution``. This module
exists so all four can share boilerplate without re-deriving it. It
is deliberately thin — the four oracles share enough behaviour to
coordinate, but their schemas, prompts, and key shapes differ enough
that abstracting CRUD or LLM orchestration would be a net cost.

The four oracles fall into three structural shapes:

  * **Symmetric-pair shape** — predicate_equivalence and
    entity_equivalence. Key is an unordered pair under canonical
    lex ordering; label is symmetric under the swap. Each oracle
    owns its own ``_canonical_pair`` helper because the two
    oracles' normalization rules differ (predicate_equivalence
    lowercases; entity_equivalence does not — case is semantic for
    entities).

  * **Directional-pair shape** — entity_taxonomy. Key is an ordered
    pair (child, parent) plus relation_type; the column position is
    meaningful and the label encodes which direction the subsumption
    actually goes. There is NO canonical-pair helper. The natural
    direction is ``child_subsumed_by_parent``; if a caller swaps
    the arguments, the label becomes ``parent_subsumed_by_child``.
    Different orderings produce different rows; the oracle stores
    each direction the caller used.

  * **Singleton-key shape** — predicate_distribution. Key is a
    4-tuple ``(pattern, predicate, polarity, taxonomy_relation_
    type)``; there are no pairs and no swap. Each row is a verdict
    about how a single predicate (in a single pattern, at a single
    polarity) propagates across a single taxonomy relation type.
    The architectural commitment is that distribution is genuinely
    directional — ``lives_in distributes_up part_of`` is not the
    same proposition as ``part_of distributes_up lives_in`` and the
    oracle does not pretend it is.

These three shapes coexist in the substrate and compose at the
walker layer (Phase 7) without forcing a common ABC. The four
oracles share:

  * The lookup-then-LLM-then-UPSERT discipline.
  * Counts-stay-zero on consult (principle 3 — reads are not writes).
  * last_consulted_at as observability metadata (touched on hit,
    NOT a reinforcement signal).
  * Three pipeline events per oracle (``{prefix}_hit``,
    ``{prefix}_write``, ``{prefix}_classification_failed``) plus
    the shared ``oracle_consulted`` stage that the trace UI greps
    against to see all substrate consultations.
  * The ``_ClassificationFailed`` sentinel exception (defined here,
    imported by all four) for raising on malformed LLM tool output.
  * Pattern-independence vs pattern-keying: entity_equivalence and
    entity_taxonomy do NOT key on pattern (the entities they classify
    denote the same thing across patterns); predicate_equivalence
    keys on pattern (predicates are pattern-scoped); predicate_
    distribution keys on pattern (same reason).

What this module gives subclasses:

  * ``_now_iso()``         - ISO 8601 UTC timestamp helper used by
                             every oracle's UPSERT path.
  * ``_safe_emit_event(...)`` - swallow-and-continue wrapper around
                                ``store.insert_pipeline_event`` so a
                                logging failure never crashes a
                                classification call. Mirrors the v1
                                router's ``_log`` pattern.
  * ``_ClassificationFailed`` - sentinel exception raised by an
                                oracle's ``_classify_via_llm`` on
                                any malformed tool response. Carries
                                a ``reason`` (the failure mode) and
                                ``raw`` (the offending tool dict).
                                The caller catches it and converts
                                to a verdict-of-last-resort with
                                ``classification_failed=True``.
  * ``confidence_from_counts`` re-export so subclasses don't have to
    chase the import path through layer2_routing.

What this module deliberately does NOT give subclasses:

  * No abstract ``consult()`` signature. Each oracle's consult takes
    different positional arguments (predicate_equivalence wants
    ``(pattern, predicate_query, predicate_stored)``;
    entity_taxonomy wants ``(child, parent, relation_type)``;
    predicate_distribution wants a 4-tuple). A Protocol or ABC
    would be performative — the variation is everywhere.
  * No SQL CRUD parameterized over key columns. The four oracles'
    CRUD is small and direct; abstracting it would be net-uglier.
  * No LLM orchestration skeleton. Each oracle calls
    ``llm.extract_with_tool`` with its own system prompt and tool
    schema; wrapping that in a base method buys nothing.
  * No canonical-pair helper. The symmetric-pair oracles each own
    their own ``_canonical_pair`` (with different normalization);
    the directional-pair and singleton-key oracles do not have one.

The contract subclasses follow (informally — enforced by code review,
not by ABC):

  * Subclass takes a ``FactStore`` in its constructor and stores it
    on ``self._store`` (not ``self.store`` — matches RoutingMemo).
  * Subclass exposes ``consult(...)``, ``lookup(...)``, and
    ``list_rows(...)``. ``consult`` does lookup-then-LLM-then-write;
    ``lookup`` is pure read; ``list_rows`` powers the inspector
    endpoint.
  * Subclass emits ``{prefix}_hit`` on cache hit, ``{prefix}_write``
    after LLM-driven UPSERT, and ``{prefix}_classification_failed``
    on malformed LLM output. It also emits the shared
    ``oracle_consulted`` stage with ``{"oracle": "<oracle_name>",
    ...}`` so the trace UI can grep one stage across all four.
  * Subclass NEVER increments ``affirmed_count`` or
    ``contradicted_count`` from a consult path. Those are operator-
    action only, per principle 3.
  * Subclass raises ``_ClassificationFailed`` from
    ``_classify_via_llm`` on any malformed tool response and the
    public ``consult`` catches it to produce a verdict with
    ``classification_failed=True``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.fact_store import FactStore
from src.layer2_routing.constants import confidence_from_counts


__all__ = [
    "_now_iso",
    "_safe_emit_event",
    "_ClassificationFailed",
    "confidence_from_counts",
    "affirm_oracle_row",
    "contradict_oracle_row",
    "ORACLE_TABLES",
]


# ============================================================================
# Operator-action helpers (v0.14 Phase 8e)
# ============================================================================


# Whitelist of substrate oracle tables. The affirm/contradict helpers
# format the table name into the SQL UPDATE, so the whitelist is the
# anti-injection guard. Anything not in here raises ValueError.
ORACLE_TABLES: dict[str, str] = {
    "predicate_equivalence": "predicate_equivalence",
    "entity_equivalence": "entity_equivalence",
    "entity_taxonomy": "entity_taxonomy",
    "predicate_distribution": "predicate_distribution",
}


def affirm_oracle_row(
    store: "FactStore", oracle_name: str, row_id: int,
) -> dict[str, Any]:
    """Increment ``affirmed_count`` by 1 on an oracle row.

    Operator-action only (architecture principle 3): cache hits and
    consultations never invoke this path; only the operator endpoint
    does. Each call is one operator click = one independent external
    evidence event. NOT idempotent: the operator UI is responsible
    for debouncing; programmatic callers should read
    ``affirmed_count`` from the returned dict to confirm their
    increment landed.

    Returns ``{oracle, row_id, affirmed_count, contradicted_count,
    confidence}``. Raises ``ValueError`` on unknown oracle_name and
    ``LookupError`` on missing row.
    """
    return _mutate_count(store, oracle_name, row_id, column="affirmed_count")


def contradict_oracle_row(
    store: "FactStore", oracle_name: str, row_id: int,
) -> dict[str, Any]:
    """Increment ``contradicted_count`` by 1 on an oracle row.

    Mirror of ``affirm_oracle_row`` for the dispute path. Same
    idempotency contract: each call increments by 1; debouncing is
    the operator UI's responsibility.
    """
    return _mutate_count(store, oracle_name, row_id, column="contradicted_count")


def _mutate_count(
    store: "FactStore", oracle_name: str, row_id: int, *, column: str,
) -> dict[str, Any]:
    if oracle_name not in ORACLE_TABLES:
        raise ValueError(
            f"unknown oracle {oracle_name!r}; expected one of "
            f"{sorted(ORACLE_TABLES)}"
        )
    if column not in ("affirmed_count", "contradicted_count"):
        raise ValueError(f"unknown column {column!r}")
    table = ORACLE_TABLES[oracle_name]
    row = store._conn.execute(
        f"SELECT affirmed_count, contradicted_count FROM {table} "
        f"WHERE id = ?",
        (row_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"{oracle_name} row {row_id} does not exist")
    affirmed = int(row["affirmed_count"] or 0)
    contradicted = int(row["contradicted_count"] or 0)
    if column == "affirmed_count":
        affirmed += 1
    else:
        contradicted += 1
    new_confidence = confidence_from_counts(affirmed, contradicted)
    store._conn.execute(
        f"UPDATE {table} SET affirmed_count = ?, contradicted_count = ?, "
        f"last_consulted_at = ? WHERE id = ?",
        (affirmed, contradicted, _now_iso(), row_id),
    )
    store._conn.commit()
    return {
        "oracle": oracle_name,
        "row_id": row_id,
        "affirmed_count": affirmed,
        "contradicted_count": contradicted,
        "confidence": new_confidence,
    }


@dataclass
class _ClassificationFailed(Exception):
    """Internal — signals an oracle's LLM produced malformed tool output.

    Carries enough information for the
    ``{oracle}_classification_failed`` event payload to be useful to
    an operator triaging the failure: the failure ``reason`` (e.g.
    "label 'maybe' not in ('equivalent', ...)") and the ``raw`` tool-
    call dict the LLM returned.

    Shared across all four substrate oracles. Each oracle's
    ``_classify_via_llm`` raises this; each oracle's ``consult``
    catches it and converts to a verdict with
    ``classification_failed=True``.
    """

    reason: str
    raw: dict[str, Any] = field(default_factory=dict)


def _now_iso() -> str:
    """ISO 8601 UTC timestamp. Same shape as fact_store._now_iso and
    routing_memo._now_iso; duplicated here so substrate code doesn't
    cross-import private helpers from sibling modules."""
    return datetime.now(timezone.utc).isoformat()


def _safe_emit_event(
    store: FactStore,
    turn_id: int | None,
    stage: str,
    data: dict[str, Any],
) -> None:
    """Emit a pipeline event, swallowing any exception.

    The v1 router's ``_log`` follows the same discipline:
    observability is best-effort, classification correctness is the
    hard requirement. A logging failure must never crash an oracle
    consultation.

    ``turn_id`` may be None when an oracle is consulted outside a
    turn-driven flow (e.g. from the inspector endpoint). In that case
    the event is silently dropped — the inspector endpoint is its own
    audit trail and doesn't need a pipeline_events row alongside.
    """
    if turn_id is None:
        return
    try:
        store.insert_pipeline_event(turn_id, stage, data)
    except Exception:
        pass
