# Aedos

Aedos is a **verification layer for natural-language factual claims**. Given a
piece of text — typically a response from a chat LLM — it extracts the factual
claims the text makes, checks each one against grounded sources of belief, and
returns a per-claim verdict (`verified`, `contradicted`, or `abstained`) with a
complete justification trace. Its distinctive property is **soundness**: Aedos
never returns `verified` for a claim it cannot trace to a grounded premise. When
it cannot ground a claim, it abstains. The system is built to be silent rather
than wrong.

Aedos is a research prototype in the truth-maintenance-system (Doyle/Kleer)
tradition, specialized for the natural-language verification setting. It is
**not** a chatbot — it is the engine that sits behind one (or behind a
document checker, or a generated-content filter) and decides what is grounded.

> **Status:** v0.15.0-rc.1 — a release candidate. The architecture is complete
> and the mocked test suite is green, but the calibration measurement that gates
> a final `v0.15.0` has not yet run. See [Status](#status) for what is and is
> not verified.

---

## How it works

Aedos verifies text through a five-layer pipeline. Every claim is reduced to a
uniform shape — a binary relation `(subject, predicate, object)` with polarity
and optional temporal scope — so the same machinery handles every claim type.

1. **Extraction (Layer 1).** An LLM-mediated extractor reads `(text, context)`
   and produces structured claims. It normalizes predicates to a canonical form,
   decomposes multi-participant claims, extracts temporal scope, and drops
   inert prose that asserts nothing checkable.

2. **Routing (Layer 2).** Each claim is routed by its predicate's metadata to
   exactly one of four destinations: **Tier U** (context-stipulated premises —
   what the asserting party has declared true), **the KB** (a curated knowledge
   base — Wikidata in this build), **Python** (deterministic computation), or
   **abstain** (no authoritative source). There is no web-search route.

3. **The substrate (Layer 3).** The translation layer: an entity resolver and
   three oracles (predicate translation, subsumption, predicate distribution)
   that map a claim's vocabulary onto the languages of the premise sources. A
   substrate-internal consistency check detects and retracts contradictory
   rows.

4. **Sources and the derivation walker (Layer 4).** The walker is the inference
   engine. When a claim is not settled by a direct lookup, it performs a
   bounded breadth-first search over a composite premise graph, composing Tier U
   premises, KB statements, and Python results into a justification chain —
   gated by predicate distribution, with cycle detection and polarity tracking.

5. **The verification result (Layer 5).** Per-claim verdicts are aggregated
   into a single structured result: verdicts, justification traces, source
   breakdown, and aggregate metadata.

An optional **deployment layer** consumes the result. The chat-wrapper
deployment turns it into one of four categorical interventions on the LLM's
response — pass-through, abstain, correct, or decline — with no hedging.

The full specification is [`docs/architecture.md`](docs/architecture.md).

---

## Installation

Requires Python 3.10+.

```bash
git clone <repository-url> aedos
cd aedos

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[dev]"            # installs aedos + test dependencies
```

Create a fresh database with the v0.15 schema:

```bash
python scripts/reset_db.py         # creates ./aedos.db (override path: AEDOS_DB_PATH)
```

Optionally pre-load the predicate-translation seed pack. Seeds are a
convenience — they amortize first-use LLM cost — and are **not** required; a
zero-seed deployment is fully functional (see
[`docs/cold_start.md`](docs/cold_start.md)).

```bash
python seeds/load_seeds.py --db-path aedos.db
```

---

## Running it

### The chat server

```bash
uvicorn aedos.app:app --port 8000
```

Endpoints: `POST /chat` (verify and intervene on a message), `GET /health`,
`GET /verification/{id}` (inspect a verification), and read-only `GET /audit/*`
endpoints for substrate rows, consistency checks, circuit breakers, and
retractions.

> The deployed `/chat` path is currently **verification-inert** — see
> [Status](#status). The verification pipeline itself is exercised through the
> test and evaluation harnesses below.

### Environment variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Required. The chat slot defaults to an Anthropic model. |
| `OPENAI_API_KEY` | Required by default — substrate and extraction calls default to `gpt-*` models. |
| `AEDOS_DB_PATH` | SQLite database path. Defaults to `aedos.db`. |
| `RUN_LIVE_TESTS`, `RUN_LIVE_KB` | Set both to `1` to enable tests that make live LLM / live Wikidata calls. |
| `RUN_CALIBRATION` | With the two above, runs the calibration corpora against live services. |

By default the test suite runs fully mocked — no API keys, no network. Live
mode is opt-in through the gates above.

### Tests and evaluation

```bash
pytest tests/                                          # full mocked suite
pytest --run-calibration                               # calibration harness dry-run (11 corpora)
python -m tests.evaluation.benchmark --validate-harness # medium-bar harness wiring check
```

The live medium-bar evaluation and the live calibration run are operator-
supervised Phase 10.5 work; see [`docs/phase_10_5_runbook.md`](docs/phase_10_5_runbook.md)
and [`docs/evaluation_methodology.md`](docs/evaluation_methodology.md).

---

## Architecture

[`docs/architecture.md`](docs/architecture.md) is the authoritative
specification — the eight load-bearing principles, the five layers in detail,
the KB protocol, the derivation walker, and the retraction mechanism. Everything
in this build is built to that document; where code and architecture conflict,
the architecture is correct.

---

## Status

This is **v0.15.0-rc.1**, a pre-calibration release candidate.

What is verified: the architecture is fully implemented across all five layers;
the mocked test suite is green (~700 tests, plus 11 calibration corpora
collected behind `--run-calibration`); three post-audit fix-up cycles and two
re-audits have cleared the verification pipeline as sound — no path produces a
false `verified`.

What has **not** happened yet: the medium-bar evaluation that measures accuracy
against an LLM-only baseline, and the live calibration of the LLM-mediated
components, are Phase 10.5 work and have not run. A final `v0.15.0` tag is
reserved for after that measurement passes its thresholds.

Known capability gaps carried into v0.16 (full list in
[`docs/v0.16_planning.md`](docs/v0.16_planning.md)):

- **The deployed `/chat` endpoint is verification-inert.** The chat wrapper
  calls the extractor with a stale signature, so the endpoint currently passes
  responses through unverified. The verification pipeline itself is sound and
  is exercised directly by the benchmark and calibration runners; the chat
  wrapper integration is the gap.
- **Cross-source unification is partial.** The walker composes pre-seeded
  substrate taxonomy rows but cannot yet enumerate KB-sourced taxonomy
  neighbors on its own; cross-source derivation chains need their subsumption
  steps pre-seeded.
- **Retraction propagation is single-hop and session-local.** The
  consistency check and retraction wiring work within a process; the
  verdict-to-dependent-verdict cascade and cross-process persistence are not
  yet implemented.

These gaps preserve soundness — they cause false *abstains*, never false
*verifieds*.

---

## Development

The codebase is laid out by pipeline layer:

```
src/aedos/
  layer1_extraction/    extraction, normalization, decomposition, temporal, triage
  layer2_routing/       the router and structural validator
  layer3_substrate/     entity resolver, the three oracles, consistency checker
  layer4_sources/       Tier U, the KB protocol + Wikidata adapter, Python
                        verifier, the derivation walker
  layer5_result/        aggregator, justification traces, retraction, contradiction tracing
  llm/                  the LLM client
  audit/  utils/        audit log; HTTP cache and Python sandbox
  app.py                FastAPI server
  pipeline.py           shared pipeline assembly
```

Run the suite with `pytest tests/` (`tests/unit/`, `tests/integration/`,
`tests/calibration/`, `tests/evaluation/`, `tests/cold_start/`). The suite is
mocked by default and needs no API keys or network.

The complete build history — phase plans, audit reports, the three fix-up
cycles, and the two re-audits — is archived under
[`docs/v0.15_build_log/`](docs/v0.15_build_log/). It is the institutional record
of how v0.15 was built and why specific decisions were made; consult it when
investigating a behavior that traces back to a v0.15 design choice.

---

## The soundness commitment

Aedos's central architectural claim is one sentence:

> When Aedos says a claim is verified, that claim traces to a grounded source.
> When Aedos cannot ground a claim, it abstains rather than guessing.

Operationally, this means Aedos accepts a real cost: it abstains on a wider
class of inputs than a system that reasons from an LLM's training-data priors
or from web search. That cost is intentional. In domains where a false
verification is more expensive than an abstention — medical, legal, scientific,
regulatory, enterprise factual chat — the trade is the right one. Aedos commits
to never manufacturing grounding, and every abstention is a refusal to do so.
