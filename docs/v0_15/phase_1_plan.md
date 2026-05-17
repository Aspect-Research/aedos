# Phase 1 Plan — Extraction (Layer 1)

## Summary

Phase 1 produces the extraction layer: given (text, context), produces a list of structured relational claims. This includes predicate normalization, multi-participant decomposition, temporal scope handling, verifiability triage, hard-claim discipline, first-person canonicalization, source-text discipline, and contrastive correction handling.

The extractor uses a mocked LLM in tests — it does not make live API calls. Test correctness is verified by asserting over the structured claims the mocked extractor returns.

## File list

### Modified
- `src/aedos_v0_15/layer1_extraction/extractor.py` — `Extractor` class + `Claim`, `ExtractionContext` dataclasses
- `src/aedos_v0_15/layer1_extraction/normalization.py` — `normalize_predicate()`
- `src/aedos_v0_15/layer1_extraction/decomposition.py` — `decompose_event()`
- `src/aedos_v0_15/layer1_extraction/temporal.py` — `extract_temporal_scope()`, `TemporalScope`
- `src/aedos_v0_15/layer1_extraction/triage.py` — `triage()`, `TriageDecision`

### New tests
- `tests/v0_15/unit/test_extractor.py`
- `tests/v0_15/unit/test_normalization.py`
- `tests/v0_15/unit/test_decomposition.py`
- `tests/v0_15/unit/test_temporal.py`
- `tests/v0_15/unit/test_triage.py`

### New calibration corpus
- `tests/v0_15/calibration/extraction_corpus.jsonl` — ~60 cases

## Test plan

Target: ~80 new tests (cumulative ~158 including Phase 0's 78).

| Module | Coverage | Count |
|---|---|---|
| test_extractor.py | Claim dataclass fields, mocked extraction roundtrip, hard-claim discipline, source-text discipline, first-person canonicalization, future-tense rejection, contrastive corrections | ~25 |
| test_normalization.py | Snake_case, tense-neutral, voice-neutral, common forms | ~15 |
| test_decomposition.py | Multi-participant events → binary claims with shared reified_event_id | ~10 |
| test_temporal.py | Explicit scope, implicit past tense, relative scope, no-markers, future-tense | ~15 |
| test_triage.py | Each triage rule (numeric, temporal, anchor entity, comparative, always-verify) | ~15 |

## Calibration corpus adversarial-coverage strategy

The corpus covers 5 sub-categories (60 cases total). The adversarial constraint: each sub-category must include cases designed to fail in ways the implementation is tempted to paper over.

**Normalization (15 cases):** Include 5 adversarial cases where the surface form is subtly different from the canonical form — e.g., "is employed by" vs "employed_by", "was born" vs "born_in", passive constructions ("was awarded the prize" → `received_award`). These test whether normalization handles irregular forms, not just common ones.

**Multi-participant decomposition (10 cases):** Include 3 cases with 3+ participants (not just pairs), 2 cases with nested events, 2 cases where the extractor must not decompose a clearly binary relation (e.g., "Asa and Bob are friends" should produce `has_friendship(Asa, Bob)`, not event decomposition). These test that decomposition is applied selectively.

**Temporal scope (15 cases):** Include 3 cases with conflicting temporal signals (past tense verb with present-tense date marker), 2 cases with relative scope ("was there when Obama was president"), 2 cases with `before_present` sentinel correctly applied vs. explicit dates, and 2 future-tense rejection cases (must not extract as claims).

**Hard-claim discipline (10 cases):** Include 5 cases where entities are mentioned in context (prior conversation) but not asserted in the text being extracted — the extractor must NOT produce claims about those entities. Include 2 cases where `source_text` discipline could be violated by returning a paraphrase.

**First-person canonicalization (10 cases):** Include cases for chat user ("I graduated from Williams College" → asserting_party=user_test), document author ("We found in our study that..." → document_id), and deployment config context. 2 adversarial cases: "I" in a quoted sentence inside text (should it canonicalize?) — resolution: yes, first-person in ANY text we're extracting refers to asserting party.

## Ambiguities

See `docs/v0_15/phase_1_ambiguities.md`.

## Architecture decisions this phase

- The `Extractor.extract()` method uses the LLM's `extract_with_tool` call to get structured claims in a single LLM call (structured output).
- The tool schema enforces the binary-relational shape with all required fields.
- Normalization is applied to the raw predicate after the LLM extraction step (a post-processing step, not part of the LLM prompt).
- Decomposition is triggered when the LLM extraction step returns a claim with `reified_event_id` set — the LLM is prompted to produce multi-participant claims as a set of binary claims with a shared event ID.
- The `TemporalScope` extraction is part of the LLM tool schema (not a separate call).
- `triage()` is deterministic (no LLM call) — it runs after extraction.
