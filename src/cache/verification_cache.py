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

import hashlib
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
    # Provenance + count bookkeeping. evidence_hash is NULL when the
    # entry was written without an evidence dict; source_urls likewise.
    # refresh_count + contradiction_count drive the entry's confidence
    # via confidence_from_counts.
    evidence_hash: str | None = None
    source_urls: list[str] | None = None
    last_refreshed_at: str | None = None
    refresh_count: int = 0
    contradiction_count: int = 0

    @property
    def confidence(self) -> float:
        """v0.13: confidence is derived purely from observed counts.

        Beta(1,1) Laplace-smoothed posterior estimate of P(true |
        evidence). No LLM-emitted self-rating, no per-outcome path
        prior — just refresh_count and contradiction_count."""
        from src.router.constants import confidence_from_counts
        return confidence_from_counts(
            self.refresh_count, self.contradiction_count,
        )

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
            "evidence_hash": self.evidence_hash,
            "source_urls": self.source_urls or [],
            "confidence": self.confidence,
            "last_refreshed_at": self.last_refreshed_at,
            "refresh_count": self.refresh_count,
            "contradiction_count": self.contradiction_count,
        }


@dataclass
class WriteOutcome:
    """Returned from VerificationCache.write so the caller can react
    to a verdict reversal. Action values:
      * ``inserted``                  — new entry, no prior verdict
      * ``refreshed``                 — same verdict as before; TTL bumped
      * ``contradicted_and_replaced`` — new verdict differs from prior
      * ``skipped_volatile``          — ttl_seconds == 0 (no write)
    """
    action: str
    prior_verdict: str | None


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


def _safe_int(row, name: str, default: int = 0) -> int:
    """sqlite3.Row.__getitem__ raises IndexError when the column
    isn't on the row — happens when an old test/fixture builds rows
    without the v0.7.10 columns. Tolerate it."""
    try:
        v = row[name]
    except (IndexError, KeyError):
        return default
    return int(v) if v is not None else default


def _safe_str(row, name: str) -> str | None:
    try:
        v = row[name]
    except (IndexError, KeyError):
        return None
    return v


def _row_to_cached_verdict(row, *, hit_count_bump: int = 0) -> "CachedVerdict":
    """Hydrate a CachedVerdict from a sqlite3.Row, tolerating older
    row shapes that lack the v0.7.10 provenance columns."""
    src_urls_json = _safe_str(row, "source_urls")
    try:
        source_urls = json.loads(src_urls_json) if src_urls_json else []
    except (json.JSONDecodeError, TypeError):
        source_urls = []
    return CachedVerdict(
        canonical_key=row["canonical_key"],
        pattern=row["pattern"],
        predicate=row["predicate"],
        verdict=row["verdict"],
        evidence=json.loads(row["evidence"]) if row["evidence"] else None,
        stability_class=row["stability_class"],
        cached_at=row["cached_at"],
        expires_at=row["expires_at"],
        hit_count=int(row["hit_count"]) + hit_count_bump,
        evidence_hash=_safe_str(row, "evidence_hash"),
        source_urls=source_urls,
        last_refreshed_at=_safe_str(row, "last_refreshed_at"),
        refresh_count=_safe_int(row, "refresh_count"),
        contradiction_count=_safe_int(row, "contradiction_count"),
    )


def _extract_source_urls(evidence: dict | None) -> list[str]:
    """Pull the unique URLs out of the evidence dict the retrieval
    verifier emits. Keeps order. Used to populate source_urls so we
    know later WHICH sources backed a given verdict — the seed of
    "invalidate everything that cited this URL" once we add it."""
    if not evidence:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    snippets = evidence.get("snippets") if isinstance(evidence, dict) else None
    if isinstance(snippets, list):
        for s in snippets:
            url = (s or {}).get("url") if isinstance(s, dict) else None
            if isinstance(url, str) and url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


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

        Increments hit_count on a hit. Returns None for miss /
        expired / unknown key.
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
                return None
            if expiry < datetime.now(timezone.utc):
                return None

        # Hit. Bump counter (best-effort — never crash on this).
        try:
            self.store._conn.execute(
                "UPDATE verification_cache SET hit_count = hit_count + 1 "
                "WHERE id = ?", (row["id"],),
            )
            self.store._conn.commit()
        except Exception:
            pass

        return _row_to_cached_verdict(row, hit_count_bump=1)

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
    ) -> "WriteOutcome":
        """INSERT a new entry, or UPDATE if the key already exists.

        Returns a WriteOutcome describing whether the write was an
        insert / refresh-of-same-verdict / contradiction-overwrite. A
        contradiction overwrite is the load-bearing signal — caller
        logs it as a ``cache_contradiction_replaced`` pipeline event
        and triggers the v0.7.11 semantic-neighbor cascade.

        Provenance fields populated on every write:
          * evidence_hash — SHA-256 of the evidence JSON (stable
            "same answer" identity across refreshes)
          * source_urls — JSON array of unique URLs from the snippets
          * last_refreshed_at — bumped on every successful write
          * refresh_count — +1 when refreshed (verdict unchanged)
          * contradiction_count — +1 when verdict flips
        """
        if ttl_seconds == 0:
            return WriteOutcome(action="skipped_volatile", prior_verdict=None)

        now = datetime.now(timezone.utc)
        cached_at = now.isoformat()
        last_refreshed_at = cached_at
        expires_at: str | None
        if ttl_seconds is None:
            expires_at = None
        else:
            expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()

        evidence_json = json.dumps(evidence, default=str) if evidence else None
        evidence_hash = (
            hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
            if evidence_json else None
        )
        source_urls_json = json.dumps(_extract_source_urls(evidence), default=str) \
            if evidence else None

        # Look up the existing row (if any) for contradiction detection
        # AND to decide whether this write is a refresh vs an insert
        # (for refresh_count vs contradiction_count bookkeeping).
        prior_row = self.store._conn.execute(
            "SELECT verdict, refresh_count, contradiction_count "
            "FROM verification_cache WHERE canonical_key = ?",
            (canonical_key,),
        ).fetchone()
        prior_verdict = prior_row["verdict"] if prior_row else None

        if prior_row is None:
            new_refresh = 0
            new_contradictions = 0
        elif prior_verdict == verdict:
            new_refresh = int(prior_row["refresh_count"] or 0) + 1
            new_contradictions = int(prior_row["contradiction_count"] or 0)
        else:
            new_refresh = int(prior_row["refresh_count"] or 0)
            new_contradictions = int(prior_row["contradiction_count"] or 0) + 1

        self.store._conn.execute(
            """
            INSERT INTO verification_cache (
                canonical_key, pattern, predicate, verdict, evidence,
                stability_class, cached_at, expires_at, hit_count, created_at,
                evidence_hash, source_urls,
                last_refreshed_at, refresh_count, contradiction_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_key) DO UPDATE SET
                verdict = excluded.verdict,
                evidence = excluded.evidence,
                stability_class = excluded.stability_class,
                cached_at = COALESCE(verification_cache.cached_at, excluded.cached_at),
                expires_at = excluded.expires_at,
                evidence_hash = excluded.evidence_hash,
                source_urls = excluded.source_urls,
                last_refreshed_at = excluded.last_refreshed_at,
                refresh_count = excluded.refresh_count,
                contradiction_count = excluded.contradiction_count
            """,
            (canonical_key, pattern, predicate, verdict, evidence_json,
             stability_class, cached_at, expires_at, _now_iso(),
             evidence_hash, source_urls_json,
             last_refreshed_at, new_refresh, new_contradictions),
        )
        self.store._conn.commit()

        if prior_verdict is None:
            return WriteOutcome(action="inserted", prior_verdict=None)
        if prior_verdict == verdict:
            return WriteOutcome(action="refreshed", prior_verdict=prior_verdict)
        return WriteOutcome(
            action="contradicted_and_replaced",
            prior_verdict=prior_verdict,
        )

    def prune_expired(self, grace_seconds: int = 30 * 24 * 3600) -> int:
        """Delete cache rows whose ``expires_at`` is older than
        ``now - grace_seconds``. Returns the number of rows deleted.

        Called from app startup so the table doesn't grow monotonically
        with stale entries. The grace period (default 30 days past
        expiry) keeps recently-expired entries around for analytics +
        in case they get refreshed by another retrieval that picks up
        their canonical key. Immutable entries (``expires_at IS NULL``)
        are never pruned.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)
        ).isoformat()
        cur = self.store._conn.execute(
            "DELETE FROM verification_cache "
            "WHERE expires_at IS NOT NULL AND expires_at < ?",
            (cutoff,),
        )
        self.store._conn.commit()
        return cur.rowcount or 0

    def invalidate_by_slot(self, slot_name: str, slot_value: str) -> int:
        """Delete every cache row whose canonical_key references the
        given slot=value pair (case-folded, whitespace-collapsed to
        match the canonicalization rules).

        Use case: you discover a source is wrong about an entity, or
        an entity's status materially changed (X dissolved, Y
        renamed). Returns the number of rows deleted.
        """
        if not slot_name or not slot_value:
            return 0
        normalized = " ".join(str(slot_value).split()).lower()
        # canonical_key contains the substring "{slot_name}={normalized}"
        # bracketed by '|' (start of slots block) or '&' (between
        # slots) or end-of-string. Use LIKE patterns matching both
        # bracketing forms.
        needle_a = f"|{slot_name}={normalized}"
        needle_b = f"&{slot_name}={normalized}"
        # Capture the rows we're about to delete for the invalidation
        # log (Layer 4 audit trail).
        deleted_keys = [r["canonical_key"] for r in self.store._conn.execute(
            "SELECT canonical_key FROM verification_cache "
            "WHERE canonical_key LIKE ? OR canonical_key LIKE ?",
            (f"%{needle_a}%", f"%{needle_b}%"),
        ).fetchall()]
        cur = self.store._conn.execute(
            "DELETE FROM verification_cache "
            "WHERE canonical_key LIKE ? OR canonical_key LIKE ?",
            (f"%{needle_a}%", f"%{needle_b}%"),
        )
        self.store._conn.commit()
        n = cur.rowcount or 0
        if n > 0 and deleted_keys:
            self._log_invalidation(
                reason="manual_by_slot",
                primary_key=deleted_keys[0],
                propagated_to_keys=deleted_keys[1:] if len(deleted_keys) > 1 else None,
                detail={"slot_name": slot_name, "slot_value": slot_value},
                triggered_by="user",
            )
        return n

    # ---- invalidation log --------------------------------------------

    def invalidate_one(self, canonical_key: str) -> bool:
        """Delete a single cache entry by canonical_key. Returns True
        if a row was deleted."""
        cur = self.store._conn.execute(
            "DELETE FROM verification_cache WHERE canonical_key = ?",
            (canonical_key,),
        )
        self.store._conn.commit()
        if cur.rowcount and cur.rowcount > 0:
            self._log_invalidation(
                reason="admin_one",
                primary_key=canonical_key,
                propagated_to_keys=None,
                detail={"action": "delete"},
                triggered_by="user",
            )
            return True
        return False

    def _log_invalidation(
        self, *, reason: str, primary_key: str,
        propagated_to_keys: list[str] | None, detail: dict | None,
        triggered_by: str,
    ) -> None:
        """Append a row to cache_invalidation_log. Best-effort —
        never crashes the operation it's recording."""
        try:
            self.store._conn.execute(
                "INSERT INTO cache_invalidation_log "
                "(reason, primary_key, propagated_to_keys, detail, "
                " triggered_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    reason, primary_key,
                    json.dumps(propagated_to_keys) if propagated_to_keys else None,
                    json.dumps(detail, default=str) if detail else None,
                    triggered_by, _now_iso(),
                ),
            )
            self.store._conn.commit()
        except Exception:
            pass

    def recent_invalidations(self, limit: int = 50) -> list[dict]:
        """Return the most recent invalidation log entries — for the
        Inspector cache panel's audit view."""
        rows = self.store._conn.execute(
            "SELECT * FROM cache_invalidation_log "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "reason": r["reason"],
                "primary_key": r["primary_key"],
                "propagated_to_keys": (
                    json.loads(r["propagated_to_keys"])
                    if r["propagated_to_keys"] else []
                ),
                "detail": (
                    json.loads(r["detail"]) if r["detail"] else None
                ),
                "triggered_by": r["triggered_by"],
                "created_at": r["created_at"],
            })
        return out

    def health(self) -> dict:
        """Aggregate health metrics for the Inspector cache panel.
        Cheap to compute (single GROUP BY query)."""
        row = self.store._conn.execute(
            "SELECT "
            " COUNT(*)                                                   AS total, "
            " COUNT(CASE WHEN contradiction_count > 0 THEN 1 END)        AS ever_contradicted, "
            " SUM(contradiction_count)                                   AS total_contradictions, "
            " SUM(refresh_count)                                         AS total_refreshes, "
            " SUM(hit_count)                                             AS total_hits, "
            " MIN(cached_at)                                             AS oldest_cached_at, "
            " MAX(cached_at)                                             AS newest_cached_at "
            "FROM verification_cache"
        ).fetchone()
        return {
            "total_entries": int(row["total"] or 0),
            "ever_contradicted_entries": int(row["ever_contradicted"] or 0),
            "total_contradictions": int(row["total_contradictions"] or 0),
            "total_refreshes": int(row["total_refreshes"] or 0),
            "total_hits": int(row["total_hits"] or 0),
            "oldest_cached_at": row["oldest_cached_at"],
            "newest_cached_at": row["newest_cached_at"],
        }

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
                verdict = _row_to_cached_verdict(row)
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
