# Aedos v0.15 Medium-Bar Evaluation Results

## Aedos v0.15
Total cases: 122
Accuracy: 62.3% (76/122)
False-verified rate: 0.0% (0)
False-abstain rate: 44.0% (37)

Per-failure-mode breakdown:
  belief_revision: 50.0% (5/10)
  cross_source_unification: 66.7% (14/21)
  entity_disambiguation: 52.2% (12/23)
  multi_hop_distribution: 50.0% (10/20)
  predicate_translation: 53.6% (15/28)
  principled_abstention: 100.0% (20/20)

## LLM-Only Baseline
Total cases: 122
Accuracy: 76.2% (93/122)
False-verified rate: 13.1% (16)
False-abstain rate: 10.7% (9)

Per-failure-mode breakdown:
  belief_revision: 20.0% (2/10)
  cross_source_unification: 76.2% (16/21)
  entity_disambiguation: 100.0% (23/23)
  multi_hop_distribution: 95.0% (19/20)
  predicate_translation: 92.9% (26/28)
  principled_abstention: 35.0% (7/20)

## Comparison
Accuracy delta: -13.9%
False-verified delta: -13.1%

## Phase 10.5 Acceptance Criteria
False-verified ≤ 5%: PASS (actual: 0.0%)
Accuracy ≥ baseline + 15pp: FAIL (delta: -13.9%)
No-regression belief_revision: PASS (Aedos 50.0% vs baseline 20.0%)
No-regression cross_source_unification: FAIL (Aedos 66.7% vs baseline 76.2%)
No-regression entity_disambiguation: FAIL (Aedos 52.2% vs baseline 100.0%)
No-regression multi_hop_distribution: FAIL (Aedos 50.0% vs baseline 95.0%)
No-regression predicate_translation: FAIL (Aedos 53.6% vs baseline 92.9%)
No-regression principled_abstention: PASS (Aedos 100.0% vs baseline 35.0%)
Significant improvement (>= +20pp) on >= 4 of 6 modes: FAIL (2/6 modes)