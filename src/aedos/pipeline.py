"""Shared production-pipeline construction for Aedos v0.15.

`build_pipeline` assembles the full verification pipeline — substrate oracles,
sources, the derivation walker, the aggregator — with the correctness
mechanisms (consistency checker + retraction propagator) wired in. Both the
chat-wrapper deployment (`app.py`) and the medium-bar benchmark
(`tests/evaluation/benchmark.py`) build their pipeline through this one
helper, so the wiring has a single definition rather than two drifting copies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .config import Config
from .layer1_extraction.extractor import Extractor
from .layer1_extraction.wikipedia_normalizer import WikipediaNormalizer
from .layer3_substrate import Substrate
from .layer3_substrate.consistency import ConsistencyChecker
from .layer3_substrate.predicate_distribution import PredicateDistributionOracle
from .layer3_substrate.predicate_translation import PredicateTranslation
from .layer3_substrate.property_relations import PropertyRelations
from .layer3_substrate.resolver import EntityResolver
from .layer3_substrate.sling_fallback import SlingFallback
from .layer3_substrate.substrate_exceptions import SubstrateExceptionCache
from .layer3_substrate.subsumption import SubsumptionOracle
from .layer4_sources.kb_verifier import KBVerifier
from .layer4_sources.kb_wikidata import WikidataAdapter
from .layer4_sources.python_verifier import PythonVerifier
from .layer4_sources.tier_u import TierU
from .layer4_sources.walker import Walker
from .layer5_result.aggregator import Aggregator
from .layer5_result.retraction import RetractionPropagator
from .llm.client import LLMClient
from .utils.http_cache import CachingHTTPClient, LRUHTTPCache


@dataclass
class Pipeline:
    """The assembled verification pipeline. Consumers pick the components they
    need — the benchmark uses extractor/walker/aggregator/llm_client; the
    chat-wrapper takes the same three through ChatWrapper."""

    db: Any
    llm_client: LLMClient
    kb: Any
    predicate_translation: PredicateTranslation
    resolver: EntityResolver
    subsumption: SubsumptionOracle
    predicate_distribution: PredicateDistributionOracle
    substrate: Substrate
    consistency: ConsistencyChecker
    propagator: RetractionPropagator
    tier_u: TierU
    kb_verifier: KBVerifier
    python_verifier: PythonVerifier
    walker: Walker
    extractor: Extractor
    aggregator: Aggregator
    wikipedia_normalizer: WikipediaNormalizer


def build_default_kb(db, llm_client: LLMClient, config: Config) -> WikidataAdapter:
    """Construct a fully-wired `WikidataAdapter` against `db`, `llm_client`,
    `config`. Used by `build_pipeline` for the default adapter and by the
    calibration harness (`tests/calibration/test_corpus_runner.py`) for its
    own pipeline construction.

    Extracted in the Phase F2 follow-up (F-039) — the calibration harness
    was originally constructing `WikidataAdapter()` with no arguments,
    bypassing the F2 wiring fix and hitting the live-methods' wiring-gap
    `RuntimeError` under `RUN_LIVE_KB=1`. Centralizing the construction
    here prevents *new* call sites from reintroducing the same defect.
    """
    lru_cache = LRUHTTPCache(
        max_size=config.http_cache_lru_size,
        default_ttl_seconds=config.http_cache_entity_ttl_seconds,
    )
    http_client = CachingHTTPClient(
        cache=lru_cache,
        default_ttl_seconds=config.http_cache_entity_ttl_seconds,
        headers={"User-Agent": config.user_agent},
    )
    return WikidataAdapter(
        http_cache=http_client,
        llm_client=llm_client,
        db=db,
        config=config,
    )


def build_pipeline(
    db,
    llm_client: Optional[LLMClient] = None,
    kb=None,
    config: Optional[Config] = None,
) -> Pipeline:
    """Assemble the full Aedos v0.15 verification pipeline against `db`.

    `llm_client`, `kb`, and `config` may be injected — mocks for harness
    validation, live instances otherwise. When `config` is None, builds a
    `Config.from_env()` instance.

    Wiring (F2): when `kb is None`, this constructor builds a
    `CachingHTTPClient` (User-Agent + LRU cache + TTL from `config`) and
    constructs `WikidataAdapter` with the full dependency set. That makes
    the deployed pipeline reach the live Wikidata API with the configured
    HTTP cache, rate limits, and identity — closing the F-004 wiring gap
    surfaced by the F1 audit.

    The correctness mechanisms are wired exactly as architecture 5.4 /
    7.3 require: the consistency checker runs on every oracle row write
    and shares the retraction propagator that the aggregator records
    verdict traces into. The entity resolver is wired with `llm_client`
    so LLM-mediated disambiguation of close-scoring candidates is
    available.
    """
    if config is None:
        config = Config.from_env()
    client = llm_client if llm_client is not None else LLMClient()
    if kb is None:
        # Construct the live-ready Wikidata adapter with
        # HTTP cache and configuration. Adapter still runs in fixture mode
        # when RUN_LIVE_KB != 1 — only the wiring shape changes here.
        kb = build_default_kb(db, client, config)

    propagator = RetractionPropagator(db=db)
    # Rehydrate the verdict-trace index from persisted verdict_recorded
    # events so retraction propagation survives process restarts (arch 7.3).
    propagator.replay()

    # v0.16 WS3 §3D: the bounded nogood cache. Wired into the KB adapter
    # (verify_transitive_path consult/record), the subsumption oracle, the
    # walker (_nogood_vetoes), and the kb_verifier (binding-loop veto). Discovered
    # nogoods only — never seeded; an absent/flaky cache fails open everywhere.
    exception_cache = SubstrateExceptionCache(db)
    # Attribute injection keeps the adapter constructor (build_default_kb /
    # injected mocks) unchanged; verify_transitive_path reads self._exception_cache.
    if hasattr(kb, "_exception_cache"):
        kb._exception_cache = exception_cache
    consistency = ConsistencyChecker(
        db=db,
        retraction_propagator=propagator,
        # Thread circuit-breaker threshold through Config.
        circuit_breaker_threshold=config.circuit_breaker_threshold,
    )

    # v0.16 WS1: wire binding-discovery collaborators. `kb` was built above
    # (build_default_kb / injected), so the ordering holds. Both collaborators
    # fail open, so a mock kb (no fetch_property_ontology / enumerate_neighbors)
    # simply yields the oracle's single primary binding = pre-v0.16 behavior.
    pt = PredicateTranslation(
        db=db,
        llm_client=client,
        consistency_checker=consistency,
        property_relations=PropertyRelations(db, kb),
        sling=SlingFallback(db, kb, client),
    )

    # Wikipedia normalizer wired into the resolver. The
    # normalizer shares the HTTP cache layer that build_default_kb already
    # set up (CachingHTTPClient with User-Agent + LRU + TTL from Config);
    # we reuse `kb._http` to avoid building a second cache. When the
    # caller injects a mock `kb` without `_http`, fall back to a fresh
    # CachingHTTPClient — keeps test paths working without forcing every
    # mock to expose an http_cache attribute.
    normalizer_http = getattr(kb, "_http", None)
    if normalizer_http is None and config.wikipedia_normalizer_enabled:
        normalizer_lru = LRUHTTPCache(
            max_size=config.http_cache_lru_size,
            default_ttl_seconds=config.http_cache_entity_ttl_seconds,
        )
        normalizer_http = CachingHTTPClient(
            cache=normalizer_lru,
            default_ttl_seconds=config.http_cache_entity_ttl_seconds,
            headers={"User-Agent": config.user_agent},
        )
    wikipedia_normalizer = WikipediaNormalizer(
        http_cache=normalizer_http,
        llm_client=client,
        db=db,
        config=config,
        # Hand the KB adapter to the normalizer so
        # Stage B can call wbsearchentities and Stage C can call the
        # batched P31 type-filter fetch.
        kb_adapter=kb,
    ) if config.wikipedia_normalizer_enabled else None

    resolver = EntityResolver(
        kb_protocol=kb, db=db, llm_client=client,
        wikipedia_normalizer=wikipedia_normalizer,
    )
    subsumption = SubsumptionOracle(
        db=db, llm_client=client, kb_protocol=kb, consistency_checker=consistency,
        exception_cache=exception_cache,
    )
    distribution = PredicateDistributionOracle(
        db=db, llm_client=client, consistency_checker=consistency
    )
    substrate = Substrate(
        resolver=resolver,
        predicate_translation=pt,
        subsumption=subsumption,
        predicate_distribution=distribution,
    )

    tier_u = TierU(
        db=db,
        predicate_translation=pt,
        wikipedia_normalizer=wikipedia_normalizer,
        # v0.16 WS3 §3E: the premise-retraction entry point — a closed/retracted
        # Tier U premise marks dependent *_given_assertion verdicts stale.
        retraction_propagator=propagator,
    )
    kb_verifier = KBVerifier(
        kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt,
        exception_cache=exception_cache,
    )
    python_verifier = PythonVerifier(llm_client=client)
    walker = Walker(
        tier_u=tier_u,
        kb_verifier=kb_verifier,
        python_verifier=python_verifier,
        substrate=substrate,
        # Thread walker budgets / depth through Config.
        walker_wall_clock_seconds=config.walker_wall_clock_seconds,
        walker_max_llm_calls=config.walker_max_llm_calls,
        walker_max_depth=config.walker_max_depth,
        # Thread the KB adapter explicitly so the walker can
        # call `enumerate_neighbors` as a fallback when substrate-cached
        # subsumption is empty.
        kb=kb,
        # v0.16 WS3 §3D: the bounded nogood cache for _nogood_vetoes.
        exception_cache=exception_cache,
    )
    extractor = Extractor(llm_client=client)
    aggregator = Aggregator(retraction_propagator=propagator, db=db)

    return Pipeline(
        db=db,
        llm_client=client,
        kb=kb,
        predicate_translation=pt,
        resolver=resolver,
        subsumption=subsumption,
        predicate_distribution=distribution,
        substrate=substrate,
        consistency=consistency,
        propagator=propagator,
        tier_u=tier_u,
        kb_verifier=kb_verifier,
        python_verifier=python_verifier,
        walker=walker,
        extractor=extractor,
        aggregator=aggregator,
        wikipedia_normalizer=wikipedia_normalizer,
    )
