# Aedos v0.15 — Phase 10.5 Step 6 medium-bar evaluation

**Status:** Run 1 measured pre-fix; post-fix Run 2 pending (full 122 cases
queued). Documents the headline numbers, the diagnosis of v0.15's behavior
under realistic conditions, and the five surgical fixes that root-caused
the soundness signals.

## What was measured

The medium-bar evaluation is the headline release artifact for v0.15. It
tests Aedos's value proposition directly: does the verify-or-abstain
architecture produce better outcomes than passing an LLM's response through
unverified, on a 122-case curated set spanning six failure modes?

**Test set:** `tests/evaluation/medium_bar_test_set.jsonl` — 122
single-statement cases. Each case has a `statement`, a `ground_truth` verdict
(`verified` / `contradicted` / `abstain`), a `failure_mode` label, and a
`notes` field. Distribution:

| Failure mode | Cases |
|---|---:|
| predicate_translation | 28 |
| entity_disambiguation | 23 |
| cross_source_unification | 21 |
| multi_hop_distribution | 20 |
| principled_abstention | 20 |
| belief_revision | 10 |

Ground-truth distribution: verified 84 (69%), abstain 24 (20%),
contradicted 14 (11%).

**Two runners:**

- **Aedos** — `tests/evaluation/benchmark.py:AedosRunner`. Runs the full
  v0.15 pipeline: extractor → claim triage → walker → aggregator.
  Multi-claim statements aggregate via "any contradicted ⇒ contradicted,
  all verified ⇒ verified, else no_grounding_found".
- **LLM-only baseline** — `BaselineRunner`. One LLM call per statement
  with the prompt "Evaluate whether the following statement is factually
  correct, incorrect, or uncertain. Respond with exactly one of: VERIFIED,
  CONTRADICTED, or ABSTAIN." No architectural support.

**Substrate state at start of Run 1:** `aedos_phase10_5.db` — 83
`predicate_translation` rows (seeded via `seeds/load_seeds.py` and Phase
10.5 additions) + 3 Tier U rows for the "Asa" persona. By end of Run 1
the substrate had grown to 142 rows (LLM-oracle runtime additions) and
17 rows had been **retracted** by the consistency check — see *Finding 4*.

## Scoring model

Per the runbook (Step 6) and `benchmark.py`'s `compute_metrics` + the four
acceptance thresholds operationalized in `generate_report`:

**Primary headline metrics**

- `accuracy` — predicted == ground_truth, across all 122 cases.
- `false_verified_rate` — predicted = `verified` ∧ ground_truth ≠
  `verified`. Denominator: total cases. **The soundness metric** — the
  medium-bar analog of §3.2 false-verifieds.
- `false_abstain_rate` — predicted = `abstain` ∧ ground_truth =
  `verified`. Denominator: verified-ground-truth cases. The cost of
  over-caution.

**Acceptance thresholds (release gates):**

1. Aedos false-verified rate ≤ 5%
2. Aedos accuracy ≥ baseline + 15pp
3. Aedos accuracy ≥ baseline on every failure mode (no regression)
4. Aedos accuracy ≥ baseline + 20pp on ≥ 4 of 6 failure modes

**Derived metrics surfaced in this document** (not in the harness's
report, extracted from per-case JSON by
`scripts/medium_bar_aggregate.py` for the session-prompt analysis
framing):

- `fp_correction` — predicted = `contradicted` ∧ ground_truth =
  `verified`. The session prompt's "most harmful outcome" —
  equivalent to telling a user a true statement is false. Soundness
  expectation: zero or near-zero. Any non-zero count is investigated.

## How runbook scoring differs from the session-prompt framing

The session-prompt framing described a richer "draft + per-claim
intervention overlay" model. The actually-implemented harness is per-
statement verdict comparison; this document follows the runbook (per the
session prompt's "If the runbook's scoring differs from this framing,
follow the runbook" instruction) and extracts the derived metrics
alongside.

## Pre-run harness fixes (single-point findings)

Two issues were surfaced and fixed during smoke testing before any full
live run. Both are infrastructure fixes that don't change Aedos's
behavior — they unblock honest measurement.

1. **`_anthropic_chat` rejected empty system prompts.** The client
   unconditionally wrapped the system prompt in a `cache_control` text
   block. Anthropic's API rejects empty text blocks. Only
   `BaselineRunner` passes `system=""`, so the baseline returned
   `verdict="error"` on every case (~100ms latency).
   `_normalize_verdict` maps `error → abstain`, which made the bug
   invisible at the metric level. Fix at
   `src/aedos/llm/client.py:_anthropic_chat` — omit the `system` kwarg
   entirely when no system prompt is provided.

2. **`scripts/medium_bar_run.py` picked up `aedos.db` from `.env`
   instead of `aedos_phase10_5.db`.** The project's chat-deployment
   `.env` sets `AEDOS_DB_PATH=aedos.db` for the chat wrapper. The
   medium-bar runs against the Phase 10.5 seeded substrate. Fix —
   explicit `--db-path` argument with the correct default, overriding
   the env at runtime.

## Run 1 — pre-fix headline (1.8h, exit 0)

### Across-the-board metrics

| Metric | Aedos | Baseline | Delta |
|---|---:|---:|---:|
| **Accuracy** | **27.9%** (34/122) | 78.7% (96/122) | **−50.8pp** |
| False-verified rate | 1.6% (2) | 9.8% (12) | better |
| **False-positive correction** | **5** | 2 | worse — soundness signal |
| Median Aedos latency | 13.3s | ~1s | — |

### Per-failure-mode breakdown

| Mode | Aedos | Baseline | Δ |
|---|---:|---:|---:|
| principled_abstention | 90.0% (18/20) | 50.0% (10/20) | **+40.0pp** |
| belief_revision | 30.0% (3/10) | 20.0% (2/10) | +10.0pp |
| cross_source_unification | 23.8% (5/21) | 76.2% (16/21) | −52.4pp |
| predicate_translation | 17.9% (5/28) | 96.4% (27/28) | −78.6pp |
| entity_disambiguation | 13.0% (3/23) | 95.7% (22/23) | −82.6pp |
| multi_hop_distribution | **0.0%** (0/20) | 95.0% (19/20) | **−95.0pp** |

### Aedos verdict shape

84% of cases got `abstain` (`no_grounding_found`). Of the 84 verified-
ground-truth cases, Aedos abstained on 70 (83% false-abstain rate on
verified cases).

### Acceptance gates (Run 1)

1. FV ≤ 5%: **PASS** (1.6%)
2. Accuracy ≥ baseline + 15pp: **FAIL** (−50.8pp)
3. No-regression per mode: **FAIL** (4 of 6 modes regressed)
4. ≥4 of 6 modes with ≥+20pp: **FAIL** (1/6 modes — only
   `principled_abstention`)

## Diagnosis — the 5 false-positive corrections

All five fp_correction cases trace to one of two upstream mechanisms.

| Case | Statement | Mode | Mechanism |
|---|---|---|---|
| csu_009 | "Obama was born in Hawaii, and Hawaii was the 50th state admitted to the US." | cross_source_unification | walker subsumption gap (Honolulu vs Hawaii) + 2nd clause abstains → compound |
| csu_010 | "Albert Einstein was born in Germany and died in the United States." | cross_source_unification | walker subsumption gap (Ulm vs Germany; Princeton vs US) |
| bonus_001 | "Obama was born in the United States." | entity_disambiguation | walker subsumption gap (Honolulu vs US) |
| bonus_004 | "Albert Einstein was born in 1879." | predicate_translation | extractor produced `(Einstein, born_in, Einstein)` — year dropped, object copied from subject |
| bonus_008 | "Shakespeare was born in England." | entity_disambiguation | walker subsumption gap (Stratford vs England) |

### Finding 1 — Walker subsumption gap (csu_010, bonus_001, bonus_008; partial csu_009)

For functional predicates with entity-typed values,
`kb_verifier.py:_compare_positive` returned `CONTRADICTED` whenever the KB
statement value (e.g. `Honolulu`) didn't string-equal the claim's expected
value (e.g. `United States`), even when both resolved to KB entities and
the KB value was a specialization of the expected. No subsumption-upgrade
path existed — the walker compared Q-IDs literally.

### Finding 2 — Extractor self-referential parse (bonus_004)

For "Einstein was born in 1879", the extractor reproducibly produced
`(Albert Einstein, born_in, Albert Einstein)` — the year token was dropped
and the subject string was duplicated into the object slot. The seeded
year-predicates (`born_in_year` / `date_of_birth` / `born_on`, all P569)
remained unused (`used_count=0` in Run 1). The walker then queried
Wikidata for Einstein's birthplace (P19 → Ulm), found Ulm ≠ "Albert
Einstein", and returned `CONTRADICTED`.

## Diagnosis — the 2 false-verified cases

| Case | Statement | Mechanism |
|---|---|---|
| pa_003 | "The current stock price of Apple is $150." | substrate-state dependent — abstains correctly in post-fix substrate |
| pa_020 | "The number of grains of sand on all Earth's beaches exceeds 7 quintillion." | Python verifier over-eagerness — `verify()` had no `None` return path, LLM committed to `True` |

### Finding 3 — Python verifier no-abstention path (pa_020)

`PYTHON_VERIFY_TOOL`'s schema declared `def verify(...) -> bool` —
binary, no abstain. The verifier harness checked truthy/falsy, so a
genuinely uncertain LLM had to commit to `True` or `False`. For
speculative numerical claims (e.g. grains of sand), the LLM
generated code that returned `True`, producing a false-verified.

## Diagnosis — substrate destabilization during Run 1

### Finding 4 — Consistency check transitive_equivalence_violation cascade

During Run 1, **17 seeded `predicate_translation` rows were retracted by
the consistency check** between 09:37 and 10:18 UTC. The chain:

- The predicate translation oracle generated runtime entries for novel
  vocabulary the extractor produced (e.g. `held`, `held_position`,
  `the_tallest_mountain_on_earth`). Some of these were **malformed**:
  `kb_property` was set but `slot_to_qualifier` was `NULL`.
- `consistency.py:_check_predicate_translation_row` compared the
  malformed sq=NULL row against properly-formed peers on the same KB
  property. The string-inequality check fired
  (`transitive_equivalence_violation`).
- `resolve_conflict` retracted **both** rows — the malformed one and
  its well-formed seeded peer.
- One bad row (e.g. `held`, sq=NULL, P39) cascade-retracted all P39
  variants over multiple write events (`holds_role`, `held_position`,
  `occupied_position` — all properly seeded).

By end of Run 1, the retraction list included: `located_at`,
`capital_of`, `has_capital`, `graduated_from`, `admitted_to`,
`works_at`, `headquarters_in`, `head_of_government`, `holds_role`,
`held_position`, `occupied_position`, `held`, `adjacent_to`,
`shares_border_with`, `shares_a_border_with`, `member_of`,
`publisher`. The retracted predicates are exactly those needed by the
multi_hop, entity_disambiguation, and predicate_translation cases.
This destabilization compounded the abstention rate.

## The five surgical fixes

The four sub-causes (subsumption gap, extractor self-reference,
Python no-abstain, substrate destabilization) decomposed into five
targeted edits. None changes Aedos's architectural invariants; each
either closes a documented behavior gap or correctly handles a
malformed input.

### Fix 1 — `consistency.py`: skip sq=NULL conflicts

`_check_predicate_translation_row` now skips
`transitive_equivalence_violation` when either side's
`slot_to_qualifier` is NULL. A NULL sq on a kb-mapped predicate is
a malformed runtime entry; it can't be used for KB lookups anyway,
so letting it persist (rather than poisoning its well-formed peers)
preserves the seed pack's integrity.

### Fix 2 — `kb_verifier.py`: subsumption upgrade on KB mismatch

Before declaring `CONTRADICTED` (functional) or `NO_MATCH`
(non-functional) on a scope-compatible value mismatch with resolved
entity-typed values, the verifier now queries
`kb.subsumption(stmt.value, expected_value, …)` for both `part_of`
and `is_a` relation types. If either returns `a_subsumed_by_b` or
`equivalent`, the verdict upgrades to `VERIFIED`. Fails closed —
unknown relation types, invalid Q-IDs, KB outages all preserve the
prior verdict; never promotes on uncertainty.

### Fix 3 — `python_verifier.py`: explicit `None` for uncertain

`PYTHON_VERIFY_TOOL`'s schema and `_SYSTEM_PROMPT` now declare
`def verify(...) -> Optional[bool]` with explicit guidance to
return `None` on speculative / uncertain claims. The sandbox
harness now distinguishes `None` (→ `no_terminal_result` →
abstain) from truthy / falsy non-None (preserved as
verified / contradicted). The system prompt cites §3.2 soundness
invariant: "prefer None over a guessed True/False."

### Fix 4 — `extractor.py`: reject self-referential triples

`_build_claim` now drops claims where `subject` equals `object`
(case-insensitive, trimmed). The walker can no longer contradict
a true statement via a malformed (X, P, X) triple.

### Fix 5 — `client.py`: omit empty system block

`_anthropic_chat` omits the `system` kwarg entirely when no
system prompt is provided, avoiding Anthropic's
`cache_control on empty text block` rejection. Unblocks the
baseline runner (and any future caller that passes
`system=""`).

## Post-fix retest (11 cases, 15 minutes)

After applying fixes 1-2 + restoring the 17 retracted predicates
(in-place; circuit breaker cleared), an 11-case retest covering
the 5 fp_correction cases + 6 abstention samples:

| Case | GT | Run 1 (pre-fix) | Retest (post-fix) | Change |
|---|---|---|---|---|
| csu_010 | verified | contradicted | **verified** | ✓ fixed (subsumption upgrade) |
| bonus_001 | verified | contradicted | **verified** | ✓ fixed (subsumption upgrade) |
| bonus_008 | verified | contradicted | **verified** | ✓ fixed (subsumption upgrade) |
| csu_009 | verified | contradicted | abstain | partial — first clause verifies; "Hawaii was the 50th state" still abstains |
| bonus_004 | verified | contradicted | contradicted → abstain (after Fix 4) | ✓ no longer fp_correction |

After all five fixes applied (including Fix 3 Python None and Fix 4
self-reference filter), atomic claim spot-checks confirm:

- `(Paris, located_in, France)` → **verified** via Île-de-France ⊂ France
- `(Obama, born_in, Hawaii)` → **verified** via Honolulu ⊂ Hawaii
- `(Einstein, born_in, Germany)` → **verified** via Ulm ⊂ Germany
- `(Albert Einstein, born_in, Albert Einstein)` → **filtered at extraction**
- pa_020 `exceeds` → **abstain** (Python None path)
- pa_003 `stock_price` → **abstain** (substrate-state stable now)

**All 5 fp_corrections + 2 false-verifieds from Run 1 are eliminated.**

## Remaining sub-causes (post-fix; not addressed in this session)

The Aedos-vs-baseline accuracy gap will narrow with the fixes but
not close, because four architectural sub-causes persist:

### Sub-cause B — Multi-hop derivation depth budget

`(France, located_in, Europe)`, `(Eiffel Tower, located_in, Europe)`,
and similar chains exhaust the walker's derivation depth budget.
Wikidata doesn't have direct P131 statements for country → continent;
chained subsumption through intermediates is required. This is the
calibration_results.md's documented 54% derivation ceiling.

### Sub-cause C — Extractor vocabulary doesn't favor seeded predicates

The extractor naturally produces phrasal variants — `held_the_office_of`,
`received_award`, `the_prime_minister_of`, `employed_by` — instead of
the seeded `holds_role`, `awarded`, `head_of_government`, `works_at`.
The predicate translation oracle then generates new mappings (many of
which were the malformed sq=NULL rows that triggered Finding 4 before
Fix 1). Post-fix, the malformed rows persist harmlessly but still
can't be used for KB lookups.

### Sub-cause D — Generic `was` / `is` / `has` predicates

Identity / role / possession claims like "Lincoln was the 16th
President", "The Amazon is the world's largest river", "Hawaii was
the 50th state" — the extractor uses `was` / `is` as the predicate.
These have routing_hint=abstain by design. Without claim
decomposition (a v0.16 architectural addition), Aedos abstains on
the common "X was/is [identity/role]" pattern.

### Sub-cause F — Tier U predicate alignment

For Asa-persona claims, the Tier U DB has `Asa lives_in Williamstown`
but the extractor produces `Asa located_in Williamstown` — the Tier U
Stage 1 literal lookup misses. This affects cross-source claims that
require Asa-persona substrate.

### Sub-cause E (not architectural — surgical) — Python verifier over-eagerness

**Resolved by Fix 3.** Listed for completeness.

## Post-fix Run 2 (full 122 cases) — pending

A full 122-case run with all five fixes applied is queued. The
post-fix run characterizes:

- Headline accuracy delta vs baseline
- Soundness metrics (false-verified rate, fp_correction count) —
  expected ≈ 0 based on retest
- Per-mode improvements (subsumption upgrade should help geographic
  claims across cross_source_unification, entity_disambiguation,
  some multi_hop_distribution)
- Remaining sub-causes B/C/D/F manifest

## Variance discipline (D49)

Per D49, the medium-bar evaluation runs 3 times to characterize
multi-LLM-chain variance. Run 1 was pre-fix (single point); a single
post-fix Run 2 establishes the new baseline. Whether to run the
full 3-run variance pass after Run 2 is an operator call — if
post-fix headline numbers approach a release threshold, variance
discipline matters; if the underperformance persists due to sub-
causes B/C/D/F, additional runs mainly characterize the same
phenomenon.

Per-run artifacts:

- `docs/phase_10_5/medium_bar/medium_bar_run_01.{md,json}` —
  pre-fix Run 1
- `docs/phase_10_5/medium_bar/medium_bar_run_02.{md,json}` —
  post-fix Run 2 (pending)
- `docs/phase_10_5/medium_bar/aggregate_after_run1.json` —
  Run 1 aggregate

## Interpretation

**Soundness invariant held in Run 1 and strengthens after fixes.**
Run 1's false-verified rate (1.6%) was below the 5% threshold; the
2 false-verified cases plus 5 fp_corrections were the soundness
signals to investigate, not architectural collapses. Post-fix, all
seven are eliminated.

**The verify-or-abstain trade-off has a higher abstention cost
than Aedos's design originally calibrated for in this realistic
test set.** v0.15 abstains aggressively when the substrate isn't
sufficient (the LLM-only baseline commits to answers via parametric
knowledge that Aedos can't ground). Where the substrate IS
sufficient — principled_abstention (90%), belief_revision (30%
in Run 1, may improve post-fix) — Aedos beats the baseline. Where
it isn't — multi-hop, entity disambiguation, predicate translation
with non-seed vocabulary — Aedos abstains and the baseline wins
by guessing (sometimes wrongly: 9.8% false-verified vs Aedos's
1.6%).

**The Run 1 result is honest data about v0.15's architectural
ceilings against natural-language claims.** The five fixes
close the acute soundness signals; the remaining gap reflects
extraction-vocabulary alignment, walker depth, and generic-
predicate handling — work for v0.16 priority discussions.
