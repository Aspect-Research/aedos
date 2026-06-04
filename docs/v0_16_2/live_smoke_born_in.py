"""v0.16.2 LIVE smoke for the functional_entity_predicate enumeration-skip fix.

Fully live against Wikidata; NO LLM (keyless tripwire client → any LLM call
raises, proving the walk is pure-KB). Run with RUN_LIVE_KB=1 (set inside).

Proves, end-to-end against the real walker + KBVerifier + live WikidataAdapter:
  - "Obama born_in Kenya" no longer fans out over Kenya's P17 children (the live
    bug). We measure kb_neighbor_enumeration trace edges + wall time.
  - The A/B control: same claim with the new signal FORCED OFF re-creates the
    fanout — proving the *fix* is what prevents it (not a confound).
  - Non-regression: "Barack Obama born_in United States" still VERIFIES (the
    directed Honolulu ⊆ USA upgrade), and a non-functional predicate
    ("Williams College located_in United States") is NOT over-skipped.
"""
from __future__ import annotations

import os
os.environ["RUN_LIVE_KB"] = "1"

import json
import time
import tempfile
import sqlite3
from datetime import datetime, timezone

from aedos.config import Config
from aedos.database import open_memory_db
from aedos.pipeline import build_pipeline
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer4_sources.walker import VerificationContext


class _TripwireLLM:
    """Stands in for LLMClient. Any actual call raises — so if the walk needs
    the LLM, the smoke fails loudly instead of silently going non-live."""

    def __getattr__(self, name):
        def _boom(*a, **k):
            raise RuntimeError(f"LLM call '{name}' attempted — smoke must be pure-KB")
        return _boom


def _seed_distribution(conn, predicate, verdict="distributes_down"):
    """Seed predicate_distribution for both relations with a NON-neither verdict,
    so a discovery skip can only come from the functional_entity_predicate signal,
    never from the `neither` short-circuit. Polarity 0 and 1 both seeded."""
    now = "2026-01-01T00:00:00+00:00"
    for polarity in (0, 1):
        for relation_type in ("is_a", "part_of"):
            conn.execute(
                """INSERT OR REPLACE INTO predicate_distribution
                   (aedos_predicate, polarity, relation_type, verdict, reason, created_at)
                   VALUES (?, ?, ?, ?, 'live_smoke_seed', ?)""",
                (predicate, polarity, relation_type, verdict, now),
            )
    conn.commit()


def _build():
    db = open_memory_db(load_seeds=True)  # sets row_factory=sqlite3.Row + loads seeds
    _seed_distribution(db, "born_in")
    _seed_distribution(db, "located_in")
    config = Config()
    # Disable LLM-bearing paths so the walk is pure-KB.
    for attr, val in (
        ("wikipedia_normalizer_enabled", False),
        ("enable_sling", False),
        ("walker_wall_clock_seconds", 45),
    ):
        try:
            object.__setattr__(config, attr, val)
        except Exception:
            pass
    pipe = build_pipeline(db, llm_client=_TripwireLLM(), config=config)
    return pipe, db


def _claim(subject, predicate, obj):
    return Claim(
        claim_id="c", subject=subject, predicate=predicate, object=obj,
        polarity=1, source_text=f"{subject} {predicate} {obj}",
        asserting_party="live_smoke", triage_decision=TriageDecision.VERIFY,
    )


def _ctx():
    return VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(),
        asserting_party="live_smoke",
    )


def _run(pipe, claim, label):
    t0 = time.monotonic()
    try:
        result = pipe.walker.walk(claim, _ctx())
        elapsed = time.monotonic() - t0
        edges = result.trace.edges
        enum_edges = [e for e in edges if e.edge_type == "kb_neighbor_enumeration"]
        by_rel = {}
        for e in enum_edges:
            rel = e.metadata.get("relation_type")
            by_rel[rel] = by_rel.get(rel, 0) + 1
        return {
            "label": label,
            "verdict": result.verdict,
            "elapsed_s": round(elapsed, 2),
            "enum_edges": len(enum_edges),
            "enum_by_relation": by_rel,
            "abstention_reason": result.trace.walk_metadata.get("abstention_reason")
                or result.trace.walk_metadata.get("budget_exceeded"),
            "kb_sources": result.trace.source_breakdown.get("kb", 0),
            "depth_reached": result.trace.walk_metadata.get("depth_reached"),
        }
    except Exception as exc:
        return {"label": label, "ERROR": f"{type(exc).__name__}: {exc}"}


def main():
    pipe, db = _build()
    results = []

    # Case 1 — THE FIX: "Obama born_in Kenya". Whatever "Obama" resolves to
    # (the Japanese city Q41773 with no P19, or Q76 with a known P19), the walk
    # must NOT fan out over Kenya's P17 children.
    results.append(_run(pipe, _claim("Obama", "born_in", "Kenya"), "1. Obama born_in Kenya (FIX)"))

    # Case 2 — A/B CONTROL: force the new signal OFF and re-run the SAME claim.
    # Expect the fanout to RE-APPEAR (many P17 enum edges), proving the fix is
    # the cause of Case 1's clean result.
    from aedos.layer4_sources.walker import Walker
    orig = Walker._functional_entity_predicate
    # getter forced False + no-op setter (the reset in _try_external_grounding
    # still assigns self._functional_entity_predicate = False).
    Walker._functional_entity_predicate = property(
        lambda self: False, lambda self, v: None
    )
    try:
        results.append(_run(pipe, _claim("Obama", "born_in", "Kenya"),
                            "2. Obama born_in Kenya (signal FORCED OFF = pre-fix)"))
    finally:
        Walker._functional_entity_predicate = orig

    # Case 3 — NON-REGRESSION verify: directed containment still grounds.
    results.append(_run(pipe, _claim("Barack Obama", "born_in", "United States"),
                        "3. Barack Obama born_in United States (expect VERIFIED)"))

    # Case 4 — the statements-found path: correct subject, wrong country → fast
    # CONTRADICTED at the direct lookup (no discovery, no fanout).
    results.append(_run(pipe, _claim("Barack Obama", "born_in", "Kenya"),
                        "4. Barack Obama born_in Kenya (expect CONTRADICTED, no fanout)"))

    # Case 5 — non-functional control: located_in is single_valued=0, so
    # functional_entity_predicate is False → enumeration is NOT skipped by the
    # new signal (proves the fix is not over-broad).
    results.append(_run(pipe, _claim("Williams College", "located_in", "United States"),
                        "5. Williams College located_in US (non-functional control)"))

    print(json.dumps(results, indent=2))
    db.close()


if __name__ == "__main__":
    main()
