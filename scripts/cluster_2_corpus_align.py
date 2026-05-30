"""Phase H Cluster 2 step 5 — derivation_corpus expected-verdict realignment.

The mechanical rule under Cluster 2:

  Every case's `text` is now extracted into a claim and promoted to
  Tier U as `asserted_unverified` BEFORE the walker runs (step 2).
  The walker's Q-Lookup-α path (step 3) then attempts external
  grounding via KB / Python. The verdict is:

    * plain `verified` / `contradicted`     — external grounding
                                              succeeded (upgrade path)
    * `*_given_assertion`                   — external grounding
                                              failed or unavailable;
                                              the self-promoted Tier U
                                              row carried the verdict
    * plain (unchanged)                     — when seeded `tier_u_prior`
                                              (externally_verified, per
                                              Q-Seed) drives belief
                                              revision against the
                                              text-claim

Categorization rules (mechanical):

  R1 — case has `kb_claim` / `kb_claims` / `python_claim`:
       external grounding is the test's mechanism; KB/Python likely
       upgrades the asserted row. Verdict stays plain.

  R2 — text subject is a well-known real-world entity (heuristic
       allowlist: Obama, Einstein, Apple, Paris, France, Mount Everest,
       Marie Curie, Williams College, Microsoft, Cambridge): KB likely
       grounds the claim; verdict stays plain. JUDGMENT — depends on
       live KB behavior.

  R3 — text subject is a test/fictional entity (Asa, the flood, the
       company, Rex, the project, sonnet): KB cannot ground; verdict
       becomes `*_given_assertion`.

  R4 — case has `tier_u_prior` with same subject + opposite-polarity
       text ->polarity_conflict belief revision against
       externally_verified prior ->plain `contradicted` (Q-Seed).

  R5 — case expects `no_grounding_found` with no external grounding
       mechanism: under Cluster 2 the walker hits its own promoted
       Tier U row ->`verified_given_assertion`. Test intent shifts:
       was "abstain when no premise"; now "asserted-only verdict."

  R6 — text rejected at extraction (future tense, etc.): runner
       short-circuits before walker; expected `no_grounding_found`
       stays.

  Non-standard verdicts (`verified_with_correct_entity`,
  `needs_tier_u_or_kb`): handled by the runner's special-case
  branches. The script emits suggested branch updates separately.

Usage:
  py scripts/cluster_2_corpus_align.py            # dry-run, prints diff
  py scripts/cluster_2_corpus_align.py --apply    # writes the corpus
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


CORPUS_PATH = Path(__file__).resolve().parents[1] / "tests" / "calibration" / "derivation_corpus.jsonl"


# Heuristic entity allow-list for R2 (KB-likely-grounds).
_KB_LIKELY_SUBJECTS = {
    "Obama", "Einstein", "Apple", "Paris", "France", "Mount Everest",
    "Marie Curie", "Williams College", "Microsoft", "Cambridge",
    "Amazon", "Washington", "Mercury",
    # 'The company is headquartered in Palo Alto' — Palo Alto is in KB,
    # but the SUBJECT 'the company' isn't, so KB can't ground.
}


# Per-case overrides for cases the heuristic can't reason about correctly.
# Format: {case_id: (proposed_verdict, reason_for_override)}.
# These bypass the rule-based categorization. Surfaced explicitly in the
# script output so the override list is auditable.
_CASE_OVERRIDES: dict[str, tuple[str, str]] = {
    "der_predicate_translation_002": (
        "verified_given_assertion",
        "OVERRIDE: scope-mismatch test (Obama as President in 2005). "
        "KB has Obama-P39 with 2009-2017 qualifier; scope check returns "
        "NO_MATCH (not VERIFIED), so no upgrade, walker's own promoted "
        "row drives verified_given_assertion. Test intent shifts from "
        "'scope mismatch = abstain' to 'scope mismatch = no upgrade, "
        "stays asserted'.",
    ),
}


def _has_kb_or_python_grounding(case: dict) -> bool:
    """R1: case carries explicit KB/Python grounding setup."""
    inp = case["input"]
    return any(k in inp for k in ("kb_claim", "kb_claims", "python_claim"))


def _subject_is_kb_likely(case: dict) -> bool:
    """R2: case's text's SUBJECT (the part the walker's first
    extracted claim will hang off of) is a real-world named entity
    likely to be in KB.

    Heuristic: check if the text STARTS WITH a known entity (the
    likely subject position). 'Asa works in Cambridge' starts with
    'Asa', not Cambridge, so R2 does NOT fire — even though Cambridge
    is KB-real. The walker promotes 'Asa works_in Cambridge'; KB
    can't ground because Asa isn't in KB.

    Not authoritative — flagged as JUDGMENT in the output because
    KB lookup outcomes depend on more than the subject name.
    """
    text = case["input"].get("text", "").strip()
    # "All mammals have warm blood" → first word is 'All' (quantifier),
    # not a subject. Detect by looking at leading quantifier-style words.
    for prefix in ("All ", "Dogs ", "Humans ", "Somewhere ", "It "):
        if text.startswith(prefix):
            return False
    for ent in _KB_LIKELY_SUBJECTS:
        if text.startswith(ent):
            return True
    return False


def _has_tier_u_prior(case: dict) -> bool:
    """R4 candidate: case carries a `tier_u_prior` seed (now
    externally_verified per Q-Seed)."""
    inp = case["input"]
    return bool(inp.get("tier_u_prior")) or bool(inp.get("tier_u"))


def _is_future_rejected(case: dict) -> bool:
    """R6: case expects no_grounding_found because future-tense is
    rejected at extraction (no claims reach the walker)."""
    exp = case["expected_output"]
    return exp.get("reason") in ("future_claim_rejected", "modal_prediction")


def categorize(case: dict) -> dict:
    """Return a per-case categorization with proposed verdict update.

    Output shape:
      {
        "id": str,
        "current_verdict": str,
        "proposed_verdict": str,
        "rule": str,             # which rule applied
        "judgment": bool,        # True when rule depends on KB behavior
        "reason": str,           # human-readable
      }
    """
    case_id = case["id"]
    cur = case["expected_output"].get("verdict", "<none>")

    # Per-case overrides take precedence over rules.
    if case_id in _CASE_OVERRIDES:
        proposed, reason = _CASE_OVERRIDES[case_id]
        return {
            "id": case_id, "current_verdict": cur, "proposed_verdict": proposed,
            "rule": "OVERRIDE", "judgment": True, "reason": reason,
        }

    # Non-standard verdicts: don't touch the verdict field; the runner
    # handles these via branches. Surface them for branch-update review.
    if cur in ("verified_with_correct_entity", "needs_tier_u_or_kb", "<none>"):
        return {
            "id": case_id, "current_verdict": cur, "proposed_verdict": cur,
            "rule": "NON_STANDARD", "judgment": True,
            "reason": "runner's special-case branch; review separately",
        }

    # R6: future-tense rejection — extractor produces no claims, runner
    # short-circuits before walker. Unchanged.
    if _is_future_rejected(case):
        return {
            "id": case_id, "current_verdict": cur, "proposed_verdict": cur,
            "rule": "R6", "judgment": False,
            "reason": "extraction rejects future/modal claim; runner short-circuits",
        }

    # R1: explicit KB or Python grounding. Q-Lookup α upgrade fires;
    # verdict stays plain.
    if _has_kb_or_python_grounding(case):
        return {
            "id": case_id, "current_verdict": cur, "proposed_verdict": cur,
            "rule": "R1", "judgment": False,
            "reason": "kb_claim/python_claim present; upgrade path produces plain verdict",
        }

    # R4: tier_u_prior with opposite polarity ->belief-revision
    # contradicted (against externally_verified prior). Stays plain
    # contradicted. Same predicate + opposite polarity is the
    # polarity_conflict trigger; same predicate + different object on
    # functional predicate is object_conflict. Heuristic: if the case
    # expected `contradicted` AND has tier_u_prior, the belief-revision
    # mechanism is the test's point and the prior is externally_verified
    # ->plain `contradicted`. (Cases where the predicate differs from
    # the prior's predicate get caught here too; those will under-flag
    # if extraction normalizes differently — flagged as JUDGMENT.)
    if cur == "contradicted" and _has_tier_u_prior(case):
        return {
            "id": case_id, "current_verdict": cur, "proposed_verdict": cur,
            "rule": "R4", "judgment": True,
            "reason": "tier_u_prior is externally_verified; belief-revision against it is plain contradicted IF the new text-claim's predicate matches the prior's predicate (depends on extractor normalization)",
        }

    # R4 idempotent variant: tier_u_prior + same polarity + same content
    # ->idempotent write returns the prior's externally_verified row →
    # plain verified.
    if cur == "verified" and _has_tier_u_prior(case) and case["expected_output"].get("idempotent"):
        return {
            "id": case_id, "current_verdict": cur, "proposed_verdict": cur,
            "rule": "R4", "judgment": False,
            "reason": "idempotent write returns externally_verified prior; plain verified",
        }

    # R2: subject is in the KB-likely allowlist. KB upgrade may fire.
    # JUDGMENT — depends on live KB. Default per current verdict:
    #   - current was verified: stay plain `verified` (assume upgrade
    #     fires; Step 6 catches misses)
    #   - current was contradicted: stay plain `contradicted`
    #   - current was no_grounding_found: under Cluster 2 the walker
    #     always matches its own promoted row, so a `no_grounding_found`
    #     case CANNOT stay so — flip to `verified` (optimistic
    #     upgrade; falls back to `verified_given_assertion` if KB
    #     doesn't cooperate, exposed in Step 6).
    if _subject_is_kb_likely(case):
        if cur == "no_grounding_found":
            return {
                "id": case_id, "current_verdict": cur,
                "proposed_verdict": "verified",
                "rule": "R2", "judgment": True,
                "reason": (
                    "real-world subject likely in KB and current verdict "
                    "is no_grounding_found (impossible under Cluster 2 — "
                    "walker matches its own promoted row). Propose plain "
                    "verified (optimistic KB upgrade); Step 6 confirms"
                ),
            }
        return {
            "id": case_id, "current_verdict": cur, "proposed_verdict": cur,
            "rule": "R2", "judgment": True,
            "reason": "real-world subject likely in KB; upgrade path may fire — verify in Step 6",
        }

    # R3/R5 fallthrough: test/fictional subject (Asa, etc.) OR no KB
    # path ->walker hits its own promoted row ->*_given_assertion.
    #
    # IMPORTANT: under Cluster 2 the walker ALWAYS matches its own
    # promoted row (Stage 1 hit), so an old `no_grounding_found` case
    # produces `verified_given_assertion` (NOT `abstained_given_assertion`
    # — the walker found grounding, just asserted). `abstained_given_assertion`
    # only fires for user_authoritative claims with no Tier U premise
    # OR walks that exhaust depth budget; neither applies to the
    # corpus's R5 cases.
    base_to_dual = {
        "verified": "verified_given_assertion",
        "contradicted": "contradicted_given_assertion",
        "no_grounding_found": "verified_given_assertion",
    }
    proposed = base_to_dual.get(cur, cur)
    rule = "R5" if cur == "no_grounding_found" else "R3"
    if rule == "R5":
        reason = (
            "Cluster 2 semantic shift: walker matches its own promoted "
            "Tier U row, so a pre-Cluster-2 `no_grounding_found` becomes "
            "`verified_given_assertion`. Test intent shifts from 'no "
            "grounding for derivation' to 'asserted-only grounding'."
        )
    else:
        reason = (
            "subject is test/fictional (not in KB); walker hits own "
            "promoted row, upgrade fails, verdict is *_given_assertion"
        )
    return {
        "id": case_id, "current_verdict": cur, "proposed_verdict": proposed,
        "rule": rule, "judgment": False, "reason": reason,
    }


def load_corpus() -> list[dict]:
    with CORPUS_PATH.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def apply_proposals(cases: list[dict], proposals: list[dict]) -> list[dict]:
    by_id = {p["id"]: p for p in proposals}
    out: list[dict] = []
    for case in cases:
        p = by_id[case["id"]]
        if p["current_verdict"] != p["proposed_verdict"]:
            case["expected_output"]["verdict"] = p["proposed_verdict"]
        out.append(case)
    return out


def write_corpus(cases: list[dict]) -> None:
    with CORPUS_PATH.open("w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write updated corpus; otherwise dry-run prints diff")
    args = ap.parse_args()

    cases = load_corpus()
    proposals = [categorize(c) for c in cases]

    counts: dict[str, int] = {}
    judgment_ids: list[str] = []
    changed: list[dict] = []
    for p in proposals:
        counts[p["rule"]] = counts.get(p["rule"], 0) + 1
        if p["judgment"]:
            judgment_ids.append(p["id"])
        if p["current_verdict"] != p["proposed_verdict"]:
            changed.append(p)

    print("=" * 78)
    print(f"Cluster 2 corpus alignment — {len(cases)} cases")
    print("=" * 78)
    print()
    print("Per-rule counts:")
    for rule in sorted(counts):
        print(f"  {rule}: {counts[rule]:>3}")
    print(f"  CHANGED total: {len(changed)} / {len(cases)}")
    print()
    print("Per-case proposals:")
    for p in proposals:
        flag = " [JUDGMENT]" if p["judgment"] else ""
        change = ""
        if p["current_verdict"] != p["proposed_verdict"]:
            change = f"  {p['current_verdict']} ->{p['proposed_verdict']}"
        print(f"  {p['id']:<35} {p['rule']:<14}{flag}{change}")
        print(f"    {p['reason']}")
    print()
    print(f"Judgment cases ({len(judgment_ids)}) — verify under Step 6 live calibration:")
    for jid in judgment_ids:
        print(f"  - {jid}")

    if args.apply:
        updated = apply_proposals(cases, proposals)
        write_corpus(updated)
        print()
        print(f"WROTE {CORPUS_PATH}")
    else:
        print()
        print("(dry-run — pass --apply to write changes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
