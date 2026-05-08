"""Comparative / superlative claim detection + query templates (v0.7.9).

The retrieval verifier's per-pattern ``query_strategy`` builds queries
from {subject} {relation} {object}. For ordinary claims that's fine —
"Tokyo is the capital of Japan" → "Tokyo capital_of Japan" returns the
right Wikipedia article. For comparative / superlative claims it
breaks: "the Soviet Union had the heaviest losses of any nation in
WWII" extracts as ``relational.had_heaviest_losses(Soviet Union, WWII)``,
which produces "Soviet Union had_heaviest_losses World War II" — a
non-idiomatic query that surfaces battle pages, not the casualty-
ranking page that would actually settle the claim.

This module supplies a structural detector + comparative-aware query
templates that the retrieval verifier prepends when the detector
fires. Pure Python — no LLM call. False positives just expand the
query set; false negatives keep the existing behavior.

Anatomy of a comparative claim:
  * SUBJECT      — the entity being claimed at the extreme (Soviet Union)
  * SUPERLATIVE  — the comparison word (heaviest)
  * MEASURE      — what's being compared (losses, casualties)
  * DOMAIN       — the universe of comparison (World War II)

The detector reads ``predicate``, ``slots``, and ``source_text`` to
extract those four. If the predicate or source text contains one of
COMPARATIVE_MARKERS and the structure can be unambiguously decomposed,
we return a ComparativeClaim; otherwise None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# Superlative + comparative tokens that flag a ranked-comparison claim.
# Kept narrow on purpose — false negatives degrade gracefully (we just
# fall back to the standard query strategy); false positives only mean
# we add a few extra query attempts.
_SUPERLATIVES = (
    "most", "least", "fewest",
    "highest", "lowest",
    "largest", "smallest", "biggest",
    "heaviest", "lightest",
    "longest", "shortest", "tallest", "deepest", "widest",
    "fastest", "slowest",
    "best", "worst",
    "first", "earliest", "latest", "oldest", "newest",
    "greatest", "leading", "top", "bottom",
)

_COMPARATIVES = (
    "more_than", "less_than", "fewer_than",
    "greater_than", "higher_than", "lower_than",
    "all_time", "of_all", "by_country", "per_capita",
)

_PREDICATE_MARKERS = re.compile(
    r"\b(" + "|".join(_SUPERLATIVES + _COMPARATIVES) + r")\b",
    re.IGNORECASE,
)

# Source-text natural-language markers. These overlap with the
# predicate set but include phrasings that the predicate might lose
# during snake-case extraction ("by far the heaviest", "any nation").
_SOURCE_MARKERS = re.compile(
    r"\b(most|least|fewest|highest|lowest|largest|smallest|biggest|"
    r"heaviest|lightest|longest|shortest|tallest|deepest|widest|"
    r"fastest|slowest|best|worst|greatest|"
    r"the\s+only|by\s+far|of\s+all|of\s+any|per\s+capita|all-time|"
    r"all\s+time)\b",
    re.IGNORECASE,
)


@dataclass
class ComparativeClaim:
    """Decomposed comparative claim. All four fields are non-empty
    strings; the verifier formats them into query templates verbatim."""
    superlative: str    # "heaviest"
    measure: str        # "losses"
    domain: str         # "World War II"
    subject: str        # "Soviet Union"

    def to_dict(self) -> dict:
        return {
            "superlative": self.superlative,
            "measure": self.measure,
            "domain": self.domain,
            "subject": self.subject,
        }


def detect_comparative(claim: dict) -> Optional[ComparativeClaim]:
    """Return a ComparativeClaim if the claim asserts a ranked
    comparison ("X is the {superlative} Y in Z"), else None.

    Decomposition strategy (in order):
      1. Predicate of the form ``had_{SUPERLATIVE}_{MEASURE}``,
         ``has_the_{SUPERLATIVE}_{MEASURE}``, or
         ``is_the_{SUPERLATIVE}_{MEASURE}`` is the cleanest signal.
      2. Predicate matching the marker regex with adjacent token
         picked up as MEASURE.
      3. source_text natural-language phrasing as a backstop —
         only fires if a marker appears AND we can find a noun for
         the measure.

    All paths require we can supply SUBJECT (from slots: ``subject``,
    ``entity``, ``agent``) and DOMAIN (slots: ``object``, ``location``,
    ``domain``, ``role``). If either is missing, return None — we'd
    have nothing to query about.
    """
    predicate = (claim.get("predicate") or "").strip()
    source_text = (claim.get("source_text") or "").strip()
    slots = claim.get("slots") or {}

    pred_has_marker = bool(_PREDICATE_MARKERS.search(predicate.replace("_", " ")))
    src_has_marker = bool(_SOURCE_MARKERS.search(source_text))
    if not pred_has_marker and not src_has_marker:
        return None

    subject = _first_nonempty(slots, ("subject", "entity", "agent", "holder"))
    domain = _first_nonempty(slots, ("object", "domain", "location",
                                      "category", "role", "org", "target"))
    if not subject or not domain:
        return None

    superlative, measure = _decompose_predicate(predicate)
    if superlative is None or measure is None:
        # Predicate didn't decompose cleanly — try source_text fallback.
        sup_src, meas_src = _decompose_source_text(source_text)
        superlative = superlative or sup_src
        measure = measure or meas_src

    if not superlative or not measure:
        return None

    return ComparativeClaim(
        superlative=superlative.strip(),
        measure=measure.strip(),
        domain=str(domain).strip(),
        subject=str(subject).strip(),
    )


def comparative_queries(c: ComparativeClaim) -> list[str]:
    """Return the ordered list of query strings to try for a
    comparative claim. Templates lean on Wikipedia's actual page-
    naming conventions — "List of X by Y", "X by country", "X
    ranking" exist as actual articles for hundreds of measures.

    Order matters: most specific first. The retrieval verifier loops
    through these and uses the first attempt that returns ≥ 2
    results. The retry-on-inconclusive path then steps through the
    rest if the first attempt's snippets don't settle the claim.
    """
    s, m, d, subj = c.superlative, c.measure, c.domain, c.subject
    templates = [
        # Direct ranking / list pages.
        f"list of {d} {m}",
        f"{m} of {d} by country",
        f"{d} {m} by country",
        f"{d} {m} ranking",
        # Subject-anchored — narrows back to the entity, broader than
        # the relational template.
        f"{subj} {m} {d}",
        # Just the measure in the domain — broad but often hits the
        # canonical comparison page.
        f"{m} of {d}",
    ]
    # De-dupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for t in templates:
        norm = " ".join(t.split())
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


# ---- internals ----------------------------------------------------------


def _first_nonempty(slots: dict, names) -> str | None:
    for n in names:
        v = slots.get(n)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


_HAD_PREFIX_RE = re.compile(
    r"^(?:had|has|have|is|was|were|are)_(?:the_)?(?P<sup>"
    + "|".join(_SUPERLATIVES) + r")_(?P<meas>.+)$",
    re.IGNORECASE,
)
_SUFFIX_OF_RE = re.compile(
    r"^(?:had|has|have|is|was|were|are|have_the|has_the|had_the)_"
    r"(?P<meas>.+)_(?P<sup>"
    + "|".join(_SUPERLATIVES) + r")$",
    re.IGNORECASE,
)


def _decompose_predicate(predicate: str) -> tuple[str | None, str | None]:
    """Try to split a predicate into (superlative, measure)."""
    if not predicate:
        return None, None
    p = predicate.strip().lower()
    m = _HAD_PREFIX_RE.match(p)
    if m:
        return m.group("sup"), m.group("meas").replace("_", " ")
    m = _SUFFIX_OF_RE.match(p)
    if m:
        return m.group("sup"), m.group("meas").replace("_", " ")
    # Last-ditch: any superlative word anywhere in the predicate;
    # take everything else as the measure.
    for sup in _SUPERLATIVES:
        if re.search(rf"\b{sup}\b", p):
            measure = re.sub(rf"\b{sup}\b", " ", p).strip("_ ").replace("_", " ")
            measure = re.sub(
                r"^(had|has|have|is|was|were|are|the|of|in)\s+",
                "", measure,
            ).strip()
            if measure:
                return sup, measure
    return None, None


_SRC_NL_RE = re.compile(
    r"\b(?:the|by\s+far\s+the|by\s+far|of\s+any|of\s+all)?\s*"
    r"(?P<sup>" + "|".join(_SUPERLATIVES) + r")\s+"
    r"(?P<meas>[A-Za-z][A-Za-z\s]{0,40}?)"
    r"(?:\s+(?:of|in|among|across|from)\b|[.,;]|$)",
    re.IGNORECASE,
)


def _decompose_source_text(source_text: str) -> tuple[str | None, str | None]:
    """Backstop decomposition from natural language phrasing in
    source_text. Returns the first superlative + measure pair found."""
    if not source_text:
        return None, None
    m = _SRC_NL_RE.search(source_text)
    if not m:
        return None, None
    return m.group("sup").lower(), " ".join(m.group("meas").split()).lower()
