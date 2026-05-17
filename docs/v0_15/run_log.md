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


