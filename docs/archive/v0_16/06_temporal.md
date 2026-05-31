# Aedos v0.16 ? Change Specification: Workstream 6 ? Temporal T1: Granular Start/End Claims

*Implementation-ready spec. Conforms to the interface contract in `00_overview_contract_ordering.md`, which is AUTHORITATIVE on all cross-workstream interfaces. File:line references were verified by the spec-mapping pass against the v0.16 base (identical to `main` at branch creation).*

---

## DETAILED CHANGE SPEC
# Workstream 6 — Temporal T1: Implementation-Ready Change Spec

Verified against actual current code. All file:line citations confirmed by full reads of
`temporal.py`, `extractor.py`, `kb_wikidata.py`, `kb_verifier.py`, `walker.py`, `tier_u.py`,
`triage.py`, `predicate_translation.py`, `kb_protocol.py`, `aggregator.py`, and `seeds/predicate_translation.json`.

## 0. Current state (ground truth)

- **Start/end dates today land in SCOPE fields, not the object slot.** `extractor.py` Rules 12/13
  (employment) put the year in `valid_from`/`valid_until` (lines 206-237); Rule 8 (war ended) →
  `valid_until` (147-150); Rule 14 (state change) → `valid_from`/`valid_until` (239-258); Rule 7
  (founded/born year-scope) → `valid_from` (131-145). `_build_claim` (extractor.py:600-660) calls
  `extract_temporal_scope(...)` and stores `scope.valid_from/valid_until/valid_during_ref` on the
  `Claim` dataclass (fields at extractor.py:482-484).
- **The date-in-OBJECT precedent already exists** (Rule 23, extractor.py:425-443): born_on→P569,
  died_on→P570, founded_in_year→P571, dissolved_in_year→P576, published_in_year/released_in_year→P577,
  occurred_in_year→P585. Seed rows for born_on/died_on/founded_in_year/published_in_year/released_in_year
  exist (`seeds/predicate_translation.json` lines 110-132, 386-397, 458-502); `object_type: "date"`,
  `slot_to_qualifier: {"subject":"statement_subject","object":"statement_value"}`. `dissolved_in_year`
  and `occurred_in_year` are NOT seeded (runtime-oracle generated).
- **P580/P582/P642 qualifiers are ALREADY surfaced** by the adapter. `_DEFAULT_QUALIFIER_PROPS =
  ("P580","P582","P642")` (kb_wikidata.py:78); `_build_lookup_query` projects them via OPTIONAL
  blocks (kb_wikidata.py:401-405); `_parse_statement_bindings` parses them into `stmt.qualifiers`,
  converting time values to `YYYY-MM-DD` via `_parse_time_value` (kb_wikidata.py:462-471, 542-545).
- **`_scope_compatible`** (kb_verifier.py:646-669) reads `stmt.qualifiers.get("P580")`/`("P582")`
  and compares string-lexically against `claim.valid_from`/`claim.valid_until`, special-casing
  `BEFORE_PRESENT`. It returns "compatible" (always-valid) when the statement has no P580/P582. There
  is NO interval-from-events / "holds at T" resolver anywhere.
- **PRE-EXISTING INCONSISTENCY (must flag, scope-relevant):** the date predicates are seeded with
  `object_type: "date"` (10 occurrences), but the oracle prompt enum
  (predicate_translation.py:26) allows only `["entity","quantity","time","proposition","entity_list"]`
  and `_OBJECT_TYPE_COMPATIBLE_VALUE_TYPES` (kb_verifier.py:575-580) keys on `"time"` (not `"date"`).
  So a `"date"` object_type returns `None` from `_OBJECT_TYPE_COMPATIBLE_VALUE_TYPES.get(...)` and
  `_contradiction_value_type_ok` returns True (does-not-block) for ALL value_types. The contract
  (kb_verifier §) requires "CONTRADICTED only from a single_valued binding whose value-type
  constraint the resolved object satisfies." For date predicates this gate is currently a no-op.
  This workstream MUST reconcile date↔time so the new start/end date claims get a real value-type gate.

## A. Extractor change — emit start/end as SEPARATE date-in-object claims

Goal: reuse the born_on (Rule 23) date-in-object precedent. When the source text gives a YEAR/DATE
for the START or END of an interval-bearing relation (employment, membership, role-holding, state),
emit, IN ADDITION to the base relation claim, a separate date-in-object claim using a new
`*_started` / `*_ended` predicate whose object is the bare year/date.

### A.1 New date-sense predicates (seed additions, conform to PredicateBinding model)

Add to `seeds/predicate_translation.json` (NEW rows; additive). Each is `object_type` reconciled to
`"time"` (see §A.4) so the value-type gate is live, `routing_hint: "kb_resolvable"`. The KB property
for the *_started/_ended pair is the SAME base property (P108 employer, P39 position, P463 member of)
but the claim grounds against the statement's P580/P582 QUALIFIER, not the statement value — so the
binding needs a `slot_to_qualifier` that maps `object` → the qualifier. Proposed shapes:

```jsonc
// employment start/end — ground against P108 statement's P580/P582 qualifier
{ "aedos_predicate": "employment_started", "object_type": "time", "user_subject_required": 0,
  "distinct_slots": null, "routing_hint": "kb_resolvable", "kb_namespace": "wikidata",
  "kb_property": "P108",
  "slot_to_qualifier": {"subject":"statement_subject","org":"statement_value","object":"qualifier:P580"},
  "single_valued": 0,
  "reason": "v0.16 T1. Employment start date = P580 (start time) qualifier on the P108 (employer) statement. Object is the date; grounds against the qualifier, not the statement value. Multi-valued: several employments." }
{ "aedos_predicate": "employment_ended", "object_type": "time", ... "kb_property": "P108",
  "slot_to_qualifier": {"subject":"statement_subject","org":"statement_value","object":"qualifier:P582"},
  "single_valued": 0, "reason": "v0.16 T1. Employment end date = P582 (end time) qualifier on P108." }
// membership start/end on P463; role start/end on P39 (parallel shape)
{ "aedos_predicate": "membership_started", ... "kb_property": "P463", "object":"qualifier:P580" ... }
{ "aedos_predicate": "membership_ended",   ... "kb_property": "P463", "object":"qualifier:P582" ... }
{ "aedos_predicate": "role_started",  ... "kb_property": "P39",  "object":"qualifier:P580" ... }
{ "aedos_predicate": "role_ended",    ... "kb_property": "P39",  "object":"qualifier:P582" ... }
// generic state-bearing-subject interval (Rule 14): inception/dissolution already covered by
// founded_in_year (P571) / dissolved_in_year (P576). Map status_started->P571, status_ended->P576.
```

NOTE on the `slot_to_qualifier` direction: the current `_lookup_targets` (kb_verifier.py:524-533)
only understands `subject→statement_subject/object→statement_value` (standard) and
`subject→statement_value/object→statement_subject` (inverse). It returns `None` (→ NO_KB_PATH) for
ANY map that puts the object on a `qualifier:Pxxx`. **A qualifier-keyed object slot is therefore
NOT directly supported by the v0.15 lookup path.** Two options, in ascending soundness/effort:

- **(Bounded, RECOMMENDED for v0.16 T1):** the *_started/_ended claim grounds via the interval
  resolver (§B), NOT via the generic `kb_verifier.verify` value-compare path. The resolver gathers
  P580/P582 from the base-relation statement's qualifiers directly. So these predicates do NOT need
  `_lookup_targets` to interpret the qualifier map — the walker routes them to the resolver. The seed
  `slot_to_qualifier` documents intent and feeds future binding-discovery, but the live grounding is
  the resolver. This keeps the change bounded and reuses the already-surfaced qualifiers.
- (Deferred) Extend `_lookup_targets` + `_compare_positive` to compare a claim object against a
  named qualifier value. Larger blast radius; defer.

### A.2 Prompt rule changes (the load-bearing extractor change)

Rules that currently route start/end into scope fields must ALSO emit a separate date-in-object
claim. Minimal-churn approach: ADD a new Rule 25 (date-in-object for interval endpoints) and amend
Rules 12/13/14 to cross-reference it. Concretely:

- **Rule 12 (employment start, extractor.py:206-222):** keep `employed_by(...)` with
  `valid_from=YEAR` (legacy scope; see §D), AND add: "ALSO emit a separate claim
  `employment_started(subject, YEAR)` with the year in the OBJECT slot (reuse the born_on date-in-object
  pattern, Rule 23)." Example: 'Asa joined Google in 2020' → TWO claims:
  `employed_by(Asa, Google, valid_from=2020)` + `employment_started(Asa, '2020')`.
- **Rule 13 (employment termination, extractor.py:223-237):** keep `employed_by(...)` with
  `valid_until=YEAR`, AND add `employment_ended(subject, YEAR)`.
- **Rule 14 (state change, extractor.py:239-258):** for "began/started" emit `status(...,'ongoing')`
  + `founded_in_year`/`status_started` date-in-object; for "ended/concluded" emit `status(...,'ended')`
  + `dissolved_in_year`/`status_ended`. (founded_in_year/dissolved_in_year already exist as Rule 23
  date predicates — prefer reusing them over new status_* predicates where the subject is an org.)
- **New Rule 25 (canonical statement):** "INTERVAL ENDPOINTS — when a relation's START or END is
  given as a date/year, emit the relation claim (as Rules 12/13/14) AND a separate date-in-object
  claim naming the endpoint: `<relation>_started` / `<relation>_ended` with the bare year/date in the
  object slot. This mirrors Rule 23's date-in-object treatment. The endpoint claim is independently
  verifiable against the P580/P582 qualifier the KB records." Include the DO-NOT list: do not emit an
  endpoint claim when no date is present (a bare "Asa joined Google" → only `employed_by`, no
  `employment_started`); do not duplicate the date into both the object slot AND valid_from on the
  ENDPOINT claim (the endpoint claim's date is the object, like Rule 23's note at extractor.py:441-443).

Process note: per the D45 pattern already used throughout the prompt (extractor.py:104-112), each new
rule MUST carry both positive triggers AND explicit non-triggering conditions.

### A.3 `_build_claim` interaction (extractor.py:519-661)

The endpoint claims are emitted by the LLM as ordinary additional claims in the tool output, so they
flow through the existing `for raw in flat:` loop (extractor.py:508-511). No new Python decomposition
needed. BUT three existing pre-lookup drops in `_build_claim` interact:
- subject==object drop (extractor.py:579-583): an endpoint claim is `(Asa, employment_started, '2020')`
  — subject != object, safe.
- predicate==object drop (extractor.py:595-598): '2020' != 'employment_started', safe.
- The content-less occurred/happened drop (extractor.py:542-548): per the v0.16 contract item 4 this
  filter is ALREADY OBSOLETE and slated for removal in Workstream 4; it does not touch endpoint claims.

Under the v0.16 "verify every claim / never return None" contract (item 4, Workstream 4), these
become `abstention_reason` designations rather than drops. This workstream does NOT own that change
but its endpoint claims must remain shaped (subject/predicate/object all non-empty) so they survive
regardless of which Workstream-4 state lands first.

### A.4 Reconcile object_type "date" → "time"

The seed uses `"date"` (10 rows) but the enum/value-type gate use `"time"`. Pick ONE. The contract's
kb_verifier value-type gate and the existing `_OBJECT_TYPE_COMPATIBLE_VALUE_TYPES` key
(kb_verifier.py:579: `"time": {"date","literal"}`) already use `"time"`, and the Statement.value_type
vocabulary is `entity|literal|date|quantity` (kb_protocol.py:42). Recommendation: change the SEED rows
from `"date"` → `"time"` (matches the oracle prompt enum AND makes `_contradiction_value_type_ok`
live: a time predicate may only contradict on a `date` or `literal` statement value_type). This is a
seed-only edit; the new *_started/_ended seeds use `"time"` from the start. Add a one-line migration
note in the seed `reason`. Verify the seed-loader does not enforce the enum at load (it doesn't —
`_build_claim`/loader read object_type as a free string per predicate_translation.py:458).

## B. Interval-from-events resolver in the walker (holds-at-T)

NEW resolver method on the walker (or a small helper module imported by the walker), invoked when a
*_started/_ended predicate is encountered, OR when a base relation claim carries a temporal scope and
the caller asks "holds at T". Keep it BOUNDED — endpoint arithmetic with three-valued logic; NO Allen
algebra.

### B.1 Endpoint gathering

`def _gather_interval(self, subject, base_relation_claim, context) -> Interval`:
- Resolve subject to a KB Q-id via `self._kb_verifier._resolver` (reuse the exact pattern at
  walker.py:782-797 in `_verify_kb_quantitative`).
- Look up the BASE relation's KB property statements (`self._kb.lookup_statements(qid, base_prop)`),
  same call as walker.py:803. For each statement matching the claim's object entity (the org/group),
  read `stmt.qualifiers.get("P580")` (start) and `stmt.qualifiers.get("P582")` (end) — already parsed
  to `YYYY-MM-DD` by the adapter.
- ALSO gather Tier U endpoint facts: `employment_started`/`employment_ended` Tier U rows for the same
  (asserting_party, subject, base-object) via `self._tier_u.lookup(...)` on the endpoint claim. Tier U
  start/end claims thus participate alongside KB qualifiers.
- Return `Interval(start: Optional[str], end: Optional[str], start_known: bool, end_known: bool)`.

### B.2 `holds_at(T)` with three-valued logic

`def _interval_holds_at(interval, T) -> str  # "true" | "false" | "unknown"`:
- Compare ISO-date strings lexically (consistent with `_scope_compatible`'s existing string compares,
  kb_verifier.py:661/666; valid for `YYYY-MM-DD`).
- start_known and start > T → false (relation hadn't begun).
- end_known and end < T → false (relation already ended).
- start_known and end_known and start <= T <= end → true.
- start_known, end UNKNOWN (open interval = "ongoing" / subsumes BEFORE_PRESENT-as-end), start <= T → true.
- start UNKNOWN, end_known, T <= end → unknown (could have started before or after T — three-valued).
- both unknown → unknown.

This makes BEFORE_PRESENT a special case of `end = open`: a claim whose `valid_until == BEFORE_PRESENT`
(extractor.py:46, the implicit-past default) maps to "ended at unspecified past time" = `end_known=False`
but a soft signal the relation is not current; preserve current `_scope_compatible` semantics by
treating BEFORE_PRESENT as `end_known=False` in the resolver (so it never forces a false). The default
T is `context.current_time` (walker.py VerificationContext.current_time).

### B.3 Verdict for a *_started/_ended endpoint claim

`def _verify_interval_endpoint(self, claim, context, trace) -> Optional[str]`:
- Determine endpoint kind from predicate suffix (`_started`→start/P580, `_ended`→end/P582).
- Gather the matching qualifier value from the base-relation statement (resolve the claim's
  org/object from `slot_to_qualifier` `statement_value` slot).
- Compare the claim's object (the asserted year) against the KB qualifier year using the existing
  `_value_matches` year-aware compare (kb_verifier.py:622-643) / `_normalize_date_value`. Match →
  `verified`; functional/single_valued mismatch with a resolved date value_type that satisfies the
  value-type gate → `contradicted`; else `no_match`/`None` (fall through, abstain).
- Return None (no terminal verdict) on any resolution/KB failure (§3.2 soundness — never fabricate;
  mirrors `_verify_kb_quantitative` walker.py:739-841 fail-closed discipline).
- Wire into `_try_external_grounding` (walker.py:646-660) as a new branch BEFORE the generic
  kb_verifier branch, gated on `self._predicate_routing(node.predicate)` being kb_resolvable AND the
  predicate ending in `_started`/`_ended` (or a new `routing_hint: "kb_interval"` — preferred, parallels
  the existing `kb_quantitative` hint at walker.py:652). Emit a `premise_lookup`/`kb_statement` trace
  edge with `metadata={"source":"kb_interval","qualifier":"P580|P582","endpoint_value":<kb date>}`.
  Carry the contradicting value onto the edge metadata so Workstream 5's `contradicting_value` plumbing
  surfaces it (the contract names this; mirror walker.py:698-704's dropped value).

### B.4 Soundness gates

- Only positive verdicts on UNIQUE/matching qualifier; abstain on ambiguity (multiple statements with
  different P580 for the same org → abstain unless one is `preferred`-ranked, reuse the
  preferred-then-max pattern from walker.py:821-834 adapted for dates: prefer `preferred` rank, else
  abstain on conflicting starts — do NOT max dates).
- Wikidata data-model limits (document in code comments): P580/P582 are QUALIFIERS on a statement, not
  statements themselves — they cannot be looked up directly, only read off the base statement (this is
  why §A.1 RECOMMENDED routing through the resolver, not the generic value-compare path). No
  event-relative bounds in the data model (only absolute dates). Day precision is the finest the
  parser keeps (`_parse_time_value` truncates to `YYYY-MM-DD`); many P580/P582 are year-precision only
  — the year-aware `_value_matches` already handles that.

## C. Surfacing P580/P582 from the adapter

Already done — `_DEFAULT_QUALIFIER_PROPS` (kb_wikidata.py:78) includes P580/P582/P642; `_build_lookup_query`
projects them; `_parse_statement_bindings` parses them. NO adapter change required for the core T1
path. OPTIONAL hardening (low priority, only if a corpus case needs a qualifier outside the default
set): the contract's D32 dynamic-qualifier-discovery follow-up is explicitly out of scope here.
Confirm the fixture path also carries qualifiers — `_fixture_lookup` (kb_wikidata.py:770-784) uses the
same `_parse_statement_bindings`, so fixtures with P580/P582 bindings round-trip identically. Add a
fixture with P580/P582 qualifiers for the new resolver tests (e.g. `sparql_P108_<Qid>.json`).

## D. Relationship to valid_from / valid_until / valid_during_ref

- **KEEP all three legacy fields** on `Claim`, `TemporalScope`, and the `tier_u` columns. They are
  load-bearing: `_scope_compatible` (kb_verifier.py:660-667), Tier U idempotency/scope-conflict keying
  (tier_u.py:206-225, 283-287), `_query_current` BEFORE_PRESENT filter (tier_u.py:686-694),
  `lookup_object_conflict` (tier_u.py:466-474), triage (triage.py:63), and `_claim_from_parts`
  (walker.py:176-178) all read them. Removing them would break Tier U temporal semantics. The DB
  migration constraint (additive/non-destructive) forbids dropping columns anyway.
- **Do NOT add valid_from_ref / valid_until_ref in this workstream.** Rule 16 (extractor.py:283-313)
  documents that v0.15 expresses event-relative bounds via `valid_during_ref` precisely because
  valid_from_ref/valid_until_ref don't exist, and the v0.16 plan defers them. The contract scopes T1
  to "triples only" and "NO event-relative bounds" (Wikidata data-model limit). The new *_started/_ended
  date-in-object claims are the v0.16 mechanism for endpoints; event-relative bounds stay on
  `valid_during_ref`. **Recommendation: defer valid_from_ref/valid_until_ref.** If a corpus case forces
  it, add them as additive Optional fields mirroring valid_during_ref, but flag as out-of-bounded-scope.
- The endpoint claims COEXIST with the scope fields: `employed_by(...,valid_from=2020)` keeps its scope
  (drives `_scope_compatible` and Tier U scope-conflict), while `employment_started(...,'2020')` is the
  independently-groundable date-in-object claim. They are redundant by design (the contract's
  born_on/died_on precedent does the same: a place-of-birth claim and a date-of-birth claim coexist).

## E. Ordering within this workstream

1. Reconcile object_type `"date"`→`"time"` in seeds (§A.4) — prerequisite for the value-type gate.
2. Add new *_started/_ended seed rows (§A.1) + add fixture(s) with P580/P582.
3. Add interval resolver + `holds_at` + endpoint verifier to walker (§B), wired into
   `_try_external_grounding`; add `kb_interval` routing_hint handling.
4. Amend extractor prompt Rules 12/13/14 + add Rule 25 (§A.2). (Prompt change last so resolver/seed
   exist before the extractor starts emitting the new claims — system stays functional throughout.)
5. Tests: resolver unit tests, extractor prompt corpus cases, kb_verifier value-type-gate test for time.

## F. Wikidata data-model limits (must be documented in code comments)
- Qualifiers (P580/P582), not statements: read off the base statement; cannot be queried directly.
- No event-relative bounds in the data model — only absolute dates; "before/after <event>" stays on
  valid_during_ref (Rule 16).
- Day precision (parser truncates to YYYY-MM-DD); many endpoints are year-precision — year-aware
  `_value_matches` already accommodates.


## DELETIONS
- No code deletions required for the bounded T1 path. (LOC net change is roughly neutral-to-slightly-positive in code; the contract's overall net-decrease comes from other workstreams' deletions, not this one.)
- extractor.py:542-548 content-less occurred/happened drop — NOT deleted by this workstream (owned by Workstream 4), but noted as obsolete and non-interfering with endpoint claims.
- OPTIONAL (only if reconciliation chosen as rename): the 10 `"object_type": "date"` seed string values are replaced in-place with `"time"` (seeds/predicate_translation.json lines 112,124,388,460,496 and the 5 alias rows ~743-950) — a value edit, not a structural deletion; safe because the only readers (predicate_translation loader, kb_verifier value-type gate) treat object_type as a free string and the `"time"` key already exists in _OBJECT_TYPE_COMPATIBLE_VALUE_TYPES.

## ADDITIONS
- seeds/predicate_translation.json — NEW rows: employment_started/employment_ended (P108 + P580/P582), membership_started/membership_ended (P463), role_started/role_ended (P39); object_type='time', routing_hint='kb_resolvable' (or 'kb_interval'), single_valued=0. Role: give the *_started/_ended date-in-object predicates a binding.
- src/aedos/layer1_extraction/extractor.py:_SYSTEM_PROMPT — NEW Rule 25 (interval endpoints emit a separate date-in-object claim) + amendments to Rules 12/13/14 to also emit the endpoint claim. Role: extractor produces the granular start/end claims.
- src/aedos/layer4_sources/walker.py — NEW methods _gather_interval(subject, base_claim, context)->Interval, _interval_holds_at(interval, T)->{true|false|unknown}, _verify_interval_endpoint(claim, context, trace)->Optional[str]; NEW small Interval dataclass (start,end,start_known,end_known). Role: interval-from-events resolver and endpoint grounding.
- src/aedos/layer4_sources/walker.py:_try_external_grounding (~line 646) — NEW branch routing *_started/_ended (or routing_hint=='kb_interval') predicates to _verify_interval_endpoint BEFORE the generic kb_verifier branch; emits a kb_interval trace edge carrying the KB endpoint value + contradicting_value.
- tests/fixtures/wikidata/sparql_P108_<Qid>.json (and P463/P39 analogs) — NEW fixtures carrying P580/P582 qualifier bindings so the resolver has fixture-mode coverage.
- src/aedos/layer3_substrate/predicate_translation.py:_GENERATION_SYSTEM_PROMPT — OPTIONAL one-line note documenting *_started/_ended → base property + P580/P582 qualifier so runtime oracle generation matches the seed shape (keeps knowledge in prompt/KB, not a hardcoded Python table).

## CALL SITES / CONSUMERS
- src/aedos/layer1_extraction/temporal.py:6-49 — extract_temporal_scope / TemporalScope / BEFORE_PRESENT definition; UNCHANGED (legacy scope fields kept).
- src/aedos/layer1_extraction/extractor.py:11 imports BEFORE_PRESENT/TemporalScope/extract_temporal_scope; :600-606 calls extract_temporal_scope; :482-484 Claim scope fields — all KEPT; prompt amended only.
- src/aedos/layer4_sources/kb_verifier.py:10 imports BEFORE_PRESENT; :646-669 _scope_compatible reads stmt.qualifiers P580/P582 vs claim.valid_from/valid_until — KEPT; resolver reuses the same qualifier keys.
- src/aedos/layer4_sources/kb_verifier.py:575-593 _OBJECT_TYPE_COMPATIBLE_VALUE_TYPES / _contradiction_value_type_ok — CONSUMER of object_type; reconciling 'date'->'time' makes the 'time':{date,literal} gate live for endpoint predicates.
- src/aedos/layer4_sources/kb_verifier.py:524-533 _lookup_targets — only supports standard/inverse maps, returns None for qualifier-keyed object slots; this is WHY endpoint claims route through the new resolver, not the generic value-compare path.
- src/aedos/layer4_sources/tier_u.py:12 imports BEFORE_PRESENT; :206-225 idempotency+scope-conflict on valid_from/valid_until; :283-287 scope_conflict closure; :466-474 lookup_object_conflict BEFORE_PRESENT filter; :686-694 _query_current BEFORE_PRESENT filter — all KEPT; endpoint claims add NEW Tier U rows under the *_started/_ended predicate (participate in _gather_interval).
- src/aedos/layer4_sources/walker.py:176-178 _claim_from_parts copies valid_from/valid_until/valid_during_ref — KEPT; endpoint claims carry their own (empty) scope.
- src/aedos/layer4_sources/walker.py:646-660 _try_external_grounding kb_quantitative branch — PATTERN to mirror for the new kb_interval branch; :782-797 resolver-use pattern reused by _gather_interval; :821-834 preferred-then-value selection pattern reused (adapted for dates).
- src/aedos/layer1_extraction/triage.py:30-34 _TEMPORAL set + :57-58 routes _TEMPORAL to VERIFY — ADD employment_started/employment_ended/etc. or rely on _has_named_entity/_NUMERIC fallback; verify endpoint claims (numeric-year object) already hit the _NUMERIC.search VERIFY branch at triage.py:59 (they do: '2020' matches \d+).
- src/aedos/layer5_result/aggregator.py:80-89 ClaimVerdict.contradicting_value (deferred) + :109-131 _TRACE_ROW_ID_KEYS/_extract_source_rows — Workstream 5 consumer; the kb_interval trace edge must carry contradicting_value + a retractable id for that workstream.
- src/aedos/database.py:20-22 tier_u valid_from/valid_until/valid_during_ref columns — KEPT (additive constraint).
- tests/calibration/temporal_scope_corpus.jsonl — exercises extract_temporal_scope expected_scope; endpoint-claim emission is an ADDITIONAL claim, so scope expectations stay valid but the corpus runner may need to tolerate the extra claim (see affected_tests).

## AFFECTED TESTS
- tests/unit/test_temporal.py — will-break: NO (extract_temporal_scope/TemporalScope/BEFORE_PRESENT unchanged); needs-update: NO.
- tests/calibration/temporal_scope_corpus.jsonl + tests/calibration/test_corpus_runner.py — needs-update: the extractor now emits an ADDITIONAL employment_started/_ended (etc.) claim for dated interval relations; runner assertions that count claims or expect a single claim per text must tolerate/expect the extra date-in-object claim. Confirm whether the runner matches on scope-only or on full claim set.
- tests/unit/test_kb_verifier.py — needs-update/new-test-needed: add a test that a 'time' object_type predicate only contradicts on date/literal value_type (exercises the reconciled _OBJECT_TYPE_COMPATIBLE_VALUE_TYPES gate); existing _scope_compatible tests unchanged.
- tests/unit/test_extractor.py — needs-update: add cases asserting 'Asa joined Google in 2020' yields both employed_by(valid_from=2020) AND employment_started(Asa,'2020'); 'Asa left Google in 2024' yields employed_by(valid_until=2024)+employment_ended; bare 'Asa joined Google' yields NO endpoint claim.
- tests/unit/test_seed_loader.py — needs-update: assert the new *_started/_ended seed rows load with object_type='time', kb_property in {P108,P463,P39}, and the qualifier slot_to_qualifier map; verify the object_type 'date'->'time' reconciliation doesn't trip a loader enum check (there is none currently).
- tests/unit/test_wikidata_adapter.py — new-test-needed: assert a P108 fixture with P580/P582 round-trips P580/P582 into stmt.qualifiers (covers the resolver's qualifier-read path in fixture mode).
- tests/unit/test_tier_u.py — needs-update: add coverage that an employment_started Tier U row is found by _gather_interval's lookup (Tier U endpoint participates alongside KB qualifiers); existing scope-conflict tests unchanged.
- NEW tests/unit/test_walker_interval.py — new-test-needed: _interval_holds_at three-valued table (open-end=true, start>T=false, end<T=false, unknown-start=unknown); _verify_interval_endpoint verified/contradicted/abstain via fixtures; fail-closed on KB error.
- tests/evaluation/medium_bar_test_set.jsonl — affected: any employment/role/membership-with-date case now produces an extra endpoint claim; verify aggregate verdict is unaffected (endpoint claim should verify or abstain, never false-contradict).

## ORDERING / DEPENDENCIES
- 1. Reconcile object_type 'date'->'time' in seeds (prerequisite for the value-type gate to be live).
- 2. Add *_started/_ended seed rows + P580/P582 fixtures.
- 3. Implement walker interval resolver + holds_at + endpoint verifier + _try_external_grounding wiring (+ optional kb_interval routing_hint).
- 4. Amend extractor prompt (Rules 12/13/14 + new Rule 25) LAST so the resolver/seeds exist before the extractor emits new claims — system stays functional throughout.
- Cross-workstream dependency on Workstream 5 (Corrections/Observability): the kb_interval trace edge must carry contradicting_value and a retractable row id so Workstream 5's ClaimVerdict.contradicting_value plumbing and _TRACE_ROW_ID_KEYS retraction pick it up — coordinate the edge metadata keys.
- Cross-workstream dependency on Workstream 4 (verify-every-claim): endpoint claims must remain fully shaped (non-empty subject/predicate/object) so they survive whether or not the four current _build_claim drops have been converted to abstention_reason yet. No hard ordering, but the endpoint-claim shape must not rely on a dropped filter.
- Cross-workstream awareness of Workstream 1 (PredicateBinding multi-property map): the new *_started/_ended seeds are authored in the scalar legacy shape; under WS1 they synthesize one PredicateBinding from their scalar columns (migration-safe per contract). The qualifier-slot mapping (object->qualifier:P580) should be expressible as a PredicateBinding.slot_to_qualifier entry — confirm WS1's PredicateBinding carries slot_to_qualifier (it does per contract).

## RISKS / SOUNDNESS
- §3.2 never-false-verify: the interval resolver MUST fail-closed (return None/unknown) on resolution failure, KB error, ambiguous (multiple conflicting P580 for the same org), or unknown endpoint — mirroring _verify_kb_quantitative (walker.py:739-841). Three-valued 'unknown' maps to abstain, never to verified/contradicted.
- Value-type gate: reconciling object_type 'date'->'time' makes _contradiction_value_type_ok ACTUALLY gate endpoint contradictions (time -> only date/literal value_type may contradict). Before reconciliation 'date' returned None (no gate) — so a buggy mapping could fabricate a contradiction. Reconciliation is a soundness IMPROVEMENT but changes behavior for the 10 existing date predicates; regression-test born_on/died_on/founded_in_year to confirm they still verify/contradict correctly on date-typed KB values.
- Redundant claims: emitting BOTH employed_by(valid_from=2020) AND employment_started(Asa,'2020') means the aggregator sees two claims for one source span. Risk: if one verifies and the other abstains, the compound rollup must not drag the verified one down. The endpoint claim abstaining (no_grounding_found) is acceptable; it must never CONTRADICT a true base relation. Watch der_revision_006 (scope-conflict employment) and medium-bar employment cases.
- Qualifier-keyed slot_to_qualifier (object->qualifier:P580) is NOT understood by _lookup_targets (kb_verifier.py:524-533) — it returns None->NO_KB_PATH. Mitigated by routing endpoint claims through the new resolver, NOT the generic verify path. If a future change routes them through verify(), _lookup_targets must be extended first or they silently abstain.
- Wikidata data-model limits (functional-throughout): P580/P582 are statement qualifiers, year-precision common, no event-relative bounds. The resolver must tolerate missing endpoints (open intervals = ongoing) and year-only precision (year-aware _value_matches already handles). Document these limits in code.
- BEFORE_PRESENT interaction: the implicit-past default (valid_until=BEFORE_PRESENT, temporal.py:46) must map to end_known=False in the resolver so it never forces a false 'holds_at'. Confirm _scope_compatible's existing BEFORE_PRESENT special-case (kb_verifier.py:665) and the resolver agree.
- No hardcoded mappings (MEMORY constraint): the *_started/_ended -> base-property+qualifier knowledge lives in the seed JSON and the oracle prompt, NOT a Python lookup table. The walker reads kb_property + qualifier from predicate metadata, mirroring how kb_quantitative reads comparator+property from the predicate name/metadata.


==========================================================================================
