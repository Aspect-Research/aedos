# Phase 9 Plan — Chat-Wrapper Deployment + Intervention Model

## Goal

Deploy the verification engine as a chat wrapper. Four-move intervention model: pass_through,
abstain, correct, decline. FastAPI `POST /chat` and `GET /verification/{id}` endpoints.

## What's built

### `src/aedos_v0_15/deployment/chat_wrapper.py`

`InterventionType(str, Enum)`: PASS_THROUGH | ABSTAIN | CORRECT | DECLINE

`ChatResponse` dataclass:
- `final_message: str`
- `intervention_type: str`
- `verification_result: VerificationResult`
- `verification_id: str`
- `draft_message: str`

`ChatWrapper(extractor, walker, aggregator, llm_client, config)`:
- `respond(user_message, conversation_context) -> ChatResponse`:
  1. Generate LLM draft via `llm_client.chat()`
  2. Extract claims from draft via `extractor.extract()`
  3. Verify each via walker + aggregate
  4. `select_intervention(verification_result)` → InterventionType
  5. `build_response(draft, intervention_type, verification_result)` → final_message
  6. Return ChatResponse

`select_intervention(vr: VerificationResult) -> InterventionType` (deterministic):
- total = claim_count; if 0 → PASS_THROUGH
- contradicted_count + abstained_count > total * 0.5 → DECLINE
- contradicted_count > 0 → CORRECT
- abstained_count > 0 → ABSTAIN
- else → PASS_THROUGH

`build_response(draft, intervention_type, vr) -> str` (mocked-LLM-safe):
- PASS_THROUGH: draft unchanged
- ABSTAIN: draft + "\n\n[Note: some claims could not be verified.]"
- CORRECT: draft + "\n\n[Note: some claims were corrected based on verified sources.]"
- DECLINE: "I'm unable to provide a response I cannot verify."

### `src/aedos_v0_15/app.py` additions

`POST /chat` — body: `{message, conversation_id, asserting_party_id}` →
  response: `{final_message, intervention_type, verification_id}`

`GET /verification/{verification_id}` — returns stored verification result (in-memory store for
Phase 9; persistent in Phase 10).

### Tests

`tests/v0_15/unit/test_chat_wrapper.py`:
- Each intervention type triggered by synthetic VerificationResult
- Zero-claim draft → pass_through
- All verified → pass_through
- Mix abstained + none contradicted → abstain
- Any contradicted → correct
- >50% contradicted → decline
- Response text per intervention type

`tests/v0_15/integration/test_chat_endpoint.py`:
- API roundtrip via TestClient
- Correct intervention type returned

## Ambiguities resolved

1. **Triage-only claims** (routed to abstain at extraction): not in per_claim_verdicts since walker
   skips SKIP/ABSTAIN-triaged claims. The wrapper only verifies VERIFY-triaged claims; the total
   used for the >50% rule is the number of VERIFY-triaged claims, not the total extracted.
2. **Empty claims list**: no claims extracted → PASS_THROUGH (nothing to verify).
3. **Correct response text**: Phase 9 uses a fixed annotation rather than LLM rewrite, since
   the rewrite path needs KB-grounded replacement text which requires more infrastructure. The
   "correct" intervention adds a correction note; full LLM rewrite is Phase 10 scope.
