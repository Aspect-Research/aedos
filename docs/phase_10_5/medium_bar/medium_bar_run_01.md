# Aedos v0.15 Medium-Bar Evaluation Results

## Aedos v0.15
Total cases: 122
Accuracy: 27.9% (34/122)
False-verified rate: 1.6% (2)
False-abstain rate: 83.3% (70)

Per-failure-mode breakdown:
  belief_revision: 30.0% (3/10)
  cross_source_unification: 23.8% (5/21)
  entity_disambiguation: 13.0% (3/23)
  multi_hop_distribution: 0.0% (0/20)
  predicate_translation: 17.9% (5/28)
  principled_abstention: 90.0% (18/20)

## LLM-Only Baseline
Total cases: 122
Accuracy: 78.7% (96/122)
False-verified rate: 9.8% (12)
False-abstain rate: 10.7% (9)

Per-failure-mode breakdown:
  belief_revision: 20.0% (2/10)
  cross_source_unification: 76.2% (16/21)
  entity_disambiguation: 95.7% (22/23)
  multi_hop_distribution: 95.0% (19/20)
  predicate_translation: 96.4% (27/28)
  principled_abstention: 50.0% (10/20)

## Comparison
Accuracy delta: -50.8%
False-verified delta: -8.2%

## Phase 10.5 Acceptance Criteria
False-verified ≤ 5%: PASS (actual: 1.6%)
Accuracy ≥ baseline + 15pp: FAIL (delta: -50.8%)
No-regression belief_revision: PASS (Aedos 30.0% vs baseline 20.0%)
No-regression cross_source_unification: FAIL (Aedos 23.8% vs baseline 76.2%)
No-regression entity_disambiguation: FAIL (Aedos 13.0% vs baseline 95.7%)
No-regression multi_hop_distribution: FAIL (Aedos 0.0% vs baseline 95.0%)
No-regression predicate_translation: FAIL (Aedos 17.9% vs baseline 96.4%)
No-regression principled_abstention: PASS (Aedos 90.0% vs baseline 50.0%)
Significant improvement (>= +20pp) on >= 4 of 6 modes: FAIL (1/6 modes)