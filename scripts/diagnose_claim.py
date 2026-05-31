"""Live single-claim diagnostic: run one or more Medium-Bar case_ids (or raw
statements) through the full pipeline against the seeded substrate and dump the
rich v0.16 trace so a verdict can be root-caused.

Usage:
    py -3 scripts/diagnose_claim.py --cases bonus_006 mhd_018 csu_006 csu_012 csu_018
    py -3 scripts/diagnose_claim.py --text "The Vatican is in Africa."
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from aedos.utils.env import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present(_ROOT / ".env")
os.environ["RUN_LIVE_KB"] = "1"
os.environ["RUN_LIVE_TESTS"] = "1"

from tests.evaluation.benchmark import load_test_set  # noqa: E402
from aedos.database import open_db  # noqa: E402
from aedos.pipeline import build_pipeline  # noqa: E402
from aedos.deployment.chat_wrapper import claim_observability  # noqa: E402
from aedos.layer1_extraction.extractor import ExtractionContext  # noqa: E402
from aedos.layer4_sources.walker import VerificationContext  # noqa: E402


def _p(m=""):
    print(m, flush=True)


def diagnose(label, statement, pipeline):
    extractor, walker, aggregator = pipeline.extractor, pipeline.walker, pipeline.aggregator
    _p("=" * 90)
    _p(f"CASE {label}: {statement}")
    _p("=" * 90)
    ctx = ExtractionContext(asserting_party="benchmark", context_type="document")
    claims = extractor.extract(statement, ctx)
    _p(f"extracted {len(claims)} claim(s):")
    for c in claims:
        _p(f"  - ({c.subject!r}, {c.predicate!r}, {c.object!r}) pol={c.polarity} "
           f"abstention_reason={c.abstention_reason} valid_from={c.valid_from} valid_until={c.valid_until}")
    groundable = [c for c in claims if c.abstention_reason is None]
    if not groundable:
        _p("  (no groundable claims -> abstain)")
        return
    vctx = VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(),
        asserting_party="benchmark", source_text=statement)
    results = [walker.walk(c, vctx) for c in groundable]
    vr = aggregator.aggregate(groundable, results)
    obs = claim_observability(vr, verbose=True)
    for o in obs:
        _p("")
        _p(f"  CLAIM verdict={o.get('verdict')} base={o.get('base_verdict')} "
           f"conditional={o.get('conditional')} abstention_reason={o.get('abstention_reason')} "
           f"contradicting_value={o.get('contradicting_value')}")
        th = o.get("trace_human")
        if th:
            for ln in th.splitlines():
                _p("    " + ln)
    _p("")
    _p("  per_claim_verdicts: " + json.dumps(getattr(vr, "per_claim_verdicts", {}), default=str))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="*", default=[])
    ap.add_argument("--text", default=None)
    ap.add_argument("--db-path", default="aedos_phase10_5.db")
    args = ap.parse_args()
    os.environ["AEDOS_DB_PATH"] = args.db_path
    pipeline = build_pipeline(open_db(args.db_path))

    items = []
    if args.text:
        items.append(("text", args.text))
    if args.cases:
        cases = {c.case_id: c for c in load_test_set()}
        for cid in args.cases:
            if cid in cases:
                items.append((cid, cases[cid].statement))
            else:
                _p(f"(unknown case_id {cid})")
    for label, stmt in items:
        try:
            diagnose(label, stmt, pipeline)
        except Exception as exc:
            import traceback
            _p(f"  !! diagnostic raised: {exc}")
            _p(traceback.format_exc())


if __name__ == "__main__":
    main()
