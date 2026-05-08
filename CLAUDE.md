# CLAUDE.md — guidance for Claude Code sessions in this repo

## What this is

Aedos is a claim-verification and conversational-memory research prototype.
The thesis: correctness is a property of the system, not the model. Aedos
sits between a user and an LLM, extracts each factual claim into structured
form, and routes each claim through a five-layer verification stack backed
by a verified store that grows monotonically more correct over the
conversation. Bounded inference (equivalence, subsumption, derivation) is
performed over the store; open-ended reasoning is delegated to the chat
model.

See `README.md` for the user-facing introduction.

## Priorities (in order)

1. **Clarity.** Every function readable in one sitting.
2. **Observability.** Every pipeline stage writes a `pipeline_events` row.
   No silent failures.
3. **Ease of modification.** Predicates are free-form within patterns.
   Tests are narrow.

Explicit non-goals: performance, scale, cross-conversation state, general
commonsense reasoning, migration tooling.

## The seven architectural principles

These are load-bearing. Every design decision is downstream of them.

1. **Verification is upstream of memoization.** Nothing enters the verified
   store unless it cleared a verifier (Python execution, retrieval, or
   user authority on user-authoritative claim classes). A wrong oracle
   call cannot admit a falsehood into the store.
2. **Bounded-domain classification, not open-ended reasoning.** Every LLM
   semantic judgment is reduced to a small label set with a fixed schema.
   Open-ended reasoning is the chat model's job.
3. **Frequentist confidence from independent external evidence only.**
   Every reusable artifact (stored facts, oracle rows) carries
   `affirmed_count` + `contradicted_count`. Cache hits, oracle-mediated
   resolutions, and subsumption-derived matches do NOT increment counts.
   Reads are not writes.
4. **Bounded inference over a verified store.** Three operations:
   equivalence, subsumption, derivation. Memoized at the substrate level.
   Derived results are never persisted — re-walked on demand.
5. **Disciplined abstention.** Causal claims are stored as propositions
   and verified; the system never derives effects from causes.
   Aesthetic / evaluative claims are non-propositional and abstained at
   extraction. Bare counterfactuals are abstained at extraction.
6. **Auditability through a single event log.** Every layer emits
   structured events to `pipeline_events`; the trace UI reads from that
   table. New stages must log; the UI is not special-cased for stages
   that don't emit.
7. **Validate before classifying, route before reasoning.** Each LLM
   layer is preceded by a rule-based validation step. Tier precedence
   (U → W → derivation → fresh) ensures cheap rule-based work catches
   structural problems before expensive LLM work.

## Layout

```
src/
  app.py                     — FastAPI root (extract / dispatch-one / inspectors)
  fact_store.py              — SQLite wrapper, all DB operations
  session_markers.py         — "let's say for this conversation" detector
  llm_client.py              — multi-provider dispatcher (Anthropic + OpenAI)
  openai_client.py           — OpenAI SDK wrapper
  cost.py                    — per-call cost accounting
  layer1_extraction/         — extractor + pattern registry + patterns.yaml
  layer2_routing/            — validator + LLM router + routing memo
  layer3_substrate/          — four oracle classifiers + shared base
  layer4_lookup/             — Tier U + Tier W + derivation + walker + fresh
  layer5_decision/           — confidence + intervention + corrector
  cache/                     — scoping + stability classifiers
  verifiers/                 — fresh-tier infrastructure (retrieval +
                               code generation + comparative + scrapers)
static/                      — vanilla-JS trace UI
tests/                       — one test file per module + integration scenarios
scripts/reset_db.py          — wipe + recreate schema
```

Run with `python -m src.app` (serves `http://127.0.0.1:8000`). Tests with
`pytest`; live API tests gated behind `RUN_API_TESTS=1`.

## Schema

The DDL lives in `src/fact_store.py`. Eleven tables plus the `facts_flat`
view; load-bearing CHECK constraints called out below.

| Table | Purpose | Key CHECK constraints |
|---|---|---|
| `facts` | Verified store. Tier U (`is_session_local` flag) and verifier-output rows live together; `asserted_by` discriminates. | `is_session_local = 0 OR json_array_length(session_ids) <= 1` |
| `turns` | Per-turn user/assistant content + `original_content` for the corrector audit trail. | role ∈ {user, assistant} |
| `pipeline_events` | The single observability log. FK to turns. | — |
| `retrieval_cache` | Search snippets keyed by query, TTL'd. | — |
| `verification_cache` | Tier W. World-fact verifier verdicts with `stability_class` TTL + `canonical_key` UNIQUE. | UNIQUE(canonical_key) |
| `cache_invalidation_log` | Bookkeeping for cache disputes. Cascade invalidation deferred to v0.15. | — |
| `routing_memo` | Layer 2 cache, keyed by (pattern, predicate). | method ∈ {python, python_with_canonical_constants, retrieval, user_authoritative, unverifiable}; PK(pattern, predicate) |
| `predicate_equivalence` | Symmetric-pair oracle (3 labels). | label ∈ {equivalent, contradictory, distinct}; CHECK(predicate_a < predicate_b) |
| `entity_equivalence` | Symmetric-pair oracle (2 labels), case-sensitive. | label ∈ {same, different}; CHECK(entity_a < entity_b) |
| `entity_taxonomy` | Directional triple (4 labels). is_a + part_of unified. | label ∈ {child_subsumed_by_parent, parent_subsumed_by_child, equivalent, neither}; relation_type ∈ {is_a, part_of}; CHECK(child != parent) |
| `predicate_distribution` | Singleton 4-tuple (4 labels). | label ∈ {distributes_up, distributes_down, both, neither}; polarity ∈ {0, 1}; taxonomy_relation_type ∈ {is_a, part_of} |

`verification_status` enum is 8 states: `verified`, `contradicted`,
`user_asserted`, `unverifiable_in_principle`, `retrieval_inconclusive`,
`retrieval_failed`, `unverifiable_pending_implementation`,
`routing_anomaly`. **Do not collapse this enum** — each state encodes a
distinct Layer 5 intervention.

Default DB is `aedos.db`. Override via `AEDOS_DB_PATH`. Reset with
`python scripts/reset_db.py`.

## The nine patterns

Each claim extracts into one of nine structural patterns. Predicates within
a pattern are free-form; the extractor invents specific labels (e.g.
`is_obsessed_with` under `preference`) when the example list doesn't
capture the relation precisely. **The pattern set is closed; predicates
are open.**

| Pattern | Identity slots | Example predicates |
|---|---|---|
| `role_assignment` | agent, role, org | holds_role, served_as |
| `preference` | agent, object | likes, dislikes, loves, hates |
| `quantitative` | subject, property | has_count, weighs, born_in_year |
| `spatial_temporal` | entity, location | lives_in, located_in, visited |
| `categorical` | entity, category | is_a, instance_of |
| `relational` | subject, object | married_to, founded_by, causes |
| `event` | event_type, occurred_at | won_election, was_inaugurated |
| `propositional_attitude` | agent, proposition | believes, knows, hopes |
| `mereological` | part, whole | part_of, member_of, composed_of |

`mereological` is distinct from subsumption: inferences that distribute
down `is_a` chains do not in general distribute down `part_of` chains.
Keeping them separate at the pattern level lets `predicate_distribution`
learn distinct policies cleanly. The mereological scope is constitutive
parthood only; locational containment ("Tokyo is in Japan") stays in
`spatial_temporal`.

`relational` is the home for causal predicates (`causes`, `caused_by`,
`enables`, `prevents`). These are stored as propositions and routed to
retrieval; the system never derives effects from causes.

## Don't change without discussion

These are load-bearing invariants:

- **The core schema** (`facts`, `turns`, `pipeline_events`, the four
  oracle tables). Changes ripple through every component.
- **The 8-state `verification_status` enum.** Each state encodes a
  distinct Layer 5 behavior. Do not collapse.
- **The 9-pattern set.** Open-vocabulary predicates within patterns are
  fine; new patterns are architectural decisions, not routine changes.
- **The "every stage observable" UI constraint.** Every pipeline stage
  writes a `pipeline_events` row. The UI reads from that table.
- **One primary code path per flow.** No mode flags, no alternate
  routes. Add behavior to the existing path or ask first.
- **Counts are independent-external-evidence only.** No incrementing on
  cache hits, oracle-mediated resolutions, or derivation matches. Only
  user reaffirmations + verifier fresh verdicts + operator-driven affirm
  / contradict endpoints touch counts.
- **No persisted derivations.** Derivation walks are query operations;
  results are ephemeral.
- **No lowercasing entity strings.** Case is semantic for entities
  (apple ≠ Apple, mercury ≠ Mercury). Predicates are
  `strip().lower()`-normalized; entities are `strip()`-only.

## How to add a new predicate (common, no code change)

You don't add anything. The extractor produces predicates as part of
fact extraction; if it picks a label not yet seen in the codebase, that's
fine. The router dispatches by pattern — new predicates inherit the
pattern's routing through the LLM router.

If the LLM router occasionally misroutes something you expected to land
elsewhere, look at the trace UI's routing block, then adjust a worked
example in `src/layer2_routing/llm_router.py`'s prompt and run the
calibration: `RUN_API_TESTS=1 pytest tests/test_routing_memo_calibration.py`.

## How to add a new pattern (rare, architectural)

This is a load-bearing decision, not a routine change. The pattern set is
bounded for a reason: each pattern needs declared slots, discriminating
examples in the extractor prompt, key-slot identity for store lookups,
substrate behaviors documented across the four oracles, and Layer 5
interpretation of the verification status it produces.

If you genuinely need a new pattern:

1. Append the entry to `src/layer1_extraction/patterns.yaml` with all
   required fields: `description`, `slots`, `example_predicates`,
   `disambiguation_notes`.
2. Add the pattern's key slots to `KEY_SLOTS_BY_PATTERN` in
   `src/layer2_routing/constants.py`.
3. If the pattern triggers routing-anomaly invariants
   (USER_SUBJECT_PATTERNS, mereological self-parthood, event
   participants, categorical tautology), update
   `src/layer2_routing/validator.py`.
4. Update `src/fact_store.py`'s `facts_flat` view if the flat
   projection should pick up new subject/object slots.
5. Update extractor few-shots in `src/layer1_extraction/extractor.py`.
6. Tests at every layer.

## How to add a fresh-tier verifier

Fresh-tier verification dispatches python (code generation) and retrieval
verdicts via the modules under `src/verifiers/`. Adding a new verifier
kind means extending `src/layer4_lookup/fresh.py`'s dispatch + adding
the verifier under `src/verifiers/`. The dispatch contract: input is a
structured claim, output is a `WalkerDecision` with
`served_from_tier="fresh"` and an 8-state `verification_status`.

## How to debug a turn

1. Send the message through the UI.
2. Watch the right panel (Pipeline Trace). Every stage is visible:
   extraction → routing → walker → decision.
3. If something's wrong, query `pipeline_events` directly:

   ```python
   from src.fact_store import FactStore
   store = FactStore("aedos.db")
   for e in store.get_pipeline_events(turn_id=N):
       print(e["stage"], e["data"])
   ```

4. For LLM-side issues, read the routing_decision / extraction events.
   Both record the LLM's decision payload.
5. For walker issues, the `walker_decision` event carries
   `served_from_tier`, `outcome`, `via` (oracle consultation chain),
   and `derivation_path`.

## Testing conventions

- One test file per source module (`test_<module>.py`).
- LLM calls mocked by default. Live-API tests gated behind
  `RUN_API_TESTS=1` with `@pytest.mark.skipif`.
- Use `tmp_path` for SQLite files in tests; runs are hermetic.
- Calibration corpora live under `tests/calibration/`. Each oracle has
  a gold corpus; floors are documented in the calibration test files.
- Smoke corpus at `tests/smoke_corpus.jsonl`. The dispatcher
  (`tests/smoke_dispatcher.py`) validates entry shapes; tests consume
  corpus entries directly through the appropriate runner.

## v0.15 trajectory

These are the next coherent extensions of the v0.14 architecture:

- **Cascade invalidation across semantically adjacent stored facts.**
  When a fact is contradicted, semantically adjacent facts (matched via
  the substrate) propagate the contradiction. The
  `cache_invalidation_log` table is in place; the cascade logic is the
  v0.15 deliverable.
- **Cross-tier contradiction detection.** When U asserts X about the
  world and W later contradicts X, the system surfaces the disagreement
  to the operator without overriding either store.
- **Layer 1.5 faithfulness validator.** A validator between Layer 1 and
  Layer 2 that compares the extracted claim against the source for
  representational faithfulness.
- **`/api/chat` with SSE streaming.** v0.14 ships `/api/dispatch-one`
  and the inspectors; the chat endpoint is v0.15 work.
- **v0.14-native verifier rewrite.** The `src/verifiers/` modules were
  ported from prior infrastructure and retain pre-v0.14 internal shapes;
  v0.15 refactors them to v0.14-native idioms.

## When you're stuck

The pipeline is a straight line:
**extraction → routing (validate + classify) → walker (Tier U / W /
derivation / fresh) → decision (intervention + correction)**.

If behavior is wrong, walk the stages in order. The `source_text` on
every claim tells you what span was extracted. If extraction went wrong,
start there. If routing classified surprisingly, look at the
`routing_decision` event. If the walker resolved unexpectedly, look at
the `walker_decision` event's `served_from_tier` + `via` chain.

If the LLM keeps returning malformed tool inputs, the fix is almost
always to tighten the tool's `input_schema` or the system prompt — not
to add parsing fallbacks. Fail loudly.
