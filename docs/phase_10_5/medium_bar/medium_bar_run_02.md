# Aedos v0.15 Medium-Bar Evaluation Results

## Aedos v0.15
Total cases: 122
Accuracy: 38.5% (47/122)
False-verified rate: 0.0% (0)
False-abstain rate: 75.0% (63)

Per-failure-mode breakdown:
  belief_revision: 20.0% (2/10)
  cross_source_unification: 23.8% (5/21)
  entity_disambiguation: 30.4% (7/23)
  multi_hop_distribution: 30.0% (6/20)
  predicate_translation: 25.0% (7/28)
  principled_abstention: 100.0% (20/20)

## LLM-Only Baseline
Total cases: 122
Accuracy: 77.0% (94/122)
False-verified rate: 13.1% (16)
False-abstain rate: 9.5% (8)

Per-failure-mode breakdown:
  belief_revision: 20.0% (2/10)
  cross_source_unification: 76.2% (16/21)
  entity_disambiguation: 95.7% (22/23)
  multi_hop_distribution: 95.0% (19/20)
  predicate_translation: 100.0% (28/28)
  principled_abstention: 35.0% (7/20)

## Comparison
Accuracy delta: -38.5%
False-verified delta: -13.1%

## Phase 10.5 Acceptance Criteria
False-verified ≤ 5%: PASS (actual: 0.0%)
Accuracy ≥ baseline + 15pp: FAIL (delta: -38.5%)
No-regression belief_revision: PASS (Aedos 20.0% vs baseline 20.0%)
No-regression cross_source_unification: FAIL (Aedos 23.8% vs baseline 76.2%)
No-regression entity_disambiguation: FAIL (Aedos 30.4% vs baseline 95.7%)
No-regression multi_hop_distribution: FAIL (Aedos 30.0% vs baseline 95.0%)
No-regression predicate_translation: FAIL (Aedos 25.0% vs baseline 100.0%)
No-regression principled_abstention: PASS (Aedos 100.0% vs baseline 35.0%)
Significant improvement (>= +20pp) on >= 4 of 6 modes: FAIL (1/6 modes)