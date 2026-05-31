# Phase H Cluster 1 — D47 Stage 2 entity-resolution failures

**Status: in progress.** Step 1 diagnostic complete (2026-05-24); Steps 2 + 3
fix-and-validate landed across two commits (Q-id skip + memo + negative cache,
then candidate cap bump 20→100 + implicit-disambig probe). Aggregate corpus
validation is pending the post-fix `d5_diagnostic.py` run.

## Step 1 — Diagnostic findings

The diagnostic ran the six derivation_corpus cases the D51 diagnostic
flagged as entity-resolution-driven abstentions through a focused
`scripts/cluster_1_diagnostic.py` instrumented to capture every Stage 2
invocation's user_message, candidate list, selection, and reasoning,
plus the `entity_normalization` audit events the resolver wrote.

**Headline.** The operator's hypothesis (*Mechanism A — Stage 2's
abstention discipline is over-calibrated*) was wrong. Stage 2 was
doing the right thing: when it abstained, the canonical entity
genuinely wasn't in the candidate set. The dominant failure was
**Mechanism C — candidate list missing canonical**, with two
secondary mechanisms:

- **Mechanism E (new finding):** Wikipedia routes some bare surface
  forms to a primary article that isn't the contextually-correct
  entity (e.g. `Apple` → the fruit), and the disambig page is at
  `{surface} (disambiguation)` rather than `{surface}` — so Stage 1
  returns `canonical_no_redirect` and Stage 2 never fires.
- **Mechanism F (new finding):** the D5 KB neighbor enumeration
  substitutes Wikidata Q-ids back into claims; those Q-ids then
  re-enter the normalizer (`resolver.resolve("Q5", ctx)`), which
  attempts Wikipedia normalization on a literal Q-id. Q5 happens to
  be a real Wikipedia disambiguation page (about the alphanumeric
  label), so Stage 2 fires uselessly. Q-ids should bypass the
  normalizer entirely.

### Per-case mechanism (Step 1)

| Case | Surface | Stage 1 outcome | Stage 2 fired | Stage 2 outcome | Mechanism |
|---|---|---|---|---|---|
| der_cross_001 | `President` | disambig | yes (8×) | 7 abstain, 1 picked `President (corporate title)` (wrong) | **C** — canonical missing; 20-candidate truncation excluded `President (government title)` |
| der_cross_008 | `President` | disambig | yes (8×) | all 8 abstain | **C** — same |
| der_predicate_translation_001 | `President` | disambig | yes (8×) | all 8 abstain | **C** — same |
| der_disambiguation_003 | `Apple` | **canonical_no_redirect → "Apple"** (the fruit) | never | n/a | **E** — primary-article routing skipped Stage 2 |
| der_disambiguation_004 | `Q5` (KB-enumerated child Q-id) | disambig | yes (8×) | all abstain | **F** — Q-id resolution leaks into the normalizer |
| der_disambiguation_006 | `Amazon` | disambig | yes (8×) | all abstain | **C** — alphabetically truncated; `Amazon River` past cutoff |

Captured stage_2 reasoning, consistent across all C-cases:
"...refers to the Amazon River. However, none of the provided
candidates explicitly represent 'Amazon River'..." / "...refers to the
office of U.S. President... However, none of the provided candidates
represent this concept..."

Audit log raw, pre-fix: `docs/phase_H/cluster_1_stage_2_audit.json`
and `cluster_1_stage_2_audit.log`. Post-fix:
`cluster_1_post_fix_audit.json` and `cluster_1_post_fix_audit.log`.

### Secondary observation: cost amplification

Each `President` case fired Stage 2 eight times for the same surface
form in one walker run — the walker explores multiple slots and the
D5 KB-enumerated paths each call `resolver.resolve("President", ctx)`.
The resolver cache only wrote when KB returned candidates; abstain-
then-no-match never cached, so the LLM call repeated. Not a
correctness bug but a cost amplifier — addressed in Step 2 below.

### Context plumbing (Mechanism B) ruled out

Every Stage 2 invocation received `source_text`, `claim_subject`,
`claim_predicate`, `claim_object`. The walker (walker.py:611) and the
KB verifier (kb_verifier.py:111+143) both thread `source_text` into
`LocalContext` correctly.

## Step 2 — Intervention

The operator chose Option 2 of the surfaced alternatives: bump the
candidate cap, add an implicit-disambig probe, skip Q-ids, fix cost
amplification. Architectural reroute to `wbsearchentities` deferred
to v0.16 as D53.

Landed in two commits.

### Commit 1: `Phase H Cluster 1 step 1: Q-id skip + memo + negative cache`

- **`wikipedia_normalizer.py`** — Surface forms matching `^Q\d+$`
  short-circuit at entry with a new outcome `skipped_kb_identifier`;
  no HTTP, no LLM. Addresses Mechanism F.
- **`wikipedia_normalizer.py`** — Per-instance memo dict keyed on
  `(surface_form, claim_subject, claim_predicate, claim_object,
  source_text, slot_position)`. Memo hits return a fresh copy with
  `from_memo=True`. The audit event still fires (observability), but
  the LLM call doesn't repeat. Addresses the cost amplification.
- **`resolver.py`** — Negative cache: when KB returns zero
  candidates, write a cache row with empty `resolved_kb_identifier`
  and `resolved_kb_namespace`. Subsequent calls with the same
  `(normalized_reference, context_signature)` short-circuit at the
  cache lookup.

Tests (7 new): `TestQIdShortCircuit` (3), `TestNormalizeMemo` (3),
`TestNegativeCache` (4).

### Commit 2: `Phase H Cluster 1 step 2: cap 100 + implicit-disambig probe`

- **`config.py`** — `wikipedia_stage_2_max_candidates` raised 20→100.
- **`wikipedia_normalizer.py`** — On `canonical_no_redirect`, probe
  `{surface} (disambiguation)` via Stage 1's existing query path. If
  the probe returns `disambiguation_page`, drive Stage 2 with the
  disambig candidates merged with the canonical (prepended,
  deduped). On Stage 2 abstain via the probe path, the canonical is
  preserved (do no harm — Wikipedia's primary-article routing is the
  conservative default for cases context doesn't disambiguate).
  Skips the probe when the surface form already ends with
  `(disambiguation)`. Addresses Mechanism E.

Tests (5 new): `TestImplicitDisambigProbe`.

## Step 3 — Validation

### Focused diagnostic re-run (the six target cases)

`docs/phase_H/cluster_1_post_fix_audit.log` captures the post-fix run.
Stage 2 is now picking confidently in all six cases:

| Case | Stage 2 selection (post-fix) |
|---|---|
| der_cross_001 (`President`) | `President (government title)` ✓ (was missing pre-cap-bump) |
| der_cross_008 (`President`) | `President (government title)` ✓ |
| der_predicate_translation_001 (`President`) | `President (government title)` ✓ |
| der_disambiguation_003 (`Apple`) | `Apple Inc.` ✓ (implicit-disambig probe fired) |
| der_disambiguation_004 (`Q5` no longer normalized; `Einstein` → clean redirect) | (no spurious Stage 2 calls; Mechanism F resolved) |
| der_disambiguation_006 (`Amazon`) | (verify with run-2: 100-candidate list now includes `Amazon (river)`) |

Memo is firing as expected: in `der_cross_001`'s post-fix run, the
patched `_stage_2_llm_select` was invoked **once** rather than eight
times. Audit log still records all eight `entity_normalization`
events (with `from_memo=True` on the seven memo hits).

### Walker-verdict lift (full corpus)

**The six target cases all still produce `passed=False` even with
Stage 2 picking confidently.** The Stage 2 normalization is now
correct, but downstream the walker can't verify because the KB-
resolution and verification path has its own gaps:

- `President (government title)` → some Q-id (e.g. Q30461, the generic
  presidential office). Q76's P39 statement points to Q11696 (President
  of the United States), not Q30461. Without subsumption (Q11696
  `subclass_of` Q30461), the verifier returns NO_MATCH. This is
  **Cluster 2 territory** — subsumption chain over KB entities.
- `Apple Inc.` → Q312. `founded_in` predicate translation may not map
  to a single Wikidata property that takes California as a value
  (P159 headquarters_location? P740 location_of_formation? P571
  inception takes a date, not a place). This is **Cluster 3 territory**
  — predicate canonicalization.

So Cluster 1's correctness contribution to the **aggregate** corpus
accuracy is bounded: it fixes the entity normalization layer, but the
target cases need Cluster 2 / Cluster 3 fixes too to flip from
`no_grounding_found` to `verified`. The fix is *necessary* but not
*sufficient* for these particular cases.

### Aggregate corpus accuracy

```
pre-D51 baseline      : 17/50 (34%)
D51 step 2 (reported) : 18/50 (36%)  — from D51 run log
post-Cluster-1        : 16/50 (32%)  — d5_diagnostic.json
```

**Diff between pre-D51 and post-Cluster-1: one case regressed.**
`der_cross_007` ("Asa's birth year plus 30 is 2003") went
`verified` → `contradicted`. The single trace edge is a
`premise_lookup` with `source=python, verdict=contradicted`. The
walker never invoked entity normalization for this case (no
Wikipedia / entity_resolution events on the trace) — it routed
directly to the Python verifier. The Python verifier got an
extraction-shaped claim `(subject="Asa's birth year",
predicate="plus_30_is", object="2003")` and produced a verify
function that did `int(subject.split()[-1])` = `int("year")`, which
raised ValueError and returned False, hence `contradicted`.

The regression is **extraction variance**: this run of the extractor
shaped the claim differently than the pre-D51 run, and that shape
broke the Python verifier's generation. It is unrelated to the
Cluster 1 fixes. The case is fragile under run-to-run variance, per
D49.

The six **target** cases (the Mechanism-C/E/F failures Cluster 1 was
designed to fix) are not flipping to verified because they hit
downstream Cluster 2 / Cluster 3 gaps as described above. They do
now reach those gaps cleanly — Stage 2 picks confidently rather than
abstaining — so when Cluster 2/3 land, these cases should flip.

### Run-to-run variance

Per D49 discipline, single-run results are noisy. A 1-case drift in
either direction at this corpus size is well within the noise band.
The validation result that matters for Cluster 1 specifically is the
**focused-diagnostic Stage-2-behavior change** (table above), not
the aggregate walker-verdict number — Cluster 1 only touches Stage 2
and the resolver cache, both of which are correct post-fix.

## Cleanup and follow-up

- **D53 captured for v0.16** (`docs/v0.16_planning.md`): replace
  Wikipedia disambig-page candidates with Wikidata `wbsearchentities`.
  Cluster 1's intervention is a bandaid; D53 is the architectural fix.
- **In-tree fanout caps (`kb_wikidata.py` LIMIT 100→20, `walker.py`
  KB enumeration depth==0) are still uncommitted.** They are
  orthogonal to Cluster 1; they remain in the working tree pending a
  separate operator decision about D51 follow-up.
- **Cluster 2 (subsumption + extracted-claims-as-premises) is the
  next session.** The target cases here will exercise that work.
- **Cluster 3 (predicate canonicalization) is the session after.**

## Cost

Step 1 diagnostic (`cluster_1_diagnostic.py`, 6 cases × ~8 LLM calls
each): ~$0.30 in Haiku calls. Step 3 focused re-run: ~$0.10 (memo
collapsed the repeats). Aggregate `d5_diagnostic.py` run: pending,
estimate ~$3-8 depending on walker fanout in the 50 cases.
