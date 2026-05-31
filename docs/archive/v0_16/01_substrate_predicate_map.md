# Aedos v0.16 ? Change Specification: Workstream 1 ? Multi-Property Substrate & Wikidata-Ontology Discovery

*Implementation-ready spec. Conforms to the interface contract in `00_overview_contract_ordering.md`, which is AUTHORITATIVE on all cross-workstream interfaces (data model, shared tables, ordering, soundness-sensitive deletion order). All file:line references were verified by the spec-mapping pass against the v0.16 base (identical to `main` at branch creation).*

---

# AEDOS v0.16 WORKSTREAM 1 — Multi-Property Substrate, Wikidata-Ontology Discovery, SLING Fallback, Normalization Deletion, Evidence Arbitration

This is the implementation-ready change spec for Workstream 1, verified against the current code against the v0.16 base (identical to `main` at branch creation). Every file:line reference below was read in full or grep-confirmed.

---

## 0. CURRENT-STATE GROUND TRUTH (what the code actually does today)

- **`PredicateMetadata`** (`predicate_translation.py:249-273`) is a flat dataclass with scalar `kb_property`, `slot_to_qualifier`, `single_valued`, `subject_entity_types`, `object_entity_types`. There is **no `bindings` field**.
- **`predicate_translation` table** (`database.py:36-56`) has scalar columns `kb_property`, `slot_to_qualifier`, `single_valued`, `subject_entity_types`, `object_entity_types`. Migrations are idempotent `ALTER … ADD COLUMN` wrapped in `try/except sqlite3.OperationalError` (`database.py:157-214`). **No `bindings` column.**
- **`kb_verifier.verify`** (`kb_verifier.py:94-302`) consumes the scalars directly: gates on `meta.routing_hint != "kb_resolvable" or not meta.kb_property` (`:128`), reads `meta.slot_to_qualifier` (`:142`, `:524`), uses `meta.kb_property` for the single lookup (`:217`) and `_LOCATION_KB_PROPERTIES` gating (`:196`, `:395`), uses `meta.single_valued` for the contradiction branch (`:288`, `:363`). It is **single-property**: one `_lookup_targets`, one `lookup_statements`, one `_compare_positive`.
- **`normalization._CANONICAL_MAP`** (`normalization.py:15-66`) is a 52-entry surface→canonical synonym table; `normalize_predicate` (`:74-110`) does map lookup → aux-strip → snake_case fallback. Called only from `extractor.py:611`.
- **Synonym alias seed rows** in `seeds/predicate_translation.json`: the "Phase H Cluster 3" block (`is_a`/`instance_of` pair plus the 19 alias rows from `received_award` at line 772 through `successor_of` at line 988) are pure synonyms duplicating a canonical row's `kb_property`+`slot_to_qualifier`.
- **`property_relations` / `substrate_exceptions` / SLING**: **do not exist anywhere** (grep-confirmed empty).
- **Wikidata ontology query builders**: only `_build_label_type_search_query`, `_build_neighbors_query`, `_build_subsumption_ask_query`, `_build_establishing_property_query`, `_build_lookup_query` exist. **No P2302/P1647/P1696/P1659 builder.**
- **`extract_with_tool`** signature: `(system, user_message, tool, max_tokens=8192, purpose=None)` returning `dict` (`client.py:425-438`).
- **Construction**: `PredicateTranslation(db, llm_client, consistency_checker)` at `pipeline.py:136`; `WikidataAdapter(http_cache, llm_client, db, config)` at `pipeline.py:82`.

---

## 1. DETAILED CHANGE SPEC

### 1.1 `PredicateBinding` dataclass + `PredicateMetadata.bindings` + read-synthesis (`predicate_translation.py`)

**Add** a new dataclass *before* `PredicateMetadata` (insert after line 246, before `@dataclass class PredicateMetadata`):

```python
@dataclass
class PredicateBinding:
    """One candidate (predicate → KB property) binding. The substrate holds a
    RANKED LIST of these per predicate; evidence arbitrates at verify time
    (v0.16 Decision 1). A legacy scalar row synthesizes exactly one binding."""
    kb_namespace: Optional[str]
    kb_property: Optional[str]
    slot_to_qualifier: Optional[dict] = None
    single_valued: bool = False
    subject_entity_types: Optional[list[str]] = None
    object_entity_types: Optional[list[str]] = None
    source: str = "legacy_scalar"   # legacy_scalar | oracle | ontology_p2302 | sling
    rank: float = 1.0               # discovery-time prior; verify-time evidence reorders
```

**Modify `PredicateMetadata`** (`:249-273`): keep `aedos_predicate`, `object_type`, `user_subject_required`, `distinct_slots`, `routing_hint`, `reason`, `created_at`, `last_consulted_at`, `used_count`, `retracted_at`, `retraction_reason`. **Add** `bindings: list[PredicateBinding] = field(default_factory=list)`. **Retain** the scalar `kb_namespace`, `kb_property`, `slot_to_qualifier`, `single_valued`, `subject_entity_types`, `object_entity_types` as **read-only convenience accessors** that return `bindings[0]`'s values (so every existing `meta.kb_property` consumer keeps working unchanged). Implement as properties:

```python
@property
def kb_property(self) -> Optional[str]:
    return self.bindings[0].kb_property if self.bindings else None
# …same shape for kb_namespace, slot_to_qualifier, single_valued,
#    subject_entity_types, object_entity_types
```

Role: this is the "legacy rows synthesize one binding from scalar cols" requirement. The properties make the 18 grep-confirmed `meta.kb_property` / `meta.slot_to_qualifier` / `meta.single_valued` / `meta.subject_entity_types` / `meta.object_entity_types` call-sites (§4) continue compiling without edit, while new multi-property code iterates `meta.bindings`. **Note**: `PredicateMetadata(...)` is currently constructed with `kb_property=`/`slot_to_qualifier=`/`single_valued=` keyword args in 3 src sites and ~12 test sites — converting those scalars to `@property` removes them from `__init__`, which **breaks those constructors**. Resolve by adding a classmethod `PredicateMetadata.from_scalars(...)` that accepts the old kwargs, builds one `PredicateBinding`, and sets `bindings=[that]`; rewrite the internal constructors (`_generate_and_store` `:492`, `_row_to_metadata` `:531`) to call it; update tests (§5). This keeps the dataclass `bindings`-native while preserving a scalar entrypoint.

**`from dataclasses import field`** is already imported only as `dataclass` (`:5`) — change to `from dataclasses import dataclass, field`.

**Modify `_row_to_metadata`** (`:509-550`): after parsing scalar columns, read the new `bindings` JSON column (defensively, IndexError/KeyError → None like the D33 cols at `:522-529`). If `bindings` JSON is present and non-empty, deserialize each element into a `PredicateBinding`. **Else** synthesize one binding from the scalar columns (`kb_namespace`, `kb_property`, `_parse_json(slot_to_qualifier)`, `bool(single_valued)`, `subject_types`, `object_types`, `source="legacy_scalar"`). Return `PredicateMetadata(..., bindings=[…])`. This is read-synthesis (b).

**Modify `query_neighbors`** (`:350-359`): currently `if subject is None or subject.kb_property is None`. The `subject.kb_property` property still works (returns binding[0]'s). For multi-property correctness, change the SQL to match **any** binding's property — but since the table now carries `bindings` JSON, the cleanest minimal change is to keep matching the legacy `kb_property` column (still populated for back-compat, see §1.2) and additionally match predicates sharing any binding property. **Minimal v0.16**: leave `query_neighbors` keyed on the primary property (`bindings[0].kb_property`) — it's used only by `tier_u._stage3` (`:650`) as a broadening heuristic; widening it is a Composition-workstream concern, out of WS1 scope. Document the deferral inline.

### 1.2 `bindings` JSON column migration (`database.py`)

**Add to `_SCHEMA_SQL`** (`predicate_translation` CREATE, after `object_entity_types TEXT,` at `:48`): `bindings TEXT,`. Role: fresh DBs get the column from CREATE.

**Add an idempotent migration** in `create_schema`, mirroring the D33 pattern (`:167-173`). After the `subject_entity_types`/`object_entity_types` loop:
```python
try:
    conn.execute("ALTER TABLE predicate_translation ADD COLUMN bindings TEXT")
except sqlite3.OperationalError:
    pass  # column already exists
```
Role: existing DBs (incl. `aedos_phase10_5.db`) gain the column additively; legacy rows keep `bindings IS NULL`, and `_row_to_metadata` read-synthesizes from scalars — **non-destructive, additive, idempotent** per the interface contract.

**Add two NEW tables** to `_SCHEMA_SQL` (after `entity_resolution_cache`, before the indexes at `:124`):

```sql
CREATE TABLE IF NOT EXISTS property_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_namespace TEXT NOT NULL,
    kb_property TEXT NOT NULL,        -- the property the relations are ABOUT
    relation_type TEXT NOT NULL,      -- subject_type_constraint(P2302/Q21503250) |
                                      -- value_type_constraint(P2302/Q21510865) |
                                      -- inverse(P1696) | subproperty(P1647) |
                                      -- related(P1659) | single_value(P2302/Q19474404)
    related_value TEXT,               -- a Q-id (type/inverse) or P-id (subproperty/related)
    source TEXT NOT NULL,             -- 'wikidata_p2302' | 'wikidata_p1647' | ...
    created_at TEXT NOT NULL,
    last_consulted_at TEXT,
    used_count INTEGER DEFAULT 0,
    retracted_at TEXT,
    retraction_reason TEXT,
    UNIQUE(kb_namespace, kb_property, relation_type, related_value)
);

CREATE TABLE IF NOT EXISTS substrate_exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aedos_predicate TEXT NOT NULL,
    kb_namespace TEXT,
    property_path TEXT NOT NULL,        -- single P-id or '/'-joined path
    scope_key TEXT NOT NULL,            -- subject Q-id OR subtree-root Q-id
    scope_kind TEXT NOT NULL DEFAULT 'subject',  -- 'subject' | 'subtree'
    reason TEXT NOT NULL,               -- why the binding does NOT hold here
    created_at TEXT NOT NULL,
    last_consulted_at TEXT,
    used_count INTEGER DEFAULT 0,
    UNIQUE(aedos_predicate, kb_namespace, property_path, scope_key)
);

CREATE INDEX IF NOT EXISTS idx_property_relations_prop
    ON property_relations(kb_namespace, kb_property);
CREATE INDEX IF NOT EXISTS idx_substrate_exceptions_lookup
    ON substrate_exceptions(aedos_predicate, scope_key);
```

Add `"property_relations"` and `"substrate_exceptions"` to `TABLE_NAMES` (`:289-297`). Role: `property_relations` is the cache for ontology-discovered bindings (Decision 1); `substrate_exceptions` is the **bounded NOGOOD cache** (P2303-style, Decision 3 — note: full retraction/provenance is WS3, but the table is created here because the binding-loop in §1.6 reads it as the asymmetric-trust gate).

**Add to `seed_loader.load_seeds_into_connection`** (`:102-126`): the INSERT column list does not include `bindings`; leave it out (legacy seed rows stay scalar-synthesized). No change needed to seed_loader for the column — read-synthesis covers it. **Do** delete the synonym alias entries from the seed JSON (§2).

### 1.3 NEW `property_relations` module + query builders (Decision 1.c)

**New file** `src/aedos/layer3_substrate/property_relations.py`. Exposes a `PropertyRelations` class:

```python
class PropertyRelations:
    def __init__(self, db, kb_protocol, *, ttl_days=30): ...
    def fetch(self, kb_property: str, kb_namespace: str = "wikidata") -> PropertyOntology: ...
    # returns cached rows if fresh; else queries KB, caches, returns.
```

`PropertyOntology` dataclass: `subject_type_qids: list[str]`, `value_type_qids: list[str]`, `inverse_pids: list[str]`, `subproperty_pids: list[str]`, `related_pids: list[str]`, `single_valued: bool`. Role: this is the structured form of P2302 constraints used to BUILD `PredicateBinding`s (constrained subject/value types, single-value flag) and to discover sibling/inverse properties.

**Add query builders to `kb_wikidata.py`** (alongside `_build_subsumption_ask_query` etc., ~after `:385`). One SPARQL SELECT that pulls a property's constraints in a single round-trip:

```python
def _build_property_ontology_query(prop: KBPropertyID) -> str:
    """P2302 constraint statements + P1647/P1696/P1659 relations for `prop`.
    Constraints: subject-type (Q21503250) → P2308 class; value-type
    (Q21510865) → P2308 class; single-value (Q19474404). Plus P1647
    (subproperty of), P1696 (inverse property), P1659 (related property)."""
    if not _PROPERTY_ID_PATTERN.match(prop):
        raise ValueError(f"Invalid Wikidata property ID: {prop!r}")
    # uses p:P2302 / pq:P2308 / pq:P2305 against wd:Pxxx, plus wdt:P1647 etc.
    return ( ... )   # SELECT ?relType ?cls ?subProp ?invProp ?relProp WHERE { ... }
```

**Add a `WikidataAdapter` method** `fetch_property_ontology(self, prop) -> dict` that runs `_build_property_ontology_query` through the SPARQL path (clone the retry/rate-limit/cache shape of `_sparql_label_type_fallback` `:1026-1108`, using `self._sparql_limiter`, statement TTL, one audit event `kb_property_ontology`). Returns a parsed dict the `PropertyRelations.fetch` caches into `property_relations`. **Also add** `fetch_label(self, qid) -> Optional[str]` (wbgetentities `props=labels`, single Q-id; reuses `_search_limiter`) — required by Decision 5's `_format_correction` reverse-label, declared here because the discovery flow and the correction surface both need it.

Add a method to the `KBProtocol` (`kb_protocol.py:55-94`): `def fetch_property_ontology(self, prop: KBPropertyID) -> dict: ...` and `def fetch_label(self, qid: KBEntityID) -> Optional[str]: ...`. Mark both with the fail-open contract in the docstring. Update the fixture path in `WikidataAdapter` with `_fixture_property_ontology` / `_fixture_label` reading `tests/fixtures/wikidata/property_ontology_<P>.json` / `label_<Q>.json` (empty-on-miss), so non-live tests are deterministic.

### 1.4 Binding-DISCOVERY flow (Decision 1.d) — in `predicate_translation.py`

Rewrite `_generate_and_store` (`:386-507`) so that, after the oracle returns metadata, when `routing_hint == "kb_resolvable"`:

1. Collect **candidate P-ids**: the oracle's primary `raw["kb_property"]`, plus an OPTIONAL new oracle field `candidate_kb_properties: list[str]` added to `PREDICATE_METADATA_TOOL.input_schema` (`:20-101`) — a `["array","null"]` of additional plausible P-ids. Role: lets the oracle propose a ranked set instead of one (Decision 1: "RANKED SET … discovered primarily from Wikidata's own property ontology"). The prompt (`_GENERATION_SYSTEM_PROMPT` `:104-246`) gains one paragraph: "If more than one property could fit, list the others in candidate_kb_properties; evidence will arbitrate."
2. For each candidate P-id, call `PropertyRelations.fetch(pid)` → build a `PredicateBinding` with `subject_entity_types`/`object_entity_types` from the ontology's constrained types (falling back to the oracle's `subject_entity_types`/`object_entity_types` when the ontology has none), `single_valued` from the ontology's single-value constraint **OR** the oracle's `single_valued` (ontology wins when present — it is authoritative), `slot_to_qualifier` from the oracle (ontology doesn't carry Aedos slot maps), `source="ontology_p2302"`.
3. **SLING fallback** (§1.5): if `PropertyRelations.fetch` returns an *empty* ontology for a candidate (the property has no P2302 constraints — common for long-tail edges), call `SlingFallback.propose_bindings(predicate, raw)` to synthesize a `PredicateBinding` with `source="sling"` and a lower `rank`.
4. Rank the bindings (ontology-typed first, then oracle-primary, then SLING) and persist them as the `bindings` JSON column (new), **while still writing the legacy scalar columns from `bindings[0]`** for back-compat / consistency-checker / seed parity.

`PredicateTranslation.__init__` (`:284-293`) gains an optional `property_relations=None` and `sling=None`; `pipeline.py:136` passes `PropertyRelations(db, kb)` and `SlingFallback(db, kb, llm_client)` (see §4).

### 1.5 NEW SLING-style distant-supervision fallback module (Decision 1.e)

**New file** `src/aedos/layer3_substrate/sling_fallback.py`. `SlingFallback` class:

```python
class SlingFallback:
    def __init__(self, db, kb_protocol, llm_client): ...
    def propose_bindings(self, predicate: str, oracle_raw: dict) -> list[PredicateBinding]:
        """Distant supervision: for a predicate the property ontology can't
        constrain, sample entity pairs the oracle's primary property links,
        enumerate the KB properties that co-occur on those pairs
        (enumerate_neighbors), and propose the most-frequent property as a
        candidate binding. Single binding, source='sling', low rank.
        Fails open (returns [] on any KB/LLM error) — soundness over coverage."""
```

It reuses `WikidataAdapter.enumerate_neighbors` (already on the protocol, `kb_protocol.py:89-94`) to gather co-occurring properties; no new KBProtocol method needed. Caches discovered edges into `property_relations` with `source="sling"`. Role: the "edges the ontology lacks" fallback (Decision 1).

### 1.6 `kb_verifier.verify` rewritten as a binding LOOP with evidence arbitration (Decision 1.f)

Current `verify` (`:94-302`) is single-property. Rewrite the core:

- **Gate** (`:128-129`): change `if meta.routing_hint != "kb_resolvable" or not meta.kb_property` → `if meta.routing_hint != "kb_resolvable" or not meta.bindings`. (`meta.kb_property` property still returns `None` when bindings empty, so behavior identical for the no-binding case.)
- **Loop**: iterate `meta.bindings` (ranked). For each binding build the per-binding equivalents of the current scalars — replace every `meta.kb_property` (`:196`, `:217`, `:263`, `:283`, `:395`), `meta.slot_to_qualifier` (`:142`, `:524` — pass binding to `_lookup_targets`), `meta.single_valued` (`:288`, `:363`), `_types_for_slot(meta, …)` (`:161`, `:195`, `:536-544`) with the **binding's** fields. Refactor `_lookup_targets(claim, meta)` → `_lookup_targets(claim, binding)` and `_types_for_slot(meta, slot)` → `_types_for_slot(binding, slot)`.
- **Evidence arbitration**: run the existing `_compare_positive` per binding. Collect results. Decision rule (the copula fix):
  - If **any** binding yields `VERIFIED` → VERIFIED (record all verifying chains in trace — Decision 1 "EXAMINE/RECORD BOTH").
  - `CONTRADICTED` only when a binding is **`single_valued` AND** the resolved object satisfies that binding's **value-type constraint** (from `property_relations`, the P2302 value-type). This is the P31-vs-P106 fix: a copula claim ("X is a physicist") routed ambiguously to P31 (instance-of) vs P106 (occupation) — the resolved object's type (occupation Q28640) satisfies P106's value-type but not P31's, so only the P106 binding can drive a verdict; the P31 binding is value-type-incompatible and cannot contradict. Add a helper `_object_satisfies_value_type(resolved_obj_qid, binding, kb)` that checks the resolved object's P31 against the binding's `object_entity_types`/ontology value-type via `kb.subsumption(obj, value_type, "is_a")`. This **supersedes** the coarse `_contradiction_value_type_ok` (`:583-593`) datatype check (keep that as a cheap pre-filter; add the type-constraint check on top).
  - **NOGOOD gate**: before promoting a binding to CONTRADICTED, consult `substrate_exceptions` for `(predicate, property_path, subject-qid)` — if a cached "does not hold" exists, skip that binding. After computing a sound CONTRADICTED, write the NOGOOD eagerly (Decision 3 "cached EAGERLY; positive bindings only as re-verifiable hypotheses").
  - If no binding verifies and none soundly contradicts → NO_MATCH (carry the per-binding `abstention_reason`s in the trace).
- **Carry contradicting value** (Decision 5 — flagged here because it lives in this method): when CONTRADICTED, set `kb_result.matched_statement.value` onto the trace as `contradicting_value` (it already flows via `matched_statement`; add explicit `trace["contradicting_value"] = statement.value`). The `ClaimVerdict.contradicting_value` plumbing is WS where ClaimVerdict lives, but the verifier must surface it — do it here.

The `KBVerdict.trace` (`:75-80`) gains `bindings_tried: list[dict]` (per-binding property, verdict, abstention_reason) for observability (Decision 5).

### 1.7 DELETE normalization `_CANONICAL_MAP` + synonym seed rows (Decision 1.g)

- **`normalization.py`**: delete `_CANONICAL_MAP` (`:15-66`). Rewrite `normalize_predicate` (`:74-110`) to keep ONLY the mechanical normalization: lower/strip, underscore↔space, single aux-prefix strip (`_AUX_PREFIX` `:68-71` stays), trailing-article strip, snake_case. Remove the two `if … in _CANONICAL_MAP` branches (`:92-93`, `:98-99`). The result: `"works at"` → `works_at` (mechanical), not `employed_by`. The substrate's multi-property discovery + the seed pack's canonical rows now carry the synonymy that the map hardcoded — consistent with the user's "no hardcoded mappings" invariant.
- **`seeds/predicate_translation.json`**: delete the ~21 pure-synonym alias rows (the Phase-H-Cluster-3 block): `received_award`, `won_award`, `award_received`, `birthplace_is`, `death_place_is`, `held_position`, `occupied_position`, `part_of_region`, `works_at`, `authored`, `graduated_from`, `date_of_birth`, `date_of_death`, `founded_in`, `inception_date`, `has_population`, `shares_border_with`, `spouse`, `successor_of`, plus `instance_of` (alias of `is_a`) and `won_prize` (alias of `awarded`). **Keep** the canonical rows they aliased (`awarded`, `born_in`, `died_in`, `holds_role`, `part_of`, `employed_by`, `authored_by`, `educated_at`, `born_on`, `died_on`, `founded_in_year`, `population_of`, `adjacent_to`, `spouse_of`, `has_successor`, `is_a`). Soundness: with normalization no longer collapsing surfaces, the extractor now emits e.g. `works_at`; the oracle's cold-start discovery (§1.4) resolves it to P108. The alias rows become discovery targets, not seed rows.

---

## 2. DELETIONS

| File:lines | What | Why safe |
|---|---|---|
| `normalization.py:15-66` | `_CANONICAL_MAP` (52 entries) | Only readers are `normalize_predicate` itself (`:92-99`); knowledge moves to substrate discovery. **Net LOC decrease.** |
| `normalization.py:92-93, 98-99` | the two map-lookup branches in `normalize_predicate` | Replaced by mechanical-only normalization. |
| `seeds/predicate_translation.json` — 21 alias rows (lines 254-265 `instance_of`; 591-601 `won_prize`; 772-999 the Cluster-3 block) | Pure synonym rows | Each duplicates a canonical row's `kb_property`+`slot_to_qualifier`; discovery now finds them. **Reduces seed count from ~80 to ~59.** |
| `kb_verifier.py:583-593` `_contradiction_value_type_ok` | downgraded, not deleted | Kept as cheap datatype pre-filter; superseded for the *decision* by `_object_satisfies_value_type`. |

Net effect targets the contract's "LOC should net DECREASE": deleting 52 map entries + 21 seed rows + the scalar-column synthesis dead-weight outweighs the new modules.

---

## 3. ADDITIONS

| File | Block | Role |
|---|---|---|
| `predicate_translation.py` | `PredicateBinding` dataclass; `PredicateMetadata.bindings` + scalar `@property` accessors + `from_scalars` classmethod; `candidate_kb_properties` tool field; discovery flow in `_generate_and_store`; `bindings` read-synthesis in `_row_to_metadata` | Multi-property substrate core |
| `database.py` | `bindings TEXT` column + idempotent ALTER; `property_relations` + `substrate_exceptions` tables + indexes; `TABLE_NAMES` entries | Schema (additive/idempotent) |
| `property_relations.py` (NEW) | `PropertyRelations`, `PropertyOntology` | Ontology fetch + cache |
| `kb_wikidata.py` | `_build_property_ontology_query`; `WikidataAdapter.fetch_property_ontology`, `.fetch_label`; fixture variants | KB query builders for P2302/P1647/P1696/P1659 |
| `kb_protocol.py` | `fetch_property_ontology`, `fetch_label` protocol methods | Protocol parity |
| `sling_fallback.py` (NEW) | `SlingFallback.propose_bindings` | Distant-supervision fallback |
| `kb_verifier.py` | binding-loop `verify`; `_object_satisfies_value_type`; NOGOOD read/write; `bindings_tried`/`contradicting_value` trace; `_lookup_targets(claim, binding)`, `_types_for_slot(binding, slot)` refactors | Evidence arbitration + copula fix |

---

## 4. CALL-SITES / CONSUMERS TO UPDATE (grep-verified)

**Scalar-field readers — all keep working via the `@property` accessors (NO edit needed), confirmed per site:**
- `kb_verifier.py:128,142,196,217,263,283,288,363,395,524,541,543` — rewritten anyway by §1.6 to iterate bindings.
- `walker.py:776,803` (`meta.kb_property`), `walker.py:903` (`meta.single_valued`) — property accessor returns binding[0]; **no edit required**, but §6 notes walker should eventually loop bindings (deferred to Composition WS).
- `tier_u.py:606` (`.single_valued`) — property accessor; no edit.
- `predicate_translation.py:353,357` (`subject.kb_property` in `query_neighbors`) — property accessor; no edit.
- `consistency.py:150,157,164-192` — reads the `kb_property`/`slot_to_qualifier` **columns** (still written from bindings[0]); no edit.

**Constructor sites that pass scalar kwargs to `PredicateMetadata(...)` — MUST switch to `from_scalars`:**
- `predicate_translation.py:492` (`_generate_and_store`), `:531` (`_row_to_metadata`).
- `router.py:14` (`predicate_metadata: Optional[PredicateMetadata]`) — type only, no construction; no edit. `router.py:31` `consult` — fine.
- `validator.py:39` — receives `PredicateMetadata`; reads no removed scalar; verify no `.kb_property` use (none found); no edit.

**`normalize_predicate` caller:** `extractor.py:611` — no signature change; behavior changes (mechanical only). Verify extractor downstream tolerates `works_at` etc. (it routes through the oracle, which now discovers).

**Construction wiring:** `pipeline.py:136` — change to `PredicateTranslation(db=db, llm_client=client, consistency_checker=consistency, property_relations=PropertyRelations(db, kb), sling=SlingFallback(db, kb, client))`. Note `kb` is built at `:123` before `:136`, so ordering is fine.

**`query_neighbors` (subsumption variant) `subsumption.py:141`** — unrelated (subsumption table); no edit.

---

## 5. AFFECTED TESTS

**will-break (need update):**
- `tests/unit/test_normalization.py:12-149` — ~25 asserts expecting `_CANONICAL_MAP` collapses (`"is employed by"→"employed_by"`, `"lives in"→"located_in"`, `"won"→"received_award"`, `"wrote"→"authored"`, etc.). Rewrite to expect mechanical snake_case (`"is employed by"→"employed_by"` becomes `"employed by"→"employed_by"` only if aux-strip yields it — actually `"is employed by"`→ aux-strip→`"employed by"`→`employed_by`, still passes; but `"lives in"→"located_in"`, `"won"→"received_award"`, `"served as"→"holds_role"`, `"wrote"→"authored"` all BREAK). Reclassify these as discovery-layer expectations, not normalization.
- `tests/unit/test_predicate_translation.py:71-135,503-551` — `PredicateMetadata(...)` constructed with `kb_property=`, `slot_to_qualifier=`, `subject_entity_types=` kwargs. Update to `from_scalars(...)` or set `bindings=[PredicateBinding(...)]`. Add new asserts: `meta.bindings`, `meta.kb_property` property still returns binding[0].
- `tests/unit/test_kb_verifier.py:29-94,222-442` — `MockTransport.extract_with_tool` returns the scalar dict (`:38-48`); `consult` now also runs discovery. Add `candidate_kb_properties: None`; ensure `PredicateTranslation` built with `property_relations=None` falls back to oracle-only (single binding from scalars) so these stay green. The single_valued/value-type tests (`:226-272,423-442`) now exercise the binding-loop; verify the P585-mismatch test (`:423-442`) still abstains via `_object_satisfies_value_type`.
- `tests/integration/test_inverse_predicate_kb.py`, `test_kb_path.py:36-49,72`, `test_walker_with_substrate.py:32-50` — mock transports returning scalar dict: add `candidate_kb_properties` key (or make discovery tolerate its absence — preferred; treat missing as `[]`).
- `tests/unit/test_corpus_runner_predicate_metadata.py:22-31` — stand-in `PredicateMetadata` with `.kb_property` attr; keep as-is (it's a stub), but the corpus runner comparison (`test_corpus_runner.py:391-394`) reads `meta.kb_property`/`meta.slot_to_qualifier` properties — works.

**needs-update (schema):**
- `tests/unit/test_database.py:99-100` — column-name assertion list must add `"bindings"`. `:191-243` migration tests — add a `bindings`-absent→present migration test.
- `tests/unit/test_seed_loader.py`, `test_seed_pack_predicate_coverage.py:213-217` — alias-row counts and per-predicate assertions; the deleted aliases (`has_population`, `works_at`, `successor_of`, etc.) must be removed from expectations. `test_seed_pack_predicate_coverage.py:64-67` documents the alias pattern — rewrite to reflect deletion.

**new-needed:**
- `test_property_relations.py` — `PropertyRelations.fetch` caches + reuses; empty-ontology → SLING path.
- `test_sling_fallback.py` — `propose_bindings` returns ranked binding; fail-open on KB error.
- `test_kb_verifier_multiproperty.py` — copula P31-vs-P106 arbitration; multi-binding VERIFIED records both; NOGOOD gate suppresses re-contradiction; value-type-incompatible binding cannot contradict.
- `test_predicate_binding_synthesis.py` — legacy scalar row → single binding; `bindings` JSON round-trips.

**unaffected (verified):** `test_consistency_checker.py` (reads columns), `test_router.py`, `test_walker.py` (mock dicts with scalars), `test_trace.py` (`kb_property` is a TraceEdge metadata literal, unrelated).

---

## 6. ORDERING / DEPENDENCIES

1. **`database.py`** — add `bindings` column + two tables + migration (everything else reads these). Independent; ship first.
2. **`predicate_translation.py`** — `PredicateBinding`, `bindings` field + properties + `from_scalars`, `_row_to_metadata` read-synthesis. Depends on (1). Keep discovery flow stubbed (oracle-only single binding) so the system stays functional.
3. **`kb_protocol.py` + `kb_wikidata.py`** — `fetch_property_ontology`/`fetch_label` + query builders + fixtures. Independent of (2) at the interface level.
4. **`property_relations.py`** — depends on (1) + (3).
5. **`sling_fallback.py`** — depends on (1) + existing `enumerate_neighbors`.
6. **`predicate_translation.py` discovery flow** — wire (4)+(5) into `_generate_and_store`; add `candidate_kb_properties` tool field + prompt. Depends on (2,4,5).
7. **`kb_verifier.py`** — binding-loop + arbitration + NOGOOD. Depends on (2) for `meta.bindings`, (4) for value-type constraints, (1) for `substrate_exceptions`.
8. **`normalization.py` + seed deletions** — last (so the discovery flow that replaces them is already live). Depends on (6) being functional.
9. **`pipeline.py:136`** wiring — alongside (6).

System stays functional after each step: (2) read-synthesizes so verify still works on scalar rows; (7) falls back to single-binding behavior identical to today when only one binding exists.

---

## 7. RISKS / SOUNDNESS

- **`@property` shadowing `__init__` kwargs** is the sharpest footgun: making `kb_property` a property removes it from the dataclass field set, so any positional/kw construction breaks at import/runtime. Mitigation: `from_scalars` classmethod + grep-confirmed conversion of the 2 internal constructors and ~12 test constructors (§5). **This must be done atomically with step 2 or the suite won't import.**
- **False-verify guard (NEVER false-verify, §3.2):** the binding loop's "any binding VERIFIED → VERIFIED" is sound only because each binding's `_compare_positive` already requires a scope-compatible value match or a sound subsumption upgrade. The new risk is a *wrong* SLING binding verifying spuriously — mitigated by SLING bindings carrying lowest rank and `source="sling"`, and by NOT letting SLING bindings drive CONTRADICTED (only ontology-typed `single_valued` bindings with satisfied value-type can contradict).
- **Copula contradiction soundness:** `_object_satisfies_value_type` must fail **closed** — if the resolved object's type can't be confirmed against the binding's value-type (KB error / no P31), the binding cannot contradict (abstain). This preserves the architecture's "resolution failure → false-abstain, never false-contradiction."
- **NOGOOD eager-cache vs positive-hypothesis asymmetry:** writing CONTRADICTED NOGOODs eagerly while treating VERIFIED as re-verifiable hypotheses (Decision 3) is correct only if NOGOODs are scoped tightly (`(predicate, property_path, subject-qid)` UNIQUE). A too-broad subtree NOGOOD could suppress a legitimate later verification — keep `scope_kind='subject'` as the default and only widen to `'subtree'` from WS3's provenance machinery.
- **Migration on `aedos_phase10_5.db`:** the `bindings` ALTER + new tables are additive; legacy rows read-synthesize. Verified the existing D33/single_valued ALTER pattern (`database.py:157-214`) is the template — same `try/except OperationalError`. Idempotent on re-open.
- **Normalization deletion regression window:** between deleting `_CANONICAL_MAP` and discovery being live, surface forms like `"lives in"` would route to a cold-start oracle call instead of the seed `located_in`. Mitigated by ordering (step 8 last) and by the seed pack retaining the canonical rows. The 25 `test_normalization.py` asserts that encode synonymy must be reclassified, not deleted silently — they document a behavior that moved layers.
- **Live SPARQL cost:** `fetch_property_ontology` adds one SPARQL round-trip per *new* predicate discovery (cached in `property_relations` with 30-day TTL); SLING adds `enumerate_neighbors` calls only when the ontology is empty. Both are discovery-time (cold-start), not verify-time, so steady-state verify cost is unchanged. Rate-limited via the existing `_sparql_limiter`/`_search_limiter`.


##########################################################################################
