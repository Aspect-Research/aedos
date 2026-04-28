"""Tier 2 verification cache (v0.6).

Owns the ``verification_cache`` table operations:

  * ``canonicalize_claim_key(claim)`` — produce a stable canonical key
    that collides for semantically-equivalent claims (case-folding,
    whitespace normalization, slot-order independence).
  * ``VerificationCache.lookup(key)`` — return a non-expired cached
    verdict if any.
  * ``VerificationCache.write(key, ...)`` — INSERT or UPDATE.
  * ``VerificationCache.expire_now(key)`` — used by tests + admin.

This module does NOT decide WHETHER to cache — that's the scoping +
stability classifiers. It just stores what they tell it to store.

Entity canonicalization beyond simple normalization (alias resolution,
fuzzy matching) is a future iteration. The structural lookup misses
when the wording differs even slightly; that's acceptable for v0.6.
A miss is one extra retrieval call — a wrong-key hit is worse.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.fact_store import FactStore, _now_iso


@dataclass
class CachedVerdict:
    canonical_key: str
    pattern: str
    predicate: str
    verdict: str  # "verified" / "contradicted" / "inconclusive"
    evidence: dict[str, Any] | None
    stability_class: str
    cached_at: str
    expires_at: str | None
    hit_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_key": self.canonical_key,
            "pattern": self.pattern,
            "predicate": self.predicate,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "stability_class": self.stability_class,
            "cached_at": self.cached_at,
            "expires_at": self.expires_at,
            "hit_count": self.hit_count,
        }


def canonicalize_claim_key(claim: dict) -> str:
    """Produce a stable string key for cache lookup.

    Rules:
      * pattern + predicate are required and case-folded.
      * Slot keys are sorted alphabetically (so {a:1,b:2} == {b:2,a:1}).
      * Slot values are case-folded if string, str()'d otherwise.
      * Whitespace inside string values is collapsed.
      * Polarity is included so positive and negative claims don't
        collide (Tokyo IS in Japan vs Tokyo is NOT in Japan).

    Example: a claim
        {pattern:'spatial_temporal', predicate:'located_in',
         slots:{entity:'Tokyo', location:'Japan'}, polarity:1}
    canonicalizes to:
        'spatial_temporal|located_in|p=1|entity=tokyo&location=japan'
    """
    pattern = str(claim.get("pattern", "")).strip().lower()
    predicate = str(claim.get("predicate", "")).strip().lower()
    polarity = int(claim.get("polarity", 1))
    slots = claim.get("slots") or {}
    parts: list[str] = []
    for k in sorted(slots.keys()):
        v = slots[k]
        if isinstance(v, str):
            v = " ".join(v.split()).lower()
        else:
            v = json.dumps(v, default=str, sort_keys=True)
        parts.append(f"{k}={v}")
    slots_block = "&".join(parts)
    return f"{pattern}|{predicate}|p={polarity}|{slots_block}"


class VerificationCache:
    def __init__(self, store: FactStore):
        self.store = store

    def lookup(self, canonical_key: str) -> Optional[CachedVerdict]:
        """Return a cached verdict if it exists and hasn't expired.

        Increments hit_count on a hit (even if stale; the count tracks
        attempted lookups). Returns None for miss, expired, or unknown
        key.
        """
        row = self.store._conn.execute(
            "SELECT * FROM verification_cache WHERE canonical_key = ?",
            (canonical_key,),
        ).fetchone()
        if row is None:
            return None

        # Expiry check (None = immutable).
        expires_at = row["expires_at"]
        if expires_at is not None:
            try:
                expiry = datetime.fromisoformat(expires_at)
            except ValueError:
                # Malformed expiry — treat as expired so we re-derive.
                return None
            if expiry < datetime.now(timezone.utc):
                return None

        # Hit. Bump counter (best-effort — never crash a verification on
        # this).
        try:
            self.store._conn.execute(
                "UPDATE verification_cache SET hit_count = hit_count + 1 "
                "WHERE id = ?", (row["id"],),
            )
            self.store._conn.commit()
        except Exception:
            pass

        return CachedVerdict(
            canonical_key=row["canonical_key"],
            pattern=row["pattern"],
            predicate=row["predicate"],
            verdict=row["verdict"],
            evidence=json.loads(row["evidence"]) if row["evidence"] else None,
            stability_class=row["stability_class"],
            cached_at=row["cached_at"],
            expires_at=row["expires_at"],
            hit_count=int(row["hit_count"]) + 1,
        )

    def write(
        self,
        canonical_key: str,
        *,
        pattern: str,
        predicate: str,
        verdict: str,
        stability_class: str,
        ttl_seconds: int | None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        """INSERT a new entry, or UPDATE if the key already exists.

        ``ttl_seconds`` semantics:
          * None  → no expiry (immutable)
          * 0     → don't cache (caller should not have called this;
                    we treat as instant-expiry — equivalent to never
                    cached, lookup will miss on the next call)
          * > 0   → expires_at = now + ttl_seconds
        """
        if ttl_seconds == 0:
            # The caller was supposed to gate on this; tolerate but
            # don't actually persist anything that's already expired.
            return

        now = datetime.now(timezone.utc)
        cached_at = now.isoformat()
        expires_at: str | None
        if ttl_seconds is None:
            expires_at = None
        else:
            expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()

        evidence_json = json.dumps(evidence, default=str) if evidence else None

        self.store._conn.execute(
            """
            INSERT INTO verification_cache (
                canonical_key, pattern, predicate, verdict, evidence,
                stability_class, cached_at, expires_at, hit_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(canonical_key) DO UPDATE SET
                verdict = excluded.verdict,
                evidence = excluded.evidence,
                stability_class = excluded.stability_class,
                cached_at = excluded.cached_at,
                expires_at = excluded.expires_at
            """,
            (canonical_key, pattern, predicate, verdict, evidence_json,
             stability_class, cached_at, expires_at, _now_iso()),
        )
        self.store._conn.commit()

    def expire_now(self, canonical_key: str) -> None:
        """Force a cached entry to expire immediately. Test/admin tool."""
        now = datetime.now(timezone.utc).isoformat()
        self.store._conn.execute(
            "UPDATE verification_cache SET expires_at = ? "
            "WHERE canonical_key = ?", (now, canonical_key),
        )
        self.store._conn.commit()

    def stats(self) -> dict[str, Any]:
        """Aggregate stats for the trace UI / admin."""
        row = self.store._conn.execute(
            "SELECT COUNT(*) AS total, "
            "       COUNT(CASE WHEN expires_at IS NULL THEN 1 END) AS immutable, "
            "       SUM(hit_count) AS total_hits "
            "FROM verification_cache"
        ).fetchone()
        return {
            "total_entries": int(row["total"] or 0),
            "immutable_entries": int(row["immutable"] or 0),
            "total_hits": int(row["total_hits"] or 0),
        }
