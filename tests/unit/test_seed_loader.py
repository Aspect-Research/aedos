"""Tests for seed pack: parse, validate, load into a fresh in-memory DB."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_SEEDS_FILE = Path(__file__).parents[2] / "seeds" / "predicate_translation.json"
_SEED_VERSION_FILE = Path(__file__).parents[2] / "seeds" / "SEED_VERSION.txt"
_LOAD_SCRIPT = Path(__file__).parents[2] / "seeds" / "load_seeds.py"

_REQUIRED_FIELDS = {
    "aedos_predicate",
    "object_type",
    "user_subject_required",
    "routing_hint",
    "kb_namespace",
    "kb_property",
    "slot_to_qualifier",
    "single_valued",
    "reason",
}
_VALID_ROUTING_HINTS = {"user_authoritative", "kb_resolvable", "python", "abstain"}

# The functional (single-valued) predicates in the seed pack — a subject has at
# most one true object (M4 backfill). See docs/v0.15_build_log/fixup2_report.md
# for the per-predicate reasoning.
_FUNCTIONAL_PREDICATES = {
    "born_in", "died_in", "born_on", "died_on", "capital_of", "has_capital",
    "continent_of", "founded_in_year", "head_of_government", "head_of_state",
    "gender",
    # Phase G D39 (2026-05-23) additions:
    "born_in_year",  # one birth date per person
    "prefers",       # functional at a point in time per object-class (revisable)
    "status",        # one current status per entity (revisable)
    # Phase G D23 (2026-05-23) correction (was single_valued=0):
    "lives_in",      # one current residence per person (revisable on relocation)
    # Phase H Cluster 3 (2026-05-26) — functional aliases:
    "birthplace_is",     # alias of born_in (P19)
    "death_place_is",    # alias of died_in (P20)
    "date_of_birth",     # alias of born_on (P569)
    "date_of_death",     # alias of died_on (P570)
    "founded_in",        # alias of founded_in_year (P571)
    "inception_date",    # alias of founded_in_year (P571)
}


# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------

class TestSeedFileExists:
    def test_seeds_json_exists(self):
        assert _SEEDS_FILE.exists(), f"Missing {_SEEDS_FILE}"

    def test_seed_version_exists(self):
        assert _SEED_VERSION_FILE.exists(), f"Missing {_SEED_VERSION_FILE}"

    def test_load_script_exists(self):
        assert _LOAD_SCRIPT.exists(), f"Missing {_LOAD_SCRIPT}"


# ---------------------------------------------------------------------------
# Parse and schema validation
# ---------------------------------------------------------------------------

class TestSeedParsing:
    @pytest.fixture(scope="class")
    def seeds(self):
        return json.loads(_SEEDS_FILE.read_text(encoding="utf-8"))

    def test_parses_as_list(self, seeds):
        assert isinstance(seeds, list)

    def test_at_least_60_entries(self, seeds):
        assert len(seeds) >= 60, f"Expected ≥60 seeds, got {len(seeds)}"

    def test_all_required_fields_present(self, seeds):
        for idx, entry in enumerate(seeds):
            missing = _REQUIRED_FIELDS - entry.keys()
            assert not missing, f"Entry {idx} ({entry.get('aedos_predicate')!r}) missing: {missing}"

    def test_all_routing_hints_valid(self, seeds):
        for idx, entry in enumerate(seeds):
            assert entry["routing_hint"] in _VALID_ROUTING_HINTS, (
                f"Entry {idx}: invalid routing_hint {entry['routing_hint']!r}"
            )

    def test_all_aedos_predicates_non_empty(self, seeds):
        for idx, entry in enumerate(seeds):
            assert entry["aedos_predicate"], f"Entry {idx}: empty aedos_predicate"

    def test_all_reasons_non_empty(self, seeds):
        for idx, entry in enumerate(seeds):
            assert entry.get("reason"), f"Entry {idx} ({entry.get('aedos_predicate')!r}): empty reason"

    def test_slot_to_qualifier_null_or_dict(self, seeds):
        for idx, entry in enumerate(seeds):
            stq = entry.get("slot_to_qualifier")
            assert stq is None or isinstance(stq, dict), (
                f"Entry {idx}: slot_to_qualifier must be null or dict, got {type(stq)}"
            )

    def test_user_subject_required_is_int(self, seeds):
        for idx, entry in enumerate(seeds):
            assert isinstance(entry.get("user_subject_required"), int), (
                f"Entry {idx}: user_subject_required must be int"
            )

    def test_kb_resolvable_has_kb_property(self, seeds):
        for idx, entry in enumerate(seeds):
            if entry["routing_hint"] == "kb_resolvable":
                assert entry.get("kb_property"), (
                    f"Entry {idx} ({entry.get('aedos_predicate')!r}): "
                    "kb_resolvable entry must have kb_property"
                )

    def test_category_coverage_roles(self, seeds):
        role_predicates = {"holds_role", "educated_at", "employed_by", "member_of", "occupation"}
        predicates = {e["aedos_predicate"] for e in seeds}
        assert role_predicates & predicates, "Missing role predicates"

    def test_category_coverage_locations(self, seeds):
        location_predicates = {"born_in", "died_in", "located_in", "located_at", "capital_of"}
        predicates = {e["aedos_predicate"] for e in seeds}
        assert location_predicates & predicates, "Missing location predicates"

    def test_category_coverage_kinship(self, seeds):
        kinship_predicates = {"spouse_of", "parent_of", "child_of", "sibling_of"}
        predicates = {e["aedos_predicate"] for e in seeds}
        assert kinship_predicates & predicates, "Missing kinship predicates"

    def test_category_coverage_categorical(self, seeds):
        cat_predicates = {"is_a", "instance_of", "subclass_of"}
        predicates = {e["aedos_predicate"] for e in seeds}
        assert cat_predicates & predicates, "Missing categorical predicates"

    def test_category_coverage_mereological(self, seeds):
        mere_predicates = {"part_of", "has_part", "contains"}
        predicates = {e["aedos_predicate"] for e in seeds}
        assert mere_predicates & predicates, "Missing mereological predicates"

    def test_category_coverage_quantitative(self, seeds):
        quant_predicates = {"population_of", "area_of", "founded_in_year"}
        predicates = {e["aedos_predicate"] for e in seeds}
        assert quant_predicates & predicates, "Missing quantitative predicates"


# ---------------------------------------------------------------------------
# M4 backfill — single_valued
# ---------------------------------------------------------------------------

class TestSeedSingleValued:
    """M4 backfill: every entry carries single_valued; the functional
    predicates are 1 and everything else is 0."""

    @pytest.fixture(scope="class")
    def seeds(self):
        return json.loads(_SEEDS_FILE.read_text(encoding="utf-8"))

    def test_every_entry_has_single_valued(self, seeds):
        for idx, entry in enumerate(seeds):
            assert "single_valued" in entry, (
                f"Entry {idx} ({entry.get('aedos_predicate')!r}): missing single_valued"
            )
            assert entry["single_valued"] in (0, 1), (
                f"Entry {idx}: single_valued must be 0 or 1, got {entry['single_valued']!r}"
            )

    def test_functional_predicates_are_single_valued(self, seeds):
        by_pred = {e["aedos_predicate"]: e for e in seeds}
        for pred in _FUNCTIONAL_PREDICATES:
            assert pred in by_pred, f"functional predicate {pred!r} missing from seed pack"
            assert by_pred[pred]["single_valued"] == 1, f"{pred!r} should be single_valued=1"

    def test_non_functional_predicates_not_single_valued(self, seeds):
        for entry in seeds:
            if entry["aedos_predicate"] not in _FUNCTIONAL_PREDICATES:
                assert entry["single_valued"] == 0, (
                    f"{entry['aedos_predicate']!r} should be single_valued=0 (conservative default)"
                )

    def test_functional_count_matches_set(self, seeds):
        # Phase G D39 added 3 functional predicates (born_in_year, prefers,
        # status); count is asserted against `_FUNCTIONAL_PREDICATES` rather
        # than a hardcoded 11 so further seed-pack changes update one place.
        functional = [e for e in seeds if e["single_valued"] == 1]
        assert len(functional) == len(_FUNCTIONAL_PREDICATES), (
            f"seed pack has {len(functional)} functional entries but "
            f"_FUNCTIONAL_PREDICATES lists {len(_FUNCTIONAL_PREDICATES)}; "
            f"sync the test fixture with the seed pack additions."
        )


# ---------------------------------------------------------------------------
# Load into in-memory DB
# ---------------------------------------------------------------------------

class TestSeedLoading:
    @pytest.fixture
    def db_path(self, tmp_path):
        from aedos.database import open_db
        db_file = tmp_path / "test.db"
        conn = open_db(str(db_file))
        conn.close()
        return str(db_file)

    def test_loads_without_error(self, db_path):
        import sys
        sys.path.insert(0, str(Path(__file__).parents[2]))
        from seeds.load_seeds import load_seeds
        n = load_seeds(db_path)
        assert n >= 60

    def test_idempotent_load(self, db_path):
        import sqlite3
        from seeds.load_seeds import load_seeds
        load_seeds(db_path)
        load_seeds(db_path)
        conn = sqlite3.connect(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM predicate_translation WHERE retracted_at IS NULL"
        ).fetchone()[0]
        conn.close()
        seeds = json.loads(_SEEDS_FILE.read_text(encoding="utf-8"))
        assert count == len(seeds)

    def test_loaded_rows_queryable(self, db_path):
        import sqlite3
        from seeds.load_seeds import load_seeds
        load_seeds(db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT routing_hint, kb_property FROM predicate_translation WHERE aedos_predicate = 'holds_role'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "kb_resolvable"
        assert row[1] == "P39"

    def test_loaded_rows_have_created_at(self, db_path):
        import sqlite3
        from seeds.load_seeds import load_seeds
        load_seeds(db_path)
        conn = sqlite3.connect(db_path)
        nulls = conn.execute(
            "SELECT COUNT(*) FROM predicate_translation WHERE created_at IS NULL"
        ).fetchone()[0]
        conn.close()
        assert nulls == 0

    def test_loaded_rows_slot_to_qualifier_json(self, db_path):
        import sqlite3
        from seeds.load_seeds import load_seeds
        load_seeds(db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT slot_to_qualifier FROM predicate_translation WHERE aedos_predicate = 'holds_role'"
        ).fetchone()
        conn.close()
        assert row is not None
        stq = json.loads(row[0])
        assert "subject" in stq
        assert "object" in stq

    def test_loaded_rows_carry_single_valued(self, db_path):
        # M4 Step 3: a clean DB load lands every functional predicate with
        # single_valued=1, not the column default 0. Pre-Step-3, load_seeds did
        # not list single_valued in its INSERT, so all 61 rows defaulted to 0.
        import sqlite3
        from seeds.load_seeds import load_seeds
        load_seeds(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT aedos_predicate, single_valued FROM predicate_translation"
        ).fetchall()
        conn.close()
        by_pred = {r["aedos_predicate"]: r["single_valued"] for r in rows}
        for pred in _FUNCTIONAL_PREDICATES:
            assert by_pred[pred] == 1, f"{pred!r} loaded with single_valued={by_pred[pred]}"
        loaded_functional = {p for p, sv in by_pred.items() if sv == 1}
        assert loaded_functional == _FUNCTIONAL_PREDICATES

    def test_loaded_rows_carry_entity_types(self, db_path):
        # Phase G D33: when a seed entry carries subject_entity_types /
        # object_entity_types, the loader persists them as JSON. born_in_year
        # is the load-bearing example (Q5 subject, Q577 object).
        import sqlite3
        from seeds.load_seeds import load_seeds
        load_seeds(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT subject_entity_types, object_entity_types "
            "FROM predicate_translation WHERE aedos_predicate='born_in_year'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert json.loads(row["subject_entity_types"]) == ["Q5"]
        assert json.loads(row["object_entity_types"]) == ["Q577"]

    def test_seed_without_entity_types_loads_null(self, db_path):
        # Phase G D33: entries with no entity-type fields (the 61 pre-D33
        # entries) persist NULL — the filter no-ops for those predicates.
        import sqlite3
        from seeds.load_seeds import load_seeds
        load_seeds(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # holds_role is one of the original 61 entries without entity types
        row = conn.execute(
            "SELECT subject_entity_types, object_entity_types "
            "FROM predicate_translation WHERE aedos_predicate='holds_role'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["subject_entity_types"] is None
        assert row["object_entity_types"] is None
