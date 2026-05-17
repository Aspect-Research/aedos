# Phase 0 Plan — Foundation

## Summary

Phase 0 produces a runnable but empty v0.15 system. When it completes, the process boots cleanly, the database schema is in place, the LLM client is wired, the sandbox is functional, the HTTP cache layer exists, a FastAPI health endpoint responds, and the audit log records and retrieves events. No claim processing occurs. All later phases have a concrete foundation to build on.

## File list (created or modified)

### New directories
- `src/aedos_v0_15/` — v0.15 package root
- `src/aedos_v0_15/layer1_extraction/`
- `src/aedos_v0_15/layer2_routing/`
- `src/aedos_v0_15/layer3_substrate/`
- `src/aedos_v0_15/layer4_sources/`
- `src/aedos_v0_15/layer5_result/`
- `src/aedos_v0_15/deployment/`
- `src/aedos_v0_15/audit/`
- `src/aedos_v0_15/llm/`
- `src/aedos_v0_15/utils/`
- `tests/v0_15/`
- `tests/v0_15/unit/`
- `tests/v0_15/integration/`
- `tests/v0_15/smoke/`
- `tests/v0_15/calibration/`
- `tests/v0_15/fixtures/`
- `tests/v0_15/fixtures/wikidata/`
- `tests/v0_15/evaluation/`
- `tests/v0_15/cold_start/`
- `docs/v0_15/`
- `seeds/v0_15/`

### Substantive new files (Phase 0)
- `src/aedos_v0_15/__init__.py`
- `src/aedos_v0_15/app.py` — FastAPI server with health endpoint + lifespan
- `src/aedos_v0_15/config.py` — deployment configuration dataclass
- `src/aedos_v0_15/database.py` — SQLite schema creation + connection management
- `src/aedos_v0_15/audit/log.py` — audit log writes + queries
- `src/aedos_v0_15/llm/client.py` — LLM client (lifted from v0.14 with v0.15 model defaults)
- `src/aedos_v0_15/utils/sandbox.py` — Python sandbox with import allow-list enforcement
- `src/aedos_v0_15/utils/http_cache.py` — httpx-based HTTP client with ETag + in-process LRU
- `tests/v0_15/conftest.py` — pytest fixtures: db, llm_client_mock, kb_mock, temp_audit_log
- `tests/v0_15/unit/test_database.py`
- `tests/v0_15/unit/test_audit_log.py`
- `tests/v0_15/unit/test_llm_client.py`
- `tests/v0_15/unit/test_sandbox.py`
- `tests/v0_15/unit/test_http_cache.py`
- `tests/v0_15/unit/test_app.py`

### Placeholder stub files (populated in later phases)
All subdirectory `__init__.py` files + placeholder module stubs for:
- `src/aedos_v0_15/layer1_extraction/{extractor,normalization,decomposition,temporal,triage}.py`
- `src/aedos_v0_15/layer2_routing/{router,validator}.py`
- `src/aedos_v0_15/layer3_substrate/{resolver,predicate_translation,subsumption,predicate_distribution,consistency}.py`
- `src/aedos_v0_15/layer4_sources/{tier_u,kb_protocol,kb_wikidata,python_verifier,walker}.py`
- `src/aedos_v0_15/layer5_result/{aggregator,trace,retraction}.py`
- `src/aedos_v0_15/deployment/chat_wrapper.py`

### Empty calibration corpus files (populated in later phases)
- `tests/v0_15/calibration/{extraction,predicate_metadata,kb_mapping,entity_resolution,subsumption,predicate_distribution,derivation,python_verification,temporal_scope,consistency_check,intervention}_corpus.jsonl`

## Test plan

Six test modules targeting ~30 tests total:

| Module | What it covers | Target count |
|---|---|---|
| `test_database.py` | All 6 tables exist; schema constraints enforced (NOT NULL, CHECK, UNIQUE) | ~8 |
| `test_audit_log.py` | log_event writes rows; query_events filters by event_type and subject; audit log is queryable | ~5 |
| `test_llm_client.py` | Mock transport: chat, chat_stream, extract_with_tool, rewrite; purpose → model dispatch; provider routing (claude vs gpt) | ~8 |
| `test_sandbox.py` | Safe code executes; `import os` refused; `import subprocess` refused; `datetime.date.today()` succeeds; timeout enforced; exception capture works | ~5 |
| `test_http_cache.py` | Cache hit avoids second HTTP request; ETag conditional-request sent on cache hit; LRU eviction; TTL expiry | ~5 |
| `test_app.py` | Health endpoint returns `{"status": "ok", "version": "0.15.0"}`; lifespan starts without error | ~3 |

## Calibration corpus

No calibration corpus in Phase 0. Empty JSONL files are created as placeholders for later phases.

## Ambiguities

See `docs/v0_15/phase_0_ambiguities.md`.

## Notes on lifted components

The LLM client is lifted from `src/llm_client.py`. Key changes for v0.15:
- Import path changes from `src.cost` to an inline cost stub (the cost module is a v0.14 artifact; v0.15 tracks budget via the walker's resource budget, not a per-call cost ledger)
- Model defaults updated: `chat` → `claude-haiku-4-5`, substrate oracle calls → `gpt-4.1-mini`, extraction → `gpt-4.1`
- The `complete` method is added as an alias for `chat` for convenience

The sandbox is revised from v0.14's subprocess-only approach. v0.15 adds AST-based import scanning to enforce the allow-list before execution.
