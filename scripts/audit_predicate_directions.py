"""v0.16.3 Batch B (piece 4) — predicate-direction audit instrument.

Walks every NON-SEEDED, KB-routed, entity-object predicate_translation row and
runs the same empirical DirectionValidator the generation path uses, bucketing
each row into:

  - direction-correct  : the stored slot_to_qualifier grounds (confirmed/symmetric)
                         → leave it.
  - direction-fixable  : the OTHER direction is the grounding one (corrected)
                         → flag for a reviewed correction.
  - property-suspect   : the example grounds under NEITHER keying (likely the
                         wrong KB property) → flag for human review.
  - inconclusive       : could not probe (no example sourced / cannot orient /
                         KB error) → flag; re-run later.

REPORT-ONLY. This script NEVER modifies any predicate_translation row — row
cleanup is a separate, triaged task. Requires RUN_LIVE_KB=1 for real probing
(against fixtures the validator is inert and everything reports 'inconclusive').

Usage:
    RUN_LIVE_KB=1 python scripts/audit_predicate_directions.py [--db PATH]
                          [--include-seeded] [--json OUT.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from aedos.config import Config  # noqa: E402
from aedos.database import open_db  # noqa: E402
from aedos.layer3_substrate.direction_validator import DirectionValidator  # noqa: E402
from aedos.layer3_substrate.property_relations import PropertyRelations  # noqa: E402
from aedos.llm.client import LLMClient  # noqa: E402
from aedos.pipeline import build_default_kb  # noqa: E402
from aedos.seed_loader import DEFAULT_SEED_FILE  # noqa: E402

_BUCKET_BY_STATUS = {
    "confirmed": "direction-correct",
    "symmetric": "direction-correct",
    "corrected": "direction-fixable",
    "suspect": "property-suspect",
    "unconfirmed": "inconclusive",
}


def _seed_predicates() -> set[str]:
    data = json.loads(Path(DEFAULT_SEED_FILE).read_text(encoding="utf-8"))
    return {e["aedos_predicate"] for e in data}


def _parse_types(raw):
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return v if isinstance(v, list) else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit predicate-translation directions")
    ap.add_argument("--db", default=os.environ.get("AEDOS_DB_PATH", "data/aedos.db"))
    ap.add_argument("--include-seeded", action="store_true",
                    help="also probe seeded rows (default: non-seeded only)")
    ap.add_argument("--json", default=None, help="write the full report JSON here")
    args = ap.parse_args()

    if os.environ.get("RUN_LIVE_KB") != "1":
        print("WARNING: RUN_LIVE_KB != 1 — the validator is inert against fixtures; "
              "every row will report 'inconclusive'. Set RUN_LIVE_KB=1 for a real audit.\n")

    config = Config.from_env()
    db = open_db(args.db)
    client = LLMClient()
    kb = build_default_kb(db, client, config)
    validator = DirectionValidator(kb=kb, property_relations=PropertyRelations(db, kb))

    seeds = _seed_predicates()
    rows = db.execute(
        "SELECT id, aedos_predicate, kb_namespace, kb_property, slot_to_qualifier, "
        "subject_entity_types, object_entity_types, pinned "
        "FROM predicate_translation "
        "WHERE routing_hint='kb_resolvable' AND kb_property IS NOT NULL "
        "AND object_type='entity' AND retracted_at IS NULL "
        "ORDER BY aedos_predicate"
    ).fetchall()

    buckets: dict[str, list] = {
        "direction-correct": [], "direction-fixable": [],
        "property-suspect": [], "inconclusive": [],
    }
    probed = 0
    for r in rows:
        pred = r["aedos_predicate"]
        is_seed = pred in seeds
        if is_seed and not args.include_seeded:
            continue
        try:
            sq = json.loads(r["slot_to_qualifier"]) if r["slot_to_qualifier"] else None
        except (json.JSONDecodeError, TypeError):
            sq = None
        verdict = validator.validate(
            r["kb_property"], r["kb_namespace"], sq,
            _parse_types(r["subject_entity_types"]),
            _parse_types(r["object_entity_types"]),
        )
        probed += 1
        bucket = _BUCKET_BY_STATUS.get(verdict.status, "inconclusive")
        buckets[bucket].append({
            "predicate": pred,
            "kb_property": r["kb_property"],
            "stored_direction": sq,
            "corrected_direction": verdict.direction if verdict.status == "corrected" else None,
            "status": verdict.status,
            "reason": verdict.reason,
            "grounded": verdict.grounded,
            "seeded": is_seed,
            "pinned": bool(r["pinned"]),
        })

    db.close()

    print(f"=== Predicate-direction audit: {args.db} ===")
    print(f"probed {probed} {'(incl. seeded)' if args.include_seeded else 'non-seeded'} "
          f"KB-routed entity-object rows\n")
    for bucket in ("direction-fixable", "property-suspect", "inconclusive", "direction-correct"):
        items = buckets[bucket]
        print(f"[{bucket}] {len(items)}")
        for it in items:
            extra = ""
            if it["corrected_direction"]:
                extra = f"  -> suggest {json.dumps(it['corrected_direction'])}"
            pin = " PINNED" if it["pinned"] else ""
            print(f"    - {it['predicate']} ({it['kb_property']}){pin}: "
                  f"{it['status']} [{it['reason']}]{extra}")
        print()

    report = {"db": args.db, "probed": probed, "buckets": buckets}
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
