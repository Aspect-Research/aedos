"""FastAPI application for the v0.14 stack (post-cutover root).

Phase 9d cutover: this is the v0.14 root app, mounted at ``/``. The
v2 health, extract, dispatch-one, trace, facts, routing-memo, and
the four substrate inspectors all live here. ``/static`` serves the
trace UI assets; ``/`` serves ``static/index.html``.

Default DB path is ``aedos.db`` (override via ``AEDOS_DB_PATH``).
v0.13 working-copy databases (incompatible v0.13 schema) should be
renamed to ``aedos_v1.db`` if preserved — see CLAUDE.md.

The v0.15 trajectory adds ``/api/chat`` + SSE streaming. v0.14
operators drive the v2 stack via ``/api/dispatch-one`` (structured
claim) and the inspector endpoints.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import Response


load_dotenv()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eagerly construct the store so the schema is in place before
    # the first request, mirroring v1's lifespan pattern.
    _get_store()
    try:
        yield
    finally:
        store = _store_singleton
        if store is not None:
            store.close()


app = FastAPI(title="Aedos", version="0.14.0", lifespan=lifespan)


@app.middleware("http")
async def _no_cache_api(request, call_next):
    """No-cache headers for /api/* responses so inspector reloads
    after a Reset DB show fresh state."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = (
            "no-cache, no-store, must-revalidate"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ---- shared singletons -----------------------------------------------------
#
# The v2 stack uses ``aedos_v2.db`` as its store. Endpoints that need
# the store fetch it through ``_get_store``; the FactStore is built
# lazily on first request so app import doesn't touch SQLite (matches
# the lazy-extractor pattern below).

_store_singleton: Any = None
_routing_memo_singleton: Any = None
_predicate_equivalence_singleton: Any = None
_entity_equivalence_singleton: Any = None
_entity_taxonomy_singleton: Any = None
_predicate_distribution_singleton: Any = None


def _get_store():
    global _store_singleton
    if _store_singleton is None:
        from src.fact_store import FactStore
        _store_singleton = FactStore(os.getenv("AEDOS_DB_PATH", "aedos.db"))
    return _store_singleton


def _get_routing_memo():
    global _routing_memo_singleton
    if _routing_memo_singleton is None:
        from src.layer2_routing.routing_memo import RoutingMemo
        _routing_memo_singleton = RoutingMemo(_get_store())
    return _routing_memo_singleton


def _get_predicate_equivalence():
    global _predicate_equivalence_singleton
    if _predicate_equivalence_singleton is None:
        from src.layer3_substrate.predicate_equivalence import (
            PredicateEquivalence,
        )
        _predicate_equivalence_singleton = PredicateEquivalence(
            _get_store(),
        )
    return _predicate_equivalence_singleton


def _get_entity_equivalence():
    global _entity_equivalence_singleton
    if _entity_equivalence_singleton is None:
        from src.layer3_substrate.entity_equivalence import (
            EntityEquivalence,
        )
        _entity_equivalence_singleton = EntityEquivalence(_get_store())
    return _entity_equivalence_singleton


def _get_entity_taxonomy():
    global _entity_taxonomy_singleton
    if _entity_taxonomy_singleton is None:
        from src.layer3_substrate.entity_taxonomy import (
            EntityTaxonomy,
        )
        _entity_taxonomy_singleton = EntityTaxonomy(_get_store())
    return _entity_taxonomy_singleton


def _get_predicate_distribution():
    global _predicate_distribution_singleton
    if _predicate_distribution_singleton is None:
        from src.layer3_substrate.predicate_distribution import (
            PredicateDistribution,
        )
        _predicate_distribution_singleton = PredicateDistribution(
            _get_store(),
        )
    return _predicate_distribution_singleton


def _set_store(store: Any) -> None:
    """Test hook: inject a tmp_path-backed FactStore so endpoint tests
    don't touch the real ``aedos_v2.db``. Production code never calls
    this. Dependent singletons are reset alongside so subsequent
    calls rebuild against the injected store."""
    global _store_singleton, _routing_memo_singleton
    global _predicate_equivalence_singleton, _entity_equivalence_singleton
    global _entity_taxonomy_singleton, _predicate_distribution_singleton
    _store_singleton = store
    _routing_memo_singleton = None
    _predicate_equivalence_singleton = None
    _entity_equivalence_singleton = None
    _entity_taxonomy_singleton = None
    _predicate_distribution_singleton = None


@app.get("/health")
def health() -> dict[str, Any]:
    """Phase 0 scaffold marker. Returns the stack version + status so
    a smoke test can confirm the v2 mount is wired correctly."""
    return {"version": "0.14-dev", "status": "scaffold"}


# ---- Phase 1: extraction endpoint ----------------------------------------
#
# Lazy-instantiated singletons. The first call to /api/extract pays the
# import + LLM-client construction cost; subsequent calls reuse the
# extractor. Module-level instantiation would force the LLM client
# (and thus the API key) at app import time, which is wrong for tests
# and for environments that import the app for inspection.

_extractor_singleton: Any = None


def _get_extractor() -> Any:
    """Build the v0.14 ClaimExtractor on first use, cache it.

    Imports happen inside the function so a missing optional
    dependency (e.g. `anthropic` SDK in a constrained test env)
    only fails when /api/extract is actually hit, not at app
    import time.
    """
    global _extractor_singleton
    if _extractor_singleton is None:
        from src.layer1_extraction.extractor import ClaimExtractor
        from src.layer1_extraction.pattern_registry import (
            load_default_registry,
        )
        from src.llm_client import LLMClient

        _extractor_singleton = ClaimExtractor(LLMClient(), load_default_registry())
    return _extractor_singleton


def _set_extractor(extractor: Any) -> None:
    """Test hook: inject a mock extractor without touching LLMClient.

    The endpoint test uses this to substitute a fake extractor whose
    LLM is a stub. Production code never calls this.
    """
    global _extractor_singleton
    _extractor_singleton = extractor


class ExtractRequest(BaseModel):
    text: str = Field(..., description="The text to extract facts from.")
    role: str = Field(
        ...,
        description="'user' or 'assistant' — the speaker whose text this is.",
    )
    context: str | None = Field(
        None,
        description=(
            "Optional preceding speaker's message used only to resolve "
            "self-references like 'this sentence' / 'the word you gave me'. "
            "Facts are extracted from `text`, never from `context`."
        ),
    )


@app.post("/api/extract")
def extract(req: ExtractRequest) -> dict[str, Any]:
    """Extract structured facts from a single piece of text.

    Returns the v0.14 extractor's full result dict — valid_facts,
    rejected_facts, and any substitution warnings — so smoke-corpus
    consumers can assert on the structure.

    This is a thin HTTP wrapper around ClaimExtractor.extract. It
    does NOT route, verify, store, or correct. Phase 2+ wires the
    full pipeline behind /api/chat.
    """
    if req.role not in ("user", "assistant"):
        raise HTTPException(
            status_code=400,
            detail=f"role must be 'user' or 'assistant', got {req.role!r}",
        )
    extractor = _get_extractor()
    result = extractor.extract(req.text, role=req.role, context=req.context)
    return result.to_dict()


# ---- Phase 2: routing memo inspector --------------------------------------
#
# Read-only endpoints over the routing_memo table. The trace UI lists
# rows on /v2/api/routing-memo and inspects single (pattern, predicate)
# pairs on /v2/api/routing-memo/{pattern}/{predicate}. No write
# endpoints in Phase 2 — counts only ever change via operator-action
# endpoints which arrive in Phase 8 with the substrate inspectors.


@app.get("/api/routing-memo")
def list_routing_memo() -> dict[str, Any]:
    """List every memo row, sorted by (pattern, predicate).

    Returns ``{"rows": [...]}`` so the operator UI can render a table.
    Each row carries pattern, predicate, method, reason, counts, and
    the created/last-consulted timestamps.
    """
    memo = _get_routing_memo()
    return {"rows": [r.to_dict() for r in memo.list_all()]}


@app.get("/api/routing-memo/{pattern}/{predicate}")
def get_routing_memo_entry(pattern: str, predicate: str) -> dict[str, Any]:
    """Inspect a single (pattern, predicate) memo row.

    404 if the row doesn't exist — used by the per-claim trace UI to
    decide whether to show "served from memo" or "first time we've
    seen this (pattern, predicate)".
    """
    memo = _get_routing_memo()
    entry = memo.lookup(pattern, predicate)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no routing memo for ({pattern!r}, {predicate!r})"
            ),
        )
    return entry.to_dict()


# ---- Phase 3: predicate_equivalence inspector -----------------------------
#
# Read-only endpoints over the predicate_equivalence table. The trace
# UI lists rows on /v2/api/substrate/predicate-equivalence (with an
# optional ?pattern= filter) and inspects single triples on
# /v2/api/substrate/predicate-equivalence/{pattern}/{a}/{b}.
#
# No write endpoints in Phase 3. Phase 8's operator-action inspector
# adds re-judgment endpoints that increment the counts; reads still
# go here.


@app.get("/api/substrate/predicate-equivalence")
def list_predicate_equivalence(
    pattern: str | None = None,
) -> dict[str, Any]:
    """List every predicate_equivalence row, optionally filtered by
    pattern. Sorted by (pattern, predicate_a, predicate_b).
    """
    oracle = _get_predicate_equivalence()
    rows = oracle.list_rows(pattern=pattern)
    return {"rows": [r.to_dict() for r in rows]}


@app.get(
    "/api/substrate/predicate-equivalence/"
    "{pattern}/{predicate_a}/{predicate_b}"
)
def get_predicate_equivalence_entry(
    pattern: str, predicate_a: str, predicate_b: str,
) -> dict[str, Any]:
    """Inspect a single (pattern, p_a, p_b) row.

    The lookup is order-invariant — calling with the predicates in
    either lex order returns the same row. 404 if the canonical
    pair has not been classified yet. Self-pairs (predicate_a equals
    predicate_b after normalization) raise 400; the oracle does not
    classify self-pairs by construction.
    """
    oracle = _get_predicate_equivalence()
    try:
        row = oracle.lookup(pattern, predicate_a, predicate_b)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no predicate_equivalence row for "
                f"({pattern!r}, {predicate_a!r}, {predicate_b!r})"
            ),
        )
    return row.to_dict()


# ---- Phase 4: entity_equivalence inspector ---------------------------------
#
# Read-only endpoints over the entity_equivalence table. The trace UI
# lists rows on /v2/api/substrate/entity-equivalence and inspects
# single pairs on /v2/api/substrate/entity-equivalence/{a}/{b}. The
# entity oracle is pattern-independent so there's no pattern filter
# (unlike the predicate-equivalence list endpoint).


@app.get("/api/substrate/entity-equivalence")
def list_entity_equivalence() -> dict[str, Any]:
    """List every entity_equivalence row, sorted by (entity_a,
    entity_b)."""
    oracle = _get_entity_equivalence()
    rows = oracle.list_rows()
    return {"rows": [r.to_dict() for r in rows]}


@app.get("/api/substrate/entity-equivalence/{entity_a}/{entity_b}")
def get_entity_equivalence_entry(
    entity_a: str, entity_b: str,
) -> dict[str, Any]:
    """Inspect a single (entity_a, entity_b) row.

    Order-invariant — the canonical helper handles the lex-swap
    internally. 404 if the canonical pair has not been classified
    yet. Self-pairs raise 400 (the oracle does not classify them).
    """
    oracle = _get_entity_equivalence()
    try:
        row = oracle.lookup(entity_a, entity_b)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no entity_equivalence row for "
                f"({entity_a!r}, {entity_b!r})"
            ),
        )
    return row.to_dict()


# ---- Phase 5: entity_taxonomy inspector -----------------------------------
#
# Read-only endpoints over the entity_taxonomy table. The trace UI
# lists rows on /v2/api/substrate/entity-taxonomy (with optional
# ?relation_type= filter) and inspects single triples on
# /v2/api/substrate/entity-taxonomy/{child}/{parent}/{relation_type}.
# DIRECTIONAL — calling with (child, parent) and (parent, child) of
# the same pair returns DIFFERENT rows (or 404s on the missing
# direction); there is no canonical-pair swap.


@app.get("/api/substrate/entity-taxonomy")
def list_entity_taxonomy(
    relation_type: str | None = None,
) -> dict[str, Any]:
    """List every entity_taxonomy row, optionally filtered by
    relation_type. Sorted by (relation_type, child, parent).
    """
    oracle = _get_entity_taxonomy()
    try:
        rows = oracle.list_rows(relation_type=relation_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"rows": [r.to_dict() for r in rows]}


@app.get(
    "/api/substrate/entity-taxonomy/"
    "{child}/{parent}/{relation_type}"
)
def get_entity_taxonomy_entry(
    child: str, parent: str, relation_type: str,
) -> dict[str, Any]:
    """Inspect a single (child, parent, relation_type) row.

    NOT order-invariant — the (child, parent) ordering is positional
    and meaningful. 404 if the exact triple has not been classified.
    Self-pairs (child == parent) and unknown relation_types raise
    400.
    """
    oracle = _get_entity_taxonomy()
    try:
        row = oracle.lookup(child, parent, relation_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no entity_taxonomy row for "
                f"({child!r}, {parent!r}, {relation_type!r})"
            ),
        )
    return row.to_dict()


# ---- Phase 5: predicate_distribution inspector ----------------------------
#
# Read-only endpoints over the predicate_distribution table. List
# supports ?pattern= and ?polarity= filters (both optional, can
# combine). The single-entry endpoint takes the full 4-tuple as path
# parameters.


@app.get("/api/substrate/predicate-distribution")
def list_predicate_distribution(
    pattern: str | None = None,
    polarity: int | None = None,
) -> dict[str, Any]:
    """List every predicate_distribution row, optionally filtered.
    Sorted by (pattern, predicate, polarity, taxonomy_relation_type).
    """
    oracle = _get_predicate_distribution()
    try:
        rows = oracle.list_rows(pattern=pattern, polarity=polarity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"rows": [r.to_dict() for r in rows]}


@app.get(
    "/api/substrate/predicate-distribution/"
    "{pattern}/{predicate}/{polarity}/{relation_type}"
)
def get_predicate_distribution_entry(
    pattern: str,
    predicate: str,
    polarity: int,
    relation_type: str,
) -> dict[str, Any]:
    """Inspect a single (pattern, predicate, polarity,
    taxonomy_relation_type) row.

    Predicate is normalized (lowercase + strip) before lookup so the
    URL can use either case. Polarity must be 0 or 1; relation_type
    must be is_a or part_of (both raise 400 otherwise). 404 if the
    exact 4-tuple has not been classified.
    """
    oracle = _get_predicate_distribution()
    try:
        row = oracle.lookup(
            pattern, predicate, polarity, relation_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no predicate_distribution row for "
                f"({pattern!r}, {predicate!r}, {polarity!r}, "
                f"{relation_type!r})"
            ),
        )
    return row.to_dict()


# ---- Phase 8: operator-action endpoints ----------------------------------
#
# The ONLY code paths that increment oracle row counts (architecture
# principle 3: mutation discipline). Each request is one operator
# click = one independent external evidence event; NOT idempotent.
# The operator UI debounces duplicate submissions. Programmatic callers
# should read the returned counts to confirm their increment landed.
#
# URL shape: /v2/api/substrate/{oracle-slug}/{row_id}/{action}.
# Slug uses dashes for URL aesthetics; the helper canonicalizes back to
# the table name (predicate-equivalence -> predicate_equivalence).
#
# Each endpoint emits a pipeline_events row (oracle_affirmed /
# oracle_contradicted) for audit. Events have turn_id=NULL because
# operator actions are off-turn — the ``insert_pipeline_event`` API
# requires an int, so we use a synthetic value of 0 to indicate
# "not associated with a chat turn." The trace UI's per-turn view
# filters out turn_id=0; an admin view shows the operator-action log
# separately.


_ORACLE_SLUG_TO_NAME: dict[str, str] = {
    "predicate-equivalence": "predicate_equivalence",
    "entity-equivalence": "entity_equivalence",
    "entity-taxonomy": "entity_taxonomy",
    "predicate-distribution": "predicate_distribution",
}


def _resolve_oracle_slug(slug: str) -> str:
    """Map URL slug (dashes) to table/oracle name (underscores). 400 on
    unknown slug."""
    if slug not in _ORACLE_SLUG_TO_NAME:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown oracle slug {slug!r}; expected one of "
                f"{sorted(_ORACLE_SLUG_TO_NAME)}"
            ),
        )
    return _ORACLE_SLUG_TO_NAME[slug]


def _emit_operator_action(
    store: Any, stage: str, payload: dict,
) -> None:
    """Best-effort audit log for an operator action.

    Uses a synthetic turn_id of 0 (the operator action is off-turn).
    Failures are swallowed — the count update is the load-bearing
    write; the audit trail is observability."""
    try:
        store.insert_pipeline_event(0, stage, payload)
    except Exception:
        pass


@app.post("/api/substrate/{oracle_slug}/{row_id}/affirm")
def affirm_oracle_row_endpoint(
    oracle_slug: str, row_id: int,
) -> dict[str, Any]:
    """Increment ``affirmed_count`` on an oracle row by 1.

    Returns ``{oracle, row_id, affirmed_count, contradicted_count,
    confidence}`` after the increment. The operator UI reads this
    response to update the displayed reliability score.

    NOT idempotent: each request increments by 1. The operator UI is
    responsible for debouncing duplicate clicks; programmatic callers
    should compare the returned ``affirmed_count`` to a snapshot
    before/after to confirm the increment landed.

    404 on missing row; 400 on unknown oracle slug.
    """
    from src.layer3_substrate.classifier_base import (
        affirm_oracle_row,
    )

    oracle_name = _resolve_oracle_slug(oracle_slug)
    store = _get_store()
    try:
        result = affirm_oracle_row(store, oracle_name, row_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    _emit_operator_action(store, "oracle_affirmed", result)
    return result


@app.post("/api/substrate/{oracle_slug}/{row_id}/contradict")
def contradict_oracle_row_endpoint(
    oracle_slug: str, row_id: int,
) -> dict[str, Any]:
    """Increment ``contradicted_count`` on an oracle row by 1.

    Mirror of ``affirm_oracle_row_endpoint`` for the dispute path.
    Same idempotency contract: each request increments by 1.

    404 on missing row; 400 on unknown oracle slug.
    """
    from src.layer3_substrate.classifier_base import (
        contradict_oracle_row,
    )

    oracle_name = _resolve_oracle_slug(oracle_slug)
    store = _get_store()
    try:
        result = contradict_oracle_row(store, oracle_name, row_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    _emit_operator_action(store, "oracle_contradicted", result)
    return result


# ---- Phase 8.5: dispatch-one endpoint -------------------------------------
#
# Drives Layer 2 routing → walker (U → W → derivation → fresh) → Layer 5
# (decision_confidence + intervention) for a single structured claim. The
# trace UI uses this to surface the WalkerDecision and Intervention while
# /v2/api/chat is still a Phase 9 deliverable. Skips Layer 1 (extraction)
# and the chat-model call: the caller hands us a structured claim dict
# directly.
#
# Each request creates one synthetic turn so pipeline events have a
# turn_id to attach to. The response includes the turn_id so the trace
# UI can re-fetch events via /v2/api/trace/{turn_id} once Phase 9 wires
# that endpoint; for now the dispatch response inlines the events list.
#
# fresh dispatch is opt-in via run_fresh=true (default false): without
# an LLM key in the test environment the verifier stack errors. Tests
# of derivation/U/W resolution leave it false.


class DispatchClaim(BaseModel):
    pattern: str = Field(..., description="One of the 9 pattern names.")
    predicate: str = Field(..., description="Free-form predicate within the pattern.")
    polarity: int = Field(1, description="0 (negated) or 1 (asserted).")
    slots: dict[str, Any] = Field(default_factory=dict)
    source_text: str | None = None


class DispatchOneRequest(BaseModel):
    claim: DispatchClaim
    current_session: str | None = Field(
        None,
        description=(
            "Session id used to filter Tier U session-locality. None "
            "means cross-session view."
        ),
    )
    user_id: str = Field("default_user")
    run_fresh: bool = Field(
        False,
        description=(
            "When true, fall through to the fresh verifier dispatcher "
            "after Tier U / W / derivation miss. Requires an LLM client "
            "in the environment; off by default so the trace UI works "
            "without API keys."
        ),
    )


@app.post("/api/dispatch-one")
def dispatch_one(req: DispatchOneRequest) -> dict[str, Any]:
    """Run one structured claim through Layer 2 → walker → Layer 5.

    Returns the WalkerDecision, the planned Intervention, the
    DecisionConfidence, the synthetic ``turn_id`` the events were
    written under, and the events list for that turn.

    Layer 1 (extraction) is bypassed — the caller hands us a structured
    claim. The response inlines the pipeline events so the trace UI
    works without a /v2/api/trace endpoint (Phase 9 territory).
    """
    from src.layer1_extraction.pattern_registry import (
        load_default_registry,
    )
    from src.layer2_routing.router import Router
    from src.layer4_lookup import fresh as _fresh
    from src.layer4_lookup.walker import walk_claim
    from src.layer5_decision.confidence import (
        compute_decision_confidence,
        get_threshold,
    )
    from src.layer5_decision.intervention import plan_intervention

    claim_dict = {
        "pattern": req.claim.pattern,
        "predicate": req.claim.predicate,
        "polarity": req.claim.polarity,
        "slots": dict(req.claim.slots),
    }
    if req.claim.source_text is not None:
        claim_dict["source_text"] = req.claim.source_text

    store = _get_store()
    registry = load_default_registry()

    # Synthetic turn so pipeline events have a turn_id to attach to.
    # role='assistant' because the dispatcher emulates the model-claim
    # verification path; Layer 1 / extraction is bypassed.
    turn_id = store.insert_turn(
        role="assistant",
        content=req.claim.source_text or "",
        user_id=req.user_id,
    )

    # Optional LLM client. Built lazily so tests without an API key can
    # still hit dispatch-one for U/W/derivation paths (which only call
    # the LLM on cold oracle cells, and the lookup-first contract makes
    # cold cells skip cleanly under llm=None).
    llm = None
    if req.run_fresh:
        try:
            from src.llm_client import LLMClient
            llm = LLMClient()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"run_fresh=true but LLMClient unavailable: {exc}",
            )

    router = Router(store, registry, llm=llm)
    layer2_decision = router.classify(claim_dict, source_turn_id=turn_id)

    walker_decision = walk_claim(
        claim_dict, layer2_decision, store,
        registry=registry,
        predicate_oracle=_get_predicate_equivalence(),
        entity_oracle=_get_entity_equivalence(),
        taxonomy_oracle=_get_entity_taxonomy(),
        distribution_oracle=_get_predicate_distribution(),
        llm=llm,
        source_turn_id=turn_id,
        user_id=req.user_id,
        current_session=req.current_session,
        fresh_dispatch=_fresh.dispatch if req.run_fresh else None,
    )

    decision_confidence = compute_decision_confidence(
        walker_decision, store=store,
    )
    intervention = plan_intervention(
        walker_decision, decision_confidence, store=store,
    )

    events = store.get_pipeline_events(turn_id)

    return {
        "turn_id": turn_id,
        "threshold": get_threshold(),
        "layer2_decision": layer2_decision.to_dict(),
        "walker_decision": walker_decision.to_dict(),
        "decision_confidence": decision_confidence.to_dict(),
        "intervention": intervention.to_dict(),
        "events": events,
    }


# ---- Phase 8.5: per-turn pipeline events --------------------------------
#
# Re-exposes the existing fact_store query so the trace UI can re-fetch
# a turn's events after dispatch (or, in Phase 9, after a real chat
# turn) without round-tripping through dispatch_one.


@app.get("/api/trace/{turn_id}")
def get_trace(turn_id: int) -> dict[str, Any]:
    """Return the pipeline events for a single turn, in arrival order."""
    store = _get_store()
    return {
        "turn_id": turn_id,
        "events": store.get_pipeline_events(turn_id),
    }


# ---- DB reset --------------------------------------------------------------


@app.post("/api/reset")
def reset() -> dict[str, bool]:
    """Wipe the v0.14 store and recreate the schema. UI's Reset DB
    button hits this endpoint."""
    store = _get_store()
    store.reset()
    # Reset the dependent singletons so they rebuild against the
    # fresh schema on the next request.
    _set_store(store)
    return {"ok": True}


# ---- static UI -------------------------------------------------------------


class _NoCacheStaticFiles(StaticFiles):
    """No-cache headers on static assets — single-developer dogfooding
    means UI changes get pushed by editing static/* files in place."""

    async def get_response(self, path, scope):
        response: Response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = (
            "no-cache, no-store, must-revalidate"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


app.mount(
    "/static", _NoCacheStaticFiles(directory=str(_STATIC_DIR)), name="static",
)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        _STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.app:app",
        host="127.0.0.1",
        port=int(os.getenv("AEDOS_PORT", "8000")),
        reload=False,
    )
