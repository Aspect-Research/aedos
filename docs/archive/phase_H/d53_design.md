# Phase H D53 — design

**Status:** design draft, pending operator review.

D47 introduced a Wikipedia normalizer whose Stage 2 LLM selection
operated over candidates scraped from Wikipedia disambiguation page
link lists. Cluster 1 (post-D51 validation) surfaced this as the
wrong source of truth: the candidate lists were truncated
alphabetically, biased toward less prominent entities, and missing
canonical targets (Amazon River, the U.S. presidency article, etc.).
Cluster 1 papered over the symptoms (raised the candidate cap,
added an implicit-disambig probe, cached more aggressively).

D53 replaces the candidate source. Wikidata's `wbsearchentities` is
the architecturally correct API for programmatic entity disambiguation
— it returns ranked entity candidates by Q-id with labels,
descriptions, and aliases, and is designed for this exact use case.

## Empirical investigation result

`scripts/d53_investigation.py` and `scripts/d53_investigation_hybrid.py`
queried wbsearchentities directly for the six Cluster 1 problem cases:

| Surface | Bare wbsearch rank of canonical | Canonical-form wbsearch rank |
|---|---|---|
| Obama (→ Q76 Barack Obama) | **NOT FOUND in top 20** | **1** |
| Apple (→ Q312 Apple Inc.) | 1 | 1 |
| Amazon (→ Q3884 ambiguous) | 1 | 1 |
| Einstein (→ Q937 Albert Einstein) | 2 | **1** |
| President (→ Q11696 President of the United States) | 4 | **1** |
| Williams College (→ Q49166) | 1 | 1 |

**Finding B confirmed.** Bare ambiguous surface forms whose primary
Wikidata label points to the canonical entity via *alias* (Q76 has
label "Barack Obama", alias "Obama") get buried under entities whose
primary label *is* the surface form (towns named "Obama", etc.).
This is the same architectural pattern Wikipedia disambig pages had;
the underlying issue is search-by-string-match-on-label-only.

**Hybrid resolves it.** When Wikipedia's redirect API canonicalizes
the surface form first (Obama → Barack Obama), wbsearchentities on
the canonical form ranks the target Q-id at position 1 cleanly. Every
problem case is solved by the hybrid.

Raw data: `docs/phase_H/d53_investigation.json` and
`d53_hybrid_investigation.json`.

## Architecture

Three stages. Renaming the existing "Stage 1" / "Stage 2" in the
WikipediaNormalizer to A / B / C to disambiguate from D47's previous
two-stage usage:

```
surface_form + context
       │
       ▼
┌─────────────────────────────────────────────────────────────────┐
│ Stage A — Wikipedia canonicalization                            │
│ (existing D47 Stage 1 logic, role re-purposed)                  │
│ MediaWiki action=query&redirects=1 on surface_form              │
│   outcome ∈ {clean_redirect, canonical_no_redirect,             │
│              disambiguation_page, not_found, api_error}          │
└─────────────────────────────────────────────────────────────────┘
       │
       │ stage_b_query :=
       │   redirect_target  if clean_redirect
       │   canonical_title  if canonical_no_redirect
       │   surface_form     if disambiguation_page / not_found
       │                    (Wikipedia has no usable canonical;
       │                     let Stage B + C handle ambiguity)
       │   ABORT            if api_error (preserve surface_form,
       │                     short-circuit downstream)
       ▼
┌─────────────────────────────────────────────────────────────────┐
│ Stage B — Wikidata wbsearchentities                             │
│ action=wbsearchentities&search=stage_b_query&language=en        │
│   &type=item&limit=20                                            │
│ Returns ranked Q-id candidates with label, description,         │
│ aliases.                                                         │
└─────────────────────────────────────────────────────────────────┘
       │
       │ candidates: list[StageBCandidate]
       ▼
┌─────────────────────────────────────────────────────────────────┐
│ Stage C — Type filter + heuristic shortcut + LLM selection      │
│   1. D33 type filter applies (existing P31 batched lookup)      │
│   2. If single type-filtered candidate → shortcut, no LLM       │
│   3. Otherwise → LLM picks Q-id from filtered set, or abstains  │
└─────────────────────────────────────────────────────────────────┘
       │
       ▼
NormalizationResult(normalized_form = selected Q-id label,
                    selected_qid    = Q-id,
                    stage_a_outcome, stage_b_query,
                    stage_c_candidates, stage_c_selection, …)
```

### Stage A — Wikipedia canonicalization (existing, re-purposed)

The D47 Stage 1 code in `wikipedia_normalizer.py` stays. Its outcome
classification (`canonical_no_redirect`, `clean_redirect`,
`disambiguation_page`, `not_found`, `api_error`) is preserved as-is.

Behaviorally, Stage A's job is exactly the redirect resolution Wikipedia
is designed to do: take a short ambiguous reference and resolve aliases
to canonical article titles. We use what Wikipedia knows (Obama →
Barack Obama as a redirect, Einstein → Albert Einstein as a redirect),
and let Stage B + C handle the cases Wikipedia can't (President as
disambig, Amazon as ambiguous).

The implicit-disambig probe added in Cluster 1 step 2 is removed.
Its reason for existing — Wikipedia primary-article routing missing
the contextually-correct entity (Apple → fruit not company) — is
solved natively by wbsearchentities, which ranks Apple Inc. at rank 1
when queried for "Apple".

### Stage B — wbsearchentities client (new)

New module-level client (likely on `WikidataAdapter` since the rate
limiter, HTTP cache, and audit infrastructure live there):

```python
@dataclass
class StageBCandidate:
    qid: str
    label: str
    description: Optional[str]
    aliases: list[str]
    match_type: str       # "label" | "alias"
    match_text: str
    rank: int             # 1-based position in wbsearchentities response
```

Method:

```python
class WikidataAdapter:
    def wbsearchentities(
        self, query: str, limit: int = 20
    ) -> list[StageBCandidate]:
        """Query wbsearchentities for a candidate list."""
```

Parameters: `action=wbsearchentities`, `search=query`, `language=en`,
`type=item`, `format=json`, `limit={config.wikidata_wbsearch_limit}`
(default 20).

Rate limiting: reuses the existing `wikidata_search_rate_per_second`
limiter (50/s default). HTTP cache: TTL = entity TTL (1h default).
Failure modes: on network/timeout, retry once then return `[]` —
fail-open, Stage C sees no candidates and abstains visibly.

Audit event `wbsearchentities_query` records: query, n_candidates,
top candidate Q-ids.

### Stage C — Type filter + heuristic shortcut + LLM selection

After Stage B returns candidates, Stage C runs:

1. **D33 type filter.** Existing batched `wbgetentities` P31 lookup
   applied to Stage B's candidates. Candidates whose P31 doesn't
   intersect `local_context.expected_entity_types` are removed.
   D33's fail-open discipline: if the filter eliminates all
   candidates, pass the unfiltered list to the next step (with
   a flag in the audit event).

2. **Heuristic shortcut.** Conservative starting point per operator
   guidance: **single-candidate-only shortcut**. If exactly one
   candidate survives the type filter, use it directly without an
   LLM call.

   No multi-candidate prominence shortcut at this stage. Wikidata's
   wbsearchentities already returns ranked results; if the LLM
   would otherwise pick rank 1, the LLM call is the cost we accept
   for context-sensitive selection. v0.16 can add a prominence-
   gap shortcut if validation data shows the LLM call is over-used.

3. **LLM selection.** Identical prompt structure to the current D47
   Stage 2 — Haiku receives `(surface_form, claim_subject,
   claim_predicate, claim_object, source_text, candidates)`. The
   `candidates` format changes: instead of a list of bare strings,
   each candidate is presented as a structured line:

   ```
     - Q76  | Barack Obama
            | 44th president of the United States (1961–)
            | aliases: Obama, Barry, Barack Hussein Obama
   ```

   Haiku selects by **Q-id** (not by label) or emits `ABSTAIN`.
   Defence-in-depth: the model must select a Q-id present in the
   candidate set. Hallucinated Q-ids treated as abstention (same
   discipline as current D47 Stage 2).

   The system prompt's abstention discipline is preserved verbatim.
   Context-sensitive selection is what justifies the LLM call.

### Q-id skip (preserved from Cluster 1)

Surface forms matching `^Q\d+$` continue to short-circuit at entry.
Still needed: the D5 KB neighbor enumeration substitutes Q-ids
back into claims, and those Q-ids re-enter the resolver. They
shouldn't be sent to Wikipedia (Stage A) or wbsearchentities
(Stage B) — they're already canonical KB identifiers.

### Caching (generalized from Cluster 1)

The Cluster 1 caching applies as-is with three adjustments:

**Resolver cache (`entity_resolution_cache`):** the key changes
to a **composite of `(surface_form, stage_a_outcome, stage_b_query,
context_signature)`** so that different canonicalization paths cache
independently. Concretely: cases where Stage A produced
`clean_redirect` (Obama → Barack Obama) cache under a different key
than cases where Stage A produced `disambiguation_page` for the same
surface form (a rare but possible cross-context divergence).

Old key was `(reference=normalized_wikipedia_title,
local_context_signature)`. New key encodes Stage A's contribution
into the `local_context_signature` hash so the existing schema's
`UNIQUE(reference, local_context_signature)` constraint generalizes
cleanly. The `reference` column stores the surface form going forward;
the signature input includes `stage_a_outcome` and `stage_b_query`.

No schema migration. Old rows (Cluster 1-era, keyed by
Wikipedia title) become un-hittable cache rows that get re-resolved
on first miss. Acceptable cost.

**Normalizer memo (in-instance dict):** the memo key tuple stays
unchanged in shape (surface form + structured claim + source_text +
slot). The memo now stores `NormalizationResult` with the new fields
(`stage_b_query`, `stage_c_candidates`, `selected_qid`, etc.). Memo
hits work the same way.

**Resolver negative cache:** preserved. Stage C abstention or
fail-open empty produces a negative-cache row (empty
`resolved_kb_identifier` sentinel from Cluster 1).

### Configuration changes

```python
# REMOVED
wikipedia_stage_2_max_candidates: int  # was 100 post-Cluster-1

# ADDED
wikidata_wbsearch_limit: int = 20  # wbsearchentities `limit` parameter
```

`wikipedia_normalizer_enabled` kept (kill switch for the whole flow).

### Audit log

Each `entity_normalization` event continues to fire. Field expansion:

```
event_data:
  surface_form, normalized_form, selected_qid                # outcome
  stage_a_outcome, stage_a_redirect_target                   # Stage A
  stage_b_query, stage_b_candidate_count                     # Stage B
  stage_b_top_candidates: [{qid, label, rank, match_type}]   # top 5
  stage_c_type_filter_applied, stage_c_filtered_count        # Stage C
  stage_c_shortcut_fired, stage_c_llm_invoked
  stage_c_selection, stage_c_reasoning, stage_c_abstained
  from_memo, duration_ms, error
```

Old field aliases (`stage_1_outcome`, `stage_2_invoked`,
`stage_2_candidates`, `stage_2_selection`, `stage_2_reasoning`)
removed. Downstream audit-log readers (Phase 10.5 analysis scripts,
diagnostic scripts) updated.

### What is preserved from Cluster 1

| Cluster 1 piece | D53 fate |
|---|---|
| Q-id skip (`^Q\d+$`) | **Preserved** — still needed for D5 KB enumeration substitution |
| Normalizer per-instance memo | **Preserved** — generalizes to new flow |
| Resolver negative cache | **Preserved** — generalizes to new flow |
| Implicit-disambig probe | **Removed** — wbsearchentities handles Apple-class natively |
| `wikipedia_stage_2_max_candidates` config | **Removed** — replaced by `wikidata_wbsearch_limit` |
| Wikipedia disambig page parsing (`_fetch_disambiguation_candidates`) | **Removed** — wbsearchentities replaces this entire codepath |

## Implementation plan

Four commits, each lands green (full test suite + live normalizer tests).

### Step 1 — wbsearchentities client + unit tests

`src/aedos/layer4_sources/kb_wikidata.py`:
- Add `wbsearchentities()` method on `WikidataAdapter` (or sibling
  module if the wbsearch endpoint feels distinct from SPARQL).
- Add `StageBCandidate` dataclass.
- Wire rate limiter + HTTP cache + audit event.
- New audit event type: `wbsearchentities_query`.

Tests `tests/unit/test_wikidata_wbsearchentities.py`:
- Mocked HTTP for the four wbsearchentities response shapes
  (one candidate, many candidates, empty, malformed).
- Audit event firing.
- Rate limiter integration.

Live test `tests/integration/live/test_wikidata_wbsearchentities_live.py`:
- Six investigation cases — each surface, query verified.
- Sanity check: canonical Q-id from hybrid (e.g. "Barack Obama"
  → Q76 rank 1).

Commit: `Phase H D53 step 1: wbsearchentities client`

### Step 2 — Three-stage integration

`src/aedos/layer1_extraction/wikipedia_normalizer.py`:
- Rename internal `_stage_1_*` and `_stage_2_*` methods to `_stage_a_*`,
  `_stage_b_*`, `_stage_c_*`. Public API unchanged
  (`normalize()`, `NormalizationResult`).
- Inject `wbsearchentities` callable (via the KB adapter) in
  `__init__`.
- Replace `_compose_result` flow:
  - Stage A produces an outcome and a `stage_b_query`.
  - Stage B queries wbsearchentities.
  - Stage C runs type filter + heuristic + LLM.
- Update `NormalizationResult` shape: rename fields, add
  `selected_qid`, `stage_b_query`, `stage_c_*` fields.
- Update `_log_audit_event` to write the new event_data shape.
- D33 type filter: pass `local_context.expected_entity_types` through
  to Stage C. Type filter calls `wbgetentities` for the P31s of
  candidate Q-ids and intersects.

Tests `tests/unit/test_wikipedia_normalizer.py`:
- Restructure around new stage names.
- Mocked Stage A + Stage B + Stage C flow.
- Heuristic shortcut single-candidate test.
- Type filter integration test.
- Stage C abstention with hallucinated Q-id (defence-in-depth).

Live test `tests/integration/live/test_wikipedia_normalizer_live.py`:
- Restructure for the six investigation cases. Each should resolve
  to the expected canonical Q-id via the hybrid.

Commit: `Phase H D53 step 2: replace Wikipedia normalizer with wbsearchentities flow`

### Step 3 — Cluster 1 obsolescence cleanup

- Remove `_probe_implicit_disambiguation`, `_fetch_disambiguation_candidates`.
- Remove `OUTCOME_DISAMBIGUATION_PAGE` handling in `_compose_result`
  that drove Stage 2 over disambig page links (Stage B handles it now
  via the surface form falling through to wbsearchentities).
- Remove `wikipedia_stage_2_max_candidates` from `Config` and its
  validation.
- Remove `TestImplicitDisambigProbe`, candidate-truncation tests, and
  any other Cluster-1-specific tests that exercised the disambig-page
  path.

Commit: `Phase H D53 step 3: remove obsoleted Wikipedia disambig page infrastructure`

### Step 4 — Validation

- Re-run `scripts/cluster_1_diagnostic.py` against the six target
  cases. Each should now resolve to its expected Q-id at Stage C.
- Re-run `scripts/d5_diagnostic.py` against the full derivation_corpus.
  Compare aggregate accuracy vs. post-Cluster-1 baseline (16/50).
- Per D49 discipline: 2-3 runs, report range.
- Document findings in `docs/phase_H/d53_validation.md`.

Commit: `Phase H D53 step 4: validation`

## Expected outcomes

### Cluster 1's target cases (the six failure cases)

Stage C should pick the canonical Q-id for each:

| Case | Stage A outcome | Stage B query | Expected Stage C selection |
|---|---|---|---|
| der_cross_001 (Obama holds_role President) | clean_redirect ("Obama" → "Barack Obama") | "Barack Obama" | Q76 |
| der_cross_008 (Obama was President AND is human) | clean_redirect | "Barack Obama" | Q76 |
| der_predicate_translation_001 (Obama was President 2009-2017) | clean_redirect | "Barack Obama" | Q76 |
| der_disambiguation_003 (Apple was founded in California) | canonical_no_redirect ("Apple" → "Apple") | "Apple" | Q312 (Apple Inc.) given context |
| der_disambiguation_004 (Einstein received_award Nobel Prize) | clean_redirect ("Einstein" → "Albert Einstein") | "Albert Einstein" | Q937 |
| der_disambiguation_006 (Amazon is the world's largest river) | disambig_page | "Amazon" | Q3783 (Amazon River) given context |

### Aggregate corpus accuracy

The six cases above are the entity-resolution failures. Once their
Stage C selection is correct, downstream verification still depends
on Cluster 2 (subsumption chains) and Cluster 3 (predicate translation).
**D53 alone does not guarantee verdict flips for the six cases** —
the entity normalization layer becomes correct, but downstream gates
may still close.

The aggregate-accuracy delta is therefore bounded. A realistic
prediction: 1-3 cases flip to verified (where Cluster 2 / Cluster 3
weren't on the critical path) plus the rest now reach those gates
cleanly.

D49 discipline applies. The diagnostic value of D53 — surfacing
where Cluster 2/3 actually bite — is high even when verdict-flip
count is low.

## Risks

**Wikidata API drift.** wbsearchentities ranking is not guaranteed
stable across Wikidata changes. The live tests will catch
catastrophic regressions; subtle ranking drift may not be caught.
Acceptable for v0.15 (research prototype); production deployment
would want a fallback path.

**LLM cost.** Stage C invokes Haiku for multi-candidate cases. Cost
is comparable to current Cluster 1 (1 LLM call per ambiguous
resolution, gated by memo). The wbsearchentities candidate list
(20 candidates × ~80 chars per candidate description ≈ 1.6KB) is
slightly larger than the Wikipedia disambig list it replaces but
well within Haiku's prompt budget.

**Type filter eliminating all candidates.** D33's fail-open
discipline already handles this. The Cluster 1 audit log captured
several cases where the type filter was too aggressive; this
behavior is preserved (no D33 change in D53).

**Cache key drift.** Existing entity_resolution_cache rows from
the Cluster 1 build use Wikipedia article titles as `reference`. D53
switches to surface form. Mixed-row state during deployment is
benign — old rows simply miss for the new lookup pattern and get
re-resolved. No migration required.

## Open questions

1. **Should the Q-id resolution result be passed downstream as a
   Q-id directly?** The current `NormalizationResult.normalized_form`
   is a string (a Wikipedia article title). With D53, the natural
   answer is a Q-id like "Q76". The resolver and KB verifier
   would need to handle this; downstream KB queries become more
   direct (no "label → Q-id" round-trip via wbsearchentities's
   second call).

   **Proposal:** `normalized_form` stays as the canonical *label*
   (so downstream string-based code still works), and a new field
   `selected_qid` carries the Q-id. The KB verifier prefers
   `selected_qid` when available; otherwise falls back to label
   resolution (existing behavior).

2. **Where does `wbsearchentities()` live?** Three options:
   - On `WikidataAdapter` alongside SPARQL methods.
   - In a new module `wikidata_search.py`.
   - As a free function in `kb_wikidata.py`.

   **Proposal:** new method on `WikidataAdapter`. Same rate limiter
   pool, same HTTP cache, same audit infrastructure. The endpoint
   differs from SPARQL (`api.php` vs `query.wikidata.org/sparql`)
   but both are Wikidata.

3. **Audit event shape changes — break v0.16 readers?** The
   Phase 10.5 analysis scripts that read `entity_normalization`
   events will need updates. Acceptable cost: that work is
   in-scope for Phase 10.5 prep anyway.

Surface these questions inline with operator review before
implementation begins. The base architecture is approved; the open
questions are scope-bounded design choices.
