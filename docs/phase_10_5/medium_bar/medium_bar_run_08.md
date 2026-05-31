# Aedos v0.15 Medium-Bar Evaluation Results

## Aedos v0.15
Total cases: 122
Accuracy: 57.4% (70/122)
False-verified rate: 0.0% (0)
False-abstain rate: 48.8% (41)

Per-failure-mode breakdown:
  belief_revision: 60.0% (6/10)
  cross_source_unification: 66.7% (14/21)
  entity_disambiguation: 43.5% (10/23)
  multi_hop_distribution: 45.0% (9/20)
  predicate_translation: 39.3% (11/28)
  principled_abstention: 100.0% (20/20)

## LLM-Only Baseline
Total cases: 122
Accuracy: 77.9% (95/122)
False-verified rate: 10.7% (13)
False-abstain rate: 10.7% (9)

Per-failure-mode breakdown:
  belief_revision: 20.0% (2/10)
  cross_source_unification: 76.2% (16/21)
  entity_disambiguation: 95.7% (22/23)
  multi_hop_distribution: 95.0% (19/20)
  predicate_translation: 96.4% (27/28)
  principled_abstention: 45.0% (9/20)

## Comparison
Accuracy delta: -20.5%
False-verified delta: -10.7%

## Phase 10.5 Acceptance Criteria
False-verified ≤ 5%: PASS (actual: 0.0%)
Accuracy ≥ baseline + 15pp: FAIL (delta: -20.5%)
No-regression belief_revision: PASS (Aedos 60.0% vs baseline 20.0%)
No-regression cross_source_unification: FAIL (Aedos 66.7% vs baseline 76.2%)
No-regression entity_disambiguation: FAIL (Aedos 43.5% vs baseline 95.7%)
No-regression multi_hop_distribution: FAIL (Aedos 45.0% vs baseline 95.0%)
No-regression predicate_translation: FAIL (Aedos 39.3% vs baseline 96.4%)
No-regression principled_abstention: PASS (Aedos 100.0% vs baseline 45.0%)
Significant improvement (>= +20pp) on >= 4 of 6 modes: FAIL (2/6 modes)