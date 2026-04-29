"""Tests for the v0.6 VerificationCache + canonicalize_claim_key."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from src.cache.verification_cache import (
    VerificationCache,
    canonicalize_claim_key,
)
from src.fact_store import FactStore


# ---- canonicalize_claim_key --------------------------------------------


def test_canonicalize_basic():
    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1,
    }
    key = canonicalize_claim_key(claim)
    assert key == "spatial_temporal|located_in|p=1|entity=tokyo&location=japan"


def test_canonicalize_case_insensitive_for_pattern_predicate_strings():
    a = canonicalize_claim_key({
        "pattern": "Spatial_Temporal", "predicate": "Located_In",
        "slots": {"entity": "TOKYO", "location": "japan"}, "polarity": 1,
    })
    b = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "tokyo", "location": "Japan"}, "polarity": 1,
    })
    assert a == b


def test_canonicalize_slot_order_independent():
    a = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"}, "polarity": 1,
    })
    b = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"location": "Japan", "entity": "Tokyo"}, "polarity": 1,
    })
    assert a == b


def test_canonicalize_whitespace_normalized():
    a = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "  Tokyo  ", "location": "Japan"}, "polarity": 1,
    })
    b = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"}, "polarity": 1,
    })
    assert a == b


def test_canonicalize_polarity_distinguishes():
    pos = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"}, "polarity": 1,
    })
    neg = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"}, "polarity": 0,
    })
    assert pos != neg


def test_canonicalize_numeric_value_stable():
    a = canonicalize_claim_key({
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
        "polarity": 1,
    })
    b = canonicalize_claim_key({
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
        "polarity": 1,
    })
    assert a == b


# ---- predicate stem normalization (the user's child_of / is_child_of case) ----


def test_canonicalize_strips_is_prefix_from_predicate():
    """User reported two cache entries for the same fact:
    'relational|child_of|...' and 'relational|is_child_of|...' —
    different keys for semantically-identical claims. The is_ prefix
    should be stripped during canonicalization."""
    a = canonicalize_claim_key({
        "pattern": "relational", "predicate": "child_of",
        "slots": {"subject": "Barron Trump", "relation": "child_of",
                  "object": "Donald Trump"},
        "polarity": 1,
    })
    b = canonicalize_claim_key({
        "pattern": "relational", "predicate": "is_child_of",
        "slots": {"subject": "Barron Trump", "relation": "is_child_of",
                  "object": "Donald Trump"},
        "polarity": 1,
    })
    assert a == b


def test_canonicalize_strips_all_common_stems():
    """Each common stem prefix collapses to the bare core."""
    bare = canonicalize_claim_key({
        "pattern": "relational", "predicate": "founded",
        "slots": {"subject": "Apple", "relation": "founded",
                  "object": "Steve Jobs"},
        "polarity": 1,
    })
    for stem in ("is_", "was_", "has_", "have_", "are_", "were_",
                 "does_", "did_", "do_"):
        prefixed = canonicalize_claim_key({
            "pattern": "relational", "predicate": stem + "founded",
            "slots": {"subject": "Apple", "relation": stem + "founded",
                      "object": "Steve Jobs"},
            "polarity": 1,
        })
        assert prefixed == bare, f"stem {stem!r} did not collapse"


def test_canonicalize_does_not_strip_partial_match():
    """Don't false-strip predicates that happen to start with a stem
    SUBSTRING. ``island_of`` starts with 'is' but not 'is_'; must
    keep the whole word."""
    no_underscore = canonicalize_claim_key({
        "pattern": "categorical", "predicate": "island_of",
        "slots": {"entity": "x", "category": "y"},
        "polarity": 1,
    })
    # The control: 'land_of' should NOT match either since it's a
    # different word from 'island_of'.
    different = canonicalize_claim_key({
        "pattern": "categorical", "predicate": "land_of",
        "slots": {"entity": "x", "category": "y"},
        "polarity": 1,
    })
    assert no_underscore != different


def test_canonicalize_does_not_strip_when_only_stem():
    """A predicate that's literally just 'is_' shouldn't collapse to
    empty string. Defensive — the extractor should never produce
    this, but if it does, keep the original."""
    weird = canonicalize_claim_key({
        "pattern": "x", "predicate": "is_",
        "slots": {}, "polarity": 1,
    })
    assert "is_" in weird


def test_normalize_predicate_helper_directly():
    """Unit test on the _normalize_predicate helper itself so changes
    to the stem list are caught without re-deriving via the full key."""
    from src.cache.verification_cache import _normalize_predicate
    assert _normalize_predicate("is_child_of") == "child_of"
    assert _normalize_predicate("WAS_PRESIDENT") == "president"
    assert _normalize_predicate("child_of") == "child_of"  # unchanged
    assert _normalize_predicate("") == ""
    assert _normalize_predicate("island_of") == "island_of"  # 'is' alone doesn't match


# ---- _predicate_tokens + _parse_slots_block helpers ----


def test_predicate_tokens_splits_on_underscore():
    from src.cache.verification_cache import _predicate_tokens
    assert _predicate_tokens("child_of") == {"child", "of"}
    assert _predicate_tokens("is_child_of") == {"child", "of"}  # stem stripped first
    assert _predicate_tokens("son_of") == {"son", "of"}
    assert _predicate_tokens("") == set()


def test_parse_slots_block_round_trip():
    """The slot-block parser reverses the canonicalize_claim_key
    encoding so semantic_lookup can filter on identity slots."""
    from src.cache.verification_cache import _parse_slots_block
    block = "object=donald trump&relation=child_of&subject=barron trump"
    parsed = _parse_slots_block(block)
    assert parsed == {
        "object": "donald trump",
        "relation": "child_of",
        "subject": "barron trump",
    }


def test_parse_slots_block_handles_empty():
    from src.cache.verification_cache import _parse_slots_block
    assert _parse_slots_block("") == {}


def test_parse_slots_block_handles_value_with_equals():
    """Values may contain '=' (e.g. JSON-encoded numbers) — split
    only on the FIRST '=' per pair."""
    from src.cache.verification_cache import _parse_slots_block
    parsed = _parse_slots_block("subject=foo&value={\"a\": 1}")
    assert parsed["subject"] == "foo"
    assert parsed["value"] == "{\"a\": 1}"


# ---- VerificationCache: lookup / write / TTL ---------------------------


def test_write_then_lookup_returns_entry(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="spatial_temporal", predicate="located_in",
        verdict="verified",
        stability_class="decade_stable",
        ttl_seconds=10 * 365 * 24 * 3600,
        evidence={"snippets": [{"title": "wiki", "url": "x"}]},
    )
    hit = cache.lookup("k1")
    assert hit is not None
    assert hit.verdict == "verified"
    assert hit.stability_class == "decade_stable"
    assert hit.evidence == {"snippets": [{"title": "wiki", "url": "x"}]}
    assert hit.expires_at is not None
    store.close()


def test_lookup_miss_returns_none(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    assert cache.lookup("nonexistent") is None
    store.close()


def test_immutable_entry_has_no_expiry(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="quantitative", predicate="has_count",
        verdict="verified",
        stability_class="immutable",
        ttl_seconds=None,
    )
    hit = cache.lookup("k1")
    assert hit is not None
    assert hit.expires_at is None
    store.close()


def test_volatile_ttl_zero_does_not_persist(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="quantitative", predicate="stock_price",
        verdict="verified",
        stability_class="volatile",
        ttl_seconds=0,
    )
    # Volatile-TTL writes are deliberately no-ops — caller should
    # have gated, but if they didn't we don't store stale-on-arrival.
    assert cache.lookup("k1") is None
    store.close()


def test_expired_entry_returns_none(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="quantitative", predicate="stock_price",
        verdict="verified",
        stability_class="days_stable",
        ttl_seconds=24 * 3600,
    )
    # Force expiry to the past.
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        "UPDATE verification_cache SET expires_at = ? WHERE canonical_key = ?",
        (past, "k1"),
    )
    store._conn.commit()
    assert cache.lookup("k1") is None
    store.close()


def test_lookup_increments_hit_count(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="spatial_temporal", predicate="located_in",
        verdict="verified",
        stability_class="decade_stable",
        ttl_seconds=10 * 365 * 24 * 3600,
    )
    h1 = cache.lookup("k1")
    h2 = cache.lookup("k1")
    h3 = cache.lookup("k1")
    assert h1.hit_count == 1
    assert h2.hit_count == 2
    assert h3.hit_count == 3
    store.close()


def test_write_upserts_existing_key(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="spatial_temporal", predicate="located_in",
        verdict="verified",
        stability_class="decade_stable",
        ttl_seconds=10 * 365 * 24 * 3600,
    )
    # Same key, updated verdict.
    cache.write(
        canonical_key="k1",
        pattern="spatial_temporal", predicate="located_in",
        verdict="contradicted",
        stability_class="years_stable",
        ttl_seconds=365 * 24 * 3600,
    )
    hit = cache.lookup("k1")
    assert hit.verdict == "contradicted"
    assert hit.stability_class == "years_stable"
    # Only one row in the table — UPSERT, not INSERT.
    rows = store._conn.execute(
        "SELECT COUNT(*) AS n FROM verification_cache"
    ).fetchone()
    assert rows["n"] == 1
    store.close()


def test_expire_now(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="x", predicate="y", verdict="verified",
        stability_class="decade_stable",
        ttl_seconds=10 * 365 * 24 * 3600,
    )
    assert cache.lookup("k1") is not None
    cache.expire_now("k1")
    assert cache.lookup("k1") is None
    store.close()


def test_malformed_expires_at_treated_as_expired(tmp_path):
    """If expires_at can't be parsed (corrupt DB row), treat the entry
    as expired and return None — re-derive on next call."""
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1", pattern="x", predicate="y",
        verdict="verified", stability_class="decade_stable",
        ttl_seconds=10 * 365 * 24 * 3600,
    )
    # Corrupt the expires_at field.
    store._conn.execute(
        "UPDATE verification_cache SET expires_at = ? WHERE canonical_key = ?",
        ("not-a-valid-iso-timestamp", "k1"),
    )
    store._conn.commit()
    assert cache.lookup("k1") is None
    store.close()


def test_cached_verdict_to_dict_shape(tmp_path):
    """CachedVerdict.to_dict serializes the full payload — field
    rename safety."""
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1", pattern="spatial_temporal",
        predicate="located_in", verdict="verified",
        stability_class="immutable",
        ttl_seconds=None,
        evidence={"snippets": ["x", "y"]},
    )
    hit = cache.lookup("k1")
    d = hit.to_dict()
    assert set(d.keys()) == {
        "canonical_key", "pattern", "predicate", "verdict", "evidence",
        "stability_class", "cached_at", "expires_at", "hit_count",
    }
    assert d["verdict"] == "verified"
    assert d["evidence"] == {"snippets": ["x", "y"]}
    assert d["expires_at"] is None
    store.close()


def test_stats_aggregates_correctly(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(canonical_key="k1", pattern="x", predicate="y",
                verdict="verified", stability_class="immutable",
                ttl_seconds=None)
    cache.write(canonical_key="k2", pattern="x", predicate="y",
                verdict="verified", stability_class="decade_stable",
                ttl_seconds=10 * 365 * 24 * 3600)
    cache.lookup("k1")
    cache.lookup("k1")
    cache.lookup("k2")

    s = cache.stats()
    assert s["total_entries"] == 2
    assert s["immutable_entries"] == 1
    assert s["total_hits"] == 3
    store.close()


# ---- VerificationCache.semantic_lookup ----------------------------------


def _write_relational(cache, *, predicate, subject, obj, verdict="verified"):
    """Helper: write a cache entry with a deterministic canonical_key
    so semantic_lookup can find it in tests below."""
    claim = {
        "pattern": "relational", "predicate": predicate,
        "slots": {"subject": subject, "object": obj, "relation": predicate},
        "polarity": 1,
    }
    key = canonicalize_claim_key(claim)
    cache.write(
        canonical_key=key, pattern="relational", predicate=predicate,
        verdict=verdict, stability_class="immutable", ttl_seconds=None,
    )
    return key


def test_semantic_lookup_finds_synonymous_predicate(tmp_path):
    """The user's case beyond stem-stripping: ``son_of`` cached, looking
    up ``child_of``. Tokens overlap (``of``), Jaccard 1/3 = 0.33 — below
    the default 0.7 threshold, so this would NOT match. Use a closer
    pair: ``is_child_of`` already stem-collapses, but ``has_child_of``
    vs ``child_of`` is a real test of the semantic layer when stem
    miss occurs."""
    store = FactStore(tmp_path / "s.db")
    cache = VerificationCache(store)

    # Cache a fact under ``child_of``.
    _write_relational(cache, predicate="child_of",
                      subject="barron trump", obj="donald trump")

    # Lookup with a structurally-equivalent predicate ``begat`` won't
    # match (no token overlap → Jaccard 0). Lookup with ``child`` (one
    # token in common with ``child_of``: "child") gets Jaccard 1/2 =
    # 0.5, still below threshold. Lookup with the same predicate
    # under a different stem prefix is the realistic case.
    hit = cache.semantic_lookup({
        "pattern": "relational", "predicate": "child",
        "slots": {"subject": "barron trump", "object": "donald trump",
                  "relation": "child"},
        "polarity": 1,
    }, identity_slot_names=["subject", "object"], threshold=0.5)
    assert hit is not None
    assert hit.verdict.predicate == "child_of"
    assert hit.score == 0.5  # tokens {child} vs {child, of}
    store.close()


def test_semantic_lookup_anchored_on_identity_slots(tmp_path):
    """Reversed-relation safety: parent_of(A,B) cached should NOT
    match child_of(B,A) — different identity-slot values mean
    different shape, no candidate."""
    store = FactStore(tmp_path / "s2.db")
    cache = VerificationCache(store)

    # Cache: Donald Trump parent_of Barron Trump
    _write_relational(cache, predicate="parent_of",
                      subject="donald trump", obj="barron trump")

    # Lookup: Barron Trump child_of Donald Trump (same relationship,
    # different identity slots). Identity-slot anchor MUST prevent
    # this from matching.
    hit = cache.semantic_lookup({
        "pattern": "relational", "predicate": "child_of",
        "slots": {"subject": "barron trump", "object": "donald trump",
                  "relation": "child_of"},
        "polarity": 1,
    }, identity_slot_names=["subject", "object"], threshold=0.5)
    assert hit is None
    store.close()


def test_semantic_lookup_threshold_excludes_low_overlap(tmp_path):
    """``founded_by`` cached, lookup ``built_by`` — no token overlap.
    Jaccard 0 → no match regardless of identity-slot match."""
    store = FactStore(tmp_path / "s3.db")
    cache = VerificationCache(store)
    _write_relational(cache, predicate="founded_by",
                      subject="apple", obj="steve jobs")
    hit = cache.semantic_lookup({
        "pattern": "relational", "predicate": "built_by",
        "slots": {"subject": "apple", "object": "steve jobs",
                  "relation": "built_by"},
        "polarity": 1,
    }, identity_slot_names=["subject", "object"], threshold=0.5)
    assert hit is None
    store.close()


def test_semantic_lookup_polarity_distinguishes(tmp_path):
    """polarity=1 cached, polarity=0 lookup — must NOT match. Negation
    flips meaning entirely."""
    store = FactStore(tmp_path / "s4.db")
    cache = VerificationCache(store)
    _write_relational(cache, predicate="located_in",
                      subject="tokyo", obj="japan")
    hit = cache.semantic_lookup({
        "pattern": "relational", "predicate": "located_in",
        "slots": {"subject": "tokyo", "object": "japan",
                  "relation": "located_in"},
        "polarity": 0,
    }, identity_slot_names=["subject", "object"], threshold=0.5)
    assert hit is None
    store.close()


def test_semantic_lookup_returns_none_with_no_identity_slots(tmp_path):
    """Without at least one identity slot to anchor, semantic lookup
    is unsafe (would degrade to whole-cache text similarity). Return
    None rather than risk a wrong match."""
    store = FactStore(tmp_path / "s5.db")
    cache = VerificationCache(store)
    _write_relational(cache, predicate="x",
                      subject="a", obj="b")
    hit = cache.semantic_lookup({
        "pattern": "relational", "predicate": "x",
        "slots": {},  # no identity slot values present
        "polarity": 1,
    }, identity_slot_names=["subject", "object"], threshold=0.0)
    assert hit is None
    store.close()


def test_semantic_lookup_skips_expired_entries(tmp_path):
    """Mirrors the lookup() expiry contract — expired entries are not
    returned even if they'd otherwise be the best semantic match."""
    from datetime import datetime, timedelta, timezone
    store = FactStore(tmp_path / "s6.db")
    cache = VerificationCache(store)
    key = _write_relational(cache, predicate="X_of",
                            subject="a", obj="b")
    # Force expiry.
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        "UPDATE verification_cache SET expires_at = ? WHERE canonical_key = ?",
        (past, key),
    )
    store._conn.commit()

    hit = cache.semantic_lookup({
        "pattern": "relational", "predicate": "X_of",
        "slots": {"subject": "a", "object": "b", "relation": "X_of"},
        "polarity": 1,
    }, identity_slot_names=["subject", "object"], threshold=0.0)
    assert hit is None
    store.close()


def test_semantic_lookup_bumps_hit_count(tmp_path):
    """Semantic hits count toward the matched entry's lifetime
    utility — same accounting as exact lookup()."""
    store = FactStore(tmp_path / "s7.db")
    cache = VerificationCache(store)
    _write_relational(cache, predicate="child_of",
                      subject="x", obj="y")
    hit = cache.semantic_lookup({
        "pattern": "relational", "predicate": "child",
        "slots": {"subject": "x", "object": "y", "relation": "child"},
        "polarity": 1,
    }, identity_slot_names=["subject", "object"], threshold=0.4)
    assert hit is not None
    assert hit.verdict.hit_count == 1  # bumped from 0
    # Persisted on the row.
    row = store._conn.execute(
        "SELECT hit_count FROM verification_cache WHERE canonical_key = ?",
        (hit.matched_key,),
    ).fetchone()
    assert row["hit_count"] == 1
    store.close()


def test_semantic_lookup_picks_best_score_among_candidates(tmp_path):
    """When multiple candidates exceed threshold, the highest-score
    one wins. Predicate ``child_of`` is closer to ``child`` than
    ``descendant`` is."""
    store = FactStore(tmp_path / "s8.db")
    cache = VerificationCache(store)
    _write_relational(cache, predicate="descendant",
                      subject="x", obj="y")
    _write_relational(cache, predicate="child_of",
                      subject="x", obj="y")
    hit = cache.semantic_lookup({
        "pattern": "relational", "predicate": "child",
        "slots": {"subject": "x", "object": "y", "relation": "child"},
        "polarity": 1,
    }, identity_slot_names=["subject", "object"], threshold=0.0)
    # ``child`` vs ``descendant``: 0/2 = 0
    # ``child`` vs ``child_of``:    1/2 = 0.5
    # Best is child_of.
    assert hit is not None
    assert hit.verdict.predicate == "child_of"
    assert hit.score == 0.5
    store.close()


# ---- schema-level tests (was test_verification_cache_schema.py) -----------


def test_verification_cache_table_exists(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cols = store._conn.execute("PRAGMA table_info(verification_cache)").fetchall()
    col_names = {c["name"] for c in cols}
    assert col_names == {
        "id", "canonical_key", "pattern", "predicate", "verdict",
        "evidence", "stability_class", "cached_at", "expires_at",
        "hit_count", "created_at",
    }
    store.close()


def test_verification_cache_has_unique_key_index(tmp_path):
    store = FactStore(tmp_path / "v.db")
    indexes = store._conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name='verification_cache'"
    ).fetchall()
    by_name = {row["name"]: row["sql"] for row in indexes}
    assert "idx_verification_cache_key" in by_name
    assert "UNIQUE" in (by_name["idx_verification_cache_key"] or "").upper()
    assert "idx_verification_cache_expires" in by_name
    store.close()


def test_verification_cache_unique_key_constraint(tmp_path):
    import sqlite3
    store = FactStore(tmp_path / "v.db")
    store._conn.execute(
        "INSERT INTO verification_cache (canonical_key, pattern, predicate, "
        "verdict, stability_class, cached_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("k1", "spatial_temporal", "located_in", "verified",
         "decade_stable", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    store._conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO verification_cache (canonical_key, pattern, predicate, "
            "verdict, stability_class, cached_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("k1", "spatial_temporal", "located_in", "verified",
             "decade_stable", "2026-01-02T00:00:00", "2026-01-02T00:00:00"),
        )
    store.close()


def test_cache_pipeline_event_stages_registered(tmp_path):
    """The 4 cache-related stages must be in PIPELINE_STAGES so the
    scoping classifier can log without raising."""
    from src.fact_store import PIPELINE_STAGES
    for stage in (
        "cache_scoping_decision", "cache_stability_decision",
        "cache_lookup", "cache_write",
    ):
        assert stage in PIPELINE_STAGES, f"missing pipeline stage: {stage}"

    store = FactStore(tmp_path / "v.db")
    turn_id = store.insert_turn("user", "test")
    for stage in (
        "cache_scoping_decision", "cache_stability_decision",
        "cache_lookup", "cache_write",
    ):
        store.insert_pipeline_event(turn_id, stage, {"smoke": True})
    events = store.get_pipeline_events(turn_id)
    stages = {e["stage"] for e in events}
    assert stages.issuperset({"cache_scoping_decision",
                              "cache_stability_decision",
                              "cache_lookup", "cache_write"})
    store.close()


def test_legacy_db_migration_adds_verification_cache(tmp_path):
    """A pre-v0.6 DB without the table should pick it up on FactStore
    open."""
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern TEXT NOT NULL, predicate TEXT NOT NULL,
            slots TEXT NOT NULL, polarity INTEGER NOT NULL,
            confidence REAL NOT NULL, asserted_by TEXT NOT NULL,
            verification_status TEXT NOT NULL,
            valid_from TEXT, valid_until TEXT,
            source_turn_id INTEGER, source_text TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL, content TEXT NOT NULL,
            original_content TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE pipeline_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id INTEGER NOT NULL, stage TEXT NOT NULL,
            data TEXT NOT NULL, created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

    store = FactStore(db)
    cols = store._conn.execute("PRAGMA table_info(verification_cache)").fetchall()
    assert len(cols) > 0
    store.close()
