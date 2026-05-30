# Phase F4 — End-to-End Validation

*The F4 artifact that proves Phase 10.5 can run honestly. Single case
manually traced through the full pipeline against real services. Per
F1 §3 commit #12 plan; tagged `v0.15.0-rc.8` at landing.*

---

## Environment

- Date: 2026-05-20
- Phase F build: post-`d37bf89` (F3 follow-up captured)
- Tag at landing: `v0.15.0-rc.8`
- Pipeline configuration:
  - **Substrate / verifier / extractor:** `gpt-4.1-mini` (per
    operator-confirmed Path C — defer open-weight migration until
    Phase 10.5 measurement data informs it)
  - **Chat slot:** `claude-haiku-4-5` (per F-009 alignment)
  - **KB:** live Wikidata (`RUN_LIVE_KB=1`)
  - **Substrate:** fresh DB with the 61-row seed pack loaded plus
    the Phase 10.5 Step 3 Asa Tier U rows seeded
- `Config()` constructed with defaults; threaded via `build_pipeline(db, config=Config())`.

## Case selection

**`der_disambiguation_002` — "Paris is the capital of France"**

Selection rationale (per operator's F4 case-selection refinement):

- Exercises live KB resolve (Paris + France — well-known robust
  entities, less likely to hit D33's "canonical entity unreachable
  in top-10" effect).
- Exercises live KB lookup with the D19 inverse-direction path
  (`capital_of`'s `slot_to_qualifier` swaps subject↔object: the KB
  statement is keyed on France, not Paris).
- Non-trivial expected verdict (`verified` — discriminates "worked
  correctly" from "broke into the default abstain").
- Single-claim case so trace inspection is tractable.

Subsumption is **not** exercised — no derivation case in the corpus
exercises the live `_live_subsumption` path through the walker (the
walker uses substrate-row subsumption via `find_neighbors`, not the
KB protocol's `subsumption()`; related to existing v0.16 D5).
F2's `test_pipeline_reaches_wikidata.py` verifies the `_live_subsumption`
wiring separately; F4 doesn't duplicate that verification.

## Captured trace

### Extractor output

```
Claims extracted: 1
  [0] subject='Paris' predicate='capital_of' object='France' polarity=1
      triage=verify
      source_text='Paris is the capital of France'
```

Extraction took 2.7s. The extractor:
- Identified the relational claim correctly.
- Normalized the predicate to `capital_of` (not `is_capital_of`,
  `capital`, or some variant — matches the seed pack's canonical name).
- Set polarity=1 (asserted, not negated).
- `source_text` matches the input verbatim (per architecture §4.1's
  source-text discipline).
- Triage decision: VERIFY (the claim passes verifiability triage).
- No spurious additional claims.

### Predicate metadata (oracle consultation)

```
predicate=capital_of:
  routing_hint=kb_resolvable, object_type=entity, single_valued=True
  kb_namespace=wikidata, kb_property=P36
  slot_to_qualifier={'subject': 'statement_value', 'object': 'statement_subject'}
```

The seeded row drives the routing. `slot_to_qualifier` is the inverse
mapping: the Aedos subject (Paris) maps to the KB statement's *value*
(P36 returns the capital as the value); the Aedos object (France) maps
to the KB statement's *subject* (P36 is keyed on the country). This is
the D19 inverse-direction case fixup-3 resolved.

No `row_created` event fired — the predicate is in the 61-row seed
pack, the consult was a cache hit, no LLM call required.

### KB live calls

```
kb_live_resolve: 2 events
  - Paris  → candidate_count=10, duration_ms=500, retry_count=0
  - France → candidate_count=10, duration_ms=500, retry_count=0
kb_live_lookup: 1 event
  - Q142:P36 → statement_count=10, duration_ms=469, retry_count=0
```

The KBVerifier's flow:
1. Consult predicate_translation → see inverse mapping
2. `_lookup_targets` swaps: lookup-ref=France (claim's object),
   expected-ref=Paris (claim's subject), `lookup_inverted=True`
3. Resolve France → resolver returns 10 candidates; top candidate is
   Q142 (verified by the lookup target below)
4. Resolve Paris → resolver returns 10 candidates
5. SPARQL lookup on `wd:Q142 p:P36 ?statement` → 10 statements
   returned (France has multiple historical capitals plus the current
   one; rank filtering excluded deprecated rows server-side)
6. Compare each statement value against the resolved Paris Q-id;
   first scope-compatible match → VERIFIED

### Walker traversal

```
verdict=verified
abstention_reason=None
llm_calls=0
depth=0
source_breakdown={'tier_u': 0, 'kb': 1, 'python': 0}
polarity_trace=[1]

Trace edges:
  [premise_lookup] source=kb, verdict=verified, lookup_inverted=True
```

Walker walked at depth 0 — direct KB verification succeeded on the
first attempt. The §6.5 lookup order engaged:

1. **Tier U lookup** — empty (no Tier U row asserts the Paris-France
   capital relation; the seeded Asa rows aren't relevant).
2. **Belief revision (polarity / object conflict)** — no prior Tier U
   row to revise against.
3. **KB verification** — VERIFIED (the load-bearing edge).
4. **Python verifier** — **did not fire** (predicate is kb_resolvable,
   not python; F-042 routing gate held).
5. **Subsumption / derivation expansion** — not needed (direct KB
   verdict reached).

`llm_calls=0` — no inline LLM generation happened. The predicate
was seeded; the resolver's `EntityResolver.select` may have invoked
the LLM for disambiguation if scores were close, but in this run it
didn't (the top candidates' scores must have been separated by ≥
`_AMBIGUITY_GAP=0.15`).

### Aggregator output

```
per_claim_verdicts: {<claim_id>: 'verified'}
aggregate_metadata:
  claim_count=1
  verified=1, contradicted=0, abstained=0
  total_llm_calls=0
  max_depth_reached=0
  source_breakdown={'tier_u': 0, 'kb': 1, 'python': 0}
  budget_exceedances=0
audit_log_entries: 1
consistency_warnings: []
```

Verdict propagated faithfully from walker → aggregator. No
information loss. One verdict_recorded audit event captured. No
consistency warnings.

### Audit log (full)

```
kb_live_resolve: 2 events
kb_live_lookup:  1 event
verdict_recorded: 1 event
(every other event type: 0)
```

The minimum architecturally-required event set fired:
- KB API calls audited (kb_live_*).
- Verdict outcome recorded (verdict_recorded).
- No `row_created` (predicate seeded), no consistency violations,
  no circuit breakers, no parallel Tier U assertions.

## Verification against operator's 6-point checklist

| # | Criterion | Result |
|---|---|---|
| 1 | Extractor produced the structured claim faithfully | ✓ |
| 2 | Oracle consultations have corresponding audit log entries | ✓ (seeded predicate → no row_created; verdict_recorded fired) |
| 3 | KB calls show appropriate Wikidata API calls | ✓ (resolve Paris + France; lookup Q142:P36 — correct D19 inverse direction) |
| 4 | Walker traversal follows §6.5's pattern | ✓ (Tier U → KB; Python did not fire on kb_resolvable route per F-042) |
| 5 | Aggregator constructed verdict without losing information | ✓ |
| 6 | Audit log captured every architecturally-required event | ✓ |

## Subtle findings surfaced

Per Phase F's discipline pattern ("the integration surface has subtler
issues than isolated tests caught"), F4 inspection surfaced two findings:

### Finding 1 — D13 confirmed empirically (no new delta)

The `verdict_recorded` event records `"source_rows": []` for this
KB-grounded verdict. The KB premise-lookup trace edge carries no
retractable identifier (no `tier_u_row_id`,
`predicate_translation_row_id`, `subsumption_row_id`, or
`entity_resolution_cache_row_id`). Architecture §7.3's
retraction-propagation cannot reach this verdict — if the underlying
KB statement is later contradicted, the verdict won't be retracted.

This is D13 (already a v0.16 candidate, captured by the re-audit and
reaffirmed in Phase F1). F4 confirms it manifests in the most common
verdict type (KB-grounded). The fix is v0.16 work: record
`entity_resolution_cache` row ids on the KB premise_lookup edge, add
that table to `_TRACE_ROW_ID_KEYS` in the aggregator, decide how a
cached `lookup_statements` result is identified for retraction.

No new delta needed.

### Finding 2 — F-043: Resolver selection not in audit log (new v0.16 candidate)

The `kb_live_resolve` event records:
```
{"candidate_count": 10, "duration_ms": 500, "retry_count": 0, "error": null}
```

It does NOT record **which candidate was selected** by
`EntityResolver.select()`. For F4's case, the corpus notes say "Paris
must resolve to Q90 (city) not a person" — but the audit log can't
confirm this resolved correctly without re-running the case. The
selection happens after the live call returns; that path has no
audit event.

Captured below as D42.

## D42 capture (new v0.16 candidate)

Added inline here so the rc.8 commit closes Phase F's open
v0.16-planning state.

> **D42 — Entity resolver selection has no audit event.**
> `WikidataAdapter._live_resolve` emits `kb_live_resolve` with the
> candidate count, but the subsequent selection by
> `EntityResolver.select` (the LLM-disambiguation path, or the
> rank-1 default) is silent. Post-hoc trace analysis cannot confirm
> "Paris resolved to Q90 (city) not Q1138 (Trojan prince)" — the
> very disambiguation question that's the architecture §5.1's
> reason for ranked candidates. v0.16 should emit a
> `resolver_selection` audit event capturing the resolved Q-id, the
> selection mode (rank-1 / LLM-mediated), and the
> ambiguity-gap delta. Small (~10 LOC change to `resolver.py`).
> Surfaced by F4 trace inspection.

## Performance

| Step | Elapsed |
|---|---|
| Extractor (1 LLM call) | 2.7s |
| Walker (KB calls only, 0 LLM) | 1.5s |
| **Total** | **~4.2s** |

Well within budget. The HTTP cache amortization isn't relevant for a
single-case run.

## F4 outcome

All six checklist criteria met. The verdict matches the corpus's
expected outcome (`verified`). The pipeline executes end-to-end
against real services with no surprises. Two subtle findings
(D13 reconfirmed; D42 new candidate) are recorded as v0.16 work; both
are observability gaps, not soundness violations.

**Phase 10.5 can run honestly from `v0.15.0-rc.8`.** The deployment-
readiness audit gate is closed.

## Fallback note

Per Phase B's report, the calibration-anomaly fallback remains
`v0.15.0-rc.2`. Phase F's changes are additive (implementing
previously-stub methods plus wiring fixes plus the F-042 routing
gate fix); none affect the D16/D6 calibration paths the fallback was
chosen against. The new default starting point for Phase 10.5 is
`v0.15.0-rc.8`; `rc.2` remains available if calibration surfaces a
D16/D6 anomaly.

---

*End of Phase F4 end-to-end validation.*
