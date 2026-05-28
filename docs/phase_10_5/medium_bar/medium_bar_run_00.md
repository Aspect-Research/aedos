# Aedos v0.15 Medium-Bar Evaluation Results

## Aedos v0.15
Total cases: 3
Accuracy: 33.3% (1/3)
False-verified rate: 0.0% (0)
False-abstain rate: 100.0% (1)

Per-failure-mode breakdown:
  cross_source_unification: 100.0% (1/1)
  multi_hop_distribution: 0.0% (0/2)

## LLM-Only Baseline
Total cases: 3
Accuracy: 66.7% (2/3)
False-verified rate: 33.3% (1)
False-abstain rate: 0.0% (0)

Per-failure-mode breakdown:
  cross_source_unification: 0.0% (0/1)
  multi_hop_distribution: 100.0% (2/2)

## Comparison
Accuracy delta: -33.3%
False-verified delta: -33.3%

## Phase 10.5 Acceptance Criteria
False-verified ≤ 5%: PASS (actual: 0.0%)
Accuracy ≥ baseline + 15pp: FAIL (delta: -33.3%)
No-regression cross_source_unification: PASS (Aedos 100.0% vs baseline 0.0%)
No-regression multi_hop_distribution: FAIL (Aedos 0.0% vs baseline 100.0%)
Significant improvement (>= +20pp) on >= 4 of 6 modes: FAIL (1/2 modes)