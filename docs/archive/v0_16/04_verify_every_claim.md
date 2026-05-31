# Aedos v0.16 ? Change Specification: Workstream 4 ? Verify Every Claim & Quiet Designations

*Implementation-ready spec. Conforms to the interface contract in `00_overview_contract_ordering.md`, which is AUTHORITATIVE on all cross-workstream interfaces (data model, shared tables, ordering, soundness-sensitive deletion order). All file:line references were verified by the spec-mapping pass against the v0.16 base (identical to `main` at branch creation).*

---

# WORKSTREAM 4 — Verify Every Claim (no silent drops) + Quiet Designations + Prompt Shrink — IMPLEMENTATION-READY CHANGE SPEC

This spec covers four code-touch areas: (4a) `Claim.abstention_reason` + `_build_claim` never returning `None`; (4b) walker short-circuit of malformed-reason claims to `no_grounding_found` before any KB lookup; (4c) triage `INERT_PROSE` → quiet `not_checkworthy` `ClaimVerdict` (stop the chat_wrapper VERIFY-filter silent drop, surface quietly); (4d) v5 prompt rule removals once the substrate predicate-map owns predicate→property mapping.

All file:line references are verified against the current code (read in full above).

---

## (1) DETAILED CHANGE SPEC

### 4a — `Claim.abstention_reason`; the four `_build_claim` `None`-returns become reasoned shaped claims

#### 4a.0 — The `abstention_reason` enum (single source of truth)

There is **no** existing enum type for abstention reasons — they are bare strings. Today abstention reasons are produced in two disjoint places as plain strings:
- `walker.py`: `"budget_wall_clock"` (line 338), `"budget_llm_calls"` (line 348), `"depth_exhausted"` (line 403).
- `kb_verifier.py`: `"lookup_subject_unresolved"` (line 177), `"no_statements"` (line 264) — written into the KB **trace** dict, not into `WalkResult.abstention_reason`.
- `aggregator.py` reads the string and pattern-matches: `"budget" in result.abstention_reason` (line 194) and `== "circuit_breaker_triggered"` (line 197).

**ADD** a string-valued enum **in `triage.py`** (it already owns `TriageDecision`; this co-locates the extraction-layer designations and avoids a circular import — `extractor.py` already imports from `triage.py`, and `walker.py`/`aggregator.py` can import it without a cycle). The interface contract names exactly five **new** values; we make the enum a `str` subclass so the existing string-comparison consumers (`aggregator.py:194,197`) keep working unchanged.

```python
# triage.py — ADD below TriageDecision
class AbstentionReason(str, Enum):
    """Why a claim could not be (or should not be) externally grounded.

    The five extraction-layer reasons (set in extractor._build_claim) plus
    the quiet not_checkworthy designation are the v0.16 additions; the
    walker-layer reasons (budget_*, depth_exhausted) and KB-layer reasons
    pre-date v0.16 and remain bare strings written by their own modules.
    Subclassing str keeps existing `"budget" in reason` / `== "..."`
    comparisons in aggregator.py working unchanged.
    """
    SELF_REFERENTIAL = "self_referential"
    PREDICATE_EQ_OBJECT = "predicate_eq_object"
    CONTENT_LESS_EVENT = "content_less_event"
    SUBJECT_ABSENT_FROM_SOURCE = "subject_absent_from_source"
    NOT_CHECKWORTHY = "not_checkworthy"
```

Role: typed vocabulary for the extraction-layer reasons. `extractor._build_claim` sets these; the walker reads them to short-circuit (4b); `select_interventions` reads `not_checkworthy` to suppress intervention (4c).

Where each is set:
| Value | Set at | Trigger |
|---|---|---|
| `subject_absent_from_source` | `extractor._build_claim`, replaces line 527-528 | hard-claim check fails (subject AND object not in text) |
| `content_less_event` | **REMOVED** filter (see Deletions); not set — claim is now emitted with reason `None` | (obsolete per contract) |
| `self_referential` | `extractor._build_claim`, replaces line 579-583 | subject == object after casefold |
| `predicate_eq_object` | `extractor._build_claim`, replaces line 595-598 | predicate == object after casefold |
| `not_checkworthy` | `extractor._build_claim` (derived from triage result) and consumed at chat_wrapper / select_interventions | `triage(...) == INERT_PROSE` |

#### 4a.1 — `Claim` gains `abstention_reason`

**CURRENT** `extractor.py:472-485`:
```python
@dataclass
class Claim:
    claim_id: str
    subject: str
    predicate: str
    object: str
    polarity: int
    source_text: str
    asserting_party: str
    triage_decision: TriageDecision
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    valid_during_ref: Optional[str] = None
    reified_event_id: Optional[str] = None
```

**ADD** one optional field (default `None` so every existing `Claim(...)` construction across src+tests stays valid — there are ~30 such constructions enumerated in §4):
```python
    abstention_reason: Optional[str] = None
```
Insert as the last field (after `reified_event_id`) to keep positional-arg back-compat for any caller using positional args (none found — all use kwargs — but last-field is safest).

Role: carries the extraction-layer drop reason forward instead of dropping the claim. `None` means "shaped, no malformed-reason — verify normally."

#### 4a.2 — `_build_claim` NEVER returns `None` for a shaped claim

`_build_claim` currently has **four** `None`-return drop sites plus the future-tense drop. We keep the future-tense drop (future claims are explicitly filtered per Rule 4 and are out of scope — the contract lists only the four drops). We convert the four named drops into reasoned claims and let the walker short-circuit them (4b).

The function's strategy changes from "compute reason, return None early" to "compute `abstention_reason`, fall through to a single `Claim(...)` construction that always runs." This requires restructuring so the normalization/scope/triage steps still run (they must, because we need `subject`, `predicate`, `object_value`, `polarity`, `source_text`, `scope`, and `triage_decision` populated on the returned claim).

Concretely:

**(i) Hard-claim drop — `extractor.py:526-528`:**
CURRENT:
```python
        # Hard-claim discipline heuristic: reject claims whose entities don't appear in text
        if not self._passes_hard_claim_check(raw_subject, raw_object, text, reified_id):
            return None
```
REPLACE with a reason capture (do not return):
```python
        abstention_reason: Optional[str] = None
        # Hard-claim discipline: subject/object absent from source text.
        if not self._passes_hard_claim_check(raw_subject, raw_object, text, reified_id):
            abstention_reason = AbstentionReason.SUBJECT_ABSENT_FROM_SOURCE.value
```

**(ii) Content-less event drop — `extractor.py:542-548`:** **DELETE entirely** (obsolete per contract item 4). See Deletions §2. The duplicate `raw_pred_check` assignment at line 542 is removed with it; the later reuse at line 595 reassigns it so no NameError.

**(iii) Self-referential drop — `extractor.py:579-583`:**
CURRENT:
```python
        if (
            raw_subject.strip().casefold() == raw_object.strip().casefold()
            and raw_subject.strip()
        ):
            return None
```
REPLACE (only set if not already set — first reason wins; precedence documented below):
```python
        if (
            abstention_reason is None
            and raw_subject.strip().casefold() == raw_object.strip().casefold()
            and raw_subject.strip()
        ):
            abstention_reason = AbstentionReason.SELF_REFERENTIAL.value
```

**(iv) Predicate==object drop — `extractor.py:595-598`:**
CURRENT:
```python
        raw_pred_check = (raw.get("predicate") or "").strip().casefold()
        raw_obj_check = raw_object.strip().casefold()
        if raw_pred_check and raw_obj_check and raw_pred_check == raw_obj_check:
            return None
```
REPLACE:
```python
        raw_pred_check = (raw.get("predicate") or "").strip().casefold()
        raw_obj_check = raw_object.strip().casefold()
        if (
            abstention_reason is None
            and raw_pred_check and raw_obj_check and raw_pred_check == raw_obj_check
        ):
            abstention_reason = AbstentionReason.PREDICATE_EQ_OBJECT.value
```

**(v) Future-tense drop — `extractor.py:607-608`:** **KEEP AS-IS** (`return None`). Future claims are not "shaped claims to verify"; Rule 4 declares them filtered. This is the one legitimate remaining `None`-return. (Tests `TestFutureTenseRejection` depend on it — see §5.)

**(vi) `not_checkworthy` from triage — at the existing `triage(...)` call `extractor.py:639-646`:** After computing `triage_decision`, if it is `INERT_PROSE` and no malformed reason was already set, stamp `not_checkworthy`:
```python
        triage_decision = triage(...)  # unchanged call
        if abstention_reason is None and triage_decision == TriageDecision.INERT_PROSE:
            abstention_reason = AbstentionReason.NOT_CHECKWORTHY.value
```

**(vii) The single always-runs construction — `extractor.py:648-661`:** add the field:
```python
        return Claim(
            claim_id=str(uuid.uuid4()),
            subject=subject,
            ...
            reified_event_id=reified_id,
            abstention_reason=abstention_reason,
        )
```

**Precedence (order claims are checked → first wins):** `subject_absent_from_source` → `self_referential` → `predicate_eq_object` → `not_checkworthy`. This matches today's top-to-bottom drop order (hard-claim check is first; triage is last), so a claim that today is dropped by the hard-claim check still carries `subject_absent_from_source` rather than a later reason. Documented in a comment block above the function.

**Note on `extractor.extract` loop (`extractor.py:507-511`):** the `if claim is not None: claims.append(claim)` guard stays — it now only filters the future-tense `None`. No change needed, but add a clarifying comment that `_build_claim` returns `None` ONLY for future-tense claims now.

---

### 4b — Walker short-circuits a malformed-reason claim to `no_grounding_found` BEFORE any KB lookup

The contract requires `self_referential` / `predicate_eq_object` to be short-circuited **pre-lookup** (they describe malformed triples whose KB lookup would risk a §3.2 false-contradiction — exactly the soundness reason the drops existed). `subject_absent_from_source` and `not_checkworthy` are also short-circuited (no point grounding a not-checkworthy or fabricated claim), but the two **pre-lookup-mandatory** ones are the malformed-triple reasons.

**Location:** `walker.py` `walk()`, at the very top of the per-node processing, BEFORE `_direct_lookup` is called. The cleanest insertion point is inside the `for node in frontier:` loop right after the `visited` bookkeeping and `polarity_trace.append`, BEFORE `verdict, lookup_source, llm_delta = self._direct_lookup(...)` at `walker.py:361`. But because the short-circuit applies to the **root claim's** reason (the malformed-ness is a property of the original extracted claim, not of substrate-expanded child nodes which are freshly synthesized via `_claim_from_parts` and have `abstention_reason=None`), the check belongs at walk entry on `claim`, before the frontier loop.

**ADD** a module-level constant naming the pre-lookup short-circuit reasons, and a guard at the start of `walk()`.

**CURRENT** `walker.py:298-326` (walk setup, just before `frontier = [claim]`):
```python
        start_time = time.monotonic()
        llm_calls = 0
        root_node = TraceNode(node_type="claim", content={...})
        trace = JustificationTrace(root=root_node, source_breakdown={...})
        polarity_trace: list[int] = []

        if self._predicate_routing(claim.predicate) == "user_authoritative":
            trace.chain_includes_assertion = True

        frontier: list[Claim] = [claim]
```

**ADD** immediately after `trace = JustificationTrace(...)` (so the trace exists for the returned `WalkResult`) and before the `user_authoritative` block:
```python
        # Workstream 4 (4b): a claim carrying an extraction-layer
        # abstention_reason is malformed or not-checkworthy. Short-circuit
        # to no_grounding_found BEFORE any Tier U / KB / Python lookup.
        # The self_referential / predicate_eq_object reasons are the
        # §3.2-soundness-critical ones: their malformed triples would,
        # if looked up, risk a false contradiction (the v0.15 reason the
        # extractor dropped them). subject_absent_from_source and
        # not_checkworthy are also returned here (nothing to ground).
        if claim.abstention_reason:
            consumption = BudgetConsumption(wall_clock_ms=0.0, llm_calls=0)
            trace.walk_metadata["short_circuit"] = claim.abstention_reason
            trace.polarity_trace = []
            return WalkResult(
                verdict="no_grounding_found",
                trace=trace,
                abstention_reason=claim.abstention_reason,
                budget_consumption=consumption,
            )
```

Notes:
- We return the **base** `no_grounding_found` (NOT `_apply_assertion_designation`). These claims never reach a Tier U premise, so `chain_includes_assertion` is False and the designation would be a no-op anyway; calling it pre-loop is harmless but we keep it base for clarity and because the `user_authoritative` chain-flag set happens *after* this guard (so it's irrelevant).
- We do NOT set `current_verdict` / iterate the frontier; the early `return` is correct because a malformed root claim has no meaningful expansion.
- `not_checkworthy` flowing to `no_grounding_found` at the **walker** is the fallback for any path that does NOT pre-filter on triage (the chat_wrapper currently pre-filters at line 264 — see 4c — but the corpus runner intervention path and any future caller that walks all claims get correct behavior). The `abstention_reason` rides along so `select_interventions` (4c) can suppress it.

**No `_direct_lookup` change needed:** because the guard is at `walk()` entry, the subject==object / predicate==object claims never reach `_direct_lookup`, satisfying "subject==object/predicate==object MUST stay pre-lookup." (Defense-in-depth note: `_direct_lookup`'s own `flipped`/`oc_result`/Stage-1 lookups are never invoked for these claims.)

---

### 4c — triage `INERT_PROSE` → quiet `not_checkworthy` `ClaimVerdict` (no silent drop; surface quietly)

Today the VERIFY-filter at **`chat_wrapper.py:264`** and **`chat_wrapper.py:228`** (user-message promotion path) and **`benchmark.py:203`** silently discard every `INERT_PROSE` claim. The contract requires INERT_PROSE claims to be **carried as a `ClaimVerdict`** with a quiet `not_checkworthy` designation — verified-through the pipeline but producing **no intervention**.

Two sub-changes:

#### 4c.1 — Stop the chat_wrapper draft-extraction VERIFY-filter from dropping INERT_PROSE (`chat_wrapper.py:262-264`)

**CURRENT:**
```python
            claims = self._extractor.extract(draft, extraction_context)
            # Only keep VERIFY-triaged claims for verification
            claims = [c for c in claims if c.triage_decision == TriageDecision.VERIFY]
```

**REPLACE WITH** (keep all shaped claims; the walker short-circuits INERT_PROSE→`not_checkworthy` via 4b because `_build_claim` stamped `abstention_reason=not_checkworthy`):
```python
            claims = self._extractor.extract(draft, extraction_context)
            # Workstream 4 (4c): do NOT drop INERT_PROSE claims here. They
            # now carry abstention_reason='not_checkworthy' (extractor) and
            # the walker short-circuits them to no_grounding_found (4b); the
            # aggregator records a ClaimVerdict so the designation is
            # observable, and select_interventions suppresses any
            # not_checkworthy claim from the user-facing notes (quiet).
```
i.e. delete the filter line entirely. All extracted claims now flow into the walk loop (`chat_wrapper.py:276-278`).

**Cost note to call out in the spec:** removing the filter means INERT_PROSE claims are now *walked*. Because 4b short-circuits them at `walk()` entry with **zero** Tier U / KB / Python calls and zero LLM calls, the added per-claim cost is one trace-node allocation and an early return — negligible. This is load-bearing for the soundness story: no extra external lookups.

#### 4c.2 — `select_interventions` suppresses `not_checkworthy` (quiet)

`select_interventions` (`chat_wrapper.py:101-156`) currently treats every non-`verified`, non-`contradicted` base verdict as an ABSTAIN action (the `else` branch at line 145). A `not_checkworthy` claim has base verdict `no_grounding_found`, so today it would emit a user-facing "Aedos could not verify:" note — which is exactly the noisy behavior we must avoid ("surface quietly"). We add a guard to skip claims whose `abstention_reason == "not_checkworthy"` entirely from action generation AND from the `verified_count`/`actions` accounting.

**CURRENT** `chat_wrapper.py:132-150`:
```python
    actions: list[ClaimAction] = []
    verified_count = 0
    for cv in claim_verdicts:
        base = base_verdict_of(cv.verdict)
        if base == "verified":
            verified_count += 1
            continue
        if base == "contradicted":
            actions.append(ClaimAction(...CORRECT...))
        else:  # no_grounding_found (and its dual abstained_given_assertion)
            actions.append(ClaimAction(...ABSTAIN...))
```

**REPLACE** the loop body's top with a not_checkworthy skip:
```python
    actions: list[ClaimAction] = []
    verified_count = 0
    for cv in claim_verdicts:
        # Workstream 4 (4c): not_checkworthy claims are quiet — they are
        # recorded as ClaimVerdicts (observable in VerificationResult) but
        # produce no user-facing note and do not count toward the
        # verified/problematic tallies that drive PASS_THROUGH/DECLINE.
        if cv.abstention_reason == AbstentionReason.NOT_CHECKWORTHY.value:
            continue
        base = base_verdict_of(cv.verdict)
        if base == "verified":
            verified_count += 1
            continue
        ...
```
Add import at `chat_wrapper.py:10`: `from ..layer1_extraction.triage import TriageDecision, AbstentionReason`.

**Why skip from tallies:** a draft that is *all* inert prose (e.g. "That's a great question!") would otherwise, with the old filter, produce an empty claim list → PASS_THROUGH. Skipping not_checkworthy claims from `actions` and `verified_count` preserves that: empty effective set → `if not actions: PASS_THROUGH` (line 152-153). Without the skip, an all-inert draft would yield N abstain actions and (N≥2, verified_count==0) → spurious DECLINE — a regression. The skip is therefore behavior-preserving for the user-facing surface while making the designation observable.

#### 4c.3 — `select_interventions` STOP collapsing `*_given_assertion` (contract item 5: "select_interventions stops collapsing *_given_assertion; emit conditional annotation")

This is named in contract Decision 5; flag it as **adjacent, recommended-in-this-workstream but not strictly required by WS4's bullet list.** Minimal WS4-scoped change: leave `base_verdict_of` collapse in place for the PASS_THROUGH/DECLINE policy decision, but when emitting an action, set a `conditional` marker on the `ClaimAction` when `is_given_assertion(cv.verdict)`. Concretely add a field `conditional: bool = False` to `ClaimAction` and pass `conditional=is_given_assertion(cv.verdict)` in both action branches; `_format_correction`/`_format_abstention` append " (conditional on your assertion)" when set. **Recommendation:** defer the annotation-text change to Workstream 5 (Corrections+Conditional+Observability owns it) to avoid double-touching `_format_*`; WS4 only needs the not_checkworthy suppression. Note the dependency in §6.

#### 4c.4 — User-message promotion VERIFY-filter (`chat_wrapper.py:226-230`)

**CURRENT:**
```python
            user_claims = self._extractor.extract(user_message, user_ctx)
            user_claims = [c for c in user_claims if c.triage_decision == TriageDecision.VERIFY]
            if user_claims:
                promote_assertions(user_claims, self._tier_u)
```
**KEEP THE FILTER HERE** but tighten its semantics: this path promotes user assertions into Tier U as premises. A not_checkworthy / malformed claim should NOT become a premise. Replace the triage filter with an `abstention_reason`-aware filter so promotion excludes anything carrying a reason (malformed or not-checkworthy), which is a strict superset of the old behavior for the drop cases and identical for VERIFY claims:
```python
            user_claims = self._extractor.extract(user_message, user_ctx)
            # Workstream 4 (4c): promote only checkworthy, well-formed
            # assertions as premises — exclude any claim carrying an
            # extraction-layer abstention_reason (not_checkworthy, self_
            # referential, predicate_eq_object, subject_absent_from_source).
            user_claims = [c for c in user_claims if c.abstention_reason is None]
```
This drops the `TriageDecision == VERIFY` test in favor of `abstention_reason is None`. They are equivalent for the promotion gate because `_build_claim` now stamps `not_checkworthy` exactly when `triage()==INERT_PROSE` (and the malformed reasons are an additional, correct exclusion). `TriageDecision` import is still needed elsewhere in the file? — check: after this change `TriageDecision` is used at line 264 (being deleted) and 228 (being changed). **Both uses removed** → the `TriageDecision` import at line 10 becomes unused for runtime but `AbstentionReason` is now imported. Keep `TriageDecision` in the import only if any remaining reference exists; per grep, none remain in chat_wrapper after these edits → **remove `TriageDecision` from the import, add `AbstentionReason`.** (Verify with a grep post-edit.)

#### 4c.5 — benchmark VERIFY-filter (`benchmark.py:201-205`)

**CURRENT:**
```python
            claims = extractor.extract(case.statement, ctx)
            # Only VERIFY-triaged claims are verified (matches the chat-wrapper).
            claims = [c for c in claims if c.triage_decision == TriageDecision.VERIFY]
            if not claims:
                return RunResult(case_id=case.case_id, verdict="no_grounding_found", ...)
```
The comment says "matches the chat-wrapper." To keep that parity after 4c.1 removes the chat-wrapper filter, **change benchmark to filter on `abstention_reason is None`** (so not_checkworthy/malformed claims are excluded from the benchmark's single-verdict rollup, preserving benchmark scoring semantics — the benchmark measures groundable claims, not prose):
```python
            claims = extractor.extract(case.statement, ctx)
            # Workstream 4 (4c): exclude extraction-layer-reasoned claims
            # (not_checkworthy + malformed) from the benchmark rollup, the
            # benchmark scores groundable claims. Mirrors the promotion gate.
            claims = [c for c in claims if c.abstention_reason is None]
            if not claims:
                return RunResult(case_id=case.case_id, verdict="no_grounding_found", ...)
```
Import update: `from aedos.layer1_extraction.triage import AbstentionReason` is not even needed (we test `is None`); `TriageDecision` import in benchmark becomes unused → remove it from the local import inside the function (`benchmark.py` imports `TriageDecision` inside the run method per the grep at the top of the file — confirm and drop).

---

### 4d — v5 prompt rules made unnecessary by the substrate predicate-map

The substrate's **multi-property predicate map** (Decision 1) and **discover/verify split** (Decision 2) move predicate→KB-property mapping out of the extraction prompt and into the substrate, where evidence arbitrates. Several v5 rules exist ONLY to pre-bake a specific surface-verb→canonical-predicate→implied-KB-property choice that the LLM should not have to make once the substrate owns it. Below: which rules are candidates for removal/shrink, the recommendation, and the regression gate for each.

**Principle for the recommendation:** A rule is removable iff (a) it encodes a *predicate-name selection* whose only purpose is to route to a specific KB property, AND (b) the substrate predicate-map can now discover that property from the verb-derived predicate via Wikidata's own ontology (P2302 constraints / subproperty / inverse) or distant-supervision fallback. A rule is NOT removable if it encodes a *structural* extraction decision (slot placement, reification, temporal-field routing, polarity) that the substrate cannot reconstruct from a flattened predicate. The discipline (per MEMORY: "knowledge belongs in prompt/KB/oracle, not hardcoded") is to move predicate→property knowledge to the substrate, but to KEEP structural shaping in the prompt.

| Rule | What it does | Recommendation | Why / regression gate |
|---|---|---|---|
| **1, 2, 5, 6** (core: explicit-only, first-person, source-text, contrastive) | Structural extraction discipline | **KEEP** | Not predicate→property. `TestFirstPersonCanonicalization`, `TestSourceTextDiscipline`, `TestContrastiveCorrections` pin these. |
| **3** (reported speech) | Structural (reification of assertion) | **KEEP** | Substrate cannot reconstruct the inner-claim split. |
| **4** (future tense) | Structural (filter signal) | **KEEP** | `TestFutureTenseRejection` depends on it. |
| **7** (year/date → valid_from vs object; location→object) | **Structural** temporal-slot routing | **KEEP** (do not remove) | This is slot-placement (date→`valid_from` vs object), not predicate→property. Removing it reintroduces the year-in-object ambiguity. But see Rule 23 interaction below. Regression: `test_pipeline_handles_employment_start_shape`. |
| **8** (end-of-event year → valid_until) | Structural temporal-slot routing | **KEEP** | Same as 7. `_TEMPORAL` triage set + `test_prompt_carries_rule_8_event_non_trigger`. |
| **9, 15, 16, 17** (temporal qualifiers / decade expansion / event-relative bounds) | Structural (valid_during_ref / valid_from / valid_until routing) | **KEEP** | Pure temporal-field shaping; substrate has no view of these. T1 (contract item 6) builds ON these, doesn't remove them. |
| **10, 11** (multi-participant event reification vs non-reification) | Structural (participants/event_type) | **KEEP** | `decompose_event` depends on the shape. |
| **12, 13** (employment start/termination → `employed_by` + valid_from/until) | **Predicate selection** (`joined`/`hired`/`started`→`employed_by`) AND temporal-slot routing | **SHRINK, do not delete** | The verb→`employed_by` mapping is exactly what the substrate predicate-map can now own (P108 employer + subproperty/related discovery from `joined`/`hired_by`). BUT the valid_from/valid_until routing is structural and must stay. **Recommendation:** keep the temporal-slot half; drop the worked examples that only disambiguate `member_of` vs `employed_by` (Rule 12's DO-NOT block) IF the substrate's value-type constraint (object_entity_types: organization vs club) can arbitrate — **defer this trim**, it touches substrate Decision 1 which is another workstream. Regression gate before any trim: `test_pipeline_handles_employment_start_shape`, `test_pipeline_handles_employment_termination_shape`, `test_prompt_carries_rule_12_*`, `test_prompt_carries_rule_13_*`, and the live `derivation_corpus` employment cases. **For WS4: KEEP unchanged; flag as substrate-workstream trim candidate.** |
| **14** (state-change → `status` predicate) | Predicate selection + temporal routing | **KEEP for WS4** | `test_prompt_carries_rule_14_*` and `test_pipeline_handles_status_change_shape` pin it; `status`/`ended`/`ongoing` is a bespoke vocabulary the substrate map does not yet cover. |
| **18** (residence → `lives_in`) | **Predicate selection only** (`lives`/`resides`→`lives_in`, not `located_in`) | **CANDIDATE TO SHRINK; KEEP the Python `_RESIDENCE_VERB` rewrite (`extractor.py:623-624`) regardless** | The prompt rule + the post-hoc regex both exist because the LLM mis-emits `located_in`. The substrate predicate-map (Decision 1) maps `lives_in`→P551 (residence) and `located_in`→P131; if the prompt emits `located_in` for a person+place, the substrate's **subject_entity_types** constraint (P551 wants a human subject; P131 wants a place subject) can arbitrate at verify time. **However**, the `_RESIDENCE_VERB` regex rewrite is a hardcoded band-aid (MEMORY: no hardcoded mappings) — it should ultimately be deleted in favor of substrate arbitration. **For WS4: KEEP Rule 18 and the regex** (deleting either is a substrate-workstream concern with §3.2 risk if the constraint arbitration isn't yet wired). Flag both as removal candidates once Decision 1's value-type arbitration lands. Regression gate: the `_RESIDENCE_VERB` rewrite has no direct unit test (grep shows only integration `lives_in` cases in `test_walker_*`); a **new test is needed** before removal (see §5). |
| **19** (`instance_of` for "X is a Y") | Predicate selection (→P31) | **KEEP for WS4** | Routes to P31; structural-ish (article stripping). Substrate could own `is_a`→P31 but the article-stripping + indefinite-vs-definite disambiguation (vs Rule 20) is shaping. Defer. |
| **20** (`holds_role` for "X is the President of Y") | Predicate selection (→P39) + object compounding | **KEEP** | The "President of the United States" object-compounding is structural and substrate cannot reconstruct it from a bare `holds_role`. |
| **21, 22** (nationality / compound nationality → `has_nationality` + demonym object) | Predicate selection (→P27 via P1549 demonym resolution) | **KEEP for WS4** | The demonym-as-object convention is load-bearing for the adapter's P1549 reverse lookup. Substrate-side demonym resolution already exists; the prompt shaping (keep bare demonym, one claim per demonym) is structural. |
| **23** (date-valued event predicates: `born_on`/`founded_in_year`/… with date in OBJECT) | **Predicate selection** (date-sense predicate→P569/P570/P571/P576/P577/P585) | **KEEP for WS4; PRIME candidate to shrink later** | This is the cleanest case where the substrate predicate-map should own the mapping: `born_on`→P569 is exactly a single-property binding the substrate seeds. The rule's *structural* contribution is "put the date in the OBJECT slot, NOT valid_from" — that **must stay** (it's the precedent T1/Decision 6 builds on). The *predicate-name menu* (born_on/died_on/founded_in_year/…) is candidate to collapse to a generic "date-sense predicate" once the substrate maps any `<verb>_on`/`<verb>_in_year` to its date property. **For WS4: KEEP.** Regression gate: `test_seed_loader` pins `born_on`/`founded_in_year` predicate names and their P569/P571 mapping — those must stay green. |
| **24** (quantitative count comparison → `<measure>_greater_than`) | Predicate selection (→kb_quantitative routing) + comparator-in-name | **KEEP** | The walker's `_verify_kb_quantitative` (`walker.py:739`) parses the comparator out of the predicate name and reads `meta.kb_property`; removing the rule breaks that contract. `test_seed_loader` references `population_greater_than`-style. |

**Net WS4 recommendation on the prompt:** Make **no deletions** to `_SYSTEM_PROMPT` in this workstream. Every removal candidate (12/13 employment menu, 18 residence, 23 date-sense menu) couples to substrate Decision 1's evidence-arbitration, which is a *different* workstream; trimming them here without the arbitration wired risks §3.2 false-contradictions (the exact failure documented in `extractor.py:570-578`). The WS4 deliverable for 4d is: **(1) document the four "predicate-selection-only" rules (12/13 partial, 18, 21/22 partial, 23 partial, 24) as removable-once-substrate-arbitrates, with the regression gate for each (listed above); (2) when the substrate workstream lands, the removals are: drop Rule 18 + the `_RESIDENCE_VERB` regex; collapse Rule 23's seven-name menu to a single generic date-sense instruction; drop Rule 12/13's `member_of`-disambiguation DO-NOT blocks.** Each must be regression-tested against the live `derivation_corpus` and the `tests/unit/test_extractor.py` prompt-pinning tests, which will need updating in lockstep (see §5).

---

## (2) DELETIONS

| # | File:lines | What | Why safe |
|---|---|---|---|
| 1 | `extractor.py:526-528` (the `return None`) | Hard-claim drop's `return None` | Replaced by `abstention_reason = SUBJECT_ABSENT_FROM_SOURCE`; claim now flows and the walker short-circuits it. No caller relies on the claim being absent — `extract`'s loop already tolerates the claim, and the chat_wrapper/benchmark now filter on `abstention_reason`. |
| 2 | `extractor.py:530-548` (entire content-less-event block incl. the `raw_pred_check` assign at 542 and the comment 530-541) | Content-less occurred/happened event filter | Contract item 4 declares it OBSOLETE. The standalone `(World War II, occurred, '')` shape it guarded against is now handled by emitting the claim with NO abstention reason and letting the walker abstain naturally (empty object → no grounding) — or, if it slips through, the aggregator no longer drags the compound verdict because per-claim verdicts are independent. Removing it also removes a duplicate `raw_pred_check` definition (re-defined at line 595). Safe: no test asserts these claims are dropped (grep: no `occurred`/`happened` drop test). |
| 3 | `extractor.py:579-583` (`return None`) | Self-referential drop's `return None` | Replaced by reason capture + walker pre-lookup short-circuit. The §3.2-soundness intent (don't let subject==object reach KB lookup) is PRESERVED by 4b's pre-lookup guard. |
| 4 | `extractor.py:595-598` (`return None`) | Predicate==object drop's `return None` | Same as #3 — soundness preserved by 4b. |
| 5 | `chat_wrapper.py:264` (the VERIFY filter list-comp) | `claims = [c for c ... == VERIFY]` on draft claims | Replaced by walker short-circuit (4b) + select_interventions suppression (4c.2). No silent drop; designation observable. |
| 6 | `chat_wrapper.py:10` partial: `TriageDecision` from import | Unused after 4c | Both its uses (228, 264) removed/changed to `abstention_reason`. Grep-verify no other use in file before removing. |
| 7 | `benchmark.py` local `TriageDecision` import + filter (lines ~ the local import and 203) | VERIFY filter | Replaced by `abstention_reason is None` filter. |

**No deletions to `_SYSTEM_PROMPT` in WS4** (see 4d rationale).

---

## (3) ADDITIONS

| File | Block | Role |
|---|---|---|
| `triage.py` | `class AbstentionReason(str, Enum)` with 5 members | Typed vocabulary for extraction-layer abstention reasons; consumed by extractor (set), walker (short-circuit), chat_wrapper (suppress not_checkworthy). |
| `extractor.py` | `Claim.abstention_reason: Optional[str] = None` field | Carries the drop reason forward instead of dropping. |
| `extractor.py` | `from .triage import TriageDecision, triage, AbstentionReason` (extend existing import line 12) | Access enum. |
| `extractor.py` | `_build_claim` restructured: `abstention_reason` local, four `None`-returns → reason captures with `is None` precedence guards, `not_checkworthy` stamp after triage, single `Claim(...)` always-construct with `abstention_reason=`; KEEP future-tense `return None` | Never returns None for a shaped claim. |
| `walker.py` | walk-entry guard: `if claim.abstention_reason: return WalkResult("no_grounding_found", ..., abstention_reason=claim.abstention_reason)` after `trace` creation, before `user_authoritative` block | Pre-lookup short-circuit (4b). |
| `chat_wrapper.py` | import `AbstentionReason`; remove `TriageDecision`; delete draft VERIFY-filter; change promotion filter to `abstention_reason is None`; add `not_checkworthy` skip in `select_interventions` loop | Stop silent drop; quiet surface. |
| `benchmark.py` | change filter to `abstention_reason is None`; drop `TriageDecision` import | Parity + groundable-claim rollup. |

---

## (4) CALL-SITES / CONSUMERS (grep-verified, every site)

**`Claim(...)` constructions** — the new `abstention_reason` field defaults to `None`, so **no constructor call needs updating** for correctness. Enumerated for completeness (all keyword-arg style, all safe):
- `extractor.py:648` (the build site — UPDATED to pass `abstention_reason=`).
- `walker.py:166` `_claim_from_parts` (synthesized child nodes — leaves `abstention_reason` defaulted `None`, correct: substrate-expanded nodes are not malformed).
- `tests`: `test_corpus_runner.py:423,560,635`; `benchmark.py` claim build; `test_d47_pipeline_integration.py:247`; `test_end_to_end.py:113`; `test_inverse_predicate_kb.py:92`; `test_kb_path.py:94`; `test_python_path.py:91`; `test_routing_to_tier_u.py:64`; `test_seed_single_valued_kb.py:80`; `test_walker_failure_modes.py:111`; `test_walker_with_substrate.py:103`; `test_aggregator.py:23`; `test_chat_wrapper.py:43`; `test_extractor.py:75,92,108,122`; `test_kb_verifier.py:110`; `test_promotion.py:29`; `test_python_verifier.py:45`; `test_retraction_propagator.py:22`; `test_router.py:70`; `test_tier_u.py:39`; `test_walker.py:146`; `test_walker_cluster_2.py:109,488`; `test_walker_kb_neighbors.py:46`. **All keyword-arg, all unaffected.**

**`triage_decision` filter consumers** (must update logic):
- `chat_wrapper.py:228` — CHANGE to `abstention_reason is None`.
- `chat_wrapper.py:264` — DELETE.
- `benchmark.py:203` — CHANGE to `abstention_reason is None`.

**`select_interventions` consumers** (verify the not_checkworthy skip doesn't break them):
- `chat_wrapper.py:288` (production call).
- `test_corpus_runner.py:540-577` (`_run_intervention` builds ClaimVerdicts with `verdict` only, no `abstention_reason` → `None` → skip never fires → behavior unchanged). SAFE.
- `test_chat_wrapper.py` `_make_claim_verdicts` (abstained branch sets `abstention_reason="no_kb_path"`, NOT `not_checkworthy` → skip never fires). SAFE.

**`WalkResult.abstention_reason` consumers** (verify new `self_referential`/`not_checkworthy`/etc. don't break pattern-matches):
- `aggregator.py:176` — passes through to `ClaimVerdict.abstention_reason`. SAFE (any string).
- `aggregator.py:194` — `"budget" in result.abstention_reason` — none of the new reasons contain "budget". SAFE.
- `aggregator.py:197` — `== "circuit_breaker_triggered"` — no collision. SAFE.

**`WalkResult.verdict` consumers** (the short-circuit returns base `no_grounding_found`, an existing value):
- `aggregator.py:179` `_VERDICT_TO_BASE_COUNT.get("no_grounding_found")` → `"abstained"`. SAFE.
- `chat_wrapper.py:135` `base_verdict_of` → `no_grounding_found`. SAFE (but skipped for not_checkworthy via 4c.2).

**`TriageDecision` import consumers** after removing from chat_wrapper/benchmark: grep confirms `TriageDecision` is still imported and used widely in tests and in `walker.py:165`, `extractor.py:12`, `triage.py` — those are untouched.

---

## (5) AFFECTED TESTS

| File / test | Classification | Action |
|---|---|---|
| `tests/unit/test_triage.py` (all) | **no change** | Triage logic unchanged; `INERT_PROSE` still returned. |
| `tests/unit/test_extractor.py::TestHardClaimDiscipline::test_entity_not_in_text_is_dropped` (line 402) | **WILL BREAK** | Asserts `"Bob" not in subjects`. After 4a, Bob's claim is emitted with `abstention_reason=subject_absent_from_source` (no longer dropped). **needs-update**: assert the Bob claim exists with `abstention_reason == "subject_absent_from_source"` (and/or that it walks to `no_grounding_found`). |
| `tests/unit/test_extractor.py::TestExtractorRoundtrip` (basic/multiple/triage_set) | **no change** | These use well-formed claims (`abstention_reason` stays `None`). `test_triage_decision_set` still passes (`triage_decision` still set). |
| `tests/unit/test_extractor.py::TestFutureTenseRejection` (all) | **no change** | Future-tense `return None` KEPT. `len(claims)==0` still holds. |
| `tests/unit/test_extractor.py::TestClaimDataclass` | **no change** | New field is optional/defaulted; existing constructions valid. (Optionally add `test_abstention_reason_defaults_none` — **new-needed**.) |
| `tests/unit/test_extractor.py` prompt-pin tests (`test_prompt_carries_rule_12/13/14/8`) | **no change in WS4** | No prompt deletions in WS4. (Will need updates when 4d trims land — flagged.) |
| `tests/unit/test_chat_wrapper.py` (`select_interventions` tests, `_make_claim_verdicts`) | **no change** | Synthetic ClaimVerdicts don't set `not_checkworthy`; skip never fires. |
| `tests/unit/test_chat_wrapper.py` full-`respond` integration (if any walks a mock extractor returning INERT_PROSE) | **needs-update IF present** | Verify: a draft that previously produced 0 claims (all inert filtered) now produces N claims all walking to `no_grounding_found`+`not_checkworthy`, and `select_interventions` still returns PASS_THROUGH. Add assertion that `vr.claim_verdicts` now CONTAINS the not_checkworthy claims (observability). |
| `tests/unit/test_aggregator.py` | **no change** | `abstention_reason` already plumbed through `ClaimVerdict`; new reason strings are inert to the counts. |
| `tests/calibration/test_corpus_runner.py::_run_intervention` (540+) | **no change** | Builds ClaimVerdicts without `abstention_reason`. |
| `tests/evaluation/benchmark.py` | **needs-update** | Filter changes to `abstention_reason is None`; drop `TriageDecision` local import. No assertions, but its `RunResult` rollup for inert-only statements still yields `no_grounding_found` (now via filter producing empty list). Re-run benchmark to confirm no score regression. |
| **NEW** `test_extractor.py::test_self_referential_sets_reason` | **new-needed** | Raw with `subject==object` → claim emitted, `abstention_reason=="self_referential"`. |
| **NEW** `test_extractor.py::test_predicate_eq_object_sets_reason` | **new-needed** | Raw with `predicate==object` → `abstention_reason=="predicate_eq_object"`. |
| **NEW** `test_extractor.py::test_subject_absent_sets_reason` | **new-needed** | Raw whose subject+object not in text → `abstention_reason=="subject_absent_from_source"`. |
| **NEW** `test_extractor.py::test_inert_prose_sets_not_checkworthy` | **new-needed** | Raw triaging to INERT_PROSE (e.g. `is_nice/weather/pleasant`, all lowercase) → claim emitted, `abstention_reason=="not_checkworthy"`. |
| **NEW** `test_extractor.py::test_reason_precedence` | **new-needed** | subject_absent + self_referential simultaneously → `subject_absent_from_source` wins. |
| **NEW** `test_walker.py::test_abstention_reason_short_circuits_pre_lookup` | **new-needed** | Walk a claim with `abstention_reason="self_referential"` → `WalkResult.verdict=="no_grounding_found"`, `abstention_reason` echoed, AND assert the mock Tier U / KB verifier `.lookup`/`.verify` were **never called** (pre-lookup guarantee). |
| **NEW** `test_chat_wrapper.py::test_not_checkworthy_suppressed_from_notes` | **new-needed** | `select_interventions` with one `verified` + one `no_grounding_found`/`not_checkworthy` ClaimVerdict → INTERVENE/PASS_THROUGH WITHOUT an ABSTAIN action for the not_checkworthy claim. |
| **NEW** `test_chat_wrapper.py::test_all_inert_draft_passes_through` | **new-needed** | All-not_checkworthy ClaimVerdicts → PASS_THROUGH (not DECLINE). |
| `tests/unit/test_triage.py` | optionally **new** `test_abstention_reason_enum_values` | pins the 5 enum values. |
| Live `derivation_corpus` / `extraction` corpora (Phase E) | **regression-only** | Re-run after WS4; the only behavioral delta is previously-dropped claims now abstain (cannot regress soundness — abstention is the conservative outcome). Confirm no claim that previously verified now abstains (it can't: well-formed claims keep `abstention_reason=None`). |

---

## (6) ORDERING / DEPENDENCIES

1. **`triage.py`**: add `AbstentionReason` enum first (no deps).
2. **`extractor.py`**: add `Claim.abstention_reason` field + restructure `_build_claim` (depends on 1). This is independently testable (new extractor tests).
3. **`walker.py`**: add walk-entry short-circuit guard (depends on 2 — `Claim` must have the field). Independently testable.
4. **`aggregator.py`**: **no code change** — already passes `abstention_reason` through. Verify only.
5. **`chat_wrapper.py`**: remove draft VERIFY-filter, change promotion filter, add not_checkworthy suppression (depends on 2+3 — needs the walker to actually produce `not_checkworthy` verdicts and the extractor to stamp the reason).
6. **`benchmark.py`**: change filter (depends on 2).
7. Tests: update breaking tests (`test_entity_not_in_text_is_dropped`) and add new tests in lockstep with each step.

**Cross-workstream dependencies to flag:**
- 4c.3 (stop collapsing `*_given_assertion` / conditional annotation) is **owned by Workstream 5** (Corrections+Conditional+Observability). WS4 should implement ONLY the `not_checkworthy` suppression in `select_interventions`; leave the `_given_assertion` collapse and `_format_*` edits to WS5 to avoid a merge conflict on `select_interventions`/`_format_correction`.
- 4d prompt trims (Rule 18/23/12-13 menus + `_RESIDENCE_VERB` regex deletion) are **owned by the substrate workstream** (Decision 1 multi-property predicate map). WS4 must NOT delete them; it documents the removal plan and the regression gates. Deleting before substrate value-type arbitration is wired reintroduces the §3.2 false-contradiction class documented at `extractor.py:570-578`.

---

## (7) RISKS / SOUNDNESS

1. **§3.2 (never false-verify) preserved for malformed triples.** The self_referential / predicate_eq_object claims previously avoided KB lookup by being dropped. 4b's **pre-lookup** short-circuit (at `walk()` entry, before `_direct_lookup`) preserves exactly that: these claims never reach `_tier_u.lookup` / `kb_verifier.verify` / the `flipped`/`oc_result` belief-revision paths. The new test asserting "lookup never called" pins this invariant. **Risk if mis-placed:** if the guard were placed inside the frontier loop AFTER `_direct_lookup`, it would be a soundness regression. The spec places it at walk entry — verify on review.

2. **Content-less-event removal (Deletion #2) is the highest-judgment item.** The old filter prevented a compound-verdict drag from `(WWII, occurred, '')`. With per-claim independent verdicts, an empty-object event claim now walks to `no_grounding_found` (empty object → no KB grounding; substrate expansion finds nothing) — abstention, the conservative outcome. The ONLY regression risk is if such a claim's empty object somehow routes to a contradiction; mitigated because (a) empty object → `triage` likely yields `not_checkworthy` (no named entity in object, depends on subject) → 4b short-circuits, OR (b) it reaches `_direct_lookup` and the KB verifier abstains on an unresolvable empty object. **Recommend** an explicit regression test walking `(World War II, occurred, "")` to confirm `no_grounding_found`, not `contradicted`.

3. **Cost of walking inert prose.** Removing the chat_wrapper VERIFY-filter (4c.1) means INERT_PROSE claims are walked. 4b short-circuits them with zero external lookups and zero LLM calls — so the added cost is one trace allocation + early return per inert claim. No budget impact. Verified against `walk()` budget accounting (the guard returns before the budget loop).

4. **All-inert draft must stay PASS_THROUGH.** Without the 4c.2 tally-skip, an all-inert draft (N≥2 inert claims, 0 verified) would hit `select_interventions`' `verified_count==0 and len(actions)>=2 → DECLINE` and spuriously refuse a benign chat turn. The not_checkworthy skip (excluding from both `actions` and `verified_count`) makes the effective set empty → `if not actions: PASS_THROUGH`. This is the single most important behavioral-equivalence check; pinned by the new `test_all_inert_draft_passes_through`.

5. **`AbstentionReason(str, Enum)` and existing string consumers.** Making it a `str` subclass means `aggregator.py:194` `"budget" in result.abstention_reason` and `:197` `== "circuit_breaker_triggered"` keep working when the value is `AbstentionReason.X.value` (a plain `str`). We store `.value` (plain str) on the `Claim`, not the enum member, to avoid any `Enum`-vs-`str` equality surprise in JSON serialization / DB writes. **Risk:** if any code does `isinstance(reason, str)` it still passes; if any does `is` comparison it would fail — grep shows only `==`/`in` comparisons. SAFE.

6. **Promotion gate tightening (4c.4).** Switching the user-message promotion filter from `triage==VERIFY` to `abstention_reason is None` additionally excludes the three *malformed* reasons from becoming Tier U premises. This is strictly safer (a malformed user assertion shouldn't be a premise) and equivalent for well-formed VERIFY claims. **Risk:** a user assertion that triages INERT_PROSE but the user genuinely wants recorded (e.g. "I like jazz" → preference) — but those route `user_authoritative` and triage VERIFY (predicate `likes` + named-entity-free object → actually INERT_PROSE today and already dropped by the old filter). So behavior is unchanged for the cases the old filter already dropped. No regression.

7. **Unused-import lint.** After removing `TriageDecision` from `chat_wrapper.py` and `benchmark.py`, a lint/CI step may flag if any stray reference remains. Post-edit grep on each file is required (spec calls it out in §2 row 6 and §4c.5).

**Net soundness posture:** every change moves a *silent drop* to an *observable abstention*. Abstention is the conservative verdict; no change can turn a prior `verified`/`no_grounding_found` into a `contradicted`. The only contradiction-risk vector (malformed triples reaching KB) is explicitly closed pre-lookup by 4b.


##########################################################################################
