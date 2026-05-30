"""Compare Phase E5 Haiku run to D16 post-fix derivation results."""
import json
from pathlib import Path

repo = Path(__file__).resolve().parent.parent
pre_path = repo / "docs/phase_E/results/phase_e5_per_component/claude-haiku-4-5__derivation_corpus.json"
post_path = repo / "docs/phase_H/d16_rebaseline_derivation_corpus.json"

with open(pre_path) as f:
    pre_data = json.load(f)
with open(post_path) as f:
    post_data = json.load(f)

# Locate the per-case outcomes in the pre data.
pre_outcomes = None
if isinstance(pre_data, dict):
    pre_outcomes = pre_data.get("per_case_outcomes") or pre_data.get("outcomes")
if pre_outcomes is None and isinstance(pre_data, list):
    pre_outcomes = pre_data

if pre_outcomes is None:
    # Pre-data nesting — search one level deeper.
    for v in (pre_data.values() if isinstance(pre_data, dict) else []):
        if isinstance(v, list) and v and isinstance(v[0], dict) and "case_id" in v[0]:
            pre_outcomes = v
            break

pre_by_id = {o["case_id"]: o for o in pre_outcomes}
post_by_id = {o["case_id"]: o for o in post_data["outcomes"]}

pre_pass = sum(1 for o in pre_outcomes if o.get("passed"))
post_pass = sum(1 for o in post_data["outcomes"] if o.get("passed"))
print(f"Pre-fix  (Haiku, Phase E5): {pre_pass}/{len(pre_outcomes)} = {pre_pass/len(pre_outcomes):.1%}")
print(f"Post-fix (production):      {post_pass}/{len(post_data['outcomes'])} = {post_pass/len(post_data['outcomes']):.1%}")
print()

print(f"{'CASE_ID':<35} {'PRE':<25} {'POST':<25} DIRECTION")
print("-" * 110)

moved = []
for case_id in sorted(set(pre_by_id) | set(post_by_id)):
    pre = pre_by_id.get(case_id, {})
    post = post_by_id.get(case_id, {})
    pre_v = pre.get("produced_verdict") or "?"
    post_v = post.get("produced_verdict") or "?"
    pre_p = pre.get("passed", False)
    post_p = post.get("passed", False)
    direction = ""
    if pre_p and not post_p:
        direction = "PASS -> FAIL"
    elif not pre_p and post_p:
        direction = "FAIL -> PASS"
    elif pre_v != post_v:
        direction = "verdict shifted"
    if direction:
        moved.append((case_id, direction, pre_v, post_v))
        print(f"  {case_id:<33} {pre_v:<25} {post_v:<25} {direction}")

print()
print(f"Total moved cases: {len(moved)}")
pre_to_fail = sum(1 for _, d, _, _ in moved if d == "PASS -> FAIL")
fail_to_pass = sum(1 for _, d, _, _ in moved if d == "FAIL -> PASS")
print(f"  pass -> fail: {pre_to_fail}")
print(f"  fail -> pass: {fail_to_pass}")
print(f"  verdict shifted (same pass/fail): {len(moved) - pre_to_fail - fail_to_pass}")
