# Aedos v0.16 ? Change Specification: Workstream 3 ? Partial TMS: Provenance, Nogood Cache, Lazy Premise-Retraction

*Implementation-ready spec. Conforms to the interface contract in `00_overview_contract_ordering.md`, which is AUTHORITATIVE on all cross-workstream interfaces (data model, shared tables, ordering, soundness-sensitive deletion order). All file:line references were verified by the spec-mapping pass against the v0.16 base (identical to `main` at branch creation).*

---

# WORKSTREAM 3 — Partial TMS: Lazy Provenance + Nogood/Exception Cache + Lazy Premise-Retraction — Implementation-Ready Change Spec

This spec covers v0.16 Decision 3 (Partial TMS / Provenance) plus the D13 fix and the `verify_transitive_path` nogood consult. All file:line references are verified against the current code against the v0.16 base. LOC nets roughly flat-to-negative: `retraction.py`/`contradiction_tracer.py` lose dead eager-cascade paths and gain a single provenance-driven path; the additions are concentrated in `trace.py`, `database.py`, and a new small `substrate_exceptions` module.

---

## (1) DETAILED CHANGE SPEC

### 3A. The `JustificationTrace.provenance` term — structured lazy AND/OR over premise ids + sources

**Role.** A per-claim, session-scoped provenance term recording WHICH premises (and their retractable row ids + sources) the verdict's derivation rests on, combined with AND/OR structure. It replaces the monotonic boolean `chain_includes_assertion` as the *source of truth*: `chain_includes_assertion` becomes a derived property. It is built lazily by the walker as edges are appended, discarded per session (never persisted as an eager global web — only the flattened `(table,row_id)` list is persisted, exactly as today via `verdict_recorded`).

**Current code — `trace.py:22-37`:**
```python
@dataclass
class JustificationTrace:
    root: TraceNode
    edges: list[TraceEdge] = field(default_factory=list)
    polarity_trace: list[int] = field(default_factory=list)
    source_breakdown: dict = field(default_factory=dict)
    walk_metadata: dict = field(default_factory=dict)
    chain_includes_assertion: bool = False
```

**Added — new provenance dataclasses + field in `trace.py` (after `TraceEdge`, before `JustificationTrace`):**

```python
# Semiring-style provenance literal: one grounded premise the verdict
# rests on. `table`/`row_id` is the retractable substrate/Tier U row
# (None for transient sources e.g. live KB statements with no cached row);
# `source` ∈ {tier_u, kb, python, subsumption, predicate_translation,
# entity_resolution}; `status` carries the Tier U premise status when
# source=='tier_u' (asserted_unverified | externally_verified | ...),
# else None. `assertion` is True iff this literal makes the verdict
# assertion-conditional (an asserted_unverified Tier U premise, or the
# Q-UserAuth pre-seed).
@dataclass(frozen=True)
class ProvenanceLiteral:
    source: str
    table: Optional[str] = None
    row_id: Optional[int] = None
    status: Optional[str] = None
    assertion: bool = False

# AND/OR provenance term. `op` ∈ {'lit','and','or'}. A 'lit' node wraps
# one ProvenanceLiteral; 'and'/'or' nodes combine children. The walker
# composes a term per claim: each grounded premise contributes one
# alternative (OR across independent grounding chains found in one walk),
# and a multi-hop chain ANDs its hops. Lazy: built only while the walk
# runs, discarded with the trace at session end.
@dataclass
class ProvenanceTerm:
    op: str = "or"                       # default: OR over alternatives
    literal: Optional[ProvenanceLiteral] = None
    children: list["ProvenanceTerm"] = field(default_factory=list)

    @classmethod
    def lit(cls, literal: ProvenanceLiteral) -> "ProvenanceTerm":
        return cls(op="lit", literal=literal)

    def add_alternative(self, term: "ProvenanceTerm") -> None:
        """OR a fresh grounding alternative into this (root) term."""
        self.children.append(term)

    def literals(self) -> list[ProvenanceLiteral]:
        if self.op == "lit" and self.literal is not None:
            return [self.literal]
        out: list[ProvenanceLiteral] = []
        for c in self.children:
            out.extend(c.literals())
        return out

    def includes_assertion(self) -> bool:
        """True iff ANY literal on the term is assertion-conditional.
        chain_includes_assertion derives from this (monotone-OR over
        literals, matching the legacy boolean's monotonic semantics)."""
        return any(l.assertion for l in self.literals())

    def source_rows(self) -> list[tuple[str, int]]:
        """Distinct (table,row_id) pairs across all literals — the
        retraction dependency footprint. Mirrors aggregator._extract_source_rows
        but sourced from the term rather than re-scanning edges."""
        seen: set[tuple[str, int]] = set()
        rows: list[tuple[str, int]] = []
        for l in self.literals():
            if l.table is not None and l.row_id is not None and (l.table, l.row_id) not in seen:
                seen.add((l.table, l.row_id))
                rows.append((l.table, l.row_id))
        return rows
```

**Field added to `JustificationTrace` (replacing the boolean, with back-compat shim):**

```python
@dataclass
class JustificationTrace:
    root: TraceNode
    edges: list[TraceEdge] = field(default_factory=list)
    polarity_trace: list[int] = field(default_factory=list)
    source_breakdown: dict = field(default_factory=dict)
    walk_metadata: dict = field(default_factory=dict)
    # WS3: lazy AND/OR provenance term. The walker populates it as edges
    # are appended. chain_includes_assertion is now a DERIVED property.
    provenance: ProvenanceTerm = field(default_factory=ProvenanceTerm)

    @property
    def chain_includes_assertion(self) -> bool:
        return self.provenance.includes_assertion()
```

**Why a property, not a field.** Five walker sites currently do `trace.chain_includes_assertion = True` (`walker.py:321, 484, 528, 590`; plus the read at `191`). A property with no setter would break those assignments. Two options — chosen: **keep `chain_includes_assertion` as a settable field that mirrors the term**, OR convert the five assignment sites to push a literal. The disciplined choice (per "knowledge belongs in the structured form") is to convert the assignment sites to push provenance literals and make `chain_includes_assertion` a read-only derived property. See §3B for the exact five edits. The `trace_to_json` serializer is updated to emit both the derived boolean (back-compat for `test_aggregator.py:338`) and a flattened provenance view.

**Updated `trace_to_json` — `trace.py:40-60`:**
```python
def trace_to_json(trace: JustificationTrace) -> dict:
    ...
    def _prov(t: ProvenanceTerm) -> dict:
        if t.op == "lit" and t.literal is not None:
            return {"op": "lit", "literal": asdict(t.literal)}
        return {"op": t.op, "children": [_prov(c) for c in t.children]}
    return {
        "root": _node(trace.root),
        "edges": [_edge(e) for e in trace.edges],
        "polarity_trace": trace.polarity_trace,
        "source_breakdown": trace.source_breakdown,
        "walk_metadata": trace.walk_metadata,
        "chain_includes_assertion": trace.chain_includes_assertion,  # derived
        "provenance": _prov(trace.provenance),
    }
```
(`asdict` is already imported at `trace.py:4`.)

---

### 3B. Walker populates the provenance term (the five `chain_includes_assertion = True` sites + the grounded-premise edges)

The walker appends a `ProvenanceLiteral` (wrapped in a `lit` term, OR'd into `trace.provenance`) at every point it appends a grounding `TraceEdge`, and removes the direct boolean assignments. A small private helper avoids repetition.

**Add helper on `Walker` (near `walker.py:411`, before `_direct_lookup`):**
```python
def _record_premise(self, trace, *, source, table=None, row_id=None,
                    status=None, assertion=False):
    """WS3: append one grounding premise to the trace's provenance term
    as a fresh OR-alternative. Each grounded premise found in a walk is
    an independent way the verdict could hold (OR); a future multi-hop
    composition ANDs hops within one alternative. Centralizes literal
    construction so every grounding site contributes provenance."""
    from ..layer5_result.trace import ProvenanceLiteral, ProvenanceTerm
    trace.provenance.add_alternative(
        ProvenanceTerm.lit(ProvenanceLiteral(
            source=source, table=table, row_id=row_id,
            status=status, assertion=assertion,
        ))
    )
```

**Edit 1 — Q-UserAuth pre-seed, `walker.py:320-321`:**
```python
        if self._predicate_routing(claim.predicate) == "user_authoritative":
            trace.chain_includes_assertion = True
```
→
```python
        if self._predicate_routing(claim.predicate) == "user_authoritative":
            self._record_premise(trace, source="tier_u", assertion=True)
```

**Edit 2 — polarity-conflict belief revision, `walker.py:483-484`** (inside the existing `if flipped_status == "asserted_unverified":`):
```python
            if flipped_status == "asserted_unverified":
                trace.chain_includes_assertion = True
```
→
```python
            self._record_premise(
                trace, source="tier_u", table="tier_u",
                row_id=flipped_row.get("id"), status=flipped_status,
                assertion=(flipped_status == "asserted_unverified"),
            )
```
(Records the row id unconditionally — D13-style retractability — and sets `assertion` only for `asserted_unverified`.)

**Edit 3 — object-conflict belief revision, `walker.py:527-528`:**
```python
                if oc_status == "asserted_unverified":
                    trace.chain_includes_assertion = True
```
→
```python
                self._record_premise(
                    trace, source="tier_u", table="tier_u",
                    row_id=oc_row.get("id"), status=oc_status,
                    assertion=(oc_status == "asserted_unverified"),
                )
```

**Edit 4 — Stage-1 verified Tier U match, after the `TraceEdge` append at `walker.py:547-556`** (currently there is NO provenance/flag set on this path; add one so the verified premise is in the term and thus retractable):
```python
            self._record_premise(
                trace, source="tier_u", table="tier_u",
                row_id=row_id, status=row_status,
                assertion=False,  # status branch below upgrades when asserted
            )
```

**Edit 5 — Q-Lookup-α fallthrough, `walker.py:588-591`:**
```python
            # No external grounding available; chain stays
            # assertion-conditional.
            trace.chain_includes_assertion = True
            return "verified", "tier_u", llm_delta
```
→
```python
            # No external grounding available; chain stays
            # assertion-conditional. Mark the Stage-1 premise literal
            # assertion-conditional by appending an assertion literal
            # (OR semantics: includes_assertion() now True).
            self._record_premise(
                trace, source="tier_u", table="tier_u",
                row_id=row_id, status="asserted_unverified", assertion=True,
            )
            return "verified", "tier_u", llm_delta
```

**Edit 6 — KB premise edges, `_try_external_grounding`.** At the two KB `TraceEdge` appends (`walker.py:670-680` verified branch and `689-697` contradicted branch) and the Python branch (`721-729`), add the matching provenance literal. KB/Python have no retractable substrate row in the current schema EXCEPT the resolver's `entity_resolution_cache` row (D13 — see §3C). So:

- KB verified/contradicted: `self._record_premise(trace, source="kb", table="entity_resolution_cache", row_id=<resolver cache id>, assertion=False)` — the `row_id` comes from the D13 plumbing in §3C.
- Python: `self._record_premise(trace, source="python", assertion=False)` (no retractable row).
- `kb_quantitative` (`walker.py:653-659`): `self._record_premise(trace, source="kb", assertion=False)`.

**`_apply_assertion_designation` (`walker.py:181-200`) is unchanged in behavior** — it reads `trace.chain_includes_assertion`, which now resolves through the derived property. The lazy import of `_BASE_OF_DUAL` stays. The single read at `walker.py:191` works as-is.

---

### 3C. D13 — record `entity_resolution_cache` row id on KB premise_lookup edges + add to `_TRACE_ROW_ID_KEYS`

**Problem.** KB verdicts are currently NOT retractable: the walker's KB `premise_lookup` edges carry only `{"source":"kb","entity":...,"lookup_inverted":...}` (`walker.py:675-679`, `693-696`) — no row id — and `_TRACE_ROW_ID_KEYS` (`aggregator.py:111-115`) has no `entity_resolution_cache` key. So when the resolver's cached entity resolution is later retracted, dependent KB verdicts are not propagated.

**Fix, three coordinated edits:**

**(i) Resolver exposes the cache row id.** `EntityResolver.resolve` (`resolver.py:48`) currently returns `list[ResolutionCandidate]`; the cache row id it reads/writes (`resolver.py:110 cached["id"]`, and the `INSERT`s at `86,145,168`) is not surfaced. Add a cheap accessor rather than changing the return type (which `kb_verifier.py:168-170, 209-211` and `walker.py:795-797` consume):

Add to `EntityResolver`:
```python
def last_cache_row_id(self) -> Optional[int]:
    """WS3 D13: the entity_resolution_cache row id touched by the most
    recent resolve() on this resolver. None when the last resolve did
    not hit/create a cache row (mock paths, normalizer Stage-C INSERT
    OR IGNORE that collided). Request-scoped; the KBVerifier reads it
    immediately after select() to stamp the premise edge."""
    return self._last_cache_row_id
```
Initialize `self._last_cache_row_id = None` in `__init__` (`resolver.py:32`). Set it in `resolve`:
- cache-hit branch (`resolver.py:117`): `self._last_cache_row_id = cached["id"]`.
- positive INSERT branch (`resolver.py:142-157`): after the INSERT, `self._last_cache_row_id = self._db.execute("SELECT last_insert_rowid()").fetchone()[0]`.
- negative-cache and Stage-C INSERT branches: set to the `last_insert_rowid()` likewise (so even negative-cache-backed NO_MATCH verdicts carry the row; harmless — propagation only fires if that row is retracted).
- On entry, reset `self._last_cache_row_id = None`.

**(ii) KBVerifier surfaces the row id in its trace.** `KBVerdict.trace` is a free dict (`kb_verifier.py:80`). After the lookup-subject `select` (`kb_verifier.py:168-170`), capture `resolution_cache_row_id = self._resolver.last_cache_row_id()` and add it into the `trace` dicts built at `kb_verifier.py:281-290` (verified/contradicted path), `246-254` (subsumption fallback), and the NO_MATCH dicts. Key: `"resolution_cache_row_id"`. Guard with `getattr(self._resolver, "last_cache_row_id", None)` for mock resolvers.

**(iii) Walker stamps the edge + aggregator indexes it.** In `_try_external_grounding`, both KB edges (`walker.py:670-680`, `689-697`) gain `metadata["entity_resolution_cache_row_id"] = kb_result.trace.get("resolution_cache_row_id")`, and the `_record_premise` call (§3B Edit 6) passes that as `row_id`.

In `aggregator.py:111-115`, extend the map:
```python
_TRACE_ROW_ID_KEYS = {
    "tier_u_row_id": "tier_u",
    "predicate_translation_row_id": "predicate_translation",
    "subsumption_row_id": "subsumption",
    "entity_resolution_cache_row_id": "entity_resolution_cache",   # WS3 D13
}
```
`_extract_source_rows` (`aggregator.py:118-131`) then automatically pulls KB verdicts' resolution-cache dependency into `source_rows`, which `record_verdict_trace` persists. `entity_resolution_cache` is already a `_RETRACTABLE_TABLES` member (`contradiction_tracer.py:15`) and already has `retracted_at`/`retraction_reason` columns (`database.py:119-120`) plus `EntityResolver.retract_cache_entry` (`resolver.py:292-299`), so the retraction UPDATE in the rewritten tracer works without schema change.

**Optional reinforcement** (recommended, low-risk): have `aggregator._extract_source_rows` prefer `trace.provenance.source_rows()` when the term is populated, falling back to the edge scan for traces built by tests that don't populate provenance. This makes the term the single source of truth while staying back-compatible with `test_retraction_propagator.py`'s hand-built traces (which set only edge metadata).

---

### 3D. NEW `substrate_exceptions` (nogood) table + read/write API; consulted by `verify_transitive_path`

**Role.** A bounded, EAGERLY-cached P2303-style "does NOT hold" cache, keyed per `(predicate-or-relation, property-path, subject-or-subtree)`. Positive bindings are NOT cached as established truth — only as re-verifiable hypotheses (asymmetric trust). The first consumer is the geographic `part_of` transitive ASK (the Marie-Curie P361 leak): once a `(source-subtree, target, part_of-path)` is confirmed NOT to hold (or is operator-marked as a known false-verify), the nogood short-circuits future `verify_transitive_path`/`subsumption` calls without re-hitting SPARQL, and guarantees the leak stays closed even if the property alternation is later widened.

**New table — added to `_SCHEMA_SQL` in `database.py` (after `entity_resolution_cache`, before the indexes at `database.py:124`):**
```sql
CREATE TABLE IF NOT EXISTS substrate_exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exception_kind TEXT NOT NULL,          -- 'subsumption' | 'transitive_path'
    relation_type TEXT NOT NULL,           -- e.g. 'part_of', 'is_a'
    property_path TEXT NOT NULL,           -- canonical "P131|P30|P17" alternation
    source_identifier TEXT NOT NULL,       -- subject / subtree root Q-id
    target_identifier TEXT NOT NULL,       -- object Q-id
    reason TEXT NOT NULL,                  -- 'ask_false' | 'operator_marked' | 'leak_guard'
    created_at TEXT NOT NULL,
    last_consulted_at TEXT,
    used_count INTEGER DEFAULT 0,
    retracted_at TEXT,
    retraction_reason TEXT,
    UNIQUE(exception_kind, relation_type, property_path, source_identifier, target_identifier)
);
CREATE INDEX IF NOT EXISTS idx_substrate_exceptions_lookup
    ON substrate_exceptions(exception_kind, relation_type, source_identifier, target_identifier);
```
Migration is additive/idempotent (`CREATE TABLE IF NOT EXISTS` re-run is a no-op). Add `"substrate_exceptions"` to `TABLE_NAMES` (`database.py:289-297`).

**New module `src/aedos/layer3_substrate/substrate_exceptions.py`:**
```python
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from ..audit.log import log_event

_NOW = lambda: datetime.now(timezone.utc).isoformat()

class SubstrateExceptionCache:
    """Bounded nogood cache (P2303-style). Stores ONLY negative facts:
    `(relation, path, source, target)` confirmed NOT to hold. Positive
    subsumption stays a re-verifiable hypothesis — never cached here.
    Capacity-bounded (LRU by last_consulted_at) so the cache cannot grow
    unbounded across a long session."""

    def __init__(self, db, max_rows: int = 5000) -> None:
        self._db = db
        self._max_rows = max_rows

    def is_nogood(self, kind, relation_type, property_path, source, target) -> bool:
        row = self._db.execute(
            """SELECT id FROM substrate_exceptions
               WHERE exception_kind=? AND relation_type=? AND property_path=?
               AND source_identifier=? AND target_identifier=? AND retracted_at IS NULL""",
            (kind, relation_type, property_path, source, target),
        ).fetchone()
        if row is None:
            return False
        self._db.execute(
            "UPDATE substrate_exceptions SET used_count=used_count+1, last_consulted_at=? WHERE id=?",
            (_NOW(), row["id"]),
        )
        self._db.commit()
        return True

    def record_nogood(self, kind, relation_type, property_path, source, target, reason) -> int:
        now = _NOW()
        self._db.execute(
            """INSERT OR IGNORE INTO substrate_exceptions
               (exception_kind, relation_type, property_path, source_identifier,
                target_identifier, reason, created_at, last_consulted_at, used_count)
               VALUES (?,?,?,?,?,?,?,?,0)""",
            (kind, relation_type, property_path, source, target, reason, now, now),
        )
        self._db.commit()
        row_id = self._db.execute("SELECT last_insert_rowid()").fetchone()[0]
        self._evict_if_over_cap()
        log_event(self._db, event_type="substrate_exception_recorded",
                  event_subject=f"{kind}:{relation_type}:{source}->{target}",
                  event_data={"property_path": property_path, "reason": reason})
        return row_id

    def retract(self, row_id: int, reason: str) -> None:
        self._db.execute(
            "UPDATE substrate_exceptions SET retracted_at=?, retraction_reason=? WHERE id=?",
            (_NOW(), reason, row_id))
        self._db.commit()

    def _evict_if_over_cap(self) -> None:
        count = self._db.execute(
            "SELECT COUNT(*) FROM substrate_exceptions WHERE retracted_at IS NULL").fetchone()[0]
        if count <= self._max_rows:
            return
        # LRU eviction: hard-delete oldest-consulted surplus rows.
        surplus = count - self._max_rows
        self._db.execute(
            """DELETE FROM substrate_exceptions WHERE id IN (
                 SELECT id FROM substrate_exceptions WHERE retracted_at IS NULL
                 ORDER BY COALESCE(last_consulted_at, created_at) ASC LIMIT ?)""",
            (surplus,))
        self._db.commit()
```

**`verify_transitive_path` — generalize the `part_of` ASK into a first-class method on `WikidataAdapter`** (Decision 2 names this; WS3 wires the nogood consult). Add to `WikidataAdapter` (alongside `subsumption`, `kb_wikidata.py:600-605`):
```python
def verify_transitive_path(self, source, target, relation_type, *, exception_cache=None):
    """First-class transitive-path existence check for any relation in
    _SUBSUMPTION_PROPERTIES. Wraps the per-direction ASK
    (_run_subsumption_ask). WS3: when `exception_cache` is wired,
    consult the nogood cache BEFORE the SPARQL ASK; if a matching
    nogood exists, return False without a network call (the leak-guard).
    On a negative ASK result, EAGERLY record the nogood."""
    props = _SUBSUMPTION_PROPERTIES.get(relation_type)
    path = "|".join(props or ())
    if exception_cache is not None and exception_cache.is_nogood(
        "transitive_path", relation_type, path, source, target):
        return False
    held, _r, _e = self._run_subsumption_ask(source, target, relation_type)
    if not held and exception_cache is not None:
        exception_cache.record_nogood(
            "transitive_path", relation_type, path, source, target, reason="ask_false")
    return held
```
And route the directional ASKs in `_live_subsumption` (`kb_wikidata.py:1393-1394`) through the cache: replace the two `_run_subsumption_ask` calls with `verify_transitive_path(..., exception_cache=self._exception_cache)` (store `self._exception_cache` on the adapter, defaulting None; set by `build_pipeline`). This is the consult point that keeps the Marie-Curie P361 leak closed: a one-time operator `record_nogood("transitive_path","part_of","P131|P30|P17","Q270"(Warsaw),"Q183"(Germany),reason="leak_guard")` (or the automatic `ask_false` caching from the trimmed alternation) means widening the alternation later cannot resurrect the false-verify for that subtree.

**`SubsumptionOracle` consult** (`subsumption.py:95-125`) Priority-1 KB branch passes the cache through: the oracle's `__init__` (`subsumption.py:83-93`) gains `exception_cache=None`, stored as `self._exception_cache`, and `build_pipeline` wires it.

---

### 3E. REWRITE `retraction.py` + `contradiction_tracer.py` — provenance-driven lazy staleness, consume `propagate_retraction`'s return, scope to `*_given_assertion`, drop dead eager-cascade

**Current behavior (what gets removed/replaced).**
- `RetractionPropagator.propagate_retraction` (`retraction.py:78-107`) builds `VerdictRetraction` objects and writes `verdict_retracted` audit events but its return value is *consumed by no production caller* — `consistency.py:127` calls it and discards the return; `contradiction_tracer.py:72` extends a list that `trace_contradiction` returns but no production code reads. This is the "dormant eager cascade": retraction marks verdicts retracted but nothing re-derives or surfaces them.
- `ContradictionTracer.trace_contradiction` (`contradiction_tracer.py:43-87`) eagerly issues `retracted_at` UPDATEs on every contributing row and propagates — but it is wired only in tests (`test_end_to_end.py`, `test_retraction_propagator.py`), never in `pipeline.py`/`chat_wrapper.py`.

**Rewrite — `RetractionPropagator`** (`retraction.py`): keep `record_verdict_trace`/`replay`/`_trace_index`/`_verdict_index` unchanged (D6 cross-process replay tests depend on them). Replace `propagate_retraction` with a version that returns a richer result AND marks dependent verdicts STALE (lazy), scoping the stale-marking to `*_given_assertion` verdicts:

```python
@dataclass
class VerdictRetraction:
    claim_id: str
    verdict: str
    retracted_row_id: int
    retracted_table: str
    retracted_at: str
    stale: bool = False          # WS3: marked for lazy re-derivation
    scoped_given_assertion: bool = False  # WS3: True iff verdict was *_given_assertion

class RetractionPropagator:
    def __init__(self, db=None) -> None:
        self._db = db
        self._trace_index = {}
        self._verdict_index = {}
        self._stale: set[str] = set()       # WS3: claim_ids needing re-derivation

    # record_verdict_trace, replay — UNCHANGED

    def propagate_retraction(self, table, row_id) -> list[VerdictRetraction]:
        from .aggregator import is_given_assertion   # lazy: avoid import cycle
        now = datetime.now(timezone.utc).isoformat()
        retracted = []
        for claim_id, rows in self._trace_index.items():
            if (table, row_id) not in rows:
                continue
            verdict = self._verdict_index.get(claim_id, "unknown")
            ga = is_given_assertion(verdict)
            # WS3: lazy staleness is scoped to *_given_assertion verdicts —
            # the assertion-conditional verdicts are the ones a premise
            # correction/retraction can invalidate. Base verdicts grounded
            # in externally-verified sources are not made stale by a Tier U
            # premise retraction (asymmetric trust).
            if ga:
                self._stale.add(claim_id)
            vr = VerdictRetraction(
                claim_id=claim_id, verdict=verdict, retracted_row_id=row_id,
                retracted_table=table, retracted_at=now,
                stale=ga, scoped_given_assertion=ga,
            )
            retracted.append(vr)
            if self._db is not None:
                log_event(self._db, event_type="verdict_retracted",
                          event_subject=claim_id,
                          event_data={"verdict": verdict, "retracted_row_id": row_id,
                                      "retracted_table": table, "stale": ga})
        return retracted

    def is_stale(self, claim_id: str) -> bool:
        """WS3: True iff a dependent premise was retracted/corrected and the
        verdict has not yet been re-derived. The deployment layer checks this
        lazily on next reference and re-runs the walk for stale claims."""
        return claim_id in self._stale

    def clear_stale(self, claim_id: str) -> None:
        """WS3: called after a stale verdict is re-derived."""
        self._stale.discard(claim_id)
```

**Rewrite — `ContradictionTracer`** (`contradiction_tracer.py`): keep the public method but consume `propagate_retraction`'s return (now load-bearing) and emit the STALE marking rather than eagerly re-deriving. The per-row `retracted_at` UPDATE on contributing rows stays (it is the actual retraction the test `test_contradiction_tracer_issues_retracted_at_update` asserts), but the dead `_RETRACTABLE_TABLES` set is reduced to the tables actually reachable from `_trace_index` and the method now returns the propagator's stale-aware results:

```python
class ContradictionTracer:
    """WS3: on an external premise correction/retraction, retract the
    contributing row(s) and mark dependent *_given_assertion verdicts STALE
    for lazy re-derivation. Replaces the dormant eager-cascade tracer."""
    def __init__(self, db=None, retraction_propagator=None) -> None:
        self._db = db
        self._propagator = retraction_propagator or RetractionPropagator(db=db)
        if retraction_propagator is None:
            self._propagator.replay()

    def trace_contradiction(self, contradicted_claim_id, contradicting_premise):
        source_rows = self._propagator._trace_index.get(contradicted_claim_id, [])
        now = _now()
        all_retracted = []
        for table, row_id in source_rows:
            if self._db is not None and table in _RETRACTABLE_TABLES:
                self._db.execute(
                    f"UPDATE {table} SET retracted_at=?, retraction_reason=? WHERE id=?",
                    (now, f"contradiction_trace:{contradicted_claim_id}", row_id))
                self._db.commit()
            all_retracted.extend(self._propagator.propagate_retraction(table, row_id))
            if self._db is not None:
                log_event(self._db, event_type="contradiction_traced",
                          event_subject=contradicted_claim_id,
                          event_data={"contradicting_premise": contradicting_premise,
                                      "retracted_table": table, "retracted_row_id": row_id})
        return all_retracted
```

**Premise-retraction entry point — `tier_u.write` closure.** The "user corrects/retracts a Tier U premise" trigger is `TierU.write` closing a prior row (the `closed_row_ids` loop, `tier_u.py:296-299`) and `TierU.retract` (`tier_u.py:478-491`). Wire a callback so a closure/retraction propagates STALE marking:

- Add `retraction_propagator=None` to `TierU.__init__` (`tier_u.py:59-82`), stored as `self._propagator`.
- In `write`, after the `closed_row_ids` UPDATE loop (`tier_u.py:296-299`), for each `closed_id` call `if self._propagator is not None: self._propagator.propagate_retraction("tier_u", closed_id)`. This marks any `*_given_assertion` verdict that depended on the now-superseded premise STALE.
- In `retract` (`tier_u.py:478-491`), after the UPDATE, the same `propagate_retraction("tier_u", row_id)` call.
- `build_pipeline` passes `retraction_propagator=propagator` to the `TierU(...)` construction (`pipeline.py:184-188`).

**Lazy re-derivation surface — chat_wrapper.** In `ChatWrapper.respond` (`chat_wrapper.py:276-278`), before walking each claim, consult staleness; the walk *is* the re-derivation, so no special path is needed for fresh turns. For verdicts stored in `_verification_store` (`chat_wrapper.py:294`) that become stale between turns, add `get_verification` (`chat_wrapper.py:304-305`) to re-derive lazily: if any `claim_verdict` in the stored `VerificationResult` is `propagator.is_stale(cid)`, re-walk that claim, re-aggregate, `propagator.clear_stale(cid)`, and return the refreshed result. (The propagator is reachable via the aggregator's `_propagator`; thread it onto `ChatWrapper` in `build_pipeline`/the app wiring.)

---

### 3F. `ClaimVerdict.contradicting_value` (Decision 5 dependency surfaced here for observability)

`aggregator.py:80-85` documents `contradicting_value` as deferred; the KB contradicting value is computed (`kb_result.matched_statement.value`) and dropped at `walker.py:698-704`. WS3's observability requirement (per-claim provenance + corrected value inspectable) needs it. Add `contradicting_value: Optional[str] = None` to the frozen `ClaimVerdict` (`aggregator.py:68-89`); the walker carries `kb_result.matched_statement.value` onto the contradicted KB `TraceEdge.metadata` (`walker.py:689-697`, add `"contradicting_value"`), and `aggregator.aggregate` (`aggregator.py:172-177`) reads it from `result.trace` and populates the field. This is plumbing only for WS3 (the structured inspectable surface); `_format_correction` emission is Decision 5's workstream.

---

## (2) DELETIONS (file:line-range, what, why safe)

- **`trace.py:29-37`** — the `chain_includes_assertion: bool` field declaration plus its long Phase-H comment. Safe: replaced by the derived `@property` of the same name; all five writers convert to `_record_premise`, the one reader (`walker.py:191`) and the serializer (`trace.py:59`) read through the property; `test_aggregator.py:331-338` constructs `JustificationTrace(chain_includes_assertion=True)` — see Affected Tests, this constructor call must change to populate `provenance` (will-break → needs-update).
- **`retraction.py:78-107`** — old `propagate_retraction` body. Safe: replaced by the stale-aware version; the only production caller (`consistency.py:127`) discards the return and continues to work; tests in `test_retraction_propagator.py:66-115` assert the returned `VerdictRetraction` shape, which is preserved (new fields are defaulted).
- **`contradiction_tracer.py:9-16`** — `_RETRACTABLE_TABLES` keeps `tier_u`, `predicate_translation`, `subsumption`, `entity_resolution_cache`; **remove `predicate_distribution`** (no walker edge ever stamps a `predicate_distribution` row id — verified: `_TRACE_ROW_ID_KEYS` has no distribution key, and Decision 2 demotes the distribution gate to a ranker). Safe: nothing writes distribution row ids into traces, so the entry was dead.
- **`walker.py:321, 484, 528, 590`** — the four `trace.chain_includes_assertion = True` assignment statements (and the missing-flag Stage-1 path). Safe: replaced by `_record_premise(...)` which sets `assertion=True` where the boolean was set; `includes_assertion()` reproduces the monotonic-OR semantics exactly.

No whole-function deletions in `consistency.py` (the `propagate_retraction` call at `consistency.py:124-127` stays — it now feeds the stale set, which is the intended over-time-soundness behavior).

---

## (3) ADDITIONS (file, block, role)

| File | Block | Role |
|---|---|---|
| `trace.py` | `ProvenanceLiteral`, `ProvenanceTerm` dataclasses; `provenance` field; `chain_includes_assertion` property; `_prov` in `trace_to_json` | The semiring AND/OR provenance term; `chain_includes_assertion` derivation |
| `walker.py` | `_record_premise` helper; 6 call-sites | Walker populates the term as it grounds premises |
| `aggregator.py` | `entity_resolution_cache` key in `_TRACE_ROW_ID_KEYS`; `contradicting_value` field; optional `provenance.source_rows()` preference in `_extract_source_rows` | D13 indexing; observability |
| `resolver.py` | `last_cache_row_id()` + `_last_cache_row_id` state | D13: surface cache row id |
| `kb_verifier.py` | `resolution_cache_row_id` in `KBVerdict.trace` dicts | D13: thread row id to walker |
| `database.py` | `substrate_exceptions` table + index in `_SCHEMA_SQL`; `TABLE_NAMES` append | Nogood cache schema (additive migration) |
| `layer3_substrate/substrate_exceptions.py` (NEW) | `SubstrateExceptionCache` | Bounded eager nogood read/write API |
| `kb_wikidata.py` | `verify_transitive_path` method; `self._exception_cache`; route `_live_subsumption` ASKs through it | First-class transitive verify + nogood consult (P361 leak guard) |
| `subsumption.py` | `exception_cache` ctor param | Pass-through to KB |
| `retraction.py` | `_stale` set; `is_stale`/`clear_stale`; new `VerdictRetraction` fields; rewritten `propagate_retraction` | Lazy staleness, scoped to `*_given_assertion` |
| `contradiction_tracer.py` | rewritten `trace_contradiction` consuming propagator return | Provenance-driven retraction |
| `tier_u.py` | `retraction_propagator` ctor param; `propagate_retraction` calls in `write` closure loop + `retract` | Premise-retraction entry point |
| `pipeline.py` | wire `propagator` into `TierU`; wire `SubstrateExceptionCache` into adapter + subsumption | Assembly |
| `chat_wrapper.py` | stale-check in `get_verification` lazy re-derivation; thread propagator | Lazy re-derive surface |

---

## (4) CALL-SITES / CONSUMERS (grep-verified)

**`chain_includes_assertion`** (all become reads through the derived property except the 5 writers, which are converted):
- `walker.py:191` (read) — unchanged.
- `walker.py:321, 484, 528, 590` (writes) — converted to `_record_premise`.
- `trace.py:37` (decl), `trace.py:59` (serialize) — decl removed, serializer reads property.
- `test_aggregator.py:331-338` — needs-update (construct via provenance).
- `test_walker_cluster_2.py:133,140,169,176,206,237,269,320,354,394,495` — all are `assert result.trace.chain_includes_assertion is {True,False}` — pass unchanged through the derived property (the walk populates the term, the property derives the boolean). No edit needed IF `_record_premise` sets `assertion` correctly at every site (Edits 1-6).

**`propagate_retraction`**:
- `consistency.py:127` (production) — return still discarded; now feeds stale set. Works unchanged.
- `contradiction_tracer.py:72` — rewritten to consume return.
- `tier_u.py` write/retract — NEW callers (§3E).
- Tests `test_retraction_propagator.py:69,75,85,91,98,107,114,182,188,238`, `test_end_to_end.py:244,281,346` — all assert on returned `VerdictRetraction` list; preserved shape (new defaulted fields). No break.

**`record_verdict_trace`**: `aggregator.py:207` (production, unchanged); tests `test_retraction_propagator.py:74,84,90,96,97,106,113,125,138,139`, `test_end_to_end.py:243,250` — unchanged signature.

**`ContradictionTracer` / `trace_contradiction`**: `test_end_to_end.py:100,251,355`, `test_retraction_propagator.py:122-142` — rewritten body preserves the public method signature and return type; `test_contradiction_tracer_issues_retracted_at_update` (`test_end_to_end.py:349-359`) still passes (UPDATE retained). No production caller exists yet — `pipeline.py` does not construct a tracer; the deployment wiring in §3E is additive.

**`_TRACE_ROW_ID_KEYS` / `_extract_source_rows`**: `aggregator.py:111,118-131`, `207`. Consumers: the D6 replay tests and `test_end_to_end.py:336-338`. Adding the `entity_resolution_cache` key is additive.

**`EntityResolver.resolve` / `.select`**: `kb_verifier.py:168-170, 209-211`; `walker.py:795-797`. Return type unchanged; `last_cache_row_id()` is a NEW accessor, no existing caller affected.

**`WikidataAdapter.subsumption` / `_run_subsumption_ask`**: `kb_verifier.py:441,451,452,461,462,485`, `subsumption.py:105`, `walker.py` (D5 neighbor path), `kb_wikidata.py:1393-1394`. `verify_transitive_path` is NEW; routing `_live_subsumption` through it preserves the `(bool, retries, error)` semantics the verdict logic needs (the method returns just the bool; keep `_run_subsumption_ask` for the retry/error accounting in `_live_subsumption`, OR have `verify_transitive_path` return the tuple — recommended: `verify_transitive_path` returns bool, `_live_subsumption` keeps its two `_run_subsumption_ask` calls but consults/records the cache around them to avoid touching the audit/retry accounting).

**`TierU.__init__`**: `pipeline.py:184`, `tier_u.py` construction in tests (`test_end_to_end.py`, `test_walker_*`, `test_routing_to_tier_u.py`, `test_d47_pipeline_integration.py`). New kwarg defaults None → all existing constructions unaffected.

---

## (5) AFFECTED TESTS

| Test | Classification | Note |
|---|---|---|
| `test_aggregator.py:331-338` (`trace_to_json` round-trips `chain_includes_assertion`) | **needs-update** | Replace `JustificationTrace(chain_includes_assertion=True)` with a trace whose `provenance` has an assertion literal; assert the serialized derived boolean is still `True` and `provenance` is present. |
| `test_walker_cluster_2.py` (11 `chain_includes_assertion` asserts) | **needs-update (likely pass-through)** | These walk real claims and assert the derived boolean. Should pass unchanged once Edits 1-6 set `assertion` correctly. Re-run to confirm; any drift is a sign a `_record_premise` `assertion=` flag is wrong. |
| `test_retraction_propagator.py` (all) | **needs-update (additive)** | `VerdictRetraction` gains defaulted fields — existing asserts pass. Add new tests for `is_stale`/`clear_stale` and the `*_given_assertion` scoping. |
| `test_end_to_end.py:325-368` (`TestRetractionWiring`) | **needs-update** | `trace_contradiction` rewrite preserves behavior; add an assertion that a `*_given_assertion` verdict is marked stale and a base verdict is not. |
| `test_database.py` | **new-needed** | Add `test_substrate_exceptions_exists` + column-name check, mirroring `test_entity_resolution_cache_exists` (`test_database.py:59-60,179`). |
| `test_entity_resolver.py:74-177` | **needs-update (additive)** | Add a test that `last_cache_row_id()` returns the touched row id on hit/insert; existing cache tests unaffected. |
| NEW `test_substrate_exceptions.py` | **new-needed** | `is_nogood`/`record_nogood`/`retract`/eviction; idempotent `INSERT OR IGNORE`. |
| NEW `test_provenance.py` | **new-needed** | `ProvenanceTerm.includes_assertion()`, `.source_rows()`, `.literals()`; derivation of `chain_includes_assertion`. |
| NEW transitive-path / leak-guard test | **new-needed** | `verify_transitive_path` consults the nogood cache and short-circuits SPARQL; Marie-Curie `(Warsaw, Germany, part_of)` nogood stays closed after a hypothetical alternation widening. |
| `test_d47_pipeline_integration.py`, `test_routing_to_tier_u.py`, `test_walker_*` (TierU constructions) | **needs-update (none expected)** | New `retraction_propagator` kwarg defaults None. |

No tests are classified will-break-with-no-fix.

---

## (6) ORDERING / DEPENDENCIES

1. **`database.py`** — add `substrate_exceptions` table + `TABLE_NAMES` (independent; unblocks the cache module and its tests).
2. **`trace.py`** — `ProvenanceLiteral`/`ProvenanceTerm`/`provenance` field/derived property/serializer (foundation for the walker and aggregator).
3. **`resolver.py` + `kb_verifier.py`** — D13 cache-row-id plumbing (must precede the walker KB-edge stamping so `kb_result.trace["resolution_cache_row_id"]` exists).
4. **`walker.py`** — `_record_premise` + 6 edits + KB-edge `entity_resolution_cache_row_id` + `contradicting_value` metadata (depends on 2 and 3).
5. **`aggregator.py`** — `_TRACE_ROW_ID_KEYS` key, `contradicting_value` field, optional `provenance.source_rows()` preference (depends on 2, 4).
6. **`substrate_exceptions.py`** (new) — cache API (depends on 1).
7. **`kb_wikidata.py` + `subsumption.py`** — `verify_transitive_path` + cache consult + ctor params (depends on 6).
8. **`retraction.py`** — rewritten `propagate_retraction` + stale set (depends on `is_given_assertion` in `aggregator.py`, already present at `aggregator.py:63`).
9. **`contradiction_tracer.py`** — rewritten tracer (depends on 8).
10. **`tier_u.py`** — propagator ctor + write/retract propagation (depends on 8).
11. **`pipeline.py`** — wire propagator into TierU, cache into adapter + subsumption (depends on 6, 7, 10).
12. **`chat_wrapper.py`** — lazy re-derivation surface (depends on 8, 11).
13. **Tests** — update + new (after each owning module).

The system stays functional after each step: steps 1-3 are additive; step 4's `_record_premise` reproduces the old boolean before step 8 even exists.

---

## (7) RISKS / SOUNDNESS

- **Soundness invariant (never false-verify).** The nogood cache stores ONLY negatives and positive bindings remain re-verifiable hypotheses — asymmetric trust. A stale nogood can only cause a *false abstain* (we decline a path that might now hold), never a false-verify. The leak-guard for Marie-Curie P361 is therefore strictly on the safe side. The eviction-by-LRU could evict a leak-guard nogood; mitigate by reserving `reason='leak_guard'`/`reason='operator_marked'` rows from eviction (add `AND reason NOT IN ('leak_guard','operator_marked')` to the `_evict_if_over_cap` DELETE). **Flag for operator: confirm leak-guard rows are eviction-exempt.**
- **Lazy staleness scoping.** Scoping STALE to `*_given_assertion` (Decision 3) means retracting a Tier U premise does NOT invalidate a base `verified`/`contradicted` verdict that happened to also touch that row. This is correct under asymmetric trust (base verdicts are externally grounded) — but the D13 KB-edge plumbing now also records `entity_resolution_cache` rows on *base* KB verdicts. A retraction of a resolver cache row that fed a base verdict will NOT mark it stale under the `is_given_assertion` scope. **Decision point: should resolver-cache retraction also stale base KB verdicts?** The architecture text says premise-retraction is "the `*_given_assertion` ones." I have scoped strictly to that; the `entity_resolution_cache` dependency is still *recorded* (so `propagate_retraction` returns the `VerdictRetraction` for audit), just not marked stale. **Flag for operator confirmation.**
- **Property vs field for `chain_includes_assertion`.** Making it a read-only property is the disciplined choice but is the highest-churn risk: any test or future code that does `trace.chain_includes_assertion = X` will raise `AttributeError`. Grep shows only the 5 walker sites (converted) and the `test_aggregator.py` constructor (updated). A frozen-field shim alternative exists if churn proves wider, but the property keeps the term as single source of truth.
- **Resolver `last_cache_row_id` is request-scoped mutable state.** The walker must read it *immediately* after `select()` in `kb_verifier.verify` (it is, at `kb_verifier.py:168-170`), before any other `resolve()` (the value-entity `resolve` at `kb_verifier.py:209-211` overwrites it). Spec stamps the *lookup-subject* cache id — the entity the statement is keyed on, the load-bearing dependency. Capture it into a local before the value `resolve`. **Confirm: lookup-subject cache row is the right retraction dependency** (it is — a wrong subject resolution is what a correction would retract).
- **No DB-destructive migration.** All schema changes are `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` / list append — idempotent and additive, matching the existing ALTER-guard pattern (`database.py:157-214`).
- **Import cycle.** `retraction.py` importing `is_given_assertion` from `aggregator.py` while `aggregator.py` imports `JustificationTrace` from `trace.py` — `retraction` does NOT import `aggregator` at module load today; the lazy in-function import (shown in the rewrite) keeps the graph acyclic, mirroring `walker._apply_assertion_designation`'s existing lazy import (`walker.py:193`).


##########################################################################################
