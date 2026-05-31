# Aedos v0.16 — Change Specification

This directory is the **complete, implementation-ready change specification** for v0.16, produced from the forward-planning documents (`docs/v0_16_synthesis.md`, `docs/aedos_forward_planning_part_1.md`, `docs/aedos_forward_planning_part_2.md`) and the operator's decisions on them. It specifies, per file and per line, **what is deleted, what is added in its place, the role of each block, what it relates to, and the order of changes** — across the six code workstreams plus tests, deletions, and migrations.

**Status: specification only. No code has been changed.** Implementation follows operator confirmation.

## Read in this order

| Doc | Workstream | What it covers |
|---|---|---|
| [`00_overview_contract_ordering.md`](00_overview_contract_ordering.md) | — (spine, **authoritative**) | The shared interface contract / data model, cross-workstream reconciliations, global ordering + dependency graph, DB-migration plan, soundness-sensitive deletion ordering, test strategy, LOC accounting + checkpoint, observability, and the open decisions needing your sign-off. **Read this first; it governs the rest.** |
| [`01_substrate_predicate_map.md`](01_substrate_predicate_map.md) | WS1 | Multi-property `PredicateBinding`; Wikidata-ontology discovery (P2302/P1647/P1696/P1659); SLING fallback; evidence-arbitration `verify` (the P31-vs-P106 fix); delete `_CANONICAL_MAP` + synonym seed rows. |
| [`02_composition.md`](02_composition.md) | WS2 | Discover/verify split; generalized `verify_transitive_path`; premise-forward / bidirectional search; `predicate_distribution` gate→ranker; remove the depth-0 cap. |
| [`03_provenance_tms.md`](03_provenance_tms.md) | WS3 | Lazy per-claim AND/OR provenance term; D13 retractable KB verdicts; bounded `substrate_exceptions` nogood cache; lazy premise-retraction; rewrite of `retraction.py`/`contradiction_tracer.py`. |
| [`04_verify_every_claim.md`](04_verify_every_claim.md) | WS4 | `AbstentionReason` enum; `Claim.abstention_reason`; `_build_claim` never drops; walker pre-lookup short-circuit; `not_checkworthy` quiet designation. |
| [`05_corrections_observability.md`](05_corrections_observability.md) | WS5 | `ClaimVerdict.contradicting_value`; emit the corrected value (`fetch_label`); conditional verdicts; `trace_to_human` + `claim_observability` + endpoint surfaces. |
| [`06_temporal.md`](06_temporal.md) | WS6 | Start/end as separate date-in-object claims; interval-from-events resolver (three-valued endpoint arithmetic); P580/P582 qualifier surfacing. |
| [`07_tests.md`](07_tests.md) | WS7 | Test-impact inventory per workstream; the **test-agent assignment map** (TA-1…TA-6 + TA-CAL, separate from code-agents); calibration-corpus discipline. |
| [`08_deletions_migrations_ordering.md`](08_deletions_migrations_ordering.md) | WS8 | The complete deletion list (with orphan checks); the additive/idempotent DB-migration plan; the global change ordering + dependency graph; LOC-delta accounting; soundness-sensitive deletion ordering. |

## The shape of the change

- **Architectural cuts:** scalar single-property → multi-property substrate (the keystone); gated BFS → discover/verify composition; dormant eager retraction → bounded lazy provenance/premise-retraction.
- **Bounded fixes:** verify-every-claim (quiet designations); emit-corrected-value; conditional verdicts; temporal T1; observability surfaces.
- **Deletions (net LOC target down):** `_CANONICAL_MAP` + ~21 synonym seed rows; the geo hardcode cluster (`CONTINENT_QIDS`, `_location_disjoint`, `_GEO_REGION_TYPES`); the depth-0 cap; the persona-subject SQL guard; the distribution gate + hand-seeded rubric; the four `_build_claim` drops; `contradiction_tracer.py` + the retraction eager loop. (Net-down is contingent on WS2 *replacing* rather than *layering* the verifier's scalar ladder — see `00` §0.7.)
- **Deferred (documented, not touched in v0.16):** Layer 2 Router/Validator removal; Phase E model re-selection; evaluation-harness building; Python-tier capability expansion.

## Confirmation gate

Per the operator directive, this set is for review. The open decisions in `00` §0.11 need sign-off. On approval, implementation proceeds in the Phase 0→6 order in `00` §0.5, with separate code-agents and test-agents, a green `pytest tests/` after each phase, the post-WS2 LOC checkpoint, and the soundness-sensitive deletions (SS1–SS5) landing only after their replacements with regression pins in place.
