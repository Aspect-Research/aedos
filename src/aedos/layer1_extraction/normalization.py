from __future__ import annotations

import re

# v0.16 WS1 (Decision 1.g): the surface→canonical synonym map was DELETED.
# Predicate synonymy is no longer a hardcoded lookup table — it is carried by
# the substrate's multi-property binding discovery (Wikidata-ontology +
# SLING) and the seed pack's canonical rows. `normalize_predicate` now keeps
# ONLY mechanical normalization (lower/strip, underscore↔space, single
# aux-prefix strip, trailing-article strip, snake_case), consistent with the
# project's "no hardcoded mappings" invariant: a surface form like "works at"
# normalizes mechanically to `works_at` and the oracle's cold-start discovery
# resolves it to its KB property (e.g. P108), rather than the map collapsing
# it to `employed_by` up front.

_AUX_PREFIX = re.compile(
    r"^(is|was|were|has|have|had|will|would|does|did)\s+",
    re.IGNORECASE,
)


def normalize_predicate(raw: str) -> str:
    """Normalize a predicate to canonical snake_case, tense-neutral,
    voice-neutral form via MECHANICAL transforms only (v0.16 WS1).

    Steps: lower/strip → underscores treated as spaces → strip a single
    leading auxiliary verb → strip a trailing definite/indefinite article →
    snake_case. There is no longer a synonym lookup table: e.g. "works at"
    → `works_at` (not `employed_by`); the substrate's binding discovery and
    the seed pack's canonical rows now carry the synonymy.
    """
    stripped = raw.strip().lower()
    if not stripped:
        return "unknown_predicate"

    # Treat underscores as equivalent to spaces so an extractor that emits
    # `works_at` and one that emits "works at" normalize identically.
    space_form = stripped.replace("_", " ")

    # Strip a leading auxiliary verb once ("is employed by" → "employed by").
    no_aux = _AUX_PREFIX.sub("", space_form).strip()
    if no_aux:
        space_form = no_aux

    # Remove trailing definite/indefinite article
    space_form = re.sub(r"\s+(the|a|an)$", "", space_form)

    # snake_case: remove non-word/non-space chars, collapse spaces to underscores
    result = re.sub(r"[^\w\s]", "_", space_form)
    result = re.sub(r"\s+", "_", result.strip())
    result = re.sub(r"_+", "_", result).strip("_")

    return result or "unknown_predicate"
