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
from .layer3_substrate import Substrate
from .layer3_substrate.consistency import ConsistencyChecker
from .layer3_substrate.predicate_distribution import PredicateDistributionOracle
from .layer3_substrate.predicate_translation import PredicateTranslation
from .layer3_substrate.resolver import EntityResolver
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
        # F-004 closure: construct the live-ready Wikidata adapter with
        # HTTP cache and configuration. Adapter still runs in fixture mode
        # when RUN_LIVE_KB != 1 — only the wiring shape changes here.
        lru_cache = LRUHTTPCache(
            max_size=config.http_cache_lru_size,
            default_ttl_seconds=config.http_cache_entity_ttl_seconds,
        )
        http_client = CachingHTTPClient(
            cache=lru_cache,
            default_ttl_seconds=config.http_cache_entity_ttl_seconds,
            headers={"User-Agent": config.user_agent},
        )
        kb = WikidataAdapter(
            http_cache=http_client,
            llm_client=client,
            db=db,
            config=config,
        )

    propagator = RetractionPropagator(db=db)
    # D6: rehydrate the verdict-trace index from persisted verdict_recorded
    # events so retraction propagation survives process restarts (arch 7.3).
    propagator.replay()
    consistency = ConsistencyChecker(db=db, retraction_propagator=propagator)

    pt = PredicateTranslation(db=db, llm_client=client, consistency_checker=consistency)
    resolver = EntityResolver(kb_protocol=kb, db=db, llm_client=client)
    subsumption = SubsumptionOracle(
        db=db, llm_client=client, kb_protocol=kb, consistency_checker=consistency
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

    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    python_verifier = PythonVerifier(llm_client=client)
    walker = Walker(
        tier_u=tier_u,
        kb_verifier=kb_verifier,
        python_verifier=python_verifier,
        substrate=substrate,
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
    )
