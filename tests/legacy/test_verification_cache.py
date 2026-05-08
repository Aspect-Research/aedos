"""Tests for the v0.6 VerificationCache + canonicalize_claim_key."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from src.cache.verification_cache import (
    VerificationCache,
    canonicalize_claim_key,
)
from src.legacy.fact_store import FactStore


# ---- canonicalize_claim_key --------------------------------------------


def test_canonicalize_basic():
    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1,
    }
    key = canonicalize_claim_key(claim)
    # v0.7.6: includes a tense bit (`t=present`/`t=past`) so past- and
    # present-tense versions of the same structured claim cache
    # independently. No source_text → defaults to present.
    assert key == "spatial_temporal|located_in|p=1|t=present|entity=tokyo&location=japan"


def test_canonicalize_past_tense_distinct_from_present():
    """A past-tense source_text produces a different canonical key
    than the same claim with present-tense source. Without this
    split, the tense-aware judge's verdicts could leak across — a
    SUPPORTED past-tense verdict for a dissolved entity must NOT
    serve a present-tense lookup of the same structured claim."""
    base = {
        "pattern": "categorical", "predicate": "is_a",
        "slots": {"entity": "Soviet Union", "category": "communist superpower"},
        "polarity": 1,
    }
    present = canonicalize_claim_key({**base, "source_text": "Soviet Union is a communist superpower"})
    past = canonicalize_claim_key({**base, "source_text": "Soviet Union was a communist superpower"})
    assert present != past
    assert "|t=present|" in present
    assert "|t=past|" in past


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
    # v0.7.10 added provenance + bookkeeping fields.
    # flagged_for_review removed when the flagging system was retired.
    assert set(d.keys()) == {
        "canonical_key", "pattern", "predicate", "verdict", "evidence",
        "stability_class", "cached_at", "expires_at", "hit_count",
        "evidence_hash", "source_urls", "confidence",
        "last_refreshed_at", "refresh_count", "contradiction_count",
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
        "evidence_hash", "source_urls",
        "last_refreshed_at", "refresh_count", "contradiction_count",
    }
    store.close()


def test_cache_invalidation_log_table_exists(tmp_path):
    """v0.7.11: every invalidation gets an audit row."""
    store = FactStore(tmp_path / "v.db")
    cols = store._conn.execute(
        "PRAGMA table_info(cache_invalidation_log)"
    ).fetchall()
    col_names = {c["name"] for c in cols}
    assert col_names == {
        "id", "reason", "primary_key", "propagated_to_keys",
        "detail", "triggered_by", "created_at",
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
    from src.legacy.fact_store import PIPELINE_STAGES
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


# ---- v0.7.8: WriteOutcome + COALESCE cached_at + prune + invalidate ----


def test_write_returns_inserted_outcome_on_first_write(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    outcome = cache.write(
        canonical_key="k1", pattern="x", predicate="y",
        verdict="verified", stability_class="decade_stable",
        ttl_seconds=365 * 24 * 3600,
    )
    assert outcome.action == "inserted"
    assert outcome.prior_verdict is None
    store.close()


def test_write_returns_refreshed_when_verdict_unchanged(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1", pattern="x", predicate="y",
        verdict="verified", stability_class="years_stable",
        ttl_seconds=90 * 24 * 3600,
    )
    outcome = cache.write(
        canonical_key="k1", pattern="x", predicate="y",
        verdict="verified", stability_class="years_stable",
        ttl_seconds=90 * 24 * 3600,
    )
    assert outcome.action == "refreshed"
    assert outcome.prior_verdict == "verified"
    store.close()


def test_write_returns_contradicted_when_verdict_flips(tmp_path):
    """A new verdict that disagrees with the cached one is the load-
    bearing signal — caller turns this into a
    cache_contradiction_replaced event."""
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1", pattern="x", predicate="y",
        verdict="verified", stability_class="years_stable",
        ttl_seconds=90 * 24 * 3600,
    )
    outcome = cache.write(
        canonical_key="k1", pattern="x", predicate="y",
        verdict="contradicted", stability_class="years_stable",
        ttl_seconds=90 * 24 * 3600,
    )
    assert outcome.action == "contradicted_and_replaced"
    assert outcome.prior_verdict == "verified"
    # And the row now reflects the new verdict.
    hit = cache.lookup("k1")
    assert hit.verdict == "contradicted"
    store.close()


def test_cached_at_preserved_across_refresh(tmp_path):
    """COALESCE on UPSERT means the original first-cache timestamp
    survives subsequent refreshes — useful for "how long has this
    fact been holding?" telemetry."""
    import time
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1", pattern="x", predicate="y",
        verdict="verified", stability_class="years_stable",
        ttl_seconds=90 * 24 * 3600,
    )
    original_cached_at = cache.lookup("k1").cached_at
    time.sleep(0.01)
    cache.write(
        canonical_key="k1", pattern="x", predicate="y",
        verdict="verified", stability_class="years_stable",
        ttl_seconds=90 * 24 * 3600,
    )
    refreshed_cached_at = cache.lookup("k1").cached_at
    assert refreshed_cached_at == original_cached_at
    store.close()


def test_prune_expired_removes_old_rows(tmp_path):
    """prune_expired deletes rows whose expires_at is more than
    ``grace_seconds`` in the past. Immutable rows (NULL expires_at)
    are never pruned."""
    from datetime import datetime, timedelta, timezone
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="immutable_key", pattern="x", predicate="y",
        verdict="verified", stability_class="immutable",
        ttl_seconds=None,
    )
    cache.write(
        canonical_key="alive_key", pattern="x", predicate="y",
        verdict="verified", stability_class="years_stable",
        ttl_seconds=90 * 24 * 3600,
    )
    # Force an old expires_at on a third row.
    cache.write(
        canonical_key="dead_key", pattern="x", predicate="y",
        verdict="verified", stability_class="days_stable",
        ttl_seconds=3600,
    )
    long_ago = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    store._conn.execute(
        "UPDATE verification_cache SET expires_at = ? WHERE canonical_key = ?",
        (long_ago, "dead_key"),
    )
    store._conn.commit()
    n = cache.prune_expired(grace_seconds=30 * 24 * 3600)
    assert n == 1
    assert cache.lookup("immutable_key") is not None
    assert cache.lookup("alive_key") is not None
    # dead_key gone
    assert cache.lookup("dead_key") is None
    store.close()


def test_invalidate_by_slot_removes_matching_entries(tmp_path):
    """invalidate_by_slot deletes every cache row whose canonical_key
    references the slot=value pair (case-folded match)."""
    from src.cache.verification_cache import canonicalize_claim_key
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)

    def make(claim):
        key = canonicalize_claim_key(claim)
        cache.write(
            canonical_key=key, pattern=claim["pattern"], predicate=claim["predicate"],
            verdict="verified", stability_class="years_stable",
            ttl_seconds=90 * 24 * 3600,
        )
        return key

    a = make({"pattern": "categorical", "predicate": "is_a",
              "slots": {"entity": "Soviet Union", "category": "communist superpower"},
              "polarity": 1})
    b = make({"pattern": "spatial_temporal", "predicate": "located_in",
              "slots": {"entity": "Soviet Union", "location": "Eurasia"},
              "polarity": 1})
    c = make({"pattern": "categorical", "predicate": "is_a",
              "slots": {"entity": "United States", "category": "country"},
              "polarity": 1})

    n = cache.invalidate_by_slot("entity", "Soviet Union")
    assert n == 2
    assert cache.lookup(a) is None
    assert cache.lookup(b) is None
    assert cache.lookup(c) is not None  # different entity untouched
    store.close()


def test_invalidate_by_slot_case_insensitive(tmp_path):
    from src.cache.verification_cache import canonicalize_claim_key
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    key = canonicalize_claim_key({
        "pattern": "categorical", "predicate": "is_a",
        "slots": {"entity": "Soviet Union", "category": "x"},
        "polarity": 1,
    })
    cache.write(
        canonical_key=key, pattern="categorical", predicate="is_a",
        verdict="verified", stability_class="years_stable",
        ttl_seconds=90 * 24 * 3600,
    )
    n = cache.invalidate_by_slot("entity", "soviet union")
    assert n == 1
    n = cache.invalidate_by_slot("entity", "SOVIET   UNION")  # whitespace + case
    assert n == 0  # already gone
    store.close()


# ---- v0.7.11: causal cascade + admin actions + log ----------------------


def test_invalidate_one_deletes_entry(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(canonical_key="k1", pattern="x", predicate="y",
                verdict="verified", stability_class="years_stable",
                ttl_seconds=90 * 24 * 3600)
    assert cache.invalidate_one("k1") is True
    assert cache.lookup("k1") is None
    assert cache.invalidate_one("k1") is False  # second call: nothing to delete
    store.close()


def test_invalidation_log_records_each_action(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(canonical_key="k1", pattern="x", predicate="y",
                verdict="verified", stability_class="years_stable",
                ttl_seconds=90 * 24 * 3600)
    cache.invalidate_one("k1")
    log = cache.recent_invalidations()
    actions = [e["reason"] for e in log]
    assert "admin_one" in actions
    # Every entry has the standard fields.
    for e in log:
        assert "primary_key" in e
        assert "triggered_by" in e
        assert "created_at" in e
    store.close()


def test_health_metrics_aggregate_correctly(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(canonical_key="k1", pattern="x", predicate="y",
                verdict="verified", stability_class="years_stable",
                ttl_seconds=90 * 24 * 3600)
    cache.write(canonical_key="k1", pattern="x", predicate="y",
                verdict="contradicted", stability_class="years_stable",
                ttl_seconds=90 * 24 * 3600)  # contradiction → contradiction_count++
    cache.write(canonical_key="k2", pattern="x", predicate="y",
                verdict="verified", stability_class="years_stable",
                ttl_seconds=90 * 24 * 3600)
    cache.write(canonical_key="k2", pattern="x", predicate="y",
                verdict="verified", stability_class="years_stable",
                ttl_seconds=90 * 24 * 3600)  # refresh → refresh_count++

    h = cache.health()
    assert h["total_entries"] == 2
    assert h["ever_contradicted_entries"] == 1
    assert h["total_contradictions"] == 1
    assert h["total_refreshes"] == 1
    assert "flagged_for_review" not in h
    store.close()


def test_write_populates_provenance_fields(tmp_path):
    """v0.7.10/v0.13: every successful write fills evidence_hash,
    source_urls, last_refreshed_at. Confidence is no longer stored
    on the cache row — it's a derived property of refresh_count and
    contradiction_count (see v0.13 frequentist refactor)."""
    from src.legacy.router.constants import confidence_from_counts
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    evidence = {
        "snippets": [{"title": "t1", "snippet": "s1", "url": "https://en.wikipedia.org/wiki/X"},
                     {"title": "t2", "snippet": "s2", "url": "https://en.wikipedia.org/wiki/Y"}],
        "verdict": "SUPPORTED",
    }
    cache.write(canonical_key="k1", pattern="x", predicate="y",
                verdict="verified", stability_class="years_stable",
                ttl_seconds=90 * 24 * 3600,
                evidence=evidence)
    hit = cache.lookup("k1")
    assert hit.evidence_hash is not None and len(hit.evidence_hash) == 64
    assert "https://en.wikipedia.org/wiki/X" in hit.source_urls
    assert "https://en.wikipedia.org/wiki/Y" in hit.source_urls
    # First write: refresh_count=0, contradiction_count=0 → 0.5.
    assert hit.refresh_count == 0
    assert hit.contradiction_count == 0
    assert hit.confidence == pytest.approx(confidence_from_counts(0, 0))
    assert hit.last_refreshed_at is not None
    store.close()



# ---- v0.13: cacheable-verdict gate (replaces confidence floor) ---------


def test_gate_does_not_cache_inconclusive_verdicts(tmp_path):
    """v0.13: retrieval_inconclusive is no longer in the cacheable-
    verdict set. Pre-v0.13 we wrote it then skipped via a confidence
    floor; now the verdict-set gate handles it directly."""
    from src.cache.gate import CacheGate, ClaimCacheState
    from src.cache.stability_classifier import StabilityDecision
    from types import SimpleNamespace

    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    gate = CacheGate(cache=cache, scoping_fn=None, stability_fn=None, store=store)

    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "X", "location": "Y"}, "polarity": 1,
    }
    key = canonicalize_claim_key(claim)
    gate._states[key] = ClaimCacheState(
        canonical_key=key,
        scope=SimpleNamespace(scope="world_fact"),
        stability=StabilityDecision(
            stability_class="years_stable", reason="r",
            ttl_seconds=90 * 24 * 3600,
        ),
    )
    inconclusive = SimpleNamespace(
        verification_status="retrieval_inconclusive",
        code_gen_result=None, retrieval_result=None,
        served_from_cache=False,
    )
    gate.maybe_write(inconclusive, claim, turn_id=1)
    assert cache.lookup(key) is None
    store.close()


def test_gate_caches_verified_verdicts(tmp_path):
    from src.cache.gate import CacheGate, ClaimCacheState
    from src.cache.stability_classifier import StabilityDecision
    from types import SimpleNamespace

    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    gate = CacheGate(cache=cache, scoping_fn=None, stability_fn=None, store=store)

    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "X", "location": "Y"}, "polarity": 1,
    }
    key = canonicalize_claim_key(claim)
    gate._states[key] = ClaimCacheState(
        canonical_key=key,
        scope=SimpleNamespace(scope="world_fact"),
        stability=StabilityDecision(
            stability_class="years_stable", reason="r",
            ttl_seconds=90 * 24 * 3600,
        ),
    )
    verified = SimpleNamespace(
        verification_status="verified",
        code_gen_result=None, retrieval_result=None,
        served_from_cache=False,
    )
    gate.maybe_write(verified, claim, turn_id=1)
    assert cache.lookup(key) is not None
    store.close()


def test_turn_savings_aggregates_hits(tmp_path):
    """Each hit bumps the per-turn counter; reset_for_turn clears it."""
    from src.cache.gate import CacheGate
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    gate = CacheGate(cache=cache, scoping_fn=None, stability_fn=None, store=store)
    assert gate.turn_savings()["hits"] == 0
    gate._record_hit_savings()
    gate._record_hit_savings()
    s = gate.turn_savings()
    assert s["hits"] == 2
    assert s["estimated_usd_saved"] > 0
    gate.reset_for_turn()
    assert gate.turn_savings()["hits"] == 0
    store.close()


# ---- v0.13: pure-frequentist confidence_from_counts -------------------


def test_confidence_from_counts_zero_evidence_is_uniform_prior():
    from src.legacy.router.constants import confidence_from_counts
    assert confidence_from_counts(0, 0) == pytest.approx(0.5)


def test_confidence_from_counts_grows_with_refreshes():
    from src.legacy.router.constants import confidence_from_counts
    c0 = confidence_from_counts(0, 0)
    c1 = confidence_from_counts(1, 0)
    c5 = confidence_from_counts(5, 0)
    c20 = confidence_from_counts(20, 0)
    assert c0 < c1 < c5 < c20 < 1.0
    # Asymptotic: large refresh count → close to 1.
    assert c20 > 0.9


def test_confidence_from_counts_penalized_by_contradictions():
    from src.legacy.router.constants import confidence_from_counts
    no_flips = confidence_from_counts(5, 0)
    one_flip = confidence_from_counts(5, 1)
    five_flips = confidence_from_counts(5, 5)
    assert no_flips > one_flip > five_flips


def test_confidence_from_counts_clamps_negatives():
    from src.legacy.router.constants import confidence_from_counts
    # Negative counts treated as 0.
    assert confidence_from_counts(-3, -2) == confidence_from_counts(0, 0)


# ---- v0.13: facts gain reinforcement_count + boost recomputes ----


def test_facts_table_has_reinforcement_count(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cols = {r["name"] for r in store._conn.execute(
        "PRAGMA table_info(facts)"
    ).fetchall()}
    assert "reinforcement_count" in cols
    store.close()


def test_boost_confidence_increments_reinforcement_count(tmp_path):
    """v0.13: boost_confidence increments reinforcement_count by 1
    and recomputes confidence as confidence_from_counts(R, 0)."""
    from src.legacy.fact_store import Fact
    from src.legacy.router.constants import confidence_from_counts
    store = FactStore(tmp_path / "v.db")
    fid = store.insert_fact(Fact(
        pattern="categorical", predicate="is_a",
        slots={"entity": "Tokyo", "category": "city"},
        polarity=1, confidence=confidence_from_counts(0, 0),
        asserted_by="model", verification_status="verified",
    ))
    new_conf = store.boost_confidence(fid)
    rows = store._conn.execute(
        "SELECT confidence, reinforcement_count FROM facts WHERE id = ?",
        (fid,),
    ).fetchone()
    assert rows["reinforcement_count"] == 1
    assert rows["confidence"] == pytest.approx(confidence_from_counts(1, 0))
    assert new_conf == rows["confidence"]
    store.close()


# ---- v0.13: model find-or-boost integration --------------------------


def test_router_store_or_boost_reuses_existing_model_fact(tmp_path):
    """Re-verifying the same model claim boosts the existing fact
    instead of inserting a duplicate. v0.13: confidence comes purely
    from reinforcement_count."""
    from src.legacy.pattern_registry import PatternRegistry
    from src.legacy.router.router import Router
    from src.legacy.router.constants import confidence_from_counts

    store = FactStore(tmp_path / "v.db")
    reg = PatternRegistry.from_yaml("patterns.yaml")
    router = Router(store=store, registry=reg)

    claim = {
        "pattern": "categorical", "predicate": "is_a",
        "slots": {"entity": "Tokyo", "category": "city"},
        "polarity": 1, "source_text": "Tokyo is a city",
    }
    fact_id_1, conf_1, count_1 = router._store_or_boost_model_fact(
        claim, source_turn_id=1, verification_status="verified",
    )
    assert count_1 == 0
    assert conf_1 == pytest.approx(confidence_from_counts(0, 0))

    fact_id_2, conf_2, count_2 = router._store_or_boost_model_fact(
        claim, source_turn_id=2, verification_status="verified",
    )
    assert fact_id_2 == fact_id_1
    assert count_2 == 1
    assert conf_2 == pytest.approx(confidence_from_counts(1, 0))
    assert conf_2 > conf_1
    store.close()


# ---- v0.7.14: tiered precedence verification -------------------------


def test_session_marker_detection():
    from src.legacy.session_markers import is_session_scoped
    # Positive cases.
    assert is_session_scoped("for this conversation, X = 5")
    assert is_session_scoped("In our discussion, A = B")
    assert is_session_scoped("let's say the cat is black")
    assert is_session_scoped("hypothetically, the budget is $100")
    assert is_session_scoped("In this scenario, the deadline is Friday")
    # Negative cases — common assertions that should NOT be session-scoped.
    assert not is_session_scoped("I like peanut butter")
    assert not is_session_scoped("Tokyo is a city in Japan")
    assert not is_session_scoped("I went to the store today")
    assert not is_session_scoped("")
    assert not is_session_scoped(None)


def test_facts_table_has_session_id_column(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cols = {r["name"] for r in store._conn.execute(
        "PRAGMA table_info(facts)"
    ).fetchall()}
    assert "session_id" in cols
    store.close()


def test_find_currently_valid_filters_by_session_id(tmp_path):
    """find_currently_valid honors the new session_id parameter:
    None = cross-session only, specific id = that session only,
    sentinel = any (legacy behavior)."""
    from src.legacy.fact_store import Fact
    store = FactStore(tmp_path / "v.db")
    # One cross-session, one session-scoped, same identity.
    cross = Fact(
        pattern="categorical", predicate="is_a",
        slots={"entity": "X", "category": "thing"},
        polarity=1,
        asserted_by="user", verification_status="user_asserted",
    )
    scoped = Fact(
        pattern="categorical", predicate="is_a",
        slots={"entity": "X", "category": "thing"},
        polarity=1,
        asserted_by="user", verification_status="user_asserted",
        session_id="sess_a",
    )
    store.insert_fact(cross)
    store.insert_fact(scoped)

    # Cross-session only.
    cross_only = store.find_currently_valid(
        "categorical", session_id=None,
    )
    assert all(f.session_id is None for f in cross_only)
    assert len(cross_only) == 1

    # Session-scoped only.
    scoped_only = store.find_currently_valid(
        "categorical", session_id="sess_a",
    )
    assert all(f.session_id == "sess_a" for f in scoped_only)
    assert len(scoped_only) == 1

    # Default sentinel = any.
    everything = store.find_currently_valid("categorical")
    assert len(everything) == 2
    store.close()


def test_microtheory_takes_precedence_over_user_store_in_pipeline(tmp_path):
    """A model claim that matches a session-scoped user fact gets
    served from microtheory (tier 1), not from cache or fresh."""
    from src.legacy.fact_store import Fact
    from src.legacy.pattern_registry import PatternRegistry
    from src.legacy.router.router import Router
    from src.legacy.pipeline import Pipeline
    from src.legacy.extractor import ClaimExtractor
    from src.legacy.corrector import Corrector

    class _MockLLM:
        def chat(self, *a, **k): return "draft"
        def extract_with_tool(self, *a, **k): return {"facts": []}
        def rewrite(self, *a, **k): return "rewrite"
        def pop_recorded_calls(self): return []

    store = FactStore(tmp_path / "v.db")
    reg = PatternRegistry.from_yaml("patterns.yaml")
    mock = _MockLLM()
    router = Router(store=store, registry=reg)
    p = Pipeline(
        store, reg, mock, ClaimExtractor(mock, reg),
        router, Corrector(mock),
        session_id="sess_test",
    )

    # Pre-populate a session-scoped user fact.
    store.insert_fact(Fact(
        pattern="categorical", predicate="is_a",
        slots={"entity": "Tokyo", "category": "city"},
        polarity=1,
        asserted_by="user", verification_status="user_asserted",
        session_id="sess_test",
    ))

    claim = {
        "pattern": "categorical", "predicate": "is_a",
        "slots": {"entity": "Tokyo", "category": "city"},
        "polarity": 1, "source_text": "Tokyo is a city",
    }
    decision = p._tier_microtheory_lookup(claim, turn_id=1)
    assert decision is not None
    assert decision.served_from_tier is None  # Pipeline assigns this in _stage_verify
    # The boost path means confidence rises above the prior.
    assert decision.boosted_fact_id is not None
    store.close()


def test_user_store_tier_returns_none_when_only_microtheory_match(tmp_path):
    """Tier 2 (cross-session) should NOT see session-scoped facts —
    they belong to tier 1."""
    from src.legacy.fact_store import Fact
    from src.legacy.pattern_registry import PatternRegistry
    from src.legacy.router.router import Router
    from src.legacy.pipeline import Pipeline
    from src.legacy.extractor import ClaimExtractor
    from src.legacy.corrector import Corrector

    class _MockLLM:
        def chat(self, *a, **k): return "x"
        def extract_with_tool(self, *a, **k): return {"facts": []}
        def rewrite(self, *a, **k): return "x"
        def pop_recorded_calls(self): return []

    store = FactStore(tmp_path / "v.db")
    reg = PatternRegistry.from_yaml("patterns.yaml")
    mock = _MockLLM()
    router = Router(store=store, registry=reg)
    p = Pipeline(
        store, reg, mock, ClaimExtractor(mock, reg),
        router, Corrector(mock),
        session_id="sess_test",
    )
    # Only a session-scoped fact exists; tier 2 (NULL session) should miss.
    store.insert_fact(Fact(
        pattern="categorical", predicate="is_a",
        slots={"entity": "Tokyo", "category": "city"},
        polarity=1,
        asserted_by="user", verification_status="user_asserted",
        session_id="sess_test",
    ))
    claim = {
        "pattern": "categorical", "predicate": "is_a",
        "slots": {"entity": "Tokyo", "category": "city"},
        "polarity": 1, "source_text": "Tokyo is a city",
    }
    assert p._tier_user_store_lookup(claim, turn_id=1) is None
    # But tier 1 hits.
    assert p._tier_microtheory_lookup(claim, turn_id=1) is not None
    store.close()


def test_router_stamps_session_id_on_session_scoped_user_assertion(tmp_path):
    """A user assertion whose source_text carries a session marker
    gets stored with session_id set (microtheory). Non-session-marked
    assertions stay session_id=NULL (cross-session)."""
    from src.legacy.fact_store import Fact
    from src.legacy.pattern_registry import PatternRegistry
    from src.legacy.router.router import Router
    store = FactStore(tmp_path / "v.db")
    reg = PatternRegistry.from_yaml("patterns.yaml")
    router = Router(store=store, registry=reg, session_id="sess_x")

    cross_session_claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "tea"},
        "polarity": 1, "source_text": "I like tea",
    }
    # v0.10.0: session_id stamping still rides on the user-asserted
    # storage path, which now requires a SELF-ATTRIBUTE claim (the
    # subject is the user). A categorical claim about "X" would be
    # routed through the world-claim verification path instead.
    session_scoped_claim = {
        "pattern": "preference", "predicate": "prefers",
        "slots": {"agent": "user", "object": "espresso"},
        "polarity": 1,
        "source_text": "for this conversation, I prefer espresso",
    }
    pattern_a = reg.get("preference")
    pattern_b = reg.get("preference")
    router._route_user(cross_session_claim, pattern_a, source_turn_id=1)
    router._route_user(session_scoped_claim, pattern_b, source_turn_id=2)

    facts = store.find_currently_valid("preference",
                                       slot_match={"object": "tea"})
    assert facts[0].session_id is None
    facts = store.find_currently_valid("preference",
                                       slot_match={"object": "espresso"})
    assert facts[0].session_id == "sess_x"
    store.close()


def test_router_stamps_session_id_via_raw_text_when_source_text_strips_marker(
    tmp_path,
):
    """**Phase 8.6 Bug 2 fix.** Real chat extractors strip session-marker
    phrases from ``source_text`` ("Let's say for this conversation I
    live in Williamsburg" → source_text "I live in Williamsburg").
    Pre-fix, the marker check ran on source_text alone and missed; the
    fact got stored cross-session even though the user explicitly
    bounded it.

    Phase 8.6: ``Router.route()`` accepts a ``raw_text`` param that
    cascades through to ``_store``, which uses it for the marker
    check. This test reproduces the chat-stream scenario: extractor
    has stripped the marker, but the pipeline passes the raw turn
    text via raw_text, and the fact lands in microtheory."""
    from src.legacy.pattern_registry import PatternRegistry
    from src.legacy.router.router import Router
    store = FactStore(tmp_path / "phase86b.db")
    reg = PatternRegistry.from_yaml("patterns.yaml")
    router = Router(store=store, registry=reg, session_id="sess_y")

    stripped_claim = {
        "pattern": "preference", "predicate": "prefers",
        "slots": {"agent": "user", "object": "espresso"},
        "polarity": 1,
        # Marker has been stripped by the extractor's projection.
        "source_text": "I prefer espresso",
    }
    router.route(
        stripped_claim, origin="user", source_turn_id=1,
        raw_text="Let's say for this conversation I prefer espresso",
    )

    facts = store.find_currently_valid("preference",
                                       slot_match={"object": "espresso"})
    assert len(facts) == 1
    assert facts[0].session_id == "sess_y", (
        "Phase 8.6 raw_text cascade failed: marker was in raw_text, "
        "extractor stripped it from source_text, but the storage path "
        f"still used source_text for the marker check (session_id="
        f"{facts[0].session_id!r})"
    )
    store.close()


def test_router_raw_text_none_preserves_legacy_source_text_check(tmp_path):
    """Backwards compatibility: when raw_text is omitted (default
    None), the marker check falls back to source_text. Pre-Phase-8.6
    callers (direct ``_route_user`` invocations) keep working
    unchanged."""
    from src.legacy.pattern_registry import PatternRegistry
    from src.legacy.router.router import Router
    store = FactStore(tmp_path / "phase86b_compat.db")
    reg = PatternRegistry.from_yaml("patterns.yaml")
    router = Router(store=store, registry=reg, session_id="sess_z")

    intact_claim = {
        "pattern": "preference", "predicate": "prefers",
        "slots": {"agent": "user", "object": "espresso"},
        "polarity": 1,
        "source_text": "for this conversation, I prefer espresso",
    }
    # No raw_text arg — default None — falls back to source_text.
    router.route(intact_claim, origin="user", source_turn_id=1)

    facts = store.find_currently_valid("preference",
                                       slot_match={"object": "espresso"})
    assert facts[0].session_id == "sess_z"
    store.close()


# ---- v0.7.16: combined classifier + prompt caching --------------------


def test_combined_classifier_returns_scope_and_stability_in_one_call():
    """One LLM call returns both scope and (when world_fact) stability."""
    from src.cache.classify_combined import classify_for_cache

    class _MockLLM:
        def __init__(self):
            self.calls = 0
        def extract_with_tool(self, system, user_message, tool, **_):
            self.calls += 1
            return {
                "scope": "world_fact",
                "scope_reason": "geographic fact",
                "scope_confidence": 0.99,
                "stability_class": "decade_stable",
                "stability_reason": "stable on decade timescale",
                "stability_confidence": 0.95,
            }
    mock = _MockLLM()
    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1, "source_text": "Tokyo is in Japan",
    }
    combined = classify_for_cache(claim, mock)
    assert mock.calls == 1
    assert combined.scoping.scope == "world_fact"
    assert combined.stability is not None
    assert combined.stability.stability_class == "decade_stable"
    assert combined.stability.ttl_seconds == 365 * 24 * 3600


def test_combined_classifier_skips_stability_for_non_world_fact():
    """When scope is user_specific or session_specific, stability is
    not required and returns None."""
    from src.cache.classify_combined import classify_for_cache

    class _MockLLM:
        def extract_with_tool(self, system, user_message, tool, **_):
            return {
                "scope": "user_specific",
                "scope_reason": "agent is the user",
                "scope_confidence": 0.99,
            }
    mock = _MockLLM()
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "tea"},
        "polarity": 1, "source_text": "I like tea",
    }
    combined = classify_for_cache(claim, mock)
    assert combined.scoping.scope == "user_specific"
    assert combined.stability is None


def test_combined_classifier_world_fact_without_stability_raises():
    """A world_fact verdict without a stability_class is malformed."""
    from src.cache.classify_combined import classify_for_cache

    class _MockLLM:
        def extract_with_tool(self, system, user_message, tool, **_):
            return {
                "scope": "world_fact",
                "scope_reason": "geographic fact",
                "scope_confidence": 0.99,
                # missing stability_class
            }
    mock = _MockLLM()
    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1, "source_text": "Tokyo is in Japan",
    }
    with pytest.raises(RuntimeError, match="stability_class"):
        classify_for_cache(claim, mock)


def test_cache_gate_uses_combined_classifier_when_wired():
    """CacheGate prefers combined_fn over the legacy two-call path."""
    from src.cache.gate import CacheGate
    from src.cache.classify_combined import CombinedDecision
    from src.cache.scoping_classifier import ScopingDecision
    from src.cache.stability_classifier import (
        StabilityDecision, STABILITY_TTL_SECONDS,
    )

    combined_calls = 0
    legacy_scope_calls = 0
    legacy_stab_calls = 0

    def fake_combined(claim):
        nonlocal combined_calls
        combined_calls += 1
        return CombinedDecision(
            scoping=ScopingDecision(scope="world_fact", reason="x"),
            stability=StabilityDecision(
                stability_class="years_stable", reason="x",
                ttl_seconds=STABILITY_TTL_SECONDS["years_stable"],
            ),
        )
    def fake_scope(claim):
        nonlocal legacy_scope_calls
        legacy_scope_calls += 1
        return ScopingDecision(scope="world_fact", reason="x")
    def fake_stab(claim):
        nonlocal legacy_stab_calls
        legacy_stab_calls += 1
        return StabilityDecision(
            stability_class="years_stable", reason="x",
            ttl_seconds=STABILITY_TTL_SECONDS["years_stable"],
        )

    gate = CacheGate(
        cache=None,  # disabled cache, but classify still runs since combined_fn is wired
        scoping_fn=fake_scope, stability_fn=fake_stab,
        combined_fn=fake_combined,
    )
    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "X", "location": "Y"},
        "polarity": 1, "source_text": "X in Y",
    }
    gate.classify(claim, turn_id=1)
    assert combined_calls == 1
    assert legacy_scope_calls == 0
    assert legacy_stab_calls == 0


def test_rewrite_uses_prompt_caching():
    """v0.7.16: rewrite() wraps the system prompt with cache_control:
    ephemeral so the (large, stable) judge / corrector / code-writer
    prompts become cacheable across the 5-minute Anthropic TTL.
    Verifies the kwargs shape that ends up at messages.create."""
    import inspect
    src = inspect.getsource(__import__("src.llm_client", fromlist=["LLMClient"]).LLMClient.rewrite)
    assert "cache_control" in src
    assert "ephemeral" in src
