"""v0.16 WS1 — SLING-style distant-supervision fallback (SlingFallback).

For a predicate whose Wikidata property ontology can't constrain it (no P2302
constraints — common for long-tail edges), `SlingFallback.propose_bindings`
samples entity pairs the oracle's primary property links, enumerates the KB
properties that co-occur on those entities (via `enumerate_neighbors`), and
proposes the most-frequent co-occurring property as a candidate
`PredicateBinding`.

A single binding, `source='sling'`, low `rank` — SLING bindings are the
weakest candidates: they can drive VERIFIED (lowest priority) but, per the
soundness contract, never CONTRADICTED.

FAIL-OPEN: any KB/LLM error, a missing primary property, or no co-occurring
signal returns `[]`. Soundness over coverage — a SLING miss simply leaves the
oracle's primary binding in place (current behavior). Nothing here raises.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Optional

from ..audit.log import log_event
from .predicate_translation import PredicateBinding

# SLING candidates rank below ontology-typed (1.0) and oracle-primary bindings.
# Low enough that any positively-grounding ontology/oracle binding wins, but
# still > 0 so the binding participates in the verify loop.
_SLING_RANK = 0.1

# Cap on how many co-occurring properties we consider, and how many sample
# entities we enumerate — distant supervision is a cheap heuristic, not an
# exhaustive crawl.
_MAX_SAMPLE_ENTITIES = 3
_TOP_PROPERTY_LIMIT = 1


class SlingFallback:
    """Distant-supervision binding proposer for predicates the property
    ontology can't constrain. Reuses `kb_protocol.enumerate_neighbors`
    (already on the protocol) — no new KBProtocol method needed."""

    def __init__(
        self,
        db: sqlite3.Connection,
        kb_protocol,
        llm_client,
    ) -> None:
        self._db = db
        self._kb = kb_protocol
        self._llm = llm_client

    def propose_bindings(
        self, predicate: str, oracle_raw: dict
    ) -> list[PredicateBinding]:
        """Propose candidate bindings via distant supervision. Returns a list
        (at most one binding in v0.16) or `[]` on any failure / no signal."""
        try:
            return self._propose(predicate, oracle_raw)
        except Exception:
            # Belt-and-suspenders: the inner method already fails open per
            # step, but distant supervision must NEVER break discovery.
            return []

    def _propose(
        self, predicate: str, oracle_raw: dict
    ) -> list[PredicateBinding]:
        if not isinstance(oracle_raw, dict):
            return []
        primary_prop = oracle_raw.get("kb_property")
        if not primary_prop or not isinstance(primary_prop, str):
            return []
        kb_namespace = oracle_raw.get("kb_namespace") or "wikidata"

        sample_entities = self._sample_entities(oracle_raw)
        if not sample_entities:
            return []

        enumerate_fn = getattr(self._kb, "enumerate_neighbors", None)
        if enumerate_fn is None:
            return []

        # Count properties that co-occur on the sampled subject entities,
        # excluding the oracle's primary property itself.
        cooccurring: Counter = Counter()
        for entity in sample_entities[:_MAX_SAMPLE_ENTITIES]:
            try:
                neighbors = enumerate_fn(entity, [])
            except Exception:
                continue
            if not isinstance(neighbors, dict):
                continue
            for prop, values in neighbors.items():
                if prop == primary_prop:
                    continue
                if values:
                    cooccurring[prop] += len(values)

        if not cooccurring:
            return []

        candidate_prop, _count = cooccurring.most_common(_TOP_PROPERTY_LIMIT)[0]
        binding = PredicateBinding(
            kb_namespace=kb_namespace,
            kb_property=candidate_prop,
            slot_to_qualifier=oracle_raw.get("slot_to_qualifier"),
            single_valued=False,  # SLING never licenses a contradiction
            subject_entity_types=oracle_raw.get("subject_entity_types"),
            object_entity_types=oracle_raw.get("object_entity_types"),
            source="sling",
            rank=_SLING_RANK,
        )
        self._cache_edge(predicate, kb_namespace, primary_prop, candidate_prop)
        self._log(
            predicate,
            {
                "primary_property": primary_prop,
                "candidate_property": candidate_prop,
                "sample_entity_count": len(sample_entities),
                "cooccurrence_count": _count,
            },
        )
        return [binding]

    def _sample_entities(self, oracle_raw: dict) -> list[str]:
        """Gather sample subject entities for distant supervision. v0.16 uses
        any entity Q-ids the oracle already surfaced (e.g. example subjects);
        absent those, returns [] (no sampling → no SLING binding). This keeps
        SLING zero-cost on the common path and avoids an unbounded crawl."""
        sample = oracle_raw.get("sample_subject_qids") or oracle_raw.get("example_qids")
        if isinstance(sample, list):
            return [q for q in sample if isinstance(q, str) and q.startswith("Q")]
        return []

    def _cache_edge(
        self,
        predicate: str,
        kb_namespace: str,
        primary_prop: str,
        candidate_prop: str,
    ) -> None:
        """Cache the discovered co-occurrence into `property_relations` with
        source='sling' (a 'related' edge from the primary to the candidate)
        so a later discovery can reuse it. Best-effort; never raises."""
        try:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            self._db.execute(
                "INSERT OR IGNORE INTO property_relations "
                "(kb_namespace, kb_property, relation_type, related_value, "
                " source, created_at, used_count) "
                "VALUES (?, ?, 'related', ?, 'sling', ?, 0)",
                (kb_namespace, primary_prop, candidate_prop, now),
            )
            self._db.commit()
        except Exception:
            pass

    def _log(self, event_subject: str, event_data: dict) -> None:
        try:
            log_event(
                self._db,
                event_type="sling_fallback_proposed",
                event_subject=event_subject,
                event_data=event_data,
            )
        except Exception:
            pass
