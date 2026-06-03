# v0.16.2 — Walker fix: direct-binding-first (kill the P17 fanout)

## Symptom
A live chat claim `Pope Francis holds_role Pope` returned `no_grounding_found`
with `abstain: budget_wall_clock`, and the trace showed **18 steps, all
`kb_neighbor_enumeration via property=P17`** (P17 = country), `Sources {kb: 0}`.
The walker spent its entire wall-clock budget fanning out over geographic
`part_of` (P17) neighbor edges and never grounded — a `budget_wall_clock` abstain.

## Root cause
`Walker._discover_chains` runs, for each `relation_type in ("is_a", "part_of")`,
a substrate `find_neighbors` arm and — when that produces nothing — an UNBOUNDED
KB-neighbor enumeration fallback (`_expand_via_kb_neighbors`) over live Wikidata.
This enumeration fired for **every** predicate, regardless of whether the
predicate can be grounded through that relation. For `holds_role` (which does not
distribute over `part_of`), it enumerated P17 country neighbors of the resolved
entities, created candidate substitution claims, and explored them depth after
depth until the budget was exhausted.

This was also a **latent §3.2 false-verify surface**: `_verify_chain` gates `is_a`
substitution on the distribution verdict (a `neither` predicate is rejected) but
treats `part_of` distribution as "a pure ranker" — admitting a `part_of`
substitution on the structural edge alone. A non-distributing predicate riding a
`part_of` edge to a grounded substitution would unsoundly verify the original.
(The newer `premise_forward` arm already recognized this and fails closed:
"a non-distributing place predicate must not ride a part_of edge to a false.")

## Fix (direct-binding-first)
Gate the KB-neighbor enumeration fallback on the predicate actually distributing
through the relation — reusing the distribution verdict already computed for the
ranker (no extra oracle call):

```python
if not sub_produced:
    if verdict_label != "neither":
        kb_produced = self._expand_via_kb_neighbors(node, relation_type, preferred, dist.verdict, trace)
        expanded.extend(kb_produced)
```

A confident `neither` forecloses every substitution through that relation, so the
enumeration is skipped and the claim's **own direct predicate binding** (e.g.
`holds_role → P39`) stays the grounding path. The walk ends at a FAST
`no_grounding_found` (or grounds directly) instead of chasing irrelevant neighbors
to a `budget_wall_clock` timeout.

## Why it's sound (verdict-preserving / soundness-improving)
- **is_a + `neither`**: `_verify_chain`'s kind-entailment gate already REJECTS
  every such candidate, so enumerating them is pure waste — skipping changes no
  verdict (provably verdict-preserving).
- **part_of + `neither`**: a `part_of` substitution is unsound unless the predicate
  distributes, so skipping removes only never-grounding work **and closes the
  latent false-verify surface** above. Aligned with the existing `premise_forward`
  fail-closed gate.
- **Fail OPEN**: only a *confident* `neither` skips, so a wrong distribution
  verdict can never cause a false-abstain (preserves the WS2 §3 "discover liberally"
  intent for every non-`neither` predicate).
- Scope: only the unbounded KB-enumeration fallback is gated; the cheap
  operator-seeded substrate `find_neighbors` arm is untouched.

In the failing `neither` scenario the pre-fix walker ALSO ended in abstain (via
verify-time rejection for is_a, or a budget timeout for part_of) — the fix yields
the same verdict, but fast and without the timeout, while removing a latent
false-verify.

## Tests / verification
- `tests/unit/test_walker_kb_neighbors.py::TestD5Fallback::test_neither_distribution_skips_kb_enumeration`
  — `neither` ⇒ `enumerate_neighbors` not called, fast `no_grounding_found`.
- Positive controls unchanged: `distributes_down/up/both` still enumerate
  (`test_fires_*`, `test_entailed_is_a_candidate_admitted`).
- Full offline gated suite: **1721 passed**, 1 xfail, 1 xpass (pre-existing
  sandbox boundaries).

## Note (orthogonal to E1–E4)
The E1–E4 fixes correct the VERDICT once grounding is found; this fixes *getting
to* the grounding. It does not depend on the date/temporal-currency work.
