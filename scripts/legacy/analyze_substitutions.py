"""Apply the extractor's substitution detector to existing corpus dumps.

Walks diagnostic_output/<prefix>_*.json. For each file's extracted
facts, runs ClaimExtractor._flag_substitutions against the original
chat draft and reports any flagged facts.

Useful for: post-hoc analysis of past corpus runs, validating that
the detector catches what we expect, and surfacing the substitution
rate as a monitorable metric.

Usage:
    python scripts/analyze_substitutions.py
    python scripts/analyze_substitutions.py --prefix hallu
    python scripts/analyze_substitutions.py --prefix dogfood
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extractor import ClaimExtractor, ExtractionResult


def _diag_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "diagnostic_output"


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default="hallu")
    args = parser.parse_args(argv[1:])

    files = sorted(_diag_dir().glob(f"{args.prefix}_*.json"))
    if not files:
        print(f"no files matching {args.prefix}_*.json in {_diag_dir()}",
              file=sys.stderr)
        return 1

    total_facts = 0
    total_warnings = 0
    warnings_by_kind: dict[str, int] = {}
    flagged: list[dict] = []

    for f in files:
        d = json.load(f.open(encoding="utf-8"))
        if "trace" not in d:
            continue  # error-format file
        draft = d["trace"].get("final_content", "")
        extracted = d["trace"].get("assistant_extraction", {}).get("valid_facts", [])
        if not extracted:
            continue
        total_facts += len(extracted)

        result = ExtractionResult(valid_facts=list(extracted))
        ClaimExtractor._flag_substitutions(result, draft)
        for w in result.warnings:
            kind = w["kind"]
            warnings_by_kind[kind] = warnings_by_kind.get(kind, 0) + 1
        if result.warnings:
            flagged.append({
                "id": (d.get("summary") or {}).get("id", f.stem),
                "category": (d.get("summary") or {}).get("category", "?"),
                "warnings": result.warnings,
                "draft": draft,
            })
            total_warnings += len(result.warnings)

    print(f"=== substitution analysis: {args.prefix} ({len(files)} files) ===\n")
    print(f"  total extracted facts: {total_facts}")
    print(f"  flagged facts:         {total_warnings}")
    if total_facts:
        rate = (total_warnings / total_facts) * 100
        print(f"  rate:                  {rate:.1f}%")
    print(f"  flagged turns:         {len(flagged)}")
    print(f"  by kind:")
    for k, n in sorted(warnings_by_kind.items()):
        print(f"    {k}: {n}")
    print()

    if flagged:
        print(f"\n=== flagged turns ===\n")
        for entry in flagged:
            print(f"--- {entry['id']} [{entry['category']}] ---")
            print(f"  draft (first 240): {entry['draft'][:240]}")
            for w in entry["warnings"]:
                print(f"    [{w['kind']}] {w['detail'][:200]}")
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
