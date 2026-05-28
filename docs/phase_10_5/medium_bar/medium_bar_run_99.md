# Aedos v0.15 Medium-Bar Evaluation Results

## Aedos v0.15
Total cases: 11
Accuracy: 27.3% (3/11)
False-verified rate: 0.0% (0)
False-abstain rate: 63.6% (7)

Per-failure-mode breakdown:
  cross_source_unification: 50.0% (1/2)
  entity_disambiguation: 50.0% (2/4)
  multi_hop_distribution: 0.0% (0/2)
  predicate_translation: 0.0% (0/3)

## LLM-Only Baseline
Total cases: 11
Accuracy: 100.0% (11/11)
False-verified rate: 0.0% (0)
False-abstain rate: 0.0% (0)

Per-failure-mode breakdown:
  cross_source_unification: 100.0% (2/2)
  entity_disambiguation: 100.0% (4/4)
  multi_hop_distribution: 100.0% (2/2)
  predicate_translation: 100.0% (3/3)

## Comparison
Accuracy delta: -72.7%
False-verified delta: +0.0%

## Phase 10.5 Acceptance Criteria
False-verified ≤ 5%: PASS (actual: 0.0%)
Accuracy ≥ baseline + 15pp: FAIL (delta: -72.7%)
No-regression cross_source_unification: FAIL (Aedos 50.0% vs baseline 100.0%)
No-regression entity_disambiguation: FAIL (Aedos 50.0% vs baseline 100.0%)
No-regression multi_hop_distribution: FAIL (Aedos 0.0% vs baseline 100.0%)
No-regression predicate_translation: FAIL (Aedos 0.0% vs baseline 100.0%)
Significant improvement (>= +20pp) on >= 4 of 6 modes: FAIL (0/4 modes)