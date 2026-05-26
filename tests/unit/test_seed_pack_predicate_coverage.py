"""Phase G D39 structural test: every predicate referenced by a calibration
corpus is either in `seeds/predicate_translation.json` (the seed pack) OR
documented as expected-cold-start.

The test discriminates two predicate kinds:

1. **Cold-start corpora** — corpora whose *purpose* is testing the cold-start
   generation path itself. Their predicates are intentionally absent from the
   seed pack; the corpus checks whether the substrate oracles can generate
   correct metadata when no seed exists. These corpora are listed in
   `_COLD_START_CORPORA`.

2. **Reference corpora** — every other calibration corpus. These exercise the
   pipeline through predicates the deployed system would commonly encounter.
   Their predicates *should* be in the seed pack, since the corpus's purpose
   is to test the pipeline given working predicate translation, not to test
   cold-start. The test enforces this with an allowlist for known drift
   (predicates that appear in reference corpora but use a name not matching
   the seed pack's canonical form — these are v0.16 corpus-vs-seed-pack
   normalization candidates, captured at D46).

The failure message names exactly which predicate is missing, which corpus
case carries it, and what the operator's next step is (add to seed pack,
add to allowlist, or rename the corpus reference).
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_PATH = _REPO_ROOT / "seeds" / "predicate_translation.json"
_CALIBRATION_DIR = _REPO_ROOT / "tests" / "calibration"

# Corpora whose purpose IS testing cold-start generation. Their predicates
# are intentionally absent from the seed pack.
_COLD_START_CORPORA = {
    # The corpus directly tests predicate-metadata cold-start generation;
    # every aedos_predicate here is the system-under-test for "given a
    # never-seen predicate, can the substrate oracle generate routing_hint,
    # object_type, etc. correctly?"
    "predicate_metadata_corpus.jsonl",
    # Same pattern for predicate-distribution metadata.
    "predicate_distribution_corpus.jsonl",
    # python_verification predicates are computational (days_after,
    # is_prime, count_char_r); they fire on the python verifier route. Some
    # WILL eventually want to be in the seed pack with routing_hint=python,
    # but absence is currently a v0.16 item, not a defect to gate.
    "python_verification_corpus.jsonl",
}

# Archived corpora — not live; skip.
_ARCHIVED_CORPORA = {"extraction_corpus_v0.jsonl"}

# Known drift in reference corpora — predicates that use a name not
# matching the seed pack's canonical form. Phase H Cluster 3
# (2026-05-26): the majority of the original D46 drift list was
# closed by adding alias rows to the seed pack (one row per
# corpus-observed surface form, sharing kb_property + slot semantics
# with the canonical entry). Walker Stage 3 already broadens
# predicate lookup via the predicate_translation oracle's
# kb_property, so the alias pattern is bridge-equivalent to literal
# canonicalization without an extraction-time rewrite.
#
# The entries below are surface forms that map to a Wikidata property
# Aedos does not yet seed (P37 official_language, P576 dissolved,
# P749 parent_organization, P800 notable_work) or that require new
# semantic modeling (co_founded as a multi-author variant of
# founded_by). Each is captured as a v0.16 candidate; the allowlist
# documents the gap so this test catches *new* drift while these
# stay triaged.
_KNOWN_DRIFT: set[tuple[str, str]] = {
    # v0.16: needs a new seed entry — predicate maps to a Wikidata
    # property not yet covered by the seed pack.
    ("co_founded", "extraction_corpus.jsonl"),              # multi-author variant of founded_by; needs distinct modeling
    ("co_founded", "kb_mapping_corpus.jsonl"),
    ("dissolved_in", "kb_mapping_corpus.jsonl"),           # P576 (date of dissolution) — not yet seeded
    ("notable_work", "kb_mapping_corpus.jsonl"),           # P800 (notable work) — not yet seeded
    ("official_language", "kb_mapping_corpus.jsonl"),      # P37 (official language) — distinct from P407 `language`
    ("parent_organization", "kb_mapping_corpus.jsonl"),    # P749 (parent organization) — distinct from P361 `part_of`
}


def _load_seeded_predicates() -> set[str]:
    entries = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    return {e["aedos_predicate"] for e in entries}


def _walk_predicates(obj, out: list[str]) -> None:
    """Recursively collect predicate strings from any of three known field
    names: `predicate`, `aedos_predicate`, `expected_predicate`."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("predicate", "aedos_predicate", "expected_predicate") and isinstance(v, str):
                out.append(v)
            _walk_predicates(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_predicates(item, out)


def _collect_corpus_predicates() -> dict[tuple[str, str], list[str]]:
    """(predicate, corpus) → list of case_ids that reference it."""
    refs: dict[tuple[str, str], list[str]] = {}
    for corpus_path in sorted(glob.glob(str(_CALIBRATION_DIR / "*.jsonl"))):
        fname = Path(corpus_path).name
        if fname in _COLD_START_CORPORA or fname in _ARCHIVED_CORPORA:
            continue
        for line_no, line in enumerate(open(corpus_path, encoding="utf-8"), 1):
            try:
                case = json.loads(line)
            except json.JSONDecodeError:
                continue
            case_id = case.get("id", f"<line_{line_no}>")
            found: list[str] = []
            _walk_predicates(case, found)
            for predicate in set(found):  # dedupe per case
                refs.setdefault((predicate, fname), []).append(case_id)
    return refs


def test_every_reference_corpus_predicate_is_seeded_or_documented():
    """Phase G D39: catch new predicate drift between corpus and seed pack.

    For each (predicate, corpus) pair in a reference corpus, require that
    EITHER the predicate is in the seed pack OR the pair is in the
    documented `_KNOWN_DRIFT` allowlist (which is itself a v0.16
    normalization queue captured at D46).
    """
    seeded = _load_seeded_predicates()
    refs = _collect_corpus_predicates()

    missing: list[str] = []
    for (predicate, corpus), case_ids in sorted(refs.items()):
        if predicate in seeded:
            continue
        if (predicate, corpus) in _KNOWN_DRIFT:
            continue
        # Show up to 3 case_ids so the failure points at concrete cases.
        sample = ", ".join(case_ids[:3])
        more = f" (and {len(case_ids) - 3} more)" if len(case_ids) > 3 else ""
        missing.append(
            f"  predicate {predicate!r} in {corpus} (case_ids: {sample}{more}) "
            f"is not in seeds/predicate_translation.json and has no documented "
            f"cold-start exception."
        )

    if missing:
        pytest.fail(
            "Seed pack coverage gap — new predicate drift detected.\n\n"
            + "\n".join(missing) + "\n\n"
            "Three resolutions for each line above:\n"
            "  (a) Add the predicate to seeds/predicate_translation.json "
            "with routing_hint + entity types + reason (preferred for "
            "reference-data predicates).\n"
            "  (b) Rename the corpus reference to a seeded predicate "
            "(preferred if the corpus is using a synonym of an existing "
            "canonical form — e.g. 'spouse' → 'spouse_of').\n"
            "  (c) Add the (predicate, corpus) pair to `_KNOWN_DRIFT` in "
            "this test file, documenting it as a v0.16 normalization "
            "task (D46 work item)."
        )


def test_known_drift_entries_actually_appear_in_a_corpus():
    """Guard against `_KNOWN_DRIFT` growing stale: every entry must
    correspond to a real (predicate, corpus) pair currently in a corpus.
    If a known-drift entry no longer appears in its corpus (e.g. because
    the corpus was normalized to the seeded name), delete the entry —
    the test should not carry dead allowlist rows."""
    refs = _collect_corpus_predicates()
    stale = [
        entry for entry in sorted(_KNOWN_DRIFT)
        if entry not in refs
    ]
    if stale:
        pytest.fail(
            "`_KNOWN_DRIFT` entries no longer appearing in any corpus "
            "(remove them):\n" + "\n".join(f"  {e}" for e in stale)
        )


def test_seed_pack_contains_phase_g_d39_additions():
    """Pin the three Phase G D39 additions; guards against silent removal."""
    seeded = _load_seeded_predicates()
    for predicate in ("born_in_year", "prefers", "status"):
        assert predicate in seeded, (
            f"Phase G D39 (2026-05-23) added {predicate!r} to the seed pack; "
            f"the entry is now missing. See docs/v0.16_planning.md D39 for "
            f"the rationale before removing."
        )
