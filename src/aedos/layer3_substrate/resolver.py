from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from ..layer4_sources.kb_protocol import KBEntityID, LocalContext, ResolutionCandidate

_NOW = lambda: datetime.now(timezone.utc).isoformat()
_SELECT_THRESHOLD = 0.6
_AMBIGUITY_GAP = 0.15  # if top two scores within this gap, use LLM selection


def _cache_key(reference: str, predicate: str, slot_position: str, asserting_party: Optional[str]) -> str:
    parts = f"{reference}\x00{predicate}\x00{slot_position}\x00{asserting_party or ''}"
    return hashlib.sha256(parts.encode()).hexdigest()


class EntityResolverError(Exception):
    pass


class EntityResolver:
    def __init__(
        self,
        kb_protocol,
        db: sqlite3.Connection,
        llm_client=None,
        wikipedia_normalizer=None,
    ) -> None:
        """`wikipedia_normalizer` is the Phase H D47 normalizer. When
        provided, `resolve` invokes it on the reference before the cache
        lookup / KB call — bare ambiguous references are normalized to
        canonical Wikipedia article titles (when Wikipedia's redirect
        system or Stage 2's LLM selection determines a canonical form).

        When None (e.g. test paths that don't wire it), `resolve` skips
        normalization and behaves exactly as before. The deployed
        pipeline (`build_pipeline`) always wires it.
        """
        self._kb = kb_protocol
        self._db = db
        self._llm = llm_client
        self._normalizer = wikipedia_normalizer

    def resolve(self, reference: str, local_context: LocalContext) -> list[ResolutionCandidate]:
        """Cache-first resolution. On miss, delegates to KB and writes cache.

        Phase H D47: before the cache lookup, normalize the reference via
        the Wikipedia normalizer when one is wired. The cache and KB
        lookups then key on the normalized form, so cross-utterance
        references that resolve to the same canonical entity dedupe
        through one cache row.

        Phase H D53: the normalizer's Stage C may produce a `selected_qid`
        directly. When present, the resolver short-circuits with that
        Q-id (single ResolutionCandidate at score 1.0) and skips the
        KB.resolve_entity call. When absent, the existing label-based
        fallback runs (KB.resolve_entity wraps wbsearchentities + D33
        + SPARQL fallback as today).

        Skipped when:
          - no normalizer is wired (None);
          - the normalizer is disabled via Config;
          - the reference is the asserting party itself (first-person
            canonicalization output — not a Wikipedia article title);
          - the reference looks like a synthetic event id (starts with
            "event_").
        """
        normalized_reference, selected_qid = self._normalize_if_applicable(
            reference, local_context
        )

        # Phase H D53 step 2: short-circuit when the normalizer's Stage C
        # produced a Q-id. The KB.resolve_entity path would re-run
        # wbsearchentities and the type filter — wasteful given the
        # normalizer has already done that work.
        if selected_qid:
            key = _cache_key(
                normalized_reference, local_context.predicate,
                local_context.slot_position, local_context.asserting_party,
            )
            self._db.execute(
                """INSERT OR IGNORE INTO entity_resolution_cache
                   (reference, local_context_signature, resolved_kb_namespace,
                    resolved_kb_identifier, provenance, created_at, used_count)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (
                    normalized_reference, key,
                    "wikidata",
                    selected_qid,
                    json.dumps({"source": "wikipedia_normalizer_stage_c"}),
                    _NOW(),
                ),
            )
            self._db.commit()
            return [ResolutionCandidate(
                kb_identifier=selected_qid,
                provenance={"source": "wikipedia_normalizer_stage_c", "label": normalized_reference},
                score=1.0,
            )]

        key = _cache_key(
            normalized_reference, local_context.predicate,
            local_context.slot_position, local_context.asserting_party,
        )
        cached = self._db.execute(
            """SELECT id, resolved_kb_namespace, resolved_kb_identifier, provenance, used_count
               FROM entity_resolution_cache
               WHERE local_context_signature=? AND reference=? AND retracted_at IS NULL
               ORDER BY id LIMIT 1""",
            (key, normalized_reference),
        ).fetchone()

        if cached is not None:
            self._db.execute(
                "UPDATE entity_resolution_cache SET used_count=used_count+1, last_used_at=? WHERE id=?",
                (_NOW(), cached["id"]),
            )
            self._db.commit()
            # Phase H Cluster 1 step 1: negative cache. Empty
            # resolved_kb_identifier means a prior call resolved this
            # (normalized_reference, context) to no KB candidates; surface
            # the same answer without re-querying KB. The schema's
            # `resolved_kb_identifier TEXT NOT NULL` constraint is
            # satisfied by storing `''`; a fuller alternative
            # (`ALTER TABLE ... DROP NOT NULL`) would require SQLite's
            # multi-step table-recreation dance.
            if not cached["resolved_kb_identifier"]:
                return []
            prov = json.loads(cached["provenance"]) if cached["provenance"] else {}
            return [ResolutionCandidate(
                kb_identifier=cached["resolved_kb_identifier"],
                provenance={**prov, "cache_hit": True},
                score=1.0,
            )]

        candidates = self._kb.resolve_entity(normalized_reference, local_context)

        if candidates:
            best = candidates[0]
            self._db.execute(
                """INSERT OR IGNORE INTO entity_resolution_cache
                   (reference, local_context_signature, resolved_kb_namespace,
                    resolved_kb_identifier, provenance, created_at, used_count)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (
                    normalized_reference, key,
                    "wikidata",
                    best.kb_identifier,
                    json.dumps(best.provenance),
                    _NOW(),
                ),
            )
            self._db.commit()
        else:
            # Negative cache write. The Cluster 1 diagnostic surfaced
            # that abstain-then-no-match cases re-queried KB on every
            # walker iteration (the cache only wrote on candidates>0),
            # so a single walk fired one KB call per surface-form
            # occurrence — eight per case for the "President" surface
            # forms. The empty-string sentinel marks the row as
            # "asked KB, got nothing"; subsequent calls short-circuit
            # at the cache lookup above.
            self._db.execute(
                """INSERT OR IGNORE INTO entity_resolution_cache
                   (reference, local_context_signature, resolved_kb_namespace,
                    resolved_kb_identifier, provenance, created_at, used_count)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (
                    normalized_reference, key,
                    "",
                    "",
                    json.dumps({"negative_cache": True}),
                    _NOW(),
                ),
            )
            self._db.commit()

        return candidates

    # ------------------------------------------------------------------
    # D47 normalization helper
    # ------------------------------------------------------------------

    def _normalize_if_applicable(
        self, reference: str, local_context: LocalContext
    ) -> tuple[str, Optional[KBEntityID]]:
        """Run the Wikipedia normalizer and return (normalized_form,
        selected_qid). `normalized_form` is the surface form when
        normalization is skipped or fails. `selected_qid` is a Wikidata
        Q-id when the normalizer's Stage C produced one (D53), or None
        when it abstained, errored, or wasn't asked.
        """
        if self._normalizer is None or not reference:
            return reference, None

        # Skip first-person canonicalization output (the asserting party
        # is not a Wikipedia article title; normalizing it would silently
        # invent a wrong canonical).
        if local_context.asserting_party and reference == local_context.asserting_party:
            return reference, None

        # Skip synthetic event ids produced by the extractor's event
        # decomposition path.
        if reference.startswith("event_"):
            return reference, None

        try:
            result = self._normalizer.normalize(
                surface_form=reference,
                claim_subject=local_context.claim_subject,
                claim_predicate=local_context.claim_predicate or local_context.predicate,
                claim_object=local_context.claim_object,
                source_text=local_context.source_text,
                slot_position=local_context.slot_position,
                claim_id=local_context.claim_id,
                expected_entity_types=list(local_context.expected_entity_types or []),
            )
        except Exception:
            # Fail-open: a normalizer outage must not abstain on every
            # resolution. The normalizer's own audit-log path records the
            # failure; the resolver keeps moving with the surface form.
            return reference, None

        # Phase 10.5 Step 6 Batch 7+: leading-"the" retry. The extractor
        # often emits references like "the Nobel Prize in Physics", "the
        # Institute for Advanced Study" — Wikipedia's redirect system
        # doesn't always redirect the article-prefixed form to the
        # canonical title, so Stage A returns not_found and Stage B's
        # wbsearchentities ranks the canonical Q-id too low for Stage C.
        # If the initial normalization produced no selected_qid, retry
        # with the leading lowercase "the " stripped. Capital-T "The "
        # is preserved because it indicates the article is part of the
        # canonical name (e.g. "The Beatles", "The Hague").
        if (
            result.selected_qid is None
            and len(reference) > 4
            and reference[:4] == "the "
        ):
            stripped = reference[4:].strip()
            if stripped:
                try:
                    retry = self._normalizer.normalize(
                        surface_form=stripped,
                        claim_subject=local_context.claim_subject,
                        claim_predicate=local_context.claim_predicate or local_context.predicate,
                        claim_object=local_context.claim_object,
                        source_text=local_context.source_text,
                        slot_position=local_context.slot_position,
                        claim_id=local_context.claim_id,
                        expected_entity_types=list(local_context.expected_entity_types or []),
                    )
                except Exception:
                    retry = None
                if retry is not None and retry.selected_qid is not None:
                    return (retry.normalized_form or stripped, retry.selected_qid)

        return (result.normalized_form or reference, result.selected_qid)

    def select(
        self, candidates: list[ResolutionCandidate], local_context: LocalContext
    ) -> Optional[KBEntityID]:
        """Return best candidate. Returns None if no usable candidate."""
        if not candidates:
            return None
        sorted_c = sorted(candidates, key=lambda c: c.score, reverse=True)
        top = sorted_c[0]
        if top.score < _SELECT_THRESHOLD:
            return None
        # If ambiguous and LLM client available, delegate selection
        if (
            len(sorted_c) > 1
            and self._llm is not None
            and (top.score - sorted_c[1].score) < _AMBIGUITY_GAP
        ):
            # LLM selection: in tests this is exercised via mock LLM returning the first candidate's id
            selected_id = self._llm.chat(
                system="Select the best Wikidata entity for this reference.",
                messages=[{"role": "user", "content": json.dumps({
                    "reference": local_context.predicate,
                    "candidates": [{"id": c.kb_identifier, "prov": c.provenance} for c in sorted_c[:3]],
                })}],
                purpose="substrate:entity_resolution",
            )
            if selected_id and selected_id.strip():
                return selected_id.strip()
        return top.kb_identifier

    def retract_cache_entry(self, cache_id: int, reason: str) -> None:
        """Soft-delete a cache entry and log the retraction."""
        now = _NOW()
        self._db.execute(
            "UPDATE entity_resolution_cache SET retracted_at=?, retraction_reason=? WHERE id=?",
            (now, reason, cache_id),
        )
        self._db.commit()
