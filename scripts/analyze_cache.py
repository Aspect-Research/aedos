"""Verification-cache hit-rate analysis.

Walks a DB's pipeline_events for cache_lookup and cache_write rows
and produces a report:

  total lookups, hits, misses, errors
  hit rate
  by-stability-class hit rate
  most-reused canonical_keys (highest hit_count)
  total writes (and write errors)

Usage:
    python scripts/analyze_cache.py path/to/aedos.db
    python scripts/analyze_cache.py path/to/aedos.db --top 20

Pure read-only; doesn't mutate the DB.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("db_path")
    parser.add_argument("--top", type=int, default=10,
                        help="Show top N most-reused canonical keys")
    args = parser.parse_args(argv[1:])

    from src.fact_store import FactStore

    db = Path(args.db_path)
    if not db.exists():
        print(f"ERROR: db not found: {db}", file=sys.stderr)
        return 2
    store = FactStore(str(db))

    lookup_rows = store._conn.execute(
        "SELECT turn_id, data FROM pipeline_events "
        "WHERE stage = 'cache_lookup' ORDER BY turn_id"
    ).fetchall()
    write_rows = store._conn.execute(
        "SELECT turn_id, data FROM pipeline_events "
        "WHERE stage = 'cache_write' ORDER BY turn_id"
    ).fetchall()

    if not lookup_rows and not write_rows:
        print(f"no cache events in {db}")
        print("(was AEDOS_CACHE_SCOPING / AEDOS_CACHE_STABILITY / "
              "AEDOS_CACHE_WRITES enabled during the run?)")
        store.close()
        return 0

    hits = 0
    misses = 0
    errors = 0
    by_stability: dict[str, dict[str, int]] = defaultdict(
        lambda: {"hits": 0, "misses": 0})
    # Track the highest-hit-count seen per canonical key (the cache-stored
    # counter; a key reused 5 times in this DB will appear 5 times in the
    # event log with hit_count incrementing each time).
    key_hit_count: Counter = Counter()
    key_to_stability: dict[str, str] = {}

    for r in lookup_rows:
        try:
            data = json.loads(r["data"])
        except (TypeError, ValueError):
            continue
        if data.get("error"):
            errors += 1
            continue
        result = data.get("result")
        key = data.get("canonical_key", "")
        if result == "hit":
            hits += 1
            stab = data.get("stability_class", "unknown")
            by_stability[stab]["hits"] += 1
            hc = data.get("hit_count", 0)
            if hc > key_hit_count[key]:
                key_hit_count[key] = hc
            key_to_stability.setdefault(key, stab)
        elif result == "miss":
            misses += 1
            # Stability class isn't on the miss event (we only know it
            # post-write). Bucket as "miss-only".
            by_stability["(miss-only)"]["misses"] += 1

    write_ok = 0
    write_err = 0
    write_by_stability: Counter = Counter()
    for r in write_rows:
        try:
            data = json.loads(r["data"])
        except (TypeError, ValueError):
            continue
        if data.get("error"):
            write_err += 1
        else:
            write_ok += 1
            stab = data.get("stability_class", "unknown")
            write_by_stability[stab] += 1

    total_lookups = hits + misses
    print(f"=== cache analysis: {db} ===\n")
    print(f"  total cache lookups:   {total_lookups}")
    print(f"    hits:                {hits}")
    print(f"    misses:              {misses}")
    print(f"    errors:              {errors}")
    if total_lookups:
        print(f"  hit rate:              {hits / total_lookups:.1%}")
    print()
    print(f"  total cache writes:    {write_ok + write_err}")
    print(f"    successful:          {write_ok}")
    print(f"    errors:              {write_err}")
    print()

    if by_stability:
        print(f"  hit/miss by stability class (hits include first-time hits "
              f"only — misses bucket as '(miss-only)' since the stability "
              f"class is unknown until after a write):")
        for stab, counts in sorted(by_stability.items()):
            h, m = counts["hits"], counts["misses"]
            tot = h + m
            rate = f"{h / tot:.1%}" if tot else "n/a"
            print(f"    {stab:20s}  hits={h:4d}  misses={m:4d}  rate={rate}")
        print()

    if write_by_stability:
        print(f"  writes by stability class:")
        for stab, n in write_by_stability.most_common():
            print(f"    {stab:20s}  {n}")
        print()

    if key_hit_count:
        print(f"  top {args.top} most-reused canonical keys "
              f"(by max observed hit_count):")
        for key, hc in key_hit_count.most_common(args.top):
            stab = key_to_stability.get(key, "?")
            print(f"    [{stab}] hit_count={hc}: {key}")
        print()
    elif hits:
        # We had hits but no key_hit_count entries — means events lacked
        # the hit_count field (older schema). Don't claim "no reuse"
        # falsely.
        print("  (per-key hit counts unavailable — events missing "
              "hit_count field; upgrade the writer or run a fresh session)")
        print()

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
