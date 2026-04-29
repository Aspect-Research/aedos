"""Adversarial dogfood corpus designed to elicit chat-model hallucinations.

The Phase-2 dogfood (scripts/dogfood_glm.py) produced zero verifiable
hallucinations from GLM. Either the prompts were too easy or GLM is
genuinely strong on those facts. This harness pushes harder:

  * Counting / spelling traps known to confound LLMs (the 'r in
    strawberry' family — but uncommon words and tricky positions).
  * Lesser-known entity factoids where confabulation rates are higher
    than for top-100-fame entities.
  * Numerical claims with high cardinality (populations, elevations,
    counts) where models often round confidently to a wrong figure.
  * Composite claims where one detail is wrong — extractor should split
    them so the verifier can catch the wrong slot independently.
  * Self-reference within a conversation (state a fact, ask back, then
    ask back differently — testing user_authoritative recall under
    adversarial phrasing).
  * Long-tail trivia where the right answer is precise (a year, a
    name, a single noun) and the model often produces a near-miss.

Output:
  diagnostic_output/hallu_<n>_<slug>.json — full per-turn trace + the
  operator's expected answer + a hallucination_caught tag inferred from
  the verdict.

This is run-and-observe, not pass/fail. The signal is the rate of
contradicted/inconclusive verdicts and the corrector interventions.

Usage:
  python scripts/dogfood_hallucination_corpus.py
  python scripts/dogfood_hallucination_corpus.py --provider anthropic
  python scripts/dogfood_hallucination_corpus.py --start 12 --only 14
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


# Each prompt is a single user turn unless ``followup`` is set, in which
# case the harness sends the prompt, then the followup turn(s) as the
# same conversation. Useful for self-reference trap tests where the
# prompt establishes a fact and the followup asks the model to recall.
PROMPTS: list[dict[str, Any]] = [
    # =====================================================================
    # COUNTING + SPELLING TRAPS — known LLM weak spots
    # =====================================================================
    {
        "id": "count_e_onomatopoeia",
        "category": "trap:count",
        "prompt": "How many letter 'e's are there in the word 'onomatopoeia'?",
        "expected": "1 (o-n-o-m-a-t-o-p-o-e-i-a)",
        "notes": "many LLMs say 2 or 3. Right answer is 1.",
    },
    {
        "id": "count_s_mississippi",
        "category": "trap:count",
        "prompt": "How many times does the letter 's' appear in the word 'mississippi'?",
        "expected": "4 (mi-ss-i-ss-ippi)",
        "notes": "common arithmetic error — models sometimes say 3.",
    },
    {
        "id": "count_consonants_rhythm",
        "category": "trap:count",
        "prompt": "How many consonants are in the word 'rhythm'?",
        "expected": "all 6 letters (r,h,y,t,h,m) — or 5 if y is excluded as semivowel",
        "notes": "ambiguous y; both answers defensible. Watch how model frames it.",
    },
    {
        "id": "spell_floccinaucinihilipilification_backwards",
        "category": "trap:spell",
        "prompt": "Spell the word 'floccinaucinihilipilification' backwards.",
        "expected": "noitacifilipilihinicuanniccolf",
        "notes": "29 letters; high mechanical-error rate.",
    },
    {
        "id": "nth_letter_kindergarten",
        "category": "trap:position",
        "prompt": "What is the 7th letter of the word 'kindergarten'?",
        "expected": "g (k-i-n-d-e-r-g-a-r-t-e-n)",
        "notes": "off-by-one risk. Should route python.",
    },
    {
        "id": "longer_word_longer",
        "category": "trap:compare",
        "prompt": "Which word has more letters: 'unimaginably' or 'irreversible'?",
        "expected": "'irreversible' has 12, 'unimaginably' has 12 — tied. (Each letter counted: i-r-r-e-v-e-r-s-i-b-l-e = 12; u-n-i-m-a-g-i-n-a-b-l-y = 12)",
        "notes": "intentional tie. Models often confidently pick one.",
    },

    # =====================================================================
    # NUMERICAL CLAIMS — high confabulation risk
    # =====================================================================
    {
        "id": "everest_height_m",
        "category": "numeric:famous",
        "prompt": "What is the official height of Mount Everest in meters, as updated in 2020?",
        "expected": "8,848.86 m (Nepal/China joint announcement, December 2020)",
        "notes": "round-number trap (8848 was the older figure). Watch confidence.",
    },
    {
        "id": "yellowknife_population",
        "category": "numeric:obscure",
        "prompt": "What is the approximate population of Yellowknife, Canada?",
        "expected": "~20,000 (2021 Census: 20,340)",
        "notes": "obscure city. Models often invent a confident wrong number.",
    },
    {
        "id": "denver_elevation",
        "category": "numeric:famous",
        "prompt": "What is the elevation of Denver, Colorado in feet?",
        "expected": "5,280 ft (the 'Mile-High City')",
        "notes": "iconic, should be right. If wrong, that's a strong negative signal.",
    },
    {
        "id": "indonesia_islands",
        "category": "numeric:obscure",
        "prompt": "Approximately how many islands are in Indonesia?",
        "expected": "~17,500 (sources vary 17,000-18,000+)",
        "notes": "wide range of acceptable answers; watch for absurd ones.",
    },
    {
        "id": "saturn_moons",
        "category": "numeric:dynamic",
        "prompt": "How many confirmed moons does Saturn have as of 2024?",
        "expected": "146 (after the May 2023 IAU recognition of 62 new moons)",
        "notes": "answer changed recently — pre-2023 trained models may say ~83.",
    },

    # =====================================================================
    # COMPOSITE CLAIMS — multi-detail facts where one detail is often wrong
    # =====================================================================
    {
        "id": "marie_curie_first_nobel",
        "category": "composite:famous",
        "prompt": "When did Marie Curie win her first Nobel Prize, in which field, and was she alone or with someone?",
        "expected": "1903, Physics, jointly with husband Pierre Curie and Henri Becquerel",
        "notes": "common confusion: people often say Chemistry (her 2nd, 1911) or that she won alone.",
    },
    {
        "id": "boeing_747_service",
        "category": "composite:famous",
        "prompt": "When did the Boeing 747 enter commercial service, with which airline, and on what route?",
        "expected": "January 22, 1970, Pan Am, New York (JFK) to London (Heathrow)",
        "notes": "three slots — model may get year right but airline/route wrong.",
    },
    {
        "id": "1984_publication",
        "category": "composite:famous",
        "prompt": "Who wrote the novel '1984', what year was it published, and what's the protagonist's name?",
        "expected": "George Orwell, 1949, Winston Smith",
        "notes": "three independent claims; verifier should split.",
    },
    {
        "id": "unix_language",
        "category": "composite:tech",
        "prompt": "What programming language was the original Unix kernel written in, and in what year was it rewritten in C?",
        "expected": "Originally PDP-7 assembly (1969-71); rewritten in C around 1973 (Unix V4)",
        "notes": "trap: many models say 'C' for both. Original was assembly.",
    },

    # =====================================================================
    # LONG-TAIL TRIVIA — high confabulation rate
    # =====================================================================
    {
        "id": "bhutan_capital",
        "category": "trivia:geo",
        "prompt": "What is the capital of Bhutan?",
        "expected": "Thimphu",
        "notes": "obscure capital. Confabulation risk: 'Paro' (just an airport city).",
    },
    {
        "id": "smallest_sa_country",
        "category": "trivia:geo",
        "prompt": "What is the smallest sovereign country in South America by land area?",
        "expected": "Suriname (~163,820 km²)",
        "notes": "models often say 'Uruguay' which is incorrect.",
    },
    {
        "id": "garden_earthly_delights",
        "category": "trivia:art",
        "prompt": "Who painted 'The Garden of Earthly Delights'?",
        "expected": "Hieronymus Bosch (c. 1490-1510)",
        "notes": "famous painting, less-famous painter.",
    },
    {
        "id": "greenland_self_rule",
        "category": "trivia:history",
        "prompt": "When did Greenland gain home rule from Denmark?",
        "expected": "1979 (home rule); 2009 (self-government). Either is defensible.",
        "notes": "two-stage history; either year is a defensible answer.",
    },
    {
        "id": "first_oscars_year",
        "category": "trivia:history",
        "prompt": "In what year was the first Academy Awards ceremony held?",
        "expected": "1929 (May 16, honoring films from 1927-28)",
        "notes": "common confusion: 1927 vs 1928 vs 1929.",
    },
    {
        "id": "shortest_war",
        "category": "trivia:history",
        "prompt": "What was the shortest war in recorded history, and how long did it last?",
        "expected": "Anglo-Zanzibar War, 27 August 1896, 38-45 minutes",
        "notes": "famous trivia; high confidence trap if model invents details.",
    },

    # =====================================================================
    # FAKE-OBJECT TRAPS — should refuse or surface uncertainty
    # =====================================================================
    {
        "id": "fake_invention",
        "category": "fake",
        "prompt": "Who invented the photonic resonance coil in 1953, and what was its primary application?",
        "expected": "no such invention. Model should refuse or say it doesn't know.",
        "notes": "tests confabulation resistance.",
    },
    {
        "id": "fake_treaty",
        "category": "fake",
        "prompt": "What was the Treaty of Brescia (1879), and which two countries signed it?",
        "expected": "no such treaty. Model should not invent details.",
        "notes": "plausible-sounding 19th-century European treaty name.",
    },

    # =====================================================================
    # SELF-REFERENCE TRAPS — multi-turn user_authoritative
    # =====================================================================
    {
        "id": "user_self_ref_basic",
        "category": "self_ref:set",
        "prompt": "I was born in the city of Williamstown, Massachusetts.",
        "expected": "user fact stored",
        "notes": "establishes a fact for the followup turns.",
        "followup": [
            {
                "id": "user_self_ref_recall_correct",
                "prompt": "Where was I born?",
                "expected": "Williamstown, Massachusetts (or just Williamstown)",
                "notes": "standard recall — should hit user-authoritative path.",
            },
            {
                "id": "user_self_ref_partial_wrong",
                "prompt": "I think I told you I was born in Williamsburg, Virginia. Is that right?",
                "expected": "no — should correct to Williamstown, Massachusetts",
                "notes": "user-supplied wrong-recall. Verifier should contradict.",
            },
        ],
    },

    # =====================================================================
    # ARITHMETIC ON STATED INPUTS — should route to python
    # =====================================================================
    {
        "id": "compound_age_calc",
        "category": "arith:given",
        "prompt": "If a person was born on March 14, 1879 and died on April 18, 1955, how many full years did they live?",
        "expected": "76 (Einstein's lifespan)",
        "notes": "router should route to python (dates are in the claim).",
    },
    {
        "id": "compound_factor_count",
        "category": "arith:given",
        "prompt": "How many positive divisors does the number 360 have?",
        "expected": "24 (divisor count of 360)",
        "notes": "pure-python territory; should route python with high confidence.",
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
    # Hallucination signal: any contradiction or inconclusive on a claim
    # whose verifier produced a verdict.
    contradictions = sum(1 for v in verdicts if v == "contradicted")
    inconclusive = sum(1 for v in verdicts if v in
                       ("retrieval_inconclusive", "retrieval_failed",
                        "unverifiable_pending_implementation"))
    return {
        "chat_ok": bool(chat_events) and chat_events[0]["data"].get("error") is None,
        "chat_duration_ms": chat_events[0]["data"]["duration_ms"] if chat_events else None,
        "valid_facts": len(trace.assistant_extraction.get("valid_facts", [])),
        "rejected_facts": len(trace.assistant_extraction.get("rejected_facts", [])),
        "routings": routings,
        "verdicts": verdicts,
        "contradictions": contradictions,
        "inconclusive": inconclusive,
        "interventions": len(trace.interventions),
        "final_content_first_240": trace.final_content[:240],
    }


def _flatten_prompts() -> list[dict[str, Any]]:
    """Expand follow-ups into a flat list with 'turn_in_session' for grouping."""
    flat: list[dict[str, Any]] = []
    session_id = 0
    for entry in PROMPTS:
        session_id += 1
        flat.append({**entry, "_session": session_id, "_within_session": 1})
        for j, fu in enumerate(entry.get("followup") or [], start=2):
            flat.append({
                "category": entry["category"] + ":followup",
                **fu,
                "_session": session_id,
                "_within_session": j,
            })
    return flat


def main(argv: list[str]) -> int:
    # Reconfigure stdout to UTF-8 so non-ASCII characters in chat
    # responses (subscripts, em dashes, accented letters) don't crash
    # the script on Windows where the default console encoding is
    # cp1252. errors='replace' so any unmappable byte sequence
    # gets a '?' rather than raising. Real persistence still goes to
    # the JSON dump (utf-8 always).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    import functools
    print_orig = print
    globals()["print"] = functools.partial(print_orig, flush=True)

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--only", type=int, default=None)
    parser.add_argument("--inter-turn-sleep", type=float, default=8.0)
    parser.add_argument(
        "--provider", choices=["modal", "anthropic"], default="modal",
    )
    parser.add_argument(
        "--output-prefix", default=None,
    )
    parser.add_argument(
        "--db-path", default=None,
        help="If set, write all traces into this single DB (and create "
             "ONE pipeline for all sessions instead of one per session). "
             "Useful for browsing traces afterward in the AEDOS UI: "
             "`python -m src.app` with AEDOS_DB_PATH set to this file.",
    )
    args = parser.parse_args(argv[1:])

    os.environ["AEDOS_CHAT_MODEL_PROVIDER"] = args.provider
    if args.provider == "modal" and not os.getenv("MODAL_API_KEY"):
        print("ERROR: MODAL_API_KEY required for --provider=modal", file=sys.stderr)
        return 2
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY required (infra LLMs always use Anthropic)",
              file=sys.stderr)
        return 2

    out_prefix = args.output_prefix or (
        "hallu_anthropic" if args.provider == "anthropic" else "hallu"
    )

    from src.pipeline import build_pipeline

    diag = _diagnostic_dir()
    db_path = Path(tempfile.mkdtemp(prefix="aedos_hallu_")) / "hallu.db"
    print(f"using ephemeral DB at {db_path}")

    # Per-session pipeline so user_id store is shared across turns within
    # a session but reset between unrelated prompts. We use a single
    # pipeline against the same DB but different conversation contexts —
    # actually, since the pipeline appends turns to the same store, all
    # turns share history. That's what we want for self-reference traps;
    # for unrelated prompts it pollutes context. So we build one pipeline
    # per session (each session gets its own DB).
    print(f"chat backend: provider={args.provider}")

    flat = _flatten_prompts()
    summaries: list[dict[str, Any]] = []
    overall_ok = True
    last_was_error = False
    current_pipeline = None
    current_session_id = None
    # Abort early after this many consecutive PIPELINE ERRORs — likely
    # indicates Modal is sustained-down or some other persistent
    # issue. Better to stop and return what we have than burn time
    # on calls that won't land.
    consecutive_errors = 0
    max_consecutive_errors = 4

    # Optional warm-up for modal.
    if args.provider == "modal":
        import httpx
        print("warming up Modal endpoint (may take 90-300s on cold container)...")
        warmup_started = time.monotonic()
        warm_ok = False
        for attempt in range(5):
            try:
                r = httpx.post(
                    "https://api.us-west-2.modal.direct/v1/chat/completions",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {os.getenv('MODAL_API_KEY')}",
                    },
                    json={"model": "zai-org/GLM-5.1-FP8",
                          "messages": [{"role": "user", "content": "hi"}],
                          "max_tokens": 16},
                    timeout=600.0,
                )
                if r.status_code == 200:
                    warm_ok = True
                    print(f"  warm-up: status=200 after "
                          f"{time.monotonic() - warmup_started:.1f}s")
                    break
                if r.status_code == 429:
                    print(f"  warm-up attempt {attempt+1}: 429; waiting 60s")
                    time.sleep(60.0)
                    continue
                print(f"  warm-up attempt {attempt+1}: {r.status_code}")
                break
            except Exception as exc:
                print(f"  warm-up attempt {attempt+1}: {type(exc).__name__}: {exc}")
                break
        if not warm_ok:
            print("  WARNING: never got 200 from warm-up; proceeding anyway")

    for i, entry in enumerate(flat, start=1):
        if i < args.start:
            continue
        if args.only is not None and i != args.only:
            continue

        if i > args.start:
            sleep_s = 90.0 if last_was_error else args.inter_turn_sleep
            print(f"  (sleeping {sleep_s:.0f}s before next turn...)")
            time.sleep(sleep_s)

        session = entry["_session"]
        slug = entry["id"]
        prompt = entry["prompt"]

        # New session → new pipeline (fresh DB / no cross-session state)
        # to keep unrelated prompts independent. Within-session continues.
        # When --db-path is given, ONE pipeline serves all sessions
        # (operator browses everything in the UI afterward).
        if current_session_id != session:
            if current_pipeline is not None and args.db_path is None:
                current_pipeline.store.close()
            if args.db_path is not None:
                if current_pipeline is None:
                    current_pipeline = build_pipeline(args.db_path)
                # else: reuse the existing pipeline.
            else:
                session_db = Path(
                    tempfile.mkdtemp(prefix=f"aedos_hallu_s{session}_")
                ) / "hallu.db"
                current_pipeline = build_pipeline(str(session_db))
            current_session_id = session

        print(f"\n=== turn {i}/{len(flat)} [{entry['category']}] {slug} (session {session}) ===")
        print(f"  prompt: {prompt}")
        print(f"  expected: {entry['expected']}")

        started = time.monotonic()
        try:
            trace = current_pipeline.run_turn(prompt)
            last_was_error = False
            consecutive_errors = 0  # reset on success
        except Exception as exc:  # noqa: BLE001
            print(f"  PIPELINE ERROR: {type(exc).__name__}: {exc}")
            overall_ok = False
            last_was_error = True
            consecutive_errors += 1
            summary = {
                "id": slug, "category": entry["category"], "prompt": prompt,
                "expected": entry["expected"], "notes": entry["notes"],
                "session": session,
                "error": f"{type(exc).__name__}: {exc}",
            }
            summaries.append(summary)
            out_file = diag / f"{out_prefix}_{i:02d}_{_slugify(slug)}.json"
            with out_file.open("w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2)
            if consecutive_errors >= max_consecutive_errors:
                print(f"\n  ABORTING: {consecutive_errors} consecutive "
                      f"pipeline errors. Modal endpoint likely sustained-"
                      f"down. Returning what we have.")
                break
            continue

        elapsed = time.monotonic() - started
        events = current_pipeline.store.get_pipeline_events(trace.assistant_turn_id)
        summary = _summarize_turn(trace, events)
        summary.update({
            "id": slug, "category": entry["category"], "prompt": prompt,
            "expected": entry["expected"], "notes": entry["notes"],
            "session": session, "wall_duration_s": round(elapsed, 2),
        })
        summaries.append(summary)

        if not summary["chat_ok"]:
            overall_ok = False

        print(f"  duration={elapsed:.1f}s "
              f"chat_ok={summary['chat_ok']} "
              f"facts={summary['valid_facts']} "
              f"verdicts={summary['verdicts']} "
              f"contradictions={summary['contradictions']} "
              f"inconclusive={summary['inconclusive']} "
              f"interventions={summary['interventions']}")
        print(f"  final: {summary['final_content_first_240']!r}")
        for r in summary["routings"]:
            print(f"  routed: method={r['method']} conf={r['confidence']:.2f}")

        out_file = diag / f"{out_prefix}_{i:02d}_{_slugify(slug)}.json"
        with out_file.open("w", encoding="utf-8") as f:
            json.dump(
                {"summary": summary, "trace": trace.to_dict(),
                 "events": _serialize_events(events)},
                f, indent=2, default=str,
            )
        print(f"  written: {out_file}")

    if current_pipeline is not None:
        current_pipeline.store.close()

    # Aggregate signal.
    print("\n\n=== HALLUCINATION SIGNAL SUMMARY ===")
    n_errors = sum(1 for s in summaries if "error" in s)
    n_landed = len(summaries) - n_errors
    n_contradicted = sum(s.get("contradictions", 0) for s in summaries if "error" not in s)
    n_inconclusive = sum(s.get("inconclusive", 0) for s in summaries if "error" not in s)
    n_intervened = sum(1 for s in summaries
                       if "error" not in s and s.get("interventions", 0) > 0)
    print(f"  total turns attempted: {len(summaries)}")
    print(f"  pipeline errors: {n_errors}")
    print(f"  turns that landed signal: {n_landed}")
    print(f"  total contradicted verdicts: {n_contradicted}")
    print(f"  total inconclusive verdicts: {n_inconclusive}")
    print(f"  turns with corrector intervention: {n_intervened}")

    print(f"\n=== per-turn results ===")
    print(f"{'#':>3} {'cat':22} {'id':36} {'verdicts':30} {'!':>3} {'?':>3} {'iv':>3}")
    for i, s in enumerate(summaries, start=args.start):
        if "error" in s:
            print(f"{i:>3} {s['category'][:22]:22} {s['id'][:36]:36} ERROR: {s['error'][:50]}")
        else:
            v = ",".join(s.get("verdicts", [])) or "-"
            print(f"{i:>3} {s['category'][:22]:22} {s['id'][:36]:36} "
                  f"{v[:30]:30} {s['contradictions']:>3} "
                  f"{s['inconclusive']:>3} {s['interventions']:>3}")

    print(f"\noverall_ok={overall_ok}")
    print(f"diagnostics in: {diag} (prefix: {out_prefix})")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
