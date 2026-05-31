from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..audit.log import log_event
from ..layer1_extraction.extractor import Claim
from ..layer1_extraction.temporal import BEFORE_PRESENT
from ..layer3_substrate.predicate_translation import PredicateTranslation, PredicateTranslationError

_NOW = lambda: datetime.now(timezone.utc).isoformat()

# Phase H Cluster 3 step 8 (2026-05-26): strip a leading definite/indefinite
# article from a slot value. "The project" and "project" name the same
# state-bearing subject; the corpus author's seed may use one and the
# extractor's output the other. Pre-step-8 the literal Stage 1 lookup
# missed (der_revision_005 ceiling). The strip happens BEFORE the
# Wikipedia normalizer, so the normalizer sees the de-articled form and
# its Wikipedia-redirect resolution still handles proper-noun titles
# ("Beatles" → "The Beatles" via redirect graph). The strip only
# materially affects common-noun subjects where the article is
# semantically vacuous.
_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


def _strip_leading_article(value: str) -> str:
    return _LEADING_ARTICLE.sub("", value).strip() or value


@dataclass
class WriteResult:
    row_id: int
    was_idempotent: bool = False
    contradiction_closed: bool = False
    closed_row_ids: list[int] = field(default_factory=list)
    # Phase H Cluster 2 step 1: cross-source contradiction signal. Set when
    # the write would have closed an externally_verified prior row via
    # §6.1 belief revision; under the KB-wins rule the prior stays open
    # and the new row is marked `contradicted_by_externally_verified`.
    # The walker reads this on the WriteResult that the promotion step
    # returns and records `contradicted` (not `contradicted_given_assertion`
    # — the contradiction is externally grounded).
    was_cross_source_contradicted: bool = False
    cross_source_conflicting_row_ids: list[int] = field(default_factory=list)


@dataclass
class LookupResult:
    found: bool
    rows: list[dict] = field(default_factory=list)
    stage: int = 0  # 1=literal, 2=entity-resolution, 3=predicate-translation
    historical_only: bool = False  # True when only historical rows were found


class TierU:
    def __init__(
        self,
        db: sqlite3.Connection,
        entity_resolver=None,  # stub in Phase 3; wired in Phase 4
        predicate_translation: Optional[PredicateTranslation] = None,
        wikipedia_normalizer=None,
        retraction_propagator=None,
    ) -> None:
        # `db` is required; audit events are written via log_event(db, ...)
        # unconditionally — the vestigial `audit_log` flag (a D8 leftover that
        # build_pipeline never set) was removed in Phase B (B3), matching the
        # A4 cleanup of consistency.py / retraction.py / contradiction_tracer.py.
        #
        # Phase H D47: `wikipedia_normalizer` (optional) lets TierU key
        # rows on the canonical Wikipedia form rather than the surface
        # form, so cross-utterance references to the same entity dedupe
        # to one row. The original surface forms are preserved in the
        # tier_u.subject_surface / object_surface columns. When None
        # (test paths that don't wire it), TierU behaves exactly as
        # before — subject/object are keyed on the literal Claim slots.
        self._db = db
        self._resolver = entity_resolver
        self._oracle = predicate_translation
        self._normalizer = wikipedia_normalizer
        # v0.16 WS3 §3E: the premise-retraction entry point. When a user
        # correction closes a prior Tier U row (write's closed_row_ids loop) or
        # retract() soft-deletes one, propagate_retraction marks any
        # *_given_assertion verdict that depended on that premise STALE for lazy
        # re-derivation. Optional — None (test paths) skips the propagation.
        self._propagator = retraction_propagator

    def _normalize_slot(self, value: str, claim: Claim, slot: str) -> str:
        """Phase H D47: return the canonical Wikipedia form for a claim
        slot. No-op when no normalizer is wired or the value is empty;
        also skipped for the asserting party itself (first-person
        canonicalization output) and synthetic event ids.

        Phase H Cluster 3 step 8 (2026-05-26): always strip a leading
        definite/indefinite article from the value before normalization.
        "The project" and "project" refer to the same state-bearing
        subject; the corpus author's seed may use one form and the
        extractor's output may use another. Without article stripping
        the literal Stage 1 lookup misses (der_revision_005 ceiling).
        The article strip happens BEFORE the Wikipedia normalizer
        consultation, so the normalizer sees the de-articled form. For
        proper-noun subjects (`The United States`, `The Beatles`) the
        normalizer's Wikipedia-redirect resolution still produces the
        canonical title — Wikipedia handles "Beatles" → "The Beatles"
        via its redirect graph. The article strip only affects
        common-noun subjects where the article is semantically vacuous.
        """
        if not value:
            return value
        if claim.asserting_party and value == claim.asserting_party:
            return value
        if value.startswith("event_"):
            return value
        stripped = _strip_leading_article(value)
        if self._normalizer is None:
            return stripped
        try:
            result = self._normalizer.normalize(
                surface_form=stripped,
                claim_subject=claim.subject,
                claim_predicate=claim.predicate,
                claim_object=claim.object,
                source_text=claim.source_text,
                slot_position=slot,
                claim_id=claim.claim_id,
            )
        except Exception:
            return stripped
        return result.normalized_form or stripped

    def write(
        self,
        claim: Claim,
        source_context: Optional[dict] = None,
        status: str = "asserted_unverified",
        bypass_normalizer: bool = False,
    ) -> WriteResult:
        """Write claim to Tier U.

        Idempotent on matching content. A prior row is *closed* (its
        `valid_until` set to now) only when the new claim genuinely contradicts
        it (D16) — one of:

          (a) same object, opposite polarity — a direct negation; closes the
              prior regardless of the predicate's cardinality;
          (b) different object, both positive polarity, and the predicate is
              functional (single_valued) — the asserting party revised a
              single-valued slot.

        A different object on a *multi-valued* predicate is a parallel
        assertion — the prior stays open (e.g. two occupations, two hobbies).
        A different object at a different polarity (the contrastive-correction
        shape "X, not Y") is likewise compatible: the prior stays open.

        Phase H Cluster 2 step 1: `status` is the new row's provenance flag
        — `asserted_unverified` (default; entered via the promotion path)
        or `externally_verified` (pre-seeded as established fact). The
        §"KB wins" cross-source rule fires when a (D16) closure target
        is `externally_verified`: the prior stays open, the new row is
        written with status `contradicted_by_externally_verified`, and
        `was_cross_source_contradicted` is set on the WriteResult so the
        caller (promotion step) can record a `contradicted` verdict for
        the claim.
        """
        if status not in (
            "asserted_unverified", "externally_verified",
            "contradicted_by_externally_verified",
        ):
            raise ValueError(f"invalid tier_u status: {status!r}")

        now = _NOW()
        source_ctx_json = json.dumps(source_context) if source_context else None

        # Phase H D47: persist the canonical form in subject/object and the
        # surface form in subject_surface/object_surface. All downstream
        # keying (idempotency, negation, object-conflict) is on the
        # canonical form, so cross-utterance references to the same entity
        # collapse to one row. When the normalizer is not wired the
        # canonical form equals the surface form and behavior is unchanged.
        #
        # Phase H Cluster 3 step 7 (2026-05-26): `bypass_normalizer=True`
        # skips the Wikipedia normalizer for callers that already know
        # the canonical form (corpus runner seed writes; load_seeds; any
        # explicit operator seeding). Pre-Cluster-3-step-7 the seed write
        # passed claim.source_text='seed', which produced subtly different
        # canonical forms than the extractor's subsequent promotion writes
        # whose source_text was the actual claim text — two rows resulted
        # for the same intended subject and the walker matched the
        # asserted_unverified promotion row instead of the externally_verified
        # seed (der_revision_004 ceiling).
        if bypass_normalizer:
            subject_canonical = claim.subject
            object_canonical = claim.object
        else:
            subject_canonical = self._normalize_slot(claim.subject, claim, "subject")
            object_canonical = self._normalize_slot(claim.object, claim, "object")

        # Idempotency: exact match on asserting_party + subject + predicate +
        # object + polarity + scope (valid_from, valid_until). Phase H Cluster
        # 3 step 7 (2026-05-26): scope is now part of the idempotency key.
        # Pre-step-7 the idempotency check ignored scope, so a new claim with
        # a different valid_from than a prior row was silently treated as a
        # no-op write — `der_revision_006` ("Asa joined Google in 2020" with
        # prior employed_by valid_from=2019) hit this path and the walker
        # returned `verified` (matching the prior) instead of detecting the
        # scope conflict. Now the new write is recognized as scope-conflicting
        # and routed through the §"KB wins" mechanism (when the prior is
        # externally_verified) or written as a new row that the walker can
        # surface as a belief revision.
        existing_rows = self._db.execute(
            """SELECT id, status, valid_from, valid_until FROM tier_u
               WHERE asserting_party=? AND subject=? AND predicate=? AND object=?
               AND polarity=? AND retracted_at IS NULL
               ORDER BY id""",
            (claim.asserting_party, subject_canonical, claim.predicate,
             object_canonical, claim.polarity),
        ).fetchall()
        # Truly idempotent: matching key AND matching scope.
        for r in existing_rows:
            if (
                r["valid_from"] == claim.valid_from
                and r["valid_until"] == claim.valid_until
            ):
                return WriteResult(row_id=r["id"], was_idempotent=True)
        # Same key but different scope → scope_conflict candidates. If any
        # is externally_verified, the §"KB wins" mechanism fires the same
        # way it does for object_conflict / direct_negation closures.
        scope_conflict_rows = list(existing_rows)

        # (a) Direct negation: a prior row with the SAME object at the opposite
        #     polarity. Closed regardless of predicate cardinality.
        negation_rows = self._db.execute(
            """SELECT id, status FROM tier_u
               WHERE asserting_party=? AND subject=? AND predicate=?
               AND object=? AND polarity=? AND retracted_at IS NULL""",
            (claim.asserting_party, subject_canonical, claim.predicate,
             object_canonical, 1 - claim.polarity),
        ).fetchall()

        # (b) Functional object revision: prior positive rows asserting a
        #     DIFFERENT object. Only fires for a positive new claim and a
        #     functional predicate; on a multi-valued predicate the prior rows
        #     stay open as parallel assertions.
        other_object_rows: list = []
        if claim.polarity == 1:
            other_object_rows = self._db.execute(
                """SELECT id, status FROM tier_u
                   WHERE asserting_party=? AND subject=? AND predicate=?
                   AND object!=? AND polarity=1 AND retracted_at IS NULL""",
                (claim.asserting_party, subject_canonical, claim.predicate,
                 object_canonical),
            ).fetchall()

        # Phase H Cluster 2 step 1: §"KB wins" check. A would-be closure
        # whose target is `externally_verified` does NOT close the prior;
        # instead, the new row's status flips to
        # `contradicted_by_externally_verified` and the caller is informed
        # via `was_cross_source_contradicted`. asserted_unverified prior
        # rows close as before (D16 / §6.1 semantics unchanged).
        closed_row_ids: list[int] = []
        cross_source_conflict_ids: list[int] = []
        parallel_assertion = False

        for r in negation_rows:
            if r["status"] == "externally_verified":
                cross_source_conflict_ids.append(r["id"])
            else:
                closed_row_ids.append(r["id"])

        if other_object_rows:
            if self._predicate_is_functional(claim.predicate):
                for r in other_object_rows:
                    if r["status"] == "externally_verified":
                        cross_source_conflict_ids.append(r["id"])
                    else:
                        closed_row_ids.append(r["id"])
            else:
                parallel_assertion = True

        # Phase H Cluster 3 step 7 (2026-05-26): scope_conflict closure.
        # A prior row with the SAME key (subject, predicate, object, polarity)
        # but a different scope (valid_from / valid_until) is a temporal
        # contradiction — the asserting party previously stated the relation
        # began/ended at one time and now states a different one. Closes the
        # prior on asserted_unverified; defers to §"KB wins" on
        # externally_verified.
        for r in scope_conflict_rows:
            if r["status"] == "externally_verified":
                cross_source_conflict_ids.append(r["id"])
            else:
                closed_row_ids.append(r["id"])

        # If §"KB wins" fires, override the requested status. The new row
        # is still written (audit trail of what the user said) but flagged
        # so subsequent lookups skip it.
        effective_status = status
        if cross_source_conflict_ids:
            effective_status = "contradicted_by_externally_verified"

        for closed_id in closed_row_ids:
            self._db.execute(
                "UPDATE tier_u SET valid_until=? WHERE id=?", (now, closed_id)
            )

        self._db.execute(
            """INSERT INTO tier_u
               (asserting_party, subject, predicate, object, polarity,
                valid_from, valid_until, valid_during_ref,
                source_text, source_context, asserted_at,
                subject_surface, object_surface, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                claim.asserting_party,
                subject_canonical,
                claim.predicate,
                object_canonical,
                claim.polarity,
                claim.valid_from,
                claim.valid_until,
                claim.valid_during_ref,
                claim.source_text,
                source_ctx_json,
                now,
                claim.subject if subject_canonical != claim.subject else None,
                claim.object if object_canonical != claim.object else None,
                effective_status,
            ),
        )
        self._db.commit()
        row_id: int = self._db.execute("SELECT last_insert_rowid()").fetchone()[0]

        # v0.16 WS3 §3E: a closed (superseded) premise is a retraction of the
        # prior belief. Mark any *_given_assertion verdict that rested on it
        # STALE for lazy re-derivation. Fires once per closed row; a None
        # propagator (test paths) no-ops.
        if self._propagator is not None:
            for closed_id in closed_row_ids:
                self._propagator.propagate_retraction("tier_u", closed_id)

        contradiction_closed = bool(closed_row_ids)
        log_event(
            self._db,
            event_type="row_created",
            event_subject=f"tier_u:{row_id}",
            event_data={
                "asserting_party": claim.asserting_party,
                "predicate": claim.predicate,
                "contradiction_closed": contradiction_closed,
                "status": effective_status,
            },
        )
        # Audit which case fired, so Phase 10.5 can tell a belief revision
        # (a closed prior) from a parallel assertion (a multi-valued addition).
        for closed_id in closed_row_ids:
            log_event(
                self._db,
                event_type="tier_u_row_closed",
                event_subject=f"tier_u:{closed_id}",
                event_data={
                    "closed_by_row_id": row_id,
                    "asserting_party": claim.asserting_party,
                    "predicate": claim.predicate,
                },
            )
        if parallel_assertion:
            log_event(
                self._db,
                event_type="tier_u_parallel_assertion",
                event_subject=f"tier_u:{row_id}",
                event_data={
                    "asserting_party": claim.asserting_party,
                    "predicate": claim.predicate,
                },
            )
        # Phase H Cluster 2 step 1: §"KB wins" audit event. Records the
        # asymmetric outcome so Phase 10.5 / debugging can see which
        # claims were rejected because an externally-verified prior held.
        if cross_source_conflict_ids:
            log_event(
                self._db,
                event_type="cross_source_contradiction",
                event_subject=f"tier_u:{row_id}",
                event_data={
                    "asserting_party": claim.asserting_party,
                    "predicate": claim.predicate,
                    "conflicting_row_ids": cross_source_conflict_ids,
                    "new_row_status": effective_status,
                },
            )

        return WriteResult(
            row_id=row_id,
            was_idempotent=False,
            contradiction_closed=contradiction_closed,
            closed_row_ids=closed_row_ids,
            was_cross_source_contradicted=bool(cross_source_conflict_ids),
            cross_source_conflicting_row_ids=cross_source_conflict_ids,
        )

    def lookup(
        self,
        claim: Claim,
        current_time: Optional[str] = None,
        exclude_row_ids: Optional[set[int]] = None,
    ) -> LookupResult:
        """Three-stage lookup against Tier U rows.

        Phase H Cluster 3 step 7 (2026-05-26): `exclude_row_ids` lets the
        caller filter out specific rows from the lookup — used by the
        walker to skip the row written by the current walk's own promotion,
        so the polarity-conflict / object-conflict belief-revision paths
        become reachable even when promote-then-walk would otherwise let
        the walker match its own freshly-written assertion at Stage 1.
        Empty / None means no filtering (pre-step-7 behavior).
        """
        if current_time is None:
            current_time = _NOW()

        # Stage 1: literal match
        result = self._stage1(claim, current_time, exclude_row_ids=exclude_row_ids)
        if result.found:
            return result

        # Stage 2: entity-resolution broadening (stub in Phase 3)
        if self._resolver is not None:
            result = self._stage2(claim, current_time)
            if result.found:
                return result

        # Stage 3: predicate-translation broadening
        if self._oracle is not None:
            result = self._stage3(claim, current_time)
            if result.found:
                return result

        # Check if there are only historical matches
        hist = self._stage1_historical(claim)
        if hist:
            return LookupResult(found=False, rows=hist, stage=1, historical_only=True)

        return LookupResult(found=False)

    def lookup_object_conflict(
        self,
        claim: Claim,
        current_time: Optional[str] = None,
    ) -> LookupResult:
        """Find currently-valid, non-retracted, *positive* Tier U rows for the
        same (asserting_party, subject, predicate) whose object differs from the
        claim's.

        For a functional (single_valued) predicate such a row contradicts a
        positive claim — the asserting party already stipulated a different
        value for the slot. This is the object-conflict half of belief revision
        (D16); the caller (the walker) consults `single_valued` and decides.
        Multi-valued predicates do not conflict on an object difference.

        Only positive (polarity=1) rows are returned: a negative Tier U row
        `¬(S P O′)` about a different object O′ does not bear on a claim about
        O. Literal match only — no entity/predicate broadening.

        Phase H D47: subject + object are normalized to canonical Wikipedia
        form (when the normalizer is wired) before keying. A prior row
        asserting "Asa lives_in Boston" and a current claim "Asa lives_in
        Massachusetts" — wait, those are different canonicals; conflict
        legitimately fires. But "Asa lives_in Boston" vs. "Asa lives_in
        Boston, Massachusetts" — same canonical "Boston" after redirect —
        correctly dedupes through this path.
        """
        if current_time is None:
            current_time = _NOW()
        subject_canonical = self._normalize_slot(claim.subject, claim, "subject")
        object_canonical = self._normalize_slot(claim.object, claim, "object")
        # Phase H Cluster 2 step 1: `contradicted_by_externally_verified`
        # rows behave like retracted ones for verdict-influencing reads —
        # they record the user said something contrary to KB, but they
        # do not ground future verdicts.
        rows = self._db.execute(
            """SELECT * FROM tier_u
               WHERE asserting_party=? AND subject=? AND predicate=?
               AND object != ? AND polarity=1 AND retracted_at IS NULL
               AND status != 'contradicted_by_externally_verified'
               AND (valid_until IS NULL OR (valid_until != ? AND valid_until > ?))""",
            (claim.asserting_party, subject_canonical, claim.predicate,
             object_canonical, BEFORE_PRESENT, current_time),
        ).fetchall()
        rows = [dict(r) for r in rows]
        return LookupResult(found=bool(rows), rows=rows, stage=1)

    def retract(self, row_id: int, reason: str) -> None:
        """Retract a Tier U row."""
        now = _NOW()
        self._db.execute(
            "UPDATE tier_u SET retracted_at=?, retraction_reason=? WHERE id=?",
            (now, reason, row_id),
        )
        self._db.commit()
        log_event(
            self._db,
            event_type="row_retracted",
            event_subject=f"tier_u:{row_id}",
            event_data={"reason": reason},
        )
        # v0.16 WS3 §3E: propagate the retraction — mark dependent
        # *_given_assertion verdicts STALE for lazy re-derivation.
        if self._propagator is not None:
            self._propagator.propagate_retraction("tier_u", row_id)

    def mark_externally_verified(
        self,
        row_id: int,
        grounding_chain: Optional[dict] = None,
        verdict_produced: str = "verified",
        verification_context: Optional[str] = None,
    ) -> bool:
        """Upgrade a Tier U row's status from `asserted_unverified` to
        `externally_verified`.

        Called by the walker when a successful KB / Python grounding for
        a claim also matches an asserted_unverified Tier U row (Q-Upgrade).
        The upgrade is idempotent: a row already at `externally_verified`
        is left unchanged and the call returns False. Rows at
        `contradicted_by_externally_verified` are NOT upgraded — that
        status was set by an authoritative KB-wins decision and cannot be
        overridden by a subsequent successful grounding (the contradiction
        flag means "the user said something the KB disagrees with"; a
        separate KB hit on the same row is incoherent and should not
        cancel the contradiction).

        Audit-event detail (for v0.16 retraction propagation per D14).
        The `tier_u_status_upgraded` event captures everything needed to
        reconstruct the upgrade decision without forensic walk-replay:

          - `row_id`             — captured in `event_subject` as
                                   `tier_u:<id>`
          - `from_status`,
            `to_status`          — explicit transition
          - `verdict_produced`   — the walker's verdict for the walk
                                   that triggered the upgrade. Default
                                   is `'verified'` (the upgrade only
                                   fires when external grounding
                                   succeeded — see Q-Upgrade). Captured
                                   explicitly so v0.16 can confirm the
                                   upgrade was contingent on a
                                   verified outcome.
          - `grounding_chain`    — caller-supplied structured dict
                                   describing WHICH external source
                                   grounded the upgrade. Expected
                                   shape per source:
                                     KB:   {"source": "kb",
                                            "entity": "<Q-id>",
                                            "kb_property": "<P-id>",
                                            "statement_value": "<…>"}
                                     Py:   {"source": "python",
                                            "code_hash": "<sha>",
                                            "inputs": {…},
                                            "output": "…"}
                                     mix:  {"source": "derivation",
                                            "chain": [<edges>]}
                                   The walker populates this in step 3.
                                   v0.15 does NOT implement
                                   reverse-upgrade propagation (a
                                   retracted KB row that triggered an
                                   upgrade does not auto-downgrade the
                                   tier_u row); the chain is captured
                                   now so v0.16 D14 can implement that
                                   propagation without archaeological
                                   reconstruction.
          - `occurred_at`        — timestamp; auto-captured by
                                   `audit_log` schema (architecture
                                   §5.2's `occurred_at` column)
          - `verification_context` — optional caller-supplied
                                     verification-context identifier;
                                     audit_log carries this in its own
                                     column for cross-event correlation
                                     (turn / batch / session id)

        Returns True when an upgrade was performed, False otherwise
        (row not at asserted_unverified, or row missing).
        """
        row = self._db.execute(
            "SELECT status FROM tier_u WHERE id=?", (row_id,)
        ).fetchone()
        if row is None or row["status"] != "asserted_unverified":
            return False
        self._db.execute(
            "UPDATE tier_u SET status='externally_verified' WHERE id=?",
            (row_id,),
        )
        self._db.commit()
        log_event(
            self._db,
            event_type="tier_u_status_upgraded",
            event_subject=f"tier_u:{row_id}",
            event_data={
                "from_status": "asserted_unverified",
                "to_status": "externally_verified",
                "verdict_produced": verdict_produced,
                "grounding_chain": grounding_chain or {},
            },
            verification_context=verification_context,
        )
        return True

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _predicate_is_functional(self, predicate: str) -> bool:
        """Whether `predicate` is functional (single_valued) per the predicate
        translation oracle.

        Treated as multi-valued — the architecture 5.2 conservative default —
        when no oracle is wired or the consult fails: a wrong 0 keeps a
        parallel row open (a false abstain at worst), whereas a wrong 1 would
        wrongly close a live row. In the assembled pipeline the predicate has
        already been routed by Layer 2, so this consult is a cache hit.
        """
        if self._oracle is None:
            return False
        try:
            return bool(self._oracle.consult(predicate).single_valued)
        except Exception:
            return False

    def _stage1(
        self,
        claim: Claim,
        current_time: str,
        exclude_row_ids: Optional[set[int]] = None,
    ) -> LookupResult:
        subject_canonical = self._normalize_slot(claim.subject, claim, "subject")
        object_canonical = self._normalize_slot(claim.object, claim, "object")
        rows = self._query_current(
            claim.asserting_party, subject_canonical, claim.predicate,
            object_canonical, claim.polarity, current_time,
            exclude_row_ids=exclude_row_ids,
        )
        if rows:
            return LookupResult(found=True, rows=rows, stage=1)
        return LookupResult(found=False)

    def _stage1_historical(self, claim: Claim) -> list[dict]:
        subject_canonical = self._normalize_slot(claim.subject, claim, "subject")
        object_canonical = self._normalize_slot(claim.object, claim, "object")
        rows = self._db.execute(
            """SELECT * FROM tier_u
               WHERE asserting_party=? AND subject=? AND predicate=? AND object=?
               AND polarity=? AND retracted_at IS NULL
               AND status != 'contradicted_by_externally_verified'
               AND (valid_until IS NOT NULL OR valid_until=?)""",
            (
                claim.asserting_party, subject_canonical, claim.predicate,
                object_canonical, claim.polarity, BEFORE_PRESENT,
            ),
        ).fetchall()
        return [dict(r) for r in rows]

    def _stage2(self, claim: Claim, current_time: str) -> LookupResult:
        """Entity-resolution broadening — stub; not implemented until Phase 4."""
        return LookupResult(found=False)

    def _stage3(self, claim: Claim, current_time: str) -> LookupResult:
        """Predicate-translation broadening via oracle neighbors."""
        try:
            neighbors = self._oracle.query_neighbors(claim.predicate)
        except PredicateTranslationError:
            return LookupResult(found=False)
        subject_canonical = self._normalize_slot(claim.subject, claim, "subject")
        object_canonical = self._normalize_slot(claim.object, claim, "object")
        for neighbor in neighbors:
            if neighbor.retracted_at is not None:
                continue
            rows = self._query_current(
                claim.asserting_party, subject_canonical, neighbor.aedos_predicate,
                object_canonical, claim.polarity, current_time,
            )
            if rows:
                return LookupResult(found=True, rows=rows, stage=3)
        return LookupResult(found=False)

    def _query_current(
        self,
        asserting_party: str,
        subject: str,
        predicate: str,
        object_val: str,
        polarity: int,
        current_time: str,
        exclude_row_ids: Optional[set[int]] = None,
    ) -> list[dict]:
        """Return non-retracted, currently-valid rows matching all given fields.

        Phase H Cluster 2 step 1: `contradicted_by_externally_verified`
        rows are excluded — they record what the user said but cannot
        ground a verdict (the KB-wins decision is preserved).

        Phase H Cluster 3 step 7 (2026-05-26): `exclude_row_ids` filters
        specific row ids out — used by the walker to skip the current
        walk's own promoted row.
        """
        sql = (
            "SELECT * FROM tier_u "
            "WHERE asserting_party=? AND subject=? AND predicate=? AND object=? "
            "AND polarity=? AND retracted_at IS NULL "
            "AND status != 'contradicted_by_externally_verified' "
            "AND (valid_until IS NULL OR (valid_until != ? AND valid_until > ?))"
        )
        params: list = [asserting_party, subject, predicate, object_val, polarity,
                        BEFORE_PRESENT, current_time]
        if exclude_row_ids:
            placeholders = ",".join("?" * len(exclude_row_ids))
            sql += f" AND id NOT IN ({placeholders})"
            params.extend(exclude_row_ids)
        rows = self._db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
