from __future__ import annotations

import json
import os
import re
import time
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

_FIXTURE_DIR = Path(__file__).parent.parent.parent.parent / "tests" / "fixtures" / "wikidata"
_DEPRECATED_RANK = "http://wikiba.se/ontology#DeprecatedRank"

# Defaults used when no Config is wired (test paths that construct
# WikidataAdapter directly without a Config object). Production paths
# come through build_pipeline which passes a Config.
_DEFAULT_SEARCH_ENDPOINT = "https://www.wikidata.org/w/api.php"
_DEFAULT_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
_DEFAULT_CANDIDATE_POOL_SIZE = 10
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

        duration_ms = (time.monotonic() - start) * 1000.0
        self._log_audit_event(
            event_type="kb_live_resolve",
            event_subject=reference,
            event_data={
                "candidate_count": len(candidates),
                "duration_ms": round(duration_ms, 2),
                "retry_count": retries,
                "error": last_error,
            },
        )
        return candidates

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
    ) -> SubsumptionResult:  # pragma: no cover
        raise NotImplementedError("Live KB calls require RUN_LIVE_KB=1 and a real HTTP client")
