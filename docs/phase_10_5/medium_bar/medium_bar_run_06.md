# Aedos v0.15 Medium-Bar Evaluation Results

## Aedos v0.15
Total cases: 122
Accuracy: 68.0% (83/122)
False-verified rate: 1.6% (2)
False-abstain rate: 39.3% (33)

Per-failure-mode breakdown:
  belief_revision: 60.0% (6/10)
  cross_source_unification: 71.4% (15/21)
  entity_disambiguation: 56.5% (13/23)
  multi_hop_distribution: 75.0% (15/20)
  predicate_translation: 50.0% (14/28)
  principled_abstention: 100.0% (20/20)

## LLM-Only Baseline
Total cases: 122
Accuracy: 74.6% (91/122)
False-verified rate: 13.1% (16)
False-abstain rate: 11.9% (10)

Per-failure-mode breakdown:
  belief_revision: 30.0% (3/10)
  cross_source_unification: 71.4% (15/21)
  entity_disambiguation: 95.7% (22/23)
  multi_hop_distribution: 95.0% (19/20)
  predicate_translation: 92.9% (26/28)
  principled_abstention: 30.0% (6/20)

## Comparison
Accuracy delta: -6.6%
False-verified delta: -11.5%

## Phase 10.5 Acceptance Criteria
False-verified ≤ 5%: PASS (actual: 1.6%)
Accuracy ≥ baseline + 15pp: FAIL (delta: -6.6%)
No-regression belief_revision: PASS (Aedos 60.0% vs baseline 30.0%)
No-regression cross_source_unification: PASS (Aedos 71.4% vs baseline 71.4%)
No-regression entity_disambiguation: FAIL (Aedos 56.5% vs baseline 95.7%)
No-regression multi_hop_distribution: FAIL (Aedos 75.0% vs baseline 95.0%)
No-regression predicate_translation: FAIL (Aedos 50.0% vs baseline 92.9%)
No-regression principled_abstention: PASS (Aedos 100.0% vs baseline 30.0%)
Significant improvement (>= +20pp) on >= 4 of 6 modes: FAIL (2/6 modes)