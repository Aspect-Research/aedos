# Aedos v0.15 — Implementation Plan (Overnight / Calibration-Deferred Variant)

*This document specifies the work of building v0.15 from the Draft 2 architecture document. It is the spec for an unattended Claude Code run that produces v0.15 Phases 0 through 10 in a single session, ending at the `v0.15-phase-10-complete` tag. The architecture document specifies what v0.15 is; this document specifies how it gets built.*

*Read alongside `aedos_v0_15_architecture_draft_2.md`. Where this document and the architecture conflict, the architecture wins, and this document gets revised.*

**This is the calibration-deferred variant of the original phased plan.** The structural build (code, schemas, mocked tests, corpora as authored artifacts) is performed phase-by-phase in a single unattended run. All calibration-corpus *execution* — every operation gated on `RUN_CALIBRATION=1`, `RUN_LIVE_TESTS=1`, or `RUN_LIVE_KB=1` — is deferred to a new Phase 10.5 (Calibration Pass) that is **not part of the overnight run** and is invoked by the operator the following day under supervision. See "Calibration deferral policy" below.

---

## 0. How to use this document

The plan is 11 phases (Phase 0 through Phase 10) plus a deferred Phase 10.5 (Calibration Pass) that the operator runs separately. In this variant, **all of Phases 0-10 are executed in a single unattended Claude Code session**. The phases remain sequential — each builds on the substrate of the previous ones, with explicit dependencies listed at the top of each phase — but they are not separate sessions. The session works continuously through phases, committing and tagging at each phase boundary, until Phase 10 is tagged and the session stops.

Each phase has:

- **Goal.** What this phase achieves operationally.
- **Dependencies.** Which prior phases must be complete.
- **Scope: what's built.** The concrete code, schemas, files, and tests this phase produces.
- **Scope: what's not built.** Things that might seem in-scope but are explicitly deferred. Resisting scope creep is half the discipline of this plan.
- **Calibration corpus.** The JSONL file produced or extended in this phase, with target size and shape. **In this variant the corpus is *authored* but not *executed*.** See "Calibration deferral policy" below.
- **Acceptance criteria.** What must be true at phase end. Includes test counts (relative to phase entry), the calibration-corpus authoring requirement (execution deferred to Phase 10.5), smoke-corpus expectations, and the soundness check (zero false verifieds on the gated test set).
- **Phase-end commit.** The tag at completion.

The Claude Code session is given the architecture document, this implementation plan, and a single kickoff prompt (`aedos_v0_15_overnight_kickoff.md`) that defines the end condition, the per-phase discipline, the ambiguity-handling policy, the failure-handling policy, and the budget hygiene rules. At the start of each phase, the session writes a `docs/v0_15/phase_N_plan.md` document, then implements against it, then tags. This is how planning discipline survives unattended execution: the written per-phase plan is the artifact, not a human review step.

## Calibration deferral policy

Under this variant, **no calibration runs occur during Phases 0-10**. Specifically:

- The environment variables `RUN_CALIBRATION=1`, `RUN_LIVE_TESTS=1`, and `RUN_LIVE_KB=1` are **never set** during the unattended run. The default `make test` runs only mocked unit and integration tests, which are sufficient for all phase acceptance criteria in this variant.
- Each phase still *authors* its calibration corpus. The corpus is the spec for that phase's component, and writing it disciplines the implementation. But the corpus is not executed against the live system until Phase 10.5.
- Each phase verifies that its corpus is **schema-valid** (parses as JSONL, each line conforms to the corpus's documented schema, target case counts are met) and that it **loads without error** through the corpus loader. This is enough to catch malformed corpora during the build without paying for LLM execution.
- The corpora must remain *adversarial* — they should cover edge cases, failure modes the architecture targets, and structural traps. The temptation under deferred calibration is to write thin corpora that pass trivially when Phase 10.5 runs them; the session is required to resist this and the per-phase plan must explicitly document the adversarial-coverage strategy.

**Phase 10.5 — Calibration Pass.** After the overnight run completes and the operator reviews the result, the operator invokes Phase 10.5: every calibration corpus is run against the live system, per-corpus accuracy is reported against the original thresholds in the canonical (non-overnight) plan, and any corpus that fails its threshold triggers a targeted fix-up session against the responsible layer. Only after Phase 10.5 passes does `v0.15.0` get tagged and the v0.14 deletion commit proceed.

The acceptance thresholds for Phase 10.5, by corpus, are the same ones that were originally per-phase in the canonical plan:

| Corpus | Threshold |
|---|---|
| `extraction_corpus` | ≥ 90% |
| `predicate_metadata_corpus` | ≥ 85% |
| `temporal_scope_corpus` | extraction ≥ 90%, lookup 100% |
| `entity_resolution_corpus` | ≥ 90% (live KB) |
| `kb_mapping_corpus` | ≥ 90% (live KB) |
| `subsumption_corpus` | ≥ 90% KB-mediated, ≥ 80% substrate-generation |
| `predicate_distribution_corpus` | ≥ 85% |
| `derivation_corpus` | ≥ 80% (live KB) |
| `python_verification_corpus` | ≥ 85% |
| `consistency_check_corpus` | 100% detection, 100% circuit breaker correctness |
| `intervention_corpus` | ≥ 90% intervention-type classification |

These are reproduced here so they remain reachable when Phase 10.5 runs, even though they no longer gate phase boundaries during the overnight build.

## Unattended-run operating constraints

These constraints apply across all phases in this variant:

- **No live LLM in tests.** `RUN_LIVE_TESTS=1` is never set. The mocked LLM client returns canned responses; integration tests use those. (The system's *own* internal LLM calls — extraction, oracle consultation, walker steps — still occur during smoke runs because the system needs the LLM to function; these are not gated by `RUN_LIVE_TESTS`. See the per-phase scope.)
- **No live KB.** `RUN_LIVE_KB=1` is never set. The Wikidata adapter is exercised in tests via mock responses backed by fixture JSON files (recorded responses from manual development queries, or hand-authored fixtures). Phase 4 produces the fixture set.
- **No calibration execution.** `RUN_CALIBRATION=1` is never set. Corpora are authored, schema-validated, and loaded — not run.
- **v0.14 is read-only.** The existing v0.14 codebase at `src/` is referenced for inspiration but never modified, deleted, or renamed during the overnight run. The deletion commit happens only after Phase 10.5 succeeds.
- **Failure means stop.** If a phase's acceptance criteria cannot be met after reasonable iteration (the kickoff prompt specifies "2-3 attempts at the failing piece"), the session records the blocker in `docs/v0_15/phase_N_blockers.md` and stops. It does not proceed to subsequent phases with a partially-failing prior phase. It does not commit incomplete work as if it were complete.
- **Ambiguities are resolved in writing.** Every ambiguity surfaced during a phase is recorded in `docs/v0_15/phase_N_ambiguities.md` with the resolution chosen and the reasoning. The default bias is toward the more conservative interpretation — the one that makes false verifieds less likely.
- **Run log is mandatory.** After every phase tag, a one-paragraph entry is appended to `docs/v0_15/run_log.md` summarizing the phase: commit SHA, test count, ambiguities resolved, blockers (if any).

## Greenfield directory structure

v0.15 is built greenfield at `src/aedos_v0_15/`. The existing v0.14 code at `src/` is *referenced for inspiration* but not modified, imported, or evolved into v0.15. When v0.15 is complete and tested (Phase 10), a single commit deletes the v0.14 directory and renames `src/aedos_v0_15/` to `src/`. Until that point, the two systems coexist in the repo.

Top-level layout for v0.15:

```
src/aedos_v0_15/
  __init__.py
  app.py                          # FastAPI server + endpoints
  config.py                       # Deployment configuration
  database.py                     # SQLite schema + connection management
  layer1_extraction/
    __init__.py
    extractor.py                  # Relational claim extraction
    normalization.py              # Predicate normalization at extraction
    decomposition.py              # Multi-participant decomposition
    temporal.py                   # Temporal scope handling
    triage.py                     # Verifiability triage
  layer2_routing/
    __init__.py
    router.py                     # Route determination
    validator.py                  # Structural invariant checks
  layer3_substrate/
    __init__.py
    resolver.py                   # Entity resolver (Section 5.1)
    predicate_translation.py      # Predicate translation oracle
    subsumption.py                # Subsumption resolution oracle
    predicate_distribution.py     # Predicate distribution oracle
    consistency.py                # Substrate-internal consistency check
  layer4_sources/
    __init__.py
    tier_u.py                     # Tier U store
    kb_protocol.py                # Abstract KB protocol interface
    kb_wikidata.py                # Wikidata adapter
    python_verifier.py            # Python verification path
    walker.py                     # Derivation walker
  layer5_result/
    __init__.py
    aggregator.py                 # Verification result assembly
    trace.py                      # Justification trace structures
    retraction.py                 # Retraction propagation
  deployment/
    __init__.py
    chat_wrapper.py               # Chat-wrapper deployment + intervention
  audit/
    __init__.py
    log.py                        # Audit log writes + queries
  llm/
    __init__.py
    client.py                     # LLM client (lifted from v0.14 with updates)
  utils/
    __init__.py
    sandbox.py                    # Python sandbox (lifted from v0.14)
    http_cache.py                 # HTTP/cache layer (lifted from v0.14)
tests/v0_15/
  unit/                           # Per-component unit tests
  integration/                    # Cross-layer integration tests
  smoke/
    smoke_corpus.jsonl            # End-to-end smoke cases
    smoke_runner.py               # Smoke test driver
  calibration/
    extraction_corpus.jsonl
    predicate_metadata_corpus.jsonl
    kb_mapping_corpus.jsonl
    entity_resolution_corpus.jsonl
    subsumption_corpus.jsonl
    predicate_distribution_corpus.jsonl
    derivation_corpus.jsonl
    python_verification_corpus.jsonl
    temporal_scope_corpus.jsonl
    consistency_check_corpus.jsonl
    intervention_corpus.jsonl
docs/v0_15/
  walkthroughs/                   # Phase-end worked examples
  cold_start.md                   # Cold-start documentation (Phase 10)
seeds/v0_15/
  predicate_translation.json      # Optional seed pack (Phase 10)
```

## Components lifted from v0.14

Lifted with light updates, not rewritten:

- **LLM client** (`src/llm_client.py` → `src/aedos_v0_15/llm/client.py`). Anthropic and OpenAI routing. Add `chat_stream`, `extract_with_tool`, `rewrite` methods as in v0.14. Update model defaults per Section 9.1 of the architecture (Haiku 4.5 for chat, gpt-4.1-mini for substrate oracle calls, gpt-4.1 for extraction).
- **Python sandbox** (`src/aedos_v0_15/utils/sandbox.py`). The v0.14 sandbox structure with the allow-list updated per Section 6.3 of the architecture (datetime, math, decimal, fractions, statistics, re, unicodedata, string).
- **HTTP/cache layer** (`src/aedos_v0_15/utils/http_cache.py`). httpx-based with ETag-conditional support, in-process LRU, deployment-configurable TTLs.
- **FastAPI skeleton** (`src/aedos_v0_15/app.py`). Server bootstrap, health endpoint, lifespan management. The endpoints themselves are v0.15-specific.

Rewritten from scratch (no v0.14 reuse):

- All of Layer 1-5 substantive code.
- All database schemas (the v0.14 schemas don't match v0.15's).
- All tests.
- The audit log (v0.14 had bits of it; v0.15 has explicit Section 5.4 mechanics).

## Test infrastructure conventions

- **pytest** with v0.15-specific configuration in `tests/v0_15/conftest.py`. Fixtures for LLM client (mocked by default, live with `RUN_LIVE_TESTS=1`), database (fresh sqlite per test), and the KB adapter (Wikidata mocked by default, live with `RUN_LIVE_KB=1`).
- **Calibration corpora as JSONL.** Each line is a structured case with `id`, `input`, `expected_output`, and `notes`. Calibration runs evaluate the system against the corpus, reporting accuracy. Gated behind `RUN_CALIBRATION=1`.
- **Smoke corpus as JSONL.** End-to-end cases that exercise the full pipeline (whatever portion is built by the current phase). Each smoke case has `id`, `text`, `context`, and `expected_verification_result_summary` (with assertions over verdicts, traces, abstentions).
- **Test counts as acceptance criteria.** Each phase commits with a known passing count; the next phase's first task is to confirm that count and increment it.

---

# Phase 0 — Foundation

**Goal.** A runnable empty system. v0.15 boots, has its database schema, has the audit log working, has the LLM client and HTTP cache wired, has FastAPI serving a health endpoint, has the test infrastructure in place. No claim processing yet.

**Dependencies.** None.

## Scope: what's built

### Repo and directory structure

The full `src/aedos_v0_15/` directory tree from Section 0 of this plan is created with empty `__init__.py` files and the placeholder files listed. The full `tests/v0_15/` tree is created with empty corpus files and a working `conftest.py`. `docs/v0_15/` is created.

### Database schema

A `database.py` module that creates and manages the SQLite schema for v0.15. The schema includes every table specified in the architecture document, even tables that are not populated until later phases. This avoids schema migrations within v0.15's implementation phases.

Tables created in Phase 0:

- `tier_u` (Section 6.1 schema).
- `predicate_translation` (Section 5.2 schema).
- `subsumption` (Section 5.2 schema).
- `predicate_distribution` (Section 5.2 schema).
- `audit_log` — for retraction events, consistency-check reports, inline-generation events, circuit-breaker triggerings, budget exceedances. Schema:

```sql
audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,           -- row_created, row_retracted, consistency_violation, circuit_breaker_triggered, budget_exceeded, etc.
  event_subject TEXT NOT NULL,        -- table_name:row_id or claim_id or 'global'
  event_data TEXT NOT NULL,           -- JSON payload with event-specific details
  occurred_at TEXT NOT NULL,
  verification_context TEXT           -- nullable; reference to the verification result that triggered this, if applicable
)
```

- `consistency_circuit_breaker` — tracks regeneration cycles per substrate question. Schema:

```sql
consistency_circuit_breaker (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question_signature TEXT NOT NULL UNIQUE,  -- canonical hash of the substrate question
  cycle_count INTEGER NOT NULL DEFAULT 0,
  last_triggered_at TEXT NOT NULL,
  unresolvable INTEGER NOT NULL DEFAULT 0,  -- 1 after circuit breaker triggers
  unresolvable_reason TEXT
)
```

- `entity_resolution_cache` — used by Phase 4 but schema created now:

```sql
entity_resolution_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reference TEXT NOT NULL,
  local_context_signature TEXT NOT NULL,
  resolved_kb_namespace TEXT NOT NULL,
  resolved_kb_identifier TEXT NOT NULL,
  provenance TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  used_count INTEGER DEFAULT 0,
  retracted_at TEXT,
  retraction_reason TEXT,
  UNIQUE(reference, local_context_signature)
)
```

### LLM client (lifted with updates)

`src/aedos_v0_15/llm/client.py` ports the v0.14 client structure. Methods: `chat`, `chat_stream`, `extract_with_tool`, `complete`. Models default per Section 9.1 of architecture. Configuration via environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, model overrides).

### Python sandbox (lifted with updates)

`src/aedos_v0_15/utils/sandbox.py` ports the v0.14 sandbox. The allow-list is updated to exactly `datetime`, `math`, `decimal`, `fractions`, `statistics`, `re`, `unicodedata`, `string`. The sandbox executes code with typed inputs and captures output, runtime, and exceptions per Section 6.3 of architecture.

### HTTP/cache layer (lifted with updates)

`src/aedos_v0_15/utils/http_cache.py` provides an httpx-based client with ETag-conditional support and an in-process LRU. Used by Phase 4 for the Wikidata adapter. Configurable TTL per cache key.

### FastAPI server

`src/aedos_v0_15/app.py` bootstraps a FastAPI server with:
- `GET /health` — returns `{"status": "ok", "version": "0.15.0"}`.
- Lifespan management that initializes the database, the LLM client, and the HTTP cache.

No verification endpoints yet; those come in later phases.

### Audit log infrastructure

`src/aedos_v0_15/audit/log.py` provides:
- `log_event(event_type, event_subject, event_data, verification_context=None)` — write to `audit_log`.
- `query_events(filters)` — query helpers.

This module is used by every later phase.

### Test infrastructure

`tests/v0_15/conftest.py` with fixtures:
- `db` — fresh in-memory SQLite with v0.15 schema applied.
- `llm_client_mock` — mocked LLM client returning canned responses.
- `kb_mock` — mocked KB adapter (placeholder; populated in Phase 4).
- `temp_audit_log` — isolated audit log for tests.

A first set of tests:
- `tests/v0_15/unit/test_database.py` — schema correctness, table existence, constraint enforcement on each table.
- `tests/v0_15/unit/test_audit_log.py` — log writes, queries.
- `tests/v0_15/unit/test_llm_client.py` — mocked-client correctness.
- `tests/v0_15/unit/test_sandbox.py` — sandbox execution with allowed and disallowed imports.
- `tests/v0_15/unit/test_http_cache.py` — caching behavior.
- `tests/v0_15/unit/test_app.py` — health endpoint, lifespan.

Target ~30 tests passing.

## Scope: what's not built

- Any claim processing.
- Any LLM-mediated logic (extraction, oracle queries, walker).
- Any KB queries.
- Any user-facing endpoints beyond health.
- Calibration corpora (created as empty files; populated by their owning phases).

## Calibration corpus

None this phase. Empty files created for later phases.

## Acceptance criteria

- All v0.15 tables exist with the architecture-document schemas.
- All v0.15 directories exist with placeholder `__init__.py` files.
- `pytest tests/v0_15/ -q` passes with ~30 tests.
- `uvicorn aedos_v0_15.app:app` starts cleanly and `GET /health` responds.
- LLM client can be instantiated and a smoke ping to each provider succeeds. **The smoke ping is deferred to Phase 10.5** (it requires a live LLM call). In Phase 0 the LLM client is exercised against a mocked transport that confirms request shape and response parsing.
- Sandbox executes simple safe code (`datetime.date.today()`) and refuses unsafe code (`import os`).
- Audit log records and retrieves events.

## Phase-end commit

`v0.15-phase-0-complete`. Commit message: `v0.15 Phase 0: foundation`.

---

# Phase 1 — Extraction (Layer 1)

**Goal.** Given (text, context), produce a list of relational claims. Predicate normalization, multi-participant decomposition, temporal scope handling, hard-claim discipline, source-text discipline, first-person canonicalization, verifiability triage. No verification yet.

**Dependencies.** Phase 0.

## Scope: what's built

### Extraction core

`src/aedos_v0_15/layer1_extraction/extractor.py` implements the `Extractor` class:

```python
class Extractor:
    def __init__(self, llm_client, config):
        ...

    def extract(self, text: str, context: ExtractionContext) -> list[Claim]:
        ...
```

The `Claim` dataclass:

```python
@dataclass
class Claim:
    subject: str                              # natural-language reference
    predicate: str                            # canonical predicate name
    object: Any                               # typed per predicate.object_type
    polarity: int                             # 0 or 1
    valid_from: Optional[str] = None          # ISO 8601 or 'before_present'
    valid_until: Optional[str] = None         # ISO 8601 or 'before_present'
    valid_during_ref: Optional[str] = None    # references another claim id for relative scope
    source_text: str = ""
    source_provenance: dict = field(default_factory=dict)
    reified_event_id: Optional[str] = None    # set when claim is part of an event decomposition
    claim_id: str = ""                        # generated identifier for cross-reference
```

The `ExtractionContext` dataclass:

```python
@dataclass
class ExtractionContext:
    asserting_party: str
    turn_id: Optional[int] = None
    prior_conversation: Optional[list] = None
    document_id: Optional[str] = None
    document_context: Optional[dict] = None
    deployment_config: Optional[dict] = None
```

The extractor uses an LLM call with a structured tool schema to produce claims. The prompt emphasizes the binary-relational shape, normalized predicate names, hard-claim discipline, and source-text discipline.

### Predicate normalization

`src/aedos_v0_15/layer1_extraction/normalization.py`:

- `normalize_predicate(raw_predicate: str) -> str` — applies canonical-form transformations: lowercase, snake_case, strip articles, tense-neutralize (lives_in not "is living in", "holds_role" not "was holding role"), voice-neutralize.

The normalization layer is conservative: it produces canonical forms for common cases but does not attempt to unify semantically-equivalent predicates with different stems (`lives_in` vs `resides_at`). Cross-predicate equivalence is handled downstream by the predicate translation oracle (Phase 2).

The normalization rules are documented as a short specification in `src/aedos_v0_15/layer1_extraction/normalization.py`'s module docstring. Examples:

- "is a junior at" → `holds_role` (with role=junior, org=...)
- "was president of" → `holds_role` (with valid_until=before_present)
- "lives in" → `lives_in`
- "is located in" → `located_in` (extractor's choice; predicate translation later unifies with `lives_in` if applicable)
- "prefers" → `prefers`
- "thinks that" → `believes` (propositional attitude)

### Multi-participant decomposition

`src/aedos_v0_15/layer1_extraction/decomposition.py`:

- `decompose_event(participants: list[str], event_type: str, ...) -> list[Claim]` — given multi-participant input, produces a set of binary claims linked by a reified event identifier.

The reified event identifier is a fresh UUID per extraction (no deterministic naming, per architecture Section 4.1.1).

The extractor recognizes multi-participant utterances via the LLM extraction step (the prompt instructs: when a single utterance describes an event with multiple participants, decompose into multiple binary claims with a shared `reified_event_id`).

### Temporal scope handling

`src/aedos_v0_15/layer1_extraction/temporal.py`:

- `extract_temporal_scope(text: str, claim_span: str) -> TemporalScope` — given the text and the claim's source span, determines temporal scope.

```python
@dataclass
class TemporalScope:
    valid_from: Optional[str]             # ISO 8601 or 'before_present' or None
    valid_until: Optional[str]            # ISO 8601 or 'before_present' or None
    valid_during_ref: Optional[str]       # claim_id for relative scope
```

Three cases handled per architecture Section 4.1.2:

- Explicit scope. Dates and durations in the source text resolve to ISO 8601.
- Implicit past tense without dates. Past-tense verbs without explicit time set `valid_until = 'before_present'`.
- No temporal markers. Both fields None; treated as currently valid.

Relative scope ("X was Y when Z was W") is recognized by the extractor and produces two claims, each carrying `valid_during_ref` pointing to the other's `claim_id`.

Future-tense claims are rejected at extraction with an audit-log entry.

### Verifiability triage

`src/aedos_v0_15/layer1_extraction/triage.py`:

- `triage(claim: Claim) -> TriageDecision` — returns VERIFY or PASS_THROUGH.

The triage rules carry forward from v0.14 with updates for the one-pattern world. The rules apply to a relational claim (not to a pattern), and they check slot specificity, predicate semantic content, and verifiability cues. Rough rule set:

- Numeric value in subject or object → VERIFY.
- Temporal scope present → VERIFY.
- Both subject and object pass `_looks_specific` → VERIFY.
- Comparative claim detected → VERIFY.
- Anchor entity (proper noun) with non-vague predicate → VERIFY.
- Predicate in the deployment's always-verify list (carries forward v0.14's concept) → VERIFY.
- Otherwise → PASS_THROUGH.

PASS_THROUGH claims are dropped (the response's prose flows around them without verification); their record is in the audit log.

### Hard-claim discipline

The extractor's prompt includes explicit instructions:
- Do not extract claims about entities merely mentioned in the surrounding `context`. Extract only what `text` itself asserts.
- The `source_text` field is the verbatim assertion span, not a bare noun phrase or a paraphrase.

A validation step after extraction enforces these rules; violating claims are dropped with audit-log entries.

### First-person canonicalization

The extractor's prompt applies universal first-person canonicalization: any first-person reference ("I", "me", "my") in `text` resolves to the asserting party (per `context`), uniformly across all predicates.

### Tests

`tests/v0_15/unit/test_extractor.py` covers:
- Basic extraction from simple sentences.
- Multi-participant decomposition.
- Temporal scope (explicit, implicit past, no marker, relative, future-rejected).
- Hard-claim discipline (entities mentioned but not asserted are not extracted).
- First-person canonicalization.
- Source-text discipline.
- Predicate normalization.
- Verifiability triage (per-rule cases).
- Contrastive corrections (parallel extraction of both polarities).

Target ~80 tests passing in Phase 1.

## Scope: what's not built

- Predicate metadata (handled by the predicate translation oracle in Phase 2). The extractor produces a predicate name; the predicate's `object_type` constraint is checked downstream.
- Routing decisions (Phase 3).
- Verification (Phases 4+).

## Calibration corpus

`tests/v0_15/calibration/extraction_corpus.jsonl` is populated with ~60 cases:

- 15 cases covering normalization (different surface forms of the same canonical predicate).
- 10 cases covering multi-participant decomposition.
- 15 cases covering temporal scope (explicit, implicit past, relative, no marker).
- 10 cases covering hard-claim discipline (entities mentioned but not asserted).
- 10 cases covering first-person canonicalization across deployments (chat user, document author, deployment config).

Each case:

```json
{
  "id": "ext-001",
  "text": "I'm a junior at Williams College.",
  "context": {"asserting_party": "user_test_01"},
  "expected_claims": [
    {
      "subject": "user_test_01",
      "predicate": "holds_role",
      "object": "junior",
      "extras": {"org": "Williams College"},
      "polarity": 1,
      "valid_from": null,
      "valid_until": null
    }
  ]
}
```

**Calibration deferral.** The corpus is authored in this phase but not executed. Execution against the live LLM is deferred to Phase 10.5, where the target accuracy is ≥ 90%. The corpus must be adversarial in coverage — the per-phase plan documents how each of the five sub-categories above includes deliberately tricky cases (ambiguous surface forms, conflicting temporal markers, near-miss hard-claim violations, etc.) rather than only the easy versions.

## Acceptance criteria

- All Phase 1 tests pass; total v0.15 test count is approximately 110 (Phase 0's 30 plus Phase 1's 80).
- `pytest tests/v0_15/ -q` passes (mocked LLM, no live calls).
- `extraction_corpus.jsonl` exists, contains ≥ 60 cases distributed across the five sub-categories per the Calibration corpus section, parses as valid JSONL, and loads through the corpus loader without error. Adversarial-coverage strategy is documented in `docs/v0_15/phase_1_plan.md`. **Execution against the live LLM is deferred to Phase 10.5.**
- The extractor can be called as `extractor.extract(text, context) -> list[Claim]` and produces correctly-structured claims for the smoke set (~5 representative cases) under the mocked LLM client.
- The audit log records all dropped claims (triage PASS_THROUGH, hard-claim violations, future-tense rejections).
- Zero false verifieds: this phase produces no verifications, so the criterion is vacuous; it becomes meaningful from Phase 3 onward.

## Phase-end commit

`v0.15-phase-1-complete`. Commit message: `v0.15 Phase 1: extraction (Layer 1)`.

---

# Phase 2 — Predicate translation oracle

**Goal.** The substrate's central oracle. Given a predicate, produce or retrieve its metadata (object type, validation invariants, routing hint, KB mapping when applicable). This phase produces the oracle that Phases 3-6 depend on.

**Dependencies.** Phase 0 (database, audit log, LLM client). Does not depend on Phase 1 (extractor) — the oracle is independent of the extractor.

## Scope: what's built

### The PredicateTranslation oracle

`src/aedos_v0_15/layer3_substrate/predicate_translation.py`:

```python
class PredicateTranslation:
    def __init__(self, db, llm_client, audit_log, config):
        ...

    def consult(self, aedos_predicate: str, kb_namespace: Optional[str] = None) -> PredicateMetadata:
        """
        Returns the predicate's full metadata. If the row exists in the database, returns it.
        If not, generates a new row via LLM call, stores it, returns it.
        """
        ...

    def retract(self, row_id: int, reason: str) -> None:
        """
        Retract a row (set retracted_at, retraction_reason). Trigger downstream
        retraction propagation for verdicts that included this row.
        """
        ...

    def query_neighbors(self, aedos_predicate: str) -> list[PredicateMetadata]:
        """
        Returns other predicate translation rows that might conflict with the named predicate
        (e.g., other predicates mapping to the same kb_property). Used by consistency check.
        """
        ...
```

The `PredicateMetadata` dataclass exposes all the row fields from architecture Section 5.2.

### Inline metadata generation

The oracle's `consult` method, on cache miss, calls the LLM with a structured prompt:

```
Given the Aedos predicate "<aedos_predicate>", produce the following metadata:

1. object_type: what kind of value does this predicate's object slot hold?
   Options: entity, quantity, time, proposition, entity_list.

2. user_subject_required: does this predicate's subject slot have to be the asserting party?
   (Examples: "prefers", "believes" require user-subject; "lives_in", "born_in" do not.)

3. distinct_slots: are there slot pairs that must differ for the claim to be valid?
   (Example: "part_of" requires part != whole.)

4. routing_hint: which verification route handles this predicate?
   Options: user_authoritative, python, kb_resolvable, abstain.
   - user_authoritative: the asserting party is the ground truth.
   - python: the claim reduces to deterministic computation.
   - kb_resolvable: the predicate maps to a KB property.
   - abstain: no source of belief supports this predicate.

5. kb_namespace + kb_property: when routing_hint is kb_resolvable, which KB and property?
   (Default kb_namespace: "wikidata". Property is the P-number.)

6. slot_to_qualifier: when kb_property is set, how do Aedos slots map to the KB statement?
   Specify which slot is the statement subject, which is the statement value, and which
   qualifiers (with P-numbers) carry additional slots.

7. reason: a 1-2 sentence justification for the choices above.

Return as a structured JSON object.
```

The LLM call uses `gpt-4.1-mini` by default (Section 9.1 of architecture). The response is parsed into a `PredicateMetadata` dataclass and written to the database.

### Consistency check participation

The oracle exposes `query_neighbors` so the substrate-internal consistency check (Phase 8) can detect:
- Two predicates mapped to the same KB property with conflicting slot_to_qualifier.
- One predicate with two different rows for the same kb_namespace (which should be precluded by UNIQUE but is checked defensively).

The consistency check itself is built in Phase 8.

### Audit log integration

Every row creation logs a `row_created` event with the predicate and the LLM call details. Every retraction logs a `row_retracted` event with the reason.

### Tests

`tests/v0_15/unit/test_predicate_translation.py` covers:
- Cold-cache consult triggers LLM call and stores row.
- Warm-cache consult returns stored row without LLM call.
- Retraction sets retracted_at and excludes the row from future consultations.
- `query_neighbors` returns conflict candidates.
- Audit log records creation and retraction events.
- LLM-call failure (malformed response, timeout) is handled gracefully and surfaced as an unresolved-predicate condition.

Target ~50 tests passing in Phase 2.

## Scope: what's not built

- Routing logic that consults the oracle (Phase 3).
- KB queries against `kb_namespace` + `kb_property` (Phase 4).
- The actual consistency check that uses `query_neighbors` (Phase 8).
- Subsumption oracle (Phase 5) and predicate distribution oracle (Phase 5).

## Calibration corpus

`tests/v0_15/calibration/predicate_metadata_corpus.jsonl` is populated with ~80 cases covering:

- 20 user_authoritative predicates (preference, belief, want, intend, etc.).
- 15 python-routed predicates (arithmetic, comparison, date ops, string ops).
- 30 kb_resolvable predicates spanning locations, roles, kinship, categorical membership, mereological relations, quantitative properties, events.
- 10 abstain-routed predicates (subjective, counterfactual, modal, causal where the KB lacks coverage).
- 5 ambiguous cases where the correct routing depends on the deployment configuration; these test the oracle's behavior when context is underspecified.

Each case:

```json
{
  "id": "pred-001",
  "aedos_predicate": "holds_role",
  "expected_metadata": {
    "object_type": "entity",
    "user_subject_required": 0,
    "routing_hint": "kb_resolvable",
    "kb_namespace": "wikidata",
    "kb_property": "P39",
    "slot_to_qualifier_required_keys": ["subject", "object", "org"]
  }
}
```

Note: the calibration evaluates each metadata field independently. For `slot_to_qualifier`, the corpus checks that *required keys* are present without requiring the exact qualifier P-numbers (Wikidata's qualifier vocabulary can drift; we test for structural correctness, not exact mappings).

Target accuracy under Phase 10.5: 85% on `predicate_metadata_corpus.jsonl`. This is lower than the extraction-corpus threshold because metadata generation involves more LLM judgment.

**Calibration deferral.** Corpus authored in this phase; not executed. The per-phase plan documents the adversarial-coverage strategy across the five sub-categories (user_authoritative, python-routed, kb_resolvable, abstain-routed, ambiguous-deployment-dependent).

## Acceptance criteria

- All Phase 2 tests pass; total v0.15 test count is approximately 160.
- `predicate_metadata_corpus.jsonl` exists with ≥ 80 cases distributed across the five sub-categories, parses, and loads. **Execution deferred to Phase 10.5.**
- `PredicateTranslation` oracle correctly handles cold-cache (LLM call, row stored) and warm-cache (no LLM call, row returned), as exercised by unit tests with a mocked LLM.
- Retraction propagation works: retracting a row sets `retracted_at` and excludes the row from `consult`.
- Audit log entries are written for every row creation and retraction.
- The oracle's behavior under the mocked LLM is deterministic: the same predicate consulted twice returns the same metadata; LLM calls happen only on cold cache.
- Zero false verifieds: this phase produces no verifications.

## Phase-end commit

`v0.15-phase-2-complete`. Commit message: `v0.15 Phase 2: predicate translation oracle`.

---

# Phase 3 — Routing (Layer 2) + Tier U

**Goal.** Layer 2's router decides routes based on predicate metadata from Phase 2. Tier U is fully implemented: schema, write path, three-stage read path with entity-resolution and predicate-translation broadening. User-authoritative claims roundtrip through Tier U. KB and Python routes are stubbed (they return a sentinel "would have routed here").

**Dependencies.** Phase 0, Phase 1 (extractor produces the claims this layer routes), Phase 2 (router consults the predicate translation oracle).

## Scope: what's built

### Router

`src/aedos_v0_15/layer2_routing/router.py`:

```python
class Router:
    def __init__(self, predicate_translation, validator, audit_log):
        ...

    def route(self, claim: Claim) -> RoutingDecision:
        """
        Returns a RoutingDecision specifying the route and any validation results.
        """
        ...
```

The router:
1. Calls the validator (see below) to check structural invariants.
2. On structural failure, returns a routing anomaly.
3. Otherwise, consults the predicate translation oracle for the claim's predicate.
4. The oracle's `routing_hint` determines the route.
5. Returns a `RoutingDecision` with the route and any metadata needed downstream.

### Validator

`src/aedos_v0_15/layer2_routing/validator.py`:

```python
class Validator:
    def validate(self, claim: Claim, predicate_metadata: PredicateMetadata) -> ValidationResult:
        """
        Checks structural invariants per Section 4.2 of the architecture.
        Returns Pass or RoutingAnomaly.
        """
        ...
```

Invariants checked:
- If `user_subject_required` is true, the claim's subject must canonicalize to the asserting party.
- If `distinct_slots` is non-empty, the specified slot pairs must differ in the claim.
- The claim's `object` must match the predicate's `object_type`.

Each violation produces a specific `RoutingAnomaly` with a reason.

### Tier U

`src/aedos_v0_15/layer4_sources/tier_u.py`:

```python
class TierU:
    def __init__(self, db, audit_log, entity_resolver, predicate_translation):
        ...

    def write(self, claim: Claim) -> WriteResult:
        """
        Writes a claim to Tier U. Idempotent on same-context-same-content.
        Returns the row that was written (or matched).
        """
        ...

    def lookup(self, claim: Claim, current_time: str) -> LookupResult:
        """
        Three-stage lookup per Section 6.1 of architecture:
        1. Literal match.
        2. Entity-resolution broadening (Phase 4 will populate entity_resolver; for now, stub).
        3. Predicate-translation broadening (Phase 2's oracle).
        """
        ...

    def retract(self, row_id: int, reason: str) -> None:
        """
        Set retracted_at. Trigger downstream retraction propagation (Phase 8).
        """
        ...
```

The three-stage lookup is implemented:

- **Stage 1 (literal match):** SQL query for exact match on (asserting_party, subject, predicate, object, polarity) with `retracted_at IS NULL` and temporal scope intersecting `current_time`.
- **Stage 2 (entity-resolution broadening):** For each entity slot, the entity resolver (Phase 4) provides a KB identifier. SQL query matches rows whose `resolved_*_id` equals the new resolution. In Phase 3, the resolver is stubbed — the broadening is implemented in the code but disabled until Phase 4 wires it up. Tests use a mocked resolver to exercise the broadening logic.
- **Stage 3 (predicate-translation broadening):** Consults the predicate translation oracle to find equivalent predicates (predicates with the same `kb_namespace + kb_property`). Retries stages 1 and 2 with the equivalent predicate.

### Temporal scope at read

The lookup checks each candidate row's `valid_from` / `valid_until` against the claim's temporal scope:
- A row with `valid_until < claim.valid_from` is historical and may appear as a historical premise (the lookup returns it with a `historical=True` marker), but cannot ground a present-tense claim.
- A row with `valid_until = 'before_present'` is similarly historical with unspecified end time.
- A row with `valid_during_ref` requires resolving the referenced claim's scope; if the referenced claim is itself in Tier U, the lookup recurses (with cycle detection).

### Routing-driven Tier U writes

When a claim is routed user-authoritative and Tier U finds no contradicting row, the claim is written to Tier U with the asserting party from `context`. When a contradicting row is found (same content, opposite polarity, or different object for the same predicate where the predicate is single-valued), the existing row's `valid_until` is set to `now()` and a new row is written.

### Tests

`tests/v0_15/unit/test_router.py` covers:
- Each routing decision (user_authoritative, python, kb_resolvable, abstain).
- Validator anomaly cases.
- Routing decisions on cold predicate cache (triggers Phase 2's oracle).

`tests/v0_15/unit/test_tier_u.py` covers:
- Write path (insert, idempotency, contradiction-handling).
- Three-stage read path (literal match, entity-resolution broadening with mocked resolver, predicate-translation broadening).
- Temporal scope at read (historical, before_present, relative).
- Retraction.

`tests/v0_15/integration/test_routing_to_tier_u.py` covers:
- End-to-end: claim → router → Tier U write or lookup → result.

Target ~70 new tests; total v0.15 test count approximately 230.

## Scope: what's not built

- KB queries (Phase 4 stubs out as `LookupResult(route="kb_resolvable", verdict=None, reason="kb_stubbed")` for kb_resolvable routes).
- Python verification (Phase 7 stubs out similarly).
- Derivation walker (Phase 6).
- Layer 5 result aggregation (Phase 8).
- Substrate-internal consistency check (Phase 8).
- Entity resolver (Phase 4 implements; Phase 3 uses stub).

## Calibration corpus

`tests/v0_15/calibration/temporal_scope_corpus.jsonl` is populated with ~40 cases covering:
- 10 cases with explicit scope.
- 10 cases with implicit past tense.
- 10 cases with relative scope (`valid_during_ref`).
- 5 cases with no temporal markers.
- 5 future-tense cases (expected: rejected).

Each case is a (text, expected_temporal_scope) pair; the corpus tests the extractor's temporal handling (which lives in Phase 1) combined with Tier U's read-time scope comparison.

Target accuracy under Phase 10.5: 90% on temporal-scope-extraction and 100% on temporal-scope-at-lookup (the lookup logic is deterministic; the extraction is the LLM-mediated piece).

**Calibration deferral.** Corpus authored in this phase; not executed. The lookup-time scope comparison is deterministic and *is* exercised by unit tests in this phase (with mocked Tier U rows) — that is testing logic, not calibration, and the 100% expectation applies to those unit tests. The extraction side of the corpus is deferred to Phase 10.5.

## Acceptance criteria

- All Phase 3 tests pass; total v0.15 test count is approximately 230.
- `temporal_scope_corpus.jsonl` exists with ≥ 40 cases per the sub-category distribution, parses, and loads. **Extraction-side execution deferred to Phase 10.5.**
- The deterministic lookup-time scope comparison is 100% correct on the unit-test suite for Tier U scope handling (this is not calibration; it is testable without live LLM calls).
- A claim routed user_authoritative writes correctly to Tier U.
- A second claim with the same content from the same asserting party is idempotent.
- A contradicting claim closes the prior Tier U row and writes a new one.
- The router correctly handles all four routes (with kb_resolvable and python returning stub results).
- Routing anomalies are detected for the validator's invariants.
- Zero false verifieds: user-authoritative roundtrips never produce a false verified (the asserting party is the ground truth).

## Phase-end commit

`v0.15-phase-3-complete`. Commit message: `v0.15 Phase 3: routing + Tier U`.

---

# Phase 4 — KB protocol + Wikidata adapter

**Goal.** The three protocol operations (`resolve_entity`, `lookup_statements`, `subsumption`) abstracted as a protocol and implemented against Wikidata. Entity resolver with type filtering and local-context disambiguation. Statement lookup with qualifier extraction. Subsumption traversal. Result-cache layer. KB-resolvable claims roundtrip through the KB.

**Dependencies.** Phase 0 (HTTP cache, database, audit log), Phase 2 (predicate metadata informs entity-type filtering).

## Scope: what's built

### KB protocol interface

`src/aedos_v0_15/layer4_sources/kb_protocol.py`:

```python
class KBProtocol(Protocol):
    def resolve_entity(self, reference: str, local_context: LocalContext) -> list[ResolutionCandidate]:
        ...

    def lookup_statements(self, entity: KBEntityID, predicate: KBPropertyID) -> list[Statement]:
        ...

    def subsumption(self, entity_a: KBEntityID, entity_b: KBEntityID, relation_type: str) -> SubsumptionResult:
        ...
```

The dataclasses:

```python
@dataclass
class LocalContext:
    predicate: str                          # the claim's predicate
    slot_position: str                      # 'subject' or 'object'
    asserting_party: Optional[str]
    prior_resolutions: list[ResolutionCandidate]  # soft evidence; not deterministic

@dataclass
class ResolutionCandidate:
    kb_identifier: KBEntityID
    provenance: dict                        # type_filter_match, kb_search_rank, local_context_match, llm_selection_reason
    score: float                            # ranking score (used for ordering only)

@dataclass
class Statement:
    value: Any                              # entity_id, literal, date, quantity
    value_type: str                         # entity | literal | date | quantity
    qualifiers: dict                        # qualifier P-number → value(s)
    rank: str                               # preferred | normal | deprecated (excluded automatically)
    provenance: dict

@dataclass
class SubsumptionResult:
    verdict: str                            # a_subsumed_by_b | b_subsumed_by_a | equivalent | unrelated
    establishing_property: Optional[str]    # which KB property established this
    traversal_chain: list[KBEntityID]       # intermediate entities if any
```

### Wikidata adapter

`src/aedos_v0_15/layer4_sources/kb_wikidata.py`:

```python
class WikidataAdapter:
    def __init__(self, http_cache, llm_client, db, audit_log, config):
        ...

    # Implements KBProtocol
    def resolve_entity(self, reference, local_context):
        ...

    def lookup_statements(self, entity, predicate):
        ...

    def subsumption(self, entity_a, entity_b, relation_type):
        ...
```

**Entity resolution.** Uses `wbsearchentities` API with:
- Reference as the search query.
- Language filter (default English; configurable).
- Candidate-pool size of 10.

For each candidate:
1. Fetch P31 (instance of) values via SPARQL.
2. Apply type filter: the predicate metadata's expected entity-type for the slot position. The mapping from predicate slot to expected entity-type is encoded in the predicate translation row (Phase 2 expands the predicate metadata schema to include slot-type expectations).
3. Score by type-filter match strength + search rank + local-context match (overlap with prior_resolutions on shared properties).
4. When multiple candidates pass filtering, an LLM call selects the best with reasoning.

The result cache (`entity_resolution_cache` table) is consulted before invoking the search API. Cache key: SHA256 hash of (reference, predicate, slot_position, asserting_party). Cache hits do not increment any usage counter that gates decisions; they increment `used_count` for observability.

**Statement lookup.** SPARQL query against WDQS with templated patterns:

```sparql
SELECT ?value ?valueType ?qual ?qualValue ?rank WHERE {
  wd:<entity> p:<predicate> ?statement .
  ?statement ps:<predicate> ?value .
  ?statement wikibase:rank ?rank .
  OPTIONAL { ?statement pq:<qual> ?qualValue . }
  FILTER(?rank != wikibase:DeprecatedRank)
}
```

Results are aggregated per statement: each statement object has its value, value type, all qualifiers (including start_time, end_time, of, location, others), and rank.

**Subsumption traversal.** SPARQL recursive queries over P31, P279, P131, P361:

```sparql
SELECT ?intermediate WHERE {
  wd:<entity_a> (wdt:P31|wdt:P279)+ ?intermediate .
  ?intermediate (wdt:P31|wdt:P279)* wd:<entity_b> .
}
```

The traversal is bounded (default 6 hops, configurable). If a chain exists, the verdict is `a_subsumed_by_b` (or the reverse, depending on direction). If both chains exist in both directions, `equivalent`. If neither, `unrelated`.

### Entity resolver wired to Tier U and other consumers

`src/aedos_v0_15/layer3_substrate/resolver.py`:

```python
class EntityResolver:
    def __init__(self, kb_protocol, db, audit_log):
        ...

    def resolve(self, reference: str, local_context: LocalContext) -> list[ResolutionCandidate]:
        """
        Consults result cache; on miss, delegates to the KB protocol's resolve_entity;
        stores in cache; returns candidates.
        """
        ...

    def select(self, candidates: list[ResolutionCandidate], local_context: LocalContext) -> Optional[KBEntityID]:
        """
        Pick the top candidate per scoring + LLM-mediated selection when ambiguous.
        Returns None if no candidate is sufficient (escalates to abstention).
        """
        ...

    def retract_cache_entry(self, cache_id: int, reason: str) -> None:
        """
        Set retracted_at on the cache entry. Trigger downstream retraction.
        """
        ...
```

Phase 3's stubbed resolver is replaced. Phase 3's tests are updated to use the real resolver (with a mocked KB for unit tests; live Wikidata calls under `RUN_LIVE_KB=1`).

### KB-resolvable claim verification

`src/aedos_v0_15/layer4_sources/kb_verifier.py`:

```python
class KBVerifier:
    def __init__(self, kb_protocol, entity_resolver, predicate_translation, audit_log):
        ...

    def verify(self, claim: Claim, current_time: str) -> KBVerdict:
        """
        Resolves entity slots, translates the predicate, queries the KB,
        compares qualifier scope against claim scope, returns verdict + trace.
        """
        ...
```

The KBVerifier:
1. Consults predicate_translation for the predicate's metadata (kb_namespace, kb_property, slot_to_qualifier).
2. If no kb_property or routing_hint is not kb_resolvable, returns `no_kb_path`.
3. Resolves each entity slot via entity_resolver.
4. Calls `kb_protocol.lookup_statements(entity, kb_property)`.
5. For each returned statement, compares qualifier scope against claim's temporal scope.
6. If a matching statement is found with compatible scope, returns `verified`.
7. If matching predicate but contradicting value, returns `contradicted`.
8. If no matching statement, returns `no_match`.

### Tests

`tests/v0_15/unit/test_wikidata_adapter.py` (mocked HTTP):
- Entity resolution against canned wbsearchentities responses.
- Statement lookup against canned SPARQL responses.
- Subsumption traversal across canned graphs.
- Rank handling (deprecated excluded).
- Qualifier extraction.

`tests/v0_15/unit/test_entity_resolver.py`:
- Cold cache → KB call → cache write.
- Warm cache → no KB call.
- Selection with multiple candidates.
- Retraction.

`tests/v0_15/unit/test_kb_verifier.py`:
- Verify with matching statement.
- Contradiction.
- No match.
- Qualifier scope mismatch.

`tests/v0_15/integration/test_kb_path.py`:
- End-to-end: claim → router (kb_resolvable) → KB verifier → verdict.

`tests/v0_15/live/test_wikidata_live.py` (under `RUN_LIVE_KB=1`):
- Live SPARQL queries for ~5 known entities.
- Live entity resolution.

Target ~90 new tests; total v0.15 test count approximately 320.

## Scope: what's not built

- Subsumption oracle (Phase 5) — KB-mediated subsumption queries work via `kb_protocol.subsumption`, but the substrate's *meta-subsumption rows* for non-KB-encoded judgments are Phase 5.
- Predicate distribution oracle (Phase 5).
- Derivation walker (Phase 6).
- Python verification (Phase 7).
- Substrate-internal consistency check (Phase 8).

## Calibration corpora

`tests/v0_15/calibration/entity_resolution_corpus.jsonl` ~50 cases:
- 20 unambiguous reference cases (single dominant Q-number).
- 15 ambiguous cases requiring local context disambiguation (e.g., "Paris" with location-slot vs person-slot context).
- 10 type-filter cases where the wrong type would be the top search result.
- 5 no-match cases (references that should resolve to no Wikidata entity).

`tests/v0_15/calibration/kb_mapping_corpus.jsonl` ~40 cases (extends Phase 2's predicate_metadata_corpus with explicit kb-mapping evaluation):
- 30 kb_resolvable predicates with their expected Wikidata properties.
- 10 with their expected slot_to_qualifier mappings.

Target accuracy under Phase 10.5: 90% on entity_resolution_corpus, 90% on kb_mapping_corpus. Both run under `RUN_LIVE_KB=1` for live evaluation against Wikidata in Phase 10.5.

**Calibration deferral and fixture requirement.** Phase 4 is the first phase that would, in the canonical plan, exercise the live Wikidata API. In this variant the live KB is **not** queried during the overnight run. Instead, this phase produces a **Wikidata fixture set** at `tests/v0_15/fixtures/wikidata/` consisting of hand-authored or template-generated JSON files that mirror the response shapes from `wbsearchentities`, `wbgetentities`, and the SPARQL endpoint for the entities referenced in the smoke corpus and the unit tests. The KB adapter is wired to consult these fixtures when `RUN_LIVE_KB` is not set. This makes the adapter testable without network access and without paying live-query costs.

The fixture set must be:
- **Complete enough** for the Phase 4 unit and integration tests to exercise every code path in the adapter (search, single-entity fetch, multi-entity batch, subsumption traversal, qualifier extraction, no-match).
- **Documented** with a README in the fixtures directory listing each fixture's source (which Q-number, which property, which API endpoint shape) so Phase 10.5 can validate that the fixtures match live Wikidata's current shape.
- **Schema-validated** against the structural expectations the adapter parses.

The corpora (`entity_resolution_corpus.jsonl`, `kb_mapping_corpus.jsonl`) are authored as usual; their live-KB execution is deferred to Phase 10.5.

## Acceptance criteria

- All Phase 4 tests pass; total v0.15 test count approximately 320.
- `entity_resolution_corpus.jsonl` and `kb_mapping_corpus.jsonl` exist with the case counts and sub-category distributions specified above, parse, and load. **Live-KB execution deferred to Phase 10.5.**
- Wikidata fixture set exists at `tests/v0_15/fixtures/wikidata/` covering every entity, property, and traversal exercised by Phase 4 tests, with a README documenting each fixture.
- The KBProtocol interface is implemented by WikidataAdapter and the interface admits other implementations (a `DummyKBAdapter` for testing demonstrates this).
- Entity resolution correctly disambiguates per local context for the ambiguous test cases, using fixture data.
- Subsumption traversal correctly handles direct relations, multi-hop relations, and unrelated entities, using fixture data.
- Qualifier extraction returns the expected qualifiers from the fixture statements.
- HTTP cache reduces redundant queries on cache hits (testable against fixtures by counting fixture loads).
- Zero false verifieds: every `kb_verified` verdict in the test suite traces to a fixture statement that genuinely supports the claim. (Whether the fixture statement matches *live* Wikidata is a Phase 10.5 question.)

## Phase-end commit

`v0.15-phase-4-complete`. Commit message: `v0.15 Phase 4: KB protocol + Wikidata adapter`.

---

# Phase 5 — Subsumption resolution and predicate distribution oracles

**Goal.** The two remaining substrate oracles. Subsumption: most queries go through KB protocol (Phase 4); substrate rows exist for meta-judgments. Predicate distribution: inline generation, lookup-first, the four-verdict enum. Substrate complete after this phase.

**Dependencies.** Phase 0, Phase 2 (predicate translation oracle pattern), Phase 4 (KB-native subsumption).

## Scope: what's built

### Subsumption oracle

`src/aedos_v0_15/layer3_substrate/subsumption.py`:

```python
class SubsumptionOracle:
    def __init__(self, db, llm_client, kb_protocol, audit_log):
        ...

    def consult(self, entity_a: EntityRef, entity_b: EntityRef, relation_type: str) -> SubsumptionVerdict:
        """
        Returns the subsumption verdict.

        Resolution priority:
        1. KB-mediated: if both entities are KB-resolved, delegate to kb_protocol.subsumption.
        2. Substrate row: if a row exists in the subsumption table for this entity pair, return it.
        3. Cold-start: LLM-generate a row, store, return.
        """
        ...

    def retract(self, row_id, reason):
        ...

    def query_neighbors(self, entity_a, entity_b, relation_type):
        ...
```

The oracle handles three cases:
- **Both entities KB-resolved.** Delegate to `kb_protocol.subsumption`. The result is *not* stored as a substrate row — the KB is the source. The verdict is returned with provenance pointing to the KB.
- **Mixed-namespace** (one entity is KB-resolved, the other is an Aedos-only identifier like a Tier U entity that didn't resolve to Wikidata). The substrate row is consulted; on miss, the LLM is invoked to generate a verdict and row is stored.
- **Both Aedos-only** (uncommon but possible). Same as mixed-namespace: substrate row or LLM-generated.

### Predicate distribution oracle

`src/aedos_v0_15/layer3_substrate/predicate_distribution.py`:

```python
class PredicateDistributionOracle:
    def __init__(self, db, llm_client, audit_log):
        ...

    def consult(self, predicate: str, polarity: int, relation_type: str) -> DistributionVerdict:
        """
        Returns whether the predicate distributes up, down, both, or neither
        a subsumption relation of the given type.

        Lookup-first; LLM-generates on cold cache.
        """
        ...

    def retract(self, row_id, reason):
        ...

    def query_neighbors(self, predicate, polarity, relation_type):
        ...
```

LLM prompt for cold-cache generation:

```
For the predicate "<predicate>" with polarity <polarity> (1=asserted, 0=negated)
and the subsumption relation "<relation_type>" (is_a or part_of), determine whether
the predicate distributes:

- distributes_up: if predicate(X, ...) is true and X is_a/part_of Y, then predicate(Y, ...) is true.
- distributes_down: if predicate(Y, ...) is true and X is_a/part_of Y, then predicate(X, ...) is true.
- both: distributes in both directions.
- neither: does not distribute in either direction.

Example: "lives_in" distributes up "part_of": if Asa lives in Williamstown,
and Williamstown is part of Massachusetts, then Asa lives in Massachusetts. So distributes_up.

Example: "prefers" does not distribute up "is_a": if Asa prefers golden retrievers,
and golden retriever is_a dog, it does NOT follow that Asa prefers dogs. So neither (in the up direction;
similarly in the down direction).

Return the verdict and a 1-2 sentence reason as a structured JSON object.
```

### Tests

`tests/v0_15/unit/test_subsumption_oracle.py`:
- KB-mediated case (both entities KB-resolved, delegates to kb_protocol).
- Substrate row case (mixed-namespace, row exists).
- Cold-cache substrate row creation.
- Retraction.

`tests/v0_15/unit/test_predicate_distribution_oracle.py`:
- Cold cache → LLM call → row stored.
- Warm cache → no LLM call.
- All four verdicts (distributes_up, distributes_down, both, neither).
- Retraction.

`tests/v0_15/integration/test_substrate_complete.py`:
- All four substrate components (resolver, predicate_translation, subsumption, predicate_distribution) accessible via a `Substrate` facade.
- Cross-oracle consistency: a predicate translation row that maps to a kb_property has consistent slot_to_qualifier structure with the kb_protocol's actual qualifier patterns.

Target ~60 new tests; total v0.15 test count approximately 380.

## Scope: what's not built

- The substrate-internal consistency check (Phase 8 builds this; the oracles expose `query_neighbors` for it).
- The derivation walker (Phase 6).
- Python verification (Phase 7).

## Calibration corpora

`tests/v0_15/calibration/subsumption_corpus.jsonl` ~60 cases:
- 30 KB-resolvable subsumption cases (testing the KB-mediated path).
- 20 mixed-namespace cases requiring substrate-row generation.
- 10 with directly opposing entities (testing the unrelated verdict).

`tests/v0_15/calibration/predicate_distribution_corpus.jsonl` ~50 cases:
- 12 distributes_up cases (lives_in, located_in, occurred_in over part_of).
- 8 distributes_down cases (mortal, has_part over is_a).
- 5 both cases.
- 25 neither cases (the largest set: most predicates do not distribute cleanly).

Target accuracy under Phase 10.5: 90% on subsumption (95% on KB-mediated cases, 80% on substrate-row generation); 85% on predicate distribution.

**Calibration deferral.** Both corpora authored in this phase; not executed. The subsumption corpus's KB-mediated cases will exercise the live Wikidata adapter at Phase 10.5; the substrate-generation cases exercise the LLM inline-generation path. The per-phase plan documents adversarial coverage on both paths.

## Acceptance criteria

- All Phase 5 tests pass; total v0.15 test count approximately 380.
- `subsumption_corpus.jsonl` and `predicate_distribution_corpus.jsonl` exist with the documented case counts and sub-category distributions, parse, and load. **Execution deferred to Phase 10.5.**
- Substrate facade exposes all four components with a uniform interface (each has `consult`, `retract`, `query_neighbors`).
- Cross-oracle consistency tests pass under mocked LLM.
- Zero false verifieds on subsumption test cases.

## Phase-end commit

`v0.15-phase-5-complete`. Commit message: `v0.15 Phase 5: subsumption + predicate distribution oracles`.

---

# Phase 6 — Derivation walker

**Goal.** The inference engine. BFS over the composite premise graph at depth 4, cycle detection, polarity tracking, predicate-distribution gating, inline substrate-row generation, resource budgets, full justification trace emission. Result: Layer 4 produces grounded verdicts with traces.

**Dependencies.** Phases 0-5. The walker uses the complete substrate.

## Scope: what's built

### The Walker

`src/aedos_v0_15/layer4_sources/walker.py`:

```python
class Walker:
    def __init__(self, tier_u, kb_verifier, python_verifier, substrate, audit_log, config):
        ...

    def walk(self, claim: Claim, context: VerificationContext) -> WalkResult:
        """
        Walks the derivation graph for the claim.
        Returns WalkResult with verdict, trace, and any abstention reason.
        """
        ...
```

The `WalkResult` dataclass:

```python
@dataclass
class WalkResult:
    verdict: str                              # verified | contradicted | no_grounding_found
    trace: JustificationTrace                 # complete trace
    abstention_reason: Optional[str]          # set when no_grounding_found
    budget_consumption: BudgetConsumption     # wall_clock_ms, llm_calls
```

The walker's algorithm:

```
walk(claim):
    initialize:
        frontier = [initial_node from claim]
        visited = {}
        wall_clock_start = now()
        llm_call_count = 0
        depth = 0

    while frontier not empty and depth < max_depth:
        check_budget(wall_clock_start, llm_call_count) -> may abstain
        next_frontier = []
        for node in frontier:
            canonical_key = canonicalize(node)
            if canonical_key in visited:
                continue
            visited[canonical_key] = node

            # Direct premise lookup
            tier_u_result = tier_u.lookup(node, context.current_time)
            if tier_u_result.terminal:
                return WalkResult(verdict=tier_u_result.verdict, trace=...)

            kb_result = kb_verifier.verify(node, context.current_time)
            if kb_result.terminal:
                return WalkResult(verdict=kb_result.verdict, trace=...)

            python_result = python_verifier.verify(node)
            if python_result.terminal:
                return WalkResult(verdict=python_result.verdict, trace=...)

            # Expand via substrate operations
            for edge in expand_via_substrate(node):
                next_frontier.append(edge.target)

        frontier = next_frontier
        depth += 1

    return WalkResult(verdict='no_grounding_found', trace=..., abstention_reason='depth_exhausted')


expand_via_substrate(node):
    edges = []

    # Equivalence substitution: predicate translation
    pred_meta = substrate.predicate_translation.consult(node.predicate)
    for equivalent_pred in find_equivalent_predicates(pred_meta):
        new_node = node with predicate replaced
        edges.append(Edge(kind='predicate_equivalence', source=node, target=new_node, ...))

    # Equivalence substitution: entity resolution
    for slot in [subject, object]:
        candidates = substrate.resolver.resolve(node[slot], local_context)
        for candidate in candidates:
            new_node = node with slot replaced by candidate.kb_id
            edges.append(Edge(kind='entity_equivalence', source=node, target=new_node, ...))

    # Subsumption traversal with distribution gating
    for slot in [subject, object]:
        for relation_type in ['is_a', 'part_of']:
            distribution = substrate.predicate_distribution.consult(
                node.predicate, node.polarity, relation_type
            )
            if distribution.verdict in ['neither']:
                continue  # gate closed
            for direction in distribution.verdict_directions():
                neighbors = substrate.subsumption.find_neighbors(node[slot], relation_type, direction)
                for neighbor in neighbors:
                    new_node = node with slot replaced by neighbor
                    edges.append(Edge(kind='subsumption_traversal', ...))

    return edges
```

### Justification trace structure

`src/aedos_v0_15/layer5_result/trace.py`:

```python
@dataclass
class TraceNode:
    node_type: str
    content: dict

@dataclass
class TraceEdge:
    edge_type: str                            # premise_lookup | predicate_equivalence | entity_equivalence | subsumption_traversal
    source: TraceNode
    target: TraceNode
    metadata: dict                            # oracle row IDs, KB statement IDs, etc.

@dataclass
class JustificationTrace:
    root: TraceNode
    edges: list[TraceEdge]
    polarity_trace: list[int]
    source_breakdown: dict                    # how many premises from each of Tier U / KB / Python
    walk_metadata: dict                       # walk depth reached, llm_call_count, wall_clock_ms
```

Every walker call returns a complete trace. The trace is serializable (JSON) for storage in audit logs and inclusion in verification results.

### Resource budgets

`src/aedos_v0_15/layer4_sources/walker.py` includes budget enforcement:

```python
@dataclass
class WalkerBudget:
    wall_clock_seconds: float = 30.0
    max_llm_calls: int = 10

def check_budget(start_time, llm_call_count, budget):
    elapsed = now() - start_time
    if elapsed > budget.wall_clock_seconds:
        raise BudgetExceeded('wall_clock')
    if llm_call_count >= budget.max_llm_calls:
        raise BudgetExceeded('llm_calls')
```

Budget exceedance triggers abstention with the trace recording which budget was exceeded.

### Multi-chain handling

When the walker finds multiple chains producing the same verdict, the trace records all of them. When chains produce *different* verdicts (one verified, one contradicted), the result is a contradiction with the substrate inconsistency flagged in the audit log (the next consistency-check run, Phase 8, will detect and resolve).

### Tests

`tests/v0_15/unit/test_walker.py` covers:
- BFS at varying depths.
- Cycle detection.
- Polarity tracking through substitution and traversal.
- Predicate-distribution gating (claims that should derive vs. claims that should not).
- Inline row generation during walks.
- Budget enforcement (wall_clock and llm_call).
- Trace emission.

`tests/v0_15/integration/test_walker_with_substrate.py` covers:
- Multi-source chains (Tier U + KB + Python composition).
- Multi-hop derivation.
- Adversarial inputs that trigger budget exceedance.

Target ~80 new tests; total v0.15 test count approximately 460.

## Scope: what's not built

- Python verification path (Phase 7); walker calls a stubbed PythonVerifier that returns `no_terminal_result`.
- Layer 5 aggregation (Phase 8).
- Substrate-internal consistency check (Phase 8).
- Retraction propagation (Phase 8).

## Calibration corpus

`tests/v0_15/calibration/derivation_corpus.jsonl` ~50 cases covering the six failure modes:

- 12 multi-hop with predicate distribution (e.g., user-location → KB-containment → claim about wider region).
- 10 cross-source unification (Tier U + KB + Python).
- 8 contextual entity disambiguation (claims where entity resolution matters).
- 8 structural predicate translation (claims requiring slot-to-qualifier handling).
- 6 cross-context belief revision (Tier U contradiction detection).
- 6 principled abstention (claims that should abstain because no source supports them).

Target accuracy under Phase 10.5: 80% on derivation_corpus (lower than other corpora because end-to-end derivation involves more LLM-mediated steps and live KB calls).

**Calibration deferral.** Corpus authored in this phase; not executed. The walker is the most semantically demanding piece of the system and is the place where calibration deferral creates the most risk — passing unit tests with mocked oracles is significantly weaker evidence than running the corpus end-to-end. The per-phase plan must document this explicitly and ensure the unit/integration test suite includes representative cases for each of the six failure modes using the Wikidata fixture set from Phase 4 and seeded predicate-metadata rows. This phase's mock-only test coverage is the load-bearing pre-Phase-10.5 evidence that the walker works.

## Acceptance criteria

- All Phase 6 tests pass; total v0.15 test count approximately 460.
- `derivation_corpus.jsonl` exists with the documented case counts and sub-category distributions across the six failure modes, parses, and loads. **Execution deferred to Phase 10.5.**
- The walker correctly handles each of the six failure modes (one integration test case minimum per failure mode passes against fixtures and seeded substrate rows).
- Budget exceedance produces abstention with traceable budget consumption.
- Cycle detection prevents infinite walks on cyclic substrates.
- Polarity tracking correctly handles contradictory edges.
- Predicate-distribution gating correctly blocks invalid taxonomic compositions (e.g., "prefers" does not distribute up "is_a").
- Trace is serializable and re-derivable: a saved trace, when re-executed against the same substrate state, produces the same verdict.
- Zero false verifieds in the integration test suite's verified-expected cases.

## Phase-end commit

`v0.15-phase-6-complete`. Commit message: `v0.15 Phase 6: derivation walker`.

---

# Phase 7 — Python verification path

**Goal.** Single-generation Python verification. Sandbox integration. Justification structure (code + inputs + output + runtime metadata). Python-routed claims roundtrip.

**Dependencies.** Phase 0 (sandbox), Phase 3 (router classifies Python claims), Phase 6 (walker consults Python verifier).

## Scope: what's built

### PythonVerifier

`src/aedos_v0_15/layer4_sources/python_verifier.py`:

```python
class PythonVerifier:
    def __init__(self, sandbox, llm_client, audit_log):
        ...

    def verify(self, claim: Claim) -> PythonVerdict:
        """
        Single-generation: LLM generates Python code for the claim,
        sandbox executes it with typed inputs, return verdict + justification.
        """
        ...
```

The verification flow:
1. The LLM is given the claim (subject, predicate, object) and asked to generate Python code.
2. The prompt: "Given the claim <claim>, write a Python function `verify(subject, predicate, object) -> bool` that returns True if the claim holds. Use only the allowed standard library."
3. The code is executed in the sandbox with the claim's typed slot values.
4. The function's return value determines the verdict.
5. Exceptions are caught; runtime metadata is captured.

### Python verdict and trace integration

```python
@dataclass
class PythonVerdict:
    verdict: str                              # verified | contradicted | no_terminal_result
    generated_code: str
    inputs: dict
    output: Any
    runtime_metadata: dict                    # runtime_ms, exception_info
```

The walker (Phase 6) consults `python_verifier.verify(node)` for python-routed claims. The result is wrapped in a trace node.

### Tests

`tests/v0_15/unit/test_python_verifier.py`:
- Date arithmetic (e.g., "the date 2026-05-17 is 100 days after 2026-02-06" — verify or contradict).
- String operations (e.g., "the word 'strawberry' contains 3 'r' characters").
- Numerical comparison.
- List/set operations.
- Disallowed imports rejected at sandbox.
- Exception capture.

`tests/v0_15/integration/test_python_path.py`:
- End-to-end: claim → router (python) → python verifier → verdict.

Target ~40 new tests; total v0.15 test count approximately 500.

## Scope: what's not built

- Python rule caching (v0.16).
- Two-code-generation cross-check (v0.16; dropped from v0.15 per architecture).
- Layer 5 aggregation (Phase 8).

## Calibration corpus

`tests/v0_15/calibration/python_verification_corpus.jsonl` ~30 cases:
- 10 date arithmetic.
- 8 string operations.
- 6 numerical comparison.
- 6 list/set operations.

Target accuracy under Phase 10.5: 85% on python_verification_corpus (the LLM code generation is the variable; the sandbox is deterministic).

**Calibration deferral.** Corpus authored in this phase; not executed. The sandbox itself is deterministic and *is* exercised by unit tests in this phase — that is testing logic, not calibration. The LLM-mediated code-generation step is the deferred piece.

## Acceptance criteria

- All Phase 7 tests pass; total v0.15 test count approximately 500.
- `python_verification_corpus.jsonl` exists with the documented case counts across the four sub-categories, parses, and loads. **Execution deferred to Phase 10.5.**
- Sandbox unit tests achieve 100% on allowed/disallowed-import cases and on the sandbox's input/output handling (this is logic testing, not calibration).
- Python verdicts integrate cleanly with the walker's trace structure (testable with mocked LLM that returns canned code).
- Zero false verifieds on the Python integration test suite's verified-expected cases.

## Phase-end commit

`v0.15-phase-7-complete`. Commit message: `v0.15 Phase 7: Python verification path`.

---

# Phase 8 — Layer 5 + substrate-internal consistency check

**Goal.** Verification result aggregation. Consistency check with retract-both + circuit breaker. Retraction propagation through justification graphs. Downstream contradiction tracing infrastructure. End-to-end pipeline complete; correctness mechanisms in place.

**Dependencies.** Phases 0-7.

## Scope: what's built

### Layer 5 aggregator

`src/aedos_v0_15/layer5_result/aggregator.py`:

```python
class Aggregator:
    def __init__(self, audit_log):
        ...

    def aggregate(self, claims: list[Claim], per_claim_results: list[WalkResult]) -> VerificationResult:
        """
        Builds the verification result object per architecture Section 7.1.
        """
        ...
```

The `VerificationResult` dataclass per architecture:

```python
@dataclass
class VerificationResult:
    claims_extracted: list[Claim]
    per_claim_verdicts: dict[str, str]
    per_claim_traces: dict[str, JustificationTrace]
    aggregate_metadata: dict
    audit_log_entries: list[int]
    text_input: dict
    consistency_warnings: list[ConsistencyWarning]
```

### Substrate-internal consistency check

`src/aedos_v0_15/layer3_substrate/consistency.py`:

```python
class ConsistencyChecker:
    def __init__(self, db, audit_log, retraction_propagator, config):
        ...

    def check_on_write(self, table: str, row_id: int) -> ConsistencyResult:
        """
        Check a newly-written or updated row against neighbors.
        Returns ConsistencyResult indicating either Pass or Conflict.
        """
        ...

    def check_periodic(self) -> list[ConsistencyResult]:
        """
        Periodic batch scan over all substrate tables.
        Returns list of all detected conflicts.
        """
        ...

    def resolve_conflict(self, conflict: ConsistencyResult) -> None:
        """
        Retract-both. Check circuit breaker.
        If under threshold: retract both rows, log, update circuit_breaker table.
        If at threshold: mark substrate question unresolvable.
        """
        ...
```

The three inconsistency classes (per architecture Section 5.4) are detected:
- Transitive equivalence violation (predicate translation rows).
- Contradicting subsumption verdicts.
- Conflicting distribution judgments.

When a conflict is detected, `resolve_conflict`:
1. Retracts both rows (sets retracted_at, retraction_reason).
2. Logs the consistency_violation event.
3. Increments `cycle_count` in `consistency_circuit_breaker` for the substrate question's signature.
4. If `cycle_count` reaches the configured threshold (default 3), sets `unresolvable = 1` and logs `circuit_breaker_triggered`.

### Retraction propagation

`src/aedos_v0_15/layer5_result/retraction.py`:

```python
class RetractionPropagator:
    def __init__(self, db, audit_log):
        ...

    def propagate_retraction(self, retracted_subject: str) -> list[VerdictRetraction]:
        """
        Given a retracted row (substrate, tier_u, or cache entry),
        find all verdicts whose justification traces include it.
        Mark each for re-derivation. Log retraction events.
        """
        ...
```

For each retracted row, the propagator queries verdict traces (stored in audit log or a dedicated traces table) and identifies verdicts whose chain includes the retracted item. Those verdicts are marked retracted; re-derivation is triggered on next consultation.

### Downstream contradiction tracing

`src/aedos_v0_15/layer5_result/contradiction_tracer.py`:

```python
class ContradictionTracer:
    def __init__(self, db, audit_log, retraction_propagator):
        ...

    def trace_contradiction(self, contradicted_verdict_id: str, contradicting_premise: dict) -> None:
        """
        Given a verdict shown wrong (by later premise, by deployment correction),
        walk the verdict's trace, identify contributing rows, retract those rows.
        """
        ...
```

This is the mechanism by which external corrections feed back: the deployment surfaces a user-visible contradiction → the contradiction tracer walks the verdict's trace → contributing substrate rows are retracted → retraction propagates.

### Tests

`tests/v0_15/unit/test_consistency_checker.py`:
- Each of the three inconsistency classes detected.
- Retract-both behavior.
- Circuit breaker increments and triggers.

`tests/v0_15/unit/test_retraction_propagator.py`:
- Retracting a row triggers verdict re-derivation.
- Multiple verdicts dependent on one row all retracted.

`tests/v0_15/unit/test_contradiction_tracer.py`:
- External correction traces back through verdict's chain to substrate rows.
- Retracted rows trigger propagation.

`tests/v0_15/integration/test_end_to_end.py`:
- Full pipeline: (text, context) → extraction → routing → walker → verification result.
- Verification result has all required fields with traces.
- Consistency checker catches a deliberately-injected conflict.
- Retraction propagation works end-to-end.

Target ~70 new tests; total v0.15 test count approximately 570.

## Scope: what's not built

- Chat-wrapper deployment intervention (Phase 9).
- Audit-log query endpoints (Phase 10).

## Calibration corpus

`tests/v0_15/calibration/consistency_check_corpus.jsonl` ~25 cases:
- 10 cases with deliberately-conflicting rows; expected: retract-both detected.
- 8 cases where retract-and-regenerate produces consistent rows; expected: convergence.
- 7 cases where regeneration produces the same conflict repeatedly; expected: circuit breaker triggers within N cycles.

Target under Phase 10.5: 100% detection of seeded conflicts; 100% correct circuit breaker behavior. The detection logic is deterministic (it inspects substrate rows for conflicts); the regeneration logic involves LLM calls (the predicate translation oracle is re-consulted with hint context).

**Calibration deferral (partial).** The seeded-conflict detection sub-corpus (10 cases) and the circuit-breaker-trigger sub-corpus (7 cases) are *deterministic* and can be exercised by the unit test suite in this phase with mocked oracles — the consistency check is purely structural for those cases, and a 100% pass rate against those cases is an acceptance criterion for this phase. The regeneration-convergence sub-corpus (8 cases) requires live LLM calls (the regenerated rows are LLM-mediated) and is deferred to Phase 10.5.

## Acceptance criteria

- All Phase 8 tests pass; total v0.15 test count approximately 570.
- `consistency_check_corpus.jsonl` exists with all 25 cases per the sub-category distribution, parses, and loads.
- The deterministic sub-corpora — seeded-conflict detection (10 cases) and circuit-breaker triggering (7 cases) — pass at 100% as part of the unit/integration test suite, using mocked oracles. **The regeneration-convergence sub-corpus (8 cases) is deferred to Phase 10.5.**
- End-to-end pipeline produces a complete VerificationResult for representative claim sets (with mocked LLM and fixture KB).
- Retraction propagation correctly identifies and retracts dependent verdicts.
- Downstream contradiction tracing works for injected external corrections.
- Zero false verifieds in the integration test suite's verified-expected cases.

## Phase-end commit

`v0.15-phase-8-complete`. Commit message: `v0.15 Phase 8: Layer 5 + consistency check`.

---

# Phase 9 — Chat-wrapper deployment + intervention model

**Goal.** The deployment layer for the chat-wrapper. Pass-through / abstain / correct / decline interventions. FastAPI chat endpoint. The four-move intervention logic against the verification result.

**Dependencies.** Phases 0-8.

## Scope: what's built

### Chat wrapper

`src/aedos_v0_15/deployment/chat_wrapper.py`:

```python
class ChatWrapper:
    def __init__(self, extractor, router, walker, aggregator, llm_client, config):
        ...

    def respond(self, user_message: str, conversation_context: dict) -> ChatResponse:
        """
        End-to-end:
        1. Generate the LLM's draft response.
        2. Extract claims from the draft.
        3. Verify each claim.
        4. Apply intervention model (pass-through, abstain, correct, decline).
        5. Return final response + verification trace.
        """
        ...
```

The intervention logic per architecture Section 4.6:

- **Pass-through.** All claims verified or out-of-scope. Return draft unmodified.
- **Abstain.** Some claims could not be grounded. Either remove the ungrounded sentence from the draft, or annotate it with "I couldn't verify the claim that X" — deployment configuration determines which.
- **Correct.** Some claims contradicted. Rewrite the draft replacing contradicted claims with corrected versions (sourcing from the trace's KB statement or Tier U row).
- **Decline.** Response dominated by ungrounded/contradicted content. Return a refusal message.

The choice between interventions is determined by:
- All verified or pass-through-triaged → pass-through.
- Some abstained, none contradicted → abstain (annotate or remove).
- Some contradicted, the rest verified → correct.
- More than 50% of claims contradicted or abstained → decline.

### FastAPI chat endpoint

`src/aedos_v0_15/app.py` gains:
- `POST /chat` — request body has `message`, `conversation_id`, `asserting_party_id`. Response is `ChatResponse` with the final message, intervention type, and a summary of the verification result.
- `GET /verification/{verification_id}` — retrieve the full verification result for a chat response.

### Tests

`tests/v0_15/unit/test_chat_wrapper.py`:
- Each intervention type triggered by appropriate verification result.
- Edge cases: empty draft, draft with no extracted claims.

`tests/v0_15/integration/test_chat_endpoint.py`:
- End-to-end via the API.

`tests/v0_15/smoke/smoke_corpus.jsonl` ~30 cases covering each intervention type and the six failure modes.

Target ~50 new tests; total v0.15 test count approximately 620.

## Scope: what's not built

- Audit-log query endpoints beyond `/verification/{id}` (Phase 10).
- Seed pack (Phase 10).
- Cold-start documentation (Phase 10).

## Calibration corpus

`tests/v0_15/calibration/intervention_corpus.jsonl` ~30 cases:
- 10 pass-through cases.
- 8 abstain cases.
- 7 correct cases.
- 5 decline cases.

Each case: (input draft, expected intervention type, expected final response shape).

Target accuracy under Phase 10.5: 90% intervention-type-classification correctness when run end-to-end against live LLM and live KB.

**Calibration deferral.** Corpus authored in this phase; not executed end-to-end. The intervention-type *classification logic itself* (given a verification result, which intervention type applies) is deterministic per the rules in architecture Section 4.6 and is exercised at 100% by unit tests in this phase. The end-to-end calibration evaluates whether the full pipeline (draft generation → extraction → verification → intervention selection) reaches the right classification; that's the deferred piece.

## Acceptance criteria

- All Phase 9 tests pass; total v0.15 test count approximately 620.
- `intervention_corpus.jsonl` exists with the documented case counts across the four intervention types, parses, and loads. **End-to-end execution deferred to Phase 10.5.**
- Unit tests for the deterministic intervention-selection logic pass at 100% (given a synthetic VerificationResult, the correct intervention type is chosen).
- The chat endpoint roundtrips through the full pipeline correctly under mocked LLM and fixture KB.
- Each intervention type produces a sensible final response on representative inputs (mocked LLM).
- Smoke corpus passes end-to-end under mocks/fixtures.
- Zero false verifieds in chat-wrapper end-to-end tests.

## Phase-end commit

`v0.15-phase-9-complete`. Commit message: `v0.15 Phase 9: chat-wrapper deployment`.

---

# Phase 10 — Hardening + Wikidata seeds + cold-start documentation + medium-bar evaluation

**Goal.** The optional seed pack. Cold-start test (zero-seed run on representative claim set). Audit-log query endpoints. Operational documentation. **Plus** the medium-bar evaluation: held-out evaluation on a benchmark or curated test set demonstrating Aedos's improvements over an LLM-only baseline on the six failure modes.

**Dependencies.** Phases 0-9.

## Scope: what's built

### Predicate translation seed pack

`seeds/v0_15/predicate_translation.json` is created with ~60-80 hand-curated mappings. Each follows the schema in architecture Section 9.2. Coverage spans:

- Role/employment predicates (holds_role → P39, educated_at → P69, employed_by → P108, etc.).
- Location predicates (lives_in → P551, located_in → P276, born_in → P19, died_in → P20, etc.).
- Kinship predicates (spouse_of → P26, parent_of → P22/P25, sibling_of → P3373, child_of → P40).
- Categorical predicates (is_a → P31, instance_of → P31, subclass_of → P279).
- Mereological predicates (part_of → P361, has_part → P527, contains → P527 inverse).
- Quantitative predicates (population_of → P1082, area_of → P2046, elevation_of → P2044, founded_in_year → P571).
- Event predicates (occurred_in → P585, founded_by → P112).

The seeds are version-stamped. A `seeds/v0_15/SEED_VERSION.txt` file records the version and the date of last review.

A `python seeds/v0_15/load_seeds.py` script loads the seeds into a deployment's database. This is *optional* per architecture Section 9.2; a deployment that runs without loading seeds is fully functional.

### Cold-start zero-seed test

`tests/v0_15/cold_start/test_zero_seed_correctness.py`:
- Initialize a fresh database with v0.15 schema, no seeds loaded.
- Run a representative claim set (10 claims spanning all routes) end-to-end.
- Verify each claim produces the expected verdict.
- Measure first-claim latency (the most expensive case) vs. tenth-claim latency (the LLM-call-amortized case).

The test scaffolding is written in this phase. **Execution is deferred to Phase 10.5** — it requires live LLM calls and (for KB-routed claims) live Wikidata. In this phase the scaffolding is exercised with mocked LLM and fixture KB to confirm the test harness works structurally; the assertion "all 10 claims produce expected verdicts against the live system" is a Phase 10.5 acceptance criterion.

### Audit-log query endpoints

`src/aedos_v0_15/app.py` gains:
- `GET /audit/substrate-rows` — list substrate rows, with filters by table, retracted status, predicate.
- `GET /audit/consistency-checks` — list consistency-check reports.
- `GET /audit/circuit-breakers` — list triggered circuit breakers.
- `GET /audit/retractions` — list retraction events with reasons.

All endpoints are query-only. None mutate substrate rows. (The architecture's no-operator-in-the-loop commitment means there is no `POST /audit/retract` endpoint.)

### Cold-start documentation

`docs/v0_15/cold_start.md`:
- Deployment guide: clone repo, install dependencies, configure env vars, initialize database.
- Zero-seed configuration: how to run without loading seeds; expected first-use latency.
- Seed configuration: how to load the optional seed pack; trade-offs.
- Operational recommendations: monitoring the audit log, interpreting circuit breaker reports, when to investigate consistency violations.

### Medium-bar evaluation scaffolding

`tests/v0_15/evaluation/benchmark.py` is **written but not executed** in this phase. It contains the runner code, the baseline driver, the metrics computation, and the per-failure-mode breakdown logic.

**Test set construction.** A curated set of 100-150 cases is authored in this phase as `tests/v0_15/evaluation/medium_bar_test_set.jsonl`. Each case has:
- A natural-language statement that an LLM might produce.
- A ground-truth verdict (verified, contradicted, abstain).
- A categorization into one of the six failure modes.

The test set is biased toward the failure modes Aedos addresses; this is intentional (the medium-bar evaluation is meant to show Aedos's advantage where Aedos's design is meant to help). The test set's bias is documented in the evaluation methodology.

**Baseline.** An LLM-only baseline: the same chat LLM asked to evaluate each statement's correctness in one forward pass with access to a web search tool but no other architectural support. The baseline runner is written in this phase but not invoked.

**Aedos.** The v0.15 system as built through Phase 9, configured with the Wikidata adapter and the optional seed pack loaded. The Aedos runner is written in this phase but not invoked.

**Metrics.**
- Accuracy (correct verdicts / total).
- False-verified rate (incorrect verdicts marked verified / total verifieds).
- False-abstain rate (correct claims that Aedos abstained on / total correct claims).
- Per-failure-mode breakdown.

**Acceptance for Phase 10.5 (not this phase).** Aedos's false-verified rate is ≤ 5% (the soundness commitment in practice). Aedos's overall accuracy is ≥ baseline + 15 percentage points on the curated test set. Per-failure-mode, Aedos's accuracy is ≥ baseline on every mode (no regression) and significantly higher on at least 4 of the 6 modes.

The evaluation methodology, results, and per-failure-mode analysis are documented in `docs/v0_15/evaluation_results.md` once Phase 10.5 completes.

### Tests

`tests/v0_15/cold_start/` — zero-seed correctness test scaffolding (execution deferred).
`tests/v0_15/evaluation/` — benchmark runner code and result-analysis code (execution deferred).
`tests/v0_15/integration/test_audit_endpoints.py` — audit-log query correctness (this *is* run in Phase 10 because it doesn't require live LLM or live KB).

Target ~40 new tests; total v0.15 test count approximately 660.

## Scope: what's not built

- A second KB implementation (post-v0.15 paper-experiment scope).
- Non-chat deployment (post-v0.15).
- **Execution of the cold-start test, the medium-bar evaluation, or any deferred calibration corpora** — all of that is Phase 10.5.

## Acceptance criteria

- All Phase 10 tests pass; total v0.15 test count approximately 660. (This excludes the deferred-execution scaffolding, which counts as code but does not run as tests in this phase.)
- Seed pack file exists at `seeds/v0_15/predicate_translation.json` with ≥ 60 mappings spanning the documented categories. A unit test confirms the seed file parses, validates against the schema, and loads cleanly into a fresh database (no LLM or KB calls required).
- Zero-seed cold-start test scaffolding exists and structurally executes against mocks/fixtures. **End-to-end zero-seed execution deferred to Phase 10.5.**
- Audit-log query endpoints return correct results on synthetic audit-log data (testable without live LLM).
- Cold-start documentation is complete in `docs/v0_15/cold_start.md`.
- Medium-bar test set exists at `tests/v0_15/evaluation/medium_bar_test_set.jsonl` with 100-150 cases distributed across the six failure modes, parses, and loads. Methodology documented in `docs/v0_15/evaluation_methodology.md`. **Execution deferred to Phase 10.5.**
- Benchmark runner code (`benchmark.py`) exists and structurally executes against mocks/fixtures to confirm the harness works. **Live execution deferred to Phase 10.5.**
- Zero false verifieds across the v0.15 test suite remains true.
- A `docs/v0_15/phase_10_5_runbook.md` is produced as a handoff document: it lists every command the operator needs to run for Phase 10.5, the order to run them in, the expected runtime and cost for each, and the acceptance thresholds (reproduced from the "Calibration deferral policy" section above).

## Phase-end commit

`v0.15-phase-10-complete`. **Do NOT tag `v0.15.0`** — that tag is reserved for after Phase 10.5 passes. Commit message: `v0.15 Phase 10: hardening + scaffolding (calibration deferred to Phase 10.5)`.

**This is the end of the unattended overnight run.** The session stops here. It does not proceed to Phase 10.5. It does not proceed to the v0.14 deletion commit. The operator reviews the run log, reviews any blockers or ambiguities recorded, and decides whether to invoke Phase 10.5.

---

# Phase 10.5 — Calibration Pass (operator-supervised)

**Goal.** Run every deferred calibration corpus, the cold-start zero-seed test, and the medium-bar evaluation. Confirm the system meets the original acceptance thresholds. This phase is **not part of the unattended overnight run**.

**Dependencies.** Phases 0-10 complete and tagged. Operator present.

## Scope

- Set `RUN_CALIBRATION=1`, `RUN_LIVE_TESTS=1`, `RUN_LIVE_KB=1` and run each calibration corpus from its owning phase. Record per-corpus accuracy. Compare against the thresholds in the "Calibration deferral policy" table.
- Run the cold-start zero-seed test against live LLM + live Wikidata. Confirm all 10 representative claims produce expected verdicts.
- Run the medium-bar evaluation: baseline + Aedos against the curated test set. Confirm the four acceptance thresholds (false-verified ≤ 5%, accuracy ≥ baseline + 15 pp, no per-mode regression, significant improvement on ≥ 4 of 6 modes).
- Run the regeneration-convergence sub-corpus of `consistency_check_corpus.jsonl` (deferred from Phase 8). Confirm 100% convergence.

## Acceptance criteria

- Every deferred corpus meets its threshold (per the table above).
- Cold-start zero-seed test passes.
- Medium-bar evaluation passes all four thresholds.
- Zero false verifieds across the entire live-run test suite.

## Phase-end commit

Tag `v0.15.0` on the Phase 10 commit. Commit message (on a follow-up commit recording the Phase 10.5 results): `v0.15.0: release (calibration pass complete)`.

If any threshold fails, a targeted fix-up session is opened against the responsible layer. Phase 10.5 is re-run after the fix-up. `v0.15.0` is not tagged until everything passes.

---

# After Phase 10.5: the v0.14 deletion commit

When `v0.15.0` is tagged (which only happens after Phase 10.5 passes), a single follow-up commit on the `v0.15` branch:

1. Deletes the v0.14 codebase under `src/`.
2. Renames `src/aedos_v0_15/` to `src/`.
3. Updates `pyproject.toml`, `Makefile`, CI configuration, and any other references.
4. Re-runs the full test suite to confirm no regressions.

Commit message: `v0.15: replace v0.14 codebase`. Tag: `v0.15.1` (the first version that lives as the canonical Aedos).

---

# Cross-phase concerns

## Test count tracking

Each phase has a target test count. The expected progression:

- Phase 0: ~30 tests
- Phase 1: ~110 tests (cumulative)
- Phase 2: ~160 tests
- Phase 3: ~230 tests
- Phase 4: ~320 tests
- Phase 5: ~380 tests
- Phase 6: ~460 tests
- Phase 7: ~500 tests
- Phase 8: ~570 tests
- Phase 9: ~620 tests
- Phase 10: ~660 tests

These are *targets, not contracts*. Each phase may produce slightly more or fewer tests; the soundness criterion (zero false verifieds) is the load-bearing acceptance criterion, not the test count itself.

## Calibration corpus discipline

Each corpus is owned by its producing phase. The corpus is:
- Versioned (each phase tags its corpus).
- Documented (a header comment explains the corpus's scope and acceptance threshold).
- Stable (cases are not removed or modified silently; new cases are added in subsequent phases as needed).

The corpora collectively are the system's testable specification. The architecture is what it claims; the calibration corpora are what the architecture must accomplish.

## API cost projection

Costs split between the overnight run and Phase 10.5.

**Overnight run (Phases 0-10) — calibration deferred, mocked LLM in tests.** The system *itself* makes no live LLM calls during the overnight build because tests run against mocked clients. The LLM calls during this run come from the Claude Code session driving the build (the coding agent itself), not from running the system. Those are billed against the operator's Anthropic API key for the Claude Code session, not against the system's configured providers. Expected coding-agent cost: variable by run length, typically $10-40 for a build of this size with Sonnet 4.6.

**Phase 10.5 (operator-supervised).** This is where the original $250-400 estimate lives. Breakdown:

- Phase 1 calibration (extraction): ~$5-10.
- Phase 2 calibration (predicate translation): ~$20-30.
- Phase 4 calibration (KB protocol, live Wikidata): ~$10-15.
- Phase 5 calibration (subsumption + distribution): ~$15-20.
- Phase 6 calibration (derivation walker, multi-step): ~$30-50.
- Phase 7 calibration (Python): ~$10.
- Phase 8 calibration (consistency regeneration sub-corpus): ~$5.
- Phase 9 calibration (intervention end-to-end): ~$15-25.
- Cold-start zero-seed test (live): ~$5-15.
- Medium-bar evaluation: ~$100-150 (largest single line item).

Total Phase 10.5 cost: ~$250-400.

## Operating modes

The variant respects three operating modes, but only one is active during the overnight run:

- **Default (overnight run):** Run all unit and integration tests with mocked LLM and fixture KB. Fast, no system-side API costs. **No environment variables set.**
- **`RUN_LIVE_TESTS=1`:** Reserved for Phase 10.5. Adds live LLM tests.
- **`RUN_LIVE_KB=1`:** Reserved for Phase 10.5. Adds live Wikidata tests.
- **`RUN_CALIBRATION=1`:** Reserved for Phase 10.5. Runs the full calibration corpus.

The default `make test` runs only fast tests; this is what runs during the overnight build. `make calibrate` runs the calibration suite (Phase 10.5 only). `make evaluate` runs the benchmark (Phase 10.5 only).

## Branch and commit discipline

All v0.15 work lives on a single branch: `v0.15`. The overnight session produces a range of commits within the branch, terminating in `v0.15-phase-10-complete`. Phase 10.5 produces additional commits and the final `v0.15.0` tag. The branch is rebased onto main if needed before tagging `v0.15.0`.

No phase merges into main until Phase 10.5 is complete and the v0.14 deletion commit is ready.

---

# Surfacing ambiguity

The first task of each phase within the unattended session is to read the architecture document and this implementation plan, write `docs/v0_15/phase_N_plan.md` proposing a concrete implementation plan for the phase, and surface any ambiguities. **Because there is no operator present, ambiguities are not "resolved by the operator" — they are resolved by the session itself in writing.** Every ambiguity surfaced is recorded in `docs/v0_15/phase_N_ambiguities.md` with the resolution chosen, the reasoning, and an explicit note on which alternative was rejected and why. The default bias is toward the more conservative interpretation — the one that makes false verifieds less likely. After writing the per-phase plan and ambiguity document, the session proceeds to implementation.

Specific ambiguities likely to surface (anticipated, not exhaustive):

- **Phase 1:** What exactly counts as a "user-authoritative" predicate at extraction vs. routing? Resolution: the extractor doesn't decide; it produces relational claims with normalized predicates, and Layer 2 (Phase 3) classifies.

- **Phase 2:** What does the predicate translation oracle return when the LLM confidently says "no Wikidata mapping exists for this predicate"? Resolution: a row is stored with `routing_hint=abstain` and null kb_namespace/kb_property; this is a valid metadata state.

- **Phase 4:** When entity resolution returns multiple equally-strong candidates and the LLM tiebreaker is itself uncertain, what does the resolver return? Resolution: the top candidate by combined score, with the trace recording the ambiguity. If a downstream verdict turns out wrong, the trace points to entity_resolution_cache.id, which can be retracted.

- **Phase 4 (variant-specific):** How are Wikidata fixtures chosen and authored, given the live API is not available during the overnight run? Resolution: fixtures are hand-authored to mirror the documented Wikidata response shape (the adapter is parsing well-defined JSON), covering exactly the entities and properties referenced by the smoke corpus, the seed pack, and the Phase 4 unit tests. Each fixture file includes a comment recording the source Q-number/P-number it represents so Phase 10.5 can validate against live Wikidata. If a Phase 6/8/9 test requires a fixture that doesn't exist yet, the session adds it with a header comment and continues.

- **Phase 6:** What if a substrate row needed for an edge is mid-regeneration (the circuit-breaker has triggered) during a walk? Resolution: the walker treats the substrate question as unresolvable for this walk; the edge is skipped; the walk continues with other edges; if no other edges produce a verdict, abstain.

- **Phase 8:** When retraction propagation finds 100+ dependent verdicts, what does the system do? Resolution: process them in batches; the retraction is recorded in the audit log; if the propagation takes too long for a single transaction, it spans multiple transactions with safe-to-resume markers.

These are example ambiguities. Each phase's session is expected to find more and resolve them against the architecture document.

---

*End of v0.15 implementation plan, Draft 1 — Overnight / Calibration-Deferred Variant.*