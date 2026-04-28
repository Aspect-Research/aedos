"""Eval harness — measure AEDOS verification's impact end-to-end.

For each prompt in a corpus, run it twice:

  raw     — just the chat model, no AEDOS verification or correction.
            This is the "what would the user have seen without AEDOS"
            baseline.
  aedos   — full AEDOS pipeline: extraction → verification → correction.

Then for each turn we record:

  raw_response       — what the chat model produced
  aedos_response     — what AEDOS shipped to the user (corrected if any)
  raw_matches_expected — does the raw response contain the expected
                          answer? (substring check; loose)
  aedos_matches_expected — same for AEDOS's output
  aedos_intervened   — did the corrector apply any intervention?
  aedos_verdicts     — list of verification statuses

The aggregate signal of interest:

  - **caught**:    raw didn't have the right answer; aedos has it
                   (verifier corrected a wrong claim)
  - **preserved**: raw had the right answer; aedos still has it
                   (verifier didn't break a correct claim)
  - **broken**:    raw had the right answer; aedos doesn't
                   (verifier or corrector damaged a correct claim)
  - **missed**:    neither raw nor aedos has it (verifier didn't help)
  - **uncertain**: aedos hedged — output may or may not be right

These are loose substring-match metrics; they aren't a substitute for
human review of edge cases. Save the full per-turn dump for that.

Run:
  python scripts/eval_harness.py
  python scripts/eval_harness.py --provider anthropic
  python scripts/eval_harness.py --corpus hallucination

Output:
  eval_results/eval_<timestamp>_<provider>.json — full per-turn data
  eval_results/eval_<timestamp>_<provider>_summary.txt — readable
                                                          aggregate report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()


def _eval_dir() -> Path:
    d = Path(__file__).resolve().parent.parent / "eval_results"
    d.mkdir(exist_ok=True)
    return d


def _load_corpus(name: str) -> list[dict[str, Any]]:
    """Load the prompt corpus by name. Currently supports
    'hallucination' (the dogfood_hallucination_corpus prompts)."""
    if name == "hallucination":
        from scripts.dogfood_hallucination_corpus import _flatten_prompts
        return _flatten_prompts()
    raise ValueError(f"unknown corpus: {name}")


def _expected_substrings(expected: str) -> list[str]:
    """Pull substrings from the operator's expected-answer string that
    we'll look for in responses. Splits on commas/semicolons/slashes/
    'or'/'and' but NOT on space, so multi-word proper nouns
    ('Matthew Prince', 'New England') stay intact. Drops parentheticals
    and tokens shorter than 3 chars."""
    import re
    # Drop content in parentheses (often parenthetical explanation).
    cleaned = re.sub(r"\([^)]*\)", "", expected)
    # Split on commas, semicolons, slashes, ' or ', ' and '.
    parts = re.split(r"[,;/]| or | and ", cleaned)
    out: list[str] = []
    for p in parts:
        s = p.strip().strip(".'\":")
        if len(s) >= 3 and not s.lower().startswith("no "):
            out.append(s.lower())
    return out


def _matches_expected(text: str, expected: str) -> bool:
    """Loose substring match. True if any meaningful substring of the
    operator's expected answer appears in the response."""
    if not text or not expected:
        return False
    needles = _expected_substrings(expected)
    if not needles:
        return False
    haystack = text.lower()
    return any(n in haystack for n in needles)


def _classify_turn(raw_resp: str, aedos_resp: str, expected: str,
                   intervened: bool, verdicts: list[str]) -> str:
    """One of: caught / preserved / broken / missed / uncertain."""
    raw_ok = _matches_expected(raw_resp, expected)
    aedos_ok = _matches_expected(aedos_resp, expected)

    # Hedged / inconclusive: AEDOS may have softened a correct answer.
    if any(v in {"retrieval_inconclusive", "retrieval_failed",
                 "unverifiable_pending_implementation"}
           for v in verdicts):
        return "uncertain"

    if not raw_ok and aedos_ok:
        return "caught"
    if raw_ok and aedos_ok:
        return "preserved"
    if raw_ok and not aedos_ok:
        return "broken"
    return "missed"  # neither had it


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


def main(argv: list[str]) -> int:
    import functools
    print_orig = print
    globals()["print"] = functools.partial(print_orig, flush=True)

    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["modal", "anthropic"],
                        default="modal")
    parser.add_argument("--corpus", default="hallucination",
                        help="corpus name (currently only 'hallucination')")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--limit", type=int, default=None,
                        help="cap total turns for a quick run")
    parser.add_argument("--inter-turn-sleep", type=float, default=8.0)
    args = parser.parse_args(argv[1:])

    os.environ["AEDOS_CHAT_MODEL_PROVIDER"] = args.provider
    if args.provider == "modal" and not os.getenv("MODAL_API_KEY"):
        print("ERROR: MODAL_API_KEY required for --provider=modal",
              file=sys.stderr)
        return 2
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY required",
              file=sys.stderr)
        return 2

    corpus = _load_corpus(args.corpus)
    if args.limit:
        corpus = corpus[: args.limit]

    out_dir = _eval_dir()
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_path = out_dir / f"eval_{timestamp}_{args.provider}.json"
    summary_path = out_dir / f"eval_{timestamp}_{args.provider}_summary.txt"

    from src.llm_client import ChatMessage
    from src.llm_clients import build_chat_backend
    from src.llm_client import LLMClient
    from src.pipeline import build_pipeline

    llm = LLMClient()
    chat_backend = build_chat_backend(llm=llm)
    print(f"corpus: {args.corpus} ({len(corpus)} prompts)")
    print(f"chat backend: {type(chat_backend).__name__}")
    print(f"output: {out_path}")

    results: list[dict[str, Any]] = []
    for i, entry in enumerate(corpus, start=1):
        if i < args.start:
            continue
        prompt = entry["prompt"]
        expected = entry.get("expected", "")
        slug = entry["id"]
        print(f"\n=== {i}/{len(corpus)} {slug} ===")
        print(f"  prompt: {prompt}")

        # ---- raw chat (no verification) ----
        raw_resp = ""
        raw_error = None
        raw_started = time.monotonic()
        try:
            raw_resp = chat_backend.chat(
                "You are a helpful assistant. Answer concisely.",
                [ChatMessage(role="user", content=prompt)],
                max_tokens=1024,
            )
        except Exception as exc:  # noqa: BLE001
            raw_error = f"{type(exc).__name__}: {exc}"
        raw_elapsed = time.monotonic() - raw_started
        print(f"  raw ({raw_elapsed:.1f}s): {raw_resp[:200]!r}"
              if raw_resp else f"  raw ERROR: {raw_error}")

        # Brief delay to let Modal slot release before next call.
        time.sleep(args.inter_turn_sleep)

        # ---- AEDOS pipeline ----
        db_path = Path(tempfile.mkdtemp(prefix=f"aedos_eval_{i:02d}_")) / "e.db"
        pipeline = build_pipeline(str(db_path))
        aedos_resp = ""
        aedos_error = None
        intervened = False
        verdicts: list[str] = []
        aedos_started = time.monotonic()
        try:
            trace = pipeline.run_turn(prompt)
            aedos_resp = trace.final_content
            intervened = bool(trace.interventions)
            verdicts = [d.get("verification_status", "?")
                        for d in trace.verification_decisions]
        except Exception as exc:  # noqa: BLE001
            aedos_error = f"{type(exc).__name__}: {exc}"
        aedos_elapsed = time.monotonic() - aedos_started
        pipeline.store.close()

        print(f"  aedos ({aedos_elapsed:.1f}s): {aedos_resp[:200]!r}"
              if aedos_resp else f"  aedos ERROR: {aedos_error}")
        print(f"  verdicts={verdicts} intervened={intervened}")

        classification = (
            _classify_turn(raw_resp, aedos_resp, expected,
                           intervened, verdicts)
            if not raw_error and not aedos_error else "errored"
        )
        print(f"  classification: {classification}")

        results.append({
            "i": i,
            "id": slug,
            "category": entry.get("category"),
            "prompt": prompt,
            "expected": expected,
            "raw": {"response": raw_resp, "error": raw_error,
                    "duration_s": round(raw_elapsed, 2)},
            "aedos": {"response": aedos_resp, "error": aedos_error,
                      "duration_s": round(aedos_elapsed, 2),
                      "intervened": intervened, "verdicts": verdicts},
            "classification": classification,
        })

        # Persist incrementally so a crash doesn't lose all results.
        with out_path.open("w", encoding="utf-8") as f:
            json.dump({"corpus": args.corpus, "provider": args.provider,
                       "results": results}, f, indent=2)

    # Aggregate.
    counts: dict[str, int] = {}
    for r in results:
        c = r["classification"]
        counts[c] = counts.get(c, 0) + 1

    summary_lines = [
        f"=== AEDOS eval — corpus={args.corpus} provider={args.provider} ===",
        f"timestamp: {timestamp}",
        f"total turns: {len(results)}",
        "",
        "Aggregate counts:",
    ]
    for c in ("caught", "preserved", "broken", "missed", "uncertain", "errored"):
        if counts.get(c, 0):
            summary_lines.append(f"  {c}: {counts[c]}")
    summary_lines.append("")
    summary_lines.append("Per-turn:")
    for r in results:
        summary_lines.append(
            f"  [{r['classification']}] {r['id']:36} "
            f"intervened={r['aedos'].get('intervened', False)} "
            f"verdicts={r['aedos'].get('verdicts', [])}"
        )

    summary_text = "\n".join(summary_lines)
    summary_path.write_text(summary_text, encoding="utf-8")
    print("\n" + summary_text)
    print(f"\nfull results: {out_path}")
    print(f"summary:      {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
