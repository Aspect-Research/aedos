# Aedos v0.15 Medium-Bar Evaluation Results

## Aedos v0.15
Total cases: 122
Accuracy: 45.1% (55/122)
False-verified rate: 0.0% (0)
False-abstain rate: 67.9% (57)

Per-failure-mode breakdown:
  belief_revision: 50.0% (5/10)
  cross_source_unification: 33.3% (7/21)
  entity_disambiguation: 39.1% (9/23)
  multi_hop_distribution: 30.0% (6/20)
  predicate_translation: 28.6% (8/28)
  principled_abstention: 100.0% (20/20)

## LLM-Only Baseline
Total cases: 122
Accuracy: 73.8% (90/122)
False-verified rate: 15.6% (19)
False-abstain rate: 9.5% (8)

Per-failure-mode breakdown:
  belief_revision: 20.0% (2/10)
  cross_source_unification: 76.2% (16/21)
  entity_disambiguation: 95.7% (22/23)
  multi_hop_distribution: 95.0% (19/20)
  predicate_translation: 96.4% (27/28)
  principled_abstention: 20.0% (4/20)

## Comparison
Accuracy delta: -28.7%
False-verified delta: -15.6%

## Phase 10.5 Acceptance Criteria
False-verified ≤ 5%: PASS (actual: 0.0%)
Accuracy ≥ baseline + 15pp: FAIL (delta: -28.7%)
No-regression belief_revision: PASS (Aedos 50.0% vs baseline 20.0%)
No-regression cross_source_unification: FAIL (Aedos 33.3% vs baseline 76.2%)
No-regression entity_disambiguation: FAIL (Aedos 39.1% vs baseline 95.7%)
No-regression multi_hop_distribution: FAIL (Aedos 30.0% vs baseline 95.0%)
No-regression predicate_translation: FAIL (Aedos 28.6% vs baseline 96.4%)
No-regression principled_abstention: PASS (Aedos 100.0% vs baseline 20.0%)
Significant improvement (>= +20pp) on >= 4 of 6 modes: FAIL (2/6 modes)