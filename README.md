# Aedos

A claim-verification and conversational-memory research prototype.

The thesis: **correctness is a property of the system, not the model.** Aedos sits between a user and an LLM, extracts every factual claim the model makes into structured form, and routes that claim through a five-layer verification stack backed by a verified store that grows monotonically more correct over the course of a conversation. The system performs bounded inference — equivalence, subsumption, derivation — over its store, but does not attempt open-ended reasoning. Verification is the source of truth; memoization compounds verification's value over time.

## The five layers

```
Layer 1 — extraction       text → structured claims (pattern + slots)
Layer 2 — routing          rule-based validator + LLM router (memoized)
Layer 3 — substrate        four oracle classifiers, all memoized
Layer 4 — lookup           Tier U → Tier W → derivation → fresh
Layer 5 — decision         intervention planner + corrector
```

**The four-oracle substrate** is the architectural commitment. Every semantic judgment is reduced to a small label set:

| Oracle | Shape | Labels | Calibration |
|---|---|---|---|
| `predicate_equivalence`  | symmetric pair, pattern-keyed              | equivalent / contradictory / distinct                                            | 0.967 vs 0.90 floor |
| `entity_equivalence`     | symmetric pair, case-sensitive             | same / different                                                                  | 0.978 vs 0.85 floor |
| `entity_taxonomy`        | directional (child, parent, relation_type) | child_subsumed_by_parent / parent_subsumed_by_child / equivalent / neither       | 0.966 vs 0.85 floor |
| `predicate_distribution` | singleton 4-tuple                          | distributes_up / distributes_down / both / neither                                | 0.977 vs 0.85 floor |

Classifications are memoized at the SQL layer; warm-cache lookup cost is approximately zero LLM calls. Counts (`affirmed_count` / `contradicted_count`) increment only on independent external evidence — cache hits, oracle-mediated equivalence resolutions, and subsumption-derived matches do not increment.

**Derivation** is bounded BFS over the substrate. The walker composes facts from Tier U (user microtheory) and Tier W (world cache) via equivalence and subsumption chains. Two canonical cases ship working:

- *cheetahs-via-derivation*: stored `dislikes(user, animals)` + `cheetah is_a animal` (entity_taxonomy) + `dislikes distributes_down is_a` (predicate_distribution) ⇒ "user dislikes cheetahs" verified by walking three oracles.
- *Williamstown-via-derivation*: stored `lives_in(user, Williamstown)` + `Williamstown part_of Massachusetts` + `lives_in distributes_up part_of` ⇒ "user lives in Massachusetts" verified by walking two oracles.

Derived results are never persisted; every derivation is current with U + W state on every walk.

## Setup

```bash
git clone https://github.com/Aspect-Research/aedos && cd aedos
uv sync                        # or: pip install -e ".[dev]"
cp .env.example .env           # paste ANTHROPIC_API_KEY
                               # (and OPENAI_API_KEY if you want gpt-* purposes)
python -m src.app              # serves http://127.0.0.1:8000
```

The HTTP surface (`src/app.py`) exposes the v2 stack at `/`:

| Endpoint | Purpose |
|---|---|
| `GET /` | trace-UI shell (vanilla JS, no build step) |
| `POST /api/extract` | Layer 1 extractor over HTTP |
| `POST /api/dispatch-one` | Layer 2 → walker → Layer 5 for one structured claim |
| `GET /api/trace/{turn_id}` | pipeline events for a turn |
| `GET /api/routing-memo[/{pattern}/{predicate}]` | Layer 2 memo inspector |
| `GET /api/substrate/{oracle-slug}[/{key...}]` | per-oracle inspectors |
| `POST /api/substrate/{oracle-slug}/{row_id}/{affirm,contradict}` | operator-driven count updates (the only paths that mutate substrate counts) |
| `POST /api/reset` | wipe and recreate the v0.14 schema |

`/api/chat` (full turn-level orchestration) is v0.15 work. v0.14 ships dispatch-one + the inspectors.

## Eight verification states

| Status | Meaning | Layer 5 intervention |
|---|---|---|
| `verified` | verifier confirmed | pass-through |
| `contradicted` | verifier disconfirmed | replace |
| `user_asserted` | user assertion (Tier U) | pass-through |
| `unverifiable_in_principle` | routing decided no method applies | soften |
| `retrieval_inconclusive` | verifier ran, evidence thin | hedge |
| `retrieval_failed` | verifier broke; no evidence | noop |
| `unverifiable_pending_implementation` | verifier-side error | hedge with flag |
| `routing_anomaly` | Layer 2 validator rejected | noop, flag operator |

The enum is not collapsible — each state encodes a distinct downstream behavior.

## Per-purpose model routing

Every internal LLM call carries a `purpose` tag (`extractor:user`, `router`, `predicate_equivalence`, `corrector`, ...). The dispatcher (`src/llm_client.py`) resolves each purpose to a concrete model via `DEFAULT_MODEL_BY_PURPOSE`:

| Purpose | Default model |
|---|---|
| `chat` | `claude-haiku-4-5` |
| `extractor:user` / `extractor:assistant` | `gpt-4.1-mini` |
| `router` | `gpt-4.1-mini` |
| `prompt_builder` / `code_writer` / `retrieval_judge` / `corrector` | `gpt-4.1-mini` |
| `cache_classify` / `cache_scoping` / `cache_stability` | `gpt-4.1-nano` |

Override per-process via `AEDOS_MODEL_<purpose>=<model_id>`. Anthropic prompt caching (`cache_control: ephemeral`) and OpenAI automatic caching (gpt-4.1 / gpt-4o family) are both on; the cost ledger reads provider-specific cache-tier token counts.

## Running tests

```bash
pytest                         # ~1050 fast tests, LLM calls mocked
RUN_API_TESTS=1 pytest         # also runs live calibration tests
                               # (each oracle's gold corpus + scoping/stability)
```

Calibration corpora live under `tests/calibration/`. The four oracle floors and the 25-scenario derivation corpus are gated behind `RUN_API_TESTS=1`.

## Resetting state

```bash
python scripts/reset_db.py     # drops aedos.db and recreates the v0.14 schema
```

Or click **Reset DB** in the UI header — same endpoint.

## Optional environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key |
| `OPENAI_API_KEY` | — | Required when any purpose routes to a `gpt-*` model |
| `AEDOS_DB_PATH` | `aedos.db` | SQLite file location |
| `AEDOS_DECISION_THRESHOLD` | `0.5` | Layer 5 threshold T for hard vs soft verdicts |
| `AEDOS_MODEL_<purpose>` | — | Override any per-purpose model |

## Layout

```
src/
  app.py                     — FastAPI root: extract / dispatch-one / inspectors
  fact_store.py              — SQLite wrapper: facts + turns + pipeline_events +
                               4 oracle tables + Tier W (verification_cache) +
                               retrieval_cache + cache_invalidation_log
  session_markers.py         — "let's say for this conversation" detector
  llm_client.py              — multi-provider dispatcher (Anthropic + OpenAI)
  openai_client.py           — OpenAI SDK wrapper, shared cost ledger
  cost.py                    — per-call cost accounting
  layer1_extraction/         — extractor + 9-pattern registry
  layer2_routing/            — validator + LLM router + routing memo
  layer3_substrate/          — four oracle classifiers + classifier_base
  layer4_lookup/             — Tier U + Tier W + derivation + walker + fresh
  layer5_decision/           — confidence + intervention planner + corrector
  cache/                     — scoping + stability classifiers (fresh-tier
                               write gates)
  verifiers/                 — fresh-tier infrastructure
    types.py                 — VerificationOutcome / VerificationResult
    retrieval_verifier.py    — slot-aware Wikipedia retrieval + LLM judge
    comparative.py           — superlative-claim detector + query templates
    code_generation/         — neutral-prompt → code → sandbox → compare
    scrapers/                — Wikipedia MediaWiki client
static/                      — single-page UI (vanilla JS)
tests/                       — one test file per module + integration scenarios
scripts/
  reset_db.py                — wipe + recreate schema
patterns.yaml                — under src/layer1_extraction/; 9 structural patterns,
                               free-form predicates within each
```

## License

MIT — see `LICENSE`.
