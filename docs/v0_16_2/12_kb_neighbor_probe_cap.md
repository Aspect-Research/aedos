# v0.16.2 — KB-neighbor probe cap (fix budget_wall_clock timeouts)

## Symptom
"Obama born in Kenya" and "Eiffel Tower in London" (both FALSE) → `budget_wall_clock`
timeout with a trace full of `kb_neighbor_enumeration` (P31/P279 is_a, P131/P17/P361
part_of) edges. A timeout, not a fast verdict or abstain.

## General root cause
When the direct KB binding abstains (NO_MATCH), the walker falls into
`_discover_chains → _expand_via_kb_neighbors`. Each enumerated neighbor QID is routed
through `_verify_chain`, which issues **one rate-limited live SPARQL transitive-path
ASK** (~5/s ⇒ ≥200 ms each). The existing per-walk fanout cap (`max_frontier_
expansions`, default 2000) counts **only ADMITTED expansions** — but for a false /
abstaining claim almost every candidate is **rejected**, so that counter stays ~0 and
never trips. The rejected-candidate ASKs — the dominant cost — are bounded by **no
counter**, and the wall clock is sampled only at depth boundaries. They accumulate
across depths/slots/directions until 30 s elapses → `budget_wall_clock`.

(Per-case: "Obama born in Kenya" abstains because Obama's P19 holds two distinct
birthplaces — a hospital + Honolulu — so the multi-distinct-value guard correctly
refuses a functional contradiction; "Eiffel Tower in London" abstains because London
is a city, deliberately excluded from the geo-place gate that drives the disjoint
contradiction (to prevent a known false-contradict). Both then fan out and time out.
The investigators advised **not** to force a contradict in either — that's the
false-contradict direction. The general fix just makes them abstain *fast*.)

## Fix
`WalkerBudget.max_kb_neighbor_probes` (default **48**) — a hard per-walk cap on the
number of distinct neighbor candidates **probed** (counted whether admitted OR
rejected, with per-walk QID dedupe via a `seen` set, since the same famous container
— Q30, Q142 — recurs across slots/directions/depths). On exhaustion the walk returns
a **fast** `no_grounding_found` with a distinct, deterministic
`abstention_reason="budget_kb_neighbor_probes"` instead of timing out. Threaded
`walk() → _discover_chains → _expand_via_kb_neighbors`; guarded so a verdict found
earlier in the frontier is preferred over the probe abstain.

## Why it's sound (verdict-preserving)
The cap lives only in the discovery fallback and can only **remove** candidate
exploration — it never fabricates a verdict; every candidate still passes
`_verify_chain`'s §3.2 gate before it could become one. So it only ever converts a
(already-timing-out) abstain into a **fast** abstain — the safe direction. A genuine
grounding's true container/kind chain is reached in a few confirmed hops, far under
48; and at ≤5 ASK/s a path needing >48 distinct rejected probes could not have
completed under the wall clock anyway, so nothing that would have grounded *faster
than the timeout* is removed. The admitted-only fanout cap remains as a backstop for
the substrate / premise-forward arms (which the probe counter does not gate).

## Tests / verification
- `tests/unit/test_walker_ws2_budget.py`: the blowup-KB pin now abstains via
  `budget_kb_neighbor_probes` (tighter than the fanout cap); a new dedupe pin
  (repeated neighbor QIDs don't burn the budget, no wall-clock timeout); the
  `enumerate_neighbors` call-count bound holds.
- Full offline gated suite: **1729 passed**, 1 xfail, 1 xpass (pre-existing sandbox
  boundaries).

## Marie Curie "nationality Polish" — investigated, left as a (now-fast) abstain (operator decision)
Separately investigated: "Marie Curie nationality French" verifies (France ∈ her P27
citizenship) but "Polish" abstains. Root: her P27 holds the **historical** Second
Polish Republic (Q207272), not modern Poland (Q36), and the bridge that would connect
them was **deliberately removed** — the code names *"the Marie-Curie-class
false-verify"*. Her Polish identity is really her **ethnic group** P172 = Poles
(Q1026), which nationality doesn't consult — and "Polish" doesn't even resolve to
Poles (Q1026's label is "Poles"; no bare-"Polish" alias), unlike "French" which
resolves to a "French" ethnic-group entity. The only **sound** fix is an ethnic-group
(P172) reading with `demonym → country → ethnic-group` resolution. **Operator chose
to leave it as a sound (now-fast) abstain** rather than add an ethnonational reading
in the guarded false-verify area. No code change for this case.
