# Aedos v0.15 Cold-Start Deployment Guide

## Overview

Aedos v0.15 is fully functional with an empty substrate. A freshly deployed system with no pre-loaded seeds will generate predicate translation rows inline on first use. This guide covers both zero-seed and seed-pack deployments.

## Prerequisites

- Python 3.11+
- SQLite 3.37+ (bundled with Python's standard library)
- An LLM API key (set `ANTHROPIC_API_KEY` or equivalent)
- Optional: Wikidata internet access for KB-routed claims

## Installation

```bash
git clone <repo>
cd aedos
pip install -e .
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes (for live LLM) | — | API key for the LLM client |
| `AEDOS_DB_PATH` | No | `aedos.db` | Path to the SQLite database file |
| `AEDOS_LLM_MODEL` | No | `claude-sonnet-4-6` | LLM model ID |
| `AEDOS_WALKER_MAX_DEPTH` | No | `4` | Maximum BFS depth for the derivation walker |
| `AEDOS_WALKER_MAX_LLM_CALLS` | No | `8` | LLM call budget per claim walk |
| `AEDOS_CIRCUIT_BREAKER_THRESHOLD` | No | `3` | Consistency conflicts before circuit breaker fires |

## Database initialization

The database is initialized automatically on first startup. To initialize manually:

```bash
python -c "from aedos.database import open_db; open_db('aedos.db')"
```

## Starting the server

```bash
uvicorn aedos.app:app --host 0.0.0.0 --port 8000
```

Verify the server is running:

```bash
curl http://localhost:8000/health
```

Expected response: `{"status": "ok", "version": "0.15.0"}`

## Zero-seed deployment

A zero-seed deployment starts with an empty `predicate_translation` table. The system is fully functional; on first encounter of any predicate, the LLM is consulted to generate the translation row inline.

**Trade-offs:**
- First use of any predicate incurs an additional LLM call (for translation row generation). Expect 5-15s extra latency on the first occurrence.
- Subsequent uses of the same predicate are served from the cache.
- Zero false verifieds: the system abstains rather than guesses if translation fails.

**Verification of zero-seed correctness:**
The test scaffolding at `tests/cold_start/test_zero_seed_correctness.py` verifies structural correctness. For live acceptance testing:

```bash
RUN_LIVE_TESTS=1 RUN_LIVE_KB=1 pytest tests/cold_start/test_zero_seed_correctness.py -v
```

Expected results: all 10 cases produce expected verdicts. First-claim latency ≤ 30s. Tenth-claim latency ≤ 5s.

## Seed-pack deployment (optional)

The optional seed pack pre-populates 65 common predicate translation rows, covering roles, locations, kinship, categorical membership, mereological relations, quantitative properties, and event predicates.

**Loading seeds:**

```bash
python seeds/load_seeds.py --db-path aedos.db
```

The load is idempotent (safe to run multiple times). Seeds are tagged with their generation date.

**Trade-offs:**
- Reduces first-use latency for the 65 covered predicates.
- Does not constrain the system: predicates not in the seed set are still handled by inline generation.
- Seeds may be stale if Wikidata property IDs change; check `seeds/SEED_VERSION.txt` for the last-reviewed date.

## Monitoring the audit log

The audit log records all substrate mutations and consistency events. Use the API endpoints to query:

```bash
# Substrate rows created
curl http://localhost:8000/audit/substrate-rows?limit=20

# Consistency violations detected
curl http://localhost:8000/audit/consistency-checks?limit=20

# Circuit breaker triggers
curl http://localhost:8000/audit/circuit-breakers?limit=20

# Rows retracted by the consistency checker
curl http://localhost:8000/audit/retractions?limit=20
```

## Interpreting circuit breaker reports

A circuit breaker fires when the same consistency conflict recurs `AEDOS_CIRCUIT_BREAKER_THRESHOLD` (default: 3) times. This indicates a predicate translation row is repeatedly generating conflicting substrate knowledge.

When a circuit breaker fires:
1. Both conflicting rows are retracted.
2. The `consistency_circuit_breaker` table records the conflict signature and cycle count.
3. Future attempts to generate the same conflicting rows are blocked.

If you see frequent circuit breaker triggers for the same predicate, the predicate translation oracle is generating unstable mappings. Options:
- Add a hand-curated seed for that predicate with an explicit `slot_to_qualifier` mapping.
- Increase the circuit breaker threshold if the conflicts are expected (e.g., multi-value predicates).

## Investigating consistency violations

Consistency violations fall into three classes:

1. **transitive_equivalence_violation**: Two predicates map to the same Wikidata property but specify different `slot_to_qualifier` schemas. This means one translation is wrong. Check the `predicate_translation` table for the two conflicting predicates and manually retract the incorrect one.

2. **contradicting_subsumption**: Two subsumption oracle rows conflict (A subsumes B and B subsumes A, or A subsumes B and A is unrelated to B). Retract the incorrect row.

3. **conflicting_distribution**: Two predicate distribution oracle rows give incompatible verdicts. Retract the incorrect row.

The retraction API logs all retractions; use `GET /audit/retractions` to review recent retractions and their reasons.

## Health checks and observability

- `GET /health` — liveness check; returns version string.
- `GET /audit/substrate-rows` — substrate health; high rate of `row_created` events indicates normal knowledge accumulation.
- `GET /audit/circuit-breakers` — stability indicator; any events here warrant investigation.
