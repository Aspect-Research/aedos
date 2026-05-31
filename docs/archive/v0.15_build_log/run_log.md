# Aedos v0.15 — Overnight Build Run Log

This file records one entry per phase of the unattended overnight build.

---

## Phase 0 — Foundation

- Commit SHA: c2ec18c
- Tag: v0.15-phase-0-complete
- Test count: 78 (target was ~30; all pass)
- Calibration corpus: none (empty placeholder files created)
- Ambiguities resolved this phase: 5 (see phase_0_ambiguities.md)
- Blockers: none
- One-sentence summary: Bootstrapped the full v0.15 directory structure, database schema (7 tables matching architecture §5.2 and §6.1), LLM client (lifted from v0.14), Python sandbox (with AST-based import allow-list), HTTP/LRU cache, FastAPI health endpoint with lifespan, and audit log infrastructure, with 78 passing tests.


## Phase 1 — Extraction (Layer 1)

- Commit SHA: 9f8728a
- Tag: v0.15-phase-1-complete
- Test count: 122 new (200 cumulative; target was ~80 new / ~158 cumulative; all pass)
- Calibration corpus: tests/v0_15/calibration/extraction_corpus.jsonl — 60 cases across 5 sub-categories (normalization 15, decomposition 10, temporal 15, hard-claim discipline 7, first-person 10)
- Ambiguities resolved this phase: 5 (see phase_1_ambiguities.md)
- Blockers: none
- One-sentence summary: Implemented the full Layer 1 extraction stack — Extractor class with mocked-LLM test roundtrip, predicate normalization (canonical map + snake_case fallback), multi-participant event decomposition with shared reified_event_id, temporal scope extraction (explicit, implicit past-tense → before_present sentinel, relative scope, future rejection), verifiability triage (5 rule categories), and hard-claim / first-person / source-text discipline in post-processing, with 122 new passing tests.


## Phase 2 — Predicate Translation Oracle

- Commit SHA: f397e6b
- Tag: v0.15-phase-2-complete
- Test count: 39 new (239 cumulative; target was ~50 new / ~160 cumulative; all pass)
- Calibration corpus: tests/v0_15/calibration/predicate_metadata_corpus.jsonl — 80 cases across 5 sub-categories (user_authoritative 20, python 15, kb_resolvable 30, abstain 10, ambiguous 5)
- Ambiguities resolved this phase: 5 (see phase_2_ambiguities.md)
- Blockers: none
- One-sentence summary: Implemented the PredicateTranslation oracle with cold-cache LLM generation (INSERT OR REPLACE for retracted row re-generation), warm-cache lookup with used_count/last_consulted_at update, retraction (sets retracted_at, excludes from future consults), query_neighbors (conflict detection by kb_property), and audit log integration for row_created / row_retracted / row_generation_failed events, with 39 new passing tests.


## Phase 3 — Routing + Tier U

- Commit SHA: 72edfb6
- Tag: v0.15-phase-3-complete
- Test count: 49 new (288 cumulative; target was ~80 new; all pass)
- Calibration corpus: tests/v0_15/calibration/temporal_scope_corpus.jsonl — 40 cases across 5 sub-categories (explicit_scope 10, implicit_past 10, relative_scope 10, no_markers 5, future_rejection 5)
- Ambiguities resolved this phase: 5 (see phase_3_ambiguities.md)
- Blockers: none
- One-sentence summary: Implemented Layer 2 router (four routes: user_authoritative/python/kb_resolvable/abstain, structural anomaly detection, stub flag), Validator (user_subject_required/distinct_slots/object_type heuristics), and Tier U with three-stage lookup (literal, entity-resolution stub, predicate-translation broadening), idempotent writes, contradiction detection/closure, temporal scope enforcement (BEFORE_PRESENT sentinel, past valid_until), and retraction, with 49 new passing tests and 14 integration tests covering the full claim→router→Tier U roundtrip.


## Phase 4 — KB Protocol + Wikidata Adapter

- Commit SHA: d92adac
- Tag: v0.15-phase-4-complete
- Test count: 64 new (352 cumulative; target was ~90 new; all pass)
- Calibration corpus: entity_resolution_corpus.jsonl (50 cases: unambiguous 20, ambiguous 15, type_filter 10, no_match 5), kb_mapping_corpus.jsonl (40 cases: kb_resolvable 30, qualifier_mapping 10)
- Ambiguities resolved this phase: 5 (see phase_4_plan.md)
- Blockers: none
- One-sentence summary: Implemented KBProtocol interface (LocalContext, ResolutionCandidate, Statement, SubsumptionResult dataclasses), WikidataAdapter (fixture-backed entity search, SPARQL statement lookup, subsumption traversal, FixtureNotFoundError for clean test failures), EntityResolver (cache-first resolution with entity_resolution_cache, candidate scoring and LLM-mediated selection), KBVerifier (full 6-step pipeline: predicate translation → entity resolution → KB lookup → qualifier scope comparison → verdict), and a Wikidata fixture set at tests/v0_15/fixtures/wikidata/ covering 10 fixture files with a README, with 64 new passing tests across protocol, adapter, resolver, verifier, and KB path integration.


## Phase 5 — Subsumption + Predicate Distribution Oracles

- Commit SHA: 5b751a2
- Tag: v0.15-phase-5-complete
- Test count: 43 new (395 cumulative; target was ~60 new; all pass)
- Calibration corpus: subsumption_corpus.jsonl (60 cases: kb_resolvable 30, mixed_namespace 20, unrelated 10), predicate_distribution_corpus.jsonl (50 cases: distributes_up 12, distributes_down 8, both 5, neither 25)
- Ambiguities resolved this phase: 5 (see phase_5_plan.md)
- Blockers: none
- One-sentence summary: Implemented SubsumptionOracle (three-priority resolution: KB-mediated for wikidata/wikidata pairs, substrate-row lookup for mixed/aedos-only, LLM cold-cache generation with INSERT OR REPLACE, retraction, query_neighbors), PredicateDistributionOracle (four-verdict DistributionVerdictType enum, lookup-first, LLM cold-cache via extract_with_tool, retraction, query_neighbors), and the Substrate facade dataclass wiring all four components (resolver, predicate_translation, subsumption, predicate_distribution) for uniform walker access, with 43 new passing tests and a cross-oracle integration suite.


## Phase 6 — Derivation Walker

- Commit SHA: 7b1a2b7
- Tag: v0.15-phase-6-complete
- Test count: 39 new (434 cumulative; target was ~80 new per the implementation
  plan Phase 6 and phase_6_plan.md:32 — 39 is ~51% under target; the original
  entry misquoted the target as "~50". Corrected during fix-up 1 per audit M6.)
- Calibration corpus: derivation_corpus.jsonl (50 cases: multi_hop_distribution 12, cross_source 10, entity_disambiguation 8, predicate_translation 8, belief_revision 6, abstention 6)
- Ambiguities resolved this phase: 3 (wall_clock budget negative-threshold trick; MockTransport purpose= dispatch; BudgetExceeded exception shape)
- Blockers: wall_clock budget tests initially used 0.0 threshold — fixed to -1.0 so elapsed > threshold is always true on first check
- One-sentence summary: Implemented the derivation Walker (BFS depth-4, cycle detection via canonical claim key, budget enforcement for wall_clock_seconds and max_llm_calls, polarity tracking, predicate-distribution gating on subsumption expansion), JustificationTrace structure (TraceNode, TraceEdge, polarity_trace, source_breakdown, walk_metadata, trace_to_json serialization), PythonVerifier stub (terminal=False until Phase 7), and WalkResult with BudgetConsumption, with 39 new passing tests across unit, integration, and calibration corpus files.


## Phase 7 — Python Verification Path

- Commit SHA: b924567
- Tag: v0.15-phase-7-complete
- Test count: 34 new (468 cumulative; target was ~40 new; all pass)
- Calibration corpus: python_verification_corpus.jsonl (30 cases: date_arithmetic 10, string_operations 8, numerical_comparison 6, list_set_operations 6)
- Ambiguities resolved this phase: 3 (no_terminal_result vs abstain semantics; string-only slot inputs; bool coercion for truthy non-bool returns)
- Blockers: none
- One-sentence summary: Implemented full PythonVerifier (LLM generates verify(subject, predicate, obj)->bool via PYTHON_VERIFY_TOOL, sandbox executes with real subprocess, stdout TRUE/FALSE determines verdict, all error cases return no_terminal_result), updated Walker to emit python trace edges and check verdict != "no_terminal_result" instead of terminal flag, and authored an adversarial 30-case calibration corpus covering date off-by-one traps (1900 leap year, non-leap Feb 29), string count adversarials (Mississippi, strawberry), and integer-vs-string sort pitfalls, with 34 new passing tests across unit and integration suites.


## Phase 8 — Layer 5 + Substrate-Internal Consistency Check

- Commit SHA: d63e1e4
- Tag: v0.15-phase-8-complete
- Test count: 54 new (522 cumulative; target was ~70 new; all pass)
- Calibration corpus: consistency_check_corpus.jsonl (25 cases: seeded_conflict_detection 10, retract_and_regenerate 8, circuit_breaker_trigger 7)
- Ambiguities resolved this phase: 3 (transitive_equivalence_violation detection uses cross-predicate same-kb_property logic not same-predicate UNIQUE-violating rows; UNIQUE constraints mean sub/dist conflict tests use synthetic ConsistencyResult; PythonVerifier in end-to-end tests uses no-client stub)
- Blockers: UNIQUE constraints on subsumption/predicate_distribution prevent inserting two conflicting rows via normal INSERT — resolved by (a) using predicate_translation for all check_on_write tests, (b) testing resolve_conflict/circuit_breaker with directly-constructed ConsistencyResult objects
- One-sentence summary: Implemented VerificationResult aggregator (per_claim_verdicts/traces, aggregate_metadata counts, consistency_warnings), ConsistencyChecker (three inconsistency classes: transitive_equivalence_violation across different predicates sharing same kb_property, contradicting_subsumption, conflicting_distribution; retract-both resolution; circuit_breaker with configurable threshold via consistency_circuit_breaker table), RetractionPropagator (session-local verdict-trace index, propagate_retraction returns VerdictRetraction list), ContradictionTracer (walks verdict traces to retract contributing rows), and end-to-end integration tests verifying the full pipeline, with 54 new passing tests.


## Phase 9 — Chat-Wrapper Deployment + Intervention Model

- Commit SHA: 300437e
- Tag: v0.15-phase-9-complete
- Test count: 30 new (552 cumulative; target was ~50 new; all pass)
- Calibration corpus: intervention_corpus.jsonl (30 cases: pass_through 10, abstain 8, correct 7, decline 5)
- Ambiguities resolved this phase: 3 (triage-only claims excluded from intervention total; empty claims list → pass_through; correct response uses fixed annotation not LLM rewrite in Phase 9)
- Blockers: none
- One-sentence summary: Implemented ChatWrapper with four-move intervention model (select_intervention: deterministic rules on >50% declining, any contradicted correcting, any abstained noting; build_response: pass_through/abstain/correct/decline text generation), InterventionType enum, ChatResponse dataclass, FastAPI POST /chat and GET /verification/{id} endpoints with lazy initialization of full pipeline from DB lifespan, in-memory verification store for session-local verification retrieval, and authored a 30-case intervention corpus covering all four intervention types with adversarial boundary cases (exactly 50% contradicted → correct not decline), with 30 new passing tests.


## Phase 10 — Hardening + Seeds + Cold-Start Docs + Evaluation Scaffolding

- Commit SHA: 1f617d1
- Tag: v0.15-phase-10-complete
- Test count: 71 new (623 cumulative; target was ~40 new; all pass; 1 skipped/deferred)
- Calibration corpus: medium_bar_test_set.jsonl (122 cases across 6 failure modes: multi_hop_distribution 20, cross_source_unification 18, entity_disambiguation 16, predicate_translation 18, belief_revision 10, principled_abstention 20, bonus 20)
- Ambiguities resolved this phase: 3 (SQLite NULL≠NULL in UNIQUE prevents INSERT OR REPLACE idempotency for null-kb_namespace seeds — fixed with DELETE+INSERT; zero-seed latency test uses >= 0 not > 0 for Windows timer granularity; benchmark structural self-test uses mock results not live run)
- Blockers: none
- One-sentence summary: Implemented the optional predicate translation seed pack (65 entries spanning 7 category groups, idempotent load_seeds.py with NULL-namespace fix), cold-start zero-seed test scaffolding (10 representative claims across all routing paths, deferred live execution), audit-log query endpoint tests (23 new integration tests confirming all four /audit/* endpoints return correct event types and respect limit parameter), medium-bar evaluation scaffolding (122-case test set across six failure modes, benchmark.py with AedosRunner/BaselineRunner/MetricsComputer/generate_report, structural self-test confirming harness wiring), and all three Phase 10.5 handoff documents (cold_start.md, evaluation_methodology.md, phase_10_5_runbook.md), with 71 new passing tests.


## Release Preparation — Minor Fixes (R1/R2/R3), Restructure, README

- Commits: `fixup-3.5: R1/R2/R3`; `Remove v0.14 code and tests`; `Promote aedos_v0_15 to aedos; restructure tests and seeds`; `Restructure docs; archive build history; write README`
- Tag: v0.15.0-rc.1 (release candidate; `v0.15.0` reserved for after Phase 10.5 passes its thresholds)
- Test count: 699 passed, 1 skipped, 11 deselected (+3 over the fixup-3 baseline of 696 — two R1 walker-trace tests, one R3 polarity test)
- Blockers: none
- One-sentence summary: Resolved the three Minor findings from the second re-audit — R1 (the walker now copies the D19 `lookup_inverted` flag onto the KB trace edge so the result-level trace records inverted lookups), R2 (the KB verifier's direction-ambiguous trace fields renamed to `value_resolved` / `value_entity` / `lookup_subject_unresolved` / `value_unresolved`), R3 (added a polarity×inverted-predicate test) — then deleted the v0.14 codebase and tests, promoted `src/aedos_v0_15/` to the primary `src/aedos/` package with all imports updated and `pythonpath` configured, promoted `tests/v0_15/` and `seeds/v0_15/` up one level, bumped the package version to `0.15.0rc1`, archived the v0.15 build history (phase plans, audit/fix-up/re-audit reports, run log, implementation plan) under `docs/v0.15_build_log/` while promoting the forward-looking docs and the architecture to `docs/`, wrote a repository README for cold readers, and tagged `v0.15.0-rc.1` as the pre-calibration release candidate.


