"""Phase H D53 — hybrid approach validation.

The main D53 investigation showed wbsearchentities buries canonical
entities for short ambiguous surface forms whose primary label is an
alias (Obama, Amazon, Williams College's expected Q-id, etc.). This
follow-up tests the hybrid hypothesis: if we use Wikipedia redirect
resolution to canonicalize the surface form first, does
wbsearchentities then return the canonical Q-id cleanly?

For each problem case, query wbsearchentities with:
  (a) the bare surface form (results captured in d53_investigation.json)
  (b) the Wikipedia-canonical form (what Stage 1 of D47 produces)

Compare canonical Q-id positions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aedos.config import Config  # noqa: E402
from aedos.utils.http_cache import CachingHTTPClient, LRUHTTPCache  # noqa: E402


# (surface, expected_qid, wikipedia_canonical_form_we_expect_redirect_to_produce)
# The "canonical" column is what Wikipedia's redirect API returns
# when you query the bare surface (i.e. what D47 Stage 1's
# clean_redirect produces today).
CASES = [
    ("Obama", "Q76", "Barack Obama"),
    ("Apple", "Q312", "Apple Inc."),  # already works, included for control
    ("Amazon", "Q3884", "Amazon"),  # Wikipedia primary for "Amazon" → disambig page, not a clean redirect; we'd skip the hybrid here
    ("Einstein", "Q937", "Albert Einstein"),
    ("President", "Q11696", "President of the United States"),
    ("Williams College", "Q49166", "Williams College"),  # corrected from brief's stale Q49112
]


def query_wbsearch(http: CachingHTTPClient, surface: str) -> list[dict]:
    params = {
        "action": "wbsearchentities",
        "search": surface,
        "language": "en",
        "limit": 20,
        "format": "json",
        "type": "item",
    }
    data = http.get(
        "https://www.wikidata.org/w/api.php",
        params=params,
        ttl_seconds=3600,
    )
    return data.get("search", [])


def find_pos(results: list[dict], qid: str) -> int:
    for i, r in enumerate(results, 1):
        if r.get("id") == qid:
            return i
    return 0


def main() -> int:
    cfg = Config()
    cache = LRUHTTPCache()
    http = CachingHTTPClient(cache=cache, headers={"User-Agent": cfg.user_agent})

    print(f"{'='*72}")
    print(f"D53 hybrid-approach validation: bare vs canonical-form wbsearchentities")
    print(f"{'='*72}\n")
    print(f"{'surface':22} {'expected':10} {'bare rank':12} {'canonical form':30} {'canonical rank'}")
    print("-" * 100)

    rows = []
    for surface, expected_qid, canonical_form in CASES:
        bare_results = query_wbsearch(http, surface)
        bare_pos = find_pos(bare_results, expected_qid)

        canon_results = query_wbsearch(http, canonical_form)
        canon_pos = find_pos(canon_results, expected_qid)

        rows.append({
            "surface": surface,
            "expected_qid": expected_qid,
            "bare_rank": bare_pos,
            "canonical_form": canonical_form,
            "canonical_rank": canon_pos,
            "canonical_top5": [
                {"rank": i, "qid": r.get("id"), "label": r.get("label"),
                 "match_type": (r.get("match") or {}).get("type")}
                for i, r in enumerate(canon_results[:5], 1)
            ],
        })

        def fmt(p):
            return "NOT FOUND" if p == 0 else str(p)
        print(f"{surface:22} {expected_qid:10} {fmt(bare_pos):12} {canonical_form[:30]:30} {fmt(canon_pos)}")

    # Detailed output
    print(f"\n\nDetailed top-5 for canonical-form queries:")
    for row in rows:
        print(f"\n  '{row['canonical_form']}' (expected {row['expected_qid']}):")
        for r in row["canonical_top5"]:
            marker = "  <-- EXPECTED" if r["qid"] == row["expected_qid"] else ""
            print(f"    {r['rank']}. {r['qid']:9} {(r['label'] or '')[:50]:50} match={r['match_type']}{marker}")

    out_path = Path(__file__).resolve().parent.parent / "docs" / "phase_H" / "d53_hybrid_investigation.json"
    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
