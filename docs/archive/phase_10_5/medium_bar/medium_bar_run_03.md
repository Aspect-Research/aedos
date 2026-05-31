# Aedos v0.15 Medium-Bar Evaluation Results

## Aedos v0.15
Total cases: 122
Accuracy: 43.4% (53/122)
False-verified rate: 0.0% (0)
False-abstain rate: 69.0% (58)

Per-failure-mode breakdown:
  belief_revision: 50.0% (5/10)
  cross_source_unification: 33.3% (7/21)
  entity_disambiguation: 34.8% (8/23)
  multi_hop_distribution: 35.0% (7/20)
  predicate_translation: 21.4% (6/28)
  principled_abstention: 100.0% (20/20)

## LLM-Only Baseline
Total cases: 122
Accuracy: 75.4% (92/122)
False-verified rate: 11.5% (14)
False-abstain rate: 11.9% (10)

Per-failure-mode breakdown:
  belief_revision: 30.0% (3/10)
  cross_source_unification: 71.4% (15/21)
  entity_disambiguation: 95.7% (22/23)
  multi_hop_distribution: 95.0% (19/20)
  predicate_translation: 92.9% (26/28)
  principled_abstention: 35.0% (7/20)

## Comparison
Accuracy delta: -32.0%
False-verified delta: -11.5%

## Phase 10.5 Acceptance Criteria
False-verified ≤ 5%: PASS (actual: 0.0%)
Accuracy ≥ baseline + 15pp: FAIL (delta: -32.0%)
No-regression belief_revision: PASS (Aedos 50.0% vs baseline 30.0%)
No-regression cross_source_unification: FAIL (Aedos 33.3% vs baseline 71.4%)
No-regression entity_disambiguation: FAIL (Aedos 34.8% vs baseline 95.7%)
No-regression multi_hop_distribution: FAIL (Aedos 35.0% vs baseline 95.0%)
No-regression predicate_translation: FAIL (Aedos 21.4% vs baseline 92.9%)
No-regression principled_abstention: PASS (Aedos 100.0% vs baseline 35.0%)
Significant improvement (>= +20pp) on >= 4 of 6 modes: FAIL (2/6 modes)