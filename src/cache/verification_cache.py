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
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.fact_store import FactStore, _now_iso


@dataclass
class SemanticHit:
    """Result of VerificationCache.semantic_lookup. Distinct from
    CachedVerdict so the caller can tell exact hits from
    semantic-shape hits and log them differently in the trace."""
    verdict: "CachedVerdict"
    matched_key: str
    score: float  # Jaccard similarity over predicate tokens, 0..1


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


# Predicate-prefix stems we strip during canonicalization so semantically-
# equivalent predicates collide on the same cache key. Order matters
# only in length — we strip longest match first so "were_" beats "we_"
# (not in the list, but illustrates the principle).
_PREDICATE_PREFIX_STEMS = (
    "were_", "was_", "are_", "has_", "have_", "is_",
    "does_", "did_", "do_",
)

# Slot keys whose VALUES are predicate-shaped identifiers (i.e. they
# carry the relation/predicate name as data) and should be stem-
# normalized too. Without this, two cache entries with identical
# predicate fields but ``slots.relation`` differing in is_/has_ prefix
# still produce different keys — exactly the user's child_of /
# is_child_of case where the predicate appears both at the top level
# and inside slots.
_PREDICATE_SHAPED_SLOTS = ("relation", "predicate", "relation_kind")


def _normalize_predicate(p: str) -> str:
    """Strip common stem prefixes (is_, has_, was_, etc.) from a
    predicate-shaped string and return the lowercased core. Pure
    string operation; no LLM, no synonym table.

    Catches the user's reported false-miss: ``is_child_of`` and
    ``child_of`` both → ``child_of``. Doesn't catch deeper synonymy
    like ``son_of`` ↔ ``child_of`` (handled by the semantic-shape
    lookup layer)."""
    p = (p or "").strip().lower()
    for stem in _PREDICATE_PREFIX_STEMS:
        if p.startswith(stem) and len(p) > len(stem):
            return p[len(stem):]
    return p


def _predicate_tokens(predicate: str) -> set[str]:
    """Tokenize a predicate string for Jaccard similarity. Lowercase,
    strip stems, split on underscores. Empty / single-token predicates
    return as-is. Used by VerificationCache.semantic_lookup."""
    p = _normalize_predicate(predicate or "")
    if not p:
        return set()
    return {tok for tok in p.split("_") if tok}


def _parse_slots_block(slots_block: str) -> dict[str, str]:
    """Reverse the ``key=val&key=val`` encoding from
    canonicalize_claim_key. Used by semantic_lookup to filter cached
    rows on identity-slot equality without needing a separate index."""
    out: dict[str, str] = {}
    if not slots_block:
        return out
    for pair in slots_block.split("&"):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        out[k] = v
    return out


# Past-tense markers that distinguish a historical claim from a
# present-tense one purely by inspecting source_text. Kept narrow on
# purpose: false positives (treating present-tense as past) only mean
# the cache key splits where it could have collided — a small loss in
# hit rate, not a correctness problem. False negatives (missed past
# tense) keep current behavior.
_PAST_TENSE_MARKERS = re.compile(
    r"\b(was|were|had been|used to|formerly|previously|once|"
    r"originally)\b",
    re.IGNORECASE,
)


def claim_tense(claim: dict) -> str:
    """Return 'past' if the claim's source_text shows past-tense
    markers, else 'present'. Used as a cache-key dimension so that
    past-tense and present-tense versions of the same structured claim
    cache independently — a SUPPORTED past-tense verdict ("USSR was
    a superpower") MUST NOT serve a present-tense lookup ("USSR is a
    superpower"), and vice versa. Mirrors the tense-awareness rule the
    retrieval judge uses to interpret the same source_text."""
    source_text = claim.get("source_text") or ""
    if _PAST_TENSE_MARKERS.search(source_text):
        return "past"
    return "present"


def canonicalize_claim_key(claim: dict) -> str:
    """Produce a stable string key for cache lookup.

    Rules:
      * pattern + predicate are required and case-folded.
      * Predicate (and predicate-shaped slot values: ``relation``,
        ``predicate``, ``relation_kind``) get stem normalization —
        ``is_child_of`` / ``has_child_of`` → ``child_of`` so
        equivalent predicates collide on the same key.
      * Slot keys are sorted alphabetically (so {a:1,b:2} == {b:2,a:1}).
      * Slot values are case-folded if string, str()'d otherwise.
      * Whitespace inside string values is collapsed.
      * Polarity is included so positive and negative claims don't
        collide (Tokyo IS in Japan vs Tokyo is NOT in Japan).
      * Tense (past/present, derived from source_text) is included so
        the tense-aware judge's verdict for a past-tense claim doesn't
        get served to a present-tense lookup.

    Example: a claim
        {pattern:'spatial_temporal', predicate:'located_in',
         slots:{entity:'Tokyo', location:'Japan'}, polarity:1,
         source_text:'Tokyo is in Japan'}
    canonicalizes to:
        'spatial_temporal|located_in|p=1|t=present|entity=tokyo&location=japan'

    With stem normalization, both
        {predicate:'is_child_of', slots:{relation:'is_child_of', ...}}
    and
        {predicate:'child_of',    slots:{relation:'child_of',    ...}}
    canonicalize to the same key (assuming same tense).
    """
    pattern = str(claim.get("pattern", "")).strip().lower()
    predicate = _normalize_predicate(str(claim.get("predicate", "")))
    polarity = int(claim.get("polarity", 1))
    tense = claim_tense(claim)
    slots = claim.get("slots") or {}
    parts: list[str] = []
    for k in sorted(slots.keys()):
        v = slots[k]
        if isinstance(v, str):
            v = " ".join(v.split()).lower()
            # Stem-normalize predicate-shaped slot values so the slot
            # block matches the top-level predicate's normalization.
            if k in _PREDICATE_SHAPED_SLOTS:
                v = _normalize_predicate(v)
        else:
            v = json.dumps(v, default=str, sort_keys=True)
        parts.append(f"{k}={v}")
    slots_block = "&".join(parts)
    return f"{pattern}|{predicate}|p={polarity}|t={tense}|{slots_block}"


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

    def semantic_lookup(
        self, claim: dict, identity_slot_names: list[str],
        *, threshold: float = 0.5,
    ) -> Optional["SemanticHit"]:
        """Find a cache entry whose IDENTITY slots match the claim
        exactly and whose predicate is similar enough by Jaccard token
        overlap.

        Anchoring on identity slots (subject, object, entity, etc.)
        rules out the dangerous reversed-relation case
        (parent_of(A,B) ↔ child_of(B,A)) — different identity-slot
        values means different shape, no match.

        Layer 2 of the semantic-cache strategy. Catches cases that
        the canonicalize_claim_key stem normalizer doesn't reach
        (e.g. ``child_of`` ↔ ``son_of`` when token overlap is high
        enough). Pure Python — no LLM, no embedding.

        Returns a SemanticHit (CachedVerdict + matched_key + score)
        on match, or None on no candidate above threshold.
        """
        slots = claim.get("slots") or {}
        pattern = str(claim.get("pattern", "")).strip().lower()
        polarity = int(claim.get("polarity", 1))
        tense = claim_tense(claim)

        # Need at least one identity slot to anchor; without anchoring
        # we'd be doing pure-text similarity across the whole cache,
        # which is unsafe.
        identity_pairs = []
        for name in identity_slot_names:
            v = slots.get(name)
            if isinstance(v, str) and v.strip():
                identity_pairs.append((name, " ".join(v.split()).lower()))
        if not identity_pairs:
            return None

        # Fetch candidate rows: same pattern + polarity. We filter on
        # identity slot values in Python (the cache table doesn't index
        # individual slots; the cache is small so a full scan within
        # this pattern is fine).
        rows = self.store._conn.execute(
            "SELECT * FROM verification_cache WHERE pattern = ?",
            (pattern,),
        ).fetchall()
        if not rows:
            return None

        my_pred_tokens = _predicate_tokens(claim.get("predicate", ""))
        if not my_pred_tokens:
            return None

        best: Optional[SemanticHit] = None
        for row in rows:
            cached_key = row["canonical_key"]
            # Polarity must match — encoded in the key as ``p=1``/``p=0``.
            if f"|p={polarity}|" not in cached_key:
                continue
            # Tense must match too — past-tense and present-tense
            # versions of the same structured claim cache independently.
            if f"|t={tense}|" not in cached_key:
                continue
            # Identity slots must match exactly. Each identity pair
            # appears in the slots block as ``name=value``.
            slots_block = cached_key.rsplit("|", 1)[-1]
            slot_dict = _parse_slots_block(slots_block)
            if not all(slot_dict.get(name) == value
                       for name, value in identity_pairs):
                continue

            # Jaccard token similarity on the predicate strings.
            cached_pred = row["predicate"] or ""
            cached_pred_tokens = _predicate_tokens(cached_pred)
            if not cached_pred_tokens:
                continue
            inter = my_pred_tokens & cached_pred_tokens
            union = my_pred_tokens | cached_pred_tokens
            score = len(inter) / len(union) if union else 0.0
            if score < threshold:
                continue

            # Skip expired entries (mirrors lookup() semantics).
            expires_at = row["expires_at"]
            if expires_at is not None:
                try:
                    if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
                        continue
                except ValueError:
                    continue

            if best is None or score > best.score:
                verdict = CachedVerdict(
                    canonical_key=cached_key,
                    pattern=row["pattern"],
                    predicate=row["predicate"],
                    verdict=row["verdict"],
                    evidence=json.loads(row["evidence"]) if row["evidence"] else None,
                    stability_class=row["stability_class"],
                    cached_at=row["cached_at"],
                    expires_at=row["expires_at"],
                    hit_count=int(row["hit_count"]),
                )
                best = SemanticHit(
                    verdict=verdict, matched_key=cached_key, score=score,
                )

        # Bump hit_count on the matched row so semantic hits count
        # toward the entry's lifetime utility — same accounting as
        # exact lookup().
        if best is not None:
            try:
                self.store._conn.execute(
                    "UPDATE verification_cache SET hit_count = hit_count + 1 "
                    "WHERE canonical_key = ?", (best.matched_key,),
                )
                self.store._conn.commit()
                best.verdict.hit_count += 1
            except Exception:
                pass

        return best

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
