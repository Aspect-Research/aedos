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

