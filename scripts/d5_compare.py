"""Compare post-D16 baseline to post-D5 validation for derivation_corpus."""
import json
from pathlib import Path
from collections import Counter

repo = Path(__file__).resolve().parent.parent
pre_path = repo / "docs/phase_H/d16_postfix_baseline_derivation_corpus.json"
post_path = repo / "docs/phase_H/d16_rebaseline_derivation_corpus.json"
post_alt = repo / "docs/phase_H/d16_d5_validation_derivation_corpus.json"
if post_alt.exists():
    post_path = post_alt

with open(pre_path) as f:
    pre = json.load(f)
with open(post_path) as f:
    post = json.load(f)

pre_by_id = {o["case_id"]: o for o in pre["outcomes"]}
post_by_id = {o["case_id"]: o for o in post["outcomes"]}

print(f"Pre-D5  (post-D16 baseline): {pre['passed']}/{pre['total_cases']} = {pre['accuracy']:.1%}")
print(f"Post-D5 (this validation):   {post['passed']}/{post['total_cases']} = {post['accuracy']:.1%}")
print(f"Delta: {(post['accuracy'] - pre['accuracy']) * 100:+.1f} pp")
print()

pre_verdicts = Counter(o.get("produced_verdict") for o in pre["outcomes"])
post_verdicts = Counter(o.get("produced_verdict") for o in post["outcomes"])
print("Produced-verdict distribution:")
print(f"  {'verdict':<25} pre  post")
for v in sorted(set(pre_verdicts) | set(post_verdicts), key=lambda x: x or "_"):
    print(f"  {str(v):<25} {pre_verdicts[v]:3}  {post_verdicts[v]:3}")
print()

print(f"{'CASE_ID':<35} {'PRE':<25} {'POST':<25} DIRECTION")
print("-" * 110)
moved = []
for case_id in sorted(set(pre_by_id) | set(post_by_id)):
    pre_o = pre_by_id.get(case_id, {})
    post_o = post_by_id.get(case_id, {})
    pre_v = pre_o.get("produced_verdict") or "?"
    post_v = post_o.get("produced_verdict") or "?"
    pre_p = pre_o.get("passed", False)
    post_p = post_o.get("passed", False)
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
pass_to_fail = sum(1 for _, d, _, _ in moved if d == "PASS -> FAIL")
fail_to_pass = sum(1 for _, d, _, _ in moved if d == "FAIL -> PASS")
print(f"  pass -> fail: {pass_to_fail}")
print(f"  fail -> pass: {fail_to_pass}")
print(f"  verdict shifted (same pass/fail): {len(moved) - pass_to_fail - fail_to_pass}")
