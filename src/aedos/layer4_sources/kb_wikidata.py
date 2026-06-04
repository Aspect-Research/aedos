from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import httpx

from ..audit.log import log_event
from ..utils.rate_limit import RateLimiter
from .kb_protocol import (
    KBEntityID,
    KBPropertyID,
    LocalContext,
    ResolutionCandidate,
    Statement,
    SubsumptionResult,
    TransitivePathResult,
)


@dataclass
class WBSearchCandidate:
    """A raw wbsearchentities result row.

    Distinct from `ResolutionCandidate` (which is the KBProtocol-shaped
    score-wrapped form `KB.resolve_entity` returns). `WBSearchCandidate`
    preserves the API's full per-result payload — label, description,
    aliases, match metadata — so the normalizer's Stage C can drive
    the LLM with rich disambiguation context.

    `rank` is 1-based position in the API response (the API already
    ranks by its prominence/relevance algorithm).
    """

    qid: KBEntityID
    label: str
    description: Optional[str]
    aliases: list[str] = field(default_factory=list)
    match_type: str = ""        # "label" | "alias" | ""
    match_text: str = ""        # the literal string the API matched against
    rank: int = 0               # 1-based

_FIXTURE_DIR = Path(__file__).parent.parent.parent.parent / "tests" / "fixtures" / "wikidata"
_DEPRECATED_RANK = "http://wikiba.se/ontology#DeprecatedRank"

# Defaults used when no Config is wired (test paths that construct
# WikidataAdapter directly without a Config object). Production paths
# come through build_pipeline which passes a Config.
_DEFAULT_SEARCH_ENDPOINT = "https://www.wikidata.org/w/api.php"
_DEFAULT_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
_DEFAULT_CANDIDATE_POOL_SIZE = 30
# wbgetentities accepts up to 50 entity ids per call.
_DEFAULT_P31_BATCH_SIZE = 50
_DEFAULT_SPARQL_RATE = 5.0
_DEFAULT_SEARCH_RATE = 50.0
_DEFAULT_ENTITY_TTL_SECONDS = 3600
_DEFAULT_STATEMENT_TTL_SECONDS = 86400
_RETRY_BACKOFF_SECONDS = 1.0

# v0.16.1 WS9 (item 8 lever A): process-scoped positive-result memo for
# verify_transitive_path. Bounded by an LRU cap AND a TTL so a Wikidata edit
# cannot pin a stale answer for the whole process lifetime. The cap is sized
# for a Medium-Bar run's distinct (relation, source, target) fan-out; the TTL
# is short enough that a same-day edit is re-asked within the run. Behavior-
# NEUTRAL: the memo only ever returns what a fresh definite ASK would.
_TRANSITIVE_MEMO_MAX_ENTRIES = 4096
_TRANSITIVE_MEMO_TTL_SECONDS = 3600.0

# Wikidata identifier patterns. Used to validate inputs to SPARQL query
# construction — defense-in-depth against injection via Q/P-id parameters.
# All upstream sources (predicate translation oracle, entity resolver) produce
# canonical IDs, but validating at the SPARQL boundary prevents a future
# careless caller from constructing a malformed query.
_ENTITY_ID_PATTERN = re.compile(r"^Q\d+$")
_PROPERTY_ID_PATTERN = re.compile(r"^P\d+$")

# Default qualifier set requested in every _live_lookup SPARQL query.
# P580 (start time) and P582 (end time) are required for
# universal scope checking; P642 (of) appears in the most-common seed
# (holds_role / P39). Other qualifiers referenced by less-common predicates'
# slot_to_qualifier maps are not collected here — v0.16 captures the
# dynamic-discovery follow-up.
_DEFAULT_QUALIFIER_PROPS = ("P580", "P582", "P642")

# v0.16.1 WS5c: the temporal interval-qualifier keys, relocated here from CORE
# (walker's interval resolver). P580 = start time, P582 = end time. These are
# the qualifier P-ids `_live_lookup` populates on every `Statement.qualifiers`
# dict; CORE reads them via the `interval_qualifier_keys` accessor rather than
# baking the P-ids into the walker. (start_key, end_key) order is contractual.
_INTERVAL_START_QUALIFIER = "P580"  # start time
_INTERVAL_END_QUALIFIER = "P582"    # end time

# When the wbsearchentities post-filter eliminates
# all candidates AND expected_entity_types is non-empty, fall back to a SPARQL
# label-OR-altLabel search constrained by P31. Live validation surfaced that
# canonical entities (Q76 for "Obama", Q49112 for "Williams College") aren't
# in wbsearchentities' returned pool at any reasonable depth — the API
# under-serves short ambiguous queries. The SPARQL fallback uses Wikidata's
# label indexes directly and scopes by type, recovering the canonical entity.
def _build_label_type_search_query(
    reference: str, expected_types: list[KBEntityID], limit: int
) -> str:
    """Build the SPARQL fallback query: items whose rdfs:label OR skos:altLabel
    matches the reference exactly in English AND whose P31 is one of the
    expected_types. Escapes embedded quotes / backslashes in `reference` for
    safe SPARQL string-literal construction (defense-in-depth — the resolver's
    upstream callers pass user-provided strings here).

    The query is deliberately scoped:
      - rdfs:label catches the canonical English label.
      - skos:altLabel catches the "also known as" aliases (where Q76's "Obama"
        and Q49112's "Williams College" actually live).
      - P31 type constraint enforces the type filter even at this fallback.
    """
    if limit <= 0 or limit > 100:
        # Keep the LIMIT bounded — runaway SPARQL on a broad altLabel match
        # is expensive on WDQS and noisy downstream.
        raise ValueError(f"limit must be in (0, 100]; got {limit!r}")
    # Validate each expected type as a Q-id — defense-in-depth against a
    # caller injecting a SPARQL fragment via expected_entity_types.
    for qid in expected_types:
        if not _ENTITY_ID_PATTERN.match(qid):
            raise ValueError(f"Invalid expected_entity_type: {qid!r}")
    escaped = reference.replace("\\", "\\\\").replace('"', '\\"')
    values_clause = " ".join(f"wd:{q}" for q in expected_types)
    return (
        f"SELECT DISTINCT ?item ?itemLabel ?itemDescription WHERE {{\n"
        f"  VALUES ?type {{ {values_clause} }}\n"
        f"  {{ ?item rdfs:label \"{escaped}\"@en . }}\n"
        f"  UNION\n"
        f"  {{ ?item skos:altLabel \"{escaped}\"@en . }}\n"
        f"  ?item wdt:P31 ?type .\n"
        f"  SERVICE wikibase:label {{ bd:serviceParam wikibase:language \"en\". }}\n"
        f"}}\n"
        f"LIMIT {limit}"
    )


# Subsumption relation-type → SPARQL property-path alternation.
# Per architecture §9.1:
#   is_a    → P31 (instance of), P279 (subclass of)
#   part_of → P131 (located in admin entity), P361 (part of),
#             P30  (continent — for country/region → continent chains),
#             P206 (located in body of water — for landmark → river/ocean),
#             P17  (country — for city → country fallback when P131 chain
#                   doesn't reach the country directly).
#
# P30 lets the walker verify
# "France ⊂ Europe" directly (France P30 Europe) and chains through
# P131 → P30 to verify city-to-continent ("Paris ⊂ Europe" via
# Paris P131 Île-de-France → ... → France P30 Europe). Without P30,
# part_of stopped at P131/P361 which deepest-runs out at the country
# level; many country→continent statements in Wikidata are only
# expressed via P30, so the chain truncated and the walker abstained
# on multi-hop geographic claims.
_SUBSUMPTION_PROPERTIES = {
    "is_a": ("P31", "P279"),
    # Trimmed alternation to
    # (P131, P30, P17). An earlier, fuller set
    # included P361 (part of) and P206 (located in body of water),
    # both of which surfaced false-subsumption paths:
    #
    #   subsumption(Warsaw, Germany, "part_of") → a_subsumed_by_b
    #     via Warsaw P206 Vistula → Vistula P17 Germany (the river's
    #     historical/regional country listing) or via Warsaw P361
    #     historical-Prussia → Germany.
    #
    # The kb_verifier's subsumption-upgrade path then verified the
    # false claim "Marie Curie was born in Germany" (KB Curie P19 =
    # Warsaw; Warsaw subsumed by Germany via the leaky path).
    #
    # The trimmed (P131, P30, P17) alternation preserves all the
    # canonical geographic-containment chains the medium-bar wins
    # required:
    #   Amazon River  ⊂ South America (via P17 country → P30 continent)
    #   Paris         ⊂ Europe        (via P131 → P30)
    #   Eiffel Tower  ⊂ Europe        (via P131 → P131 → P30)
    #   Warsaw        ⊂ Poland        (P131 chain)
    #   Honolulu      ⊂ United States (P131 chain)
    # AND it eliminates the leaky paths:
    #   Warsaw ⊂ Germany → unrelated   (no historical/water path)
    #   Rome   ⊂ Germany → unrelated   (Italy/Germany still resolve to
    #                                   same continent via P30 for the
    #                                   shared-continent DISJOINT path)
    #
    # P206 / P361 remain available for explicit type-and-relation
    # queries elsewhere (e.g. the substrate subsumption table); they
    # just don't participate in the kb_verifier's geographic
    # `part_of` transitive closure.
    "part_of": ("P131", "P30", "P17"),
}

# Default property set for KB neighbor enumeration. The
# geographic/taxonomic core covers the dominant derivation_corpus
# multi-hop shapes (X lives_in Y / part_of Z; X is_a Y / kind-properties).
# Other properties (P50 author, P108 employer, P39 position) are deferred
# to v0.16.
_DEFAULT_NEIGHBOR_PROPERTIES = ("P31", "P279", "P361", "P131", "P17")

# v0.16.1 WS5c: per-relation KB neighbor property set, relocated here from CORE
# (walker._D5_NEIGHBOR_PROPS_BY_RELATION). The walker passes the opaque
# relation_type ("is_a"/"part_of") and the adapter resolves the Wikidata P-ids,
# so no P-id naming survives above the seam. Mirrors `_SUBSUMPTION_PROPERTIES`
# for is_a/part_of, plus P17 (country) and P361 (part of) on part_of for
# country-/region-level grounding (e.g. Williams College P17 → United States;
# useful for "X is in the United States" when the substrate's subsumption oracle
# is cold). The tuples are byte-identical to the walker's former table so
# neighbor enumeration is behavior-neutral.
_NEIGHBOR_PROPERTIES_BY_RELATION: dict[str, tuple[str, ...]] = {
    "is_a": ("P31", "P279"),
    "part_of": ("P131", "P361", "P17"),
}


# Reverse enumeration's LIMIT. Unbounded properties like
# P17=Q30 (country=USA) have millions of subjects; the walker only needs
# a sample of candidate children for its substitution. 20 is the
# conservative default after a diagnostic showed the walker
# fanning out catastrophically at LIMIT=100 (per-case wall-clock blew
# past 18 min on der_multihop_002 before the run was killed): with
# LIMIT=100 a single reverse call returns 100 candidate children, the
# walker substitutes each into a new claim at depth+1, and each
# substituted claim fires more KB calls + more enumeration. 20 keeps
# fanout per call to ~20× rather than 100×, while still surfacing common
# children. Paired with the walker.py depth==0 cap on KB enumeration
# fallback to bound aggregate walker cost.
_DEFAULT_NEIGHBOR_REVERSE_LIMIT = 20


def _build_neighbors_query(
    entity: KBEntityID,
    properties: tuple[KBPropertyID, ...],
    direction: str = "outgoing",
    limit: int = _DEFAULT_NEIGHBOR_REVERSE_LIMIT,
) -> str:
    """Build a single SPARQL query that enumerates the given entity's
    neighbors along all `properties` in one round-trip.

    `direction`:
      - "outgoing" (default): `wd:E ?prop ?value` — yields
        E's parents (entities E points to). No LIMIT (outgoing edges
        are bounded by the predicate's schema; a few hits per property).
      - "incoming": `?value ?prop wd:E` — yields E's
        children (entities pointing to E). LIMIT'd to bound the query
        cost on unbounded properties (P17=Q30 has millions of subjects).

    Returns SELECT bindings shaped `?prop` (the P-id URI) and `?value`
    (the neighbor entity URI). `_parse_neighbors_bindings` converts to
    `{P-id: [Q-id, ...]}`. Direct-property (`wdt:`) only — qualifiers
    and statement-rank don't matter for neighbor enumeration; the
    walker uses these as candidate premises only, and a deprecated-rank
    neighbor that resolves to a real entity is still a valid premise
    for the walker to consider.

    Defense-in-depth: validates entity, every property id, and the
    direction string (the callers — `_live_neighbors` and walker
    integration — pass canonical IDs, but the SPARQL boundary stays
    clean).
    """
    if not _ENTITY_ID_PATTERN.match(entity):
        raise ValueError(f"Invalid Wikidata entity ID: {entity!r}")
    if not properties:
        raise ValueError("properties must be a non-empty sequence")
    for prop in properties:
        if not _PROPERTY_ID_PATTERN.match(prop):
            raise ValueError(f"Invalid Wikidata property ID: {prop!r}")
    if direction not in ("outgoing", "incoming"):
        raise ValueError(
            f"direction must be 'outgoing' or 'incoming'; got {direction!r}"
        )
    values_clause = " ".join(f"wdt:{p}" for p in properties)
    if direction == "outgoing":
        # E → neighbor (no LIMIT; outgoing fanout is naturally bounded).
        return (
            f"SELECT ?prop ?value WHERE {{\n"
            f"  VALUES ?prop {{ {values_clause} }}\n"
            f"  wd:{entity} ?prop ?value .\n"
            f"  FILTER(isIRI(?value))\n"
            f"}}"
        )
    # incoming: neighbor → E. LIMIT bounds unbounded properties.
    if limit <= 0 or limit > 1000:
        raise ValueError(f"limit must be in (0, 1000]; got {limit!r}")
    return (
        f"SELECT ?prop ?value WHERE {{\n"
        f"  VALUES ?prop {{ {values_clause} }}\n"
        f"  ?value ?prop wd:{entity} .\n"
        f"  FILTER(isIRI(?value))\n"
        f"}}\n"
        f"LIMIT {limit}"
    )


def _parse_neighbors_bindings(
    bindings: list, properties: tuple[KBPropertyID, ...]
) -> dict[KBPropertyID, list[KBEntityID]]:
    """Convert SPARQL neighbor-query bindings to `{P-id: [Q-id, ...]}`.

    Every requested property appears in the result dict with at least an
    empty list — downstream callers (the walker) can iterate the property
    list deterministically without `KeyError`s. Out-of-set properties
    that somehow appear in bindings are ignored (defense-in-depth
    against a future query change).
    """
    result: dict[KBPropertyID, list[KBEntityID]] = {p: [] for p in properties}
    allowed = set(properties)
    for row in bindings:
        prop_uri = row.get("prop", {}).get("value", "")
        value_uri = row.get("value", {}).get("value", "")
        if not prop_uri or not value_uri:
            continue
        # URI shape: http://www.wikidata.org/prop/direct/P131
        prop_id = prop_uri.rsplit("/", 1)[-1]
        if prop_id not in allowed:
            continue
        value_id = _extract_entity_id(value_uri)
        if not value_id or not _ENTITY_ID_PATTERN.match(value_id):
            continue
        if value_id not in result[prop_id]:  # dedupe within property
            result[prop_id].append(value_id)
    return result


# Geographic-region entity types (exact Wikidata P31) for the type-guarded
# P361 bridge in part_of subsumption — see _build_subsumption_ask_query. P361
# ("part of") cleanly expresses CURRENT region containment between these types
# (US state ⊂ region, country ⊂ supranational region), but is also used for
# organizational membership, historical/former containment, and water-body
# adjacency — the leaky paths the part_of alternation trim removed wholesale. Restoring P361
# only between two of these region types reopens region containment without
# the Marie-Curie-class false-verify. Exact P31 (not P31/P279*) is deliberate:
# it cannot admit a settlement via a subclass chain (a city is Q515, never a
# region type), keeping the leak provably closed. The set is grounded in the
# observed P31 of the medium-bar's region entities (Massachusetts Q35657,
# New England Q518261/Q123615496, Europe Q82794, countries Q6256).
_GEO_REGION_TYPES: tuple[str, ...] = (
    "Q5107",       # continent
    "Q6256",       # country
    "Q3624078",    # sovereign state
    "Q82794",      # geographic region
    "Q35657",      # state of the United States
    "Q107390",     # federal state
    "Q10864048",   # first-level administrative country subdivision
    "Q518261",     # region of the United States (e.g. New England)
    "Q123615496",  # region type observed on New England
)
_PART_OF_BRIDGE_PROPERTY = "P361"  # geographic "part of", type-guarded above


# v0.16.1 WS5a: the geographic predicate cluster, relocated here from CORE
# (kb_verifier) behind the kb_protocol seam. The closed seven-continent set and
# the geographic P-ids are genuine Wikidata facts and belong inside the adapter.
#
# Canonical Q-ids for the seven continents + the principal supercontinent
# groupings the medium-bar test set uses. Used by `geographic_disjoint` to
# confidently flag KB-grounded "X is in [wrong continent]" claims as
# CONTRADICTED rather than abstaining on the non-functional-predicate NO_MATCH
# path. Hand-validated against Wikidata's canonical labels:
#   Europe Q46, Asia Q48, Africa Q15, North America Q49, South America Q18,
#   Oceania Q55643, Antarctica Q51, Australia Q3960 (continent; country is Q408).
_CONTINENT_QIDS: frozenset[str] = frozenset([
    "Q46", "Q48", "Q15", "Q49", "Q18", "Q55643", "Q51", "Q3960",
])

# KB properties whose semantics are GEOGRAPHIC location-containment, for which
# the location-disjoint check is sound. Non-geographic relational predicates
# (employed_by P108, member_of P463, child_of P40, ...) must NOT use the
# disjoint check — two distinct entities can both satisfy a multi-valued
# relational predicate without contradicting each other.
_LOCATION_KB_PROPERTIES: frozenset[str] = frozenset([
    "P131",  # located in the administrative territorial entity
    "P17",   # country
    "P30",   # continent
    "P361",  # part of (used for geographic part_of)
    "P206",  # located in body of water
    "P276",  # location
])

# Geographic-container entity types that the per-predicate object-type lists
# (e.g. located_in = [country, city, settlement, ...]) historically omit. A
# claim like "Paris is in Europe" needs to resolve "Europe" to the continent
# (Q46, instance-of continent Q5107); without continent in the accepted-type
# set the resolver's type filter rejects Q46 and lands on a non-continent
# homonym, so the containment subsumption (France subset Europe via P30) can
# never match. CORE widens the object's type filter with these only for
# geographic-location predicates. Continents are a closed 7-member set, so
# admitting them cannot over-broaden resolution. (Region Q82794 is deliberately
# NOT included — it is open-ended and the cases that would need it also need the
# trimmed P361 subsumption path.)
_GEO_CONTAINER_TYPES: frozenset[str] = frozenset([
    "Q5107",  # continent
])

# v0.16.1 cycle-2: geographic-PLACE classes that gate the shared-continent
# sub-region disjoint path (path b in `_geographic_disjoint`). The expected
# object of a "X located_in Y" / "X part_of Y" claim must be a CONFIRMED
# geographic place before path b may contradict — otherwise a non-geographic
# object that merely sits on a continent (a supranational union, an
# organization, a consortium) is mis-classified as a geographically disjoint
# sub-region and the verifier FALSE-CONTRADICTS a TRUE claim.
#
# Two false-contradicts shared this root cause:
#   - "Germany located_in the European Union" — the EU (Q458) carries P30=Europe
#     so it shares Germany's continent under the part_of alternation, yet
#     Germany↔EU is `unrelated` under P131/P30/P17 (EU membership is P463, which
#     the alternation cannot see), so path b flagged them disjoint.
#   - "Williams College part_of the Consortium of Liberal Arts Colleges" — P361
#     is a location property, so an ORGANIZATIONAL part_of reached path b with a
#     consortium object.
#
# The set is grounded in a live Wikidata P31/P279* probe of the medium-bar geo
# entities. It admits countries, sovereign states, continents, rivers, bodies
# of water, and natural geographic objects, and EXCLUDES the EU / Williams /
# consortium (each False on every member):
#   country Q6256, sovereign state Q3624078:  Germany/France/Vatican=True, EU=False
#   continent Q5107:                           Europe/Asia/Africa=True
#   body of water Q15324, river Q4022:         Thames=True
#   natural geographic object Q35145263:       continents/physical features=True
#
# Deliberately EXCLUDES the broad "geographic region" (Q82794) and
# "human-geographic territorial entity" (Q15642541) classes: the probe showed
# BOTH subsume the EU (it is genuinely a human-geographic/region entity in
# Wikidata's loose ontology), so admitting them would reopen the
# Germany-in-EU false-contradict. Sub-national regions (US states, Italian
# regions) as the EXPECTED container also fall outside this set; the gate then
# fails closed -> abstain, the §3.2-safe outcome (no current pin needs a
# sub-national region as a path-b container).
_GEO_PLACE_CLASSES: tuple[str, ...] = (
    "Q6256",      # country
    "Q3624078",   # sovereign state
    "Q5107",      # continent
    "Q15324",     # body of water
    "Q4022",      # river
    "Q35145263",  # natural geographic object
)


def _is_confirmed_geographic_place(subsumption_fn, value: str) -> bool:
    """v0.16.1 cycle-2: True when `value`'s instance-of/subclass-of chain is
    subsumption-confirmed (is_a: P31|P279) to reach a geographic-PLACE class
    (`_GEO_PLACE_CLASSES`). Drives the path-b object gate in
    `_geographic_disjoint`.

    Reuses the KBProtocol `subsumption` callable (`is_a` relation) so the
    live/fixture path and SubsumptionResult shape are unchanged — the ASK is
    `value (wdt:P31|wdt:P279)+ <place_class>`, which yields
    `a_subsumed_by_b`/`equivalent` exactly when `value` is an instance of (a
    subclass of) the place class.

    FAILS CLOSED: a non-string value, a subsumption error, or no positive
    verdict against ANY place class returns False — so an unconfirmed /
    organization / union / consortium object can never satisfy the gate, and
    path b abstains rather than false-contradicting. §3.2."""
    if not isinstance(value, str) or not value:
        return False
    for place_class in _GEO_PLACE_CLASSES:
        try:
            r = subsumption_fn(value, place_class, "is_a")
        except Exception:
            continue
        if r.verdict in ("a_subsumed_by_b", "equivalent"):
            return True
    return False


def _geographic_disjoint(subsumption_fn, kb_value: str, expected_value: str) -> bool:
    """v0.16.1 WS5a: relocated from `kb_verifier._location_disjoint`. True when
    KB confirms the KB statement value is geographically disjoint from the
    claim's expected value. `subsumption_fn(a, b, relation_type)` is the
    KBProtocol `subsumption` callable (the adapter passes its own bound method,
    so the live/fixture path and the SubsumptionResult shape are unchanged).

    Two paths, both requiring positive KB evidence (a continent ancestor
    confirmed by subsumption):

    (a) Continent-level. expected_value is itself a known continent
        (_CONTINENT_QIDS) and the KB value is subsumed by a DIFFERENT continent.
        Direct evidence of disjoint continent. Targets "Thames in Asia" /
        "Vatican in Africa".

    (b) Shared-continent sub-region. Both values are subsumed by the SAME
        continent AND subsumption is `unrelated` in both directions between
        them. Two sub-regions sharing a continent ancestor with no mutual
        containment are structurally disjoint within that continent (Italy and
        Germany are both in Europe; neither contains the other; therefore
        they're disjoint countries). Targets "Rome in Germany" — Rome's P131 =
        Lazio (a sub-region of Italy), Italy's continent is Europe, Germany's
        continent is Europe, and Lazio is unrelated to Germany in both
        subsumption directions.

        v0.16.1 cycle-2 GATE: path b additionally requires the EXPECTED object
        (`expected_value`) to be a subsumption-confirmed geographic PLACE
        (`_is_confirmed_geographic_place`). Without this, a non-geographic
        object that merely sits on a continent under the part_of alternation —
        a supranational union (the EU carries P30=Europe), an organization, a
        consortium — is mis-read as a disjoint sub-region and a TRUE membership
        claim is FALSE-CONTRADICTED ("Germany located_in the EU",
        "Williams College part_of the Consortium"). The gate fails closed: an
        object whose geographic-place membership is not confirmed yields no
        disjoint -> the verifier abstains (NO_MATCH). Path a (continent-level)
        is inherently geographic and does NOT need the gate.

    Fails closed on error: any uncertainty preserves NO_MATCH (abstain). §3.2
    soundness-over-completeness.
    """
    if not isinstance(kb_value, str) or not isinstance(expected_value, str):
        return False
    if kb_value == expected_value:
        return False

    # (a) Continent-level path
    if expected_value in _CONTINENT_QIDS:
        for continent in _CONTINENT_QIDS:
            if continent == expected_value:
                continue
            try:
                r = subsumption_fn(kb_value, continent, "part_of")
            except Exception:
                continue
            if r.verdict in ("a_subsumed_by_b", "equivalent"):
                return True
        return False

    # (b) Shared-continent sub-region path.
    # GATE (v0.16.1 cycle-2): the expected object must be a confirmed geographic
    # PLACE. A union / organization / consortium that merely shares a continent
    # under the part_of alternation is NOT a disjoint sub-region — fail closed
    # (abstain) rather than false-contradict a true membership claim.
    if not _is_confirmed_geographic_place(subsumption_fn, expected_value):
        return False
    for continent in _CONTINENT_QIDS:
        try:
            kb_in = subsumption_fn(kb_value, continent, "part_of").verdict
            exp_in = subsumption_fn(expected_value, continent, "part_of").verdict
        except Exception:
            continue
        kb_in_ok = kb_in in ("a_subsumed_by_b", "equivalent")
        exp_in_ok = exp_in in ("a_subsumed_by_b", "equivalent")
        if not (kb_in_ok and exp_in_ok):
            continue
        # Both confirmed in the same continent; check mutual non-containment
        try:
            fwd = subsumption_fn(kb_value, expected_value, "part_of").verdict
            rev = subsumption_fn(expected_value, kb_value, "part_of").verdict
        except Exception:
            return False
        return fwd == "unrelated" and rev == "unrelated"
    return False


def _build_transitive_ask_query(
    source: KBEntityID,
    target: KBEntityID,
    properties: tuple[KBPropertyID, ...],
    use_part_of_bridge: bool,
) -> str:
    """v0.16 WS2 §1: build an ASK query asking whether `source` reaches
    `target` via a transitive `(wdt:P)+` path over `properties`. Path-existence
    only — ASK short-circuits on first match.

    Two shapes (extracted from the former `_build_subsumption_ask_query` body
    so the verifier's subsumption ASK and the walker's transitive-path check
    share ONE construction):
      - `use_part_of_bridge=False`: a plain alternation path
        `ASK {{ wd:source (wdt:P1|wdt:P2|...)+ wd:target . }}`. This serves
        both the is_a relation alternation (P31|P279) and any single-property
        generic transitive check (e.g. P171 parent-taxon).
      - `use_part_of_bridge=True`: the safe alternation (P131/P30/P17) augmented
        with a single TYPE-GUARDED P361 bridge — a P361 edge participates only
        between two geographic-region-typed nodes (_GEO_REGION_TYPES). This
        admits region containment (Massachusetts ⊂ New England) without
        admitting the leaky city/historical P361 paths
        (Warsaw ⊄ Germany). The bridge logic is preserved exactly;
        do not edit without re-pinning Warsaw⊄Germany.

    Defense-in-depth: validates source/target via `_ENTITY_ID_PATTERN` and
    every property via `_PROPERTY_ID_PATTERN` at the SPARQL boundary."""
    if not _ENTITY_ID_PATTERN.match(source):
        raise ValueError(f"Invalid Wikidata entity ID: {source!r}")
    if not _ENTITY_ID_PATTERN.match(target):
        raise ValueError(f"Invalid Wikidata entity ID: {target!r}")
    if not properties:
        raise ValueError("properties must be a non-empty sequence")
    for prop in properties:
        if not _PROPERTY_ID_PATTERN.match(prop):
            raise ValueError(f"Invalid Wikidata property ID: {prop!r}")
    path = "|".join(f"wdt:{p}" for p in properties)
    if not use_part_of_bridge:
        return f"ASK {{ wd:{source} ({path})+ wd:{target} . }}"
    region_values = " ".join(f"wd:{t}" for t in _GEO_REGION_TYPES)
    bp = _PART_OF_BRIDGE_PROPERTY
    # The bridge binds `target` as the P361 object (region containment is one
    # P361 hop TO the region — Massachusetts P361 New England). This keeps the
    # only unbounded traversal as `source (safe)* ?r1`, which is small (a
    # place's geographic ancestry); a free `?r2 (safe)* target` tail explodes
    # on Wikidata's endpoint and times out. The safe branch is listed first so
    # an existing P131/P30/P17 path matches without touching the bridge.
    return (
        f"ASK {{\n"
        f"  {{ wd:{source} ({path})+ wd:{target} . }}\n"
        f"  UNION\n"
        f"  {{ wd:{source} ({path})* ?r1 . ?r1 wdt:{bp} wd:{target} .\n"
        f"    ?r1 wdt:P31 ?gt1 . wd:{target} wdt:P31 ?gt2 .\n"
        f"    VALUES ?gt1 {{ {region_values} }} VALUES ?gt2 {{ {region_values} }} }}\n"
        f"}}"
    )


def _build_subsumption_ask_query(
    source: KBEntityID, target: KBEntityID, relation_type: str
) -> str:
    """Build an ASK query: does `source` reach `target` via the
    relation_type's property alternation? Path-existence only — fast,
    short-circuits on first match. Used per direction by `_live_subsumption`.

    v0.16 WS2 §1: delegates to `_build_transitive_ask_query` (which holds the
    path-construction + the type-guarded P361 bridge); output is byte-identical
    for both the is_a and part_of cases."""
    props = _SUBSUMPTION_PROPERTIES.get(relation_type)
    if props is None:
        raise ValueError(
            f"Unsupported relation_type: {relation_type!r} "
            f"(expected one of {sorted(_SUBSUMPTION_PROPERTIES)})"
        )
    return _build_transitive_ask_query(
        source, target, props, use_part_of_bridge=(relation_type == "part_of")
    )


def _build_establishing_property_query(
    source: KBEntityID, target: KBEntityID, relation_type: str
) -> str:
    """Follow-up query (operator Q2 confirmed): when subsumption holds,
    identify the immediate (depth-1) property that anchors the chain.
    Useful for trace inspection and v0.16's potential subsumption
    confidence refinement."""
    props = _SUBSUMPTION_PROPERTIES[relation_type]
    rest = "|".join(f"wdt:{p}" for p in props)
    values = " ".join(f"wdt:{p}" for p in props)
    return (
        f"SELECT ?prop WHERE {{\n"
        f"  VALUES ?prop {{ {values} }}\n"
        f"  wd:{source} ?prop ?intermediate .\n"
        f"  ?intermediate ({rest})* wd:{target} .\n"
        f"}}\n"
        f"LIMIT 1"
    )


# v0.16 WS1: Wikidata constraint/relation property IDs used by
# `_build_property_ontology_query`. P2302 is "property constraint"; the
# constraint *kind* is a qualifier (pq:P2306 -> constraint-type Q-id) and the
# constrained classes ride pq:P2308. P1647/P1696/P1659 are direct relations.
_ONTOLOGY_CONSTRAINT_PROPERTY = "P2302"            # property constraint
_ONTOLOGY_CONSTRAINT_TYPE_QUALIFIER = "P2308"      # class (the constrained type)
_ONTOLOGY_SUBJECT_TYPE_CONSTRAINT = "Q21503250"    # subject-type constraint
_ONTOLOGY_VALUE_TYPE_CONSTRAINT = "Q21510865"      # value-type constraint
_ONTOLOGY_SINGLE_VALUE_CONSTRAINT = "Q19474404"    # single-value constraint
_ONTOLOGY_SUBPROPERTY_OF = "P1647"                 # subproperty of
_ONTOLOGY_INVERSE_PROPERTY = "P1696"               # inverse property
_ONTOLOGY_RELATED_PROPERTY = "P1659"               # related property


def _build_property_ontology_query(prop: KBPropertyID) -> str:
    """v0.16 WS1: P2302 constraint statements + P1647/P1696/P1659 relations
    for `prop`, in a single SELECT round-trip.

    Pulls four things the substrate uses to BUILD `PredicateBinding`s and to
    discover sibling/inverse properties:
      - subject-type constraint (P2302 / Q21503250): the constrained
        subject classes ride pq:P2308.
      - value-type constraint (P2302 / Q21510865): the constrained value
        classes ride pq:P2308.
      - single-value constraint (P2302 / Q19474404): presence flags the
        property as functional.
      - P1647 (subproperty of), P1696 (inverse property), P1659 (related
        property): sibling/inverse properties.

    Result columns: ?constraintKind (the constraint-type Q-id), ?cls (the
    constrained class Q-id), ?subProp, ?invProp, ?relProp. A property has at
    most a handful of constraint statements, so the cross-product is small.
    `_parse_property_ontology_bindings` collapses the rows into a structured
    dict; constraint rows without a P2308 class (single-value) still carry
    ?constraintKind.

    Defense-in-depth: validates the P-id before interpolation (the SPARQL
    boundary stays clean even if a careless caller passes a raw string)."""
    if not _PROPERTY_ID_PATTERN.match(prop):
        raise ValueError(f"Invalid Wikidata property ID: {prop!r}")
    return (
        f"SELECT ?constraintKind ?cls ?subProp ?invProp ?relProp WHERE {{\n"
        f"  {{\n"
        f"    wd:{prop} p:{_ONTOLOGY_CONSTRAINT_PROPERTY} ?cstmt .\n"
        f"    ?cstmt ps:{_ONTOLOGY_CONSTRAINT_PROPERTY} ?constraintKind .\n"
        f"    OPTIONAL {{ ?cstmt pq:{_ONTOLOGY_CONSTRAINT_TYPE_QUALIFIER} ?cls . }}\n"
        f"  }} UNION {{\n"
        f"    wd:{prop} wdt:{_ONTOLOGY_SUBPROPERTY_OF} ?subProp .\n"
        f"  }} UNION {{\n"
        f"    wd:{prop} wdt:{_ONTOLOGY_INVERSE_PROPERTY} ?invProp .\n"
        f"  }} UNION {{\n"
        f"    wd:{prop} wdt:{_ONTOLOGY_RELATED_PROPERTY} ?relProp .\n"
        f"  }}\n"
        f"}}"
    )


def _empty_property_ontology() -> dict:
    """The fail-open / miss result for `fetch_property_ontology`: all-empty
    lists, single_valued=False. The discovery flow treats this as 'the
    ontology cannot constrain this property' and falls back to the oracle's
    primary binding (and, optionally, SLING)."""
    return {
        "subject_type_qids": [],
        "value_type_qids": [],
        "inverse_pids": [],
        "subproperty_pids": [],
        "related_pids": [],
        "single_valued": False,
    }


def _parse_property_ontology_bindings(bindings: list) -> dict:
    """Collapse `_build_property_ontology_query` SELECT rows into the dict
    `WikidataAdapter.fetch_property_ontology` returns (and
    `PropertyRelations.fetch` caches). Shape:

        {
          "subject_type_qids": [...],   # value-of-class constraints (subject)
          "value_type_qids": [...],     # value-of-class constraints (value)
          "inverse_pids": [...],
          "subproperty_pids": [...],
          "related_pids": [...],
          "single_valued": bool,
        }

    Defensive: ignores rows whose URIs don't resolve to Q/P-ids and de-dupes.
    Never raises on a malformed binding row (fail-open contract)."""
    subject_type_qids: list[str] = []
    value_type_qids: list[str] = []
    inverse_pids: list[str] = []
    subproperty_pids: list[str] = []
    related_pids: list[str] = []
    single_valued = False

    for row in bindings:
        if not isinstance(row, dict):
            continue
        kind_uri = row.get("constraintKind", {}).get("value", "")
        kind_qid = _extract_entity_id(kind_uri) if kind_uri else ""
        cls_uri = row.get("cls", {}).get("value", "")
        cls_qid = _extract_entity_id(cls_uri) if cls_uri else ""
        if kind_qid:
            if kind_qid == _ONTOLOGY_SINGLE_VALUE_CONSTRAINT:
                single_valued = True
            elif kind_qid == _ONTOLOGY_SUBJECT_TYPE_CONSTRAINT and _ENTITY_ID_PATTERN.match(cls_qid):
                if cls_qid not in subject_type_qids:
                    subject_type_qids.append(cls_qid)
            elif kind_qid == _ONTOLOGY_VALUE_TYPE_CONSTRAINT and _ENTITY_ID_PATTERN.match(cls_qid):
                if cls_qid not in value_type_qids:
                    value_type_qids.append(cls_qid)

        for col, bucket, pattern in (
            ("subProp", subproperty_pids, _PROPERTY_ID_PATTERN),
            ("invProp", inverse_pids, _PROPERTY_ID_PATTERN),
            ("relProp", related_pids, _PROPERTY_ID_PATTERN),
        ):
            uri = row.get(col, {}).get("value", "")
            pid = _extract_entity_id(uri) if uri else ""
            if pid and pattern.match(pid) and pid not in bucket:
                bucket.append(pid)

    return {
        "subject_type_qids": subject_type_qids,
        "value_type_qids": value_type_qids,
        "inverse_pids": inverse_pids,
        "subproperty_pids": subproperty_pids,
        "related_pids": related_pids,
        "single_valued": single_valued,
    }


def _build_lookup_query(entity: KBEntityID, predicate: KBPropertyID) -> str:
    """Build a SPARQL query for `lookup_statements`.

    Returns SELECT bindings whose shape matches what `_parse_statement_bindings`
    consumes — value, valueType (synthesized via BIND), rank, and the default
    qualifier projection. Deprecated-rank statements are filtered server-side
    (the parser also defense-skips them).
    """
    if not _ENTITY_ID_PATTERN.match(entity):
        raise ValueError(f"Invalid Wikidata entity ID: {entity!r}")
    if not _PROPERTY_ID_PATTERN.match(predicate):
        raise ValueError(f"Invalid Wikidata property ID: {predicate!r}")

    qual_select = " ".join(f"?qual_{p}" for p in _DEFAULT_QUALIFIER_PROPS)
    qual_optional = "\n  ".join(
        f"OPTIONAL {{ ?statement pq:{p} ?qual_{p} . }}"
        for p in _DEFAULT_QUALIFIER_PROPS
    )
    return (
        f"SELECT ?value ?valueType ?rank {qual_select}\n"
        f"WHERE {{\n"
        f"  wd:{entity} p:{predicate} ?statement .\n"
        f"  ?statement ps:{predicate} ?value .\n"
        f"  ?statement wikibase:rank ?rank .\n"
        f"  FILTER (?rank != wikibase:DeprecatedRank)\n"
        f"  {qual_optional}\n"
        f'  BIND(IF(isURI(?value), "entity", "literal") AS ?valueType)\n'
        f"}}"
    )


def _build_property_example_query(predicate: KBPropertyID, limit: int) -> str:
    """v0.16.3 Batch B (piece 1): SPARQL to SOURCE known-true (subject, value)
    example statements for a bare property — `?s wdt:P ?v` — used by the
    DirectionValidator to empirically confirm which KB role each Aedos slot maps
    to. ?s is the KB statement-subject by construction; ?v the value. Restricted
    to entity-valued statements (FILTER isIRI) since direction-probing only
    applies to entity-typed predicates. A SMALL LIMIT keeps WDQS from scanning a
    high-cardinality property (the same fanout caution as the neighbor query)."""
    if not _PROPERTY_ID_PATTERN.match(predicate):
        raise ValueError(f"Invalid Wikidata property ID: {predicate!r}")
    if not isinstance(limit, int) or not (0 < limit <= 50):
        raise ValueError(f"limit must be in (0, 50], got {limit!r}")
    return (
        f"SELECT ?s ?v WHERE {{\n"
        f"  ?s wdt:{predicate} ?v .\n"
        f"  FILTER(isIRI(?v))\n"
        f"}} LIMIT {limit}"
    )


def _subsumption_verdict(a_to_b: bool, b_to_a: bool) -> str:
    """Map two directional ASK results to the SubsumptionResult verdict
    string. Architecture §6.2 enumerates the four verdicts."""
    if a_to_b and b_to_a:
        return "equivalent"
    if a_to_b:
        return "a_subsumed_by_b"
    if b_to_a:
        return "b_subsumed_by_a"
    return "unrelated"


def _parse_property_example_bindings(
    bindings: list, limit: int
) -> list[tuple[KBEntityID, KBEntityID]]:
    """v0.16.3 Batch B: extract (subject_qid, value_qid) pairs from a `?s ?v`
    SPARQL result. Only well-formed entity Q-ids are kept; malformed/literal rows
    are skipped (fail-open). De-duped, capped at `limit`."""
    pairs: list[tuple[KBEntityID, KBEntityID]] = []
    seen: set = set()
    for row in bindings:
        s_raw = row.get("s", {}).get("value", "")
        v_raw = row.get("v", {}).get("value", "")
        s_qid = _extract_entity_id(s_raw) if s_raw.startswith(
            "http://www.wikidata.org/entity/"
        ) else s_raw
        v_qid = _extract_entity_id(v_raw) if v_raw.startswith(
            "http://www.wikidata.org/entity/"
        ) else v_raw
        if not (_ENTITY_ID_PATTERN.match(s_qid or "") and _ENTITY_ID_PATTERN.match(v_qid or "")):
            continue
        key = (s_qid, v_qid)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
        if len(pairs) >= limit:
            break
    return pairs


def _parse_statement_bindings(
    bindings: list, entity: KBEntityID, predicate: KBPropertyID, provenance: dict
) -> list[Statement]:
    """Parse SPARQL JSON bindings into Aedos `Statement` objects.

    Shared between fixture and live paths. The `provenance` dict is attached
    to each emitted Statement — typically `{"fixture": name, ...}` for the
    fixture path or `{"source": "live", ...}` for the live path.

    Deprecated-rank rows are skipped (defense-in-depth — the live SPARQL
    query also FILTERs them, and the fixture data may omit the filter).
    """
    statements: list[Statement] = []
    for row in bindings:
        rank_raw = row.get("rank", {}).get("value", "")
        if _DEPRECATED_RANK in rank_raw:
            continue
        rank = _rank_label(rank_raw)

        value_node = row.get("value", {})
        raw_value = value_node.get("value", "")
        value_type = row.get("valueType", {}).get("value", "entity")

        # For entity URIs, extract the Q-number
        if value_type == "entity" and raw_value.startswith("http://www.wikidata.org/entity/"):
            value = _extract_entity_id(raw_value)
        else:
            value = raw_value

        # Collect qualifiers (any key starting with "qual_")
        qualifiers: dict = {}
        for key, node in row.items():
            if key.startswith("qual_"):
                prop = key[5:]  # strip "qual_" prefix
                raw = node.get("value", "")
                # Convert time values
                if "dateTime" in node.get("datatype", "") or re.match(r"\+?\d{4}-\d{2}-\d{2}", raw):
                    qualifiers[prop] = _parse_time_value(raw)
                else:
                    qualifiers[prop] = raw

        statements.append(
            Statement(
                value=value,
                value_type=value_type,
                qualifiers=qualifiers,
                rank=rank,
                provenance=provenance,
            )
        )
    return statements


class FixtureNotFoundError(Exception):
    pass


def _fixture_path(name: str) -> Path:
    p = _FIXTURE_DIR / name
    if not p.exists():
        raise FixtureNotFoundError(f"Fixture not found: {p}")
    return p


def _load_fixture(name: str) -> dict:
    return json.loads(_fixture_path(name).read_text(encoding="utf-8"))


def _normalize_search_term(reference: str) -> str:
    term = reference.lower().strip()
    term = re.sub(r"\s+", "_", term)
    term = re.sub(r"[^a-z0-9_]", "", term)
    return term


def _rank_label(rank_uri: str) -> str:
    if "PreferredRank" in rank_uri:
        return "preferred"
    if "DeprecatedRank" in rank_uri:
        return "deprecated"
    return "normal"


def _extract_entity_id(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _extract_p31(entity_data: dict) -> list[str]:
    """Extract P31 (instance-of) Q-ids from a wbgetentities
    entity payload. Returns an empty list when the entity has no P31
    claims or the claim shape is unexpected (defensive — Wikidata's
    schema is stable in practice but downstream callers should not crash
    on a single malformed claim)."""
    claims = entity_data.get("claims", {}).get("P31", []) if isinstance(entity_data, dict) else []
    out: list[str] = []
    for claim in claims:
        try:
            qid = (
                claim.get("mainsnak", {})
                .get("datavalue", {})
                .get("value", {})
                .get("id")
            )
        except AttributeError:
            qid = None
        if isinstance(qid, str) and qid.startswith("Q"):
            out.append(qid)
    return out


def _parse_time_value(raw: str) -> str:
    # "+2009-01-20T00:00:00Z" → "2009-01-20"
    m = re.match(r"\+?(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else raw


def _extract_label_from_entities(data: dict, qid: KBEntityID) -> Optional[str]:
    """v0.16 WS1: pull the English label for `qid` from a wbgetentities
    payload (`{"entities": {"<Q>": {"labels": {"en": {"value": ...}}}}}`).
    Also accepts a terse `{"label": "..."}` fixture shape. Returns None on any
    missing/malformed shape — fail-open, never raises."""
    try:
        terse = data.get("label")
        if isinstance(terse, str) and terse:
            return terse
        label = (
            data.get("entities", {})
            .get(qid, {})
            .get("labels", {})
            .get("en", {})
            .get("value")
        )
    except AttributeError:
        return None
    return label if isinstance(label, str) and label else None


class WikidataAdapter:
    def __init__(
        self,
        http_cache=None,
        llm_client=None,
        db=None,
        config=None,
        fixture_dir: Optional[Path] = None,
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._http = http_cache
        self._llm = llm_client
        self._db = db
        self._config = config
        self._fixture_dir = fixture_dir or _FIXTURE_DIR
        self._live = os.environ.get("RUN_LIVE_KB") == "1"

        # v0.16.1 WS9 (item 8 lever A): process-scoped positive-result memo for
        # verify_transitive_path — the positive twin of the negative nogood
        # cache (`self._exception_cache`). Keyed by
        # (cache_relation, source, target) -> (TransitivePathResult, expires_at).
        # Stores ONLY DEFINITE answers (result.error is None); error/fail-open/
        # timeout results are NEVER memoized so a transient failure cannot pin a
        # wrong answer. Bounded by an LRU cap (OrderedDict move-to-end) AND a TTL
        # (monotonic clock) so a Wikidata edit cannot pin a stale positive for the
        # process lifetime. `clock` is injectable for deterministic TTL tests;
        # defaults to time.monotonic. Behavior-NEUTRAL: a hit returns exactly what
        # a fresh definite ASK would (same holds, error=None) without the network.
        self._transitive_memo: "OrderedDict[tuple[str, str, str], tuple[TransitivePathResult, float]]" = (
            OrderedDict()
        )
        self._transitive_memo_clock = clock or time.monotonic
        # v0.16.2 Phase C: the memo is a bare OrderedDict LRU shared by ALL
        # concurrent claim-walks of a turn (one adapter per pipeline). Like the
        # sibling LRUHTTPCache and RateLimiter, its check-then-act mutations
        # (get->del / move_to_end / popitem) are not concurrency-safe; guard them
        # so a parallel walk can't race-raise inside verify_transitive_path (which
        # the walker swallows to abstain) and silently diverge a verdict from
        # serial. Leaf lock: held only across in-memory dict ops, never the ASK.
        self._transitive_memo_lock = threading.Lock()
        # v0.16 WS3 §3D: bounded nogood cache (SubstrateExceptionCache). When
        # build_pipeline wires it, verify_transitive_path consults it BEFORE the
        # SPARQL ASK (return holds=False on a cached nogood — the leak guard) and
        # EAGERLY records a nogood on a negative ASK. Defaults None — fixture/
        # mock paths run exactly as before (no consult, no record).
        self._exception_cache = None

        # Rate limiters live as instance attributes: owning the state
        # here keeps future lock-protection a small change rather than a
        # refactor of where the state lives.
        override_ms_raw = os.getenv("AEDOS_KB_REQUEST_DELAY_MS")
        override_ms = int(override_ms_raw) if override_ms_raw else None
        search_rate = self._cfg_value("wikidata_search_rate_per_second", _DEFAULT_SEARCH_RATE)
        sparql_rate = self._cfg_value("wikidata_sparql_rate_per_second", _DEFAULT_SPARQL_RATE)
        self._search_limiter = RateLimiter(search_rate, override_delay_ms=override_ms)
        self._sparql_limiter = RateLimiter(sparql_rate, override_delay_ms=override_ms)

    def _cfg_value(self, attr: str, default):
        """Read a Config field by name, falling back to a default. The
        adapter accepts either a dataclass Config or None — fixture-only
        tests construct it without one."""
        if self._config is None:
            return default
        return getattr(self._config, attr, default)

    # ------------------------------------------------------------------
    # KBProtocol implementation
    # ------------------------------------------------------------------

    def resolve_entity(
        self, reference: str, local_context: LocalContext
    ) -> list[ResolutionCandidate]:
        if self._live:
            return self._live_resolve(reference, local_context)
        return self._fixture_resolve(reference, local_context)

    def lookup_statements(
        self, entity: KBEntityID, predicate: KBPropertyID
    ) -> list[Statement]:
        if self._live:
            return self._live_lookup(entity, predicate)
        return self._fixture_lookup(entity, predicate)

    def subsumption(
        self, entity_a: KBEntityID, entity_b: KBEntityID, relation_type: str
    ) -> SubsumptionResult:
        if self._live:
            return self._live_subsumption(entity_a, entity_b, relation_type)
        return self._fixture_subsumption(entity_a, entity_b, relation_type)

    def verify_transitive_path(
        self,
        source: KBEntityID,
        target: KBEntityID,
        kb_property: KBPropertyID,
        relation_type: Optional[str] = None,
        *,
        exception_cache=None,
    ) -> TransitivePathResult:
        """v0.16 WS2 §1: single-direction transitive-path existence check.

        Resolves the property alternation:
          - `relation_type` supplied: reuse the curated
            `_SUBSUMPTION_PROPERTIES` alternation (+ type-guarded P361 bridge
            for part_of). `kb_property` is ignored in this branch.
          - `relation_type` None: a single-property `(wdt:{kb_property})+` path.

        Single direction only (source -> target). FAIL-OPEN: any error returns
        `TransitivePathResult(holds=False, error=...)`. See
        `KBProtocol.verify_transitive_path`.

        v0.16 WS3 §3D nogood consult: `exception_cache` is the bounded
        SubstrateExceptionCache. When supplied (explicit arg or the adapter's
        wired `self._exception_cache`), a matching nogood SHORT-CIRCUITS to
        `holds=False` WITHOUT a SPARQL call (the leak guard); a negative ASK
        result is EAGERLY recorded as a nogood. A stale nogood can only cause a
        false abstain, never a false-verify — §3.2 stays safe."""
        cache = exception_cache if exception_cache is not None else self._exception_cache
        if relation_type is not None:
            props = _SUBSUMPTION_PROPERTIES.get(relation_type)
            if props is None:
                raise ValueError(
                    f"Unsupported relation_type: {relation_type!r} "
                    f"(expected one of {sorted(_SUBSUMPTION_PROPERTIES)})"
                )
            use_bridge = relation_type == "part_of"
            cache_relation = relation_type
        else:
            props = (kb_property,)
            use_bridge = False
            cache_relation = kb_property or ""
        path = "|".join(p for p in props if p) if props else ""

        # v0.16.1 WS9 (item 8 lever A): positive-result memo consult — at the
        # TOP, before the rate-limited ASK. A live (non-expired) DEFINITE entry
        # returns a result equivalent to a fresh definite ASK (same holds,
        # error=None) without any network call. The memo holds only definite
        # answers, so a hit can never replay a stale error/fail-open verdict.
        if cache_relation:
            memo_hit = self._transitive_memo_get(cache_relation, source, target)
            if memo_hit is not None:
                return memo_hit

        # Nogood consult FIRST — a cached "does NOT hold" closes the leak even
        # if the alternation was later widened, without re-hitting the network.
        if cache is not None and cache_relation:
            try:
                if cache.is_nogood(
                    relation_type=cache_relation,
                    source_identifier=source,
                    target_identifier=target,
                ):
                    return TransitivePathResult(holds=False)
            except Exception:
                pass  # fail-open: a flaky cache must not block a sound check

        if self._live:
            result = self._live_transitive_path(source, target, props, use_bridge)
        else:
            result = self._fixture_transitive_path(
                source, target, props, use_bridge, relation_type
            )

        # EAGER record on a negative result (no error). A path that errored is
        # not a nogood — only a confirmed non-holding ASK is cached.
        if (
            cache is not None
            and cache_relation
            and not getattr(result, "holds", False)
            and getattr(result, "error", None) is None
        ):
            try:
                cache.record_nogood(
                    relation_type=cache_relation,
                    source_identifier=source,
                    target_identifier=target,
                    property_path=path,
                    reason="ask_false",
                )
            except Exception:
                pass  # fail-open

        # v0.16.1 WS9 (item 8 lever A): memoize ONLY a DEFINITE answer
        # (result.error is None). An error/fail-open/timeout result is NEVER
        # cached — it must re-hit the network so a transient failure cannot pin a
        # wrong answer.
        #
        # Negative-result interaction with the nogood cache: when a nogood cache
        # is wired, IT owns definite negatives (it is consulted above and a
        # negative was just recorded) AND it honors operator retraction
        # (`retract`) — so a definite holds=False is left to the nogood cache and
        # NOT duplicated here, or the memo would serve a retracted negative for
        # the rest of the TTL window. We therefore memoize a definite holds=False
        # only when no nogood cache governs it (no retraction path exists; the
        # TTL still bounds staleness). holds=True is always memoized (the nogood
        # cache never records positives — this memo is its positive twin).
        if cache_relation and getattr(result, "error", None) is None:
            memoizable = result.holds or cache is None
            if memoizable:
                self._transitive_memo_put(cache_relation, source, target, result)
        return result

    # ------------------------------------------------------------------
    # v0.16.1 WS9 (item 8 lever A): positive-result memo for
    # verify_transitive_path. Process-scoped per-adapter-instance; composes
    # with the HTTP cache and the negative nogood cache (its positive twin).
    # ------------------------------------------------------------------
    def _transitive_memo_get(
        self, cache_relation: str, source: KBEntityID, target: KBEntityID
    ) -> Optional[TransitivePathResult]:
        """Return a fresh definite result for a live (non-expired) memo entry,
        else None. On a hit, returns a NEW TransitivePathResult with error=None
        — identical to what a fresh definite ASK would yield — so callers can
        never observe (or mutate) the stored object, and an expired or absent
        entry simply falls through to the live ASK. An expired entry is dropped
        on access (lazy TTL eviction)."""
        key = (cache_relation, source, target)
        with self._transitive_memo_lock:
            entry = self._transitive_memo.get(key)
            if entry is None:
                return None
            cached, expires_at = entry
            if self._transitive_memo_clock() >= expires_at:
                # Stale: drop so a Wikidata edit cannot pin it past the TTL.
                del self._transitive_memo[key]
                return None
            self._transitive_memo.move_to_end(key)  # LRU touch
            return TransitivePathResult(
                holds=cached.holds,
                establishing_property=cached.establishing_property,
                error=None,
            )

    def _transitive_memo_put(
        self,
        cache_relation: str,
        source: KBEntityID,
        target: KBEntityID,
        result: TransitivePathResult,
    ) -> None:
        """Store a DEFINITE result under a fresh TTL. Caller guarantees
        result.error is None. Bounds the map: refresh-and-LRU-touch on an
        existing key, then evict the oldest entries past the cap."""
        key = (cache_relation, source, target)
        expires_at = self._transitive_memo_clock() + _TRANSITIVE_MEMO_TTL_SECONDS
        stored = TransitivePathResult(
            holds=result.holds,
            establishing_property=result.establishing_property,
            error=None,
        )
        with self._transitive_memo_lock:
            self._transitive_memo[key] = (stored, expires_at)
            self._transitive_memo.move_to_end(key)
            while len(self._transitive_memo) > _TRANSITIVE_MEMO_MAX_ENTRIES:
                self._transitive_memo.popitem(last=False)  # evict oldest (LRU)

    def wbsearchentities(
        self, query: str, limit: Optional[int] = None
    ) -> list[WBSearchCandidate]:
        """Raw wbsearchentities query.

        Returns ranked Wikidata entity candidates with full metadata
        (label, description, aliases, match info). Unlike
        `resolve_entity`, this method does NOT apply the type
        filter — Stage C of the normalizer flow runs the filter
        downstream with knowledge of the claim's expected types.

        Fails open on error: returns `[]` and records an audit event
        with the error. Never raises. (Architecture §3.1: a transient
        Wikidata outage must not abstain on every resolution; the
        empty list lets the caller record the absence and proceed.)

        `query` is the search string. `limit` is the API's `limit`
        parameter (max 50 per the API contract); defaults to
        `Config.wikidata_wbsearch_limit` (which itself defaults to 20).

        Rate-limited via `self._search_limiter` (shared with
        `_live_resolve` and `_fetch_p31_for_candidates`). Cached at
        the HTTP layer with the entity TTL.

        Audit event: `wbsearchentities_query`. event_subject is the
        query string. event_data records query, limit, n_candidates,
        top_qids, duration, error.
        """
        if not isinstance(query, str) or not query.strip():
            return []

        if limit is None:
            limit = self._cfg_value("wikidata_wbsearch_limit", 20)

        if self._http is None:
            raise RuntimeError(
                "WikidataAdapter.wbsearchentities requires an http_cache; "
                "build_pipeline must construct the adapter with a CachingHTTPClient"
            )

        url = self._cfg_value("wikidata_search_endpoint", _DEFAULT_SEARCH_ENDPOINT)
        params = {
            "action": "wbsearchentities",
            "search": query,
            "language": "en",
            "type": "item",
            "limit": limit,
            "format": "json",
        }
        ttl = self._cfg_value("http_cache_entity_ttl_seconds", _DEFAULT_ENTITY_TTL_SECONDS)

        start = time.monotonic()
        retries = 0
        last_error: Optional[str] = None
        data = None

        for attempt in range(2):
            self._search_limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
                last_error = None
                break
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    retries += 1
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break

        candidates: list[WBSearchCandidate] = []
        if data is not None and isinstance(data, dict):
            results = data.get("search", [])
            if isinstance(results, list):
                for i, item in enumerate(results, start=1):
                    if not isinstance(item, dict):
                        continue
                    qid = item.get("id", "")
                    if not isinstance(qid, str) or not _ENTITY_ID_PATTERN.match(qid):
                        continue
                    match = item.get("match", {}) if isinstance(item.get("match"), dict) else {}
                    aliases = item.get("aliases", []) if isinstance(item.get("aliases"), list) else []
                    candidates.append(
                        WBSearchCandidate(
                            qid=qid,
                            label=item.get("label", "") or "",
                            description=item.get("description"),
                            aliases=[a for a in aliases if isinstance(a, str)],
                            match_type=match.get("type", "") or "",
                            match_text=match.get("text", "") or "",
                            rank=i,
                        )
                    )

        duration_ms = (time.monotonic() - start) * 1000.0
        self._log_audit_event(
            event_type="wbsearchentities_query",
            event_subject=query,
            event_data={
                "query": query,
                "limit": limit,
                "candidate_count": len(candidates),
                "top_qids": [c.qid for c in candidates[:5]],
                "duration_ms": round(duration_ms, 2),
                "retry_count": retries,
                "error": last_error,
            },
        )
        return candidates

    def enumerate_neighbors(
        self,
        entity: KBEntityID,
        properties: Optional[list[KBPropertyID]] = None,
        direction: str = "outgoing",
        relation_type: Optional[str] = None,
    ) -> dict[KBPropertyID, list[KBEntityID]]:
        """Enumerate `entity`'s direct KB neighbors along
        the given `properties`, in the given `direction`. Returns a dict
        keyed by property; each value is the list of neighbor entity Q-ids
        related to `entity` by that property in `direction`. Fails open
        (empty values) on error or no match; the audit log records the
        call's outcome either way.

        `properties` is a list (not a tuple) for KBProtocol parity with the
        other methods' input types — the live and fixture paths normalize
        it to a tuple before SPARQL construction.

        v0.16.1 WS5c: the OPAQUE `relation_type` ("is_a"/"part_of") lets CORE
        request a relation's neighbor property set without naming Wikidata
        P-ids. When `properties` is empty/None and `relation_type` resolves in
        `_NEIGHBOR_PROPERTIES_BY_RELATION`, the adapter uses that relation's
        P-id tuple; an explicit `properties` list takes precedence (SLING's
        co-occurrence path passes one), and an unknown/absent relation falls
        back to `_DEFAULT_NEIGHBOR_PROPERTIES`.

        `direction` is "outgoing" (default) or "incoming"; see
        `KBProtocol.enumerate_neighbors` for the semantic distinction.
        """
        if properties:
            props_tuple = tuple(properties)
        elif relation_type and relation_type in _NEIGHBOR_PROPERTIES_BY_RELATION:
            props_tuple = _NEIGHBOR_PROPERTIES_BY_RELATION[relation_type]
        else:
            props_tuple = _DEFAULT_NEIGHBOR_PROPERTIES
        if self._live:
            return self._live_neighbors(entity, props_tuple, direction)
        return self._fixture_neighbors(entity, props_tuple, direction)

    def fetch_property_ontology(self, prop: KBPropertyID) -> dict:
        """v0.16 WS1: fetch `prop`'s Wikidata constraint/relation ontology
        (P2302 subject/value-type + single-value constraints, plus
        P1647/P1696/P1659 sibling/inverse/related properties).

        Returns the dict shape `_parse_property_ontology_bindings` produces
        (subject_type_qids / value_type_qids / inverse_pids / subproperty_pids
        / related_pids / single_valued). `PropertyRelations.fetch` caches it.

        FAIL-OPEN: any error, a non-P-id input, or no constraints returns an
        EMPTY ontology dict (all-empty lists, single_valued=False). Never
        raises — discovery is additive enrichment; an ontology miss falls the
        caller back to the oracle's primary binding (current behavior)."""
        if self._live:
            return self._live_property_ontology(prop)
        return self._fixture_property_ontology(prop)

    def fetch_label(self, qid: KBEntityID) -> Optional[str]:
        """v0.16 WS1: fetch the English label of a Wikidata entity via
        wbgetentities (props=labels). Used by the discovery flow and the
        correction surface (WS5 `_format_correction` reverse-label).

        FAIL-OPEN: a non-Q-id input, a missing label, or any error returns
        None. Never raises."""
        if self._live:
            return self._live_label(qid)
        return self._fixture_label(qid)

    # ------------------------------------------------------------------
    # v0.16.1 WS5a: geographic predicate cluster (relocated from CORE)
    # ------------------------------------------------------------------

    def is_location_property(self, kb_property: KBPropertyID) -> bool:
        """v0.16.1 WS5a: True when `kb_property` is a GEOGRAPHIC
        location-containment property (_LOCATION_KB_PROPERTIES = P131/P17/P30/
        P361/P206/P276), for which the geographic-disjoint contradiction is
        sound. Relocated from CORE's `binding.kb_property in
        _LOCATION_KB_PROPERTIES` gate — behavior-identical."""
        return kb_property in _LOCATION_KB_PROPERTIES

    def geo_container_types(self) -> frozenset[KBEntityID]:
        """v0.16.1 WS5a: the geographic-container entity types (Q5107 continent)
        CORE uses to widen a location predicate's object-type filter. Relocated
        from CORE's `_GEO_CONTAINER_TYPES` — same closed set."""
        return _GEO_CONTAINER_TYPES

    def geographic_disjoint(
        self, value_qid: KBEntityID, expected_qid: KBEntityID
    ) -> bool:
        """v0.16.1 WS5a: True when KB confirms `value_qid` is geographically
        DISJOINT from `expected_qid`. Relocated from CORE's `_location_disjoint`
        (the logic lives in the module-level `_geographic_disjoint`, driven by
        this adapter's own `subsumption` — the same self.subsumption calls the
        CORE helper made on self._kb, so the live/fixture path and verdict are
        byte-identical). Fails closed on uncertainty (§3.2)."""
        return _geographic_disjoint(self.subsumption, value_qid, expected_qid)

    # ------------------------------------------------------------------
    # v0.16.1 WS5c: entity-surface search + type-fetch (relocated from the
    # Wikipedia normalizer's reach-arounds into adapter privates).
    # ------------------------------------------------------------------

    def search(self, query: str, limit: Optional[int] = None) -> list:
        """v0.16.1 WS5c: KBProtocol entity-surface search. Delegates to
        `wbsearchentities` (the implementation is unchanged — behavior is
        byte-identical), giving CORE a protocol-level search op so the
        normalizer no longer reaches into `wbsearchentities` directly."""
        return self.wbsearchentities(query, limit)

    def fetch_types(
        self, qids: list[KBEntityID]
    ) -> tuple[dict[str, list[str]], Optional[str]]:
        """v0.16.1 WS5c: KBProtocol batch P31 type-fetch. Delegates to
        `_fetch_p31_for_candidates` (unchanged), giving CORE a protocol-level
        type-fetch op so the normalizer no longer reaches into the adapter's
        private `_fetch_p31_for_candidates`. Returns `(types_by_qid, error)`;
        a non-None error is the fail-open signal (caller passes candidates
        unfiltered)."""
        return self._fetch_p31_for_candidates(qids)

    def sample_property_examples(
        self, prop: KBPropertyID, limit: int = 5
    ) -> list[tuple[KBEntityID, KBEntityID]]:
        """v0.16.3 Batch B (piece 1): source up to `limit` known-true
        (statement_subject_qid, value_qid) example pairs for a property — REAL
        Wikidata data, independent of the predicate-metadata oracle. The
        DirectionValidator probes these against `lookup_statements` to decide
        which KB role each Aedos slot maps to. FAIL-OPEN: returns [] on any error
        or a malformed/non-entity result (never raises), so a sourcing failure
        degrades the validator to 'unconfirmed', never to a wrong direction."""
        if self._live:
            return self._live_property_examples(prop, limit)
        return self._fixture_property_examples(prop, limit)

    def _fixture_property_examples(
        self, prop: KBPropertyID, limit: int
    ) -> list[tuple[KBEntityID, KBEntityID]]:
        """Fixture twin: reads property_examples_{P}.json shaped like a SPARQL
        SELECT (?s ?v bindings). Missing fixture → [] (fail-open)."""
        try:
            data = _load_fixture(f"property_examples_{prop}.json")
        except FixtureNotFoundError:
            return []
        return _parse_property_example_bindings(
            data.get("results", {}).get("bindings", []), limit
        )

    def _live_property_examples(
        self, prop: KBPropertyID, limit: int
    ) -> list[tuple[KBEntityID, KBEntityID]]:
        if self._http is None:
            return []
        try:
            query = _build_property_example_query(prop, limit)
        except ValueError:
            return []
        url = self._cfg_value("wikidata_sparql_endpoint", _DEFAULT_SPARQL_ENDPOINT)
        params = {"query": query, "format": "json"}
        ttl = self._cfg_value(
            "http_cache_statement_ttl_seconds", _DEFAULT_STATEMENT_TTL_SECONDS
        )
        data = None
        for attempt in range(2):
            self._sparql_limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
                break
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 0:
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                return []
            except Exception:
                return []
        bindings = (
            data.get("results", {}).get("bindings", []) if isinstance(data, dict) else []
        )
        pairs = _parse_property_example_bindings(bindings, limit)
        self._log_audit_event(
            event_type="kb_property_examples",
            event_subject=str(prop),
            event_data={"example_count": len(pairs)},
        )
        return pairs

    def interval_qualifier_keys(self) -> tuple[KBPropertyID, KBPropertyID]:
        """v0.16.1 WS5c: the (start, end) temporal interval-qualifier keys
        (`P580`, `P582`) CORE's interval resolver reads off
        `Statement.qualifiers`. Relocated from the walker's hardcoded P-ids so
        the qualifier P-ids live with the adapter that populates them. Order is
        contractual: (start_key, end_key)."""
        return (_INTERVAL_START_QUALIFIER, _INTERVAL_END_QUALIFIER)

    # ------------------------------------------------------------------
    # Fixture-backed implementations
    # ------------------------------------------------------------------

    def _fixture_resolve(
        self, reference: str, local_context: LocalContext
    ) -> list[ResolutionCandidate]:
        term = _normalize_search_term(reference)
        data = _load_fixture(f"search_{term}.json")
        results = data.get("search", [])
        candidates = []
        for rank, item in enumerate(results):
            score = 1.0 / (rank + 1)
            candidates.append(
                ResolutionCandidate(
                    kb_identifier=item["id"],
                    provenance={
                        "search_rank": rank,
                        "label": item.get("label"),
                        "description": item.get("description"),
                    },
                    score=score,
                )
            )
        return candidates

    def _fixture_lookup(
        self, entity: KBEntityID, predicate: KBPropertyID
    ) -> list[Statement]:
        fixture_name = f"sparql_{predicate}_{entity}.json"
        try:
            data = _load_fixture(fixture_name)
        except FixtureNotFoundError:
            return []
        bindings = data.get("results", {}).get("bindings", [])
        return _parse_statement_bindings(
            bindings,
            entity,
            predicate,
            provenance={"fixture": fixture_name, "entity": entity, "predicate": predicate},
        )

    def _fixture_neighbors(
        self,
        entity: KBEntityID,
        properties: tuple[KBPropertyID, ...],
        direction: str = "outgoing",
    ) -> dict[KBPropertyID, list[KBEntityID]]:
        """Fixture-backed enumeration. Reads
        `tests/fixtures/wikidata/neighbors_<entity>.json` for the
        outgoing direction, `neighbors_<entity>_reverse.json` for
        incoming. Both mirror the SPARQL response format
        (`{"results": {"bindings": [...]}}`). Missing fixture returns
        all-empty (treated as "no neighbors"). Filtering to the
        requested `properties` happens in the parser, so the fixture
        file can hold a superset and individual tests select the
        subset they need."""
        if direction == "incoming":
            fixture_name = f"neighbors_{entity}_reverse.json"
        else:
            fixture_name = f"neighbors_{entity}.json"
        try:
            data = _load_fixture(fixture_name)
        except FixtureNotFoundError:
            return {p: [] for p in properties}
        bindings = data.get("results", {}).get("bindings", [])
        return _parse_neighbors_bindings(bindings, properties)

    def _fixture_property_ontology(self, prop: KBPropertyID) -> dict:
        """v0.16 WS1: fixture-backed property ontology. Reads
        `tests/fixtures/wikidata/property_ontology_<P>.json`, which mirrors
        the SPARQL response format (`{"results": {"bindings": [...]}}`).
        Missing fixture (or a non-P-id) returns an EMPTY ontology — the
        ontology cannot constrain the property, so discovery falls back to
        the oracle binding. Empty-on-miss keeps non-live tests deterministic."""
        if not isinstance(prop, str) or not _PROPERTY_ID_PATTERN.match(prop):
            return _empty_property_ontology()
        try:
            data = _load_fixture(f"property_ontology_{prop}.json")
        except FixtureNotFoundError:
            return _empty_property_ontology()
        bindings = data.get("results", {}).get("bindings", []) if isinstance(data, dict) else []
        return _parse_property_ontology_bindings(bindings)

    def _fixture_label(self, qid: KBEntityID) -> Optional[str]:
        """v0.16 WS1: fixture-backed label fetch. Reads
        `tests/fixtures/wikidata/label_<Q>.json` (or `labels_<Q>.json`),
        which mirrors the wbgetentities response
        (`{"entities": {"<Q>": {"labels": {"en": {"value": "..."}}}}}`); a
        bare `{"label": "..."}` shape is also accepted for terse fixtures.
        Missing fixture, a non-Q-id, or no English label returns None."""
        if not isinstance(qid, str) or not _ENTITY_ID_PATTERN.match(qid):
            return None
        data = None
        for fixture_name in (f"label_{qid}.json", f"labels_{qid}.json"):
            try:
                data = _load_fixture(fixture_name)
                break
            except FixtureNotFoundError:
                continue
        if not isinstance(data, dict):
            return None
        return _extract_label_from_entities(data, qid)

    def _fixture_subsumption(
        self, entity_a: KBEntityID, entity_b: KBEntityID, relation_type: str
    ) -> SubsumptionResult:
        fixture_name = f"sparql_subsumption_{entity_a}.json"
        try:
            data = _load_fixture(fixture_name)
        except FixtureNotFoundError:
            return SubsumptionResult(verdict="unrelated")

        bindings = data.get("results", {}).get("bindings", [])
        if not bindings:
            return SubsumptionResult(verdict="unrelated")

        chain = [
            _extract_entity_id(row.get("intermediate", {}).get("value", ""))
            for row in bindings
            if row.get("intermediate", {}).get("value")
        ]
        # If entity_b appears in the chain or chain ends with entity_b, it's a subsumed
        # In fixture mode, presence of any chain from entity_a → treat as a_subsumed_by hierarchy
        return SubsumptionResult(
            verdict="a_subsumed_by_b",
            establishing_property="P31",
            traversal_chain=chain,
        )

    def _fixture_transitive_path(
        self,
        source: KBEntityID,
        target: KBEntityID,
        properties: tuple[KBPropertyID, ...],
        use_part_of_bridge: bool,
        relation_type: Optional[str],
    ) -> TransitivePathResult:
        """v0.16 WS2 §1: fixture-backed transitive-path check. Reuses the
        `_fixture_subsumption` keying convention — reads
        `tests/fixtures/wikidata/sparql_subsumption_<source>.json` (mirroring
        the SPARQL ASK/SELECT response format). The path is treated as
        HOLDING (holds=True) when the fixture's `results.bindings` is
        non-empty and `target` either ends the recorded chain or appears in
        it; otherwise holds=False. Missing fixture => holds=False (no path).
        Single direction (source -> target), consistent with the live ASK."""
        fixture_name = f"sparql_subsumption_{source}.json"
        try:
            data = _load_fixture(fixture_name)
        except FixtureNotFoundError:
            return TransitivePathResult(holds=False)

        # ASK-shaped fixture: {"boolean": true|false}.
        if isinstance(data, dict) and "boolean" in data:
            return TransitivePathResult(holds=bool(data.get("boolean")))

        bindings = data.get("results", {}).get("bindings", [])
        if not bindings:
            return TransitivePathResult(holds=False)
        chain = [
            _extract_entity_id(row.get("intermediate", {}).get("value", ""))
            for row in bindings
            if row.get("intermediate", {}).get("value")
        ]
        # A chain to `target` exists iff target ends the recorded chain or
        # appears within it; an empty/None chain with present bindings is
        # treated as a hit (mirrors `_fixture_subsumption`'s any-chain rule).
        holds = (not chain) or (target in chain)
        establishing = properties[0] if properties else None
        return TransitivePathResult(
            holds=holds,
            establishing_property=establishing if holds else None,
        )

    # ------------------------------------------------------------------
    # Live stubs
    # ------------------------------------------------------------------

    def _live_resolve(
        self, reference: str, local_context: LocalContext
    ) -> list[ResolutionCandidate]:
        """Resolve a natural-language reference to ranked Wikidata candidates
        via the `wbsearchentities` API.

        Contract:
          - Returns search-ranked candidates with scores 1/(rank+1) so
            consumer behavior is identical to fixture mode.
          - Caches at the HTTP layer via the injected CachingHTTPClient
            (TTL = Config.http_cache_entity_ttl_seconds).
          - Rate-limited via `self._search_limiter` (50/s default; the
            runbook's `AEDOS_KB_REQUEST_DELAY_MS` overrides).
          - Single retry on transient network failure (`httpx.TimeoutException`
            / `httpx.NetworkError`); thereafter returns `[]` per
            architecture §9.4 ("Entity not found → empty candidates →
            abstention"). Never raises.
          - One audit-log event per call (`event_type="kb_live_resolve"`).

        `local_context` is accepted for protocol parity with the fixture
        path; `wbsearchentities` itself takes no local-context input.
        Disambiguation that depends on local context happens downstream
        in `EntityResolver.select`.
        """
        if self._http is None:
            # Wiring-gap defence: a live resolve was attempted without an
            # HTTP cache. Surface honestly (architecture §3.1) rather than
            # silently returning empty candidates.
            raise RuntimeError(
                "WikidataAdapter._live_resolve requires an http_cache; "
                "build_pipeline must construct the adapter with a CachingHTTPClient"
            )

        url = self._cfg_value("wikidata_search_endpoint", _DEFAULT_SEARCH_ENDPOINT)
        params = {
            "action": "wbsearchentities",
            "search": reference,
            "language": "en",
            "type": "item",
            "limit": self._cfg_value(
                "wikidata_candidate_pool_size", _DEFAULT_CANDIDATE_POOL_SIZE
            ),
            "format": "json",
        }
        ttl = self._cfg_value("http_cache_entity_ttl_seconds", _DEFAULT_ENTITY_TTL_SECONDS)

        start = time.monotonic()
        candidates: list[ResolutionCandidate] = []
        retries = 0
        last_error: Optional[str] = None

        for attempt in range(2):
            self._search_limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    retries += 1
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                break
            except Exception as exc:
                # 4xx/5xx from raise_for_status, JSON parse errors,
                # anything else: log and return empty (architecture §9.4).
                last_error = f"{type(exc).__name__}: {exc}"
                break

            # Successful response: parse and return.
            results = data.get("search", []) if isinstance(data, dict) else []
            for rank, item in enumerate(results):
                if not isinstance(item, dict) or "id" not in item:
                    continue
                candidates.append(
                    ResolutionCandidate(
                        kb_identifier=item["id"],
                        provenance={
                            "search_rank": rank,
                            "label": item.get("label"),
                            "description": item.get("description"),
                        },
                        score=1.0 / (rank + 1),
                    )
                )
            last_error = None
            break

        # Post-filter the candidate pool by P31 ∩ expected types.
        pre_filter_count = len(candidates)
        filter_eliminated_count = 0
        filter_no_op_reason: Optional[str] = None
        sparql_fallback_used = False
        sparql_fallback_count = 0
        sparql_fallback_error: Optional[str] = None
        expected_types = list(local_context.expected_entity_types or [])
        type_filter_on = self._cfg_value("wikidata_type_filter_enabled", True)
        filter_ran_cleanly = False

        if not candidates:
            filter_no_op_reason = "no_candidates"
        elif not type_filter_on:
            filter_no_op_reason = "filter_disabled"
        elif not expected_types:
            filter_no_op_reason = "no_expected_types"
        else:
            candidate_ids = [c.kb_identifier for c in candidates]
            p31_by_qid, fetch_error = self._fetch_p31_for_candidates(candidate_ids)
            if fetch_error is not None:
                # Fail-open per design doc: a transient wbgetentities failure
                # must not abstain on every resolution. Audit makes it visible.
                filter_no_op_reason = "wbgetentities_failed"
                if last_error is None:
                    last_error = fetch_error
            else:
                type_set = set(expected_types)
                kept: list[ResolutionCandidate] = []
                for cand in candidates:
                    p31 = p31_by_qid.get(cand.kb_identifier, [])
                    if type_set & set(p31):
                        kept.append(cand)
                filter_eliminated_count = len(candidates) - len(kept)
                candidates = kept
                filter_ran_cleanly = True

        # Surfaced during live validation: the
        # wbsearchentities top-N candidate pool sometimes does not contain the
        # canonical entity even after raising pool size — short ambiguous
        # references like "Obama" or "Williams College" return mostly
        # disambiguation noise. When the post-filter empties under those
        # conditions, fall back to a SPARQL label-OR-altLabel search scoped
        # by expected_types. The fallback only runs when the filter ran
        # cleanly (so we don't double-call on fail-open paths) and the pool
        # genuinely doesn't contain a type-matching candidate.
        if filter_ran_cleanly and not candidates and expected_types:
            sparql_fallback_used = True
            fb_candidates, fb_error = self._sparql_label_type_fallback(
                reference, expected_types
            )
            sparql_fallback_count = len(fb_candidates)
            sparql_fallback_error = fb_error
            if fb_error is not None and last_error is None:
                last_error = fb_error
            candidates = fb_candidates

        # Demonym → country fallback. When the slot is type-constrained
        # to a country (Q6256) and neither wbsearchentities nor the SPARQL
        # label/altLabel fallback resolved the reference, try a Wikidata P1549
        # (demonym) reverse lookup: the reference may be a demonym ("German",
        # "American") whose country is unreachable by label match (the label is
        # "Germany", not "German"). Generalizes the hand-curated
        # _DEMONYM_TO_COUNTRY map to every demonym Wikidata records. Sound:
        # fires only for a country-typed slot, accepts a unique match.
        if not candidates and "Q6256" in set(expected_types):
            demonym_qid = self._resolve_demonym_to_country(reference)
            if demonym_qid is not None:
                candidates = [
                    ResolutionCandidate(
                        kb_identifier=demonym_qid,
                        provenance={"source": "demonym_p1549", "demonym": reference},
                        score=1.0,
                    )
                ]

        duration_ms = (time.monotonic() - start) * 1000.0
        self._log_audit_event(
            event_type="kb_live_resolve",
            event_subject=reference,
            event_data={
                "candidate_count": len(candidates),
                "duration_ms": round(duration_ms, 2),
                "retry_count": retries,
                "error": last_error,
                # Type-filter audit fields.
                "pre_filter_count": pre_filter_count,
                "filter_eliminated_count": filter_eliminated_count,
                "expected_entity_types": expected_types,
                "filter_no_op_reason": filter_no_op_reason,
                "sparql_fallback_used": sparql_fallback_used,
                "sparql_fallback_count": sparql_fallback_count,
                "sparql_fallback_error": sparql_fallback_error,
            },
        )
        return candidates

    def _sparql_label_type_fallback(
        self, reference: str, expected_types: list[KBEntityID]
    ) -> tuple[list[ResolutionCandidate], Optional[str]]:
        """SPARQL fallback when the wbsearchentities post-filter
        empties. Looks up items by rdfs:label OR skos:altLabel match in English,
        constrained by P31 ∈ expected_types. Returns (candidates, error).

        Candidates are scored 1/(rank+1) like wbsearchentities, preserving the
        downstream resolver's score-based selection. The fallback can't match
        the search API's relevance ranking (SPARQL has no equivalent), so
        ranking is bound to the SPARQL result order — which is typically
        Wikidata's internal item order. In practice, exact-label matches with
        type constraint return few results (the type filter is sharp), so
        ranking effects are small.
        """
        if not expected_types:
            return ([], None)
        if self._http is None:
            raise RuntimeError(
                "WikidataAdapter._sparql_label_type_fallback requires an http_cache"
            )

        url = self._cfg_value("wikidata_sparql_endpoint", _DEFAULT_SPARQL_ENDPOINT)
        # Use the search pool size as the LIMIT so the fallback can't return
        # more candidates than the primary path's pool size.
        limit = self._cfg_value(
            "wikidata_candidate_pool_size", _DEFAULT_CANDIDATE_POOL_SIZE
        )
        try:
            query = _build_label_type_search_query(reference, expected_types, limit)
        except ValueError as exc:
            # Invalid Q-id in expected_types — return ([], error) and let the
            # caller record fail-open behavior. Never crash a live resolution
            # on a substrate-provided type list.
            return ([], f"{type(exc).__name__}: {exc}")

        params = {"query": query, "format": "json"}
        ttl = self._cfg_value(
            "http_cache_statement_ttl_seconds", _DEFAULT_STATEMENT_TTL_SECONDS
        )

        for attempt in range(2):
            self._sparql_limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 0:
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                return ([], f"{type(exc).__name__}: {exc}")
            except Exception as exc:
                return ([], f"{type(exc).__name__}: {exc}")

            bindings = (
                data.get("results", {}).get("bindings", [])
                if isinstance(data, dict)
                else []
            )
            candidates: list[ResolutionCandidate] = []
            for rank, row in enumerate(bindings):
                item_uri = row.get("item", {}).get("value", "")
                if not item_uri:
                    continue
                qid = _extract_entity_id(item_uri)
                if not _ENTITY_ID_PATTERN.match(qid):
                    continue
                label = row.get("itemLabel", {}).get("value")
                description = row.get("itemDescription", {}).get("value")
                candidates.append(
                    ResolutionCandidate(
                        kb_identifier=qid,
                        provenance={
                            "search_rank": rank,
                            "label": label,
                            "description": description,
                            "source": "sparql_fallback",
                        },
                        score=1.0 / (rank + 1),
                    )
                )
            return (candidates, None)

        return ([], "unreachable_code")  # pragma: no cover

    def _resolve_demonym_to_country(self, demonym: str) -> Optional[str]:
        """(S1b) Resolve a demonym ("German", "American") to the Q-id of the
        country whose Wikidata P1549 (demonym) value includes it, constrained
        to instances/subclasses of country (Q6256). Returns the Q-id on a
        UNIQUE match, else None — fail closed on 0 / >1 matches, a non-word
        input, or any error. The general replacement for the hand-curated
        _DEMONYM_TO_COUNTRY map: every demonym Wikidata records, none in code.
        """
        if not isinstance(demonym, str):
            return None
        token = demonym.strip()
        # Demonyms are short alphabetic words (optionally hyphenated/spaced).
        # Rejecting anything else is both a soundness gate and an injection
        # defense — `token` is interpolated into a SPARQL string literal.
        if not token or not re.match(r"^[A-Za-z][A-Za-z .'\-]{0,40}$", token):
            return None
        if self._http is None:
            return None
        safe = token.replace("\\", "\\\\").replace('"', '\\"').lower()
        query = (
            "SELECT DISTINCT ?item WHERE {\n"
            "  ?item wdt:P1549 ?d .\n"
            f'  FILTER(LCASE(STR(?d)) = "{safe}")\n'
            "  ?item wdt:P31/wdt:P279* wd:Q6256 .\n"
            "} LIMIT 3"
        )
        url = self._cfg_value("wikidata_sparql_endpoint", _DEFAULT_SPARQL_ENDPOINT)
        params = {"query": query, "format": "json"}
        ttl = self._cfg_value(
            "http_cache_statement_ttl_seconds", _DEFAULT_STATEMENT_TTL_SECONDS
        )
        data = None
        for attempt in range(2):
            self._sparql_limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
                break
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 0:
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                return None
            except Exception:
                return None
        bindings = (
            data.get("results", {}).get("bindings", [])
            if isinstance(data, dict) else []
        )
        qids: list[str] = []
        for row in bindings:
            uri = row.get("item", {}).get("value", "")
            qid = _extract_entity_id(uri)
            if _ENTITY_ID_PATTERN.match(qid) and qid not in qids:
                qids.append(qid)
        # Unique match only — multiple countries sharing a demonym is
        # ambiguous; abstain rather than guess (soundness over completeness).
        return qids[0] if len(qids) == 1 else None

    def _fetch_p31_for_candidates(
        self, candidate_ids: list[KBEntityID]
    ) -> tuple[dict[str, list[str]], Optional[str]]:
        """Batch-fetch P31 (instance-of) Q-ids for each candidate.

        Calls ``wbgetentities`` with ``props=claims``, splitting into batches
        of at most ``Config.wikidata_type_filter_p31_batch_size`` (default 50,
        the API limit). Returns ``(p31_by_qid, error)``:

          - ``p31_by_qid`` maps each candidate Q-id to its P31 list. Candidates
            whose entity payload is missing or unparseable get an empty list.
          - ``error`` is None on full success. On API failure ``error`` is the
            stringified exception; ``_live_resolve`` interprets a non-None
            error as "fail-open" (return unfiltered candidates) per the design
            doc's transient-failure decision.

        Uses the search rate-limiter — wbgetentities shares the action-API
        endpoint with wbsearchentities and the same per-IP fairness budget.
        Caches at the HTTP layer with the entity TTL (P31 values rarely
        change, and the entity TTL is the right shelf-life).
        """
        if not candidate_ids:
            return ({}, None)
        if self._http is None:
            raise RuntimeError(
                "WikidataAdapter._fetch_p31_for_candidates requires an http_cache; "
                "build_pipeline must construct the adapter with a CachingHTTPClient"
            )

        url = self._cfg_value("wikidata_search_endpoint", _DEFAULT_SEARCH_ENDPOINT)
        ttl = self._cfg_value("http_cache_entity_ttl_seconds", _DEFAULT_ENTITY_TTL_SECONDS)
        batch_size = self._cfg_value(
            "wikidata_type_filter_p31_batch_size", _DEFAULT_P31_BATCH_SIZE
        )

        p31_by_qid: dict[str, list[str]] = {qid: [] for qid in candidate_ids}
        for start in range(0, len(candidate_ids), batch_size):
            batch = candidate_ids[start : start + batch_size]
            params = {
                "action": "wbgetentities",
                "ids": "|".join(batch),
                "props": "claims",
                "format": "json",
            }
            self._search_limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
            except Exception as exc:
                # Batch-level failure: surface as fail-open signal.
                # Per design doc lines 283-289: a transient wbgetentities
                # failure should not abstain on every resolution.
                return (p31_by_qid, f"{type(exc).__name__}: {exc}")

            entities = data.get("entities", {}) if isinstance(data, dict) else {}
            for qid in batch:
                entity_data = entities.get(qid)
                if isinstance(entity_data, dict):
                    p31_by_qid[qid] = _extract_p31(entity_data)
                # If the API omitted the entity, it stays at the [] seed.

        return (p31_by_qid, None)

    def _log_audit_event(self, event_type: str, event_subject: str, event_data: dict) -> None:
        """Best-effort audit logging. No-ops when no db is wired (test
        constructions that don't pass one)."""
        if self._db is None:
            return
        try:
            log_event(
                self._db,
                event_type=event_type,
                event_subject=event_subject,
                event_data=event_data,
            )
        except Exception:
            # Audit logging is observability, not correctness; never
            # let a logging failure break the verification path.
            pass

    def _live_lookup(
        self, entity: KBEntityID, predicate: KBPropertyID
    ) -> list[Statement]:
        """Look up statements for (entity, predicate) via SPARQL against WDQS.

        Contract:
          - SPARQL endpoint = `Config.wikidata_sparql_endpoint`
            (default `https://query.wikidata.org/sparql`).
          - Returns `Statement` objects with rank, qualifiers (default set
            P580/P582/P642), and provenance.
          - Direction-neutral: looks up whatever entity/predicate it is
            given. `KBVerifier` is responsible for swapping the lookup
            direction for inverse predicates.
          - Polarity-neutral: returns positive statements only; polarity
            handling lives in `KBVerifier._apply_polarity`.
          - HTTP-layer caching with statement TTL
            (`Config.http_cache_statement_ttl_seconds`, default 86400s).
          - Rate-limited via `self._sparql_limiter` (5/s default).
          - Single retry on transient network failure; thereafter
            returns `[]` per architecture §9.4. Never raises (except
            on the wiring-gap defence below).
          - One audit-log event per call (`event_type="kb_live_lookup"`).
        """
        if self._http is None:
            raise RuntimeError(
                "WikidataAdapter._live_lookup requires an http_cache; "
                "build_pipeline must construct the adapter with a CachingHTTPClient"
            )

        url = self._cfg_value("wikidata_sparql_endpoint", _DEFAULT_SPARQL_ENDPOINT)
        try:
            query = _build_lookup_query(entity, predicate)
        except ValueError as exc:
            # Malformed Q/P-id from a caller — surface honestly. Architecture
            # §3.1 / §9.4: abstain on no-grounding, but a malformed lookup is
            # a programming error, not an abstention.
            self._log_audit_event(
                event_type="kb_live_lookup",
                event_subject=f"{entity}:{predicate}",
                event_data={"statement_count": 0, "error": str(exc)},
            )
            raise

        # WDQS supports `format=json` as a URL parameter; cleaner than
        # negotiating via Accept headers through the CachingHTTPClient layer.
        params = {"query": query, "format": "json"}
        ttl = self._cfg_value(
            "http_cache_statement_ttl_seconds", _DEFAULT_STATEMENT_TTL_SECONDS
        )

        start = time.monotonic()
        statements: list[Statement] = []
        retries = 0
        last_error: Optional[str] = None

        for attempt in range(2):
            self._sparql_limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    retries += 1
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break

            bindings = (
                data.get("results", {}).get("bindings", [])
                if isinstance(data, dict)
                else []
            )
            statements = _parse_statement_bindings(
                bindings,
                entity,
                predicate,
                provenance={
                    "source": "live",
                    "entity": entity,
                    "predicate": predicate,
                },
            )
            last_error = None
            break

        duration_ms = (time.monotonic() - start) * 1000.0
        self._log_audit_event(
            event_type="kb_live_lookup",
            event_subject=f"{entity}:{predicate}",
            event_data={
                "statement_count": len(statements),
                "duration_ms": round(duration_ms, 2),
                "retry_count": retries,
                "error": last_error,
            },
        )
        return statements

    def _live_subsumption(
        self, entity_a: KBEntityID, entity_b: KBEntityID, relation_type: str
    ) -> SubsumptionResult:
        """Resolve subsumption between two Wikidata entities via SPARQL.

        Contract:
          - Two ASK queries (one per direction) for path-existence;
            ASK short-circuits on first match, so this is fast even on
            broad-fanout properties.
          - Verdict logic:
              direction_a→b  direction_b→a  → verdict
              true           false           a_subsumed_by_b
              false          true            b_subsumed_by_a
              true           true            equivalent
              false          false           unrelated
          - Operator Q2: on non-`unrelated` verdicts, a follow-up SELECT
            identifies the immediate (depth-1) establishing property —
            useful for trace inspection.
          - Bounded depth: relies on WDQS's 60s server timeout +
            CachingHTTPClient's 30s client timeout, not on an explicit
            depth quantifier (Wikidata's blazegraph doesn't support
            depth-bounded property paths cleanly).
          - On timeout / network failure: single retry per ASK; if both
            attempts fail, returns `unrelated` with the error noted in
            the audit log (architecture §9.4: timeout escalates to
            abstention, which for subsumption is `unrelated`).
          - Rate-limited via `self._sparql_limiter`.
          - One audit event per `_live_subsumption` call.
        """
        if self._http is None:
            raise RuntimeError(
                "WikidataAdapter._live_subsumption requires an http_cache; "
                "build_pipeline must construct the adapter with a CachingHTTPClient"
            )

        if relation_type not in _SUBSUMPTION_PROPERTIES:
            raise ValueError(
                f"Unsupported relation_type: {relation_type!r} "
                f"(expected one of {sorted(_SUBSUMPTION_PROPERTIES)})"
            )

        start = time.monotonic()
        retries = 0
        last_error: Optional[str] = None

        a_to_b, r1, e1 = self._run_subsumption_ask(entity_a, entity_b, relation_type)
        b_to_a, r2, e2 = self._run_subsumption_ask(entity_b, entity_a, relation_type)
        retries = r1 + r2
        last_error = e1 or e2  # the first error encountered, if any

        verdict = _subsumption_verdict(a_to_b, b_to_a)
        establishing_property: Optional[str] = None
        traversal_chain: list[KBEntityID] = []

        # Follow-up SELECT for non-unrelated verdicts (operator Q2).
        # On `equivalent` we just pick one direction (a→b) for the property.
        if verdict in ("a_subsumed_by_b", "equivalent"):
            establishing_property = self._fetch_establishing_property(
                entity_a, entity_b, relation_type
            )
            traversal_chain = [entity_a, entity_b]
        elif verdict == "b_subsumed_by_a":
            establishing_property = self._fetch_establishing_property(
                entity_b, entity_a, relation_type
            )
            traversal_chain = [entity_b, entity_a]

        duration_ms = (time.monotonic() - start) * 1000.0
        self._log_audit_event(
            event_type="kb_live_subsumption",
            event_subject=f"{entity_a}<>{entity_b}:{relation_type}",
            event_data={
                "verdict": verdict,
                "establishing_property": establishing_property,
                "duration_ms": round(duration_ms, 2),
                "retry_count": retries,
                "error": last_error,
            },
        )
        return SubsumptionResult(
            verdict=verdict,
            establishing_property=establishing_property,
            traversal_chain=traversal_chain,
        )

    def _run_subsumption_ask(
        self, source: KBEntityID, target: KBEntityID, relation_type: str
    ) -> tuple[bool, int, Optional[str]]:
        """Execute one ASK query (single direction). Returns (boolean,
        retry_count, error_or_None). On final failure returns (False, 1, error)
        — treating timeout/error as a false ASK, which `_subsumption_verdict`
        translates to `unrelated` when both directions fail."""
        url = self._cfg_value("wikidata_sparql_endpoint", _DEFAULT_SPARQL_ENDPOINT)
        query = _build_subsumption_ask_query(source, target, relation_type)
        params = {"query": query, "format": "json"}
        ttl = self._cfg_value(
            "http_cache_statement_ttl_seconds", _DEFAULT_STATEMENT_TTL_SECONDS
        )

        retries = 0
        for attempt in range(2):
            self._sparql_limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 0:
                    retries += 1
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                return (False, retries, f"{type(exc).__name__}: {exc}")
            except Exception as exc:
                return (False, retries, f"{type(exc).__name__}: {exc}")

            # ASK responses: {"head": {}, "boolean": true|false}
            if not isinstance(data, dict):
                return (False, retries, "malformed_response")
            return (bool(data.get("boolean", False)), retries, None)

        return (False, retries, "unreachable_code")  # pragma: no cover

    def _live_transitive_path(
        self,
        source: KBEntityID,
        target: KBEntityID,
        properties: tuple[KBPropertyID, ...],
        use_part_of_bridge: bool,
    ) -> TransitivePathResult:
        """v0.16 WS2 §1: live single-direction transitive-path ASK.

        One ASK (source -> target) via `_run_transitive_ask`, mirroring the
        rate-limit/retry/cache shape of `_run_subsumption_ask`. Unlike
        `_live_subsumption` (which runs BOTH directions + an establishing
        SELECT for the four-verdict symmetric case), this is single-direction
        and skips the establishing SELECT — the depth-1 anchor is approximated
        by the alternation's first property for observability only.

        FAIL-OPEN: on timeout/network/malformed the ASK returns False with an
        error; this method surfaces it as `TransitivePathResult(holds=False,
        error=...)`. One audit event `kb_verify_transitive_path`."""
        if self._http is None:
            raise RuntimeError(
                "WikidataAdapter._live_transitive_path requires an http_cache; "
                "build_pipeline must construct the adapter with a CachingHTTPClient"
            )

        start = time.monotonic()
        holds, retries, error = self._run_transitive_ask(
            source, target, properties, use_part_of_bridge
        )
        establishing = properties[0] if (holds and properties) else None

        duration_ms = (time.monotonic() - start) * 1000.0
        self._log_audit_event(
            event_type="kb_verify_transitive_path",
            event_subject=f"{source}->{target}",
            event_data={
                "holds": holds,
                "properties": list(properties),
                "use_part_of_bridge": use_part_of_bridge,
                "establishing_property": establishing,
                "duration_ms": round(duration_ms, 2),
                "retry_count": retries,
                "error": error,
            },
        )
        return TransitivePathResult(
            holds=holds, establishing_property=establishing, error=error
        )

    def _run_transitive_ask(
        self,
        source: KBEntityID,
        target: KBEntityID,
        properties: tuple[KBPropertyID, ...],
        use_part_of_bridge: bool,
    ) -> tuple[bool, int, Optional[str]]:
        """Execute one transitive-path ASK (single direction). Returns
        (boolean, retry_count, error_or_None). Mirrors `_run_subsumption_ask`
        (rate-limit, single retry, treat timeout/error as a False ASK) but
        builds the query via `_build_transitive_ask_query` so it serves the
        single-property generic path as well as the relation alternation."""
        url = self._cfg_value("wikidata_sparql_endpoint", _DEFAULT_SPARQL_ENDPOINT)
        query = _build_transitive_ask_query(
            source, target, properties, use_part_of_bridge
        )
        params = {"query": query, "format": "json"}
        ttl = self._cfg_value(
            "http_cache_statement_ttl_seconds", _DEFAULT_STATEMENT_TTL_SECONDS
        )

        retries = 0
        for attempt in range(2):
            self._sparql_limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 0:
                    retries += 1
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                return (False, retries, f"{type(exc).__name__}: {exc}")
            except Exception as exc:
                return (False, retries, f"{type(exc).__name__}: {exc}")

            # ASK responses: {"head": {}, "boolean": true|false}
            if not isinstance(data, dict):
                return (False, retries, "malformed_response")
            return (bool(data.get("boolean", False)), retries, None)

        return (False, retries, "unreachable_code")  # pragma: no cover

    def _fetch_establishing_property(
        self, source: KBEntityID, target: KBEntityID, relation_type: str
    ) -> Optional[str]:
        """Best-effort follow-up: identify the depth-1 property that
        anchors the path. Returns the P-id (e.g. "P31") or None if the
        follow-up fails (treats the failure as observability-only —
        the verdict from the ASK is the load-bearing answer)."""
        url = self._cfg_value("wikidata_sparql_endpoint", _DEFAULT_SPARQL_ENDPOINT)
        query = _build_establishing_property_query(source, target, relation_type)
        params = {"query": query, "format": "json"}
        ttl = self._cfg_value(
            "http_cache_statement_ttl_seconds", _DEFAULT_STATEMENT_TTL_SECONDS
        )

        self._sparql_limiter.acquire()
        try:
            data = self._http.get(url, params=params, ttl_seconds=ttl)
        except Exception:
            return None

        bindings = (
            data.get("results", {}).get("bindings", [])
            if isinstance(data, dict)
            else []
        )
        if not bindings:
            return None
        prop_uri = bindings[0].get("prop", {}).get("value", "")
        if not prop_uri:
            return None
        # URI shape: http://www.wikidata.org/prop/direct/P31
        return prop_uri.rsplit("/", 1)[-1] or None

    def _live_neighbors(
        self,
        entity: KBEntityID,
        properties: tuple[KBPropertyID, ...],
        direction: str = "outgoing",
    ) -> dict[KBPropertyID, list[KBEntityID]]:
        """Live SPARQL enumeration of `entity`'s direct
        neighbors along `properties`, in the given `direction`. One
        round-trip, returns the parsed dict.

        Contract:
          - SPARQL endpoint = `Config.wikidata_sparql_endpoint`
            (default `https://query.wikidata.org/sparql`).
          - HTTP cache + 24h TTL via `Config.http_cache_statement_ttl_seconds`
            (same TTL as `_live_lookup`; neighbor data is statement-shaped).
          - Rate-limited via `self._sparql_limiter` (5/s default).
          - Single retry on transient `httpx.TimeoutException` /
            `httpx.NetworkError`; thereafter fail-open with all-empty
            (no value for any requested property). Never raises except
            on the wiring-gap defence (no `http_cache`) or a malformed
            entity/property/direction (programming error, surfaced
            honestly).
          - One audit-log event per call (`event_type="kb_live_neighbors"`,
            event_data carries `direction`).
          - Reverse direction: LIMIT bounds the query
            (`Config.wikidata_neighbor_reverse_limit`, default 100) so
            unbounded properties like P17=Q30 don't blow up.
        """
        if self._http is None:
            raise RuntimeError(
                "WikidataAdapter._live_neighbors requires an http_cache; "
                "build_pipeline must construct the adapter with a CachingHTTPClient"
            )

        url = self._cfg_value("wikidata_sparql_endpoint", _DEFAULT_SPARQL_ENDPOINT)
        limit = self._cfg_value(
            "wikidata_neighbor_reverse_limit", _DEFAULT_NEIGHBOR_REVERSE_LIMIT
        )
        try:
            query = _build_neighbors_query(entity, properties, direction, limit)
        except ValueError as exc:
            # Programming error: malformed Q/P-id or direction. Architecture
            # §3.1 / §9.4: abstain on no-grounding, but a malformed call
            # surfaces honestly.
            self._log_audit_event(
                event_type="kb_live_neighbors",
                event_subject=entity,
                event_data={
                    "direction": direction,
                    "properties_requested": list(properties),
                    "total_neighbors_returned": 0,
                    "error": str(exc),
                },
            )
            raise

        params = {"query": query, "format": "json"}
        ttl = self._cfg_value(
            "http_cache_statement_ttl_seconds", _DEFAULT_STATEMENT_TTL_SECONDS
        )

        start = time.monotonic()
        result: dict[KBPropertyID, list[KBEntityID]] = {p: [] for p in properties}
        retries = 0
        last_error: Optional[str] = None

        for attempt in range(2):
            self._sparql_limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    retries += 1
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break

            bindings = (
                data.get("results", {}).get("bindings", [])
                if isinstance(data, dict)
                else []
            )
            result = _parse_neighbors_bindings(bindings, properties)
            last_error = None
            break

        duration_ms = (time.monotonic() - start) * 1000.0
        total = sum(len(v) for v in result.values())
        self._log_audit_event(
            event_type="kb_live_neighbors",
            event_subject=entity,
            event_data={
                "direction": direction,
                "properties_requested": list(properties),
                "total_neighbors_returned": total,
                "per_property_counts": {p: len(v) for p, v in result.items()},
                "duration_ms": round(duration_ms, 2),
                "retry_count": retries,
                "error": last_error,
            },
        )
        return result

    def _live_property_ontology(self, prop: KBPropertyID) -> dict:
        """v0.16 WS1: live SPARQL fetch of `prop`'s P2302/P1647/P1696/P1659
        ontology. One round-trip via `self._sparql_limiter`, statement TTL,
        single retry on transient error. FAIL-OPEN: returns an empty ontology
        on any error or a malformed P-id; one audit event either way."""
        if not isinstance(prop, str) or not _PROPERTY_ID_PATTERN.match(prop):
            return _empty_property_ontology()
        if self._http is None:
            raise RuntimeError(
                "WikidataAdapter._live_property_ontology requires an http_cache; "
                "build_pipeline must construct the adapter with a CachingHTTPClient"
            )
        url = self._cfg_value("wikidata_sparql_endpoint", _DEFAULT_SPARQL_ENDPOINT)
        ttl = self._cfg_value(
            "http_cache_statement_ttl_seconds", _DEFAULT_STATEMENT_TTL_SECONDS
        )
        try:
            query = _build_property_ontology_query(prop)
        except ValueError:
            return _empty_property_ontology()
        params = {"query": query, "format": "json"}

        start = time.monotonic()
        retries = 0
        last_error: Optional[str] = None
        ontology = _empty_property_ontology()
        for attempt in range(2):
            self._sparql_limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    retries += 1
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break
            bindings = (
                data.get("results", {}).get("bindings", [])
                if isinstance(data, dict)
                else []
            )
            ontology = _parse_property_ontology_bindings(bindings)
            last_error = None
            break

        duration_ms = (time.monotonic() - start) * 1000.0
        self._log_audit_event(
            event_type="kb_property_ontology",
            event_subject=prop,
            event_data={
                "property": prop,
                "subject_type_qids": ontology["subject_type_qids"],
                "value_type_qids": ontology["value_type_qids"],
                "inverse_pids": ontology["inverse_pids"],
                "subproperty_pids": ontology["subproperty_pids"],
                "related_pids": ontology["related_pids"],
                "single_valued": ontology["single_valued"],
                "duration_ms": round(duration_ms, 2),
                "retry_count": retries,
                "error": last_error,
            },
        )
        return ontology

    def _live_label(self, qid: KBEntityID) -> Optional[str]:
        """v0.16 WS1: live wbgetentities (props=labels) fetch of `qid`'s
        English label. One round-trip via `self._search_limiter` (the
        action-API budget shared with wbsearchentities), entity TTL, single
        retry. FAIL-OPEN: returns None on any error, a non-Q-id, or no label;
        one audit event either way."""
        if not isinstance(qid, str) or not _ENTITY_ID_PATTERN.match(qid):
            return None
        if self._http is None:
            raise RuntimeError(
                "WikidataAdapter._live_label requires an http_cache; "
                "build_pipeline must construct the adapter with a CachingHTTPClient"
            )
        url = self._cfg_value("wikidata_search_endpoint", _DEFAULT_SEARCH_ENDPOINT)
        params = {
            "action": "wbgetentities",
            "ids": qid,
            "props": "labels",
            "languages": "en",
            "format": "json",
        }
        ttl = self._cfg_value("http_cache_entity_ttl_seconds", _DEFAULT_ENTITY_TTL_SECONDS)

        start = time.monotonic()
        retries = 0
        last_error: Optional[str] = None
        label: Optional[str] = None
        for attempt in range(2):
            self._search_limiter.acquire()
            try:
                data = self._http.get(url, params=params, ttl_seconds=ttl)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    retries += 1
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break
            label = _extract_label_from_entities(data, qid) if isinstance(data, dict) else None
            last_error = None
            break

        duration_ms = (time.monotonic() - start) * 1000.0
        self._log_audit_event(
            event_type="kb_fetch_label",
            event_subject=qid,
            event_data={
                "qid": qid,
                "label": label,
                "duration_ms": round(duration_ms, 2),
                "retry_count": retries,
                "error": last_error,
            },
        )
        return label
