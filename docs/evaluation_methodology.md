# Aedos v0.15 Evaluation Methodology

## Purpose

The medium-bar evaluation measures Aedos v0.15's improvement over an LLM-only baseline on a curated set of claims designed to expose the six failure modes that Aedos's architecture addresses.

## Evaluation philosophy

Aedos is **not** a general-purpose question-answering system. It is a claim-verification engine that applies architectural discipline — Tier U assertions, KB grounding, Python computation — to avoid false verifications. The evaluation is therefore deliberately biased toward the failure modes where this architecture provides an advantage. This bias is intentional and documented.

**What Aedos optimizes for:** zero false verifieds (sound verification). Abstaining when no grounding is available is correct behavior, not failure.

**What Aedos does not optimize for:** recall. The system will abstain on claims it cannot verify even if a sophisticated human would consider them obviously true. The false-abstain rate is a secondary concern; the false-verified rate is the primary constraint.

## The six failure modes

The test set is organized around six failure modes that the architecture targets:

| Mode | Description | Addressed by |
|---|---|---|
| `multi_hop_distribution` | Claims requiring multi-hop inference (city→country→continent) | Walker BFS + predicate distribution oracle |
| `cross_source_unification` | Claims requiring evidence from multiple sources (Tier U + KB + Python) | Walker cross-source integration |
| `entity_disambiguation` | Claims where entity resolution determines correctness | EntityResolver + KB candidate scoring |
| `predicate_translation` | Claims requiring accurate slot-to-qualifier mapping | PredicateTranslation oracle + seed pack |
| `belief_revision` | Claims that contradict user-asserted context (Tier U) | Contradiction detection + retraction |
| `principled_abstention` | Claims the system should refuse to verify (opinions, future predictions, dynamic data) | Triage layer + abstention discipline |

## Test set construction

The test set (`tests/evaluation/medium_bar_test_set.jsonl`) contains 122 cases distributed as follows:

| Failure mode | Count | Verified | Contradicted | Abstain |
|---|---|---|---|---|
| multi_hop_distribution | 20 | 17 | 3 | 0 |
| cross_source_unification | 18 | 14 | 0 | 4 |
| entity_disambiguation | 16 | 14 | 2 | 0 |
| predicate_translation | 18 | 13 | 5 | 0 |
| belief_revision | 10 | 4 | 2 | 4 |
| principled_abstention | 20 | 0 | 0 | 20 |
| bonus (mixed) | 20 | 18 | 2 | 0 |

**Intentional biases:**
- Principled abstention cases are deliberately heavy (20 of 122) because an LLM-only baseline tends to confabulate verified answers on opinion/future claims. This is where Aedos's abstention discipline provides maximum advantage.
- The test set does not include ordinary factual claims with no failure-mode relevance. These would inflate both Aedos and baseline scores equally.
- Cases are chosen for ground truth certainty: each case has a definitively correct answer per authoritative KB data or logical necessity.

## Baseline

The LLM-only baseline sends a one-shot prompt asking the LLM to evaluate each statement's correctness. The prompt instructs the LLM to respond VERIFIED, CONTRADICTED, or ABSTAIN. The LLM has no architectural support (no KB grounding, no Tier U, no Python computation). The baseline uses the same underlying LLM as Aedos.

This baseline choice is conservative and favorable to the baseline: the LLM has general world knowledge and will correctly answer many KB-grounded facts from pre-training. The advantage Aedos provides is on the boundary cases — temporal claims, off-by-one errors, multi-hop chains, entity disambiguation, and principled abstention.

## Metrics

**Primary:** false-verified rate = (incorrect verdicts marked "verified") / total. This is the soundness commitment. Phase 10.5 acceptance: ≤ 5%.

**Secondary:**
- Overall accuracy = correct / total.
- False-abstain rate = (verified claims that Aedos abstained on) / total verified claims.
- Per-failure-mode accuracy breakdown.

**Acceptance thresholds (Phase 10.5):**
1. Aedos false-verified rate ≤ 5%.
2. Aedos overall accuracy ≥ baseline + 15 percentage points on the curated test set.
3. Aedos accuracy ≥ baseline on every failure mode (no regression on any mode).
4. Aedos accuracy significantly higher (≥ +20pp) on at least 4 of 6 failure modes.

## Execution

The benchmark runner is at `tests/evaluation/benchmark.py`. Execution requires:
- `RUN_LIVE_TESTS=1`
- `RUN_LIVE_KB=1`
- A valid `ANTHROPIC_API_KEY`
- Live Wikidata access

**Deferred to Phase 10.5.** Results will be documented in `docs/evaluation_results.md` once Phase 10.5 completes.

## Limitations

1. The test set is curated and biased — it does not measure real-world claim distribution.
2. Wikidata coverage affects KB-routed claims; some entities (Williams College, Williamstown) may have incomplete Wikidata records.
3. The "belief_revision" cases depend on Tier U assertions about "Asa" that must be seeded into the deployment for the test to be meaningful. The Phase 10.5 operator should load the Asa-context assertions before running the benchmark.
4. LLM non-determinism means baseline results may vary between runs. Three runs are recommended; report the median.
