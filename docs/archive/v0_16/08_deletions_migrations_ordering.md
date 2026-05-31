# Aedos v0.16 ? Change Specification: Workstream 8 ? Deletions, DB Migrations, Global Ordering, LOC

*Implementation-ready spec. Conforms to the interface contract in `00_overview_contract_ordering.md`, which is AUTHORITATIVE on all cross-workstream interfaces. File:line references were verified by the spec-mapping pass against the v0.16 base (identical to `main` at branch creation).*

---

## DETAILED CHANGE SPEC
============================================================
WORKSTREAM 8 — DELETIONS, DB MIGRATIONS, GLOBAL ORDERING, LOC
============================================================

All paths are absolute. All line numbers verified against the current tree read in full this session. Note up front: WS8 itself OWNS only the DB migrations (C:/code/aedos/src/aedos/database.py) and the cross-workstream coordination. The deletions below are EXECUTED by the workstream that lands the replacement; WS8 is the inventory + ordering authority and the place the operator looks to confirm net-LOC-down and no orphaned consumer. Each deletion lists which workstream performs it.

CRITICAL CORRECTION TO THE CONTRACT'S MENTAL MODEL (verified against code):
- normalization.py is at C:/code/aedos/src/aedos/layer1_extraction/normalization.py, NOT a substrate module. `_CANONICAL_MAP` is 66 surface→canonical entries (extractor.normalize_predicate). This IS "the ~50 synonym alias seed rows … hardcoded table in disguise" the contract refers to. There is NO separate synonym/alias DB table and there are NO alias seed rows. The seed pack (C:/code/aedos/seeds/predicate_translation.json) is a flat array of 83 PREDICATE-METADATA objects (80 kb_resolvable + 3 user_authoritative), keyed on aedos_predicate; multiple predicates legitimately share a kb_property (P166×5, P569×3, P39×3, P571×3, …). Those shared-property rows are NOT synonyms — they are distinct predicates that the multi-property substrate (WS2) will let collapse via property resolution; they are NOT deleted by WS8.
- contradiction_tracer.py (C:/code/aedos/src/aedos/layer5_result/contradiction_tracer.py, 88 lines incl. ContradictionTracer) is DEAD in production: grep shows ZERO src/ imports; only tests/integration/test_end_to_end.py:22,100 and tests/unit/test_retraction_propagator.py:10,126,132,140 import it. retraction.py's RetractionPropagator (92 lines) is NOT dead — it is wired in pipeline.py:125-128 (built + replay()) and consumed by aggregator.py:205-217 (record_verdict_trace) and aggregator.py:118-131 (_extract_source_rows). So "the dormant eager cascade" = ContradictionTracer + RetractionPropagator.propagate_retraction's eager full-index loop; record_verdict_trace/replay/_extract_source_rows are LIVE and become the provenance feed (WS5/WS4).

------------------------------------------------------------
(a) COMPLETE DELETION LIST (file:line-range, relation, replacement, orphan check)
------------------------------------------------------------

D1 — _CANONICAL_MAP hardcoded predicate-synonym table
  File: C:/code/aedos/src/aedos/layer1_extraction/normalization.py:15-66 (the dict) and its only readers normalize_predicate body lines 92-99 (the `if space_form in _CANONICAL_MAP` / `if no_aux in _CANONICAL_MAP` branches).
  Relates to: GUIDING DECISION 1 ("DELETE the hardcoded normalization _CANONICAL_MAP and the ~50 synonym alias seed rows").
  Replacement: WS2 multi-property substrate — surface synonyms collapse via property resolution at consult time (e.g. "works at"/"employed by" both resolve through the same PredicateBinding set). normalize_predicate KEEPS its mechanical snake_case/aux-strip fallback (lines 83-90, 102-110) so an extractor surface form still produces a stable key.
  Orphan check: normalize_predicate is called at extractor.py:611 and walker imports nothing from normalization. The 66-entry map is referenced ONLY at normalization.py:92-99. Deleting the dict + those two branches leaves normalize_predicate returning the mechanical snake_case form, which the WS2 substrate then resolves. NO orphan.
  Performed by: WS2 (owns substrate); WS8 confirms the net LOC.
  Estimated removal: ~58 lines (dict 52 + two lookup branches 6).

D2 — kb_verifier CONTINENT_QIDS + _location_disjoint + _LOCATION_KB_PROPERTIES + _GEO_CONTAINER_TYPES (geo hardcode cluster)
  File: C:/code/aedos/src/aedos/layer4_sources/kb_verifier.py
    - CONTINENT_QIDS frozenset: lines 31-33 (+ comment 17-30).
    - _LOCATION_KB_PROPERTIES frozenset: lines 42-49 (+ comment 35-41).
    - _GEO_CONTAINER_TYPES frozenset: lines 63-65 (+ comment 51-62).
    - _location_disjoint method: lines 403-466.
    - The CONTRADICTED-from-disjoint call site in _compare_positive: lines 389-398.
    - The geo-widening of value_types in verify(): lines 190-197 (the `if value_types and meta.kb_property in _LOCATION_KB_PROPERTIES` block).
  Relates to: GUIDING DECISION 2 (composition: generalize into a transitive-path primitive) + the contract's WS8 soundness directive ("CONTINENT_QIDS/_GEO_REGION_TYPES must be replaced by discovered transitivity + the nogood cache BEFORE deletion, or the Marie-Curie leak reopens").
  Replacement: WS2 multi-property substrate (continent-as-object admitted via discovered object_entity_types in PredicateBinding rather than a hardcoded continent set) + WS3 substrate_exceptions nogood cache (records "X part_of Y does NOT hold" so disjoint becomes a derived nogood, not a hardcoded continent enumeration) + WS_composition's generalized transitive-path primitive (the disjoint logic becomes: positive subsumption to a different discovered container ⇒ nogood-cached).
  Orphan check: CONTINENT_QIDS read only at 436,437,449. _LOCATION_KB_PROPERTIES read only at 196,395. _GEO_CONTAINER_TYPES read only at 197. _location_disjoint called only at 396. All internal to kb_verifier; no external importer (grep: no other file imports these symbols). NO orphan once the disjoint call site (389-398) and the widening block (190-197) go with them.
  Performed by: WS_substrate (WS2) after its discovered-types land; SOUNDNESS-SENSITIVE — see (e).
  Estimated removal: ~110 lines.

D3 — kb_wikidata _GEO_REGION_TYPES + _PART_OF_BRIDGE_PROPERTY + type-guarded P361 bridge in _build_subsumption_ask_query
  File: C:/code/aedos/src/aedos/layer4_sources/kb_wikidata.py
    - _GEO_REGION_TYPES tuple: lines 310-320 (+ comment 298-309).
    - _PART_OF_BRIDGE_PROPERTY: line 321.
    - The bridge UNION branch in _build_subsumption_ask_query: lines 347-365 (the `if relation_type != "part_of"` early return STAYS; the `region_values = …` through the closing UNION return is the deletion — replace the whole tail with the simple `ASK {{ wd:{source} ({path})+ wd:{target} . }}` form for all relation types).
  Relates to: GUIDING DECISION 2 ("Generalize the existing SPARQL property-path ASK (_build_subsumption_ask_query) into a first-class transitive-path primitive for ANY transitive KB property").
  Replacement: WS_composition's generalized transitive-path primitive — _build_subsumption_ask_query becomes a thin caller of a generic `_build_transitive_path_ask(source, target, properties)`; region containment (Massachusetts ⊂ New England) is recovered by DISCOVERING the right transitive property set per relation from the Wikidata ontology (P1647 subproperty / P1696 inverse) in WS2, not by hardcoding _GEO_REGION_TYPES.
  Orphan check: _GEO_REGION_TYPES read only at 314-319 (def) and 349 (bridge). _PART_OF_BRIDGE_PROPERTY read only at 321(def),350. _build_subsumption_ask_query called at 1441 (_live_subsumption). The generalized primitive must preserve the public `subsumption(entity_a, entity_b, relation_type)` shape at kb_wikidata.py:600-605 (SubsumptionOracle.consult Priority-1 at subsumption.py:105 depends on it). NO orphan IF the generic primitive returns the same SubsumptionResult.
  Performed by: WS_composition; SOUNDNESS-SENSITIVE — see (e).
  Estimated removal: ~35 lines (def 23 + bridge 12), partially offset by the generic primitive (~+15 net, but generic primitive is shared so net-down across kb_verifier disjoint deletion).

D4 — walker depth==0 KB-neighbor cap
  File: C:/code/aedos/src/aedos/layer4_sources/walker.py:991 — the `if not sub_produced and depth == 0:` guard. Delete the `and depth == 0` clause (the `if not sub_produced:` fallback STAYS; only the depth cap is removed).
  Relates to: GUIDING DECISION 2 ("REMOVE the depth==0 KB-neighbor cap").
  Replacement: WS_composition — bounded OUTGOING-edge premise-forward / bidirectional search with budget governance replaces the cap; the walker budget (walker_wall_clock_seconds / walker_max_llm_calls, pipeline.py:197-199) is the new bound, not a depth==0 cliff.
  Orphan check: the comment block 980-990 explains the cap; it should be rewritten, not orphaned. No external consumer of the cap. NO orphan.
  Performed by: WS_composition; SOUNDNESS-NEUTRAL but COST-SENSITIVE (the D51 diagnostic note at 980-990 warns of 18-min wall-clock blowup) — must land WITH the bounded bidirectional search + budget, never alone.
  Estimated removal: ~1 line code (+ comment rewrite).

D5 — walker _is_persona_subject + its call site (route persona-subject suppression through provenance/abstention instead)
  File: C:/code/aedos/src/aedos/layer4_sources/walker.py
    - _is_persona_subject method: lines 843-875.
    - Call site in _try_external_grounding: lines 643-644 (`if self._is_persona_subject(node): return None, "", 0, {}`).
  Relates to: GUIDING DECISION 4 (subject_absent_from_source / abstention_reason) — the persona guard is a special-case of "subject is user-scoped, KB is the wrong source"; it becomes an abstention_reason short-circuit rather than a hardcoded SQL lookup.
  Replacement: WS4 — the user_authoritative routing_hint + a `subject_absent_from_source`/user-persona abstention_reason set on the Claim pre-lookup. (Note: the contract WS8 prompt names _is_persona_subject for deletion. Confirm with the WS4 owner that the persona claims it guards still route to user_authoritative and never hit KB — otherwise this reopens the "Asa → Asa King of Judah" false-contradiction. SOUNDNESS-SENSITIVE.)
  Orphan check: _is_persona_subject called ONLY at 643. It reads self._tier_u._db directly (a layering smell). NO external consumer. NO orphan once 643-644 go.
  Performed by: WS4; SOUNDNESS-SENSITIVE — see (e).
  Estimated removal: ~35 lines.

D6 — predicate_distribution as a GATE → demote to RANKER (not a file deletion; the hand-seeded rubric prompt + the `if not directions: continue` gate)
  File: C:/code/aedos/src/aedos/layer4_sources/walker.py:944-945 (`directions = _distribution_directions(dist.verdict); if not directions: continue`) — the GATE. And C:/code/aedos/src/aedos/layer3_substrate/predicate_distribution.py:158-195 — the AUTHORITATIVE RUBRIC prompt block (the hand-seeded examples lives_in/mortal/prefers/both + POLARITY RULE) the contract calls "hand-seeded rubric".
  Relates to: GUIDING DECISION 2 ("DEMOTE predicate_distribution from a GATE to a RANKER (remove 'if not directions: continue')").
  Replacement: WS_composition — distribution verdict becomes a RANKING signal on discovery candidates, not a hard gate; soundness enforced at VERIFICATION (§3.2) not discovery. The hand-seeded rubric prompt is trimmed to a neutral definition-only prompt (keep lines 162-166 verdict definitions; remove the 167-195 rubric + polarity-default that bias the model toward the corpus's pinned framings — a hardcoded-knowledge smell per MEMORY feedback_no_hardcoded_mappings).
  Orphan check: PredicateDistributionOracle is wired pipeline.py:174-176, consumed walker.py:935. _distribution_directions at walker.py:203-217 STAYS (still maps verdict→directions for the ranker). The gate removal means ungated relations now expand; the §3.2 soundness must move to verification (WS_composition's discover/verify split). PredicateDistributionOracle keep; rubric trim only.
  Performed by: WS_composition; SOUNDNESS-SENSITIVE (removing the gate liberalizes discovery — the verify-side §3.2 guard MUST land first).
  Estimated removal: ~30 lines prompt + 1 line gate.

D7 — The four _build_claim drops → abstention_reason (not dropped)
  File: C:/code/aedos/src/aedos/layer1_extraction/extractor.py
    - Drop #1 (hard-claim substring): lines 526-528 (`if not self._passes_hard_claim_check(...): return None`). _passes_hard_claim_check itself (669-688) — confirm with WS4 whether it stays as a designation source or goes.
    - Drop #2 (content-less occurred/happened event): lines 530-548. Contract says this filter is ALREADY OBSOLETE given per-claim interventions — can be removed outright OR converted to abstention_reason="content_less_event".
    - Drop #3 (subject==object self-referential): lines 556-583 (`if raw_subject==raw_object: return None`) → abstention_reason="self_referential". MUST stay pre-lookup.
    - Drop #4 (predicate==object): lines 585-598 (`if raw_pred_check==raw_obj_check: return None`) → abstention_reason="predicate_eq_object". MUST stay pre-lookup.
    - (The future-scope drop at 607-608 is NOT in the four — temporal; leave to WS6.)
  Relates to: GUIDING DECISION 4 ("_build_claim must NEVER return None for a shaped claim … become an explicit abstention_reason that the walker short-circuits to no_grounding_found BEFORE any KB lookup").
  Replacement: WS4 — Claim gains `abstention_reason: Optional[str]`; the four `return None` become `abstention_reason=<value>` set on the Claim; walker short-circuits to no_grounding_found before KB lookup for self_referential/predicate_eq_object (these MUST stay pre-lookup or they cause false-contradictions per the contract).
  Orphan check: _build_claim called at extractor.py:509 inside `if claim is not None: claims.append(claim)` (510-511) — the None-filter at 510 must be removed too (claims always appended). NO orphan; the extractor's caller (chat_wrapper, benchmark) iterates the list.
  Performed by: WS4; the subject==object/predicate==object pre-lookup ordering is SOUNDNESS-SENSITIVE.
  Estimated change: ~0 net (returns become assignments; comments trimmed — slight LOC down).

D8 — INERT_PROSE silent drop at the chat_wrapper boundary → not_checkworthy ClaimVerdict
  File: C:/code/aedos/src/aedos/deployment/chat_wrapper.py:264 (`claims = [c for c in claims if c.triage_decision == TriageDecision.VERIFY]`) and the user-claim filter at :228.
  Relates to: GUIDING DECISION 4 ("triage INERT_PROSE becomes a QUIET 'not_checkworthy' designation carried as a ClaimVerdict, never silently dropped at chat_wrapper").
  Replacement: WS4/WS5 — INERT_PROSE claims carry abstention_reason="not_checkworthy" and flow through to a ClaimVerdict (quiet, non-intervening). select_interventions (chat_wrapper.py:101-156) treats not_checkworthy as PASS_THROUGH-eligible (no annotation).
  Orphan check: TriageDecision still used (extractor.py:481 field, walker.py:174). The :264 filter removal means INERT_PROSE claims reach the walker; the walker short-circuits them via abstention_reason. select_interventions must learn not_checkworthy. The :228 user-claim filter (promotion path) is a SEPARATE decision — promoting inert prose into Tier U may be undesirable; confirm with WS4 (likely KEEP :228 filter, remove :264 only).
  Performed by: WS4 (Claim/abstention) + WS5 (ClaimVerdict surfacing); coordinate with WS8 ordering.
  Estimated change: ~+5 lines (gain a quiet designation path).

D9 — ContradictionTracer (dead eager cascade) — DELETE the file
  File: C:/code/aedos/src/aedos/layer5_result/contradiction_tracer.py (entire file, 88 lines, ContradictionTracer + _RETRACTABLE_TABLES + _now).
  Relates to: GUIDING DECISION 3 ("REWRITE retraction.py/contradiction_tracer.py from the dormant eager cascade into this bounded provenance-driven version").
  Replacement: WS5 provenance-driven lazy retraction — premise-retraction marks dependent verdicts STALE lazily (those whose provenance includes the retracted Tier U row = the *_given_assertion ones) and re-derives on next reference. ContradictionTracer's eager row-walk is replaced by lazy provenance traversal.
  Orphan check: ZERO src/ importers (grep confirmed). ONLY test importers: tests/integration/test_end_to_end.py:22,100 and tests/unit/test_retraction_propagator.py:10,126,132,140. These tests MUST be deleted/rewritten by WS5 in the SAME change (else import error). AFFECTED TESTS — see below.
  Performed by: WS5; coordinate test deletion.
  Estimated removal: 88 lines (file) + test rewrites.

D10 — RetractionPropagator.propagate_retraction eager full-index loop → lazy provenance query
  File: C:/code/aedos/src/aedos/layer5_result/retraction.py:78-107 (propagate_retraction iterates the ENTIRE _trace_index eagerly). record_verdict_trace (36-39), replay (41-76), _extract_source_rows feed STAY (they become the provenance feed).
  Relates to: GUIDING DECISION 3 (lazy, per-claim, semiring-style provenance; "operator: lazy, not eager").
  Replacement: WS5 — propagate_retraction becomes a lazy staleness-marker keyed on the provenance term; the eager `for claim_id, rows in self._trace_index.items()` scan is replaced by a targeted "which verdicts' provenance includes this Tier U row" lookup.
  Orphan check: propagate_retraction called ONLY by contradiction_tracer.py:72 (which D9 deletes) and tests. After D9, propagate_retraction has no production caller — confirm before rewrite. record_verdict_trace called aggregator.py:207 (LIVE — keep). NO orphan if the WS5 lazy API replaces propagate_retraction's role.
  Performed by: WS5.
  Estimated change: ~-20 lines net (eager loop out, lazy query in).

------------------------------------------------------------
(b) DB MIGRATION PLAN — all ADDITIVE + IDEMPOTENT
------------------------------------------------------------

ALL migrations land in C:/code/aedos/src/aedos/database.py inside create_schema (called by open_db:260 and open_memory_db:275). The existing pattern is the authority: CREATE TABLE IF NOT EXISTS in _SCHEMA_SQL (lines 10-128) for fresh DBs, plus guarded `try: conn.execute("ALTER TABLE … ADD COLUMN …") except sqlite3.OperationalError: pass` for existing DBs (lines 157-214 show the established single_valued/D33/D47/status precedent). Every new migration MUST follow this exact pattern. create_schema is idempotent and the empty-table seed gate (_maybe_load_seeds:218-245) is preserved.

M1 — bindings JSON column on predicate_translation (WS1/WS2)
  In _SCHEMA_SQL predicate_translation CREATE (after line 48 object_entity_types): add `bindings TEXT,`.
  Migration guard (after the D33 loop ~line 173): 
    `try: conn.execute("ALTER TABLE predicate_translation ADD COLUMN bindings TEXT") except sqlite3.OperationalError: pass`
  Legacy scalar columns kb_namespace/kb_property/slot_to_qualifier/single_valued/subject_entity_types/object_entity_types are RETAINED (read-synthesized — PredicateMetadata._row_to_metadata at predicate_translation.py:509-550 synthesizes one PredicateBinding from the scalar columns when bindings IS NULL). Half-migrated DB stays functional: rows without bindings synthesize from scalars; rows with bindings use them.

M2 — property_relations table (WS2 — cached Wikidata property ontology P2302/P1647/P1696/P1659)
  Add to _SCHEMA_SQL:
    CREATE TABLE IF NOT EXISTS property_relations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kb_namespace TEXT NOT NULL,
      from_property TEXT NOT NULL,
      relation TEXT NOT NULL,          -- subproperty|inverse|related|constraint
      to_property TEXT,                -- nullable for constraint rows
      constraint_json TEXT,            -- P2302 payload (value-type, single-value)
      source TEXT NOT NULL,            -- wikidata_ontology|sling
      created_at TEXT NOT NULL,
      last_consulted_at TEXT,
      used_count INTEGER DEFAULT 0,
      retracted_at TEXT,
      retraction_reason TEXT,
      UNIQUE(kb_namespace, from_property, relation, to_property)
    );
    CREATE INDEX IF NOT EXISTS idx_property_relations_from ON property_relations(from_property);
  No ALTER needed (new table; CREATE TABLE IF NOT EXISTS is idempotent on existing DBs).

M3 — substrate_exceptions table (WS3 — nogoods / P2303-style exception_to_constraint)
  Add to _SCHEMA_SQL:
    CREATE TABLE IF NOT EXISTS substrate_exceptions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      predicate TEXT NOT NULL,
      property_path TEXT NOT NULL,     -- the resolved binding/path that does NOT hold
      subject_or_subtree TEXT NOT NULL,
      object_ref TEXT,
      reason TEXT NOT NULL,
      source TEXT NOT NULL,            -- kb_check|verification
      created_at TEXT NOT NULL,
      last_consulted_at TEXT,
      used_count INTEGER DEFAULT 0,
      UNIQUE(predicate, property_path, subject_or_subtree, object_ref)
    );
    CREATE INDEX IF NOT EXISTS idx_substrate_exceptions_lookup ON substrate_exceptions(predicate, subject_or_subtree);
  Nogoods are cached EAGERLY (only make Aedos more conservative = safe). No retracted_at needed for the safe-by-sign reason (positive bindings are the ones gated by live re-check; nogoods persist). New table; idempotent.

M4 — provenance persistence on JustificationTrace / verdict (WS5)
  Decision: provenance is computed PER CLAIM and DISCARDED per session (contract: "NOT an eager global belief web"). So the PRIMARY provenance is in-memory (JustificationTrace.provenance field, added by WS5 in trace.py). The DB persistence that IS needed is the RETRACTABLE id on KB premise_lookup edges (D13): the entity_resolution_cache row id must be recorded on the KB premise edge so KB verdicts are retractable. That row id ALREADY exists in the entity_resolution_cache table (database.py:109-122) — no schema change; WS5 adds `entity_resolution_cache_row_id` to the TraceEdge.metadata and to aggregator._TRACE_ROW_ID_KEYS (aggregator.py:111-115). No DB migration; the persistence channel is the existing audit_log verdict_recorded events (retraction.py:41-76 replay). 
  IF the operator later wants provenance persisted across sessions (out of the lazy-per-session model), a `provenance TEXT` column on a new `claim_verdict` table would be the additive home — but the contract explicitly says discard-per-session, so WS8 does NOT add it. DOCUMENT this as the deliberate non-addition.

M5 — Claim/verdict persistence
  Claim.abstention_reason (WS4) and ClaimVerdict.contradicting_value (WS5) are DATACLASS fields, not persisted (Claim/ClaimVerdict are session objects). No DB migration. The audit_log (database.py:91-98) already captures verdict_recorded; if observability (DECISION 5) wants the abstention_reason + contradicting_value + tried-bindings persisted, they go into the EXISTING audit_log.event_data JSON (no schema change — event_data is free-form TEXT). DOCUMENT: observability rides audit_log, no new column.

MIGRATION ORDERING within create_schema (top-to-bottom, all idempotent):
  1. _SCHEMA_SQL executescript (fresh-DB CREATE TABLE IF NOT EXISTS for tier_u, predicate_translation incl. new `bindings`, property_relations, substrate_exceptions, plus all existing).
  2. Existing guarded ALTERs (single_valued:157-162, D33 loop:167-173, D47 loop:181-185) — UNCHANGED.
  3. NEW guarded ALTER: predicate_translation ADD COLUMN bindings (M1) — placed AFTER the D33/D47 loop, BEFORE _maybe_load_seeds.
  4. _maybe_load_seeds(conn) (187-188) — UNCHANGED (empty-table gate).
  5. Existing status ALTER (204-214) — UNCHANGED.
  6. conn.commit() (215).
  property_relations / substrate_exceptions need no ALTER (CREATE TABLE IF NOT EXISTS in step 1 covers both fresh and existing DBs).
  Also: append "property_relations" and "substrate_exceptions" to TABLE_NAMES (database.py:289-297) so test fixtures / introspection see them.

HALF-MIGRATED DB FUNCTIONALITY:
  - Old DB opened by new code: bindings column ALTERed in (idempotent); rows lacking bindings synthesize a PredicateBinding from scalar columns (M1 read-synthesis). property_relations/substrate_exceptions created empty; substrate falls back to scalar bindings + (cold) ontology fetch. System functional.
  - New DB opened by old code (rollback): old code ignores the bindings/property_relations/substrate_exceptions it doesn't know about; reads scalar columns; CHECK constraints on tier_u.status still satisfied. Functional (additive guarantee).

------------------------------------------------------------
(c) GLOBAL CHANGE ORDERING across the 6 code workstreams + dependency graph
------------------------------------------------------------

Foundation→substrate→composition→provenance→rest. The dependency edges (X → Y means X must land before Y):

PHASE 0 (WS8 — foundation, no behavior change, lands FIRST):
  - All DB migrations M1-M3 (additive columns/tables) + TABLE_NAMES update. System stays green (nothing reads bindings/property_relations/substrate_exceptions yet). This unblocks every other workstream.
  WS8.DB → {WS1, WS2, WS3}

PHASE 1 (WS1 — data model):
  - PredicateBinding dataclass; PredicateMetadata.bindings:list[PredicateBinding] with read-synthesis from scalar columns; Claim.abstention_reason; ClaimVerdict.contradicting_value; JustificationTrace.provenance field; TraceEdge entity_resolution_cache_row_id key + aggregator._TRACE_ROW_ID_KEYS addition.
  - These are ADDITIVE dataclass changes; consumers still read scalar kb_property (kb_verifier.py:128,217 etc.) so behavior unchanged until WS2 flips the verifier to loop bindings.
  WS1 → {WS2, WS4, WS5}

PHASE 2 (WS2 — substrate, multi-property map):
  - property_relations population (ontology discovery) + binding resolution; kb_verifier.verify loops the binding set (replaces scalar kb_property reads at 128-129,217).
  - THEN D1 (_CANONICAL_MAP), D2 (geo cluster in kb_verifier) — these deletions land ONLY AFTER bindings + discovered object types prove out (D2 is soundness-sensitive).
  WS2 → {WS_composition, WS3}; D2 depends on WS2.discovered-types + WS3.nogood

PHASE 3 (WS_composition):
  - Discover/verify split; generic transitive-path primitive (then D3 in kb_wikidata); premise-forward/bidirectional search + budget (then D4 walker cap); distribution gate→ranker (D6); route walker subsumption through SubsumptionOracle.consult (subsumption.py:95) instead of find_neighbors (walker.py:951).
  WS_composition depends on WS2 (binding set drives discovery) + WS3 (nogood gates verify)

PHASE 4 (WS3 — partial TMS / provenance + nogood):
  - substrate_exceptions writes (nogood cache, eager); positive bindings gated by live KB check; THEN D2's disjoint logic is fully replaced (nogood-derived).
  WS3 → enables D2 final deletion

PHASE 5 (WS5 — provenance/retraction rewrite):
  - JustificationTrace.provenance computed lazily; chain_includes_assertion DERIVABLE from it; D13 retractable KB premise ids; lazy premise-retraction; THEN D9 (delete contradiction_tracer.py + tests) + D10 (retraction.py eager loop → lazy).
  WS5 depends on WS1 (provenance field) + WS_composition (trace edges to walk)

PHASE 6 (WS4 — verify-every-claim + WS6 temporal, can parallelize with 3-5 after WS1):
  - D7 (_build_claim 4 drops → abstention_reason), D8 (INERT_PROSE → not_checkworthy), D5 (walker _is_persona_subject → user_authoritative/subject_absent abstention).
  - WS6 temporal T1 (start/end date-in-object, interval resolver, P580/P582 surfacing) — independent of the deletions; orders after WS1.
  WS4 depends on WS1 (Claim.abstention_reason); D5 SOUNDNESS-SENSITIVE (coordinate WS4↔WS2 persona routing)

DEPENDENCY GRAPH (compact):
  WS8.DB → WS1 → WS2 → WS_composition → WS5
                  WS2 → WS3 → (D2 final)
                  WS1 → WS4
                  WS1 → WS6
  Deletion gating: D1←WS2 ; D2←WS2+WS3 ; D3←WS_composition ; D4←WS_composition ; D5←WS4+WS2 ; D6←WS_composition(verify-side §3.2) ; D7,D8←WS4 ; D9,D10←WS5.

------------------------------------------------------------
(d) LOC-DELTA ESTIMATE (rough; validates net-decrease)
------------------------------------------------------------
DELETIONS (lines removed):
  D1 _CANONICAL_MAP+branches: -58
  D2 kb_verifier geo cluster: -110
  D3 kb_wikidata geo bridge: -35 (then +~15 generic primitive = -20 net)
  D4 walker depth cap: -1 (comment -10)
  D5 walker persona: -35
  D6 distribution rubric+gate: -31
  D7 _build_claim drops: -5 net (returns→assigns, comments out)
  D8 INERT_PROSE: +5 (quiet path)
  D9 contradiction_tracer.py: -88
  D10 retraction eager loop: -20
  Subtotal deletions ≈ -358 lines.

ADDITIONS (rough, per other workstreams — WS8 estimates for accounting only):
  WS8 DB migrations (3 ALTER guards + 2 CREATE + index + TABLE_NAMES): +~40
  WS1 PredicateBinding + dataclass fields + read-synthesis: +~80
  WS2 binding resolution + ontology discovery + verifier loop: +~150
  WS_composition discover/verify split + transitive primitive + bidirectional search: +~140
  WS3 nogood cache writes + live-check gating: +~70
  WS5 lazy provenance + retraction rewrite: +~90 (offset by D9/D10 -108)
  WS4 abstention_reason plumbing: +~30
  WS6 temporal triples + interval resolver: +~90
  Subtotal additions ≈ +690 GROSS, but ≈ +582 net of the D9/D10 self-offsets already counted.

NET: additions (~+690) vs deletions (~-358) ⇒ this looks like +332 GROSS UP, which CONTRADICTS the operator's net-LOC-down expectation IF every workstream lands its full feature. The reconciliation: (1) the contract's LOC-down target is realistic ONLY because the multi-property substrate REPLACES the entire hand-tuned geo/persona/disjoint/rubric machinery (D2+D3+D5+D6 = ~-187 of compensating hardcode) with DISCOVERED knowledge that is DATA (DB rows), not code; (2) WS2/WS_composition's gross additions are over-estimated here — much of the binding-loop logic SUBSUMES existing scalar-path code in kb_verifier.verify (lines 122-302) rather than adding alongside it (kb_verifier should NET shrink ~-40 as the scalar special-cases collapse into one binding loop). REALISTIC net target: roughly FLAT to modestly DOWN (~-50 to -150) IF WS2 truly replaces rather than layers. WS8 RECOMMENDATION to operator: net-LOC-down is achievable but is CONTINGENT on WS2 collapsing kb_verifier's special-case ladder; flag a checkpoint after WS2 lands to re-measure. If WS2 layers instead of replaces, net will be UP and the operator should push back.

------------------------------------------------------------
(e) SOUNDNESS-SENSITIVE DELETIONS (must be ordered AFTER their replacement)
------------------------------------------------------------
SS1 (HIGHEST) — D2 (CONTINENT_QIDS/_location_disjoint/_GEO_CONTAINER_TYPES, kb_verifier) and D3 (_GEO_REGION_TYPES, kb_wikidata). These are the explicit Marie-Curie-leak guards (kb_wikidata.py:298-309 comment: "Restoring P361 only between two of these region types reopens region containment WITHOUT the Marie-Curie-class false-verify"; the trimmed _SUBSUMPTION_PROPERTIES at 143-167 documents the Warsaw⊂Germany leak). Deleting either BEFORE the WS2 discovered-types + WS3 nogood cache + WS_composition generic transitive primitive land REOPENS the leak (false-VERIFY "Marie Curie born in Germany" / false-CONTRADICT). ORDER: D2 after WS2+WS3; D3 after WS_composition. NEVER delete in isolation. The §3.2 never-false-verify invariant is directly at stake.

SS2 — D5 (walker _is_persona_subject). Guards against resolving "Asa" → "Asa, King of Judah" and emitting a false-contradiction on a negation ("Asa is not in France"). Must NOT delete until WS4 confirms persona-subject claims route user_authoritative (never reach KB). Else §3.2 false-contradiction reopens.

SS3 — D6 (distribution GATE→ranker). Removing `if not directions: continue` LIBERALIZES discovery — without the WS_composition verify-side §3.2 enforcement landing FIRST, ungated distribution lets unsound chains through to verification. Order: verify-side soundness guard BEFORE gate removal.

SS4 — D7 ordering constraint (NOT deletion-order but lookup-order): the subject==object (D7 #3) and predicate==object (D7 #4) filters MUST remain PRE-LOOKUP (set abstention_reason before any KB lookup). The contract is explicit: "filters subject==object/predicate==object MUST stay pre-lookup or they cause false-contradictions." Converting them to post-lookup abstention reopens a false-CONTRADICT class.

SS5 (LOW / SAFE-BY-SIGN) — D9/D10 (contradiction_tracer/retraction eager cascade). These are RETRACTION paths; deleting the eager cascade before the lazy provenance lands only LOSES retraction coverage temporarily (verdicts not marked stale) — that is a false-VERIFY-over-time risk if a Tier U premise is retracted and the dependent verdict is not re-derived. Lower than SS1-SS4 because contradiction_tracer is already dormant (no production caller). Still: land WS5 lazy retraction in the SAME change that deletes D9/D10, never a gap.

SAFE-AT-ANY-TIME — D1 (_CANONICAL_MAP) and D4 (depth cap) are soundness-neutral. D1 only affects predicate-key stability (WS2 resolution covers it); a miss costs a false-ABSTAIN (safe), never a false-verify. D4 is cost-sensitive (D51 18-min blowup) not soundness-sensitive — gate on the budget/bidirectional-search landing, not on §3.2.

## DELETIONS
- C:/code/aedos/src/aedos/layer1_extraction/normalization.py:15-66 — _CANONICAL_MAP dict (66 surface→canonical entries) + lookup branches at 92-99 — safe: WS2 multi-property substrate collapses synonyms via property resolution; normalize_predicate keeps mechanical snake_case fallback; a miss is a false-abstain (safe), never false-verify. Performed by WS2.
- C:/code/aedos/src/aedos/layer4_sources/kb_verifier.py:31-33 (CONTINENT_QIDS) +42-49 (_LOCATION_KB_PROPERTIES) +63-65 (_GEO_CONTAINER_TYPES) +190-197 (geo value_types widening) +389-398 (disjoint CONTRADICTED call site) +403-466 (_location_disjoint) — replaced by WS2 discovered object_entity_types + WS3 nogood cache + WS_composition transitive primitive. SOUNDNESS-SENSITIVE (Marie-Curie leak). No external importer (grep). Performed by WS2 after WS3.
- C:/code/aedos/src/aedos/layer4_sources/kb_wikidata.py:310-320 (_GEO_REGION_TYPES) +321 (_PART_OF_BRIDGE_PROPERTY) +347-365 (type-guarded P361 bridge UNION in _build_subsumption_ask_query) — replaced by generic transitive-path primitive (WS_composition); region containment recovered via discovered properties (WS2). SOUNDNESS-SENSITIVE. Read only at 314-319/321/349-350/1441; subsumption() public shape (600-605) preserved. Performed by WS_composition.
- C:/code/aedos/src/aedos/layer4_sources/walker.py:991 — the `and depth == 0` clause of the KB-neighbor fallback cap — replaced by bounded bidirectional search + walker budget (WS_composition). COST-sensitive (D51 18-min blowup), not soundness; gate on budget landing. No external consumer.
- C:/code/aedos/src/aedos/layer4_sources/walker.py:843-875 (_is_persona_subject) +643-644 (call site) — replaced by user_authoritative routing + subject_absent_from_source abstention_reason (WS4). SOUNDNESS-SENSITIVE (Asa→King of Judah false-contradiction). Called only at 643. Performed by WS4 coordinated with WS2 persona routing.
- C:/code/aedos/src/aedos/layer4_sources/walker.py:944-945 — `if not directions: continue` distribution GATE → demote to ranker (keep _distribution_directions) — SOUNDNESS-SENSITIVE: verify-side §3.2 guard must land first (WS_composition discover/verify split).
- C:/code/aedos/src/aedos/layer3_substrate/predicate_distribution.py:167-195 — the hand-seeded AUTHORITATIVE RUBRIC prompt block (lives_in/mortal/prefers/both examples + POLARITY RULE) — trim to neutral definition-only prompt (keep 162-166); hardcoded-knowledge smell per MEMORY no-hardcoded-mappings. Performed by WS_composition.
- C:/code/aedos/src/aedos/layer1_extraction/extractor.py:526-528 (hard-claim drop) +530-548 (content-less event drop; contract says obsolete) +556-583 (subject==object drop → self_referential) +585-598 (predicate==object drop → predicate_eq_object) +510 None-filter — become abstention_reason on Claim, never return None. subject==object/predicate==object MUST stay pre-lookup (SOUNDNESS-SENSITIVE). Performed by WS4.
- C:/code/aedos/src/aedos/deployment/chat_wrapper.py:264 — `claims = [c for c in claims if c.triage_decision == TriageDecision.VERIFY]` silent INERT_PROSE drop — replaced by not_checkworthy abstention_reason carried as a quiet ClaimVerdict (WS4/WS5). NOTE: keep the :228 user-claim/promotion filter (separate decision — confirm with WS4). select_interventions must learn not_checkworthy.
- C:/code/aedos/src/aedos/layer5_result/contradiction_tracer.py:1-88 — ENTIRE FILE (ContradictionTracer + _RETRACTABLE_TABLES + _now) — DEAD in production (zero src/ importers; only 2 test files). Replaced by WS5 lazy provenance-driven retraction. Test importers test_end_to_end.py:22,100 and test_retraction_propagator.py:10,126,132,140 MUST be rewritten in the same change. Performed by WS5.
- C:/code/aedos/src/aedos/layer5_result/retraction.py:78-107 — propagate_retraction eager full-index loop → lazy provenance-keyed staleness query (WS5). record_verdict_trace(36-39)/replay(41-76)/_extract_source_rows STAY (provenance feed). After D9, propagate_retraction has no production caller. SAFE-BY-SIGN but land with WS5 lazy API, no gap.

## ADDITIONS
- C:/code/aedos/src/aedos/database.py — _SCHEMA_SQL: add `bindings TEXT,` to predicate_translation CREATE; add CREATE TABLE IF NOT EXISTS property_relations (M2, with idx_property_relations_from); add CREATE TABLE IF NOT EXISTS substrate_exceptions (M3, with idx_substrate_exceptions_lookup). Role: additive schema for multi-property substrate + nogood cache.
- C:/code/aedos/src/aedos/database.py:~173 (after D33/D47 ALTER loop, before _maybe_load_seeds) — guarded ALTER: try: conn.execute('ALTER TABLE predicate_translation ADD COLUMN bindings TEXT') except sqlite3.OperationalError: pass. Role: M1 idempotent migration for existing DBs; legacy scalar columns retained for read-synthesis.
- C:/code/aedos/src/aedos/database.py:289-297 — append 'property_relations' and 'substrate_exceptions' to TABLE_NAMES. Role: test-fixture/introspection visibility of new tables.
- DECISION-RECORD (no code) — provenance is per-session/in-memory (JustificationTrace.provenance, WS5), NOT a DB column; retractable KB-premise id rides the EXISTING entity_resolution_cache.id via TraceEdge.metadata (no migration); observability rides the EXISTING audit_log.event_data JSON (no migration). WS8 deliberately adds NO claim_verdict/provenance persistence table — document this as intentional per the lazy-discard contract.

## CALL SITES / CONSUMERS
- normalize_predicate consumer: C:/code/aedos/src/aedos/layer1_extraction/extractor.py:611 — only caller; unaffected by D1 (keeps mechanical fallback).
- PredicateMetadata scalar kb_property consumers (must keep working via read-synthesis until WS2 flips to bindings): C:/code/aedos/src/aedos/layer4_sources/kb_verifier.py:128-129,196,217,288; C:/code/aedos/src/aedos/layer4_sources/walker.py:776,803 (_verify_kb_quantitative); C:/code/aedos/src/aedos/layer3_substrate/predicate_translation.py:353,356 (query_neighbors). All read PredicateMetadata.kb_property — WS1 must keep the scalar attr live (synthesized from bindings) or these break.
- kb_verifier.verify call site: C:/code/aedos/src/aedos/layer4_sources/walker.py:663-667 (in _try_external_grounding) — consumes KBVerdict; WS5 must carry kb_result.matched_statement.value (computed, dropped at walker.py:698-704) onto TraceEdge.metadata + ClaimVerdict.contradicting_value.
- _GEO_REGION_TYPES / _build_subsumption_ask_query consumers: kb_wikidata.py:349 (region_values),1441 (_live_subsumption calls _build_subsumption_ask_query). subsumption() public method at 600-605 consumed by SubsumptionOracle.consult Priority-1 at C:/code/aedos/src/aedos/layer3_substrate/subsumption.py:105 — generic primitive MUST preserve SubsumptionResult shape.
- _is_persona_subject consumer: walker.py:643 (only). Reads self._tier_u._db directly.
- predicate_distribution gate consumer: walker.py:935-945 (_expand_via_substrate). PredicateDistributionOracle wired pipeline.py:174-176. _distribution_directions (walker.py:203-217) stays.
- SubsumptionOracle.find_neighbors consumer: walker.py:951 (_expand_via_substrate) — WS_composition reroutes this through SubsumptionOracle.consult (subsumption.py:95).
- _build_claim None consumers: extractor.py:509-511 (the `if claim is not None` filter must go — claims always appended); downstream iterators chat_wrapper.py:264 and tests/evaluation/benchmark.py.
- TriageDecision.VERIFY filter consumers: chat_wrapper.py:228 (user claims — KEEP), chat_wrapper.py:264 (draft claims — REMOVE for D8). TriageDecision still used extractor.py:481, walker.py:174.
- ClaimVerdict.contradicting_value / abstention_reason consumers: aggregator.py:172-177 (builds ClaimVerdict), chat_wrapper.py:78-98 (_format_correction/_format_abstention), chat_wrapper.py:101-156 (select_interventions). _format_correction (78-87) must emit 'the source indicates {value} instead' once contradicting_value plumbed (WS5).
- ContradictionTracer consumers (production): NONE (grep-verified zero src/ imports). Test consumers: tests/integration/test_end_to_end.py:22,100; tests/unit/test_retraction_propagator.py:10,126,132,140.
- RetractionPropagator consumers: pipeline.py:125-128 (build+replay); aggregator.py:205-217 (record_verdict_trace + verdict_recorded audit). propagate_retraction called ONLY by contradiction_tracer.py:72 + tests.
- _TRACE_ROW_ID_KEYS consumer: aggregator.py:118-131 (_extract_source_rows) — WS5 adds entity_resolution_cache_row_id key here for D13 retractability.
- chain_includes_assertion consumers: walker.py:191,321,484,528,590 (sets it); aggregator/_BASE_OF_DUAL via _apply_assertion_designation (walker.py:181-200); trace.py:37 (field). WS5 makes it DERIVABLE from provenance.
- TABLE_NAMES consumer: tests/unit/test_database.py (asserts table set) — must update for property_relations/substrate_exceptions.

## AFFECTED TESTS
- C:/code/aedos/tests/unit/test_retraction_propagator.py — will-break: imports ContradictionTracer (10,126,132,140) deleted by D9, and exercises propagate_retraction eager loop changed by D10. needs-update/rewrite by WS5 to the lazy provenance API.
- C:/code/aedos/tests/integration/test_end_to_end.py:22,100 — will-break: imports/uses ContradictionTracer. needs-update by WS5.
- C:/code/aedos/tests/unit/test_database.py — needs-update: TABLE_NAMES grows by property_relations + substrate_exceptions; assert new schema columns (bindings) present and migration idempotent on a pre-bindings DB.
- C:/code/aedos/tests/unit/test_predicate_distribution_oracle.py — needs-update: D6 trims the hand-seeded rubric prompt; any test asserting the rubric examples or the 'neither' polarity default will break; the gate→ranker change affects expected expansion.
- C:/code/aedos/tests/unit/test_walker.py, test_walker_cluster_2.py, test_walker_kb_neighbors.py, test_walker_failure_modes.py — needs-update: D4 (depth cap removal), D5 (_is_persona_subject removal), D6 (gate→ranker) change walker expansion/grounding; persona-subject suppression now via routing/abstention.
- C:/code/aedos/tests/unit/test_subsumption_oracle.py + any kb_wikidata subsumption test — needs-update: D3 changes _build_subsumption_ask_query to the generic primitive; region-containment (Massachusetts⊂New England) and the Marie-Curie negative (Warsaw⊄Germany) must be re-pinned against discovered-property behavior. SOUNDNESS regression guard.
- kb_verifier geo tests (Thames-in-Asia / Vatican-in-Africa / Rome-in-Germany disjoint cases referenced in code comments) — will-break/needs-update: D2 removes _location_disjoint; the CONTRADICTED-on-disjoint cases must be re-pinned against the WS3 nogood path. SOUNDNESS regression guard.
- C:/code/aedos/tests/unit/test_seed_pack_predicate_coverage.py — needs-update if PredicateMetadata gains bindings (read-synthesis); ensure 83-row seed still loads and synthesizes one PredicateBinding per legacy scalar row.
- Extractor _build_claim tests (self-referential / predicate==object / content-less-event / hard-claim drops) — will-break: D7 converts return None → abstention_reason; tests asserting len(claims) drops must assert abstention_reason instead. new-test-needed: self_referential/predicate_eq_object stay pre-lookup (assert no KB lookup fires).
- C:/code/aedos/tests/integration/test_chat_wrapper.py + tests/unit/test_chat_wrapper.py + tests/integration/test_chat_endpoint.py — needs-update: D8 (INERT_PROSE → not_checkworthy quiet ClaimVerdict) changes which claims reach select_interventions; PASS_THROUGH behavior for not_checkworthy.
- C:/code/aedos/tests/cold_start/test_zero_seed_correctness.py — needs-update: D1 (_CANONICAL_MAP removal) changes cold-start predicate normalization; verify substrate resolution covers the synonym surface forms.
- new-test-needed (WS8-owned): migration idempotency test — open old DB (no bindings/property_relations/substrate_exceptions), run create_schema twice, assert columns/tables present, no error, scalar rows still synthesize bindings; half-migrated functional check.
- C:/code/aedos/tests/calibration/test_corpus_runner.py + tests/evaluation/* (phase_e5/phase_e_comparison) — needs-update: predicate_distribution gate→ranker and seeded-mode bindings synthesis affect medium-bar verdict mix; re-baseline after WS2/WS_composition.

## ORDERING / DEPENDENCIES
- WS8.DB migrations (M1-M3 + TABLE_NAMES) land FIRST in C:/code/aedos/src/aedos/database.py — additive, no behavior change, green build. Unblocks WS1/WS2/WS3.
- WS1 data-model (PredicateBinding, bindings read-synthesis, Claim.abstention_reason, ClaimVerdict.contradicting_value, JustificationTrace.provenance, _TRACE_ROW_ID_KEYS entity_resolution_cache key) lands SECOND; depends on WS8.DB; must keep scalar kb_property attr live for current consumers.
- WS2 substrate (binding resolution + ontology discovery + kb_verifier binding loop) lands THIRD; depends on WS1. D1 deletes after WS2 binding loop proves out.
- WS_composition (discover/verify split, generic transitive primitive, bidirectional search+budget, gate→ranker, route through SubsumptionOracle.consult) lands FOURTH; depends on WS2 + WS3. D3, D4, D6 delete here; D6 verify-side §3.2 guard MUST precede the gate removal.
- WS3 nogood/exceptions lands alongside/after WS2; D2 (geo disjoint cluster) deletes ONLY after WS2 discovered-types + WS3 nogood + WS_composition transitive primitive all land (SS1, Marie-Curie leak).
- WS5 provenance/retraction lands FIFTH; depends on WS1 + WS_composition; D9 (delete contradiction_tracer.py + rewrite its 2 test files) and D10 (retraction eager→lazy) delete here in one change, no gap.
- WS4 (verify-every-claim) + WS6 (temporal) parallelize after WS1; D7/D8 by WS4; D5 by WS4 coordinated with WS2 persona routing (SS2).
- DELETION GATING SUMMARY: D1←WS2; D2←WS2+WS3+WS_composition; D3←WS_composition; D4←WS_composition(budget); D5←WS4+WS2; D6←WS_composition(verify §3.2 first); D7,D8←WS4; D9,D10←WS5. SAFE-ANYTIME: none of the soundness-sensitive ones; D1/D4 are gate-on-feature not soundness.
- LOC checkpoint: re-measure net LOC AFTER WS2 lands kb_verifier binding loop — net-down is contingent on WS2 REPLACING (not layering over) the scalar special-case ladder in kb_verifier.verify (122-302). Flag to operator if WS2 layers.

## RISKS / SOUNDNESS
- §3.2 never-false-verify at risk on D2+D3 (SS1): _GEO_REGION_TYPES/CONTINENT_QIDS/_location_disjoint are the documented Marie-Curie-leak guards (kb_wikidata.py:298-309, _SUBSUMPTION_PROPERTIES comment 143-167 on Warsaw⊂Germany). Deleting before WS2-discovered-types + WS3-nogood + generic transitive primitive land reopens false-VERIFY 'Marie Curie born in Germany' and false-CONTRADICT 'Rome in Germany'. MUST order after replacements; keep the Warsaw/Rome/Thames/Vatican cases as regression pins.
- D5 (SS2): removing _is_persona_subject reopens 'Asa→Asa King of Judah' false-contradiction on negations unless WS4 guarantees persona-subject claims route user_authoritative and never reach KB. Coordinate WS4↔WS2.
- D7 (SS4): subject==object and predicate==object filters MUST stay PRE-LOOKUP (set abstention_reason before any KB lookup) — contract-explicit; converting to post-lookup reopens a false-CONTRADICT class. The content-less-event filter is obsolete (safe to remove); the hard-claim substring filter — confirm WS4 keeps a designation not a drop.
- D6: removing the distribution gate (`if not directions: continue`) liberalizes chain DISCOVERY; functional-throughout requires the verify-side §3.2 enforcement (WS_composition discover/verify split) to land FIRST, else unsound chains reach verification.
- Over-time soundness (SS5): D9/D10 remove the retraction cascade; if WS5 lazy provenance retraction is not in the SAME change, a retracted Tier U premise leaves dependent *_given_assertion verdicts un-marked-stale (false-verify-over-time). Land together.
- Read-synthesis fragility: WS1 must keep PredicateMetadata.kb_property/single_valued/*_entity_types live (synthesized from bindings) — kb_verifier.py:128-129,196,217,288 and walker.py:776,803 read scalars; a premature scalar removal breaks the live pipeline (functional-throughout invariant).
- Migration half-state: old-code-on-new-DB (rollback) must ignore bindings/property_relations/substrate_exceptions and still satisfy tier_u.status CHECK — verified additive; add an idempotency + half-migrated regression test (WS8-owned).
- LOC-net-down is NOT guaranteed by the deletion list alone (~-358) against gross additions (~+690); it is contingent on WS2 collapsing kb_verifier's scalar special-case ladder into one binding loop (kb_verifier should net SHRINK). If WS2 layers instead of replaces, net LOC goes UP — operator checkpoint after WS2.
- Hardcoded-knowledge regression watch (MEMORY no-hardcoded-mappings): D6 rubric trim and D2/D3 geo-table deletion are the core of moving knowledge from code to KB/ontology; resist any temptation to reintroduce a fallback hardcode table during WS2/WS_composition when a discovered binding is missing — the correct degraded behavior is abstain (false-abstain safe), never a hardcoded guess.
