"""Phase-2 dogfooding harness against GLM-5.1-FP8.

Sends a curated prompt set through the full AEDOS pipeline and dumps
per-turn diagnostics + a summary table. The smoke test (only 3 prompts,
all python-territory, all correct) didn't exercise retrieval, mixed
claims, user-authoritative recall, or confabulation-prone factoids.
This harness does.

Each prompt entry carries a tag that records:
  * `category`     — which router branch this should exercise
  * `expected`     — the operator's prediction of the correct answer (or
                     "unknown" / "may not exist")
  * `notes`        — what to watch for in the trace

Output:
  * diagnostic_output/dogfood_<n>_<slug>.json — full pipeline_events for
    each turn, plus the trace and operator metadata
  * stdout summary table

Designed to run unattended. Single conversation across all turns so
user-authoritative recall actually exercises store reads. Sleeps between
turns to avoid Modal's per-model concurrency limit.

Usage:
    python scripts/dogfood_glm.py
    python scripts/dogfood_glm.py --start 8     # resume from turn 8
    python scripts/dogfood_glm.py --only 14     # run just turn 14

Exit code 0 = every turn produced a non-empty draft and a logged
chat_model_call event. Non-zero = something broke; check the JSON dumps.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()


PROMPTS: list[dict[str, Any]] = [
    # ---- python territory: counts ----
    {
        "id": "count_m_in_commitment",
        "category": "python:count",
        "prompt": "How many m's are in the word 'commitment'?",
        "expected": "3 (c-o-m-m-i-t-m-e-n-t)",
        "notes": "operator originally wrote 'expected: 2' here — incorrect. GLM got 3, verifier confirmed 3. Recording the correction so the next reader doesn't repeat the mistake.",
    },
    {
        "id": "count_vowels_serendipitous",
        "category": "python:count",
        "prompt": "How many vowels are in 'serendipitous'?",
        "expected": "6 (e,e,i,i,o,u)",
        "notes": "router → python; verifier counts y as not a vowel by default unless asked.",
    },
    # ---- python territory: arithmetic / date ----
    {
        "id": "mult_13_17",
        "category": "python:arith",
        "prompt": "What is 13 times 17?",
        "expected": "221",
        "notes": "trivial arithmetic. should route python with high confidence.",
    },
    {
        "id": "sqrt_sum",
        "category": "python:arith",
        "prompt": "What is the square root of 144 plus the square root of 169?",
        "expected": "12 + 13 = 25",
        "notes": "two-step arithmetic, all in the claim. should route python.",
    },
    {
        "id": "date_diff_moon",
        "category": "python:date",
        "prompt": "How many days are there between July 20, 1969 (the moon landing) and April 27, 2026?",
        "expected": "20,735 days (give or take 1)",
        "notes": "date math on literal dates. router should pick python.",
    },
    # ---- python_with_canonical_constants ----
    {
        "id": "ne_states",
        "category": "python_canonical",
        "prompt": "Please list the six New England states.",
        "expected": "Maine, New Hampshire, Vermont, Massachusetts, Rhode Island, Connecticut",
        "notes": "should route python_with_canonical_constants; cross-check at temp 0/0.3 should agree.",
    },
    {
        "id": "days_of_week",
        "category": "python_canonical",
        "prompt": "Name the seven days of the week in order, starting with Monday.",
        "expected": "Mon, Tue, Wed, Thu, Fri, Sat, Sun",
        "notes": "canonical reference; cross-check should agree.",
    },
    # ---- retrieval territory ----
    {
        "id": "dali_persistence_memory",
        "category": "retrieval:art",
        "prompt": "Who painted The Persistence of Memory?",
        "expected": "Salvador Dalí",
        "notes": "famous fact; should retrieve and verify.",
    },
    {
        "id": "suriname_language",
        "category": "retrieval:geo",
        "prompt": "What is the official language of Suriname?",
        "expected": "Dutch",
        "notes": "less-famous fact; tests retrieval on a non-obvious answer.",
    },
    {
        "id": "cloudflare_founders",
        "category": "retrieval:tech",
        "prompt": "Who founded the company Cloudflare?",
        "expected": "Matthew Prince, Lee Holloway, Michelle Zatlyn",
        "notes": "multi-person attribution; retrieval may return partial names.",
    },
    {
        "id": "tokyo_old_name",
        "category": "retrieval:history",
        "prompt": "What was Tokyo called before it was renamed in 1868?",
        "expected": "Edo",
        "notes": "well-documented but specific; tests retrieval verification.",
    },
    # ---- mixed claims ----
    {
        "id": "marie_curie_lived",
        "category": "mixed",
        "prompt": "When was Marie Curie born, when did she die, and how long did she live?",
        "expected": "born 1867, died 1934, lived 66 (or 67) years",
        "notes": "extractor should make 3 claims: born_in_year (retrieval), died_in_year (retrieval), lived_for_years (python on dates). Multi-claim convention from CLAUDE.md.",
    },
    # ---- user-authoritative ----
    {
        "id": "user_pref_set",
        "category": "user_auth:set",
        "prompt": "I prefer my coffee black with no sugar.",
        "expected": "no factual claim from assistant; user-fact stored",
        "notes": "user-side extraction should record preference. Assistant ack should generate few/no model claims.",
    },
    {
        "id": "user_pref_recall",
        "category": "user_auth:recall",
        "prompt": "What did I tell you about my coffee preference?",
        "expected": "the assistant should recall 'black, no sugar' from store",
        "notes": "tests that user_authoritative routing finds the prior fact. If model invents a different preference it should be contradicted.",
    },
    {
        "id": "user_pref_recall_alt",
        "category": "user_auth:recall",
        "prompt": "Remind me — do I take sugar in my coffee?",
        "expected": "no (per the prior turn)",
        "notes": "negation form. Verifier must check polarity.",
    },
    # ---- confabulation-prone ----
    {
        "id": "fake_book",
        "category": "confab",
        "prompt": "Who wrote the 1934 novel 'The Whispering Wall'?",
        "expected": "no such novel exists; assistant should refuse or retrieval should be inconclusive",
        "notes": "tests model's susceptibility to confabulation on plausible-sounding fake things.",
    },
    {
        "id": "belgium_stamps",
        "category": "retrieval:obscure",
        "prompt": "When did Belgium first issue postage stamps?",
        "expected": "1849",
        "notes": "obscure but verifiable. tests whether DDG returns enough signal for the judge.",
    },
]


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")[:40]


def _diagnostic_dir() -> Path:
    d = Path(__file__).resolve().parent.parent / "diagnostic_output"
    d.mkdir(exist_ok=True)
    return d


def _serialize_events(events):
    out = []
    for e in events:
        row = dict(e)
        if isinstance(row.get("data"), str):
            try:
                row["data"] = json.loads(row["data"])
            except (TypeError, ValueError):
                pass
        out.append(row)
    return out


def _summarize_turn(trace, events) -> dict[str, Any]:
    chat_events = [e for e in events if e["stage"] == "chat_model_call"]
    routing_events = [e for e in events if e["stage"] == "routing_decision"]
    routings = [
        {
            "method": e["data"]["decision"]["method"],
            "confidence": e["data"]["decision"]["confidence"],
            "reason": e["data"]["decision"]["reason"][:120],
        }
        for e in routing_events
    ]
    verdicts = [
        d.get("verification_status", "?") for d in trace.verification_decisions
    ]
    return {
        "chat_ok": bool(chat_events) and chat_events[0]["data"].get("error") is None,
        "chat_duration_ms": chat_events[0]["data"]["duration_ms"] if chat_events else None,
        "valid_facts": len(trace.assistant_extraction.get("valid_facts", [])),
        "rejected_facts": len(trace.assistant_extraction.get("rejected_facts", [])),
        "routings": routings,
        "verdicts": verdicts,
        "interventions": len(trace.interventions),
        "final_content_first_240": trace.final_content[:240],
    }


def main(argv: list[str]) -> int:
    # Long-running script — flush every print so the log file (or
    # operator's terminal) shows progress in real time.
    import functools
    print_orig = print
    builtins_print = functools.partial(print_orig, flush=True)
    globals()["print"] = builtins_print

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--only", type=int, default=None)
    parser.add_argument("--inter-turn-sleep", type=float, default=8.0)
    parser.add_argument(
        "--provider", choices=["modal", "anthropic"], default="modal",
        help="chat backend to test (default modal). 'anthropic' is the "
             "fallback per MISSION.md when Modal is unreachable.",
    )
    parser.add_argument(
        "--output-prefix", default=None,
        help="filename prefix for per-turn dumps. Defaults to 'dogfood' "
             "for modal, 'dogfood_anthropic' for anthropic, so the two "
             "runs don't overwrite each other.",
    )
    args = parser.parse_args(argv[1:])

    os.environ["AEDOS_CHAT_MODEL_PROVIDER"] = args.provider
    if args.provider == "modal" and not os.getenv("MODAL_API_KEY"):
        print("ERROR: MODAL_API_KEY must be set for --provider=modal",
              file=sys.stderr)
        return 2
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY must be set (infra LLMs always "
              "use Anthropic regardless of chat provider)", file=sys.stderr)
        return 2

    out_prefix = args.output_prefix or (
        "dogfood_anthropic" if args.provider == "anthropic" else "dogfood"
    )

    from src.pipeline import build_pipeline

    diag = _diagnostic_dir()
    db_path = Path(tempfile.mkdtemp(prefix="aedos_dogfood_")) / "dogfood.db"
    print(f"using ephemeral DB at {db_path}")
    pipeline = build_pipeline(str(db_path))
    print(f"chat backend: {type(pipeline.chat_backend).__name__} "
          f"(model={getattr(pipeline.chat_backend, 'model', '?')})")

    summaries: list[dict[str, Any]] = []
    overall_ok = True
    last_was_error = False

    for i, entry in enumerate(PROMPTS, start=1):
        if i < args.start:
            continue
        if args.only is not None and i != args.only:
            continue

        if i > args.start:
            # After a pipeline error (typically a Modal timeout that holds
            # the concurrency slot), the next call is much more likely to
            # 429 unless we wait. Backoff long enough that the slot has
            # almost certainly released. The Modal client now retries 429
            # internally too, but this avoids burning retry budget on the
            # first call.
            sleep_s = 90.0 if last_was_error else args.inter_turn_sleep
            print(f"  (sleeping {sleep_s:.0f}s before next turn...)")
            time.sleep(sleep_s)

        slug = entry["id"]
        prompt = entry["prompt"]
        print(f"\n=== turn {i}/{len(PROMPTS)} [{entry['category']}] {slug} ===")
        print(f"  prompt: {prompt}")
        print(f"  expected: {entry['expected']}")

        started = time.monotonic()
        try:
            trace = pipeline.run_turn(prompt)
            last_was_error = False
        except Exception as exc:  # noqa: BLE001
            print(f"  PIPELINE ERROR: {type(exc).__name__}: {exc}")
            overall_ok = False
            last_was_error = True
            summary = {
                "id": slug, "category": entry["category"], "prompt": prompt,
                "expected": entry["expected"], "notes": entry["notes"],
                "error": f"{type(exc).__name__}: {exc}",
            }
            summaries.append(summary)
            out_file = diag / f"{out_prefix}_{i:02d}_{_slugify(slug)}.json"
            with out_file.open("w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2)
            continue

        elapsed = time.monotonic() - started
        events = pipeline.store.get_pipeline_events(trace.assistant_turn_id)
        summary = _summarize_turn(trace, events)
        summary.update(
            {"id": slug, "category": entry["category"], "prompt": prompt,
             "expected": entry["expected"], "notes": entry["notes"],
             "wall_duration_s": round(elapsed, 2)}
        )
        summaries.append(summary)

        if not summary["chat_ok"]:
            overall_ok = False

        print(f"  duration={elapsed:.1f}s "
              f"chat_ok={summary['chat_ok']} "
              f"facts={summary['valid_facts']} "
              f"verdicts={summary['verdicts']} "
              f"interventions={summary['interventions']}")
        print(f"  final: {summary['final_content_first_240']!r}")
        for r in summary["routings"]:
            print(f"  routed: method={r['method']} conf={r['confidence']:.2f}")

        out_file = diag / f"{out_prefix}_{i:02d}_{_slugify(slug)}.json"
        with out_file.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "summary": summary,
                    "trace": trace.to_dict(),
                    "events": _serialize_events(events),
                },
                f, indent=2, default=str,
            )
        print(f"  written: {out_file}")

    pipeline.store.close()

    # ---- summary table ---------------------------------------------------
    print("\n\n=== summary ===")
    print(f"{'#':>3} {'cat':18} {'id':32} {'verdicts':30} {'fac':>4} {'int':>4}")
    for i, s in enumerate(summaries, start=args.start):
        verdicts = ",".join(s.get("verdicts", [])) or "-"
        if "error" in s:
            line = f"{i:>3} {s['category'][:18]:18} {s['id'][:32]:32} ERROR: {s['error'][:60]}"
        else:
            line = (f"{i:>3} {s['category'][:18]:18} {s['id'][:32]:32} "
                    f"{verdicts[:30]:30} {s['valid_facts']:>4} "
                    f"{s['interventions']:>4}")
        print(line)

    print(f"\noverall_ok={overall_ok}")
    print(f"diagnostics in: {diag}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
