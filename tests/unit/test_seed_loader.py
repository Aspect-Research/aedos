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
# v0.16 WS6 T1: `kb_interval` is the interval-endpoint routing hint (the
# *_started/_ended date-in-object predicates ground against the P580/P582
# qualifier on the base relation's KB statement, via the walker's interval
# resolver). Mirror the SRC loader's `_VALID_ROUTING_HINTS` (seed_loader.py),
# which was extended for the new seed rows.
_VALID_ROUTING_HINTS = {
    "user_authoritative", "kb_resolvable", "python", "abstain", "kb_interval",
}

# The functional (single-valued) predicates in the seed pack — a subject has at
# most one true object (M4 backfill). See docs/v0.15_build_log/fixup2_report.md
# for the per-predicate reasoning.
#
# v0.16 WS1 (Decision 1.g): the Phase H Cluster 3 functional ALIAS rows
# (birthplace_is, death_place_is, date_of_birth, date_of_death, founded_in,
# inception_date) were DELETED from the seed pack along with the rest of the 21
# synonym aliases — synonymy is now carried by binding discovery, not by
# duplicate seed rows. The 15 canonical functional predicates below remain.
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
# v0.16 WS1 alias-row deletions (Decision 1.g)
# ---------------------------------------------------------------------------

# The synonym alias rows deleted from the seed pack in v0.16 WS1. Synonymy is
# now carried by the substrate's binding discovery (Wikidata ontology + SLING),
# not by duplicate seed rows — so these surface forms become cold-start
# discovery targets rather than pre-seeded aliases.
#
# v0.16.1 WS2 NOTE: `instance_of` was REMOVED from this deleted set and
# RE-ADDED to the seed pack — NOT as a synonym duplicate of `is_a`, but because
# the extractor actually emits `instance_of` (Rule 19/22 copula) and the row now
# carries the P106 occupation CANDIDATE binding the copula-grounding fix needs.
# A cold-start `instance_of` could not reliably acquire the value-type-gated
# P106 candidate (the discovery path only emits it when P106's ontology is
# fetchable), so the knowledge lives in the seed. It is the only re-add.
_DELETED_ALIAS_PREDICATES = {
    "authored", "award_received", "birthplace_is", "date_of_birth",
    "date_of_death", "death_place_is", "founded_in", "graduated_from",
    "has_population", "held_position", "inception_date",
    "occupied_position", "part_of_region", "received_award",
    "shares_border_with", "spouse", "successor_of", "won_award", "won_prize",
    "works_at",
}

# Canonical rows that MUST survive the alias deletion — each is the surviving
# canonical predicate the deleted aliases were synonyms of, plus the 16 the
# verifier agent confirmed retained.
_CANONICAL_SURVIVORS = {
    "holds_role", "employed_by", "born_in", "died_in", "born_on", "died_on",
    "located_in", "founded_in_year", "member_of", "occupation", "spouse_of",
    "population_of", "part_of", "is_a", "subclass_of", "capital_of",
}


class TestSeedAliasDeletions:
    """v0.16 WS1 (Decision 1.g): the 21 synonym alias rows are gone; their
    canonical counterparts remain; every surviving kb_resolvable row still
    carries a kb_property that synthesizes into a binding (back-compat)."""

    @pytest.fixture(scope="class")
    def seeds(self):
        return json.loads(_SEEDS_FILE.read_text(encoding="utf-8"))

    def test_deleted_aliases_are_absent(self, seeds):
        present = {e["aedos_predicate"] for e in seeds}
        leaked = _DELETED_ALIAS_PREDICATES & present
        assert not leaked, (
            f"alias rows that v0.16 WS1 deleted are still in the seed pack: "
            f"{sorted(leaked)} — synonymy belongs to discovery, not seed rows."
        )

    def test_canonical_survivors_remain(self, seeds):
        present = {e["aedos_predicate"] for e in seeds}
        missing = _CANONICAL_SURVIVORS - present
        assert not missing, (
            f"canonical rows that must survive the alias deletion are missing: "
            f"{sorted(missing)}"
        )

    def test_seed_count_is_71_after_deletions_t1_ws2_and_ws3b_additions(self, seeds):
        # 83 pre-v0.16 rows minus 21 deleted aliases = 62, plus the 6 v0.16 WS6
        # T1 interval-endpoint rows (employment/membership/role _started/_ended)
        # = 68, plus the 1 v0.16.1 WS2 `instance_of` copula row (the predicate
        # the extractor actually emits; carries the P106 occupation candidate
        # binding) = 69, plus the 2 v0.16.1 WS3b premise->Python comparison rows
        # (born_before, founded_before; each carries `premise_properties`) = 71.
        # v0.16.1 WS4 dropped the 2 dead status_started/status_ended rows (P571/
        # P576): they can never fire — the kb_interval arm reads P580/P582
        # qualifiers, not statement values, and org subjects already route to
        # founded_in_year/dissolved_in_year. A hard count guards against an
        # accidental re-add of an alias, a silent drop of a canonical row, or a
        # dropped endpoint row.
        assert len(seeds) == 71, (
            f"expected 71 seed rows (62 after the 21 alias deletions + 6 T1 "
            f"interval-endpoint rows + 1 WS2 instance_of copula row + 2 WS3b "
            f"premise->Python rows), got {len(seeds)}"
        )

    def test_every_kb_resolvable_row_synthesizes_a_binding(self, seeds):
        # Back-compat invariant: a surviving kb_resolvable row carries a
        # kb_property, so PredicateMetadata.__post_init__ synthesizes exactly
        # one legacy_scalar binding from the scalar columns (single-binding
        # path == pre-v0.16 behavior).
        from aedos.layer3_substrate.predicate_translation import PredicateMetadata

        for entry in seeds:
            if entry["routing_hint"] != "kb_resolvable":
                continue
            meta = PredicateMetadata(
                id=0,
                aedos_predicate=entry["aedos_predicate"],
                object_type=entry["object_type"],
                user_subject_required=bool(entry["user_subject_required"]),
                distinct_slots=entry.get("distinct_slots"),
                routing_hint=entry["routing_hint"],
                kb_namespace=entry.get("kb_namespace"),
                kb_property=entry.get("kb_property"),
                slot_to_qualifier=entry.get("slot_to_qualifier"),
                reason=entry["reason"],
                created_at="t",
                single_valued=bool(entry.get("single_valued", 0)),
            )
            assert len(meta.bindings) == 1, entry["aedos_predicate"]
            b = meta.bindings[0]
            assert b.source == "legacy_scalar"
            assert b.kb_property == entry.get("kb_property")


# ---------------------------------------------------------------------------
# v0.16 WS6 T1 — interval-endpoint seed rows + date→time reconciliation
# ---------------------------------------------------------------------------

# The 6 *_started/_ended date-in-object endpoint predicates. Each grounds
# against the P580 (start time) / P582 (end time) qualifier on the base
# relation's KB statement; object_type='time', routing_hint='kb_interval',
# slot_to_qualifier maps subject→statement_subject, org→statement_value,
# object→qualifier:P580|P582.
#
# v0.16.1 WS4 dropped the dead status_started/status_ended rows (P571 inception /
# P576 dissolution): the kb_interval arm reads P580/P582 *qualifiers* off a base
# statement, but for P571/P576 the date is the statement VALUE itself, so the
# qualifier-gathering arm yields nothing — those rows could never fire. Org
# subjects already route to founded_in_year (P571) / dissolved_in_year (P576).
_T1_ENDPOINT_BASE_PROPERTY = {
    "employment_started": "P108", "employment_ended": "P108",
    "membership_started": "P463", "membership_ended": "P463",
    "role_started": "P39", "role_ended": "P39",
}

# The 6 existing date predicates reconciled from object_type 'date' → 'time' in
# §A.4 so the kb_verifier value-type gate ('time' → {date, literal}) is live.
_RECONCILED_TIME_PREDICATES = {
    "born_on", "died_on", "founded_in_year", "published_in_year",
    "released_in_year", "born_in_year",
}


class TestSeedT1IntervalEndpoints:
    """v0.16 WS6 T1: the new *_started/_ended interval-endpoint seed rows load
    with object_type='time', the correct base kb_property, routing_hint
    'kb_interval', and a qualifier-keyed slot_to_qualifier — and synthesize a
    single legacy_scalar PredicateBinding."""

    @pytest.fixture(scope="class")
    def by_pred(self):
        seeds = json.loads(_SEEDS_FILE.read_text(encoding="utf-8"))
        return {e["aedos_predicate"]: e for e in seeds}

    def test_all_endpoint_rows_present(self, by_pred):
        missing = set(_T1_ENDPOINT_BASE_PROPERTY) - set(by_pred)
        assert not missing, f"missing T1 endpoint seed rows: {sorted(missing)}"

    def test_endpoint_rows_object_type_is_time(self, by_pred):
        for pred in _T1_ENDPOINT_BASE_PROPERTY:
            assert by_pred[pred]["object_type"] == "time", (
                f"{pred}: object_type must be 'time' (value-type gate), "
                f"got {by_pred[pred]['object_type']!r}"
            )

    def test_endpoint_rows_routing_hint_is_kb_interval(self, by_pred):
        for pred in _T1_ENDPOINT_BASE_PROPERTY:
            assert by_pred[pred]["routing_hint"] == "kb_interval", (
                f"{pred}: routing_hint must be 'kb_interval'"
            )

    def test_endpoint_rows_kb_property_is_base_relation(self, by_pred):
        for pred, base_prop in _T1_ENDPOINT_BASE_PROPERTY.items():
            assert by_pred[pred]["kb_property"] == base_prop, (
                f"{pred}: kb_property must be the BASE relation property "
                f"{base_prop} (the resolver reads the P580/P582 qualifier off "
                f"it), got {by_pred[pred]['kb_property']!r}"
            )

    def test_employment_membership_role_kb_properties(self, by_pred):
        # Explicit pin on the spec'd P108/P463/P39 mapping the test plan names.
        assert by_pred["employment_started"]["kb_property"] == "P108"
        assert by_pred["employment_ended"]["kb_property"] == "P108"
        assert by_pred["membership_started"]["kb_property"] == "P463"
        assert by_pred["membership_ended"]["kb_property"] == "P463"
        assert by_pred["role_started"]["kb_property"] == "P39"
        assert by_pred["role_ended"]["kb_property"] == "P39"

    def test_started_rows_map_object_to_p580_qualifier(self, by_pred):
        for pred in (p for p in _T1_ENDPOINT_BASE_PROPERTY if p.endswith("_started")):
            stq = by_pred[pred]["slot_to_qualifier"]
            assert stq is not None, f"{pred}: missing slot_to_qualifier"
            assert stq.get("subject") == "statement_subject", pred
            assert stq.get("object") == "qualifier:P580", (
                f"{pred}: object slot must map to the P580 (start time) qualifier, "
                f"got {stq.get('object')!r}"
            )

    def test_ended_rows_map_object_to_p582_qualifier(self, by_pred):
        for pred in (p for p in _T1_ENDPOINT_BASE_PROPERTY if p.endswith("_ended")):
            stq = by_pred[pred]["slot_to_qualifier"]
            assert stq is not None, f"{pred}: missing slot_to_qualifier"
            assert stq.get("object") == "qualifier:P582", (
                f"{pred}: object slot must map to the P582 (end time) qualifier, "
                f"got {stq.get('object')!r}"
            )

    def test_endpoint_rows_synthesize_a_binding(self, by_pred):
        # WS1 read-synthesis: a scalar-shaped endpoint row builds exactly one
        # legacy_scalar PredicateBinding from its scalar columns, carrying the
        # base kb_property and the qualifier-keyed slot map.
        from aedos.layer3_substrate.predicate_translation import PredicateMetadata

        for pred, base_prop in _T1_ENDPOINT_BASE_PROPERTY.items():
            entry = by_pred[pred]
            meta = PredicateMetadata(
                id=0,
                aedos_predicate=entry["aedos_predicate"],
                object_type=entry["object_type"],
                user_subject_required=bool(entry["user_subject_required"]),
                distinct_slots=entry.get("distinct_slots"),
                routing_hint=entry["routing_hint"],
                kb_namespace=entry.get("kb_namespace"),
                kb_property=entry.get("kb_property"),
                slot_to_qualifier=entry.get("slot_to_qualifier"),
                reason=entry["reason"],
                created_at="t",
                single_valued=bool(entry.get("single_valued", 0)),
            )
            assert len(meta.bindings) == 1, pred
            b = meta.bindings[0]
            assert b.source == "legacy_scalar"
            assert b.kb_property == base_prop
            assert b.slot_to_qualifier == entry.get("slot_to_qualifier")

    def test_endpoint_rows_load_without_tripping_loader(self, tmp_path):
        # The reconciled date→time object_type and the new kb_interval routing
        # hint must both pass the seed loader's _validate_entry (the only
        # load-time enum gate). A full load lands all 6 endpoint rows.
        from aedos.database import open_db
        from aedos.seed_loader import load_seeds_into_connection

        db_file = tmp_path / "t1.db"
        conn = open_db(str(db_file))
        n = load_seeds_into_connection(conn)
        assert n == 71  # WS4 dropped 2 dead status_* rows; WS3b added born_before/founded_before
        conn.row_factory = __import__("sqlite3").Row
        rows = conn.execute(
            "SELECT aedos_predicate, object_type, routing_hint, kb_property "
            "FROM predicate_translation WHERE routing_hint='kb_interval'"
        ).fetchall()
        conn.close()
        loaded = {r["aedos_predicate"]: r for r in rows}
        assert set(loaded) == set(_T1_ENDPOINT_BASE_PROPERTY)
        for pred, base_prop in _T1_ENDPOINT_BASE_PROPERTY.items():
            assert loaded[pred]["object_type"] == "time"
            assert loaded[pred]["kb_property"] == base_prop


class TestSeedDateToTimeReconciliation:
    """v0.16 WS6 §A.4: the date predicates were reconciled from object_type
    'date' → 'time' so the kb_verifier value-type gate (_OBJECT_TYPE_COMPATIBLE_
    VALUE_TYPES key 'time' → {date, literal}) is live for them. No seed row may
    carry the stale 'date' object_type, and the loader must not trip on it."""

    @pytest.fixture(scope="class")
    def seeds(self):
        return json.loads(_SEEDS_FILE.read_text(encoding="utf-8"))

    def test_no_seed_row_carries_stale_date_object_type(self, seeds):
        stale = [e["aedos_predicate"] for e in seeds if e["object_type"] == "date"]
        assert not stale, (
            f"these rows still use the stale object_type 'date'; §A.4 reconciles "
            f"date→time so the value-type gate is live: {stale}"
        )

    def test_reconciled_date_predicates_are_time(self, seeds):
        by_pred = {e["aedos_predicate"]: e for e in seeds}
        for pred in _RECONCILED_TIME_PREDICATES:
            assert pred in by_pred, f"{pred} missing from seed pack"
            assert by_pred[pred]["object_type"] == "time", (
                f"{pred}: object_type must be 'time' after the §A.4 reconciliation"
            )

    def test_reconciled_rows_load_cleanly(self, tmp_path):
        # The reconciliation is a pure value edit; a full load must still land
        # all rows (no loader enum check on object_type).
        from aedos.database import open_db
        from aedos.seed_loader import load_seeds_into_connection

        db_file = tmp_path / "recon.db"
        conn = open_db(str(db_file))
        n = load_seeds_into_connection(conn)
        conn.close()
        assert n == 71  # WS4 dropped 2 dead status_* rows; WS3b added 2 premise->Python rows


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


# ---------------------------------------------------------------------------
# v0.16.1 WS2 — occupation-copula candidate binding synthesis
# ---------------------------------------------------------------------------

class TestSeedWS2CopulaCandidateBindings:
    """The `instance_of` / `is_a` copula seed rows declare
    candidate_kb_properties=["P106"]; the loader synthesizes the `bindings`
    column = [P31 primary, P106 value-type-gated occupation candidate]. Every
    other seed row keeps bindings NULL (read-synthesizes one legacy_scalar
    binding from scalar columns, unchanged)."""

    @pytest.fixture(scope="class")
    def by_pred(self):
        seeds = json.loads(_SEEDS_FILE.read_text(encoding="utf-8"))
        return {e["aedos_predicate"]: e for e in seeds}

    def test_instance_of_row_present_with_candidate(self, by_pred):
        # The extractor emits `instance_of` (Rule 19/22), so the seeded row uses
        # that exact predicate name and declares the P106 candidate.
        assert "instance_of" in by_pred
        row = by_pred["instance_of"]
        assert row["kb_property"] == "P31"
        assert row["candidate_kb_properties"] == ["P106"]
        assert row["candidate_object_entity_types"]["P106"] == ["Q12737077", "Q28640"]

    def test_is_a_row_also_carries_candidate(self, by_pred):
        assert by_pred["is_a"]["candidate_kb_properties"] == ["P106"]

    def test_synthesize_builds_p31_then_p106(self):
        from aedos.seed_loader import _synthesize_bindings_json
        entry = {
            "aedos_predicate": "instance_of", "kb_namespace": "wikidata",
            "kb_property": "P31",
            "slot_to_qualifier": {"subject": "statement_subject", "object": "statement_value"},
            "single_valued": 0,
            "candidate_kb_properties": ["P106"],
            "candidate_object_entity_types": {"P106": ["Q12737077", "Q28640"]},
        }
        binds = json.loads(_synthesize_bindings_json(entry))
        assert [b["kb_property"] for b in binds] == ["P31", "P106"]
        primary, candidate = binds
        # Primary P31: NOT value-type-gated, no object_entity_types (open object).
        assert primary["value_type_gated"] is False
        assert primary["object_entity_types"] is None
        assert primary["source"] == "legacy_scalar"
        # Candidate P106: value-type-gated, occupation constraint, never contradicts.
        assert candidate["value_type_gated"] is True
        assert candidate["single_valued"] is False
        assert candidate["object_entity_types"] == ["Q12737077", "Q28640"]
        assert candidate["source"] == "candidate"

    def test_non_candidate_row_synthesizes_null(self):
        from aedos.seed_loader import _synthesize_bindings_json
        entry = {"aedos_predicate": "born_in", "kb_namespace": "wikidata",
                 "kb_property": "P19", "slot_to_qualifier": None, "single_valued": 1}
        assert _synthesize_bindings_json(entry) is None

    def test_loaded_instance_of_consults_two_bindings(self):
        from aedos.database import open_memory_db
        from aedos.seed_loader import load_seeds_into_connection
        from aedos.layer3_substrate.predicate_translation import PredicateTranslation
        from aedos.llm.client import LLMClient

        class _T:
            def extract_with_tool(self, *a, **k):
                raise AssertionError("seeded row should not hit the oracle")

            def chat(self, *a, **k):
                return ""

        db = open_memory_db()
        load_seeds_into_connection(db)
        pt = PredicateTranslation(db=db, llm_client=LLMClient(_transport=_T()))
        meta = pt.consult("instance_of")
        assert [b.kb_property for b in meta.bindings] == ["P31", "P106"]
        # bindings[0] mirrors the scalar columns (primary P31, open object).
        assert meta.kb_property == "P31"
        assert meta.object_entity_types is None
        p106 = meta.bindings[1]
        assert p106.value_type_gated is True
        assert p106.single_valued is False
        assert p106.object_entity_types == ["Q12737077", "Q28640"]
