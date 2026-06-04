"""v0.16.2 observability: the durable, replay-free verification store.

`VerificationStore` writes the FULL per-claim walk result (verdict + lossless
trace + resolved QIDs + directed-over-enumerate signals + per-claim budget +
provenance footprint) to SQLite at verify time, so GET /verification/{id} reads
it back losslessly — no re-walk, surviving process restart. It wraps the SAME
shared `sqlite3.Connection` the pipeline uses (single connection,
`check_same_thread=False`, WAL); every write already runs under the deploy's
`engine_lock`, so no new connection or thread is introduced.

`persist(...)` is delete-then-insert per `verification_id` inside one
`transaction()`, so a stale re-derivation (ChatWrapper.get_verification re-walk)
can re-persist idempotently with NO orphan child rows (claims / traces / premises
from a prior walk are removed first). §3.2-neutral: this is an observability
sink; it reads verdicts/traces, never produces or changes one.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ..database import transaction

_log = logging.getLogger(__name__)


def _dumps(obj: Any) -> str:
    """json.dumps with default=str so a stray non-JSON-native value in an OPEN
    metadata/trace dict (Statement.value is typed Any; edge metadata is an open
    dict) can never RAISE inside persist() and silently drop the whole record.
    Matches the codebase convention for serializing open structures."""
    return json.dumps(obj, default=str)
from ..layer5_result.abstention_templates import abstention_line
from ..layer5_result.aggregator import base_verdict_of, is_given_assertion
from ..layer5_result.trace import trace_to_human, trace_to_json_lossless


def _b(value: Any) -> Optional[int]:
    """bool/None -> 0/1/None for an INTEGER column (preserve 'unknown' as NULL)."""
    if value is None:
        return None
    return 1 if value else 0


def extract_claim_facts(walk_result: Any) -> dict:
    """Pull the denormalized per-claim columns from one WalkResult: resolved QIDs
    + directed-over-enumerate signals (from the root verify's walk_metadata stamp)
    and the per-claim budget (from budget_consumption). All optional — a claim
    grounded via Tier-U (no KB verify) leaves the KB-derived fields NULL."""
    trace = getattr(walk_result, "trace", None)
    wm = getattr(trace, "walk_metadata", {}) or {}
    bc = getattr(walk_result, "budget_consumption", None)
    return {
        "resolved_subject_qid": wm.get("resolved_subject_qid"),
        "resolved_subject_cache_row_id": wm.get("resolved_subject_cache_row_id"),
        "resolved_value_qid": wm.get("resolved_value_qid"),
        "functional_value_known": _b(wm.get("functional_value_known")),
        "value_known_entity": _b(wm.get("value_known_entity")),
        "functional_entity_predicate": _b(wm.get("functional_entity_predicate")),
        "wall_clock_ms": getattr(bc, "wall_clock_ms", None) if bc else None,
        "llm_calls": getattr(bc, "llm_calls", None) if bc else None,
    }


class VerificationStore:
    def __init__(self, conn) -> None:
        self._conn = conn

    # ----------------------------------------------------------------- write
    def persist(
        self,
        verification_id: str,
        asserting_party: str,
        vr: Any,
        *,
        source_kind: str,
        created_at: str,
        walk_results: list,
        chat_extras: Optional[dict] = None,
        extracted_claims: Optional[list] = None,
    ) -> None:
        """Persist a full VerificationResult. Idempotent (delete-then-insert per
        verification_id). `walk_results` MUST be claim-ordered, aligned with
        `vr.claim_verdicts` (both come from zip(claims, walk_results) in
        Aggregator.aggregate). `chat_extras` carries the /chat-only fields
        (including `per_claim_actions`). `extracted_claims` is the FULL extraction
        list (incl. extraction-abstained claims that were never walked), so the
        durable record reflects every claim the input produced."""
        extras = chat_extras or {}
        text_input = getattr(vr, "text_input", {}) or {}
        agg = getattr(vr, "aggregate_metadata", {}) or {}
        cvs = list(getattr(vr, "claim_verdicts", []) or [])
        wrs = list(walk_results or [])
        # Alignment is by construction (aggregate zips the same lists). If they
        # ever diverge, persist what aligns and warn rather than corrupting the
        # record or raising in the live path — silent tail-drop would be invisible.
        if len(cvs) != len(wrs):
            _log.warning(
                "verification_store: claim_verdicts (%d) / walk_results (%d) length "
                "mismatch for %s — persisting %d aligned claims",
                len(cvs), len(wrs), verification_id, min(len(cvs), len(wrs)),
            )
        n = min(len(cvs), len(wrs))

        with transaction(self._conn):
            self._conn.execute(
                """INSERT OR REPLACE INTO verification
                   (verification_id, asserting_party, created_at, source_kind,
                    user_message, draft_message, final_message, intervention_type,
                    aggregate_metadata, consistency_warnings, audit_log_entries,
                    not_assessed_claims, selection_summary, extracted_claims,
                    per_claim_actions)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    verification_id, asserting_party, created_at, source_kind,
                    text_input.get("message"), text_input.get("draft"),
                    extras.get("final_message"), extras.get("intervention_type"),
                    _dumps(agg),
                    _dumps(getattr(vr, "consistency_warnings", []) or []),
                    _dumps(getattr(vr, "audit_log_entries", []) or []),
                    _dumps(extras.get("not_assessed_claims", []) or []),
                    extras.get("selection_summary"),
                    _dumps(extracted_claims or []),
                    _dumps(extras.get("per_claim_actions", []) or []),
                ),
            )
            # Drop any prior child rows for this id (stale re-derivation safety —
            # a re-walk may yield a different premise/claim set; no orphans).
            for tbl in ("verification_claim", "verification_trace", "verification_premise"):
                self._conn.execute(
                    f"DELETE FROM {tbl} WHERE verification_id=?", (verification_id,)
                )

            for idx in range(n):
                try:
                    self._persist_claim(
                        verification_id, idx, cvs[idx], wrs[idx], asserting_party
                    )
                except Exception:
                    # One bad claim degrades to a PARTIAL record (+ log), never a
                    # TOTAL loss: the parent row and the other claims still commit
                    # within this transaction. The live turn already succeeded; the
                    # durable audit just misses one claim, with a signal.
                    _log.exception(
                        "verification_store: failed to persist claim %d of %s",
                        idx, verification_id,
                    )

    def _persist_claim(self, verification_id, idx, cv, wr, asserting_party) -> None:
        claim = cv.claim
        facts = extract_claim_facts(wr)
        self._conn.execute(
            """INSERT INTO verification_claim
               (verification_id, claim_id, claim_index, subject, predicate,
                object, polarity, source_text, asserting_party,
                claim_abstention_reason, valid_from, valid_until,
                valid_during_ref, valid_from_ref, valid_until_ref,
                verdict, base_verdict, is_given_assertion, abstention_reason,
                contradicting_value, contradicting_value_type,
                resolved_subject_qid, resolved_subject_cache_row_id,
                resolved_value_qid, wall_clock_ms, llm_calls,
                functional_value_known, value_known_entity,
                functional_entity_predicate)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                verification_id, cv.claim_id, idx,
                claim.subject, claim.predicate, claim.object, claim.polarity,
                getattr(claim, "source_text", None),
                getattr(claim, "asserting_party", asserting_party),
                getattr(claim, "abstention_reason", None),
                getattr(claim, "valid_from", None), getattr(claim, "valid_until", None),
                getattr(claim, "valid_during_ref", None),
                getattr(claim, "valid_from_ref", None),
                getattr(claim, "valid_until_ref", None),
                cv.verdict, base_verdict_of(cv.verdict),
                1 if is_given_assertion(cv.verdict) else 0,
                cv.abstention_reason,
                None if cv.contradicting_value is None else str(cv.contradicting_value),
                cv.contradicting_value_type,
                facts["resolved_subject_qid"], facts["resolved_subject_cache_row_id"],
                facts["resolved_value_qid"], facts["wall_clock_ms"],
                facts["llm_calls"], facts["functional_value_known"],
                facts["value_known_entity"], facts["functional_entity_predicate"],
            ),
        )
        trace = getattr(wr, "trace", None)
        trace_json = trace_to_json_lossless(trace, wr) if trace else None
        self._conn.execute(
            """INSERT INTO verification_trace
               (verification_id, claim_id, trace_json, trace_human)
               VALUES (?,?,?,?)""",
            (
                verification_id, cv.claim_id,
                _dumps(trace_json) if trace_json is not None else "null",
                trace_to_human(trace, claim=claim, verdict=cv.verdict) if trace else None,
            ),
        )
        # Premise footprint: distinct provenance literals (the retraction reverse-
        # index). Dedup so a literal repeated across OR-alternatives is one row;
        # literal_index keeps the PK well-defined even for transient (NULL
        # table/row_id) literals.
        if trace is not None:
            seen: set = set()
            li = 0
            for lit in trace.provenance.literals():
                key = (lit.source, lit.table, lit.row_id, lit.status, lit.assertion)
                if key in seen:
                    continue
                seen.add(key)
                self._conn.execute(
                    """INSERT INTO verification_premise
                       (verification_id, claim_id, literal_index, source,
                        source_table, source_row_id, premise_status, is_assertion)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        verification_id, cv.claim_id, li, lit.source,
                        lit.table, lit.row_id, lit.status,
                        1 if lit.assertion else 0,
                    ),
                )
                li += 1

    # ------------------------------------------------------------------ read
    def load(self, verification_id: str) -> Optional[dict]:
        """Return the FULL audit payload for a verification, or None if absent.
        Reads only SQLite — no re-walk. The caller party-scopes on the returned
        `asserting_party`."""
        core = self._conn.execute(
            "SELECT * FROM verification WHERE verification_id=?", (verification_id,)
        ).fetchone()
        if core is None:
            return None
        claim_rows = self._conn.execute(
            "SELECT * FROM verification_claim WHERE verification_id=? ORDER BY claim_index",
            (verification_id,),
        ).fetchall()
        trace_rows = {
            r["claim_id"]: r
            for r in self._conn.execute(
                "SELECT * FROM verification_trace WHERE verification_id=?",
                (verification_id,),
            ).fetchall()
        }
        premise_by_claim: dict[str, list] = {}
        for r in self._conn.execute(
            "SELECT * FROM verification_premise WHERE verification_id=? ORDER BY claim_id, literal_index",
            (verification_id,),
        ).fetchall():
            premise_by_claim.setdefault(r["claim_id"], []).append({
                "source": r["source"],
                "source_table": r["source_table"],
                "source_row_id": r["source_row_id"],
                "premise_status": r["premise_status"],
                "is_assertion": bool(r["is_assertion"]),
            })

        claims: list[dict] = []
        for cr in claim_rows:
            tr = trace_rows.get(cr["claim_id"])
            trace_json = json.loads(tr["trace_json"]) if tr and tr["trace_json"] else None
            claims.append({
                "claim_id": cr["claim_id"],
                "subject": cr["subject"],
                "predicate": cr["predicate"],
                "object": cr["object"],
                "polarity": cr["polarity"],
                "source_text": cr["source_text"],
                "claim_abstention_reason": cr["claim_abstention_reason"],
                "temporal": {
                    "valid_from": cr["valid_from"], "valid_until": cr["valid_until"],
                    "valid_during_ref": cr["valid_during_ref"],
                    "valid_from_ref": cr["valid_from_ref"],
                    "valid_until_ref": cr["valid_until_ref"],
                },
                "verdict": cr["verdict"],
                "base_verdict": cr["base_verdict"],
                "conditional": bool(cr["is_given_assertion"]),
                "abstention_reason": cr["abstention_reason"],
                "abstention_line": abstention_line(
                    cr["abstention_reason"], subject=cr["subject"],
                    predicate=cr["predicate"], object=cr["object"],
                ),
                "contradicting_value": cr["contradicting_value"],
                "contradicting_value_type": cr["contradicting_value_type"],
                "resolved_subject_qid": cr["resolved_subject_qid"],
                "resolved_subject_cache_row_id": cr["resolved_subject_cache_row_id"],
                "resolved_value_qid": cr["resolved_value_qid"],
                "signals": {
                    "functional_value_known": _ib(cr["functional_value_known"]),
                    "value_known_entity": _ib(cr["value_known_entity"]),
                    "functional_entity_predicate": _ib(cr["functional_entity_predicate"]),
                },
                "budget": {
                    "wall_clock_ms": cr["wall_clock_ms"], "llm_calls": cr["llm_calls"],
                },
                "trace_human": tr["trace_human"] if tr else None,
                "trace": trace_json,
                "provenance": trace_json.get("provenance") if trace_json else None,
                "premises": premise_by_claim.get(cr["claim_id"], []),
            })

        return {
            "verification_id": core["verification_id"],
            "asserting_party": core["asserting_party"],
            "created_at": core["created_at"],
            "source_kind": core["source_kind"],
            "text_input": {
                "message": core["user_message"], "draft": core["draft_message"],
            },
            "final_message": core["final_message"],
            "intervention_type": core["intervention_type"],
            "selection_summary": core["selection_summary"],
            "not_assessed": json.loads(core["not_assessed_claims"] or "[]"),
            "extracted_claims": json.loads(core["extracted_claims"] or "[]"),
            "per_claim_actions": json.loads(core["per_claim_actions"] or "[]"),
            "aggregate_metadata": json.loads(core["aggregate_metadata"] or "{}"),
            "consistency_warnings": json.loads(core["consistency_warnings"] or "[]"),
            "audit_log_entries": json.loads(core["audit_log_entries"] or "[]"),
            "claims": claims,
        }


def _ib(value: Any) -> Optional[bool]:
    """INTEGER column 0/1/NULL -> bool/None for the read payload."""
    return None if value is None else bool(value)
