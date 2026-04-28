# Observations

Use this file for things that surprise you, patterns you notice across
multiple traces, hypotheses you don't have time to test yet, anomalies in
GLM behavior, anything that future-you or future-me would want to know.
Treat it like a research notebook. Date every entry. Clarity over polish.

When an observation suggests work, also add a NEXT_STEPS item linking back
to the observation by date. The two files are complementary: NEXT_STEPS is
"what to do," OBSERVATIONS is "what's interesting."

---

## 2026-04-27 — GLM-5.1-FP8 first smoke test

Three turns through full pipeline (`scripts/smoke_test_glm.py`) with
AEDOS_CHAT_MODEL_PROVIDER=modal. All three turns succeeded end-to-end —
GLM produced correct answers, AEDOS extracted the right claims, the
router routed to python, code-gen verified each as correct, no
correction needed.

| turn | prompt | GLM response | extracted claim | verdict |
|------|--------|--------------|-----------------|---------|
| 1 | "How many r's are in strawberry?" | "There are 3 r's in strawberry." | quantitative.has_count(strawberry, letter_r, 3) | verified |
| 2 | "What's 23 × 47?" | "23 × 47 = 1081" | quantitative.product_equals(23×47, product, 1081) | verified |
| 3 | "Spell egalitarian backwards." | "nairatilage" | relational.reverse_of(nairatilage, reverse_of, egalitarian) | verified |

### Behavioral notes

- **Reasoning model.** GLM-5.1-FP8 emits a separate `reasoning_content`
  field in addition to `content` in the OpenAI-style response. The
  reasoning chain is long (e.g. ~207 reasoning tokens for the strawberry
  count). When `max_tokens` is too low, the model exhausts the budget on
  reasoning and `content` ends up `null`. The Modal client now raises
  `ModalResponseError` with a "try larger max_tokens" hint when this
  happens.

- **Cold start is brutal.** First turn after idle: 275s. Subsequent warm
  turns: 25–35s. The Modal container needs minutes to spin up GLM-5.1-FP8
  weights. A 60s timeout was way too tight; bumped default to 300s.

- **Concurrency limit is 1 (or close).** Concurrent requests get
  `429 "Too many concurrent requests for this model"`. A timed-out
  request appears to keep its slot occupied until the upstream finishes
  generating (or possibly the proxy gives up). Practical implication:
  the smoke test must be strictly sequential with a small inter-turn
  delay; can't parallelize evaluation runs against one Modal endpoint.

- **No hallucinations on these three.** All three are exactly the kind
  of prompts AEDOS targets (counting, arithmetic, string reversal) and
  GLM nailed all three. Either GLM-5.1 is stronger than expected on this
  class, or the prompts were too easy. Phase 2 dogfooding needs harder
  prompts (obscure trivia, tricky character counts like "how many m's
  in commitment", confabulation-prone questions about non-famous
  entities) to actually exercise the verification path with
  contradicted/inconclusive verdicts. **Working hypothesis:** simple
  arithmetic and short-string operations are within GLM's competence;
  hallucinations will surface in retrieval-territory claims (specific
  facts about non-famous entities) more than in python-territory ones.
  Worth retesting after Phase 2 with prompts targeted at retrieval.

- **Latency is the operator-facing concern.** Even warm, 30s per turn
  is uncomfortable for interactive dogfooding. If we want to do the
  10-20 turn calibration session in Phase 2 in a sitting, that's 5–10
  minutes of waiting in the best case. May be worth running dogfooding
  via a script that enqueues a list and dumps results, rather than the
  UI.

### Open threads

- Phase 2 prompts must include hard cases the strawberry/multiplication
  set didn't hit: obscure factoid retrieval, multi-claim responses,
  user-authoritative recall, contradicted-by-prior-statement.
- Should we cache the chat response in dev so we can iterate on the
  pipeline without paying GLM's 30s per turn each time? Probably not —
  it would defeat the purpose of dogfooding and also let stale GLM
  responses drift away from current model behavior. But worth a note
  in case dev velocity becomes painful.
