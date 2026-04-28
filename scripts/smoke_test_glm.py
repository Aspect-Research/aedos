"""Smoke test for the Modal-hosted GLM-5.1-FP8 chat backend.

Forces AEDOS_CHAT_MODEL_PROVIDER=modal, runs three turns through the
full AEDOS pipeline, dumps the per-turn pipeline_events to
diagnostic_output/glm_smoke_<n>.json, and prints a one-line summary
per turn so the operator can tell at a glance whether the plumbing
works.

Usage:
    python scripts/smoke_test_glm.py

Exit code 0 means each turn produced a non-empty assistant draft and
landed at least one chat_model_call event; non-zero means something
broke. Read the JSON dumps for the full picture.

This script writes to diagnostic_output/, which is gitignored — the
files are for the operator's local inspection, not for the repo.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()


SMOKE_PROMPTS = [
    "How many r's are in strawberry?",
    "What's 23 × 47?",
    "Spell egalitarian backwards.",
]


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


def main() -> int:
    os.environ["AEDOS_CHAT_MODEL_PROVIDER"] = "modal"

    if not os.getenv("MODAL_API_KEY"):
        print(
            "ERROR: MODAL_API_KEY not set. Add it to .env (see .env.example).",
            file=sys.stderr,
        )
        return 2
    if not os.getenv("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY not set — the infra LLMs (extractor, "
            "router, etc.) still run on Anthropic.",
            file=sys.stderr,
        )
        return 2

    from src.pipeline import build_pipeline

    diag = _diagnostic_dir()
    db_path = Path(tempfile.mkdtemp(prefix="aedos_smoke_")) / "smoke.db"
    print(f"using ephemeral DB at {db_path}")
    pipeline = build_pipeline(str(db_path))
    print(f"chat backend: {type(pipeline.chat_backend).__name__} "
          f"(model={getattr(pipeline.chat_backend, 'model', '?')})")

    overall_ok = True
    for i, prompt in enumerate(SMOKE_PROMPTS, start=1):
        if i > 1:
            # Modal endpoint is concurrency-limited per model; give the
            # previous request time to release its slot before firing the
            # next one. Empirically 5s is plenty for the warm window.
            time.sleep(5)
        print(f"\n--- turn {i}: {prompt!r} ---")
        started = time.monotonic()
        try:
            trace = pipeline.run_turn(prompt)
        except Exception as exc:  # noqa: BLE001
            print(f"  PIPELINE ERROR: {type(exc).__name__}: {exc}")
            overall_ok = False
            continue
        elapsed = time.monotonic() - started

        events = pipeline.store.get_pipeline_events(trace.assistant_turn_id)
        chat_events = [e for e in events if e["stage"] == "chat_model_call"]
        verification_events = [e for e in events if e["stage"] == "verification"]

        out_file = diag / f"glm_smoke_{i}.json"
        with out_file.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "prompt": prompt,
                    "trace": trace.to_dict(),
                    "events": _serialize_events(events),
                    "duration_s": elapsed,
                },
                f, indent=2, default=str,
            )

        # one-line summary
        n_claims = len(trace.assistant_extraction.get("valid_facts", []))
        n_rejected = len(trace.assistant_extraction.get("rejected_facts", []))
        n_decisions = len(trace.verification_decisions)
        verdicts = sorted(d.get("verification_status", "?")
                          for d in trace.verification_decisions)
        n_interventions = len(trace.interventions)
        chat_ok = bool(chat_events) and chat_events[0]["data"].get("error") is None
        if not chat_ok:
            overall_ok = False
        print(
            f"  duration={elapsed:.2f}s "
            f"chat_ok={chat_ok} "
            f"claims_extracted={n_claims} "
            f"rejected={n_rejected} "
            f"verdicts={verdicts} "
            f"interventions={n_interventions}"
        )
        print(f"  draft (first 240 chars): {trace.final_content[:240]!r}")
        print(f"  written: {out_file}")

    pipeline.store.close()
    print(f"\noverall_ok={overall_ok}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
