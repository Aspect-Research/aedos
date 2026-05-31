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

> **Status:** **v0.16.0** is the current release — a structural rebuild on the
> v0.15 foundation (multi-property substrate, discover/verify composition,
> partial-TMS provenance, verify-every-claim, per-claim corrections, temporal T1),
> built, reviewed, medium-bar evaluated, and merged to `main`. Soundness holds
> (0% false-verified); see [Status](#status).

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

> The `/chat` path runs the verification pipeline end-to-end — it extracts and
> verifies the model's draft response before returning it. The verification
> pipeline is also exercised directly through the test and evaluation harnesses
> below. The full internal deployment — chat interface, testing environment,
> real-use feedback — is the next phase of work.

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

The live medium-bar evaluation and the live calibration run were the
operator-supervised Phase 10.5 work that gated the v0.15.0 release; see
[`docs/phase_10_5_runbook.md`](docs/phase_10_5_runbook.md) and
[`docs/evaluation_methodology.md`](docs/evaluation_methodology.md) for the
methodology, and [Status](#status) for the result.

---

## Architecture

[`docs/architecture.md`](docs/architecture.md) is the authoritative
specification — the eight load-bearing principles, the five layers in detail,
the KB protocol, the derivation walker, and the retraction mechanism. Everything
in this build is built to that document; where code and architecture conflict,
the architecture is correct.

---

## Status

**v0.16.0** is the current release. It is a structural rebuild on the v0.15
foundation — multi-property substrate, discover/verify composition, partial-TMS
provenance, verify-every-claim, per-claim corrections, temporal T1 — built,
reviewed (two adversarial review rounds + three patch rounds), medium-bar
evaluated, and merged to `main`. v0.15.0 was the prior release; the next line of
work is v0.16.1.

What is verified: the architecture is fully implemented across all five layers;
the mocked test suite is green (~1,390 tests, plus the calibration corpora behind
`--run-calibration`); no path produces a false `verified`. The v0.15 calibration
corpora measured **zero §3.2 false-verifieds across 668 case-mode invocations**,
and the v0.16 live medium-bar run held the **false-verified rate at 0%**.

**Soundness over coverage — validated across both versions.** The v0.16 medium-bar
run (122 cases, live) scored **60.7% accuracy with 0% false-verified**, against an
LLM-only baseline of ~76% accuracy but ~12% false-verified — and improved on v0.15
(57.4% → 60.7% accuracy, 48.8% → 44.0% false-abstain) while holding the 0%
false-verified invariant. See
[`docs/v0_16/10_medium_bar_step1.md`](docs/v0_16/10_medium_bar_step1.md) and
[`docs/v0_16/11_step2_build_examine.md`](docs/v0_16/11_step2_build_examine.md).

Known remaining work (the v0.16 planning backlog and the completed v0.16 change
specs are archived under
[`docs/archive/v0.16_planning.md`](docs/archive/v0.16_planning.md) and
[`docs/archive/v0_16/`](docs/archive/v0_16/)):

- **Multi-hop derivation depth & discovery latency.** Multi-hop locative chains
  still abstain in some cases, and the discover/verify composition's per-neighbor
  KB verification makes live discovery markedly slower than v0.15 — both are
  recorded in the v0.16 evaluation findings.
- **The retraction cascade and re-derivation.** Over-time soundness holds across
  process restarts, but the full verdict-to-dependent-verdict cascade and
  re-derivation from remaining premises are not yet implemented.
- **Coverage refinements.** Free-text class subsumption, compound-claim semantics,
  the `_location_disjoint` shared-continent (country-vs-country) path under the
  walk budget, and cold-start oracle calibration on novel predicates remain.

These gaps preserve soundness — they cause false *abstains*, never false
*verifieds*.

---

## Development

The codebase is laid out by pipeline layer:

```
src/aedos/
  layer1_extraction/    extraction, normalization, decomposition, temporal, triage
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
[`docs/archive/v0.15_build_log/`](docs/archive/v0.15_build_log/), alongside the
v0.16 change specs and the Phase A–H build logs in [`docs/archive/`](docs/archive/).
It is the institutional record of how the system was built and why specific
decisions were made; consult it when investigating a behavior that traces back to
an earlier design choice.

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
