# Phase G D33 — Entity-resolution type filtering (design)

Closes v0.16 D33 work item 1. Adds a post-filter step to
`WikidataAdapter._live_resolve` that uses Wikidata's P31 (instance of)
property to filter candidate Q-ids by the expected type implied by the
predicate's resolution slot. The architecturally-required type filtering
from §9.3 lands at the resolution boundary as the operator's Option 3
chose during Phase G planning.

## Goal

Fix the empirical pattern documented in `docs/v0.16_planning.md` D33: when
the canonical entity (e.g. Q76 Barack Obama) is not in `wbsearchentities`'
top-10 default candidate pool for a query like "Obama", the deployed
pipeline cannot resolve to it. Type filtering removes the irrelevant-type
candidates that crowd out the canonical entity, lifting the architectural
ceiling on entity-resolution-heavy corpora (Phase 10.5's
`entity_resolution_corpus`, `kb_mapping_corpus`, and indirectly
`derivation_corpus` through cross-source verification cases).

## Current behavior

`_live_resolve` at `src/aedos/layer4_sources/kb_wikidata.py:372`:

1. Calls `wbsearchentities` with `limit=Config.wikidata_candidate_pool_size`
   (default 10).
2. Parses the search results into `ResolutionCandidate` objects with
   `score=1/(rank+1)`.
3. Returns the list (or `[]` on failure).

`EntityResolver.select` at `src/aedos/layer3_substrate/resolver.py:79`:

1. Sorts by score.
2. If `top.score < 0.6`, returns None (no candidate good enough).
3. If top-1 and top-2 scores within `_AMBIGUITY_GAP=0.15`, defers to LLM
   selection. Otherwise returns top-1's Q-id.

Result: the canonical entity must be in the top-10 by Wikidata's search
ranking for the pipeline to reach it. The D33 finding records that this
fails for at least Q76 (Obama) and Q49112 (Williams College) for default
queries on 2026-05-20. Two xfail tests at
`tests/integration/live/test_wikidata_live.py:111-161` codify the
failure as expected and document the v0.16 work item.

## New behavior

After `wbsearchentities` returns N candidates, an additional batched
`wbgetentities` call fetches each candidate's P31 (instance of) values.
The adapter filters the candidate list to those whose P31 ∩
`local_context.expected_entity_types` is non-empty. If the filter
eliminates all candidates, the adapter returns `[]` (triggering
abstention per §9.4) — it does NOT fall back to the unfiltered list,
because the whole point of the filter is to prevent wrong-type entities
from being selected.

If `local_context.expected_entity_types` is empty or absent (e.g. the
predicate's metadata doesn't carry types, like the new `prefers` and
`status` seed entries which accept open object types), the adapter
skips the filter and returns the unfiltered candidate list — preserving
current behavior for predicates that should not be type-filtered.

The candidate pool size is raised from 10 to 30 (configurable). The
filter eliminates wrong-type entries; a larger initial pool increases
the probability the canonical entity is present.

## Design decisions

### Post-filter, not SPARQL pre-filter

Operator confirmation: post-filter via batched P31 fetch. Three reasons
the operator cited:

1. Wikidata's relevance ranking from `wbsearchentities` is genuinely
   useful and post-filter preserves it. SPARQL pre-filter loses the
   ranking (SPARQL doesn't carry the search scoring), risking worse
   default-case behavior for the common path.
2. Runtime cost is bounded: P31 fetches are batched via `wbgetentities`
   (one extra roundtrip per resolution regardless of candidate pool
   size; the API accepts up to 50 entity ids per call). Cached
   candidates skip this entirely on cache hit.
3. Implementation surface is smaller — adds a step rather than
   replacing a call.

### Fallback on no match: empty list, not unfiltered list

Operator confirmation: return `[]` when type filter eliminates all
candidates. The alternative (fall back to unfiltered top-N) would
re-introduce the problem the filter exists to solve — wrong-type
entities being selected because the canonical entity is absent and
nothing else is constrained.

The empty list triggers abstention per architecture §9.4 ("Entity not
found → empty candidates → abstention"). This is correct behavior: if
type filtering eliminates everything, the system honestly reports it
couldn't find a matching entity, rather than silently pick a wrong-type
candidate.

### Exact-match only on entity types (no P279* traversal in v0.15)

Wikidata's class hierarchy uses P279 (subclass of). An entity instance
of Q21036474 (fictional human) is implicitly also Q5 (human) via P279.
A strictly-exact P31 match would reject Q21036474 candidates when the
expected type is Q5.

For v0.15, the implementation uses exact-match on the P31 Q-ids
returned. Sub-class traversal via P279* is deferred to v0.16. Per
operator instruction: "defer to v0.16 unless D33's testing surfaces
specific cases where exact-match filtering loses meaningful candidates."
The validation gate (D33 xfail tests + derivation corpus) will tell us
whether this matters in practice.

The implementation should be extensible — traversing P279* is a
straightforward future addition — but doesn't have to do it in v0.15.

### Caller populates `LocalContext.expected_entity_types`

The adapter doesn't fetch predicate metadata directly. The walker /
substrate caller looks up the predicate via
`PredicateTranslator.consult`, extracts `subject_entity_types` or
`object_entity_types` (depending on slot), and passes them on the
`LocalContext` it constructs.

Predicates with no entity-type fields in their seed entry (e.g. the new
`prefers` predicate, which takes open object types) pass empty lists or
None; the adapter then skips the filter.

This keeps the resolver layer agnostic to the substrate's predicate
table. Same pattern as how `predicate` and `slot_position` already
flow.

## Schema changes

### `LocalContext`

```python
@dataclass
class LocalContext:
    predicate: str
    slot_position: str
    asserting_party: Optional[str] = None
    prior_resolutions: list["ResolutionCandidate"] = field(default_factory=list)
    # Phase G D33 (2026-05-23):
    expected_entity_types: list[KBEntityID] = field(default_factory=list)
```

Backward-compatible: existing call sites that don't populate
`expected_entity_types` get an empty list, and the type filter no-ops
for them.

### `PredicateMetadata`

`subject_entity_types` and `object_entity_types` are already in the
seed pack format (Phase G D39 added them for the three new entries).
The existing `PredicateMetadata` dataclass needs the same two fields
to surface them from `predicate_translation.consult`.

Existing seed-pack entries (61 of them) don't yet have the entity-type
fields. D33 implementation does NOT bulk-populate those — that's
v0.16 work (or follow-up after Phase 10.5 surfaces which predicates
need filtering). For predicates without entity types, the filter
no-ops and current behavior is preserved.

The substrate oracle (`predicate_translation.py`) generates metadata
for cold-start predicates. The oracle's prompt should be augmented to
also emit `subject_entity_types` and `object_entity_types` when it
generates a new entry. Verify the current prompt — if it doesn't ask
for these, augment it.

### Audit event

The existing `kb_live_resolve` event records candidate_count and
duration_ms. Add fields:

- `pre_filter_count` — candidates returned by wbsearchentities (before
  type filter)
- `filter_eliminated_count` — how many candidates were dropped
- `filter_no_op_reason` — when the filter didn't fire, why
  ("no_expected_types", "wbgetentities_failed", "filter_disabled")

This makes the filter's effect visible to post-hoc analysis. Useful
for Phase 10.5 to see how often the filter rescues a canonical entity
vs. how often it eliminates a candidate the LLM would have picked
correctly anyway.

## Configuration

`src/aedos/config.py` additions:

- `wikidata_candidate_pool_size` — already exists; default raised
  from 10 to 30. Operator-overridable.
- `wikidata_type_filter_enabled` — new; default True. Allows
  disabling the filter wholesale for diagnostic comparison runs.
- `wikidata_type_filter_p31_batch_size` — new; default 50 (matches
  the wbgetentities API limit). Adjustable if Wikidata changes its
  limit.

## Batched wbgetentities mechanics

The Wikidata API endpoint `https://www.wikidata.org/w/api.php` accepts:

```
action=wbgetentities
ids=Q76|Q49112|Q41773  (pipe-separated, up to 50)
props=claims
format=json
languages=en
```

Response shape (simplified):

```json
{
  "entities": {
    "Q76": {
      "claims": {
        "P31": [
          {"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}, ...},
          ...
        ]
      }
    },
    ...
  }
}
```

The adapter extracts each candidate's P31 Q-id list:

```python
def _extract_p31(entity_data: dict) -> list[str]:
    claims = entity_data.get("claims", {}).get("P31", [])
    return [
        c["mainsnak"]["datavalue"]["value"]["id"]
        for c in claims
        if (c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id"))
    ]
```

A candidate keeps if `set(p31_ids) & set(expected_entity_types)` is
non-empty.

## Validation gates

After implementation:

1. **D33 xfail tests flip to xpass** (or pass without `xfail` mark).
   `test_obama_canonical_q76_currently_unreachable` and
   `test_williams_college_canonical_q49112_currently_unreachable`
   at `tests/integration/live/test_wikidata_live.py:111-161` —
   with type filtering, Q76 (instance of Q5 human) and Q49112
   (instance of Q3918 university) are reachable for queries with
   the right predicate metadata. Two xpass results = D33 implementation
   verified at the original symptom level.

2. **`der_revision_001/002` produce contradicted verdicts.** The D23
   `lives_in` correction is the prerequisite; D33's type filter helps
   if the cases route through entity resolution (they don't directly,
   but the broader belief-revision path benefits from filtered
   resolutions).

3. **D39's three cold-start predicates pass their corpus expectations.**
   `der_cross_007` (born_in_year), `der_cross_009` + `der_revision_001`
   (prefers), `der_revision_005` (status) — verify each produces the
   expected verdict.

4. **Full pytest suite green** — no regression on existing tests. Note:
   the existing `test_wikidata_adapter.py` tests against fixtures may
   need updating if they assert specific candidate counts that
   change with the filter.

## Tests

### Unit (offline, fixture-mocked)

- `test_type_filter_keeps_matching_p31` — given mock wbsearchentities
  + wbgetentities responses, filter retains candidates whose P31
  intersects expected_entity_types.
- `test_type_filter_drops_non_matching` — candidates whose P31 misses
  are removed.
- `test_type_filter_returns_empty_when_all_drop` — no candidate
  matches → `[]`. Audit event records `filter_eliminated_count == n`.
- `test_type_filter_skipped_when_no_expected_types` — empty
  `expected_entity_types` → filter no-ops; current behavior preserved.
- `test_type_filter_no_op_on_wbgetentities_failure` — wbgetentities
  HTTP failure → filter no-ops, returns unfiltered candidates with an
  audit `filter_no_op_reason: wbgetentities_failed`. (Trade-off:
  fail-open vs fail-closed. Fail-open is the less-disruptive default
  for v0.15 — a transient API failure shouldn't abstain on every
  resolution; the audit makes the issue visible.)
- `test_batched_wbgetentities_respects_size` — given 80 candidates,
  the adapter makes 2 wbgetentities calls (50+30).

### Integration (live, RUN_LIVE_KB=1)

- `test_d33_obama_reaches_q76_with_type_filter` — query "Obama" with
  predicate=holds_role and expected_entity_types=[Q5]; Q76 is in
  the filtered candidates. (Removes the existing xfail; this becomes
  a passing test.)
- `test_d33_williams_college_reaches_q49112_with_type_filter` — query
  "Williams College" with predicate=located_in and
  expected_entity_types=[Q3918, Q38723] (university, higher
  education institution); Q49112 is in filtered candidates.
- `test_d33_type_filter_drops_obama_fukui_for_person_query` — query
  "Obama" with expected_entity_types=[Q5]; Q41773 (Obama, Fukui town)
  is NOT in filtered candidates.

### Calibration (RUN_CALIBRATION=1 RUN_LIVE_KB=1)

Phase 10.5 runs `entity_resolution_corpus`, `kb_mapping_corpus`, and
`derivation_corpus` against the live adapter. The expected lift from
D33 type filtering is measured there, not in this commit's tests.

## Open questions (surfaced for v0.16, not blocking D33)

1. **Sub-class traversal via P279*** — when does exact-match cost us
   meaningful candidates? Phase 10.5 data should surface this.
2. **Bulk-populate entity types on existing seed entries** — D33's
   filter only fires for predicates with entity types in their
   metadata. 3 of 64 entries currently carry them (Phase G D39
   additions). The remaining 61 need entity types if their resolutions
   should be filtered. Audit pass during Phase 10.5 or v0.16.
3. **Cache behavior for filtered resolutions** — the resolver caches
   the *result* of resolution, not the candidate list. If a future
   query with different expected_entity_types hits the same cache key
   (which includes predicate), the cached resolution is consumed
   without re-filtering. This is correct because the cache key
   incorporates predicate (and predicate determines entity types via
   seed pack metadata); but if predicate metadata changes the entity
   types after a cache write, the cached resolution may be stale.
   v0.16 candidate: cache invalidation on seed-pack changes, or
   include a metadata version in the cache key.

## Estimated effort

2-4 days realistic per operator estimate. Breakdown:

- LocalContext + PredicateMetadata schema changes + tests: 0.5 day
- wbgetentities batching + P31 extraction + tests: 1 day
- Filter logic in _live_resolve + audit event additions + tests: 0.5 day
- Substrate oracle prompt augmentation for entity-type emission: 0.5 day
- D33 xfail test flip + new integration tests: 0.5 day
- Live validation (RUN_LIVE_KB=1 runs) + bug-fixing surprise behavior: 0.5-1.5 days

## Phase G sequencing reminder

D33 is the last of Phase G's three items (D39 → D23 → D33 → tag rc.9).
D39 (committed `b65d7e2`) and D23 (committed `d543ef9`) are done.
After D33 + validation, tag `v0.15.0-rc.9` per the Phase G plan; Phase
10.5 starts from rc.9 with the architectural ceiling lifted.
