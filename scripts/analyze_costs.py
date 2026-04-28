"""Cost analysis across a session or corpus run.

Walks a DB's pipeline_events for turn_cost rows and produces a
report:

  total cost across all turns
  cost per model (which model is the budget sink?)
  most-expensive turns (which prompts blow the budget?)
  average cost per turn
  call-count distribution (extractor + corrector + judge per turn)

Usage:
    python scripts/analyze_costs.py path/to/aedos.db
    python scripts/analyze_costs.py path/to/aedos.db --top 20

Pure read-only; doesn't mutate the DB.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
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
                        help="Show top N most-expensive turns")
    args = parser.parse_args(argv[1:])

    from src.fact_store import FactStore

    db = Path(args.db_path)
    if not db.exists():
        print(f"ERROR: db not found: {db}", file=sys.stderr)
        return 2
    store = FactStore(str(db))

    rows = store._conn.execute(
        "SELECT turn_id, data FROM pipeline_events WHERE stage = 'turn_cost' "
        "ORDER BY turn_id"
    ).fetchall()
    if not rows:
        print(f"no turn_cost events in {db} — was AEDOS_CACHE_SCOPING enabled?")
        print("(turn_cost events require LLMClient cost recording, which "
              "happens for any LLMClient call but only emits as an event "
              "when calls were recorded that turn)")
        store.close()
        return 0

    turn_costs: list[dict] = []
    for r in rows:
        try:
            data = json.loads(r["data"])
        except (TypeError, ValueError):
            continue
        turn_costs.append({"turn_id": r["turn_id"], **data})

    total_usd = sum(t.get("total_usd", 0) for t in turn_costs)
    total_calls = sum(t.get("total_calls", 0) for t in turn_costs)
    total_in = sum(t.get("total_input_tokens", 0) for t in turn_costs)
    total_out = sum(t.get("total_output_tokens", 0) for t in turn_costs)

    # By model.
    model_usd: Counter = Counter()
    model_calls: Counter = Counter()
    for t in turn_costs:
        for model, slot in (t.get("by_model") or {}).items():
            model_usd[model] += slot.get("total_usd", 0)
            model_calls[model] += slot.get("calls", 0)

    print(f"=== cost analysis: {db} ===\n")
    print(f"  turns with cost data: {len(turn_costs)}")
    print(f"  total cost:           ${total_usd:.4f}")
    if turn_costs:
        print(f"  avg cost / turn:      ${total_usd / len(turn_costs):.4f}")
    print(f"  total LLM calls:      {total_calls}")
    if total_calls:
        print(f"  avg cost / call:      ${total_usd / total_calls:.4f}")
        print(f"  avg calls / turn:     {total_calls / len(turn_costs):.1f}")
    print(f"  total input tokens:   {total_in:,}")
    print(f"  total output tokens:  {total_out:,}\n")

    if model_usd:
        print(f"  by model:")
        for model, usd in model_usd.most_common():
            calls = model_calls[model]
            print(f"    {model}: ${usd:.4f} ({calls} calls)")
        print()

    if turn_costs:
        # Top N most-expensive turns.
        print(f"  top {args.top} most-expensive turns:")
        sorted_turns = sorted(turn_costs, key=lambda t: -t.get("total_usd", 0))
        for t in sorted_turns[: args.top]:
            tid = t["turn_id"]
            usd = t.get("total_usd", 0)
            calls = t.get("total_calls", 0)
            # Find the user message that produced this turn.
            user_row = store._conn.execute(
                "SELECT content FROM turns WHERE id = ? - 1", (tid,)
            ).fetchone()
            user_msg = (user_row["content"] if user_row else "?")[:80]
            print(f"    turn {tid}: ${usd:.4f} ({calls} calls) — {user_msg!r}")
        print()

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
