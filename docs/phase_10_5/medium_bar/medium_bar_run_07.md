# Aedos v0.15 Medium-Bar Evaluation Results

## Aedos v0.15
Total cases: 122
Accuracy: 57.4% (70/122)
False-verified rate: 0.0% (0)
False-abstain rate: 47.6% (40)

Per-failure-mode breakdown:
  belief_revision: 60.0% (6/10)
  cross_source_unification: 47.6% (10/21)
  entity_disambiguation: 47.8% (11/23)
  multi_hop_distribution: 40.0% (8/20)
  predicate_translation: 53.6% (15/28)
  principled_abstention: 100.0% (20/20)

## LLM-Only Baseline
Total cases: 122
Accuracy: 76.2% (93/122)
False-verified rate: 12.3% (15)
False-abstain rate: 11.9% (10)

Per-failure-mode breakdown:
  belief_revision: 30.0% (3/10)
  cross_source_unification: 71.4% (15/21)
  entity_disambiguation: 95.7% (22/23)
  multi_hop_distribution: 95.0% (19/20)
  predicate_translation: 96.4% (27/28)
  principled_abstention: 35.0% (7/20)

## Comparison
Accuracy delta: -18.9%
False-verified delta: -12.3%

## Phase 10.5 Acceptance Criteria
False-verified ≤ 5%: PASS (actual: 0.0%)
Accuracy ≥ baseline + 15pp: FAIL (delta: -18.9%)
No-regression belief_revision: PASS (Aedos 60.0% vs baseline 30.0%)
No-regression cross_source_unification: FAIL (Aedos 47.6% vs baseline 71.4%)
No-regression entity_disambiguation: FAIL (Aedos 47.8% vs baseline 95.7%)
No-regression multi_hop_distribution: FAIL (Aedos 40.0% vs baseline 95.0%)
No-regression predicate_translation: FAIL (Aedos 53.6% vs baseline 96.4%)
No-regression principled_abstention: PASS (Aedos 100.0% vs baseline 35.0%)
Significant improvement (>= +20pp) on >= 4 of 6 modes: FAIL (2/6 modes)