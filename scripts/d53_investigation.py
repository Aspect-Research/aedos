"""Phase H D53 — empirical wbsearchentities investigation.

Before drafting the D53 design doc, query Wikidata's wbsearchentities
directly for the six Cluster 1 problem cases and capture the top 20
results. The question driving the investigation is whether
wbsearchentities reliably returns canonical entities for bare
ambiguous queries, or whether it has its own
prominence/surname-vs-label biases analogous to Wikipedia disambig
pages.

Three test variants per surface form:
  - default (no special params)
  - type=item explicit
  - strictlanguage=true

For each, capture position-of-canonical, top-5 labels, and match-type
of canonical (label vs alias).

This is free read-only — Wikidata is a public KB; no API key needed.
The script uses Aedos's existing CachingHTTPClient for consistent
rate-limited / cached access.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aedos.config import Config  # noqa: E402
from aedos.utils.http_cache import CachingHTTPClient, LRUHTTPCache  # noqa: E402


# Surface forms + the canonical Q-id we'd expect a well-designed
# disambiguation API to return at or near the top. From the D53 brief.
CASES = [
    ("Obama", "Q76", "Barack Obama"),
    ("Apple", "Q312", "Apple Inc."),
    ("Amazon", None, "no expected canonical without context"),
    ("Einstein", "Q937", "Albert Einstein"),
    ("President", "Q11696", "President of the United States"),
    ("Williams College", "Q49112", "Williams College"),
]

# Parameter variants — surface differences that may matter.
VARIANTS = {
    "default": {"language": "en", "limit": 20, "format": "json"},
    "type_item": {"language": "en", "limit": 20, "format": "json", "type": "item"},
    "strict_lang": {"language": "en", "limit": 20, "format": "json", "strictlanguage": "true"},
}


@dataclass
class ResultRow:
    rank: int
    qid: str
    label: str
    description: str
    match_type: str  # "label" or "alias"
    match_text: str
    aliases: list[str]


def query(http: CachingHTTPClient, surface: str, params_extra: dict) -> list[ResultRow]:
    params = {
        "action": "wbsearchentities",
        "search": surface,
        **params_extra,
    }
    data = http.get(
        "https://www.wikidata.org/w/api.php",
        params=params,
        ttl_seconds=3600,
    )
    out: list[ResultRow] = []
    for i, r in enumerate(data.get("search", []), 1):
        match = r.get("match", {}) if isinstance(r.get("match"), dict) else {}
        out.append(ResultRow(
            rank=i,
            qid=r.get("id", ""),
            label=r.get("label", ""),
            description=r.get("description", ""),
            match_type=match.get("type", ""),
            match_text=match.get("text", ""),
            aliases=r.get("aliases", []) if isinstance(r.get("aliases"), list) else [],
        ))
    return out


def find_canonical_position(results: list[ResultRow], expected_qid: str) -> int:
    """1-based rank of expected_qid in results; 0 if not present."""
    for r in results:
        if r.qid == expected_qid:
            return r.rank
    return 0


def main() -> int:
    cfg = Config()
    cache = LRUHTTPCache()
    http = CachingHTTPClient(cache=cache, headers={"User-Agent": cfg.user_agent})

    all_results: dict = {"cases": []}

    print(f"{'='*70}")
    print(f"D53 empirical investigation: wbsearchentities for problem cases")
    print(f"{'='*70}\n")

    for surface, expected_qid, note in CASES:
        case_data: dict = {
            "surface": surface,
            "expected_qid": expected_qid,
            "note": note,
            "variants": {},
        }
        print(f"\n--- {surface!r}  expected={expected_qid or '(none)'} ({note}) ---")

        for variant_name, params in VARIANTS.items():
            try:
                results = query(http, surface, params)
            except Exception as exc:
                print(f"  [{variant_name}] ERROR {type(exc).__name__}: {exc}")
                case_data["variants"][variant_name] = {"error": f"{type(exc).__name__}: {exc}"}
                continue

            pos = find_canonical_position(results, expected_qid) if expected_qid else 0
            top5 = [(r.rank, r.qid, r.label, r.match_type) for r in results[:5]]

            print(f"  [{variant_name}] {len(results)} results, canonical rank={pos or 'NOT FOUND'}")
            for rank, qid, label, mt in top5:
                marker = "  <-- EXPECTED" if expected_qid and qid == expected_qid else ""
                print(f"    {rank:2}. {qid:8} {label[:40]:40}  match={mt}{marker}")
            if expected_qid and pos and pos > 5:
                # show the canonical position context
                canonical = next(r for r in results if r.qid == expected_qid)
                print(f"    ...")
                print(f"    {canonical.rank:2}. {canonical.qid:8} {canonical.label[:40]:40}  match={canonical.match_type}  <-- EXPECTED")

            case_data["variants"][variant_name] = {
                "n_results": len(results),
                "canonical_position": pos,
                "top_20": [
                    {
                        "rank": r.rank, "qid": r.qid, "label": r.label,
                        "description": r.description,
                        "match_type": r.match_type, "match_text": r.match_text,
                        "aliases": r.aliases,
                    }
                    for r in results
                ],
            }

        all_results["cases"].append(case_data)

    # Print summary table
    print(f"\n\n{'='*70}")
    print(f"Summary: canonical position by variant")
    print(f"{'='*70}")
    print(f"{'surface':22} {'expected':10} {'default':10} {'type=item':12} {'strictlang':12}")
    for case in all_results["cases"]:
        s = case["surface"]
        eq = case["expected_qid"] or "-"
        d_pos = case["variants"].get("default", {}).get("canonical_position", "err")
        t_pos = case["variants"].get("type_item", {}).get("canonical_position", "err")
        sl_pos = case["variants"].get("strict_lang", {}).get("canonical_position", "err")

        def fmt(p):
            if p == 0:
                return "NOT FOUND"
            if p == "err":
                return "ERR"
            return str(p)
        print(f"{s:22} {eq:10} {fmt(d_pos):10} {fmt(t_pos):12} {fmt(sl_pos):12}")

    # Write structured output
    out_path = Path(__file__).resolve().parent.parent / "docs" / "phase_H" / "d53_investigation.json"
    out_path.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
