# Aedos v0.16 — Review Round 1: Findings & Resolutions

This document records the in-depth review of the v0.16 implementation (the full diff
`git diff 82668d6..HEAD` — foundation + WS1–WS6) and the disposition of every
adversarially-confirmed finding. The review ran 8 audit dimensions; each finding was
re-checked against the actual code by an independent verifier before being accepted, so
the 25 items below are confirmed-real (over-claims and already-mitigated items were
dropped at verification).

**Posture:** the paramount invariant is §3.2 — never false-verify, never false-contradict;
abstention is always the safe outcome. Every fix here either *tightens* soundness or is
a fail-open-safe robustness/observability/cleanup change. No change loosens a verdict gate.

Resolutions landed in two patch commits on `v0.16`:
- **PATCH-A** (`1567b5c`) — Tier-1 soundness/correctness + cheap cleanups.
- **PATCH-B** (`b8bc1f5`) — robustness/observability + dormant-mechanism documentation + calibration corpus.

---

## Tier 1 — soundness / correctness (FIXED in PATCH-A)

| Finding | Dimension | Fix |
|---|---|---|
| `ws2-kbneighbor-bypasses-verifychain` (**HIGH**) | WS2 | `_expand_via_kb_neighbors` emitted KB-enumerated `is_a`/`part_of` substitutions straight to the frontier without the `_verify_chain` entailment gate (the `find_neighbors` arm gated; this arm did not). Now every KB-enum candidate is routed through `_verify_chain` with the same argument shape and skipped on `False`. Restores SS3 symmetry — an unentailed downward substitution can no longer ride through to a false verify. |
| `ws2-verifychain-kbnegative-fallthrough` (MED) | WS2 | A *definite* KB transitive-negative (`tp` non-None, no `error`, `holds=False`) fell through to the substrate/LLM consult, which could fabricate a Priority-3 positive. Now `_verify_chain` returns `bool(tp.holds)` on any definite KB answer; it falls through to consult only when Q-ids were unavailable (`tp is None`) or `tp.error` is set. Positive path and the byte-identical part_of bridge preserved. |
| `ws2-premiseforward-no-distribution-check` (MED) | WS2 | `_expand_from_premises` admitted a premise-forward substitution over a `part_of` edge with no check that the *predicate* distributes upward. Now it consults `predicate_distribution(predicate, polarity, 'part_of')` and admits only `distributes_up`/`both`; it fails closed (abstains) on `neither`, down-only, absent, or error — a non-distributing place predicate can no longer ride a part_of edge to a false. |
| `ws1-ontology-singlevalue-overrides-oracle` (soundness) | WS1 | The ontology binding OR-promoted `single_valued` past the oracle's conservative `0` (`single_valued` is the only flag that drives CONTRADICTED). The oracle is now authoritative for `single_valued`; the ontology supplies only types/constraints. Closes a false-contradiction surface. |
| `ws3-subsumption-footprint-dropped` (correctness) | WS3 | The `subsumption_traversal` edge stamped `subsumption_row_id` in edge metadata but never recorded it as a provenance premise, so the dep was dropped from the provenance term (and the retraction footprint, since `_extract_source_rows` short-circuits on `provenance.source_rows()`). Now `_record_premise(source='subsumption', table='subsumption', row_id=…)` fires after the edge — the term is the single source of truth. |

Each is pinned by a focused regression test with a positive control (so the gate is proven
selective, not a blanket suppression). The part_of bridge, Williamstown→Massachusetts
geo-containment, and distribution-gated traversal tests remain green.

---

## Tier 2 — robustness / observability (FIXED in PATCH-B)

| Finding | Dimension | Fix |
|---|---|---|
| `ws5-chat-leaks-internal-rowids` (observability) | WS5 | The public POST `/chat` body returned the full `trace_to_json` (edge metadata carrying `tier_u_row_id`/`entity_resolution_cache_row_id`/`subsumption_row_id`) and the row-id-bearing provenance term. `claim_observability` gained a `verbose` flag: `/chat` now returns a lightweight per-claim view (verdict, base_verdict, conditional, abstention_reason, contradicting_value, `trace_human`) with no internal DB identifiers; the audit endpoint GET `/verification/{id}` keeps the full verbose detail. Honors the operator's "rich observability" on the audit surface while not leaking internal ids publicly. |
| `ws6-tieru-endpoint-first-row` (robustness) | WS6 | `_tier_u_endpoint` took `rows[0]` with no conflict guard while the KB path abstained on conflicting starts. Now it abstains (returns `None`) when more than one distinct Tier-U endpoint date is present — symmetric with `_interval_from_statements`. |
| `ws6-org-match-deadpath` (robustness) | WS6 | The org-narrowing branch read `getattr(claim, 'object_org', None)` but `Claim` has no such field, so it was always dead. Removed, with a comment that endpoint claims ground against the unique-or-agreeing-start interval (they never carry the org). Defense-in-depth: the contradiction branch may fire only from a uniquely-identified statement, so a future `single_valued` endpoint binding cannot false-contradict (all 8 endpoint seeds are `single_valued=0` today). |
| `ws3-lru-no-reason-guard` / `ws3-leak-guard-eviction-exemption-missing` (robustness) | WS3 | `_evict_if_over_cap` had no reason exemption. Added `AND reason NOT IN ('leak_guard','operator_marked')` to the COUNT and DELETE so reserved guard rows can never be evicted (contract §0.11 #2). Harmless now (no such rows exist), defensive for later. |
| `unused-imports-v016-deletions` (cleanup) | x-cut | Removed unused `field`/`Optional` (retraction.py) and `Optional` (sling_fallback.py) left by the v0.16 rewrites. |

---

## Tier 3 — dormant mechanisms (DEFERRED, documented in code; operator decision)

These are spec-named capabilities that are wired but **inert** today. Each is **fail-open
safe** (it can only reduce coverage, never produce a false verdict). The disciplined choice
for v0.16 was to **document them as deferred rather than activate an un-reviewed verdict
path right before sign-off**. Each carries a code comment labelling it dormant.

| Finding | Mechanism | Why deferred (not activated, not deleted) |
|---|---|---|
| `ws1-sling-dead-in-prod`, `sling-unreachable-orphan` | SLING distant-supervision fallback (spec Decision 1.e) | `propose_bindings` returns `[]` because the oracle tool schema emits no `sample_subject_qids`/`example_qids`. Activating it (adding the schema field + prompt) introduces a **new verification-driving binding source** that this review did not audit. Deferred to a future round so it can be reviewed before it can drive a verdict. Fails open today (proposes nothing). |
| `ws1-nogood-gate-no-writer-keymismatch`, `ws3-vetoes-inert-no-writer` | Binding-loop NOGOOD gate (`_binding_vetoed`/`vetoes`) | No production path writes a `subsumption`-kind nogood (the only `record_nogood` writer is `verify_transitive_path`, kind `transitive_path`), and the reader keyed on the NL subject string vs the writers' resolved Q-id. The operator explicitly forbade hand-seeded guards. The gate fails open (never suppresses a sound verdict). Eager NOGOOD-for-bindings writing — keyed on the resolved subject Q-id — is deferred; activating a veto that *suppresses contradictions* is the dangerous direction and warrants its own review. |
| `ws3-oracle-exception-cache-dead` | `SubsumptionOracle._exception_cache` | Stored but unread; reserved for the deferred symmetric-subsumption nogood routing (spec 03 §405, route `_live_subsumption`'s ASKs through `verify_transitive_path(..., exception_cache=…)`). Documented as reserved. |
| `ws6-holdsat-deadcode` | `_interval_holds_at` (three-valued holds-at-T) | Covered by unit tests but not yet consumed by a verdict path. The holds-at-T base-relation-scope consumer is deferred; endpoint grounding uses `_verify_interval_endpoint`'s year-aware equality compare. Kept as a tested forward-looking primitive (the §3.2-lower-risk choice). |
| `ws6-statusstartend-kb-noop` | `status_started`→P571 / `status_ended`→P576 seeds | The KB arm is intentionally inert for inception/dissolution: a P571/P576 date is the statement *value*, not a P580/P582 qualifier, so the interval start/end stays unknown and the endpoint abstains. Grounding for org subjects flows through Tier U or the `founded_in_year`/`dissolved_in_year` date-in-object predicates. Documented; rows kept additive. |

---

## Spec-conformance items (documentation / process — no code-soundness impact)

| Finding | Disposition |
|---|---|
| `d5-persona-deferral-undocumented` | The persona-subject SQL guard (`_is_persona_subject` + the direct Tier-U SELECT, the SS2 conservative guard) is **KEPT**. The spec scheduled D5 for deletion *once WS4 routes persona claims to `user_authoritative`*; WS4 did not implement that routing, so deleting the guard would be unsound. Deferred, gated on that routing landing. (This note is the record.) |
| `d9-contradiction-tracer-not-deleted` | `contradiction_tracer.py` is **superseded but retained**. The lazy replacement (`RetractionPropagator.propagate_retraction`) is live and wired (tier_u.py, consistency.py); `ContradictionTracer` has no production importer. Retained pending an operator call on whether `trace_contradiction` becomes a future operator-correction surface; if not, it (and its two test importers) can be deleted. Recorded here rather than silently deleted. |
| `ws8-loc-checkpoint-not-flagged` | Post-WS2 LOC checkpoint (contract §0.11 #1): measured src net **+3352 lines** (added 3705 / deleted 353); `kb_verifier.py` 669→910. The `verify()` rewrite is a genuine **replace** (a real `for binding in meta.bindings` loop, not a layer over the old scalar special-cases) — so §0.7 is satisfied. Net is *up* because the soundness-sensitive deletions (D2 geo cluster, D3, D5 persona) were **deferred** — only the non-soundness-sensitive deletions (`_CANONICAL_MAP`, 21 synonym seeds, content-less-event drop) landed. Per the operator's standing guidance, **LOC is not a target; simplicity is** — the messy special-case ladders were replaced by principled mechanisms (binding loop, discover/verify split, lazy provenance); the geo cluster remains only because its discovered replacement could not yet be proven to keep the Warsaw/Rome/Thames/Vatican/Massachusetts pins green (soundness over purity). Surfaced here for the operator. |
| `ws1-metadata-property-vs-postinit` | The contract named read-only `@property` accessors + a `from_scalars` classmethod; the implementation instead uses mutable scalar fields with a `__post_init__` mirror (synthesize one `legacy_scalar` binding when `bindings` is empty, else overwrite scalars from `bindings[0]`). Chosen to avoid churning ~30 scalar constructor sites; `bindings[0]` is the source of truth post-construction. Behaviorally equivalent for single-property rows (byte-identical legacy path, verified by tests). Documented as a deliberate substitution. |
| `ws4-hardclaim-corpus-not-updated` | doc 07 §180 mandated rewriting the hard_claim corpus rows' `expected_subjects_not_in_output` → `expected_abstention_reason`. Done in PATCH-B (corpus rows + runner branch). `tests/calibration` is outside the gated unit/integration command and may need API to run end-to-end; the edit is deterministic and parse-checked. |
| `d10-retraction-still-full-index-scan` | `propagate_retraction` still scans the full in-memory trace index (bounded, cheap). The spec's real intent — staleness scoped strictly to `*_given_assertion`, base verdicts recorded-not-staled (SS5, §0.11 #3) — **is** honored. The full-index scan is retained intentionally; a targeted reverse-index is an optional, behavior-neutral optimization, not a correctness item. |

---

---

## Review Round 2 — patch correctness & follow-ups (FIXED in PATCH-C `385f6e3`)

A second review round audited the two patch commits for correctness, *over-tightening*
(a gate that now abstains on a claim that should soundly verify), a fresh §3.2 sweep, and
completeness. Three of four dimensions came back **clean** — the fresh soundness sweep
confirmed both patches *tighten only*: no terminal verdict path was loosened and no new
false-verify/contradict was introduced. Three confirmed follow-ups, all over-tightening /
test-gap (none a soundness hole):

| Finding | Severity | Resolution |
|---|---|---|
| `r2pa-01` — definite KB transitive-negative discarded a *sound substrate row*, not just the LLM fabrication | MED (over-tightened) | PATCH-A's "KB-negative ⇒ `return False`" blocked the LLM Priority-3 fabrication (correct) but also discarded the Priority-2 substrate row (operator-seeded / discovered subsumption — e.g. a seeded *Williamstown part_of Massachusetts* where Wikidata's part_of closure is incomplete), contradicting the operator's trust order (**substrate seeded/discovered > KB > LLM**). Fixed precisely: `SubsumptionOracle.consult` gained `allow_llm: bool = True`; on a definite KB negative `_verify_chain` now falls through to the substrate consult with `allow_llm=False`, so a real substrate row still confirms the step but a cold LLM positive is never admitted over a KB negative. KB-positive still short-circuits; KB-unavailable still does the full consult. |
| `r2c-1` — `_tier_u_endpoint` intervals not marked `unique`, so the contradiction gate over-abstained on Tier-U-only endpoints | LOW (over-tightened) | `_tier_u_endpoint` now sets `unique=True` (the single-distinct-date invariant guarantees it), and `_gather_interval` propagates `unique` from a pure Tier-U interval (KB stays authoritative when present). Forward-defensive only — all 8 endpoint seeds are `single_valued=0` today, so the contradiction branch is unreachable now. |
| `r2c-2` — no test proved the leak-guard LRU exemption | LOW (test-gap) | Added `test_leak_guard_row_exempt_from_eviction`: an oldest-timestamp `leak_guard` row survives eviction and is excluded from the cap count. |

The round-1 fix `ws2-verifychain-kbnegative-fallthrough` row above is **superseded** by `r2pa-01`:
the short-circuit is now LLM-excluded-consult rather than an outright `return False`.

---

## What was NOT changed (and why)

- The geo hardcode cluster (`CONTINENT_QIDS`, `_location_disjoint`, `_GEO_REGION_TYPES`) and
  the persona-subject SQL guard remain. Both are conservative soundness guards whose
  discovered replacements were not yet provable against the regression pins. Deleting them
  is deferred, not abandoned (D2/D3/D5).
- The two `tests/unit/test_sandbox.py` boundaries (1 xfail, 1 xpass) are pre-existing v0.15
  sandbox-escape limits, unrelated to v0.16. Untouched.
- The live cold-start Marie-Curie / continent correctness pins (`tests/cold_start`,
  `TestZeroSeedLive`) are env-gated behind `RUN_LIVE_TESTS=1 RUN_LIVE_KB=1` (API + live KB);
  their offline analogues (part_of bridge, geo-containment, distribution gating) are covered
  in the integration suite.
