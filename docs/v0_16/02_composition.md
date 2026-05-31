# Aedos v0.16 ? Change Specification: Workstream 2 ? Discover/Verify Composition

*Implementation-ready spec. Conforms to the interface contract in `00_overview_contract_ordering.md`, which is AUTHORITATIVE on all cross-workstream interfaces. File:line references were verified by the spec-mapping pass against the v0.16 base (identical to `main` at branch creation).*

---

## DETAILED CHANGE SPEC
ial# WS2 Implementation-Ready Change Spec

All file:line citations verified against the actual current code read in full:
`src/aedos/layer4_sources/walker.py` (1125 lines), `kb_wikidata.py` (1608 lines),
`kb_verifier.py` (670 lines), `kb_protocol.py` (95 lines),
`layer3_substrate/predicate_distribution.py` (243 lines), `subsumption.py` (286 lines),
`layer3_substrate/__init__.py`, `layer5_result/trace.py`, `pipeline.py`.

## 0. CURRENT STATE (verified)

Two composition mechanisms exist today and DO NOT talk to each other:

1. **Walker BFS** (`walker.py:328-386` walk loop → `_expand_via_substrate` 907-997 →
   `_expand_via_kb_neighbors` 999-1124). For an ungrounded goal `P(E)`, it consults
   `predicate_distribution` (935) to learn a direction, then substitutes the subject/object
   slot with a taxonomy NEIGHBOR (via `subsumption.find_neighbors` 951, or KB
   `enumerate_neighbors` 1097), emitting a NEW Claim node re-explored next depth. Grounding
   happens by Tier-U/KB re-lookup of the substituted claim. The distribution result is a
   HARD GATE: `if not directions: continue` (943-944) skips the relation entirely.

2. **In-verifier SPARQL ASK** (`kb_verifier.py:468 _subsumption_upgrades` → `kb.subsumption`
   → `kb_wikidata._live_subsumption` 1348 → `_run_subsumption_ask` 1433 →
   `_build_subsumption_ask_query` 324). A first-class transitive property-path ASK over
   `_SUBSUMPTION_PROPERTIES` (143-178) with a type-guarded P361 bridge (357-365). Fully
   transitive (`(path)+`), entailment-correct, but ONLY reachable from inside
   `kb_verifier._compare_positive` / the no-statements fallback — the walker cannot call it
   for arbitrary goals.

`SubsumptionOracle.consult` (`subsumption.py:95`) is NOT called by the walker at all today
(grep-verified: only `find_neighbors` at walker.py:951). `consult` already does
KB→substrate→LLM three-priority resolution (99-125) but requires both entities be
`wikidata` namespace for the KB path — the walker passes `aedos`-namespace surface forms,
so even if routed, consult would skip KB and hit substrate/LLM.

The WS2 refactor UNIFIES these: a liberal `_discover_chains` (mechanism 1's neighbor
expansion, un-gated, plus a new premise-forward frontier) feeds a sound `_verify_chain`
that calls a generalized `verify_transitive_path` (mechanism 2, lifted to a first-class
KB primitive the walker can invoke for any transitive property).

---

## 1. GENERALIZE `_build_subsumption_ask_query` → first-class `verify_transitive_path`

### 1a. New KBProtocol method (`kb_protocol.py`)

ADD to `KBProtocol` (after `enumerate_neighbors`, line 94):

```python
def verify_transitive_path(
    self,
    source: KBEntityID,
    target: KBEntityID,
    kb_property: KBPropertyID,
    relation_type: Optional[str] = None,
) -> "TransitivePathResult": ...
```

ADD dataclass (near `SubsumptionResult`, line 48):

```python
@dataclass
class TransitivePathResult:
    holds: bool                                   # ASK boolean
    establishing_property: Optional[str] = None   # depth-1 anchor (observability)
    error: Optional[str] = None                   # non-None => fail-open (holds=False)
```

Rationale: today `subsumption()` is hardwired to the `relation_type → _SUBSUMPTION_PROPERTIES`
alternation and always runs BOTH directions + an establishing-property SELECT. The walker's
discover/verify needs a SINGLE-direction, SINGLE-property (or relation-alternation)
path-existence check it can drive for ANY transitive KB property (not just is_a/part_of) —
e.g. P171 (parent taxon), P127 (owned by). `relation_type` is optional: when supplied, reuse
the curated `_SUBSUMPTION_PROPERTIES` alternation (+ type-guarded P361 bridge for part_of);
when None, build a single-property `(wdt:{kb_property})+` path.

### 1b. `kb_wikidata.py` — query builder + adapter method

GENERALIZE `_build_subsumption_ask_query` (324-365). Keep its current 2-arg-by-relation_type
behavior (callers `_run_subsumption_ask` 1441 still use it) but extract the path-construction
into a helper that also serves a single-property path:

ADD module function:
```python
def _build_transitive_ask_query(
    source: KBEntityID, target: KBEntityID,
    properties: tuple[KBPropertyID, ...], use_part_of_bridge: bool,
) -> str:
    # validates source/target via _ENTITY_ID_PATTERN, every prop via _PROPERTY_ID_PATTERN
    # path = "|".join(f"wdt:{p}" for p in properties)
    # if not use_part_of_bridge: return f"ASK {{ wd:{source} ({path})+ wd:{target} . }}"
    # else: emit the existing UNION-with-_GEO_REGION_TYPES bridge (lines 357-365 body)
```
REFACTOR `_build_subsumption_ask_query` to delegate: resolve `props` from
`_SUBSUMPTION_PROPERTIES[relation_type]`, call `_build_transitive_ask_query(source, target,
props, use_part_of_bridge=(relation_type == "part_of"))`. No behavior change for existing
callers (byte-identical query output preserved).

ADD adapter method `WikidataAdapter.verify_transitive_path` (alongside `subsumption` 600-605):
```python
def verify_transitive_path(self, source, target, kb_property, relation_type=None):
    if relation_type is not None:
        props = _SUBSUMPTION_PROPERTIES.get(relation_type)
        if props is None: raise ValueError(...)
        use_bridge = relation_type == "part_of"
    else:
        props = (kb_property,)
        use_bridge = False
    # reuse _run_subsumption_ask machinery but with the new query builder;
    # single direction (source→target) only. Fail-open (holds=False) on error.
    # one audit event "kb_verify_transitive_path".
```
Implement via a small `_run_transitive_ask(source, target, props, use_bridge)` (mirrors
`_run_subsumption_ask` 1433-1466: rate-limit, single retry, returns `(bool, error)`). Fixture
path: reuse `_fixture_subsumption` logic keyed on the property/relation, returning
`TransitivePathResult(holds=...)`.

### 1c. SubsumptionOracle parity (optional, observability)

`subsumption.py SubsumptionOracle.consult` (95) stays as-is for the symmetric four-verdict
case. The walker's transitive checks go through the new `verify_transitive_path` (cheaper:
one direction, one ASK). No change to `consult` signature.

---

## 2. DISCOVER/VERIFY REFACTOR of the walk loop and `_expand_via_substrate`

### 2a. Split `_expand_via_substrate` into `_discover_chains` (liberal) + `_verify_chain` (sound)

REPLACE `_expand_via_substrate` (walker.py:907-997) with `_discover_chains`. The new method
returns a list of **candidate expansion claims** WITHOUT the distribution gate foreclosing
relations. The walk loop's call site (line 378) changes:

```python
# OLD (378):
expanded, llm_delta = self._expand_via_substrate(node, trace, depth)
# NEW:
expanded, llm_delta = self._discover_chains(node, trace, depth)
```

`_discover_chains(node, trace, depth) -> (list[Claim], int)` composes THREE liberal
discovery sources (no gate, distribution demoted to RANKER per §3):

1. **Subsumption-neighbor expansion** (current `_expand_via_substrate` body 933-995,
   minus the gate). For each `relation_type in ("is_a","part_of")`:
   - consult `predicate_distribution` (935) — KEEP, but use the verdict only as a RANKING
     HINT (see §3), NOT to `continue`.
   - gather `subsumption.find_neighbors` neighbors (951) AND KB `enumerate_neighbors`
     fallback (now un-capped, §5). Substitute slot → candidate claim. ORDER candidates so
     the distribution-preferred direction's neighbors sort first.

2. **Premise-forward expansion** `_expand_from_premises(node, trace)` (NEW, §4): seed from
   Tier-U facts about the goal's subject, expand via OUTGOING KB edges, meet the goal.

3. (No new LLM beyond the existing distribution consult + resolver/KB calls.)

Each discovered candidate is tagged in trace exactly as today (`subsumption_traversal` /
`kb_neighbor_enumeration` edges, 964 / 1109) PLUS a new `discovery_source` metadata key
(`"subsumption_neighbor"` | `"premise_forward"`) for observability (§WS5 surface).

`_verify_chain` (NEW): the SOUND per-edge check. Where the current code substitutes a slot
and lets the NEXT depth re-lookup (implicitly trusting the taxonomy edge), `_verify_chain`
makes the entailment edge EXPLICIT and entailment-safe by routing the subsumption/transitive
hop through the KB transitive primitive when both endpoints resolve to Q-ids:

```python
def _verify_chain(self, node, neighbor_claim, relation_type, slot, trace) -> bool:
    # Soundness gate (§3.2 never-false-verify): before admitting neighbor_claim as a
    # legitimate substitution, confirm the taxonomy edge actually holds in a source.
    #   - resolve node's slot value and neighbor's slot value to Q-ids (resolver).
    #   - if both resolve: verify_transitive_path(child_qid, parent_qid, prop, relation_type)
    #     consulting the WS3 substrate_exceptions nogood cache FIRST (entailment-safety:
    #     if a nogood says "this path does NOT hold for (predicate, path, subtree)", reject).
    #   - if either does not resolve: fall back to the substrate subsumption row's own
    #     verdict (find_neighbors already filtered to a_subsumed_by_b / b_subsumed_by_a,
    #     subsumption.py:182-202) — the substrate row IS the sound evidence for aedos-namespace
    #     surface forms. This preserves all current passing derivation tests
    #     (test_walker_with_substrate seeds aedos-namespace rows).
    return True/False
```

KEY DESIGN: `_discover_chains` is LIBERAL (proposes every direction's neighbors regardless
of distribution); `_verify_chain` is SOUND (admits a substitution only if the taxonomy edge
is confirmed by KB-transitive-path OR a substrate subsumption row, and not vetoed by a
nogood). The §3.2 soundness invariant is enforced ONLY at verify, per the contract.

The walk loop keeps its existing grounding logic: a substituted claim that `_verify_chain`
admits is added to `next_frontier` (380) and re-looked-up at the next depth via
`_direct_lookup` — UNCHANGED. The distribution direction is now advisory; an edge survives
to the frontier iff `_verify_chain` confirms the taxonomy hop.

### 2b. Reconciling the two composition mechanisms

The in-verifier SPARQL ASK (`kb_verifier._subsumption_upgrades` 468) and the walker BFS now
share ONE transitive primitive: `kb.verify_transitive_path`. `_subsumption_upgrades` keeps
calling `kb.subsumption` (no change required for WS2 — it is value-vs-value and works), but
the walker's chain verification calls `verify_transitive_path`. Document in
`_verify_chain`'s docstring that the verifier-internal ASK handles the
SUBJECT-FIXED/value-subsumption case (KB statement value is more specific than the claimed
value) while the walker's transitive-path handles the SLOT-SUBSTITUTION case (goal slot is a
taxonomic ancestor/descendant of a grounded premise's slot). They are duals, not duplicates.

---

## 3. DEMOTE predicate_distribution from GATE to RANKER

### 3a. Remove the gate (walker.py:942-944)

DELETE:
```python
            directions = _distribution_directions(dist.verdict)
            if not directions:
                continue  # gate closed (neither)
```
REPLACE with a ranking-only read:
```python
            directions = _distribution_directions(dist.verdict)
            # v0.16 WS2: distribution is a RANKER, not a gate. `neither` no longer
            # forecloses the relation — it deprioritizes it. Soundness is enforced
            # downstream in _verify_chain (the transitive-path/substrate edge check),
            # so a wrong `neither` can no longer cause a false-abstain by skipping a
            # genuinely-entailing chain.
            preferred = directions  # used to ORDER candidates, not to skip
```
Then in the neighbor loop (956-963), instead of `if sub.direction not in directions: continue`
(957-958), KEEP all neighbors but sort: neighbors whose `direction in preferred` first.
Same for `_expand_via_kb_neighbors`: emit both outgoing(parent)+incoming(child) candidates,
ordered by `preferred`.

### 3b. Split predicate_distribution into two notions (contract item g)

Per contract (g): "split predicate_distribution into KB-property transitivity (discoverable
from the graph/constraints) vs intensional kind-entailment (the mortal/is_a case)."

- **KB-property transitivity** (part_of containment: lives_in/located_in over part_of): a
  property of the GRAPH. This is now DISCOVERABLE via `verify_transitive_path` directly —
  the walker substitutes the slot and confirms the part_of path in KB. The distribution
  oracle's `distributes_up over part_of` verdict becomes a RANKING hint only.
- **Intensional kind-entailment** (mortal/has_property over is_a, the "humans are mortal,
  Asa is_a human → Asa mortal" case): this is NOT a graph edge — it is a property of the
  PREDICATE's semantics (does a kind-level property transfer to members?). The distribution
  oracle STAYS the authority here, because no KB transitive path expresses "mortal
  distributes_down". For `is_a` with a `distributes_down` verdict and an entity-typed
  predicate that is intensional, `_verify_chain` MUST fall back to the distribution verdict
  as the sound evidence (there is no KB path to confirm). Concretely: in `_verify_chain`,
  when `relation_type == "is_a"` and `verify_transitive_path` cannot confirm (no KB path /
  endpoints unresolved) AND a substrate is_a row exists AND the distribution verdict is
  non-`neither`, admit the edge — the distribution oracle is the kind-entailment authority.

This keeps `test_distributes_down_ascends_to_parent` (test_walker_with_substrate:280) and
`test_distribution_gate_blocks_invalid_traversal` (268) passing: for `prefers × is_a`, the
distribution verdict is `neither`; with the gate removed the relation is still EXPLORED, but
`_verify_chain` finds (a) no KB transitive path golden_retriever→dog for the `prefers`
predicate, and (b) the `neither` verdict provides no kind-entailment authority, so the edge
is REJECTED → still `no_grounding_found`, `subsumption_traversal` edge absent. The behavior
is preserved but for a sound reason (verify-time rejection) rather than a gate.

### 3c. `_distribution_directions` helper

KEEP `_distribution_directions` (203-217) unchanged — it now maps a verdict to a PREFERENCE
set rather than an authorization set. Update its docstring to say "preferred directions
(ranking hint), not a gate."

---

## 4. PREMISE-FORWARD FRONTIER `_expand_from_premises`

NEW method on Walker, invoked from `_discover_chains`:

```python
def _expand_from_premises(self, node, trace) -> list[Claim]:
    """v0.16 WS2: seed a forward frontier from Tier U facts about the goal's
    subject and expand via bounded OUTGOING KB edges, meeting the goal's object.

    For goal P(S, O): the goal asks whether S relates to O. The walker already
    has Tier U premises about S (e.g. 'Asa lives_in Williamstown'). Premise-
    forward expands Williamstown's OUTGOING part_of edges (Williamstown → MA →
    US) and proposes the substituted claims P(S, MA), P(S, US) as candidates —
    the SAME claims subsumption-neighbor discovery would produce, but seeded
    from a KNOWN premise object rather than blind taxonomy enumeration. Bounded
    by max_depth (the walk loop's existing bound) and the un-LIMIT'd outgoing
    _build_neighbors_query (outgoing fanout is naturally small)."""
```

Implementation:
- Look up Tier-U rows for `(asserting_party, node.subject, node.predicate)` of polarity 1
  via the existing `tier_u.lookup` (388) on a probe claim, OR a new lightweight
  `tier_u`-direct query. To avoid a new TierU method, reuse `_query_current`-style read:
  call `self._tier_u.lookup(node, ...)` first; if found, the goal is already grounded (the
  walk loop handles it). Premise-forward is for the case where Tier U has a DIFFERENT object
  for the same (subject, predicate) — exactly what `lookup_object_conflict` (431) surfaces.
  REUSE `lookup_object_conflict(node)` to get rows with a different object O′; for each O′
  that is a known premise, resolve O′ to a Q-id and `enumerate_neighbors(O′_qid, part_of
  props, direction="outgoing")`. If the goal's object O is among O′'s outgoing neighbors (or
  reachable — confirmed by `verify_transitive_path(O′_qid, O_qid, prop, "part_of")`), emit a
  `premise_forward` trace edge and the substitution is grounded.
- This is the BIDIRECTIONAL meet: subsumption-neighbor discovery expands DOWN from the goal
  object; premise-forward expands UP from the premise object; they meet in the middle. This
  replaces the depth==0 cap (§5) as the cost-control mechanism (forward frontier is seeded by
  a small set of premises, not blind enumeration).
- Fail-open: any resolution/KB failure returns `[]` for that premise; never raises (mirrors
  `_expand_via_kb_neighbors` 1037-1039).

---

## 5. REMOVE the depth==0 cap (walker.py:991)

DELETE the `and depth == 0` condition (991):
```python
# OLD (991):
            if not sub_produced and depth == 0:
                kb_produced = self._expand_via_kb_neighbors(...)
# NEW:
            if not sub_produced:
                kb_produced = self._expand_via_kb_neighbors(...)
```
The cap was a cost band-aid (the D51 18-min blowup, documented 984-990). It is replaced by:
- the premise-forward frontier (§4) which seeds expansion from a bounded premise set rather
  than blind multiplicative fanout, AND
- `_verify_chain`'s transitive-path check, which admits FEWER substitutions to the frontier
  (only confirmed edges survive), collapsing the fanout that the cap was guarding against.
- The existing `_DEFAULT_NEIGHBOR_REVERSE_LIMIT=20` (kb_wikidata.py:201) and the walk loop's
  `max_depth` + budget checks (328, 331-350) remain as the cost bounds.

Update the now-stale comment block (979-990) to describe the bidirectional replacement.

---

## 6. ROUTE subsumption traversal through SubsumptionOracle.consult (contract item f)

Currently `_expand_via_substrate` calls `subsumption.find_neighbors` (951) only — a
local-substrate-only read. Per contract, route through `SubsumptionOracle.consult`
(KB→substrate→LLM). In `_verify_chain`, when confirming an aedos-namespace taxonomy edge
that `verify_transitive_path` could not confirm (unresolved Q-ids), call
`self._substrate.subsumption.consult(EntityRef("aedos", child), EntityRef("aedos", parent),
relation_type)` and admit the edge iff verdict ∈ {a_subsumed_by_b, b_subsumed_by_a,
equivalent} consistent with the substitution direction. This makes the LLM the last-resort
authority for surface-form taxonomy edges the KB cannot reach — the contract's "KB → substrate
→ LLM" ordering. `find_neighbors` is retained ONLY as the cheap DISCOVERY enumerator (which
aedos rows exist), not as the verification authority. The `subsumption_row_id` (subsumption.py
SubsumptionNeighbor.row_id) is still recorded on the trace edge for retractability (WS3 D13).

---

## ORDERING WITHIN WS2
1. §1 verify_transitive_path primitive (kb_protocol + kb_wikidata) — no behavior change, additive.
2. §3a/3c demote gate to ranker (smallest diff; keeps tests green via §3b fallback).
3. §2 discover/verify split (depends on §1).
4. §4 premise-forward (depends on §1, §2).
5. §5 remove depth cap (depends on §4 + §2 to be safe on cost).
6. §6 route through consult (depends on §2's _verify_chain).

## DELETIONS
- walker.py:943-944 — `if not directions: continue` (the distribution GATE) — safe because §3b adds verify-time soundness (the relation is now explored liberally, but _verify_chain rejects unentailed edges, so `neither` predicates like `prefers × is_a` still abstain; test_distribution_gate_blocks_invalid_traversal still passes via the new reason).
- walker.py:957-958 — `if sub.direction not in directions: continue` (direction gate inside neighbor loop) — replaced by ranking (keep all neighbors, sort preferred-first); safe because verify-time check filters unentailed substitutions.
- walker.py:991 — the `and depth == 0` clause on the KB-neighbor fallback — safe because premise-forward (§4) + _verify_chain admission-narrowing replace it as the cost bound; un-capping is required for bidirectional/forward search per contract item (e).
- walker.py:979-990 — the stale D51 depth-cap rationale comment block — replaced by a bidirectional-search rationale comment.
- walker.py:907-997 — `_expand_via_substrate` is RENAMED/REWRITTEN into `_discover_chains`; its body is redistributed into _discover_chains (liberal) + _verify_chain (sound). Net: the single method becomes two, but the gate/cap deletions net-reduce branching LOC.

## ADDITIONS
- kb_protocol.py — `TransitivePathResult` dataclass + `verify_transitive_path` method on KBProtocol — the first-class transitive-path primitive the walker calls for any transitive property.
- kb_wikidata.py — module fn `_build_transitive_ask_query` (extracted from `_build_subsumption_ask_query`); `WikidataAdapter.verify_transitive_path` + `_run_transitive_ask`; fixture-path branch — the live/fixture implementation of the primitive; one new audit event `kb_verify_transitive_path`.
- walker.py — `_discover_chains` (liberal discovery: subsumption neighbors + premise-forward, distribution as ranker) replacing `_expand_via_substrate`.
- walker.py — `_verify_chain` (sound per-edge entailment check via verify_transitive_path → substrate consult → distribution-fallback for intensional is_a; consults WS3 nogood cache).
- walker.py — `_expand_from_premises` (premise-forward frontier seeded from Tier U object-conflict rows, expanding via outgoing KB edges to meet the goal).
- walker.py — new trace-edge metadata key `discovery_source` on subsumption_traversal/kb_neighbor_enumeration edges (observability per contract item 5/WS5).

## CALL SITES / CONSUMERS
- walker.py:378 — `self._expand_via_substrate(node, trace, depth)` call site → MUST change to `self._discover_chains(node, trace, depth)`.
- walker.py:935 — `self._substrate.predicate_distribution.consult(...)` — the ONLY production caller of predicate_distribution.consult (grep-verified); stays, but its result is consumed as a ranking hint not a gate.
- walker.py:951 — `self._substrate.subsumption.find_neighbors(entity_ref, relation_type)` — retained for DISCOVERY; verification now also calls `self._substrate.subsumption.consult(...)` (§6).
- walker.py:1097 — `self._kb.enumerate_neighbors(entity_qid, properties, direction=kb_dir)` — un-capped (§5); still the discovery enumerator.
- kb_wikidata.py:1441 — `_build_subsumption_ask_query` called by `_run_subsumption_ask` — preserved byte-identical via delegation to `_build_transitive_ask_query`.
- kb_verifier.py:468,485 — `_subsumption_upgrades` → `self._kb.subsumption(...)` — UNCHANGED (value-subsumption dual); documented as complementary to walker's verify_transitive_path.
- kb_verifier.py:441,451,461,462 — `_location_disjoint` → `self._kb.subsumption(...)` — UNCHANGED.
- pipeline.py:191-204 — `Walker(...)` construction with `kb=kb` — unchanged; the kb adapter now also exposes verify_transitive_path (KBProtocol grew a method, all adapters/mocks must implement it).
- subsumption.py:95 SubsumptionOracle.consult — becomes a NEW walker consumer (§6); its KB-path branch (100-111) requires wikidata-namespace, so walker passes resolved Q-ids when available else aedos surface forms (substrate/LLM path).

## AFFECTED TESTS
- tests/unit/test_walker_kb_neighbors.py — needs-update: the 8 tests assert the GATE semantics (test_does_not_fire_when_distribution_gate_closed expects enumerate_neighbors NOT called for `neither`). With the gate demoted to ranker, enumeration MAY fire for `neither` but _verify_chain must reject the result → no kb_neighbor_enumeration edge that survives to a verdict. Tests must be rewritten to assert verify-time rejection rather than discovery-time skip. The MagicMock KB must gain a `verify_transitive_path` return.
- tests/integration/test_walker_with_substrate.py::TestWalkerSubsumptionDerivation — will-break unless §3b fallback is correct: test_distribution_gate_blocks_invalid_traversal (268, `prefers × is_a` neither → no_grounding_found) and test_distributes_down_ascends_to_parent (280, mortal kind-entailment) both depend on distribution semantics; must pass via _verify_chain (reject vs admit) not the gate. test_single/multi_hop_distribution_derivation (235,246) seed aedos-namespace subsumption rows → _verify_chain must admit via substrate consult fallback.
- tests/integration/test_walker_failure_modes.py — needs-update: tests like Marie-Curie/Warsaw false-verify guards (depend on the trimmed _SUBSUMPTION_PROPERTIES + bridge) must still hold through verify_transitive_path; un-capping (§5) must not reopen the D51 cost blowup — add a budget/fanout regression assertion.
- tests/unit/test_wikidata_neighbors.py + tests/integration/live/test_wikidata_neighbors_live.py — needs-update: add coverage for verify_transitive_path (fixture + live); ensure _build_transitive_ask_query parity with the old subsumption ASK output.
- tests/unit/test_subsumption_oracle.py (find_neighbors tests 254-295) — will-break-risk-low: find_neighbors unchanged, but add a test that SubsumptionOracle.consult is now reachable from the walker path.
- new-test-needed: test_verify_transitive_path (single-property path for a non-is_a/part_of transitive property, e.g. P171); test_premise_forward_meets_goal (Tier U premise object expands to goal object via outgoing edges); test_depth_cap_removed_does_not_blow_budget (cost regression for §5); test_neither_explored_but_rejected (gate→ranker behavioral proof).
- tests/calibration/test_corpus_runner.py — needs-update: predicate_distribution_corpus runner (407) and derivation corpus exercise the gate; confirm the 0.85 threshold (52) holds under ranker semantics.
- tests/integration/test_chat_endpoint.py / test_chat_wrapper.py / test_end_to_end.py — needs-update: their mock KB/Substrate must implement verify_transitive_path (KBProtocol grew a method); MagicMock-based ones get it free, hand-rolled stubs need the method added.

## ORDERING / DEPENDENCIES
- Depends on WS1 (PredicateBinding / bindings list): _discover_chains/_verify_chain read predicate metadata; once PredicateMetadata.kb_property becomes a bindings list, the transitive-path property selection in _verify_chain/_expand_from_premises must iterate bindings (use binding.kb_property), not the scalar. Build WS2 against the scalar first, then adapt to bindings when WS1 lands (the read-synthesized single-binding shape keeps it working).
- Depends on WS3 (substrate_exceptions nogood cache): _verify_chain consults the nogood cache for entailment-safety (reject a path flagged 'does NOT hold'). Until WS3 lands, _verify_chain treats the nogood lookup as always-empty (additive, fail-open). The nogood read is a hard dependency for the CONTRADICTED-safety story but not for VERIFIED soundness.
- Provides to WS5 (observability): the discovery_source metadata + which bindings/paths were tried are surfaced through the trace; coordinate the trace-edge schema with WS3's provenance term so paths-tried is inspectable.
- Internal ordering: §1 (primitive) → §3 (demote gate) → §2 (discover/verify split) → §4 (premise-forward) → §5 (remove cap) → §6 (route through consult). §3 lands early and small to keep tests green; §5 lands LAST because un-capping is only safe once §2+§4 narrow the fanout.
- KBProtocol method addition (§1) is a breaking interface change for every KB adapter/mock — sequence the mock updates (test fixtures) in the same commit as the protocol change to keep the suite runnable throughout (contract: MUST remain functional).

## RISKS / SOUNDNESS
- §3.2 NEVER-FALSE-VERIFY: demoting the gate to a ranker is the highest-risk change — a `neither` predicate must NOT become verified by an unentailed chain. MITIGATION: soundness moves entirely into _verify_chain. A substitution is admitted ONLY IF (a) verify_transitive_path confirms the KB path, OR (b) a substrate/consult subsumption verdict confirms it, OR (c) for intensional is_a, the distribution oracle (the kind-entailment authority) confirms non-`neither`. If none confirm, the edge is rejected — identical OUTCOME to the old gate for the `neither` case, but reached soundly. This must be proven by test_neither_explored_but_rejected and by re-running the derivation + predicate_distribution corpora at the 0.85 bar.
- FALSE-CONTRADICTION via premise-forward: §4 must NOT introduce contradictions — premise-forward only proposes VERIFICATION candidates (the goal object reachable from a premise object). It never emits a contradiction; contradictions still flow only through _direct_lookup's existing belief-revision paths (462-529). Keep the subject==object/predicate==object pre-lookup filters intact (they live in _build_claim / extraction, untouched by WS2).
- COST REGRESSION (§5 un-cap): removing depth==0 was the D51 18-min blowup guard. RISK: bidirectional search could re-explode if premise-forward seeds are large or _verify_chain admits too many edges. MITIGATION: premise-forward seeds from bounded Tier U object-conflict rows (small); _verify_chain narrows admissions; _DEFAULT_NEIGHBOR_REVERSE_LIMIT=20 + max_depth + wall-clock/llm budget (328-350) remain. Add an explicit per-walk fanout/budget regression test before merging §5.
- Marie-Curie/Warsaw leak (kb_wikidata.py:143-178, 357-365): the generalized verify_transitive_path MUST preserve the trimmed (P131,P30,P17) alternation + type-guarded P361 bridge for part_of. _build_transitive_ask_query must emit byte-identical bridge logic; a regression here reopens the closed false-verify. Pin with a test asserting Warsaw⊄Germany via verify_transitive_path.
- SubsumptionOracle.consult KB-path namespace mismatch (subsumption.py:100-111): consult only takes the KB path when BOTH entities are wikidata-namespace. Walker surface forms are aedos-namespace, so consult would hit the LLM path (124) unless the walker resolves to Q-ids first. RISK: an unintended LLM call per uncached edge (latency + the §3.2 risk of LLM-fabricated subsumption). MITIGATION: in _verify_chain, prefer resolved Q-ids → verify_transitive_path (KB, sound); only fall to consult (which may LLM) when Q-ids unavailable AND a substrate row already exists — bounding LLM exposure.
- Functional-throughout: KBProtocol grows verify_transitive_path; every adapter/mock in the test suite must implement it in the same change or imports/instantiation break. The runtime_checkable Protocol (kb_protocol.py:55) means missing-method failures surface at call time, not construction — add the method to all hand-rolled stubs proactively.
- Two-mechanism reconciliation: _subsumption_upgrades (kb_verifier value-subsumption) and walker verify_transitive_path (slot-substitution) must not double-count or conflict on a single claim. They operate on different axes (value vs slot) and are invoked from different layers (verifier-internal vs walk loop); document the duality to prevent a future merge that collapses them and loses the verifier's value-subsumption VERIFIED path (kb_verifier.py:353-361).


==========================================================================================
