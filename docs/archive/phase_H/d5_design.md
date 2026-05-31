# Phase H D5 — KB neighbor enumeration: design

**Status:** design — awaiting operator confirmation on the surfaced decisions before step 1 implementation.
**Date:** 2026-05-23.
**Closes:** v0.16 D5 (the canonical entry of the gap).
**Adds:** a fourth operation to the KB protocol; new audit events; one
new SPARQL query shape; walker integration as an additional derivation
strategy in the existing stack.

## Goal

The walker can today verify a claim by traversing *known* substrate
state — Tier U premises, prior verified claims, subsumption rows the
oracle has already cached — but it cannot ask the KB "what does
Wikidata say about entity X?" as a *general enumeration* to discover
new premises mid-walk. The current three-operation KB protocol
(`resolve_entity`, `lookup_statements`, `subsumption`) all check or
fetch state for a *known* entity/pair; none enumerates.

D5 adds a fourth operation: **enumerate this entity's KB neighbors
along a constrained set of properties**, so the walker can ground a
derivation in KB-sourced premises it didn't already have.

## Why this matters under the corrected derivation baseline

Phase H D16's harness fix lowered the derivation accuracy from 36%
(Phase E v2, artifactually inflated by Tier U state leakage) to **30%
(15/50)** honest, with only **2** `verified` and **0** `contradicted`
verdicts produced. The walker is dominantly abstaining. Of the 35
failing cases, the largest sub-category by failure mode is multi-hop
derivations (`der_multihop_001`–`_008`, `_012` — 9 of 50) — exactly
the case shape D5 is designed for. Without D5 the walker cannot
discover a part_of / is_a chain from cold start; the only multi-hop
cases that pass are the three `pd_neither` gate-closed
abstain-expecting cases.

D5's expected lift, ballpark-conservatively: 5–10 multi-hop cases move
to `verified`, plus possibly 2–3 cross_source / predicate_translation
cases that benefit from KB-grounded derivations. A 10–25 pp lift on
derivation_corpus is the design target — but the operator should not
hold the implementation to a specific number; the architecture is the
contract, the lift is what it is.

## Design decisions — explicit operator-confirmation surface

Six decisions need confirmation before step 1 implementation. Each
has a recommendation; the operator can override or amend.

### Decision 1 — Property set to enumerate (architectural scope)

Wikidata has thousands of properties. The walker doesn't need most of
them; enumerating them all produces noise and burns the rate limit.
The architecture-aligned starter set:

**Recommendation: start with the geographic/taxonomic core (5
properties).**

| P-code | Property | Why for v0.15 |
|---|---|---|
| P31 | instance_of | Type-level derivations (X is_a Y) |
| P279 | subclass_of | Taxonomic chains (X subclass_of Y) |
| P361 | part_of | Mereological / compositional containment |
| P131 | located_in (admin entity) | Geographic containment |
| P17 | country | Country-level grounding |

These five cover the dominant `derivation_corpus` multi-hop shapes
(`X lives_in Williamstown`, `Williamstown part_of Massachusetts`; `X
is_a human`, `humans are mortal`; etc.). The seed pack's
distribution-gated walker traversal already targets these (see
`predicate_distribution.py` v2 prompt with its locative-containment
and kind/universal-property example families); D5's neighbor
enumeration extends the same traversal one architectural step.

**Out of v0.15 scope, defer per measured need:** P50 (author), P108
(employer), P39 (position_held), P276 (location), P127 (owned by),
P57 (director), etc. These pull in domains (work-authorship,
employment, ownership) that the derivation corpus doesn't currently
exercise multi-hop and that Phase 10.5 may surface specific cases
for.

**Confirmation needed:**

- Start with the 5-property core?
- Or include additional properties up front (operator names which)?

The implementation makes the property set a `Config` field so
adjustments are configuration-level, not code-level — but the v0.15
default set needs the operator's call.

### Decision 2 — Depth bound

Recursive enumeration explodes: `Williamstown part_of Berkshire
County part_of Massachusetts part_of New England part_of United
States part_of North America`. Walker traversal has to stop.

Two parameters interact:

- **The walker's existing `max_depth`** (`_DEFAULT_MAX_DEPTH = 4` in
  `walker.py`) bounds how many derivation hops the walker takes
  *before* abstaining. Each neighbor-enumeration call counts as one
  hop's worth of expansion.
- **A new per-call neighbor depth**: if `enumerate_neighbors(X,
  P361)` is called, does it return only X's *direct* P361 neighbors,
  or transitively-reachable ones?

**Recommendation: per-call enumeration is *one hop only* (direct
neighbors); the walker's existing `max_depth` controls overall
traversal depth via multiple enumeration calls.**

The walker walks: enumerate X's direct P361 neighbors → for each
neighbor Y, check if Y verifies the claim → if not and walker has
remaining depth budget, recurse (enumerate Y's neighbors). This
keeps each enumeration call cheap (one SPARQL query, bounded
result size) and reuses the walker's existing depth-bound
mechanism. The alternative (transitively-reachable enumeration in
one call) would push the bounding logic into the SPARQL query
itself, which the WDQS timeout policy disfavors.

Confirmation needed: one-hop-per-call, recurse via walker depth.

### Decision 3 — Caching strategy

The existing `CachingHTTPClient` (Phase F) handles per-HTTP-call
caching with a TTL. That's sufficient for `_live_lookup` because
each call is keyed on (entity, predicate). For `_live_neighbors`,
the same per-call HTTP cache works — but a *higher-level*
entity-to-neighbor-by-property cache could amortize across walker
traversals that visit the same entity multiple times.

**Recommendation: rely on the existing HTTP cache (`Config.http_cache_statement_ttl_seconds`,
default 86400s). Don't add a higher-level cache for v0.15.**

Rationale: KB neighbor data rarely changes (Wikidata edits are
infrequent for stable entities); the HTTP cache TTL already provides
24-hour freshness. A higher-level cache adds a state surface (cache
invalidation, retraction propagation, audit trail) that v0.15 doesn't
need. The HTTP cache is keyed on the SPARQL URL+query, which is
deterministic for (entity, property), so identical calls hit the
cache transparently.

If Phase 10.5 surfaces a hotspot pattern (e.g., one entity gets
enumerated 5+ times per case), v0.16 can add the higher-level cache
as a profile-driven optimization.

Confirmation needed: HTTP cache only for v0.15, or higher-level cache
now?

### Decision 4 — Rate limit projection

The existing rate limiters: search 50/s, SPARQL 5/s. `_live_neighbors`
adds one SPARQL call per (entity, property) miss in the HTTP cache.

**Projection.** A typical multi-hop case (e.g. der_multihop_001:
`Asa lives_in Williamstown`, `Williamstown part_of Massachusetts`,
walker checks if Asa lives_in Massachusetts):

- Walker walks claim (Asa, lives_in, Massachusetts).
- KB verifier abstains (Asa not in KB).
- Walker enters subsumption traversal. Subsumption oracle returns
  unrelated or unknown for (Williamstown, Massachusetts) chain on
  cold start.
- **D5 fires:** walker calls `_live_neighbors(Williamstown_QID,
  [P131, P361])`. SPARQL query returns Williamstown's neighbors —
  Massachusetts shows up. Walker derives (Asa, lives_in,
  Massachusetts) via the enumerated neighbor.
- Total: 1 new SPARQL call per case (cold cache), 0 calls on hot
  cache.

50 corpus cases × ~2 SPARQL calls per case worst-case = 100 SPARQL
calls. At 5/s rate limit, that's ~20s of pure rate-limited time
across the corpus — well within budget for a calibration run that
already takes 900s (per the D16 re-baseline).

**Recommendation: keep the existing 5/s SPARQL rate limit. No new
limiter.**

Confirmation needed: keep existing limit, or pre-emptively raise?

### Decision 5 — Walker integration point in the derivation strategy stack

The walker's derivation strategy stack today (per `walker.py`
`_direct_lookup` then `_expand_via_substrate`, then the outer walk
loop's depth iteration):

1. Tier U literal/broadened lookup (Stages 1, 2, 3).
2. Tier U negation lookup (polarity-conflict belief revision).
3. Tier U object-conflict lookup (D16/B2 functional-predicate
   belief revision).
4. KB verifier (single-statement direct lookup against `meta.kb_property`).
5. Python verifier (gated on `routing_hint == "python"`, F-042).
6. Substrate expansion: subsumption-oracle traversal under
   distribution gating.
7. Loop: re-run 1–6 on each expanded frontier, depth-bounded.

Where does D5's KB-neighbor enumeration slot in?

**Recommendation: a new step 7 — after subsumption-oracle expansion,
in the same loop body.** When `_expand_via_substrate` produces an
empty expanded frontier (the oracle has no cached subsumption rows
that match), the walker now also calls
`_expand_via_kb_neighbors(node)` and adds those enumerated children
to the next frontier. KB-enumerated neighbors are tried in the next
loop iteration via the existing depth-bound mechanism.

Rationale: cheapest-path-first. Subsumption oracle is in-process /
cached; KB enumeration is a live SPARQL call. Try the cheap path
first; fall back to the live enumeration only when the cheap path
produced nothing useful.

Alternative shapes:

- **In parallel with subsumption oracle**: enumerate KB neighbors
  always, combine with oracle's expansion. Pro: catches cases where
  the oracle's cached row is stale or wrong. Con: doubles the
  per-hop cost on every walk, even when the oracle had the answer.
- **Before subsumption oracle**: prefer KB-grounded derivations
  over oracle-based ones. Pro: KB is authoritative. Con: makes the
  walker substantially slower (live SPARQL per hop) and ignores the
  oracle's whole purpose (cache LLM-derived subsumption judgments).

**Recommendation stays: after subsumption oracle, fallback shape.**

Confirmation needed: cheapest-path-first, fallback after subsumption?

### Decision 6 — Failure handling

When `_live_neighbors` returns nothing, errors out, or hits the rate
limiter, the walker falls through to abstain — matching D33's
fail-open pattern and architecture §3.2's "soundness over
completeness" framing.

**Recommendation:** match `_live_lookup`'s shape: single retry on
transient `httpx.TimeoutException` / `httpx.NetworkError`, then
return `[]`. Log every attempt to the audit log
(`kb_live_neighbors`). Never raise except on a wiring-gap defence
(missing `http_cache` constructor argument) — the calibration runner
should not crash on transient network blips.

Confirmation needed: match `_live_lookup`'s retry/log/fail-open
shape?

## API design

### New KB protocol method

```python
# kb_protocol.py
class KBProtocol(Protocol):
    def resolve_entity(self, reference: str, local_context: LocalContext) -> list[ResolutionCandidate]: ...
    def lookup_statements(self, entity: KBEntityID, predicate: KBPropertyID) -> list[Statement]: ...
    def subsumption(self, entity_a: KBEntityID, entity_b: KBEntityID, relation_type: str) -> SubsumptionResult: ...
    # NEW (D5):
    def enumerate_neighbors(
        self,
        entity: KBEntityID,
        properties: list[KBPropertyID],
    ) -> dict[KBPropertyID, list[KBEntityID]]: ...
```

Returns a dict keyed by property: each value is the list of entity
Q-ids that appear as that property's value for the given subject
entity. Empty dict means no neighbors found or the call failed
(audit log captures the distinction).

### WikidataAdapter implementation

```python
# kb_wikidata.py
def enumerate_neighbors(
    self, entity: KBEntityID, properties: list[KBPropertyID],
) -> dict[KBPropertyID, list[KBEntityID]]:
    if self._live:
        return self._live_neighbors(entity, properties)
    return self._fixture_neighbors(entity, properties)
```

`_live_neighbors` issues one SPARQL query of the shape:

```sparql
SELECT ?prop ?value WHERE {
  VALUES ?prop { wdt:P31 wdt:P279 wdt:P361 wdt:P131 wdt:P17 }
  wd:Q49166 ?prop ?value .
  FILTER(isIRI(?value))
}
```

Parses to `{P31: [...], P361: [...], ...}`. Single round-trip per
entity regardless of property count.

`_fixture_neighbors` reads from `tests/fixtures/wikidata/neighbors_<entity>.json`
(format symmetric with `sparql_<predicate>_<entity>.json`).

### Walker integration

```python
# walker.py
def _expand_via_kb_neighbors(self, node: Claim, trace: JustificationTrace) -> list[Claim]:
    """D5: enumerate KB neighbors of premise entities as additional walker
    frontier. Called when subsumption-oracle expansion produced nothing
    and the walker still has depth budget."""
    if self._kb_verifier is None:
        return []  # walker can be configured without KB; honor that
    properties = self._cfg_value("walker_kb_neighbor_properties", _DEFAULT_NEIGHBOR_PROPERTIES)
    expanded = []
    for slot_value, slot_role in ((node.subject, "subject"), (node.object, "object")):
        resolved = self._resolve_to_qid(slot_value)
        if resolved is None:
            continue
        neighbors = self._kb_verifier._kb.enumerate_neighbors(resolved, properties)
        # ... per-property, per-neighbor: emit a new Claim with the substituted slot
        # and a trace edge marking the kb_neighbor_enumeration step.
    return expanded
```

(Details refined at step 2 implementation.)

## New audit events

- `kb_live_neighbors` — one per call. Fields: `entity`,
  `properties_requested`, `total_neighbors_returned`, `duration_ms`,
  `retry_count`, `error`.
- `walker_kb_neighbor_expansion` — one per walker call that fires
  the D5 path. Fields: `claim_id`, `expanded_from`, `enumerated_entities`,
  `properties_consulted`.

These let Phase 10.5 attribute lift to the D5 mechanism specifically
(vs. subsumption-oracle traversal or KB lookup).

## Test surface

- **Mocked unit tests** (`tests/unit/test_wikidata_neighbors.py`,
  `tests/unit/test_walker_kb_neighbors.py`): the four-outcome shape
  (success, empty, transient error+retry, hard error+fail-open) and
  walker integration with constructed scenarios.
- **Fixture-backed integration tests** (`tests/integration/test_walker_kb_neighbors.py`):
  walker integration against seeded neighbor data, mimicking
  derivation_corpus cases.
- **Live integration test** (`tests/integration/live/test_wikidata_neighbors_live.py`):
  one or two known entities (Williamstown, Honolulu) under
  `RUN_LIVE_KB=1`, asserting expected neighbors appear and the
  audit log records the call.
- **derivation_corpus re-measurement** (via `scripts/d16_recalibrate.py`
  shape): step 4 validation runs the corpus under the post-D5
  build, compares to the post-D16 baseline (30%), surfaces case
  movements.

## Step plan

1. **`_live_neighbors` + protocol method** (~1 day).
   `kb_protocol.py`: add `enumerate_neighbors` to the Protocol.
   `kb_wikidata.py`: implement `_live_neighbors` + `_fixture_neighbors`
   + `_DEFAULT_NEIGHBOR_PROPERTIES` constant.
   Tests: unit (4-outcome shape) + live (Williamstown + Honolulu).
   Commit: `Phase H D5 step 1: _live_neighbors KB enumeration`.
2. **Walker integration** (~1.5 days).
   `walker.py`: add `_expand_via_kb_neighbors`. Slot into outer
   loop after `_expand_via_substrate`. New trace edges.
   `Config`: new fields (`walker_kb_neighbor_properties`).
   Tests: unit + integration scenarios.
   Commit: `Phase H D5 step 2: walker integration with KB neighbor enumeration`.
3. **(Conditional) Entity-neighbor cache** — per Decision 3,
   skipped for v0.15. If Phase 10.5 surfaces a hotspot, v0.16
   adds it.
4. **Validation** (~0.5–1 day).
   Re-run `scripts/d16_recalibrate.py` with `derivation_corpus`
   only. Compare post-D5 to post-D16 (30%).
   Document in `docs/phase_H/d5_validation.md` per case-movement
   shape established in `d16_fix.md`.
   Commit: `Phase H D5 step 4: derivation_corpus measurement and validation`.

## Decisions summary — what needs operator confirmation

| # | Decision | Recommendation |
|---|---|---|
| 1 | Property set | 5-property geographic/taxonomic core (P31, P279, P361, P131, P17) |
| 2 | Depth bound | One-hop-per-call enumeration; recurse via walker `max_depth` |
| 3 | Caching | HTTP cache only for v0.15; defer higher-level cache to v0.16 |
| 4 | Rate limit | Existing 5/s SPARQL limit, no change |
| 5 | Walker integration point | After subsumption oracle (cheapest-path-first fallback) |
| 6 | Failure handling | Match `_live_lookup` shape (retry-once, log, fail-open) |

If the operator confirms (or amends) these six decisions, step 1
implementation begins.
