from __future__ import annotations

from dataclasses import dataclass

from .predicate_distribution import PredicateDistributionOracle
from .predicate_translation import PredicateTranslation
from .resolver import EntityResolver
from .subsumption import SubsumptionOracle


@dataclass
class Substrate:
    """Facade for all four Layer 3 substrate components."""
    resolver: EntityResolver
    predicate_translation: PredicateTranslation
    subsumption: SubsumptionOracle
    predicate_distribution: PredicateDistributionOracle
