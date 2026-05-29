from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
)


@dataclass
class WBSearchCandidate:
    """Phase H D53: a raw wbsearchentities result row.

    Distinct from `ResolutionCandidate` (which is the KBProtocol-shaped
    score-wrapped form `KB.resolve_entity` returns). `WBSearchCandidate`
    preserves the API's full per-result payload — label, description,
    aliases, match metadata — so the D53 normalizer's Stage C can drive
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

# Wikidata identifier patterns. Used to validate inputs to SPARQL query
# construction — defense-in-depth against injection via Q/P-id parameters.
# All upstream sources (predicate translation oracle, entity resolver) produce
# canonical IDs, but validating at the SPARQL boundary prevents a future
# careless caller from constructing a malformed query.
_ENTITY_ID_PATTERN = re.compile(r"^Q\d+$")
_PROPERTY_ID_PATTERN = re.compile(r"^P\d+$")

# Default qualifier set requested in every _live_lookup SPARQL query.
# Per F2 design §5: P580 (start time) and P582 (end time) are required for
# universal scope checking; P642 (of) appears in the most-common seed
# (holds_role / P39). Other qualifiers referenced by less-common predicates'
# slot_to_qualifier maps are not collected here — v0.16 D32 captures the
# dynamic-discovery follow-up.
_DEFAULT_QUALIFIER_PROPS = ("P580", "P582", "P642")

# Phase G D33 (2026-05-23): when the wbsearchentities post-filter eliminates
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
      - P31 type constraint enforces the D33 filter even at this fallback.
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
# Per architecture §9.1 and F2 design §6:
#   is_a    → P31 (instance of), P279 (subclass of)
#   part_of → P131 (located in admin entity), P361 (part of),
#             P30  (continent — for country/region → continent chains),
#             P206 (located in body of water — for landmark → river/ocean),
#             P17  (country — for city → country fallback when P131 chain
#                   doesn't reach the country directly).
#
# Phase 10.5 Step 6 sub-cause B fix: P30 lets the walker verify
# "France ⊂ Europe" directly (France P30 Europe) and chains through
# P131 → P30 to verify city-to-continent ("Paris ⊂ Europe" via
# Paris P131 Île-de-France → ... → France P30 Europe). Pre-fix,
# part_of stopped at P131/P361 which deepest-runs out at the country
# level; many country→continent statements in Wikidata are only
# expressed via P30, so the chain truncated and the walker abstained
# on multi-hop geographic claims.
_SUBSUMPTION_PROPERTIES = {
    "is_a": ("P31", "P279"),
    "part_of": ("P131", "P361", "P30", "P206", "P17"),
}

# Phase H D5: default property set for KB neighbor enumeration. The
# geographic/taxonomic core covers the dominant derivation_corpus
# multi-hop shapes (X lives_in Y / part_of Z; X is_a Y / kind-properties).
# Other properties (P50 author, P108 employer, P39 position) are deferred
# to v0.16 driven by Phase 10.5 data per `docs/phase_H/d5_design.md`
# Decision 1.
_DEFAULT_NEIGHBOR_PROPERTIES = ("P31", "P279", "P361", "P131", "P17")


# Phase H D51: reverse enumeration's LIMIT. Unbounded properties like
# P17=Q30 (country=USA) have millions of subjects; the walker only needs
# a sample of candidate children for its substitution. 20 is the
# conservative default after the first D51 diagnostic showed the walker
# fanning out catastrophically at LIMIT=100 (per-case wall-clock blew
# past 18 min on der_multihop_002 before the run was killed): with
# LIMIT=100 a single reverse call returns 100 candidate children, the
# walker substitutes each into a new claim at depth+1, and each
# substituted claim fires more KB calls + more enumeration. 20 keeps
# fanout per call to ~20× rather than 100×, while still surfacing common
# children. Paired with the walker.py depth==0 cap on KB enumeration
# fallback (D51 step 3 cleanup) to bound aggregate walker cost.
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
      - "outgoing" (Phase H D5; default): `wd:E ?prop ?value` — yields
        E's parents (entities E points to). No LIMIT (outgoing edges
        are bounded by the predicate's schema; a few hits per property).
      - "incoming" (Phase H D51): `?value ?prop wd:E` — yields E's
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


def _build_subsumption_ask_query(
    source: KBEntityID, target: KBEntityID, relation_type: str
) -> str:
    """Build an ASK query: does `source` reach `target` via the
    relation_type's property alternation? Path-existence only — fast,
    short-circuits on first match. Used per direction by `_live_subsumption`."""
    if not _ENTITY_ID_PATTERN.match(source):
        raise ValueError(f"Invalid Wikidata entity ID: {source!r}")
    if not _ENTITY_ID_PATTERN.match(target):
        raise ValueError(f"Invalid Wikidata entity ID: {target!r}")
    props = _SUBSUMPTION_PROPERTIES.get(relation_type)
    if props is None:
        raise ValueError(
            f"Unsupported relation_type: {relation_type!r} "
            f"(expected one of {sorted(_SUBSUMPTION_PROPERTIES)})"
        )
    path = "|".join(f"wdt:{p}" for p in props)
    return f"ASK {{ wd:{source} ({path})+ wd:{target} . }}"


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


def _subsumption_verdict(a_to_b: bool, b_to_a: bool) -> str:
    """Map two directional ASK results to the SubsumptionResult verdict
    string. Architecture §6.2 enumerates the four verdicts; F2 design
    §6 specifies the truth table."""
    if a_to_b and b_to_a:
        return "equivalent"
    if a_to_b:
        return "a_subsumed_by_b"
    if b_to_a:
        return "b_subsumed_by_a"
    return "unrelated"


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
    """Phase G D33: extract P31 (instance-of) Q-ids from a wbgetentities
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


class WikidataAdapter:
    def __init__(
        self,
        http_cache=None,
        llm_client=None,
        db=None,
        config=None,
        fixture_dir: Optional[Path] = None,
    ) -> None:
        self._http = http_cache
        self._llm = llm_client
        self._db = db
        self._config = config
        self._fixture_dir = fixture_dir or _FIXTURE_DIR
        self._live = os.environ.get("RUN_LIVE_KB") == "1"

        # Rate limiters live as instance attributes (per F2 design Q3:
        # owning the state here keeps future lock-protection a small
        # change rather than a refactor of where the state lives).
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

    def wbsearchentities(
        self, query: str, limit: Optional[int] = None
    ) -> list[WBSearchCandidate]:
        """Phase H D53: raw wbsearchentities query.

        Returns ranked Wikidata entity candidates with full metadata
        (label, description, aliases, match info). Unlike
        `resolve_entity`, this method does NOT apply the D33 type
        filter — Stage C of the D53 normalizer flow runs the filter
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
        properties: list[KBPropertyID],
        direction: str = "outgoing",
    ) -> dict[KBPropertyID, list[KBEntityID]]:
        """Phase H D5/D51: enumerate `entity`'s direct KB neighbors along
        the given `properties`, in the given `direction`. Returns a dict
        keyed by property; each value is the list of neighbor entity Q-ids
        related to `entity` by that property in `direction`. Fails open
        (empty values) on error or no match; the audit log records the
        call's outcome either way.

        `properties` is a list (not a tuple) for KBProtocol parity with the
        other methods' input types — the live and fixture paths normalize
        it to a tuple before SPARQL construction.

        `direction` is "outgoing" (D5; default) or "incoming" (D51); see
        `KBProtocol.enumerate_neighbors` for the semantic distinction.
        """
        props_tuple = tuple(properties) if properties else _DEFAULT_NEIGHBOR_PROPERTIES
        if self._live:
            return self._live_neighbors(entity, props_tuple, direction)
        return self._fixture_neighbors(entity, props_tuple, direction)

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
        """Phase H D5/D51: fixture-backed enumeration. Reads
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

    # ------------------------------------------------------------------
    # Live stubs (populated in Phase 10.5 live run)
    # ------------------------------------------------------------------

    def _live_resolve(
        self, reference: str, local_context: LocalContext
    ) -> list[ResolutionCandidate]:
        """Resolve a natural-language reference to ranked Wikidata candidates
        via the `wbsearchentities` API.

        Honors the F2 design contract (§4):
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

        # Phase G D33: post-filter the candidate pool by P31 ∩ expected types.
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

        # Phase G D33 (2026-05-23, surfaced during live validation): the
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

        duration_ms = (time.monotonic() - start) * 1000.0
        self._log_audit_event(
            event_type="kb_live_resolve",
            event_subject=reference,
            event_data={
                "candidate_count": len(candidates),
                "duration_ms": round(duration_ms, 2),
                "retry_count": retries,
                "error": last_error,
                # Phase G D33 audit fields.
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
        """Phase G D33: SPARQL fallback when the wbsearchentities post-filter
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

    def _fetch_p31_for_candidates(
        self, candidate_ids: list[KBEntityID]
    ) -> tuple[dict[str, list[str]], Optional[str]]:
        """Phase G D33: batch-fetch P31 (instance-of) Q-ids for each candidate.

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

        Honors the F2 design contract (§5):
          - SPARQL endpoint = `Config.wikidata_sparql_endpoint`
            (default `https://query.wikidata.org/sparql`).
          - Returns `Statement` objects with rank, qualifiers (default set
            P580/P582/P642 per F2 §5), and provenance.
          - Direction-neutral: looks up whatever entity/predicate it is
            given. `KBVerifier` is responsible for swapping the lookup
            direction for inverse predicates (D19, fixup-3 resolution).
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

        Honors the F2 design contract (§6):
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
        """Phase H D5 + D51: live SPARQL enumeration of `entity`'s direct
        neighbors along `properties`, in the given `direction`. One
        round-trip, returns the parsed dict.

        Honors the D5/D51 design contracts (`docs/phase_H/d5_design.md`
        + `docs/v0.16_planning.md` D51 entry):
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
          - Reverse direction (D51): LIMIT bounds the query
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
