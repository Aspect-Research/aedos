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
    # ------------------------------------------------------------------
    # v0.16 WS1 (Decision 1.g): the 21 synonym ALIAS rows were deleted from the
    # seed pack. Synonymy is no longer pre-seeded — it is carried by the
    # substrate's multi-property binding discovery (Wikidata ontology + SLING).
    # The corpus references below were formerly satisfied by an alias seed row;
    # they are now COLD-START DISCOVERY TARGETS. They are documented here (not
    # re-seeded and not renamed in the corpus) so this test still catches NEW
    # drift while the alias-deletion gap stays explicitly triaged. Removing a
    # pair requires either re-adding the alias seed (rejected — no hardcoded
    # synonym tables) or renaming the corpus reference to its canonical form
    # (TA-CAL's call; owns corpus .jsonl edits).
    ("authored", "entity_resolution_corpus.jsonl"),        # was alias of P50 author
    ("authored", "extraction_corpus.jsonl"),
    ("authored", "kb_mapping_corpus.jsonl"),
    ("award_received", "consistency_check_corpus.jsonl"),  # was alias of P166 (received_award)
    ("birthplace_is", "consistency_check_corpus.jsonl"),   # was functional alias of born_in (P19)
    ("date_of_birth", "kb_mapping_corpus.jsonl"),          # was functional alias of born_on (P569)
    ("date_of_death", "kb_mapping_corpus.jsonl"),          # was functional alias of died_on (P570)
    ("death_place_is", "consistency_check_corpus.jsonl"),  # was functional alias of died_in (P20)
    ("founded_in", "entity_resolution_corpus.jsonl"),      # was functional alias of founded_in_year (P571)
    ("graduated_from", "consistency_check_corpus.jsonl"),  # was alias of educated_at (P69)
    ("graduated_from", "extraction_corpus.jsonl"),
    ("has_population", "kb_mapping_corpus.jsonl"),          # was alias of population_of (P1082)
    ("held_position", "consistency_check_corpus.jsonl"),   # was alias of holds_role (P39)
    ("inception_date", "kb_mapping_corpus.jsonl"),         # was functional alias of founded_in_year (P571)
    ("instance_of", "kb_mapping_corpus.jsonl"),            # was alias of is_a (P31)
    ("occupied_position", "consistency_check_corpus.jsonl"),  # was alias of holds_role (P39)
    ("part_of_region", "consistency_check_corpus.jsonl"),  # was alias of located_in / part_of
    ("received_award", "entity_resolution_corpus.jsonl"),  # canonical row deleted; cold-start target
    ("received_award", "extraction_corpus.jsonl"),
    ("received_award", "kb_mapping_corpus.jsonl"),
    ("shares_border_with", "kb_mapping_corpus.jsonl"),     # was alias of P47 (shares border)
    ("spouse", "kb_mapping_corpus.jsonl"),                 # was alias of spouse_of (P26)
    ("successor_of", "kb_mapping_corpus.jsonl"),           # was alias of P1365 (replaces)
    ("won_award", "consistency_check_corpus.jsonl"),       # was alias of received_award (P166)
    ("works_at", "consistency_check_corpus.jsonl"),        # was alias of employed_by (P108)
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


def test_phase_h_cluster_3_step_4_corrections():
    """Pin the Phase H Cluster 3 step 4 semantic corrections.

    - `located_in` was P276 (generic location); corrected to P131
      (administrative territorial entity). All three primary corpora
      (predicate_metadata pred_kb_007, entity_resolution er_unambiguous_002+,
      kb_mapping kb_map_006) expect P131 for institution-in-place semantics.
    - `occurred_in` was P585 (point in time, a qualifier property
      incompatible with object_type=entity); corrected to P276 (location)
      for event-location semantics used by derivation_corpus
      der_multihop_003 and predicate_distribution pd_up_003.
    """
    entries = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    by_pred = {e["aedos_predicate"]: e for e in entries}
    assert by_pred["located_in"]["kb_property"] == "P131", (
        "located_in must map to P131 (administrative territorial entity), "
        "not P276 (generic location); see Phase H Cluster 3 step 4."
    )
    assert by_pred["occurred_in"]["kb_property"] == "P276", (
        "occurred_in must map to P276 (location), not P585 (point in time); "
        "P585 is a qualifier and incompatible with object_type=entity."
    )
