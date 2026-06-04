from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..audit.log import log_event
from ..llm.client import LLMClient

_NOW = lambda: datetime.now(timezone.utc).isoformat()

PREDICATE_METADATA_TOOL: dict[str, Any] = {
    "name": "generate_predicate_metadata",
    "description": (
        "Produce structured metadata for the given Aedos predicate. "
        "Choose routing_hint conservatively: prefer abstain over a speculative kb_resolvable."
    ),
    "input_schema": {
        "type": "object",
        "required": ["object_type", "user_subject_required", "routing_hint", "reason"],
        "properties": {
            "object_type": {
                "type": "string",
                "enum": ["entity", "quantity", "time", "proposition", "entity_list"],
                "description": "The type of value the object slot holds.",
            },
            "user_subject_required": {
                "type": "integer",
                "enum": [0, 1],
                "description": "1 if the subject must be the asserting party (e.g., prefers, believes).",
            },
            "distinct_slots": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Slot pairs that must differ, e.g. ['subject', 'object'].",
            },
            "routing_hint": {
                "type": "string",
                "enum": [
                    "user_authoritative",
                    "python",
                    "kb_resolvable",
                    "kb_quantitative",
                    "abstain",
                ],
            },
            "kb_namespace": {
                "type": ["string", "null"],
                "description": "KB namespace, e.g. 'wikidata'. Null when not kb_resolvable.",
            },
            "kb_property": {
                "type": ["string", "null"],
                "description": "KB property identifier, e.g. 'P39'. Null when not kb_resolvable.",
            },
            "candidate_kb_properties": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": (
                    "v0.16: ADDITIONAL plausible KB property identifiers when "
                    "more than one property could fit the predicate (e.g. a "
                    "copula 'X is a physicist' could route to P31 instance-of "
                    "OR P106 occupation). List the others here; evidence "
                    "arbitrates across them at verify time. Null/omit when the "
                    "primary kb_property is unambiguous."
                ),
            },
            "sample_subject_qids": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": (
                    "v0.16.1 (SLING distant supervision): a SMALL list (2-3) of "
                    "well-known Wikidata subject Q-ids that are canonical, "
                    "uncontroversial instances of this predicate's SUBJECT — set "
                    "ONLY for a LONG-TAIL kb_resolvable edge whose Wikidata "
                    "property has no usable P2302 type/constraint ontology (a "
                    "niche or non-standard relation). The substrate samples these "
                    "entities' co-occurring KB properties to propose ONE extra "
                    "verify-only candidate binding (it can never contradict). "
                    "Emit Q-ids you are confident name the right entity; null/omit "
                    "for any predicate whose property is well-constrained or whose "
                    "subjects you are unsure of (the default — no SLING sampling)."
                ),
            },
            "slot_to_qualifier": {
                "type": ["object", "null"],
                "description": "JSON mapping Aedos slot names to KB qualifier P-numbers.",
            },
            "premise_properties": {
                "type": ["object", "null"],
                "description": (
                    "v0.16.1 (premise -> Python channel): for a routing_hint='python' "
                    "COMPARISON predicate that must compute over a FETCHED fact about "
                    "an entity slot (not the literal slot text), a JSON mapping of the "
                    "Aedos slot name to the KB property whose value is the premise. "
                    "Example: 'born_before(X, Y)' compares X's and Y's birth dates, so "
                    "premise_properties={\"subject\": \"P569\", \"object\": \"P569\"}; "
                    "'founded_before(X, 1800)' fetches only the subject's inception, so "
                    "premise_properties={\"subject\": \"P571\"} (the object is a literal "
                    "year, no fetch). The walker resolves each named slot to a Q-id, "
                    "fetches that property's value, and passes it to the generated "
                    "verify() as a premise. Set ONLY for python comparison predicates "
                    "that genuinely need a fetched fact; null/omit otherwise (the "
                    "default — the Python tier then sees only the claim's three slots, "
                    "exactly as before)."
                ),
            },
            "single_valued": {
                "type": "integer",
                "enum": [0, 1],
                "description": (
                    "1 if the predicate is functional/single-valued — a subject "
                    "has at most one true object (e.g. place_of_birth, date_of_death). "
                    "0 if multi-valued (e.g. position_held, occupation, award_received). "
                    "Only a functional predicate licenses a KB contradiction from a "
                    "non-matching value."
                ),
            },
            "subject_entity_types": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": (
                    "Phase G D33: Wikidata Q-ids of acceptable instance-of (P31) "
                    "types for the subject slot. Used to filter entity-resolution "
                    "candidates by type. Example: ['Q5'] for a predicate whose "
                    "subject must be a human (Q5). Return null (or omit) for "
                    "open-type predicates that accept many subject types, where "
                    "filtering would over-constrain."
                ),
            },
            "object_entity_types": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": (
                    "Phase G D33: Wikidata Q-ids of acceptable instance-of (P31) "
                    "types for the object slot. Used to filter entity-resolution "
                    "candidates by type. Example: ['Q3918', 'Q38723'] for an "
                    "educational-institution slot. Return null (or omit) for "
                    "predicates whose object types are open (e.g. prefers, "
                    "status) or non-entity (literal-typed predicates)."
                ),
            },
            "reason": {
                "type": "string",
                "description": "1-2 sentence justification for the routing and mapping choices.",
            },
        },
    },
}

_GENERATION_SYSTEM_PROMPT = """\
You are a knowledge-representation expert helping to build a claim-verification system.
Given an Aedos predicate (a canonical snake_case relational predicate), produce its metadata.

object_type options:
  entity       — the object is a named entity (person, place, org, concept)
  quantity     — the object is a number with optional unit (degrees, kilograms,
                 minutes); also durations and other measurable values
  time         — the object is a date, time, or duration
  proposition  — the object is a nested claim
  entity_list  — the object is a list of entities

routing_hint — pick the SINGLE most-applicable verification source:

  user_authoritative — the asserting party is ground truth. Use whenever the
    predicate describes a personal state, preference, belief, opinion, intention,
    or first-person experience that only the asserting party can reliably
    report.
      examples: prefers, believes, feels, experienced, ranks, intends, fears,
                hopes, plans_to, regrets, remembers, knows
      signal: the asserting party is also the subject (first-person claims),
              OR the predicate is about an inner state that only the
              asserter has privileged access to.
      caution: do NOT default to abstain just because the claim is subjective —
              if the asserting party is the right authority for it, route here.

  python — reducible to deterministic computation: arithmetic, comparison,
    sequence ordering, date math, string operations, or formal logic over
    literal values. The object is typically a quantity, time, or another
    literal value.
      examples: equals, is_between, is_prime, has_length_of,
                chronologically_precedes, older_than, divides,
                is_anagram_of, lies_in_range
      signal: the verdict is a mathematical or logical computation, not a
              fact-lookup.
      premise_properties (python comparison predicates only): when the
              computation is over a FETCHED FACT about an entity slot rather
              than the slot's literal text — "X born before Y" compares the two
              people's birth dates; "X founded before 1800" compares the org's
              inception year to a literal — set premise_properties to a JSON map
              from the entity slot name to the KB property whose value is the
              premise. born_before / older_than → {"subject":"P569",
              "object":"P569"} (both birth dates, P569); founded_before /
              inception_precedes → {"subject":"P571"} when the object is a
              literal year (no fetch for a literal slot). Leave it null for pure
              arithmetic/string predicates whose operands are already the literal
              slots ("10 squared is 100", "len('aedos') is 5") — those need no
              fetch.

  kb_resolvable — maps to a structured Wikidata property and refers to a
    publicly-known fact about a real-world entity.
      examples:
        born_in        P19   has_capital     P36
        founded        P112  (founder; note: P571 is inception date, not founder)
        co_founded     P112  (also founder; multi-valued)
        died_in        P20   spouse          P26
        born_on        P569  (date of birth — object_type=time, NOT P19 place)
        died_on        P570  (date of death — object_type=time, NOT P20 place)
        founded_in_year P571 (inception — object_type=time, NOT P112 founder)
        dissolved_in_year P576 (dissolved/abolished/demolished date — time)
        published_in_year / released_in_year P577 (publication date — time)
        occurred_in_year P585 (point in time — object_type=time)
        educated_at    P69   member_of       P463
      INTERVAL ENDPOINTS (v0.16 T1): a `<relation>_started` / `<relation>_ended`
        predicate (employment_started, membership_ended, role_started, …)
        grounds against the start-time (P580) / end-time (P582) QUALIFIER on
        the BASE relation's statement, NOT a statement value. Set kb_property to
        the BASE property (employment→P108, membership→P463, role→P39),
        object_type=time, routing_hint=kb_interval, and
        slot_to_qualifier={"subject":"statement_subject","org":"statement_value",
        "object":"qualifier:P580" (started) | "qualifier:P582" (ended)}. The
        walker's interval resolver reads the qualifier; do not route these to
        the generic value-compare path.
        occupation     P106  parent          P22 / P25
        successor_of   P1365 (replaces; note: P155 is `follows` in a sequence)
        has_isbn       P212  (identifier of a book — object_type=entity, since
                              the identifier names a specific publishable work)
        part_of        P361  (mereological; distinct_slots=true to disambiguate
                              from inverse `has_part`)
      caution: when more than one property could fit, pick the property that
              matches the predicate's MEANING (founder vs. inception), not the
              first plausible match.

      candidate_kb_properties: if MORE THAN ONE property could genuinely fit
              (e.g. a copula "X is a physicist" could be P31 instance-of OR
              P106 occupation), set kb_property to your best primary choice
              and list the others in candidate_kb_properties. Evidence will
              arbitrate across them at verify time — a value-type-incompatible
              candidate simply abstains rather than contradicting. Leave it
              null when the primary choice is unambiguous.
              COPULA / instance_of RULE: for the copula predicate
              `instance_of` (or `is_a`) — "X is a <noun>" — ALWAYS set
              kb_property=P31 (instance of) as the primary AND list
              candidate_kb_properties=["P106"] (occupation). The object may be a
              profession/role noun ("guitarist", "physicist", "novelist"), in
              which case the subject's true grounding is P106 not P31 (a person's
              P31 is just Q5 human). The P106 candidate is value-type-gated: it
              only verifies when the object is a confirmed occupation/profession,
              so a non-profession copula ("X is a river"/"X is a city") harmlessly
              falls back to the P31 instance-of check. Leave the row-level
              object_entity_types null/open (the P31 object can be ANY class —
              river, city, profession); the P106 candidate's occupation value-type
              constraint is supplied by P106's own ontology at discovery time.

      sample_subject_qids (LONG-TAIL kb_resolvable edges ONLY): when the
              kb_property is a NICHE / non-standard Wikidata relation that lacks a
              usable P2302 type/constraint ontology (so candidate_kb_properties
              and the ontology can't help), you MAY list 2-3 well-known Wikidata
              subject Q-ids that are canonical, uncontroversial instances of this
              predicate's SUBJECT. The substrate uses distant supervision over
              their co-occurring properties to propose ONE extra verify-only
              candidate binding (it can never drive a contradiction and is
              value-type-gated on the positive path). Emit Q-ids ONLY when you are
              confident they name the right entity; leave it null for any common,
              well-constrained property (born_in, occupation, member_of, ...) and
              whenever you are unsure of the subjects -- the default is no sampling.

  kb_quantitative — a numeric COMPARISON predicate ('..._greater_than' /
    '..._less_than' in the name) against a count-valued Wikidata property.
    Set kb_property to the property whose value is the count (population
    P1082, members P2124, employees P1128, students P2196, seats P1342, …);
    the comparator is read from the predicate name, object_type=quantity. Use
    only for DIMENSIONLESS counts — measurements with physical units route to
    abstain until unit-aware comparison exists.
      examples: population_greater_than P1082, members_less_than P2124,
                employees_greater_than P1128
      signal: the predicate compares the subject's count of something to a
              numeric threshold the KB records.

  abstain — no authoritative source of belief. Reserve for predicates that
    are intrinsically contested across observers (no single ground truth), or
    too vague/speculative to verify.
      examples: influenced (no clean ground-truth), is_better_than,
                is_smarter_than, is_more_important_than, would_have,
                secretly_believes, relates_to, connects_to
      caution: abstain is for "no source of belief exists" — not "I'm uncertain
              which of the other three to pick." Pick a routing if any of the
              other three plausibly applies.

When in doubt between routings: prefer user_authoritative or python over
kb_resolvable (they fail safe by checking a different source); prefer
kb_resolvable over abstain (kb-not-found yields a clean abstention at
verification time, while routing_hint=abstain forecloses verification entirely).

DATATYPE CONSISTENCY (kb_resolvable only): the kb_property you choose must
return values whose Wikidata datatype matches object_type. An
object_type=entity predicate must map to a wikibase-item property (P50 author,
P112 founder, P19 place of birth) — never to a time property (P571 inception,
P585 point in time) or a quantity property. An object_type=time predicate must
map to a time property; an object_type=quantity predicate to a quantity
property. Example of the error to avoid: "published" in "X published work Y"
has object_type=entity (the work) and maps to P50 (author) — NOT P585 (point
in time); the year is a temporal scope, not the object.

INVERSE / DIRECTIONAL MAPPINGS: some predicates store their KB statement on
the OBJECT's side, not the subject's. Authorship and creation are the common
case: "X wrote / published / created / authored Y" is stored in Wikidata as
(Y, P50, X) — the work Y carries the author statement; founder is the same
shape, "X founded Y(org)" is (Y, P112, X). For these set
slot_to_qualifier = {"subject": "statement_value", "object": "statement_subject"}
so the Aedos subject is matched against the statement value and the Aedos
object is the looked-up statement subject (the same inverse shape used by
capital_of→P36 and mother_of→P25). Standard (non-inverse) predicates omit
slot_to_qualifier or set {"subject": "statement_subject", "object":
"statement_value"}.

single_valued: set 1 only for functional predicates where a subject has at
most one true object (place_of_birth, date_of_death, capital). Set 0 for
multi-valued predicates (position_held, occupation, award_received,
member_of). When unsure, choose 0 — a wrong single_valued=1 produces a false
contradiction; a wrong single_valued=0 only loses a contradiction check.

distinct_slots: set true for predicates where subject and object can map to
the same KB property but to different qualifier roles (e.g. `part_of` and
`has_part` both map to P361 but invert the slot direction).

subject_entity_types / object_entity_types: Wikidata Q-ids of the acceptable
instance-of (P31) types for each slot. The deployed pipeline post-filters
entity-resolution candidates by these types — a wrong-type Q-id is eliminated
before the resolver sees it.

Set these to a list of Q-ids only when the slot's type is naturally constrained
by the predicate's meaning. Common patterns:
  holds_role     subject=[Q5] (human),     object=[Q4164871] (position)
  has_nationality subject=[Q5] (human),    object=[Q6256] (country) — REQUIRED:
                 the country type gates the verifier's P1549 demonym→country
                 resolution, so a nationality predicate whose object is a
                 demonym ("German") must declare object_entity_types=[Q6256].
  born_in        subject=[Q5] (human),     object=[Q515, Q486972] (city, settlement)
  educated_at    subject=[Q5] (human),     object=[Q3918, Q38723] (university,
                                                  higher-education institution)
  has_capital    subject=[Q6256] (country), object=[Q515, Q5119] (city, capital)

Return null (or omit) when the slot type is open — a slot that legitimately
accepts many entity types (prefers, applies_to) or non-entity values (any
predicate where object_type is quantity, time, or proposition). Over-constraining
with a too-narrow type list will eliminate canonical candidates; under-constraining
with an over-broad list will not help filter. When in doubt, prefer null over a
guess — a missing filter is cheaper than a wrong one.
"""


@dataclass
class PredicateBinding:
    """One candidate (predicate -> KB property) binding. The substrate holds a
    RANKED LIST of these per predicate; evidence arbitrates at verify time
    (v0.16 Decision 1). A legacy scalar row synthesizes exactly one binding."""
    kb_namespace: Optional[str]
    kb_property: Optional[str]
    slot_to_qualifier: Optional[dict] = None
    single_valued: bool = False
    subject_entity_types: Optional[list[str]] = None
    object_entity_types: Optional[list[str]] = None
    source: str = "legacy_scalar"   # legacy_scalar | oracle | ontology_p2302 | sling | candidate
    rank: float = 1.0               # discovery-time prior; verify-time evidence reorders
    # v0.16.1 WS2 (occupation-copula grounding): when True, this binding's
    # POSITIVE grounding is FAIL-CLOSED type-gated — it may only yield VERIFIED
    # when the resolved object is PROVABLY subsumed by one of `object_entity_types`
    # (a confirmed occupation/profession class). Set on the P106 candidate binding
    # synthesized for a copula's `instance_of`/`is_a` predicate so a non-occupation
    # copula ("X is a river") cannot false-verify through the occupation property.
    # The primary binding (P31) leaves this False — its positive path is unchanged.
    value_type_gated: bool = False


def _binding_to_dict(b: "PredicateBinding") -> dict:
    """Serialize a PredicateBinding for the `bindings` JSON column. Mirrors the
    keys `_row_to_metadata` reads back, so a round-trip is lossless."""
    return {
        "kb_namespace": b.kb_namespace,
        "kb_property": b.kb_property,
        "slot_to_qualifier": b.slot_to_qualifier,
        "single_valued": bool(b.single_valued),
        "subject_entity_types": b.subject_entity_types,
        "object_entity_types": b.object_entity_types,
        "source": b.source,
        "rank": b.rank,
        "value_type_gated": bool(b.value_type_gated),
    }


@dataclass
class PredicateMetadata:
    id: int
    aedos_predicate: str
    object_type: str
    user_subject_required: bool
    distinct_slots: Optional[list[str]]
    routing_hint: str
    kb_namespace: Optional[str]
    kb_property: Optional[str]
    slot_to_qualifier: Optional[dict]
    reason: str
    created_at: str
    last_consulted_at: Optional[str] = None
    used_count: int = 0
    retracted_at: Optional[str] = None
    retraction_reason: Optional[str] = None
    single_valued: bool = False  # functional predicate: licenses KB contradiction
    # Wikidata Q-ids of acceptable entity types for
    # each slot. None means no filtering for that slot (open-type predicate or
    # predicate not yet annotated). Surfaced from the seed pack (predicates
    # annotated in seeds/predicate_translation.json) or from the substrate
    # oracle's cold-start generation.
    subject_entity_types: Optional[list[str]] = None
    object_entity_types: Optional[list[str]] = None
    # v0.16.1 WS3b (premise -> Python channel): for a routing_hint='python'
    # COMPARISON predicate that computes over a FETCHED fact, a JSON map from an
    # Aedos slot name ('subject' / 'object') to the KB property whose value is
    # the premise (e.g. {"subject": "P569", "object": "P569"} for born_before).
    # The walker resolves each named slot to a Q-id, fetches that property, and
    # threads the value into the generated verify(...) as a premise. None (the
    # default) means NO premise fetch — the Python tier sees only the claim's
    # three slots, exactly as before. Knowledge of WHICH property to fetch lives
    # HERE (oracle/seed), never in a Python predicate->property table.
    premise_properties: Optional[dict] = None
    # v0.16 WS1: the authoritative multi-property binding list. Evidence
    # arbitrates across these at verify time. The scalar fields above are
    # RETAINED as real dataclass fields (every existing meta.kb_property
    # reader keeps working unchanged); __post_init__ keeps scalars and
    # bindings[0] mirrored: when `bindings` is empty it synthesizes ONE
    # binding from the scalar fields (source='legacy_scalar'); when
    # `bindings` is provided it sets the scalars to mirror bindings[0].
    bindings: list["PredicateBinding"] = field(default_factory=list)
    # v0.16.3 Batch B (piece 2): operator-authoritative row. When True the row is
    # immune to retraction (consistency / contradiction-trace / explicit) and is
    # never replaced by the oracle's INSERT OR REPLACE. Set for every seed row and
    # for hand-pinned corrections (capital/capital_is). Row-scoped (not per-binding).
    pinned: bool = False

    def __post_init__(self) -> None:
        if not self.bindings:
            # Read-synthesis: legacy / scalar-only construction. Build exactly
            # one binding mirroring the scalar fields so meta.bindings is
            # always a non-empty authoritative list when a property exists.
            self.bindings = [
                PredicateBinding(
                    kb_namespace=self.kb_namespace,
                    kb_property=self.kb_property,
                    slot_to_qualifier=self.slot_to_qualifier,
                    single_valued=self.single_valued,
                    subject_entity_types=self.subject_entity_types,
                    object_entity_types=self.object_entity_types,
                    source="legacy_scalar",
                )
            ]
        else:
            # Bindings-native construction: mirror bindings[0] back onto the
            # scalar fields so the ~18 existing scalar readers stay correct.
            primary = self.bindings[0]
            self.kb_namespace = primary.kb_namespace
            self.kb_property = primary.kb_property
            self.slot_to_qualifier = primary.slot_to_qualifier
            self.single_valued = primary.single_valued
            self.subject_entity_types = primary.subject_entity_types
            self.object_entity_types = primary.object_entity_types


class PredicateTranslationError(Exception):
    def __init__(self, predicate: str, cause: str, details: str = ""):
        super().__init__(f"predicate_translation failed for {predicate!r}: {cause}. {details}")
        self.predicate = predicate
        self.cause = cause
        self.details = details


class PredicateTranslation:
    def __init__(
        self,
        db: sqlite3.Connection,
        llm_client: LLMClient,
        consistency_checker=None,
        property_relations=None,
        sling=None,
        enable_sling: bool = True,
        direction_validator=None,
    ) -> None:
        self._db = db
        self._llm = llm_client
        self._consistency = consistency_checker
        # v0.16 WS1: optional binding-discovery collaborators. Both default to
        # None so every existing construction (tests, mocks, cold pipelines)
        # keeps working: when absent, discovery FALLS OPEN to the oracle's
        # single primary binding = pre-v0.16 behavior.
        self._property_relations = property_relations
        self._sling = sling
        # v0.16.1 WS4: config gate for SLING distant supervision. When False,
        # the SlingFallback is never consulted even if wired — turning SLING off
        # can only lose a (verify-only) candidate, never change a sound verdict.
        self._enable_sling = enable_sling
        # v0.16.3 Batch B (piece 1): empirical direction validator. Optional and
        # FAIL-OPEN — when None (every existing/mock/cold construction) generation
        # behaves exactly as before; when wired (deployed pipeline) it validates
        # the oracle's slot_to_qualifier DIRECTION against real KB grounding at
        # write time and either confirms, corrects, or (if it cannot confirm)
        # suppresses the contradiction license on the generated row.
        self._direction_validator = direction_validator

    def consult(
        self,
        aedos_predicate: str,
        kb_namespace: Optional[str] = None,
    ) -> PredicateMetadata:
        """Return predicate metadata from cache or generate it via LLM.

        Raises PredicateTranslationError if generation fails.
        """
        row = self._fetch(aedos_predicate)
        if row is not None:
            self._touch(row.id)
            return row
        return self._generate_and_store(aedos_predicate, kb_namespace)

    def retract(self, row_id: int, reason: str) -> None:
        """Retract a row. Sets retracted_at; does not delete.

        v0.16.3 Batch B (piece 2): a PINNED row is operator-authoritative and is
        never retracted — skip the UPDATE and log a `pin_retraction_blocked`
        event instead. Without this, an explicit retract of a pinned seed/
        correction would silently drop it (and, because _fetch filters
        retracted_at IS NULL, expose it to oracle regeneration)."""
        if self._is_pinned(row_id):
            log_event(
                self._db,
                event_type="pin_retraction_blocked",
                event_subject=f"predicate_translation:{row_id}",
                event_data={"reason": reason, "source": "retract"},
            )
            return
        now = _NOW()
        self._db.execute(
            "UPDATE predicate_translation SET retracted_at=?, retraction_reason=? WHERE id=?",
            (now, reason, row_id),
        )
        self._db.commit()
        log_event(
            self._db,
            event_type="row_retracted",
            event_subject=f"predicate_translation:{row_id}",
            event_data={"reason": reason},
        )

    def _is_pinned(self, row_id: int) -> bool:
        """True if predicate_translation row `row_id` is pinned (operator-
        authoritative). Defensive against pre-migration DBs lacking the column."""
        try:
            row = self._db.execute(
                "SELECT pinned FROM predicate_translation WHERE id=?", (row_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return bool(row[0]) if row is not None else False

    def _pinned_row_exists(self, aedos_predicate: str) -> bool:
        """True if a non-retracted PINNED row exists for this predicate. Used as
        the pre-INSERT gate in _generate_and_store so the oracle's INSERT OR
        REPLACE never deletes+replaces a pinned row (which would drop the pin and
        reassign the row id, dangling audit refs).

        Adversarial-review fix ([4]): keyed on the PREDICATE only, NOT the
        namespace. consult() passes no namespace (so the incoming kb_namespace is
        None), while the INSERT OR REPLACE collides on the EFFECTIVE namespace
        ('wikidata') decided during generation — a namespace-qualified gate matched
        only NULL-namespace rows and was inert for exactly the wikidata seed/
        correction rows it must protect. The pin guarantee is per-predicate."""
        try:
            row = self._db.execute(
                "SELECT 1 FROM predicate_translation "
                "WHERE aedos_predicate=? AND pinned=1 AND retracted_at IS NULL LIMIT 1",
                (aedos_predicate,),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None

    def _borrow_seed_slot_to_qualifier(
        self, kb_namespace: str, kb_property: str
    ) -> Optional[dict]:
        """Return any active well-formed slot_to_qualifier mapping for
        (kb_namespace, kb_property). Used
        to backfill an oracle-generated row whose sq is missing — the
        oracle named the right KB property but didn't (or couldn't)
        spell the slot mapping, and another predicate's seed already has
        the validated form. Returns None when no such peer exists.
        """
        row = self._db.execute(
            "SELECT slot_to_qualifier FROM predicate_translation "
            "WHERE kb_namespace=? AND kb_property=? "
            "AND slot_to_qualifier IS NOT NULL "
            "AND retracted_at IS NULL "
            "ORDER BY id LIMIT 1",
            (kb_namespace, kb_property),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    def query_neighbors(self, aedos_predicate: str) -> list[PredicateMetadata]:
        """Return rows whose kb_property matches the given predicate's kb_property."""
        subject = self._fetch(aedos_predicate)
        if subject is None or subject.kb_property is None:
            return []
        rows = self._db.execute(
            "SELECT * FROM predicate_translation WHERE kb_property=? AND aedos_predicate!=?",
            (subject.kb_property, aedos_predicate),
        ).fetchall()
        return [self._row_to_metadata(r) for r in rows]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch(self, aedos_predicate: str) -> Optional[PredicateMetadata]:
        """Return the first non-retracted row for the predicate, or None."""
        row = self._db.execute(
            "SELECT * FROM predicate_translation "
            "WHERE aedos_predicate=? AND retracted_at IS NULL "
            "ORDER BY id LIMIT 1",
            (aedos_predicate,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_metadata(row)

    def _touch(self, row_id: int) -> None:
        now = _NOW()
        self._db.execute(
            "UPDATE predicate_translation "
            "SET last_consulted_at=?, used_count=used_count+1 WHERE id=?",
            (now, row_id),
        )
        self._db.commit()

    def _generate_and_store(
        self, aedos_predicate: str, kb_namespace: Optional[str]
    ) -> PredicateMetadata:
        # v0.16.3 Batch B (piece 2): never regenerate over a PINNED row. consult()
        # already serves a present non-retracted row from _fetch, so this only
        # fires on an edge cache miss (e.g. a namespace-qualified call path) — but
        # because the INSERT OR REPLACE below DELETES the colliding row, a missing
        # guard here would silently drop the pin + reassign the row id. Return the
        # pinned row's metadata instead of generating.
        if self._pinned_row_exists(aedos_predicate):
            pinned = self._fetch(aedos_predicate)
            if pinned is not None:
                self._touch(pinned.id)
                return pinned
        try:
            raw = self._llm.extract_with_tool(
                system=_GENERATION_SYSTEM_PROMPT,
                user_message=f'Generate metadata for the Aedos predicate: "{aedos_predicate}"',
                tool=PREDICATE_METADATA_TOOL,
                purpose="substrate:predicate_translation",
            )
        except Exception as exc:
            log_event(
                self._db,
                event_type="row_generation_failed",
                event_subject=f"predicate_translation:{aedos_predicate}",
                event_data={"error": str(exc)},
            )
            raise PredicateTranslationError(
                aedos_predicate, "llm_call_failed", str(exc)
            ) from exc

        # Validate required fields
        for required in ("object_type", "routing_hint", "reason"):
            if not raw.get(required):
                log_event(
                    self._db,
                    event_type="row_generation_failed",
                    event_subject=f"predicate_translation:{aedos_predicate}",
                    event_data={"error": f"missing field: {required}"},
                )
                raise PredicateTranslationError(
                    aedos_predicate,
                    "malformed_response",
                    f"missing required field: {required}",
                )

        now = _NOW()
        effective_kb_namespace = raw.get("kb_namespace") or kb_namespace
        distinct_slots_raw = raw.get("distinct_slots")

        # When the oracle declared a
        # kb_property but provided no slot_to_qualifier (the common
        # malformed-runtime shape that motivated the consistency-check
        # skip), borrow the slot_to_qualifier from any well-formed seed or
        # prior runtime row that maps to the same (kb_namespace,
        # kb_property). Predicates sharing a KB property are aliases at the
        # KB layer; the seed's hand-validated sq is the right mapping. This
        # turns the oracle's runtime additions from "kb_property is right
        # but walker can't look up via NULL sq" into "kb_property is right
        # AND walker uses the seed's validated slot map."
        if raw.get("kb_property") and effective_kb_namespace and not raw.get("slot_to_qualifier"):
            borrowed_sq = self._borrow_seed_slot_to_qualifier(
                effective_kb_namespace, raw["kb_property"]
            )
            if borrowed_sq is not None:
                raw["slot_to_qualifier"] = borrowed_sq
        slot_to_qualifier_raw = raw.get("slot_to_qualifier")
        subject_types_raw = raw.get("subject_entity_types")
        object_types_raw = raw.get("object_entity_types")
        single_valued = int(raw.get("single_valued", 0) or 0)

        # v0.16 WS1: build the RANKED binding list. For a kb_resolvable
        # predicate, discovery enriches the oracle's primary property with
        # Wikidata-ontology-typed candidates (PropertyRelations) plus a SLING
        # fallback when the ontology can't constrain a candidate. FALLS OPEN:
        # when the collaborators are absent or yield nothing (mock/cold), the
        # list is exactly the single oracle binding = pre-v0.16 behavior. The
        # scalar columns below are kept = bindings[0] for back-compat.
        bindings = self._discover_bindings(
            aedos_predicate,
            raw,
            effective_kb_namespace,
            slot_to_qualifier_raw,
            bool(single_valued),
            subject_types_raw if subject_types_raw else None,
            object_types_raw if object_types_raw else None,
        )
        primary = bindings[0] if bindings else None
        # Mirror scalar columns onto bindings[0] (back-compat / consistency-
        # checker / seed parity). When the oracle named no property the
        # primary mirrors the (None) oracle scalars exactly.
        if primary is not None:
            effective_kb_namespace = primary.kb_namespace
            primary_kb_property = primary.kb_property
            slot_to_qualifier_raw = primary.slot_to_qualifier
            single_valued = int(bool(primary.single_valued))
            subject_types_raw = primary.subject_entity_types
            object_types_raw = primary.object_entity_types
        else:
            primary_kb_property = raw.get("kb_property")

        # v0.16.3 Batch B (piece 1): validate the slot_to_qualifier DIRECTION
        # empirically before persisting. Only for entity-object KB-relational
        # predicates (direction is meaningless for literal objects / non-KB rows).
        # Orientation MUST use the oracle's RAW declared entity-types (raw.get),
        # never the discovery-overridden binding types — discovery fills
        # subject_entity_types from the ontology's statement-subject type ASSUMING
        # standard direction, so using those would make the standard assumption
        # self-confirming. Returns possibly-updated (sq, single_valued).
        if (
            self._direction_validator is not None
            and primary_kb_property
            and raw.get("routing_hint") == "kb_resolvable"
            and raw.get("object_type") == "entity"
        ):
            slot_to_qualifier_raw, single_valued, bindings = (
                self._apply_direction_validation(
                    aedos_predicate,
                    primary_kb_property,
                    effective_kb_namespace,
                    slot_to_qualifier_raw,
                    single_valued,
                    bindings,
                    raw,
                )
            )

        bindings_json = (
            json.dumps([_binding_to_dict(b) for b in bindings]) if bindings else None
        )

        # INSERT OR REPLACE handles the case where a retracted row exists for the same
        # (predicate, namespace) key — SQLite deletes the old row and inserts the new one.
        premise_properties_raw = raw.get("premise_properties") or None
        self._db.execute(
            """INSERT OR REPLACE INTO predicate_translation
               (aedos_predicate, object_type, user_subject_required, distinct_slots,
                routing_hint, kb_namespace, kb_property, slot_to_qualifier,
                single_valued, subject_entity_types, object_entity_types,
                reason, created_at, bindings, premise_properties, pinned)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                aedos_predicate,
                raw["object_type"],
                int(raw.get("user_subject_required", 0)),
                json.dumps(distinct_slots_raw) if distinct_slots_raw else None,
                raw["routing_hint"],
                effective_kb_namespace,
                primary_kb_property,
                json.dumps(slot_to_qualifier_raw) if slot_to_qualifier_raw else None,
                single_valued,
                json.dumps(subject_types_raw) if subject_types_raw else None,
                json.dumps(object_types_raw) if object_types_raw else None,
                raw["reason"],
                now,
                bindings_json,
                json.dumps(premise_properties_raw) if premise_properties_raw else None,
            ),
        )
        self._db.commit()
        row_id = self._db.execute("SELECT last_insert_rowid()").fetchone()[0]

        log_event(
            self._db,
            event_type="row_created",
            event_subject=f"predicate_translation:{row_id}",
            event_data={
                "aedos_predicate": aedos_predicate,
                "routing_hint": raw["routing_hint"],
                "kb_property": primary_kb_property,
                "binding_count": len(bindings),
                "binding_sources": [b.source for b in bindings],
            },
        )

        # Substrate-internal consistency check on write (architecture 5.4).
        if self._consistency is not None:
            _result = self._consistency.check_on_write("predicate_translation", row_id)
            if _result.status == "conflict":
                self._consistency.resolve_conflict(_result)

        return PredicateMetadata(
            id=row_id,
            aedos_predicate=aedos_predicate,
            object_type=raw["object_type"],
            user_subject_required=bool(int(raw.get("user_subject_required", 0))),
            distinct_slots=distinct_slots_raw,
            routing_hint=raw["routing_hint"],
            kb_namespace=effective_kb_namespace,
            kb_property=primary_kb_property,
            slot_to_qualifier=slot_to_qualifier_raw,
            reason=raw["reason"],
            created_at=now,
            single_valued=bool(single_valued),
            subject_entity_types=subject_types_raw if subject_types_raw else None,
            object_entity_types=object_types_raw if object_types_raw else None,
            premise_properties=premise_properties_raw,
            bindings=bindings,
        )

    def _apply_direction_validation(
        self,
        aedos_predicate: str,
        kb_property: str,
        kb_namespace: Optional[str],
        slot_to_qualifier_raw: Optional[dict],
        single_valued: int,
        bindings: list,
        raw: dict,
    ):
        """v0.16.3 Batch B (piece 1): consult the DirectionValidator and apply its
        verdict to the row about to be written. Returns the (possibly corrected)
        ``(slot_to_qualifier, single_valued, bindings)``.

        - CORRECTED: rewrite slot_to_qualifier across the scalar AND every binding
          (the back-compat invariant requires bindings[0]/scalars to agree).
        - NOT validated (unconfirmed/suspect): keep the oracle direction for
          positive grounding but force single_valued=0 — the operator-chosen
          "never CONTRADICT on an unvalidated direction" posture (single_valued is
          the only flag that licenses a CONTRADICTED verdict).
        - CONFIRMED / SYMMETRIC: unchanged.
        Always annotates the reason + logs an event for the audit trail. FAIL-OPEN
        on any error (the validator itself never raises)."""
        try:
            verdict = self._direction_validator.validate(
                kb_property,
                kb_namespace,
                slot_to_qualifier_raw,
                raw.get("subject_entity_types"),
                raw.get("object_entity_types"),
            )
        except Exception:
            return slot_to_qualifier_raw, single_valued, bindings

        if verdict.status == "corrected" and verdict.direction is not None:
            slot_to_qualifier_raw = dict(verdict.direction)
            for b in bindings:
                b.slot_to_qualifier = dict(verdict.direction)

        # Adversarial-review fix ([2]/[3]): keep the contradiction license ONLY on
        # a CONFIRMED verdict — the oracle's OWN direction grounded the known-true
        # example (independent corroboration that the oracle was self-consistent).
        # A CORRECTED direction was re-derived using the oracle's (untrusted) raw
        # entity-types, and a SYMMETRIC classification can be flipped by one
        # anomalous reciprocal example — so neither may license a CONTRADICTED
        # verdict. suspect/unconfirmed never did. The corrected DIRECTION is still
        # applied above for positive grounding; only single_valued is suppressed.
        if verdict.status != "confirmed":
            single_valued = 0
            for b in bindings:
                b.single_valued = False

        # Carry the validation outcome into the reason + audit log so the row's
        # provenance is inspectable and the piece-4 audit can correlate.
        suffix = f" [direction_validation:{verdict.status} ({verdict.reason})]"
        if raw.get("reason"):
            raw["reason"] = f"{raw['reason']}{suffix}"
        else:
            raw["reason"] = suffix.strip()
        log_event(
            self._db,
            event_type="direction_validation",
            event_subject=f"predicate_translation:{aedos_predicate}",
            event_data={
                "kb_property": kb_property,
                "status": verdict.status,
                "reason": verdict.reason,
                "grounded": verdict.grounded,
                "validated": verdict.is_validated,
            },
        )
        return slot_to_qualifier_raw, single_valued, bindings

    def _discover_bindings(
        self,
        aedos_predicate: str,
        raw: dict,
        kb_namespace: Optional[str],
        slot_to_qualifier: Optional[dict],
        oracle_single_valued: bool,
        oracle_subject_types: Optional[list],
        oracle_object_types: Optional[list],
    ) -> list[PredicateBinding]:
        """v0.16 WS1 binding discovery.

        Builds the RANKED candidate-binding list for a freshly-generated row:

          1. Collect candidate P-ids: the oracle's primary `kb_property` plus
             the optional `candidate_kb_properties` it proposed.
          2. For each candidate, fetch the Wikidata property ontology
             (PropertyRelations). When the ontology constrains the property,
             build an `ontology_p2302` binding (constrained value/subject types
             from the ontology; single_valued mirrors the oracle's flag —
             authoritative, never OR-promoted by the ontology).
             When the ontology is empty, build an `oracle` binding from the
             oracle scalars; and (SLING) ask the distant-supervision fallback
             for additional low-rank candidates.
          3. Rank: ontology-typed > oracle-primary > sling.

        FALLS OPEN: when `property_relations`/`sling` are absent (mock/cold) or
        yield nothing, the result is a SINGLE oracle binding mirroring the
        scalar columns = pre-v0.16 behavior. Never raises — any discovery
        error degrades to the oracle binding.
        """
        # Only kb_resolvable predicates carry KB bindings; everything else
        # synthesizes the same single (possibly property-less) binding the
        # __post_init__ path would, keeping non-KB rows identical to before.
        primary_prop = raw.get("kb_property")
        oracle_binding = PredicateBinding(
            kb_namespace=kb_namespace,
            kb_property=primary_prop,
            slot_to_qualifier=slot_to_qualifier,
            single_valued=oracle_single_valued,
            subject_entity_types=oracle_subject_types,
            object_entity_types=oracle_object_types,
            source="oracle",
            rank=0.5,
        )

        if raw.get("routing_hint") != "kb_resolvable" or not primary_prop:
            # Not a KB binding — preserve the legacy single-binding shape with
            # source='legacy_scalar' so it is indistinguishable from a scalar
            # row's read-synthesized binding.
            oracle_binding.source = "legacy_scalar"
            oracle_binding.rank = 1.0
            return [oracle_binding]

        if self._property_relations is None:
            # No discovery infrastructure wired (mock/cold) → fall open to the
            # oracle's single primary binding. Mark legacy_scalar so the row is
            # byte-identical to a scalar-synthesized single-binding row.
            oracle_binding.source = "legacy_scalar"
            oracle_binding.rank = 1.0
            return [oracle_binding]

        try:
            return self._discover_bindings_inner(
                aedos_predicate, raw, kb_namespace, slot_to_qualifier,
                oracle_single_valued, oracle_subject_types, oracle_object_types,
                oracle_binding,
            )
        except Exception as exc:  # discovery is enrichment; never break a write
            log_event(
                self._db,
                event_type="binding_discovery_failed",
                event_subject=f"predicate_translation:{aedos_predicate}",
                event_data={"error": str(exc)},
            )
            oracle_binding.source = "legacy_scalar"
            oracle_binding.rank = 1.0
            return [oracle_binding]

    def _discover_bindings_inner(
        self,
        aedos_predicate: str,
        raw: dict,
        kb_namespace: Optional[str],
        slot_to_qualifier: Optional[dict],
        oracle_single_valued: bool,
        oracle_subject_types: Optional[list],
        oracle_object_types: Optional[list],
        oracle_binding: PredicateBinding,
    ) -> list[PredicateBinding]:
        primary_prop = raw.get("kb_property")
        candidates: list[str] = [primary_prop]
        extra = raw.get("candidate_kb_properties")
        if isinstance(extra, list):
            for pid in extra:
                if isinstance(pid, str) and pid and pid not in candidates:
                    candidates.append(pid)

        primary_ontology_bindings: list[PredicateBinding] = []
        candidate_ontology_bindings: list[PredicateBinding] = []
        candidate_bindings: list[PredicateBinding] = []
        sling_bindings: list[PredicateBinding] = []
        any_ontology_empty = False

        for pid in candidates:
            # v0.16.1 WS2: a NON-PRIMARY candidate (e.g. P106 occupation proposed
            # alongside the primary P31 for a copula) is FAIL-CLOSED type-gated on
            # its positive path — it may only VERIFY when the resolved object is a
            # confirmed value-type-class member. The primary property is never
            # positive-gated (its mapping is the oracle's best choice).
            is_candidate = pid != primary_prop
            ontology = self._property_relations.fetch(
                pid, kb_namespace or "wikidata"
            )
            if ontology is not None and not ontology.is_empty():
                target = (
                    candidate_ontology_bindings if is_candidate
                    else primary_ontology_bindings
                )
                target.append(
                    PredicateBinding(
                        kb_namespace=kb_namespace,
                        kb_property=pid,
                        # Ontology doesn't carry Aedos slot maps; reuse oracle's.
                        slot_to_qualifier=slot_to_qualifier,
                        # single_valued is the ONLY flag that licenses a
                        # CONTRADICTED verdict, so the oracle stays AUTHORITATIVE
                        # for it: the oracle prompt is explicitly conservative
                        # ("when unsure choose 0 — a wrong single_valued=1
                        # produces a FALSE CONTRADICTION"). The ontology supplies
                        # only types/constraints; it may NOT OR-promote the flag
                        # past the oracle's deliberate 0 (§3.2 never-false-
                        # contradict). Mirror the oracle's flag verbatim.
                        single_valued=oracle_single_valued,
                        subject_entity_types=(
                            ontology.subject_type_qids or oracle_subject_types
                        ),
                        object_entity_types=(
                            ontology.value_type_qids or oracle_object_types
                        ),
                        source="ontology_p2302",
                        rank=1.0,
                        value_type_gated=is_candidate,
                    )
                )
            else:
                any_ontology_empty = True
                # v0.16.1 WS2: even with an empty ontology, keep a NON-PRIMARY
                # candidate binding so the alternative property (P106) is
                # verifiable. It carries the oracle's object_entity_types hint as
                # the value-type constraint and is FAIL-CLOSED positive-gated, so
                # it can only verify a confirmed occupation/profession object.
                if is_candidate:
                    candidate_bindings.append(
                        PredicateBinding(
                            kb_namespace=kb_namespace,
                            kb_property=pid,
                            slot_to_qualifier=slot_to_qualifier,
                            single_valued=oracle_single_valued,
                            subject_entity_types=oracle_subject_types,
                            object_entity_types=oracle_object_types,
                            source="candidate",
                            rank=0.4,
                            value_type_gated=True,
                        )
                    )

        # SLING fallback: only when the ontology couldn't constrain a candidate
        # (long-tail edges). Lowest rank; never licenses a contradiction.
        #
        # v0.16.1 WS4 — ACTIVATED (gated). The predicate-metadata oracle now emits
        # optional `sample_subject_qids` (tool schema + prompt) for long-tail edges
        # the ontology can't constrain; propose_bindings samples those entities'
        # co-occurring properties and proposes ONE verify-only candidate. The
        # binding is single_valued=False (can never CONTRADICT), ranks LAST, and is
        # value_type_gated=True (fail-closed positive gate) so a noisy co-occurrence
        # cannot false-verify on an unconfirmed object type. The `enable_sling`
        # config flag gates consumption: when off, SLING is never consulted (a
        # verify-only candidate is simply not produced — no sound verdict changes).
        if any_ontology_empty and self._sling is not None and self._enable_sling:
            proposed = self._sling.propose_bindings(aedos_predicate, raw)
            if isinstance(proposed, list):
                sling_bindings = [b for b in proposed if isinstance(b, PredicateBinding)]

        # Rank: the PRIMARY property first (its ontology binding, else the oracle
        # binding), then the value-type-gated CANDIDATE bindings (the P106 copula
        # path — ontology-typed first, then the empty-ontology fallback), then
        # sling. bindings[0] stays the primary property so every scalar reader and
        # the single-binding back-compat path are unchanged. The oracle binding is
        # always present so the primary property is verifiable even when its own
        # ontology was empty.
        ranked = [
            *primary_ontology_bindings,
            oracle_binding,
            *candidate_ontology_bindings,
            *candidate_bindings,
            *sling_bindings,
        ]

        # De-dup by (kb_namespace, kb_property) keeping the first (highest-rank)
        # occurrence — an ontology binding for the primary property supersedes
        # the plain oracle binding for the same P-id.
        seen: set = set()
        deduped: list[PredicateBinding] = []
        for b in ranked:
            key = (b.kb_namespace, b.kb_property)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(b)
        return deduped or [oracle_binding]

    @staticmethod
    def _row_to_metadata(row: sqlite3.Row) -> PredicateMetadata:
        def _parse_json(val: Optional[str]) -> Any:
            if val is None:
                return None
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return None

        # subject_entity_types / object_entity_types are later column
        # additions and may be absent from older DBs. sqlite3.Row raises IndexError when
        # the column is missing, so fall back to None defensively.
        try:
            subject_types = _parse_json(row["subject_entity_types"])
        except (IndexError, KeyError):
            subject_types = None
        try:
            object_types = _parse_json(row["object_entity_types"])
        except (IndexError, KeyError):
            object_types = None
        # v0.16.1 WS3b: premise_properties is a later column addition — absent
        # from older DBs (IndexError/KeyError) or NULL on rows without premises.
        try:
            premise_properties = _parse_json(row["premise_properties"])
        except (IndexError, KeyError):
            premise_properties = None

        # v0.16 WS1: the bindings JSON column is an M1 addition and may be
        # absent from older DBs (IndexError/KeyError) or NULL on legacy rows.
        # When present and non-empty, deserialize each element into a
        # PredicateBinding; else leave bindings empty so PredicateMetadata
        # __post_init__ read-synthesizes one binding from the scalar columns.
        # v0.16.3 Batch B: pinned is a later column addition — absent from older
        # DBs (IndexError/KeyError) defaults to unpinned, mirroring the
        # premise_properties defensive read above.
        try:
            pinned = bool(row["pinned"])
        except (IndexError, KeyError):
            pinned = False

        bindings: list[PredicateBinding] = []
        try:
            bindings_raw = _parse_json(row["bindings"])
        except (IndexError, KeyError):
            bindings_raw = None
        if bindings_raw:
            for b in bindings_raw:
                if not isinstance(b, dict):
                    continue
                bindings.append(
                    PredicateBinding(
                        kb_namespace=b.get("kb_namespace"),
                        kb_property=b.get("kb_property"),
                        slot_to_qualifier=b.get("slot_to_qualifier"),
                        single_valued=bool(b.get("single_valued", False)),
                        subject_entity_types=b.get("subject_entity_types"),
                        object_entity_types=b.get("object_entity_types"),
                        source=b.get("source", "legacy_scalar"),
                        rank=b.get("rank", 1.0),
                        value_type_gated=bool(b.get("value_type_gated", False)),
                    )
                )

        return PredicateMetadata(
            id=row["id"],
            aedos_predicate=row["aedos_predicate"],
            object_type=row["object_type"],
            user_subject_required=bool(row["user_subject_required"]),
            distinct_slots=_parse_json(row["distinct_slots"]),
            routing_hint=row["routing_hint"],
            kb_namespace=row["kb_namespace"],
            kb_property=row["kb_property"],
            slot_to_qualifier=_parse_json(row["slot_to_qualifier"]),
            reason=row["reason"],
            created_at=row["created_at"],
            last_consulted_at=row["last_consulted_at"],
            used_count=row["used_count"],
            retracted_at=row["retracted_at"],
            retraction_reason=row["retraction_reason"],
            single_valued=bool(row["single_valued"]),
            subject_entity_types=subject_types,
            object_entity_types=object_types,
            premise_properties=premise_properties,
            bindings=bindings,
            pinned=pinned,
        )
