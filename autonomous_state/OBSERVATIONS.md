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

## 2026-04-27 — Phase-2 dogfood (in progress, 7/17 turns done)

Findings so far from `scripts/dogfood_glm.py`:

### GLM is acing python-territory questions

Turns 1–5 (count_m_in_commitment, count_vowels_serendipitous, mult_13_17,
sqrt_sum, date_diff_moon) all routed to python with confidence ≥ 0.98
and verified successfully. GLM's answer for each was correct on the
first try.

  * commitment has 3 m's (NOT 2 as the operator wrote in the
    prompt set — operator-prediction error, fixed in commit 70b4b65)
  * date math: 20,735 days from moon landing to 2026-04-27 — correct

This is the calibration win of v0.5: routing computable claims to python
catches none of these because none of them are wrong. The verifier
doesn't generate corrections, but its successful checks are
load-bearing — they confirm the model's confident answers.

### Real bug: canonical-constants cross-check breaks on Opus 4.7

Turn 6 (list New England states, routed to python_with_canonical_constants)
errored with `BadRequestError: 'temperature' is deprecated for this model`.
Anthropic deprecated the `temperature` parameter for `claude-opus-4-7`,
which is the default corrector_model. The cross-check in
`pipeline.verify_with_cross_check` runs the code-gen pipeline twice at
temperatures 0.0 and 0.3 — neither call lands now.

  * **Fix shipped (commit 6d466df):** `LLMClient.rewrite` now drops
    temperature when the model is `claude-opus-4-7`-prefixed and logs
    a warning. Pipeline no longer crashes.
  * **Caveat:** with temperature dropped, both cross-check calls run
    with the same params. The cross-check's value-add is now near-zero
    on opus models (LLM nondeterminism would still occasionally vary
    output, but the deliberate variation source is gone).
  * **Follow-up:** consider running the cross-check on Sonnet 4.6
    explicitly (via a per-stage model override), or use a different
    variation source (small prompt perturbation, swap the order of
    examples, etc.). NEXT_STEPS item.

### Real calibration gap: extractor produces zero claims for canonical lists

Turn 7 (days of the week). GLM produced a perfectly clean numbered list:
"The seven days of the week in order, starting with Monday, are: 1.
Monday 2. Tuesday ... 7. Sunday". The extractor returned `valid_facts:
[]` AND `rejected_facts: []` — nothing was even attempted.

This is a real v0.5 design gap: "list canonical items in order"
responses don't fit any pattern in `patterns.yaml`. The closest is
`quantitative` (subject + property + value), but the extractor doesn't
shoehorn the list into that shape. Result: the entire `python_with_
canonical_constants` path can never trigger from this kind of response,
because no claim ever gets extracted.

**Hypothesis:** the extractor would benefit from a worked example in
`extractor.py` for "the days of the week are: [Mon, Tue, ...]" → e.g.
`quantitative.has_value(subject="days_of_week", property="ordered_list",
value=["Monday", ..., "Sunday"])`. Then the LLM router would route to
`python_with_canonical_constants`, and the cross-check would verify
the list. Worth trying.

  * Adding this needs care — list-valued slots aren't well-supported
    by the current verifier's comparator. May need a list-comparison
    branch in `comparator.py`.
  * Alternative interpretation: this is correct behavior. "Days of the
    week" is canonical reference data, not a claim about the world.
    Verifying it is busy-work. Reasonable people could argue either
    way.

I lean toward "extract it" because the whole point of canonical-
constants verification is to catch the LLM emitting WRONG canonical
reference data ("the New England states are: Maine, Vermont, NH, MA,
RI, CT, Pennsylvania" — the LLM-generated trap is to add an extra
state). If we don't extract list responses, we never catch that.

## 2026-04-27 — Phase 6 (Tier 2 verification cache) — design sketch

Not implementing yet (spec gates Phase 6 on a clean Phase 2 dogfood
pass). Sketching the design here so the next session has clean pickup.

### Why a cache, not a knowledge base

Per spec: "the cache is a performance optimization for retrieval, not
a knowledge base. Cached entries can be wrong, can go stale, and are
subject to eviction. Every cached verdict is provisional."

The key tension: retrieval is the slowest, most expensive step of the
pipeline (DDG fetch + LLM judge). For repeated questions about
stable facts ("when was Tokyo founded", "who painted Persistence of
Memory"), re-running retrieval each time is wasteful. But cached
verdicts can go stale ("47th president" was true on 2026-04-27 but
won't be on 2029-01-20).

### Components (per spec)

1. **Scoping classifier** — LLM call per claim deciding:
     - `user_specific` → Tier 1 only, never cache
     - `session_specific` → don't cache (e.g. "it's raining outside")
     - `world_fact` → cache eligible

   Worked examples:
     - "I like peanut butter" → user_specific
     - "Tokyo is in Japan" → world_fact
     - "It's raining" / "this sentence has 7 words" → session_specific

2. **Stability classifier** — LLM call per cache-eligible claim,
   choosing TTL class:
     - `immutable` (mathematical, definitions): no expiration
     - `decade_stable` (geographic): 10 years
     - `years_stable` (political offices): 1 year
     - `months_stable` (cultural facts that change slowly): 30 days
     - `days_stable` (current events): 24 hours
     - `volatile` (prices, weather): don't cache OR 1-hour cap

3. **Cache table:**
   ```sql
   CREATE TABLE verification_cache (
     id INTEGER PRIMARY KEY,
     canonical_key TEXT NOT NULL,  -- subject + predicate + normalized object
     pattern TEXT NOT NULL,
     verdict TEXT NOT NULL,         -- verified / contradicted / inconclusive
     evidence TEXT,                 -- JSON: snippets + judge justification
     stability_class TEXT NOT NULL,
     cached_at TEXT NOT NULL,
     expires_at TEXT,               -- NULL = immutable
     hit_count INTEGER DEFAULT 0,
     created_at TEXT NOT NULL
   );
   CREATE UNIQUE INDEX idx_cache_key ON verification_cache(canonical_key);
   CREATE INDEX idx_cache_expires ON verification_cache(expires_at);
   ```

4. **Lookup flow** (in retrieval_verifier or above it):
     - Compute canonical_key from claim slots (subject + predicate +
       normalized object)
     - Run scoping classifier; if not cache-eligible, skip cache
     - SELECT from cache where key = ? and (expires_at IS NULL OR
       expires_at > now)
     - HIT: increment hit_count, return cached verdict + evidence
     - MISS or expired: run retrieval normally, run stability
       classifier, INSERT into cache with computed expires_at

5. **Entity canonicalization** (the hardest sub-problem):
     - Start with a simple alias table maintained over time
     - Fall back to LLM-assisted resolution as last resort
     - Vector similarity is a fallback, not the primary lookup

### Recommended sequencing

1. Schema + cache table (no behavior change yet; just storage).
2. Scoping classifier in OBSERVATION MODE first — log decisions to
   pipeline_events for a few sessions, see what it produces on real
   claims, calibrate its prompt before wiring it to actual cache
   writes. **This is non-negotiable** — the spec is explicit:
   "Build the scoping classifier first and run it in observation
   mode for a session or two before wiring it to actual cache writes."
3. Stability classifier in observation mode (similarly).
4. Wire scoping → cache lookup (read-only first, no writes — measure
   hit rate against retrieval results).
5. Wire stability → cache writes.
6. Add a cache inspector to the trace UI.

### What doesn't go in the cache

Per the "be aggressive about what doesn't go in" guidance:
  - Anything the scoping classifier marks user_specific or session_specific
  - Claims with confidence < some threshold from the verifier
  - Claims where retrieval was inconclusive (we don't have a verdict
    worth caching)
  - Anything the stability classifier marks volatile

### Architectural framing in ARCHITECTURE.md

When this ships, add a section: "Verification cache (v0.6 / Tier 2).
The cache is a performance optimization for retrieval. Cached entries
can be wrong, can go stale, and are subject to eviction. Every cached
verdict is provisional."

### Open questions for the operator

- Should the cache be per-user_id or shared across users? Sharing
  amplifies the value (one user's "Tokyo is in Japan" lookup helps
  another) but raises consistency questions if entity-canonicalization
  is wrong. Lean toward shared with a "cache visible to admins" view.
- What's the retention policy for expired entries? Vacuum on a
  schedule, or just leave them with expires_at in the past?
- Should the cache hit_count drive eviction (LRU) or pure TTL?
  Recommend pure TTL for v0.6, add LRU later if cache size becomes a
  problem.

## 2026-04-28 — CRITICAL: extractor was substituting "correct" values

After analyzing the hallucination-corpus diagnostic dumps in detail,
the "3 catches" celebrated below are NOT genuine hallucination
catches. They're the EXTRACTOR (Opus 4.7) silently substituting its
own world knowledge for what the chat model literally said.

Concrete evidence — Saturn moons (corpus turn 11):
  Model said:    "As of 2024, Saturn has 274 confirmed moons."
  Extracted as:  value=146 (operator-expected count from older data)
  source_text:   "As of 2024, Saturn has 146 confirmed moons."
                 ← LITERALLY REWRITTEN. The string "146" doesn't
                   appear in the model's response at all.

Same pattern in Yellowknife (turn 8):
  Model said:    "the population was 21,455"
  Extracted as:  value=20340 (close to actual census figure)
  source_text:   "the population was 20,340" (rewritten)

Same likely happened on Marie Curie's married_to claim — needs
re-investigation.

**The extractor is doing the verifier's job, badly.** It looks at
what the model said, decides "actually that's wrong, the right answer
is X", then writes X as the extracted value AND rewrites source_text
to match. Then the verifier compares X against retrieval, retrieval
returns the model's actual claim from the source web, and AEDOS
flags X as contradicted — masking that the extractor was the one
who introduced X in the first place.

This is a SEVERE violation of the firewall principle. Fixed in commit
TBD with an aggressive 'CRITICAL: extract VERBATIM' rule in the
extractor system prompt + 2 worked examples (Saturn 274, Yellowknife
22085) showing the rule. Real-API regression test added (skipped by
default behind RUN_API_TESTS=1) that asserts a literal '146' in the
input produces value=146 in the extracted slots, not the model's
guess at the truth.

Implications:
  - The "GLM produced 3 hallucinations caught" finding is wrong. The
    actual GLM hallucination rate on the 28-prompt corpus needs
    re-measurement after the extractor fix lands and the corpus is
    re-run.
  - The verification pipeline IS catching real things — but the
    contradictions in those 3 cases were AEDOS's own extractor bug
    showing up as a verifier success. False-positive corrections.
  - Past Phase-2 dogfood numbers (zero hallucinations from GLM)
    might also have been wrong in the other direction — maybe GLM
    DID hallucinate, but the extractor substituted truth, and the
    verifier rubber-stamped it.

**Re-run the full dogfood + corpus once Modal is healthy** to get
clean numbers. This is THE most important Phase-2 calibration finding
of the whole autonomous run so far.

---

## 2026-04-28 — Hallucination corpus run (27 of 28 turns landed signal)

`scripts/dogfood_hallucination_corpus.py` against GLM-5.1-FP8.
Adversarial prompt set designed to elicit hallucinations the friendly
Phase-2 dogfood didn't surface.

### Verdict distribution (across 27 turns with signal)

| Verdict | Count |
|---------|-------|
| verified | 27 |
| retrieval_inconclusive | 6 |
| contradicted | 3 |
| retrieval_failed | 1 |

5 pipeline errors (Modal cold-start timeouts + 1 content=null on the
floccinaucinihilipilification spell-backwards turn — too many
reasoning tokens for max_tokens=1024 cap).

### Real catches (the verifier did its main job)

3 contradicted verdicts where AEDOS caught GLM saying something wrong
and the corrector applied a REPLACE intervention. Specifically:

  - **yellowknife_population** (turn 8) — GLM gave a confident wrong
    population, verifier caught it. Real win on the "lesser-known
    entity numerical claim" hypothesis.
  - **saturn_moons** (turn 11) — pre-2023 trained models are likely
    to say ~83 moons; the answer changed to 146 in May 2023. AEDOS
    caught the stale figure.
  - **marie_curie composite** (turn 12) — 5 facts extracted, 4 of
    them verified, 1 contradicted. Multi-claim extraction working,
    one of the slot values was wrong, AEDOS surgically corrected
    the wrong one.

### Inconclusive verdicts (verifier hedged)

6 cases where the verifier saw weak retrieval signal and the
corrector hedged (added "I think" / "may want to verify"). Most look
like good calls — the prompts were genuinely contested or specific
enough that DDG snippets didn't give a clean signal:

  - everest_height_m, marie_curie_lifespan, user_self_ref_basic
    (twice), greenland_self_rule, etc.

### One DDG dropout

  - **denver_elevation** (turn 9) — GLM said 5,280 ft (correct),
    but ALL three DDG queries returned 0 results. Verifier correctly
    classified as `retrieval_failed`. Per v0.3 design, the corrector
    did NOT hedge — verifier failure is not evidence of uncertainty.
    The user got the correct answer.

  This is a known DDG flakiness issue (anti-bot detection,
  intermittent empties). The fix space:
    1. Better User-Agent rotation in `search_duckduckgo`
    2. Retry on empty result with brief backoff
    3. Set TAVILY_API_KEY for stable retrieval (default_search
       prefers Tavily when configured)

  Adding to NEXT_STEPS as a robustness improvement.

### Calibration win that LANDED

The **lifespan/duration calibration** committed earlier this session
(extractor + router worked example for "born X, died Y, lived Z
years") wasn't yet exercised — the corpus didn't include a Marie-
Curie-style multi-claim "lived" prompt. Worth adding one for the next
corpus run to validate the calibration.

### What didn't surface

  - GLM didn't confabulate the fake_invention or fake_treaty (turns
    22, 23). Either it has good "I don't know that" instincts on the
    specific phrases I picked, or the prompts didn't push hard enough.
  - The new dynamic-fact case (saturn_moons) was a clear catch but
    we'd want more like it — political offices, currently-held world
    records, etc. — to test the "stale training data" axis.

### THE BIG MISS: turn 26 user_self_ref_partial_wrong

The most interesting failure of the whole run. Setup:

  - Turn 24 (session 24, in same conversation): user said "I was born
    in the city of Williamstown, Massachusetts." → stored as
    user-asserted fact (spatial_temporal.was_born_in,
    location=Williamstown MA).
  - Turn 26 (same session): user prompts "I think I told you I was
    born in Williamsburg, Virginia. Is that right?"

What AEDOS did:
  - Extractor pulled "I was born in Williamsburg, Virginia" as a NEW
    user assertion (polarity=1, location=Williamsburg VA).
  - Router stored the new assertion as user_stored. **No contradiction
    detected** with the prior Williamstown MA fact. Both are now in
    the store.
  - Model responded: "Yes, you did mention that you were born in
    Williamsburg, Virginia. I apologize for only mentioning
    Williamstown, Massachusetts earlier — you've actually told me
    about both locations as your birthplace." (CONFABULATION)
  - AEDOS verified BOTH model claims because both birthplaces are now
    in the store.

Multiple failures cascaded:

  1. **Extractor shouldn't extract "I think I told you X. Is that
     right?" as the user asserting X.** This is an interrogative /
     meta-claim, not a first-person assertion. Adversarial trap class:
     prompts that LOOK like assertions but are actually questions
     testing the model. The extractor needs to recognize verb
     framings like "I think I said", "did I say", "you told me" as
     non-assertion forms.

  2. **The router's contradiction model is per-key-slot exact match.**
     spatial_temporal's key slots are entity + location + relation_kind.
     Different location → different key → not a contradiction in the
     current model. But semantically a person has ONE birthplace.
     For a class of "uniquely valued per entity" predicates
     (birthplace, biological mother, native language, blood type),
     different value on the value slot WITH same entity SHOULD be
     a contradiction.

  3. **Cross-turn user contradiction not surfaced as an event.** Even
     if the router can't catch this automatically, "user said X in
     turn 24, then said NOT-X in turn 26" is a strong signal worth
     flagging. There's no event for this.

Concrete fixes (NEXT_STEPS items):

  - **Extractor calibration:** add worked examples for "I think I
    said X. Is that right?" and similar interrogative-meta forms →
    `facts=[]`. Also "did I tell you X" → `facts=[]`. The user is
    asking, not asserting.

  - **Pattern metadata for unique-value slots:** mark certain slots
    in patterns.yaml as `unique_per_entity: true`. The router would
    treat a same-entity, different-value claim on these slots as a
    contradiction even at same polarity. This is an architectural
    decision — defer to the operator.

  - **user_contradicted_self event:** when the router detects a new
    user fact that conflicts with a prior user fact (via the
    unique-value rule above), emit a prominent event so the operator
    can see "user is making conflicting claims about themselves" —
    that's interesting signal regardless of which one is true.

  This is a meaningful research finding: the contradiction-detection
  model in v0.3+ assumes polarity as the only contradiction axis.
  Adversarial multi-turn prompts can exploit the gap. Worth a paper-
  worthy section.

## 2026-04-27 — Phase-2 dogfood complete (12/17 turns landed signal)

After Modal recovered from its multi-hour 503 outage, the resumed
dogfood (`scripts/dogfood_glm.py --start 6`) ran 12 of 12 attempted
turns to completion. Two timed out on Modal cold-start (turns 6, 16).
Of the 10 that landed signal:

| # | Category | Verdict | Notable |
|---|----------|---------|---------|
| 7 | python_canonical | facts=0 | **Reproduced** — extractor refuses to extract canonical-list responses |
| 8 | retrieval:art | verified | Salvador Dalí / Persistence of Memory |
| 9 | retrieval:geo | verified | Suriname / Dutch |
| 10 | retrieval:tech | 3× verified | Multi-claim — one verdict per founder |
| 11 | retrieval:history | retrieval_failed | **Real bug** — judge said "SUPPORT", parser wanted "SUPPORTED" |
| 12 | mixed | 2 verified + 1 inconclusive + 1 hedge | **Real router calibration finding** below |
| 13 | user_auth:set | verified | Coffee preference stored |
| 14 | user_auth:recall | verified, 11.7s | Phase 5 user_id store working end-to-end |
| 15 | user_auth:recall | verified, 8.4s | Polarity-aware ("no, no sugar") matched against `polarity=0` |
| 17 | retrieval:obscure | verified | Belgium first stamps = 1849 |

### Real bugs / gaps fixed this session as a result

1. **Judge parser rejected "SUPPORT" / "CONTRADICT" / "INCONCLUSIVE"**
   despite the judge LLM clearly intending those. Tokyo→Edo turn:
   judge wrote "SUPPORT\nJustification: Multiple sources confirm Edo
   is the former name of Tokyo..." — perfect signal, marked as
   `judge_parse_error` and counted as `retrieval_failed`. Fixed by
   accepting common abbreviations as aliases for canonical labels.
   Test added; would have caught this earlier with a single fixture.

### Real bugs / gaps not yet fixed

2. **Mixed claims with python-on-given-inputs DON'T route to python.**
   The CLAUDE.md spec gives the exact case: "Marie Curie was born in
   1867 and died in 1934, so she lived 67 years" should route the
   arithmetic to python. In dogfood, GLM said exactly that, and the
   `lifespan_years=66` claim went to **retrieval** with reason "the
   claim doesn't supply [the dates]". The router is technically
   correct given what the EXTRACTOR sent it: the extractor stripped
   the dates from the lifespan claim's slots. The fix is in
   `extractor.py` worked examples — when a lifespan/duration/diff
   claim appears alongside its inputs in the same response, the
   extractor should embed the inputs as slots so the router can see
   they're self-contained. Adding this fix needs a real-API run to
   validate. NEXT_STEPS item.

3. **Days-of-week / canonical-list extraction:** still confirmed.
   Same finding as before — the extractor returns `valid_facts: []`
   for "the seven days of the week are: Mon, Tue, ..., Sun". Either
   add a worked example for list responses, or accept that canonical
   reference enumerations aren't extractable claims. Architectural
   decision, defer to operator.

### What worked on the first try

- All retrieval-territory questions where the answer was a single
  prominent fact (Dali, Suriname, Cloudflare, Belgium stamps) —
  cleanly verified.
- All user_auth turns. **Phase 5's user_id scoping is end-to-end
  validated:** stored a preference in turn 13, recalled correctly in
  turn 14 (positive form) and turn 15 (negation/polarity form).
- Multi-claim extraction (Cloudflare → 3 founders, all verified).

### What didn't surface

- **No false-positive hallucinations from GLM in this run.** Every
  claim GLM made that we could verify was correct. The retrieval
  verifier produced no `contradicted` verdicts. Either the prompts
  weren't hard enough, GLM is genuinely strong on these specific
  facts, or both. Future Phase 7 work should curate prompts where
  GLM's training data is stale or thin.
- The fake-book confabulation prompt (turn 16) timed out before we
  could see how GLM handled a non-existent thing. Re-run with
  smaller max_tokens may help — the model probably reasoned for
  hundreds of tokens trying to recall a book that doesn't exist.

### Cold-start reality

Two of two Modal cold-starts (turns 6 and 16) exceeded the 300s
timeout. This suggests either:
  - Increase timeout to 600s (ugly)
  - Lower `max_tokens` in the chat call (currently 4096; chat draft
    is short, model spends tokens reasoning) — see NEXT_STEPS
  - Pre-warm the endpoint with a tiny request before the first real
    turn (script-level fix in `dogfood_glm.py`)

The right answer is probably "lower max_tokens to ~1024 for chat" —
chat responses are inherently short, and reducing the cap forces the
reasoning chain to wrap up faster too.
