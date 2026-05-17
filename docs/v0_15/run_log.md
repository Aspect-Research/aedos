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
- Test count: 39 new (434 cumulative; target was ~50 new; all pass)
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


