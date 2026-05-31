# Aedos v0.16 — Change Specification, Part 0: Overview, Interface Contract, and Ordering

*This is the authoritative spine of the v0.16 change specification. The eight workstream documents (`01`–`08`) carry the excruciating per-file detail; this document owns the cross-workstream interface contract, the global ordering and dependency graph, the DB-migration plan, the soundness-sensitive deletion ordering, the test strategy, the LOC accounting, and the open decisions that need operator sign-off. Where a workstream document and this contract disagree, **this document wins** — the workstream specs were mapped in parallel and this is where their interfaces are reconciled.*

*Branch: `v0.16` (off `v0.16-synthesis`). **No implementation has happened.** This document set is produced for operator review per the directive: produce the spec, summarize it, and confirm before making any changes. Implementation (separate test-agents and code-agents, per the ordering below) follows confirmation.*

---

## 0.1 What v0.16 is, in one paragraph

v0.16 turns Aedos's substrate from a **scalar single-property** predicate map into a **multi-property predicate map discovered from Wikidata's own ontology**, with **evidence arbitration** at verify time (fixing the P31-vs-P106 class of error and killing the hardcoded `normalization._CANONICAL_MAP`); replaces the weak gated BFS with a **discover/verify composition** (liberal chain discovery, sound per-edge verification, `predicate_distribution` demoted from gate to ranker, a generalized SPARQL transitive-path primitive, premise-forward/bidirectional search, the depth-0 cap removed); builds the **partial truth-maintenance layer** (a lazy per-claim AND/OR provenance term, KB-grounded verdicts made retractable, a bounded nogood/exception cache, and lazy premise-retraction scoped to `*_given_assertion` verdicts); makes the extractor **verify every claim** (no silent drops — malformed shapes become quiet `abstention_reason` designations); **emits the corrected value** on contradictions and **surfaces conditional verdicts**; adds **observability** surfaces; and adds **granular temporal start/end claims**. The system stays functional throughout; migrations are additive; LOC is targeted to net-decrease (contingent — see §0.7).

## 0.2 How to read this set

- **`00` (this doc)** — contract, ordering, migrations, soundness ordering, tests, LOC, open decisions.
- **`01_substrate_predicate_map.md`** (WS1) — `PredicateBinding`, multi-property `PredicateMetadata`, Wikidata-ontology discovery, SLING fallback, evidence-arbitration `verify`, delete `_CANONICAL_MAP` + synonym seeds.
- **`02_composition.md`** (WS2) — discover/verify split, generalized `verify_transitive_path`, premise-forward search, gate→ranker, remove depth-0 cap.
- **`03_provenance_tms.md`** (WS3) — provenance term, D13 retractable KB verdicts, `substrate_exceptions` nogood cache, lazy premise-retraction, rewrite `retraction.py`/`contradiction_tracer.py`.
- **`04_verify_every_claim.md`** (WS4) — `AbstentionReason` enum, `Claim.abstention_reason`, no-None `_build_claim`, walker pre-lookup short-circuit, `not_checkworthy` quiet designation.
- **`05_corrections_observability.md`** (WS5) — `ClaimVerdict.contradicting_value`, emit corrected value, `fetch_label`, conditional verdicts, `trace_to_human`/`claim_observability`, endpoint surfaces.
- **`06_temporal.md`** (WS6) — start/end date-in-object claims, interval-from-events resolver, P580/P582 surfacing.
- **`07_tests.md`** (WS7) — test-impact inventory, test-agent assignment map (TA-1…TA-6 + TA-CAL), calibration-corpus discipline.
- **`08_deletions_migrations_ordering.md`** (WS8) — complete deletion list, migration plan, global ordering, LOC accounting, soundness-sensitive ordering.

## 0.3 The authoritative interface contract (the shared data model)

Every workstream conforms to these shapes. This resolves the one schema conflict the parallel mapping produced (the `substrate_exceptions` table — WS3's richer schema wins; see §0.4).

**`PredicateBinding`** (new dataclass, `predicate_translation.py`):
```python
@dataclass
class PredicateBinding:
    kb_namespace: Optional[str]
    kb_property: Optional[str]
    slot_to_qualifier: Optional[dict] = None
    single_valued: bool = False
    subject_entity_types: Optional[list[str]] = None
    object_entity_types: Optional[list[str]] = None
    source: str = "legacy_scalar"   # legacy_scalar | oracle | ontology_p2302 | sling
    rank: float = 1.0
```

**`PredicateMetadata`** keeps the predicate-level fields (`aedos_predicate`, `object_type`, `user_subject_required`, `distinct_slots`, `routing_hint`, `reason`, lifecycle) and gains `bindings: list[PredicateBinding]`. The scalar `kb_property` / `kb_namespace` / `slot_to_qualifier` / `single_valued` / `subject_entity_types` / `object_entity_types` become **read-only `@property` accessors returning `bindings[0]`'s values** so the ~18 existing scalar readers keep compiling unchanged. **Decision (binding for all workstreams):** because making these `@property` removes them from the dataclass `__init__`, add a `PredicateMetadata.from_scalars(...)` classmethod that accepts the legacy kwargs, builds one `PredicateBinding`, and sets `bindings=[that]`; convert the two internal constructors (`_generate_and_store`, `_row_to_metadata`) and the test constructors to it. This conversion **must land atomically with the dataclass change** or the suite won't import. (WS1 §1.1; risk S1-§7.)

**DB (owned by WS8 in `database.py`, additive + idempotent):**
- `predicate_translation` gains a `bindings TEXT` (JSON) column; legacy scalar columns are **retained** and read-synthesized when `bindings IS NULL`.
- New table `property_relations` (WS1 — cached Wikidata property ontology).
- New table `substrate_exceptions` (WS3 — bounded nogood cache; **WS3's schema is canonical**, §0.4).
- `TABLE_NAMES` gains both new tables.

**`JustificationTrace`** gains `provenance: ProvenanceTerm` (lazy AND/OR over premise literals); `chain_includes_assertion` becomes a **derived read-only `@property`** (`provenance.includes_assertion()`). New `ProvenanceLiteral` / `ProvenanceTerm` dataclasses (WS3 §3A). The five walker `chain_includes_assertion = True` writes convert to a `_record_premise(...)` helper.

**`Claim`** gains `abstention_reason: Optional[str] = None` (last field). The vocabulary is a `str`-subclass enum `AbstentionReason` in `triage.py` (WS4): `self_referential`, `predicate_eq_object`, `content_less_event`, `subject_absent_from_source`, `not_checkworthy`.

**`ClaimVerdict`** gains `contradicting_value: Optional[str] = None` (and `contradicting_value_type: Optional[str] = None`). Added **once** (coordination note: both WS3 and WS5 reference it; WS5 owns the field add + the `_format_correction` emission; WS3 only relies on it for observability).

**`kb_verifier.verify`** becomes a **binding loop with evidence arbitration**: iterate `meta.bindings`; per binding run resolve→lookup→compare; **VERIFIED if any binding grounds positively (record all verifying chains)**; **CONTRADICTED only from a `single_valued` binding whose value-type constraint the resolved object satisfies** (the P31-vs-P106 fix, via `_object_satisfies_value_type`), and only when no nogood vetoes it; else NO_MATCH/NO_KB_PATH. Operator decision confirmed: when multiple properties match positively, **examine/record both** — do not force a single winner.

**KB protocol additions:** `verify_transitive_path(source, target, relation_type, *, exception_cache=None)` (WS2 builds it; WS3 wires the nogood consult), `fetch_property_ontology(prop)` and `fetch_label(qid)` (WS1 declares; WS5 consumes `fetch_label`). All fail-open. Mock/stub adapters get them in the same change; consumers call optional ones via `getattr`.

## 0.4 Cross-workstream reconciliations (decisions this doc owns)

The parallel mapping produced a small number of overlaps; here is the binding resolution:

1. **`substrate_exceptions` schema — WS3 is canonical.** S1 sketched a simpler version; use WS3's (`exception_kind`, `relation_type`, `property_path`, `source_identifier`, `target_identifier`, `reason`, lifecycle, `UNIQUE(...)`), created in `database.py` by WS8/WS3, read/written by `SubstrateExceptionCache` (WS3 §3D), consulted by `verify_transitive_path` (WS2/WS3). WS1 references it only as the binding-loop's nogood gate.
2. **`property_relations` — WS1 is canonical** (cached ontology for binding discovery). WS8 creates the table; WS1 owns `PropertyRelations` + the query builders.
3. **`fetch_label` — declared once in WS1** (`kb_wikidata` + `kb_protocol`), consumed by WS5's `_format_correction`. Do not double-declare.
4. **`verify_transitive_path` — WS2 owns the primitive and the query-builder generalization; WS3 owns the `exception_cache` parameter and the nogood consult/record.** Land WS2's bool-returning method first; WS3 adds the cache wiring.
5. **`ClaimVerdict.contradicting_value` — WS5 owns the field + emission; WS3 relies on it read-only.** Add the field in the WS5 change.
6. **`contradicting_value` on the trace edge — walker change shared by WS3 and WS5.** The walker's CONTRADICTED branch (`walker.py:688-704`) stamps `contradicting_value` onto `TraceEdge.metadata`; both the provenance literal (WS3) and the aggregator's `_extract_contradicting_value` (WS5) read from there. One edit, two readers.
7. **D13 retractable KB verdicts — WS3 owns** the resolver `last_cache_row_id()` + `_TRACE_ROW_ID_KEYS` extension + walker edge stamping. WS1's `from_scalars` and WS5's observability both depend on it landing.
8. **Conditional-verdict annotation text — WS5 owns it.** WS4 only suppresses `not_checkworthy` from interventions; it sets no conditional annotation (avoids double-touching `_format_*`).
9. **Distribution oracle split (KB-transitivity vs intensional kind-entailment) — WS2 owns** the consumption change (gate→ranker) and the prompt trim; the oracle module itself is largely unchanged.

## 0.5 Global ordering and dependency graph

`X → Y` means X lands before Y. The system is green after every numbered step (additive-first).

```
Phase 0  WS8.DB migrations (bindings col, property_relations, substrate_exceptions, TABLE_NAMES)
            → unblocks everything; no behavior change.
Phase 1  WS1 data-model: PredicateBinding, PredicateMetadata.bindings + from_scalars + @property
            accessors; (coordinate) Claim.abstention_reason, ClaimVerdict.contradicting_value,
            JustificationTrace.provenance field + ProvenanceLiteral/Term. Additive; scalar readers
            keep working via read-synthesis. → WS2, WS4, WS5, WS6
Phase 2  WS1 substrate: property_relations discovery, SLING fallback, kb_verifier binding-loop
            arbitration. THEN delete _CANONICAL_MAP (D1) + synonym seed rows. → WS-composition, WS3
Phase 3  WS2 composition: verify_transitive_path primitive (then D3 geo bridge), discover/verify
            split, premise-forward + budget (then D4 depth cap), gate→ranker (D6, verify-side
            soundness FIRST), route subsumption via consult.
Phase 4  WS3 partial-TMS: provenance population, D13 retractable KB verdicts, substrate_exceptions
            nogood cache (THEN D2 geo-disjoint cluster can be deleted), rewrite retraction.py +
            contradiction_tracer.py (D9/D10), lazy premise-retraction.
Phase 5  WS5 corrections/observability: contradicting_value emission, conditional verdicts,
            trace_to_human + claim_observability + endpoint surfaces.
Phase 6  WS4 verify-every-claim (D7/D8, D5 persona — coordinate with WS2 routing) + WS6 temporal,
            parallelizable after Phase 1.
```

Compact dependency edges and deletion gates (from WS8 §c):
```
WS8.DB → WS1 → WS2 → WS-composition → WS3 → WS5 ; WS1 → WS4 ; WS1 → WS6
Deletions: D1←WS1-substrate ; D2←WS1+WS3+WS2 ; D3←WS2 ; D4←WS2(budget) ;
           D5←WS4+WS2(persona routing) ; D6←WS2(verify-side §3.2 first) ;
           D7,D8←WS4 ; D9,D10←WS5/WS3 (land lazy retraction in the SAME change).
```

## 0.6 DB migration plan (additive, idempotent, functional-throughout)

All migrations live in `database.py` `create_schema`, following the established guarded pattern (`try: ALTER ... ADD COLUMN except sqlite3.OperationalError: pass`; `CREATE TABLE IF NOT EXISTS`).

- **M1** `predicate_translation.bindings TEXT` — `_SCHEMA_SQL` CREATE + guarded ALTER after the D33/D47 loop, before `_maybe_load_seeds`. Legacy scalar columns retained; `bindings IS NULL` rows read-synthesize one binding.
- **M2** `property_relations` table + index (WS1).
- **M3** `substrate_exceptions` table + index (WS3 canonical schema).
- `TABLE_NAMES` += `property_relations`, `substrate_exceptions`.
- **No** new persistence for provenance, `abstention_reason`, or `contradicting_value` — these are session dataclasses (contract: provenance is lazy/discard-per-session). Observability rides the existing `audit_log.event_data` JSON. The retractable KB-verdict id rides the existing `entity_resolution_cache.id` (no new column). **This non-addition is deliberate** (WS8 §M4/M5).

Half-migrated functionality: old DB + new code → ALTER adds the column, legacy rows synthesize from scalars, new tables created empty (substrate falls back to cold discovery). New DB + old code (rollback) → ignores unknown columns/tables, reads scalars, CHECK constraints intact. Both functional.

## 0.7 LOC accounting and the contingency checkpoint

Deletions total ≈ **−358 lines** (WS8 §d): `_CANONICAL_MAP` (−58), geo cluster in `kb_verifier` (−110), geo bridge in `kb_wikidata` (−20 net), depth cap + comment (−11), persona guard (−35), distribution rubric+gate (−31), `_build_claim` drops (−5), `contradiction_tracer.py` (−88), retraction eager loop (−20). Gross additions across the feature workstreams are larger (~+690 if every feature lands as a pure addition).

**The honest position (WS8 §d):** net-LOC-down is achievable **only if WS2 truly replaces `kb_verifier.verify`'s scalar special-case ladder with the binding loop rather than layering a loop on top of it** (the verifier should net-shrink as the geo/disjoint/widening special-cases collapse). **Action: a mandatory LOC re-measurement checkpoint after WS2 lands.** If WS2 layers instead of replaces, net LOC goes up and the operator should be flagged. This is the single biggest lever on the LOC target and is called out as a decision point (§0.11).

## 0.8 Observability (cross-cutting requirement)

Per the operator's explicit requirement ("visibility is important; the user should observe what's going on"), every claim's **verdict + provenance + which bindings/paths were tried + the corrected value** must be inspectable. Implemented by WS5: `trace_to_human(trace)` (deterministic plain-text renderer), `claim_observability(vr)` (structured per-claim list), surfaced additively on `/chat` (an `observability` key) and `/verification/{id}` (a `claims` list). WS1/WS2 stamp `bindings_tried` / `discovery_source` / `paths_tried` onto trace-edge metadata; WS3's provenance term is the structured grounding record. The trace is **not** persisted across sessions (lazy/discard); it is observable within the process and via the audit log's `event_data`.

## 0.9 Soundness-sensitive ordering (the §3.2 never-false-verify guardrails)

These deletions/changes **must** be ordered after their replacements, or they reopen a false-verify/false-contradiction (WS8 §e):

- **SS1 (highest) — the Marie-Curie geo guards.** `CONTINENT_QIDS` / `_location_disjoint` (`kb_verifier`, D2) and `_GEO_REGION_TYPES` / type-guarded P361 bridge (`kb_wikidata`, D3) are the documented leak guards. Delete **only after** WS1 discovered object-types + WS3 nogood cache + WS2 generic transitive primitive land. Pin Warsaw⊄Germany, Rome⊄Germany, Thames⊄Asia, Vatican⊄Africa as regression tests.
- **SS2 — persona guard** (`_is_persona_subject`, D5): delete only after WS4 routes persona-subject claims to `user_authoritative` (never KB) — else "Asa → Asa King of Judah" false-contradiction reopens.
- **SS3 — distribution gate→ranker** (D6): the verify-side §3.2 enforcement (WS2 discover/verify split) must land **before** the gate is removed.
- **SS4 — `subject==object` / `predicate==object` filters MUST stay pre-lookup** (WS4): convert to `abstention_reason` + walker pre-lookup short-circuit; never let them reach a KB lookup (false-contradiction class).
- **SS5 — retraction cascade** (D9/D10): land WS3/WS5 lazy provenance retraction in the **same change** that removes the eager cascade — no gap (else a retracted Tier U premise leaves dependent `*_given_assertion` verdicts un-stale = false-verify-over-time).

Safe-at-any-time: D1 (`_CANONICAL_MAP`, soundness-neutral — a miss is a false-abstain), D4 (depth cap, cost-sensitive not soundness-sensitive — gate on the budget/bidirectional search landing).

## 0.10 Test strategy — code-agents and test-agents are separate (per operator)

Tests are owned by **separate agents from code**, partitioned so no test file is touched by two agents and no test-agent shares context with the code-agent for the same module. The assignment (WS7 §3):

- **TA-1** substrate/multi-property tests; **TA-2** kb-verifier/bindings-arbitration tests; **TA-3** composition/walker tests; **TA-4** TMS/provenance/retraction tests; **TA-5** verify-every-claim + corrections/observability/intervention tests; **TA-6** temporal tests; **TA-CAL** (single owner) all calibration corpora + the runner + thresholds + cold-start.

**Calibration-corpus discipline (TA-CAL, WS7 §4):** a corpus expectation changes **only** when behavior changed for a stated architectural reason — never to make a red bar green. Derivation abstain→verified flips require a trace-entity check (the intended path is in the trace). `kb_mapping`/`predicate_metadata` must pin the **evidence-arbitrated winning binding**, not "any binding in the set." Lowering any `THRESHOLDS` value to pass a regressed corpus is forbidden (guarded by `test_runbook_thresholds.py`).

The single biggest test lever (WS7 §2): **make the `bindings`-synthesizer accept the legacy flat `MockTransport` dict**, so the ~16 fixture files carrying the scalar dict don't all churn.

## 0.11 Open decisions for operator sign-off

The specs surfaced these genuine forks. Defaults are noted; flag any you'd change:

1. **Net-LOC checkpoint after WS2** (§0.7): accept that net-down is contingent on WS2 replacing (not layering) `kb_verifier.verify`, with a re-measure gate? *Default: yes, with a hard checkpoint.*
2. **Leak-guard nogood eviction (S3 §7):** exempt `reason='leak_guard'`/`'operator_marked'` rows from LRU eviction so a Marie-Curie guard can't be evicted? *Default: yes, exempt them.*
3. **Resolver-cache retraction scope (S3 §7):** premise-retraction marks `*_given_assertion` verdicts stale. Should retraction of an `entity_resolution_cache` row that fed a **base** KB verdict also stale it (the dependency is recorded either way), or stay strictly scoped to `*_given_assertion`? *Default: strict to `*_given_assertion` per the architecture text; the base-KB dependency is recorded for audit but not auto-staled.*
4. **Multi-property arbitration precedence (operator: "examine both"):** record all positive-matching bindings as alternative chains; CONTRADICTED only from a `single_valued` binding whose value-type the object satisfies. *Default: as stated; confirm a genuinely dual-typed claim surfaces both.*
5. **Observability volume (S5 §7):** put the full `trace_to_json` per claim on `/chat`, or only `trace_human` + summary on `/chat` and the full trace on `/verification/{id}`? *Default: light on `/chat`, full on `/verification/{id}`.*
6. **Candidate-count tunable:** the literature's N≈3–5 sweet spot vs your skepticism of an aggressive cap — make it a config tunable with a permissive default rather than a hard cap. *Default: config tunable, permissive.*

## 0.12 What happens after you confirm

On approval, implementation proceeds in the Phase 0→6 order (§0.5): code-agents implement each workstream against its spec doc; test-agents (TA-1…TA-CAL) update/author tests in parallel but in separate context; the full suite (`pytest tests/`) must be green after each phase; the LOC checkpoint runs after WS2; soundness-sensitive deletions (SS1–SS5) land only after their replacements with the regression pins in place. Each phase is a small commit on `v0.16`. Nothing is implemented until you say go.
