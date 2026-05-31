# Aedos v0.16 ? Change Specification: Workstream 5 ? Per-Claim Correction, Conditional Verdicts, Observability

*Implementation-ready spec. Conforms to the interface contract in `00_overview_contract_ordering.md`, which is AUTHORITATIVE on all cross-workstream interfaces. File:line references were verified by the spec-mapping pass against the v0.16 base (identical to `main` at branch creation).*

---

## DETAILED CHANGE SPEC
# WS5 Change Spec — Correction value, conditional verdicts, observability

All file:line references verified against the code read in full this session. WS5 depends on shapes from WS1/WS2 (PredicateBinding, bindings-tried list, KBVerdict trace enrichment) and WS3 (JustificationTrace.provenance). WS5 is the *consumer/surface* of those; where they are not yet present it degrades cleanly (None / absent keys). I flag every cross-workstream dependency inline.

---
## PART (a) — Carry the contradicting KB value onto the trace + grounding dict

### (a.1) walker.py CONTRADICTED branch — `_try_external_grounding`, lines 688-704

CURRENT (walker.py:688-704):
```
            elif kb_result.verdict == KBVerdictType.CONTRADICTED:
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("kb_statement", {"entity": kb_result.subject_kb_id}),
                    metadata={
                        "source": "kb", "verdict": "contradicted",
                        "lookup_inverted": kb_result.trace.get("lookup_inverted"),
                    },
                ))
                grounding = {
                    "source": "kb",
                    "entity": kb_result.subject_kb_id,
                    "kb_property": kb_result.trace.get("kb_property"),
                    "verdict": "contradicted",
                }
                return "contradicted", "kb", 0, grounding
```

The contradicting value lives at `kb_result.matched_statement.value` (KBVerdict.matched_statement is the `scope_mismatch` Statement on the CONTRADICTED path — see kb_verifier.py:299, 376; Statement.value at kb_protocol.py:41). On the no-statements subsumption fallback path matched_statement is None (kb_verifier.py:244), so guard for None.

ADDED — compute a `contradicting_value` and a `contradicting_value_type` and thread them onto BOTH the TraceEdge.metadata and the grounding dict. Replace the block with:
```
            elif kb_result.verdict == KBVerdictType.CONTRADICTED:
                matched = kb_result.matched_statement
                cv_raw = getattr(matched, "value", None) if matched is not None else None
                cv_type = getattr(matched, "value_type", None) if matched is not None else None
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("kb_statement", {"entity": kb_result.subject_kb_id}),
                    metadata={
                        "source": "kb", "verdict": "contradicted",
                        "lookup_inverted": kb_result.trace.get("lookup_inverted"),
                        # WS5(a): carry the contradicting KB value so the
                        # aggregator can populate ClaimVerdict.contradicting_value
                        # and the chat-wrapper can emit "the source indicates X".
                        "contradicting_value": cv_raw,
                        "contradicting_value_type": cv_type,
                        "kb_property": kb_result.trace.get("kb_property"),
                    },
                ))
                grounding = {
                    "source": "kb",
                    "entity": kb_result.subject_kb_id,
                    "kb_property": kb_result.trace.get("kb_property"),
                    "verdict": "contradicted",
                    "contradicting_value": cv_raw,
                    "contradicting_value_type": cv_type,
                }
                return "contradicted", "kb", 0, grounding
```
Role: `cv_type` (entity|literal|date|quantity from Statement.value_type) tells the chat-wrapper whether the value is a Q-id needing reverse-label lookup (entity) or passes through (date/quantity/literal).

### (a.2) walker.py kb_quantitative — `_verify_kb_quantitative`, lines 836-841 (value dropped)

CURRENT (walker.py:836-841): `_verify_kb_quantitative` returns ONLY a verdict string; `kb_value`/`threshold` computed at 834/758 are discarded. The caller (`_try_external_grounding`, walker.py:652-659) builds the grounding dict and increments source_breakdown but appends NO trace edge for the quantitative path — so there is no edge to carry the value, and the aggregator has nothing to read.

Two coordinated changes:

CHANGE 1 — `_verify_kb_quantitative` signature returns the comparison detail. Replace the return type and the three terminal returns:
- Line 739: `def _verify_kb_quantitative(self, claim, context, trace) -> Optional[str]:` → `-> Optional[tuple[str, dict]]:`
- Every `return None` (lines 760, 769, 776 area `if not meta.kb_property: return None`, 783, 799, 805, 807, 833) stays `return None` (no terminal verdict).
- Final return (line 840-841):
```
        verdict = "verified" if verified else "contradicted"
        detail = {
            "kb_value": kb_value,
            "threshold": threshold,
            "comparator": comparator,
            "kb_property": meta.kb_property,
        }
        return _apply_polarity_str(verdict, claim.polarity), detail
```

CHANGE 2 — caller at walker.py:652-659. CURRENT:
```
        if self._predicate_routing(node.predicate) == "kb_quantitative":
            verdict = self._verify_kb_quantitative(node, context, trace)
            if verdict is not None:
                trace.source_breakdown["kb"] = trace.source_breakdown.get("kb", 0) + 1
                return verdict, "kb_quantitative", 0, {
                    "source": "kb_quantitative",
                    "predicate": node.predicate,
                }
```
REPLACE with (unpack tuple, append a trace edge carrying value+threshold, and on contradicted carry contradicting_value):
```
        if self._predicate_routing(node.predicate) == "kb_quantitative":
            quant = self._verify_kb_quantitative(node, context, trace)
            if quant is not None:
                verdict, detail = quant
                trace.source_breakdown["kb"] = trace.source_breakdown.get("kb", 0) + 1
                edge_md = {
                    "source": "kb_quantitative",
                    "verdict": verdict,
                    "predicate": node.predicate,
                    "kb_value": detail.get("kb_value"),
                    "threshold": detail.get("threshold"),
                    "comparator": detail.get("comparator"),
                    "kb_property": detail.get("kb_property"),
                }
                if verdict == "contradicted":
                    # WS5(a): the KB value is the contradicting value for a
                    # numeric comparison ("source indicates 67000000").
                    edge_md["contradicting_value"] = detail.get("kb_value")
                    edge_md["contradicting_value_type"] = "quantity"
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("kb_statement", {"entity": node.subject}),
                    metadata=edge_md,
                ))
                grounding = {
                    "source": "kb_quantitative",
                    "predicate": node.predicate,
                    "kb_value": detail.get("kb_value"),
                    "threshold": detail.get("threshold"),
                }
                if verdict == "contradicted":
                    grounding["contradicting_value"] = detail.get("kb_value")
                    grounding["contradicting_value_type"] = "quantity"
                return verdict, "kb_quantitative", 0, grounding
```
Role: adds the missing observability edge for the quantitative path AND surfaces the contradicting numeric value. Note: pre-WS5 the quantitative path appended no edge — this is purely additive and improves observability.

### (a.3) walker.py belief-revision CONTRADICTED edges (polarity_conflict 472-485, object_conflict 516-529)

These are CONTRADICTED-from-Tier-U paths. The "contradicting value" here is the conflicting Tier U premise's object (polarity_conflict: the same S/P/O of opposite polarity; object_conflict: a different object on a functional predicate). For object_conflict the contradicting value is `oc_row.get("object")`. ADD to the object_conflict TraceEdge.metadata (walker.py:520-525):
```
                        "contradicting_value": oc_row.get("object"),
                        "contradicting_value_type": "literal",
```
For polarity_conflict (472-482) the contradicting premise asserts the SAME object with opposite polarity, so there is no distinct "instead" value — leave it without a contradicting_value (the correction text falls back to the generic form). This keeps §3.2 honest: we only emit "instead X" when there genuinely is a distinct X.

---
## PART (b) — ClaimVerdict.contradicting_value field + aggregator populates it

### (b.1) aggregator.py ClaimVerdict dataclass (lines 68-89)

ClaimVerdict is `@dataclass(frozen=True)`. ADD field after `abstention_reason` (line 89). Rewrite the deferral docstring (lines 80-85) to describe the now-live field:
```
@dataclass(frozen=True)
class ClaimVerdict:
    """... (keep existing description of claim_id/claim/verdict/abstention_reason) ...

    `contradicting_value` (WS5) is the KB/Tier-U value the source holds
    that contradicts a CONTRADICTED claim, extracted from the trace's
    contradicted premise_lookup edge (`metadata['contradicting_value']`).
    None when the verdict is not contradicted, or when the contradicted
    path carried no distinct value (e.g. polarity-conflict, or a
    subsumption-fallback contradiction). `contradicting_value_type` is the
    Statement value_type (entity|literal|date|quantity) so the chat-wrapper
    knows whether to reverse-label a Q-id."""
    claim_id: str
    claim: Claim
    verdict: str
    abstention_reason: Optional[str] = None
    contradicting_value: Optional[str] = None
    contradicting_value_type: Optional[str] = None
```
Frozen + defaulted fields are append-only and back-compat: all existing positional/kw constructions (test_chat_wrapper.py:57/61/65, test_corpus_runner.py:562, aggregator.py:172) keep working.

### (b.2) aggregator.py — extract the contradicting value from the trace

ADD a module-level helper near `_extract_source_rows` (after line 131):
```
def _extract_contradicting_value(trace: JustificationTrace) -> tuple[Optional[str], Optional[str]]:
    """WS5: pull the contradicting value (and its value_type) from the
    contradicted premise_lookup edge a CONTRADICTED verdict rests on.
    Returns (value_as_str, value_type) or (None, None). Scans edges for a
    premise_lookup whose metadata verdict == 'contradicted' and which
    carries a non-None 'contradicting_value'. First such edge wins
    (a CONTRADICTED walk short-circuits at the first contradiction —
    walker.py:371-372 — so there is at most one in practice)."""
    for edge in trace.edges:
        md = edge.metadata
        if md.get("verdict") != "contradicted":
            continue
        cv = md.get("contradicting_value")
        if cv is None:
            continue
        return (str(cv), md.get("contradicting_value_type"))
    return (None, None)
```

MODIFY the ClaimVerdict construction in `aggregate` (aggregator.py:172-177). CURRENT:
```
            claim_verdicts.append(ClaimVerdict(
                claim_id=cid,
                claim=claim,
                verdict=result.verdict,
                abstention_reason=result.abstention_reason,
            ))
```
REPLACE:
```
            cv_value, cv_value_type = (None, None)
            if base_verdict_of(result.verdict) == "contradicted":
                cv_value, cv_value_type = _extract_contradicting_value(result.trace)
            claim_verdicts.append(ClaimVerdict(
                claim_id=cid,
                claim=claim,
                verdict=result.verdict,
                abstention_reason=result.abstention_reason,
                contradicting_value=cv_value,
                contradicting_value_type=cv_value_type,
            ))
```
`base_verdict_of` is already defined in this module (aggregator.py:54) — collapses `contradicted_given_assertion` too. Import is local (same module). Role: only do the trace scan for contradicted-family verdicts (cheap guard).

---
## PART (c) — _format_correction emits the corrected value (reverse-label Q-ids)

### (c.1) kb_wikidata.py — add fetch_label(qid)

No `fetch_label` exists. The action API `wbgetentities` with `props=labels` returns labels; `_fetch_p31_for_candidates` (kb_wikidata.py:1168) is the template for the wbgetentities call shape. ADD to the KBProtocol-public method area (after `enumerate_neighbors`, line ~743) AND to the KBProtocol Protocol (kb_protocol.py:55-94) as an optional method. Implementation:

In WikidataAdapter, public dispatcher (mirrors resolve/lookup pattern at 586-605):
```
    def fetch_label(self, qid: KBEntityID) -> Optional[str]:
        """WS5: reverse-resolve a Wikidata Q-id to its English label, for
        rendering a contradicting entity value in user-facing corrections
        ('the source indicates {label} instead'). Returns None on miss /
        error / non-Q input. Fixture path reads labels_<qid>.json; live
        path calls wbgetentities props=labels. Cached at HTTP layer with
        the entity TTL. Never raises."""
        if not isinstance(qid, str) or not _ENTITY_ID_PATTERN.match(qid):
            return None
        if self._live:
            return self._live_fetch_label(qid)
        return self._fixture_fetch_label(qid)
```
Fixture impl:
```
    def _fixture_fetch_label(self, qid: KBEntityID) -> Optional[str]:
        try:
            data = _load_fixture(f"labels_{qid}.json")
        except FixtureNotFoundError:
            return None
        return (
            data.get("entities", {}).get(qid, {})
            .get("labels", {}).get("en", {}).get("value")
        )
```
Live impl (modeled on _fetch_p31_for_candidates 1196-1228 + _live_resolve audit shape):
```
    def _live_fetch_label(self, qid: KBEntityID) -> Optional[str]:
        if self._http is None:
            raise RuntimeError(
                "WikidataAdapter._live_fetch_label requires an http_cache"
            )
        url = self._cfg_value("wikidata_search_endpoint", _DEFAULT_SEARCH_ENDPOINT)
        ttl = self._cfg_value("http_cache_entity_ttl_seconds", _DEFAULT_ENTITY_TTL_SECONDS)
        params = {
            "action": "wbgetentities", "ids": qid,
            "props": "labels", "languages": "en", "format": "json",
        }
        label: Optional[str] = None
        last_error: Optional[str] = None
        for attempt in range(2):
            self._search_limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    time.sleep(_RETRY_BACKOFF_SECONDS); continue
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"; break
            if isinstance(data, dict):
                label = (
                    data.get("entities", {}).get(qid, {})
                    .get("labels", {}).get("en", {}).get("value")
                )
            last_error = None; break
        self._log_audit_event(
            event_type="kb_fetch_label", event_subject=qid,
            event_data={"label": label, "error": last_error},
        )
        return label
```
ADD to KBProtocol (kb_protocol.py): since it's `@runtime_checkable` Protocol, add `def fetch_label(self, qid: KBEntityID) -> Optional[str]: ...`. NOTE: runtime_checkable isinstance checks only check method NAME presence — the StubKB classes in tests (test_chat_wrapper.py:339, test_kb_path fixtures) do NOT implement fetch_label. To avoid breaking `isinstance(kb, KBProtocol)` checks (if any) and to keep mocks working, the chat-wrapper MUST call fetch_label via `getattr(kb, "fetch_label", None)` (see c.2), not assume presence. Recommend NOT adding to the Protocol's required set if any isinstance gate exists; grep showed no isinstance(KBProtocol) in src, so adding is safe, but the getattr guard in the consumer is the robust choice.

### (c.2) chat_wrapper.py — reverse-label + emit value in _format_correction

The chat-wrapper currently has NO KB handle. `_format_correction` (chat_wrapper.py:78-87) is a module-level function taking only `cv`. To reverse-label entity Q-ids it needs a label resolver. Decision: thread an optional `label_fetcher: Optional[Callable[[str], Optional[str]]]` through `select_interventions` → `_format_correction`, sourced from the KB adapter in ChatWrapper.respond. This keeps the pure functions testable.

CHANGE the ChatWrapper constructor (chat_wrapper.py:175-196) to accept and store a `kb` (the WikidataAdapter, which build_pipeline already constructs — pipeline exposes it; see app.py wiring in part e). Store `self._kb = kb`.

REWRITE `_format_correction` (78-87):
```
def _format_correction(cv: ClaimVerdict, label_fetcher=None) -> str:
    """Correction annotation for a contradicted claim. When the trace
    carried a contradicting value (cv.contradicting_value), emit
    '... the source indicates {value} instead.' Entity Q-ids are
    reverse-labeled via label_fetcher (cv.contradicting_value_type ==
    'entity'); dates/quantities/literals pass through. Falls back to the
    generic form when no value was captured."""
    polarity_marker = "" if cv.claim.polarity == 1 else " (negated)"
    base = (
        f"Aedos found a contradicting source for: "
        f"{cv.claim.subject} {cv.claim.predicate} {cv.claim.object}{polarity_marker}"
    )
    value = cv.contradicting_value
    if value:
        display = value
        if cv.contradicting_value_type == "entity" and isinstance(value, str) \
                and value.startswith("Q") and label_fetcher is not None:
            try:
                label = label_fetcher(value)
            except Exception:
                label = None
            if label:
                display = label
        return f"{base}; the source indicates {display} instead."
    return f"{base}."
```
Role: §3.2-safe — only emits "instead X" when a value was captured; reverse-label failure degrades to the raw Q-id (still informative), never crashes.

THREAD label_fetcher through `select_interventions` (101-156). Change signature:
```
def select_interventions(claim_verdicts, label_fetcher=None) -> InterventionPlan:
```
At the CORRECT branch (139-144) pass it:
```
                annotation=_format_correction(cv, label_fetcher=label_fetcher),
```

At the call site in respond (chat_wrapper.py:288):
```
        label_fetcher = getattr(self._kb, "fetch_label", None)
        plan = select_interventions(vr.claim_verdicts, label_fetcher=label_fetcher)
```
The corpus runner (test_corpus_runner.py:565) and unit tests (test_chat_wrapper.py) call `select_interventions(cvs)` with no fetcher — default None keeps them passing; those synthetic ClaimVerdicts have no contradicting_value so they hit the generic-form fallback (test assertions at test_chat_wrapper.py:286 "Aedos found a contradicting source for: Obama" remain a prefix match — VERIFY: line 286 asserts the literal with no trailing period in the substring, and line 281 constructs an annotation ending in a period; the build_response tests pass literal annotations directly so they are unaffected; the select_interventions-driven tests at 156-255 only assert action_type/counts, not annotation text — SAFE).

---
## PART (d) — Conditional verdicts: stop collapsing *_given_assertion at the user surface

### (d.1) chat_wrapper.py select_interventions — emit conditional annotation

CURRENT (135-150): `base = base_verdict_of(cv.verdict)` collapses the dual, so `verified_given_assertion` becomes `verified` → counts as verified, no action. `contradicted_given_assertion` → CORRECT, `abstained_given_assertion` → ABSTAIN, all with the assertion-source qualifier ERASED. The contract: STOP collapsing for the user surface; emit a conditional annotation.

UX DECISION (concrete):
- `verified_given_assertion`: currently silent pass-through. New: emit a NEW ClaimActionType.CONFIRM_CONDITIONAL action with annotation "Aedos verified {S} {P} {O}, contingent on your assertion that it holds (no independent source confirms it)." This makes the conditional nature VISIBLE per operator's observability requirement, while NOT treating it as a problem (it does not trigger DECLINE — see policy below).
- `contradicted_given_assertion`: CORRECT action, annotation appended with " (this contradiction rests on your own prior assertion)."
- `abstained_given_assertion`: ABSTAIN action, annotation appended with " (and your assertion alone is not independent grounding)."

ADD to ClaimActionType enum (chat_wrapper.py:37-42):
```
    CONFIRM_CONDITIONAL = "confirm_conditional"
```
NOTE: this widens the API enum — app.py:115 test asserts action_type ∈ ("correct","abstain") (test_chat_endpoint.py:115). That test MUST be updated to include "confirm_conditional" (see affected_tests).

ADD an `is_given_assertion` import (aggregator.py:63 exports it) to chat_wrapper.py:12-16 import block:
```
from ..layer5_result.aggregator import (
    ClaimVerdict, VerificationResult, base_verdict_of, is_given_assertion,
)
```

REWRITE the select_interventions loop body (134-150):
```
    for cv in claim_verdicts:
        base = base_verdict_of(cv.verdict)
        conditional = is_given_assertion(cv.verdict)
        if base == "verified":
            verified_count += 1
            if conditional:
                # Surface the conditional verification (observability):
                # a *_given_assertion verified claim is NOT independently
                # grounded; show it but don't treat it as a problem.
                actions.append(ClaimAction(
                    claim_id=cv.claim_id,
                    action_type=ClaimActionType.CONFIRM_CONDITIONAL,
                    annotation=_format_conditional(cv),
                ))
            continue
        if base == "contradicted":
            actions.append(ClaimAction(
                claim_id=cv.claim_id,
                action_type=ClaimActionType.CORRECT,
                annotation=_format_correction(cv, label_fetcher=label_fetcher)
                    + (" (this contradiction rests on your own prior assertion)" if conditional else ""),
            ))
        else:
            actions.append(ClaimAction(
                claim_id=cv.claim_id,
                action_type=ClaimActionType.ABSTAIN,
                annotation=_format_abstention(cv)
                    + (" (your assertion alone is not independent grounding)" if conditional else ""),
            ))
```
ADD `_format_conditional`:
```
def _format_conditional(cv: ClaimVerdict) -> str:
    polarity_marker = "" if cv.claim.polarity == 1 else " (negated)"
    return (
        f"Aedos verified, contingent on your assertion that it holds "
        f"(no independent source confirms it): "
        f"{cv.claim.subject} {cv.claim.predicate} {cv.claim.object}{polarity_marker}."
    )
```

### (d.2) DECLINE/PASS_THROUGH policy adjustment

CURRENT (152-156): `if not actions: PASS_THROUGH; if verified_count==0 and len(actions)>=2: DECLINE; else INTERVENE`. With (d.1), a draft of only `verified_given_assertion` claims now produces CONFIRM_CONDITIONAL actions (previously zero actions → PASS_THROUGH). We must NOT regress those to INTERVENE-with-bullets-only or DECLINE. Fix the policy to ignore CONFIRM_CONDITIONAL actions when deciding DECLINE, and to keep them out of the "problematic" tally:
```
    if not actions:
        return InterventionPlan(InterventionType.PASS_THROUGH)
    problematic = [a for a in actions
                   if a.action_type in (ClaimActionType.CORRECT, ClaimActionType.ABSTAIN)]
    if not problematic:
        # only conditional confirmations — surface them via INTERVENE notes
        # (visibility), never DECLINE on a draft we conditionally verified.
        return InterventionPlan(InterventionType.INTERVENE, tuple(actions))
    if verified_count == 0 and len(problematic) >= 2:
        return InterventionPlan(InterventionType.DECLINE)
    return InterventionPlan(InterventionType.INTERVENE, tuple(actions))
```
SOUNDNESS/REGRESSION NOTE: test_chat_wrapper.py uses `_make_claim_verdicts` which builds BASE verdicts only (verified/contradicted/no_grounding_found — line 57/61/65), never *_given_assertion, so `conditional` is always False there → no new actions, behavior identical → those tests (140-255) PASS unchanged. The dedicated given_assertion tests (239-255) assert action_type for contradicted/abstained duals (CORRECT/ABSTAIN) which still hold; the verified_given_assertion case is at 239-243 — VERIFY: line 242 asserts `plan.overall == PASS_THROUGH` for a single `verified_given_assertion`. THIS WILL BREAK: under new policy it becomes INTERVENE with one CONFIRM_CONDITIONAL. This test MUST be updated (see affected_tests) — it is the intended behavior change per the contract ("STOP collapsing at the user boundary; emit a conditional annotation").

---
## PART (e) — Observability: trace_to_human + structured surface through /chat and /verification/{id}

### (e.1) trace.py — trace_to_human renderer + extend trace_to_json

trace.py:40-60 has `trace_to_json`. WS3 will add `provenance` to JustificationTrace; WS1/WS2 will add a bindings-tried record. WS5 surfaces them. ADD `provenance` and `bindings_tried`/`paths_tried` to trace_to_json output (guarded with getattr so it works before WS3/WS1 land):
```
    return {
        "root": _node(trace.root),
        "edges": [_edge(e) for e in trace.edges],
        "polarity_trace": trace.polarity_trace,
        "source_breakdown": trace.source_breakdown,
        "walk_metadata": trace.walk_metadata,
        "chain_includes_assertion": trace.chain_includes_assertion,
        "provenance": getattr(trace, "provenance", None),  # WS3
    }
```

ADD `trace_to_human(trace, *, claim=None, verdict=None) -> str` — a deterministic plain-text renderer:
```
def trace_to_human(trace: JustificationTrace, *, claim=None, verdict=None) -> str:
    """WS5 observability: render a justification trace as inspectable
    plain text. Lists the claim, final verdict, each premise_lookup /
    subsumption / kb_neighbor edge with its source + key metadata
    (verdict, contradicting_value, kb_property, premise_status,
    bindings/paths tried), the provenance term (WS3), and the
    source_breakdown. Pure/deterministic; no I/O."""
    lines: list[str] = []
    root = trace.root.content if trace.root else {}
    subj = root.get("subject"); pred = root.get("predicate"); obj = root.get("object")
    lines.append(f"Claim: {subj} {pred} {obj} (polarity={root.get('polarity')})")
    if verdict is not None:
        lines.append(f"Verdict: {verdict}")
    if getattr(trace, "chain_includes_assertion", False):
        lines.append("Note: chain includes an unverified user assertion (conditional).")
    for i, e in enumerate(trace.edges, 1):
        md = e.metadata
        parts = [f"[{i}] {e.edge_type} via {md.get('source', '?')}"]
        if md.get("verdict"): parts.append(f"verdict={md['verdict']}")
        if md.get("kb_property"): parts.append(f"property={md['kb_property']}")
        if md.get("contradicting_value") is not None:
            parts.append(f"source_value={md['contradicting_value']}")
        if md.get("premise_status"): parts.append(f"premise={md['premise_status']}")
        if md.get("relation_type"):
            parts.append(f"{md['relation_type']}/{md.get('direction','')}")
        if md.get("bindings_tried"): parts.append(f"bindings_tried={md['bindings_tried']}")  # WS1/WS2
        lines.append("  " + " ".join(parts))
    prov = getattr(trace, "provenance", None)
    if prov is not None:
        lines.append(f"Provenance: {prov}")
    if trace.source_breakdown:
        lines.append(f"Sources: {trace.source_breakdown}")
    return "\n".join(lines)
```

### (e.2) chat_wrapper.py — expose a structured per-claim observability view on ChatResponse

ADD a method to build the inspectable structure from a VerificationResult (used by both /chat optional field and /verification). Put it module-level so app.py can call it too:
```
def claim_observability(vr: VerificationResult) -> list[dict]:
    """WS5: structured, inspectable per-claim view — verdict, abstention
    reason, contradicting value, provenance, bindings/paths tried, and a
    human-readable trace rendering."""
    from ..layer5_result.trace import trace_to_json, trace_to_human
    out: list[dict] = []
    for cv in vr.claim_verdicts:
        trace = vr.per_claim_traces.get(cv.claim_id)
        out.append({
            "claim_id": cv.claim_id,
            "subject": cv.claim.subject,
            "predicate": cv.claim.predicate,
            "object": cv.claim.object,
            "polarity": cv.claim.polarity,
            "verdict": cv.verdict,
            "base_verdict": base_verdict_of(cv.verdict),
            "conditional": is_given_assertion(cv.verdict),
            "abstention_reason": cv.abstention_reason,
            "contradicting_value": cv.contradicting_value,
            "contradicting_value_type": cv.contradicting_value_type,
            "provenance": getattr(trace, "provenance", None) if trace else None,
            "trace": trace_to_json(trace) if trace else None,
            "trace_human": trace_to_human(trace, claim=cv.claim, verdict=cv.verdict) if trace else None,
        })
    return out
```

### (e.3) app.py — surface through endpoints

POST /chat (app.py:136-152): ADD an `observability` key to the JSON body (keeps existing keys; additive):
```
        "observability": claim_observability(response.verification_result),
```
Import at top of the chat handler block or module: `from aedos.deployment.chat_wrapper import claim_observability` (alongside the existing lazy `ChatWrapper` import at app.py:112).

GET /verification/{id} (app.py:155-166): ADD the observability surface (this is the deeper inspection endpoint per the operator's "should observe what is going on"):
```
    return JSONResponse({
        "verification_id": verification_id,
        "per_claim_verdicts": vr.per_claim_verdicts,
        "aggregate_metadata": vr.aggregate_metadata,
        "claims": claim_observability(vr),
    })
```
test_chat_endpoint.py:137-138 asserts `aggregate_metadata` and `per_claim_verdicts` present — still present (additive). test_chat_endpoint.py:110-116 asserts per_claim_actions shape and action_type ∈ ("correct","abstain") — MUST widen to include "confirm_conditional" (affected_tests).

---
## ORDERING WITHIN WS5
1. kb_wikidata fetch_label (c.1) + kb_protocol Protocol method — independent, no consumers yet.
2. walker.py value-carrying (a.1, a.2, a.3) — produces trace metadata.
3. aggregator ClaimVerdict fields + _extract_contradicting_value (b.1, b.2) — reads (2).
4. trace_to_human + trace_to_json provenance (e.1) — independent renderer.
5. chat_wrapper _format_correction/_format_conditional/select_interventions/constructor/claim_observability (c.2, d.1, d.2, e.2) — reads (3),(4),(1).
6. app.py endpoints + ChatWrapper kb wiring (e.3) — reads (5).
7. Test updates last.

## DELETIONS
- chat_wrapper.py:80-85 — the ClaimVerdict deferral docstring paragraph ('contradicting_value is deferred to v0.16...') is deleted/rewritten; safe because WS5 implements the field it described as deferred.
- aggregator.py:80-85 — the ClaimVerdict.contradicting_value deferral docstring paragraph is deleted/rewritten to describe the now-live field; safe, docstring-only.
- walker.py:739 return-type `-> Optional[str]:` for _verify_kb_quantitative — deleted/changed to `-> Optional[tuple[str, dict]]:`; the only caller is _try_external_grounding (walker.py:652-659), updated in the same change, so no orphaned consumer.
- No code deletions of live logic — WS5 is additive at the data-model and surface level (LOC net change is small-positive for WS5 alone; the contract's net-decrease target is met by WS1/WS3 deletions, not WS5).

## ADDITIONS
- kb_wikidata.py — WikidataAdapter.fetch_label(qid) dispatcher + _fixture_fetch_label + _live_fetch_label (reverse Q-id→English label via wbgetentities props=labels; fixture reads labels_<qid>.json). Role: reverse-label for corrections. New audit event 'kb_fetch_label'.
- kb_protocol.py — KBProtocol.fetch_label(self, qid) -> Optional[str] Protocol method (optional; consumers call via getattr). Role: protocol parity.
- walker.py — contradicting_value/contradicting_value_type carried onto the CONTRADICTED kb premise_lookup edge metadata + grounding dict (a.1); kb_quantitative now returns (verdict, detail) tuple and appends an observability premise_lookup edge carrying kb_value/threshold/comparator and contradicting_value on contradiction (a.2); object_conflict edge carries contradicting_value=oc_row.object (a.3). Role: stop dropping the computed contradicting value.
- aggregator.py — ClaimVerdict gains contradicting_value + contradicting_value_type fields (frozen, defaulted); module-level _extract_contradicting_value(trace); aggregate() populates the new fields for contradicted-family verdicts. Role: plumb the value from trace to verdict.
- trace.py — trace_to_human(trace, *, claim, verdict) plain-text renderer; trace_to_json gains a 'provenance' key (getattr-guarded for WS3). Role: human-readable + structured trace surface.
- chat_wrapper.py — ClaimActionType.CONFIRM_CONDITIONAL enum member; _format_conditional(cv); _format_correction rewritten to take label_fetcher and emit 'the source indicates X instead'; select_interventions gains label_fetcher param + conditional-annotation logic + revised DECLINE policy; ChatWrapper.__init__ stores self._kb; respond() passes label_fetcher; module-level claim_observability(vr). Role: conditional UX + correction value + observability surface.
- app.py — /chat body gains 'observability'; /verification/{id} body gains 'claims' (claim_observability). ChatWrapper construction (app.py:122-132) gains kb=pipeline.kb wiring. Role: expose observability through the deployment.

## CALL SITES / CONSUMERS
- chat_wrapper.py:135 base_verdict_of(cv.verdict) — modified in-place (d.1) to also branch on is_given_assertion.
- chat_wrapper.py:288 select_interventions(vr.claim_verdicts) — updated to pass label_fetcher (c.2).
- aggregator.py:172-177 ClaimVerdict(...) construction — updated to pass contradicting_value/type (b.2).
- tests/calibration/test_corpus_runner.py:562,565 — ClaimVerdict(...) + select_interventions(cvs): both back-compat (new fields default None; label_fetcher defaults None). Synthetic verdicts are base-only, no conditional path. SAFE, no change required (but the intervention corpus may need new conditional cases — out of WS5 minimal scope).
- tests/unit/test_chat_wrapper.py:57,61,65 ClaimVerdict(...) constructions — back-compat via defaults. SAFE.
- tests/unit/test_chat_wrapper.py:239-243 — select_interventions on a single verified_given_assertion asserts overall==PASS_THROUGH. WILL BREAK under d.1/d.2 (now INTERVENE+CONFIRM_CONDITIONAL). Needs-update — intended behavior change.
- tests/integration/test_chat_endpoint.py:112-116 — asserts per_claim_actions[].action_type ∈ ('correct','abstain'). Needs-update to include 'confirm_conditional'.
- app.py:142-150 per_claim_actions serialization — unchanged shape, but action_type may now be 'confirm_conditional'; consumers of the /chat body must tolerate the third value (test above).
- tests/integration/test_chat_endpoint.py:137-138 — /verification asserts aggregate_metadata + per_claim_verdicts present; both retained (additive). SAFE.
- kb_verifier.py KBVerdict.matched_statement (kb_verifier.py:78,299,376) — read by walker a.1 via kb_result.matched_statement.value; matched_statement is None on the subsumption-fallback CONTRADICTED-via-polarity path (kb_verifier.py:244) — guarded with getattr/None check.
- tests/integration/test_kb_path.py:139-143 + tests/unit/test_kb_verifier.py:140-145 — assert matched_statement / .value; unaffected (read-only, no shape change to KBVerdict). SAFE.
- tests/unit/test_trace.py + test_walker.py:355 + test_python_path.py:146 + test_walker_with_substrate.py:199 + test_aggregator.py:337 — call trace_to_json; new 'provenance' key is additive; existing key assertions unaffected. SAFE (verify none assert exact dict equality — they assert key presence).
- pipeline.build_pipeline — must expose .kb (the WikidataAdapter) for app.py to thread into ChatWrapper(kb=...). VERIFY pipeline exposes kb; if not, add it (cross-check with WS that owns pipeline.py).

## AFFECTED TESTS
- tests/unit/test_chat_wrapper.py::TestSelectInterventions verified_given_assertion case (~line 239-243): needs-update — single verified_given_assertion now yields INTERVENE + CONFIRM_CONDITIONAL action instead of PASS_THROUGH.
- tests/integration/test_chat_endpoint.py::test_post_chat_returns_per_claim_actions (line 112-116): needs-update — action_type set widened to include 'confirm_conditional'.
- tests/unit/test_chat_wrapper.py::TestBuildResponse + _format_correction text tests: needs-update/new-test-needed — add assertions that _format_correction emits 'the source indicates X instead' when ClaimVerdict.contradicting_value is set, and reverse-labels an entity Q-id via a stub label_fetcher; existing generic-form tests still pass (no value → generic).
- tests/unit/test_aggregator.py::TestClaimVerdictsField (line 64-82): new-test-needed — assert aggregate populates contradicting_value/contradicting_value_type from a trace with a contradicted edge carrying contradicting_value; assert None for non-contradicted.
- tests/unit/test_trace.py: new-test-needed — trace_to_human renders claim/verdict/edges/source_value/provenance deterministically; trace_to_json includes 'provenance' key.
- tests/unit/test_walker.py / tests/integration/test_kb_path.py: new-test-needed — walker CONTRADICTED kb edge metadata carries contradicting_value == matched_statement.value; kb_quantitative contradicted carries kb_value.
- tests/integration/test_chat_endpoint.py::test_get_verification_returns_metadata (line 132-138): needs-update/new-test-needed — assert new 'claims' observability list is present and each entry has verdict/trace_human.
- tests/unit/test_kb_verifier.py or tests/unit/test_wikidata_adapter.py: new-test-needed — fetch_label fixture path returns label for labels_<qid>.json, None on miss/non-Q input.
- tests/calibration intervention corpus: will-break-risk — none of the 30 existing cases use *_given_assertion or contradicting_value, so the corpus passes unchanged; optionally add conditional-verdict cases (out of minimal WS5 scope, flag for operator).

## ORDERING / DEPENDENCIES
- Within WS5: (1) kb_wikidata.fetch_label + kb_protocol method → (2) walker value-carrying → (3) aggregator ClaimVerdict fields/extractor → (4) trace_to_human/trace_to_json provenance → (5) chat_wrapper correction+conditional+observability+constructor → (6) app.py endpoints+kb wiring → (7) tests.
- DEPENDS ON WS1/WS2: the kb_verifier.verify loop over PredicateBinding (WS1) must continue to set KBVerdict.matched_statement to the contradicting Statement on a single_valued CONTRADICTED (the contract says CONTRADICTED only from a single_valued binding); WS5's a.1 reads matched_statement.value, so WS1 must populate it. WS5 also reads an optional edge metadata 'bindings_tried' (WS1/WS2 should write the candidate bindings/paths tried onto the trace for observability); WS5 renders it if present, tolerates absence.
- DEPENDS ON WS3: JustificationTrace.provenance — WS5's trace_to_json/trace_to_human/claim_observability read it via getattr and tolerate absence, so WS5 can land before WS3, but the observability surface is incomplete until WS3 adds provenance.
- DEPENDS ON pipeline (verify): app.py threads kb=pipeline.kb into ChatWrapper; confirm build_pipeline exposes .kb (the WikidataAdapter). If WS1 already wires a kb handle into the walker/verifier, reuse the same instance.
- PROVIDES TO no other workstream (WS5 is the terminal surface), except the ClaimActionType.CONFIRM_CONDITIONAL enum value and claim_observability shape are part of the deployment API any consumer reads.

## RISKS / SOUNDNESS
- §3.2 never-false-verify preserved: _format_correction only emits 'the source indicates X instead' when a contradicting_value was actually captured from a CONTRADICTED edge; polarity_conflict (same-object, opposite polarity) deliberately carries NO contradicting_value, so we never invent a spurious 'instead' value. Subsumption-fallback CONTRADICTED (matched_statement=None) → contradicting_value=None → generic form.
- CONFIRM_CONDITIONAL must NOT escalate to DECLINE (d.2 policy): a draft of only conditionally-verified claims is surfaced via INTERVENE notes (visibility) not refused — verified_given_assertion is a (conditional) verification, not a problem. The revised policy tallies only CORRECT/ABSTAIN as 'problematic'. Risk: if a future caller treats every INTERVENE as 'draft has problems', conditional confirmations would be miscategorized — mitigated by the distinct action_type.
- Reverse-label failure must degrade, never crash or block: label_fetcher wrapped in try/except, falls back to raw Q-id; fetch_label itself never raises (returns None on error). A live Wikidata outage during correction rendering yields a Q-id in the note, not a 500.
- Frozen-dataclass append-only: ClaimVerdict new fields are defaulted and appended after existing fields, so all positional and kw constructions across tests/runner keep working — verified against the 6 construction sites.
- Observability volume: claim_observability embeds full trace_to_json per claim into /chat responses — for many-claim drafts this is large. Mitigation: /chat could carry only trace_human + verdict summary and reserve full trace_to_json for /verification/{id}; operator decision flagged. Functional-throughout: additive JSON keys, no removed keys, so existing /chat and /verification consumers (tests) keep working.
- kb_quantitative previously emitted NO trace edge; adding one increments source_breakdown['kb'] (already incremented at 655) — VERIFY we don't double-count: current code increments at 655 then returns; new code increments once and appends an edge. Keep the single increment. Aggregator source_breakdown sums per-edge-independent counter, so one increment is correct.
- runtime_checkable KBProtocol: adding fetch_label to the Protocol does not break existing isinstance checks (none found in src) but mock KBs lacking the method are handled via getattr in the consumer — do NOT call kb.fetch_label directly anywhere.
- Net-LOC: WS5 adds ~150 lines (renderer + fetch_label + observability). The contract's net-decrease is a whole-v0.16 target met by WS1 (_CANONICAL_MAP + ~50 alias rows) and WS3 (retraction rewrite) deletions; WS5 is justified additive observability the operator explicitly required ('visibility is important').


==========================================================================================
