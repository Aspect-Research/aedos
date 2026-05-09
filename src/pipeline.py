"""Pipeline orchestrator for a full v0.14 turn.

Runs a single user → assistant turn through every layer:

    user message
        ↓
    Layer 1 — extract user claims
        ↓
    Layer 2 — route + validator; on self-attribute → Tier U store; on
              user-stated world claim → walk + dual-write (user
              assertion row + verifier output row in Tier W)
        ↓
    build chat context (currently-visible Tier U facts + dispute
    block when the user just made a checkable world claim that the
    walker disagreed with)
        ↓
    LLM chat → assistant draft
        ↓
    Layer 1 — extract assistant claims
        ↓
    Layer 4 — for each claim: route, walk (U → W → derivation → fresh)
        ↓
    Layer 5 — decision_confidence + intervention per claim
        ↓
    Layer 5 — corrector rewrite
        ↓
    return ``TurnTrace``

Every stage emits a ``pipeline_events`` row; the trace UI rebuilds the
per-turn flow from that table.
"""

from __future__ import annotations

import concurrent.futures
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from src.cache import (
    classify_scope,
    classify_stability,
)
from src.fact_store import (
    DEFAULT_SESSION_ID,
    DEFAULT_USER_ID,
    Fact,
    FactStore,
)
from src.layer1_extraction.extractor import ClaimExtractor, ExtractionResult
from src.layer1_extraction.pattern_registry import (
    PatternRegistry,
    load_default_registry,
)
from src.layer1_extraction.verifiability_triage import (
    TriageDecision,
    TriageResult,
    triage_claim,
)
from src.layer2_routing.constants import (
    KEY_SLOTS_BY_PATTERN,
    is_self_attribute,
)
from src.layer2_routing.router import Router
from src.layer2_routing.types import Decision as Layer2Decision
from src.layer2_routing.types import RoutingOutcome
from src.layer3_substrate.entity_equivalence import EntityEquivalence
from src.layer3_substrate.entity_taxonomy import EntityTaxonomy
from src.layer3_substrate.predicate_distribution import PredicateDistribution
from src.layer3_substrate.predicate_equivalence import PredicateEquivalence
from src.layer4_lookup import fresh as _fresh
from src.layer4_lookup import tier_u as _tier_u
from src.layer4_lookup import tier_w as _tier_w
from src.layer4_lookup.types import LookupOutcome, WalkerDecision
from src.layer4_lookup.walker import walk_claim
from src.layer5_decision.confidence import (
    compute_decision_confidence,
    get_threshold,
)
from src.layer5_decision.corrector import Corrector
from src.layer5_decision.intervention import plan_intervention
from src.layer5_decision.types import (
    DecisionConfidence,
    Intervention,
    InterventionType,
)
from src.llm_client import ChatMessage, LLMClient


# ============================================================================
# Chat system prompt
# ============================================================================


CHAT_SYSTEM_TEMPLATE = """You are a helpful assistant in a single-conversation demo.

{facts_block}

Respond naturally and directly. When answering questions whose answer appears in the user-self section above, state it plainly. Do not speculate about user preferences that aren't listed — say you don't know instead.

# You have downstream verification — refusing to answer is the wrong move

Every factual claim you make is checked by an external verification pipeline before the user sees your reply. The pipeline runs arbitrary Python in a sandbox (full standard library — `zoneinfo`/IANA timezone database, `datetime`, `math`, `statistics`, `re`, `hashlib`, `decimal`, etc.) and can also search Wikipedia for world facts.

**Refusing to answer is the wrong move.** A concrete answer (even a wrong one) gets verified; the user always sees the right value. A disclaimer leaves the user with nothing — and the verifier with nothing to check. Prefer wrong-but-specific over correct-but-vague. The verifier will quietly surface a correction if you're off; you don't need to hedge.

In particular, do NOT reflexively disclaim:
  * Real-time / clock / timezone questions ("what time is it in X?")
  * String / counting / pattern questions ("how many words contain 'e' in this sentence?")
  * Arithmetic / statistics / numeric reasoning ("what's the standard deviation of …?")
  * Stable world facts the pipeline can look up (populations, historical dates, who-is-the-X-of-Y)
  * Date / calendar math ("what day of the week was …?")

Reserve disclaimers for genuinely unverifiable territory: predictions about future events, claims about other people's internal states, aesthetic judgments, policy questions, anything the verifier can't ground in code or a stable source. For everything else, commit to a value.

# Commit, don't disclaim — worked examples

WRONG (do not do this):
  User: What time is it in Cairo?
  Assistant: I don't have access to the current exact time. However,
  Cairo is in the Egypt Standard Time zone (UTC+2, or UTC+3 during
  daylight saving time). If you let me know your current time, I can
  help calculate Cairo's time.

RIGHT (do this):
  User: What time is it in Cairo?
  Assistant: It's around 9:00 PM in Cairo right now. (Cairo is UTC+2,
  or UTC+3 during DST.)

WRONG:
  User: How many words contain 'e' in "the quick brown fox"?
  Assistant: I'd need to look at that sentence carefully. Let me count
  the words and check each one for the letter 'e'...

RIGHT:
  User: How many words contain 'e' in "the quick brown fox"?
  Assistant: One word — "the" — contains 'e'.

WRONG:
  User: What's the population of Lagos?
  Assistant: I don't have current population data, but Lagos is one
  of the largest cities in Africa.

RIGHT:
  User: What's the population of Lagos?
  Assistant: Lagos has roughly 15 million people in the metro area.

**Heuristic:** if your draft contains "I don't have access to", "I can't directly", "without knowing X", "depending on", or "if you tell me" applied to a verifiable factual question — rewrite it. Pick a value, commit, let the verifier handle the rest. The only place those phrases belong is in genuinely-unverifiable territory (future predictions, others' internal states, aesthetic judgments)."""

CHAT_USER_DISPUTE_TEMPLATE = """\

IMPORTANT — the user just made claim(s) about the world that don't match what external verification found. Surface this gently in your reply, do NOT silently accept the user's version. Acknowledge what the user said, then note what external sources show, and let the user decide. Sample phrasing: "Just to flag — external sources suggest X, so closer to Y. Want me to use that instead?" Avoid blunt corrections like "you're wrong"; the user's agency comes first.

Disputed claims:
{disputes_block}
"""


def _format_facts_block(user_facts: list[Fact]) -> str:
    """Render user-asserted facts as a system-prompt section.

    Empty list → empty section (the chat model gets the standard prompt
    without a user-self block). Non-empty → ``# What you know about
    the user`` heading + bulleted lines.
    """
    if not user_facts:
        return ""
    lines = ["# What you know about the user"]
    for f in user_facts:
        lines.append(f"  - {_fact_inline(f)}")
    return "\n".join(lines) + "\n"


def _fact_inline(f: Fact) -> str:
    """One-line human-readable rendering of a stored fact."""
    pol = "" if f.polarity == 1 else "NOT "
    slots_inline = ", ".join(f"{k}={v!r}" for k, v in (f.slots or {}).items())
    return f"{pol}{f.pattern}.{f.predicate}({slots_inline})"


def _format_disputes_block(disputes: list[dict]) -> str:
    if not disputes:
        return ""
    lines = []
    for d in disputes:
        claim = d.get("claim") or {}
        verdict = d.get("verdict") or "—"
        lines.append(
            f"  - user said: {_claim_inline(claim)}\n"
            f"    external verdict: {verdict}"
        )
    return "\n".join(lines)


def _claim_inline(claim: dict) -> str:
    pat = claim.get("pattern", "")
    pred = claim.get("predicate", "")
    pol = claim.get("polarity", 1)
    pol_str = "" if pol == 1 else "NOT "
    slots = claim.get("slots", {}) or {}
    slots_inline = ", ".join(f"{k}={v!r}" for k, v in slots.items())
    return f"{pol_str}{pat}.{pred}({slots_inline})"


# ============================================================================
# TurnTrace — return shape consumed by the trace UI
# ============================================================================


@dataclass
class UserClaimDecision:
    """How the pipeline handled one user-extracted claim."""

    claim: dict
    layer2: dict                      # Layer2Decision.to_dict()
    storage_outcome: Optional[str]    # tier_u storage outcome value, or None
    walker: Optional[dict] = None     # WalkerDecision.to_dict() for world claims
    is_self_attribute: bool = False
    is_anomaly: bool = False
    user_world_dispute: bool = False  # walker contradicted the user's claim

    def to_dict(self) -> dict:
        return {
            "claim": dict(self.claim),
            "layer2": self.layer2,
            "storage_outcome": self.storage_outcome,
            "walker": self.walker,
            "is_self_attribute": self.is_self_attribute,
            "is_anomaly": self.is_anomaly,
            "user_world_dispute": self.user_world_dispute,
        }


@dataclass
class VerificationDecision:
    """The full Layer 4/5 decision for one assistant claim."""

    claim: dict
    layer2: dict
    walker: dict
    confidence: dict
    intervention: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TurnTrace:
    user_turn_id: int
    assistant_turn_id: int
    final_content: str
    original_content: Optional[str]   # non-None iff a correction was applied
    user_extraction: dict
    user_decisions: list[dict]
    assistant_extraction: dict
    verification_decisions: list[dict]
    interventions: list[dict]
    routing_anomalies: list[dict]

    def to_dict(self) -> dict:
        return {
            "user_turn_id": self.user_turn_id,
            "assistant_turn_id": self.assistant_turn_id,
            "final_content": self.final_content,
            "original_content": self.original_content,
            "user_extraction": self.user_extraction,
            "user_decisions": list(self.user_decisions),
            "assistant_extraction": self.assistant_extraction,
            "verification_decisions": list(self.verification_decisions),
            "interventions": list(self.interventions),
            "routing_anomalies": list(self.routing_anomalies),
        }


# ============================================================================
# Pipeline
# ============================================================================


class Pipeline:
    """v0.14 turn orchestrator.

    Construct with ``build_pipeline(db_path)`` for the wired-up
    production instance. Tests construct ``Pipeline(...)`` directly
    with a mock LLM and a tmp_path-backed FactStore.
    """

    def __init__(
        self,
        store: FactStore,
        registry: PatternRegistry,
        llm: LLMClient,
        extractor: ClaimExtractor,
        router: Router,
        corrector: Corrector,
        predicate_oracle: PredicateEquivalence,
        entity_oracle: EntityEquivalence,
        taxonomy_oracle: EntityTaxonomy,
        distribution_oracle: PredicateDistribution,
        user_id: str = DEFAULT_USER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ):
        self.store = store
        self.registry = registry
        self.llm = llm
        self.extractor = extractor
        self.router = router
        self.corrector = corrector
        self.predicate_oracle = predicate_oracle
        self.entity_oracle = entity_oracle
        self.taxonomy_oracle = taxonomy_oracle
        self.distribution_oracle = distribution_oracle
        self.user_id = user_id
        self.session_id = session_id

    # ---- public entry point --------------------------------------------

    def run_turn(self, user_message: str) -> TurnTrace:
        """Run one full user → assistant turn end-to-end."""
        if not user_message or not user_message.strip():
            raise ValueError("user_message must be a non-empty string")

        # Stage 1: insert user turn FIRST so all user-side events have
        # a turn_id to attach to.
        user_turn_id = self.store.insert_turn(
            role="user", content=user_message, user_id=self.user_id,
        )

        # Stage 2: user extraction
        user_extraction = self.extractor.extract(user_message, role="user")
        self._emit(user_turn_id, "user_extraction", user_extraction.to_dict())

        # Stage 3: route + store user claims
        user_decisions: list[UserClaimDecision] = []
        routing_anomalies: list[dict] = []
        disputes: list[dict] = []
        for fact in user_extraction.valid_facts:
            ud = self._handle_user_claim(fact, user_turn_id, user_message)
            user_decisions.append(ud)
            if ud.is_anomaly:
                routing_anomalies.append({
                    "claim": dict(ud.claim),
                    "validation": ud.layer2.get("validation"),
                })
            if ud.user_world_dispute:
                disputes.append({
                    "claim": dict(ud.claim),
                    "verdict": (ud.walker or {}).get("verification_status"),
                })

        # Stage 3b: aggregate user_storage event so the UI's User Message
        # step transitions out of "verifying…" — fires even when zero
        # claims were extracted (common for chitchat turns).
        self._emit(user_turn_id, "user_storage", {
            "decisions": [ud.to_dict() for ud in user_decisions],
            "n_claims": len(user_decisions),
            "n_anomalies": len(routing_anomalies),
            "n_disputes": len(disputes),
        })

        # Stage 4: build chat context
        visible_facts = self._tier_u_visible_facts()
        system_prompt = self._build_chat_system(visible_facts, disputes)
        history = self._build_history(prior_to_turn_id=user_turn_id)

        # Stage 5: insert assistant turn (placeholder content; updated post-draft)
        asst_turn_id = self.store.insert_turn(
            role="assistant", content="", user_id=self.user_id,
        )

        # Stage 6: chat draft (streaming). Each text delta broadcasts a
        # chat_draft_token event so the chat bubble fills in live.
        # Tokens use broadcast_event (NOT insert_pipeline_event) so the
        # events table doesn't accrue hundreds of rows per turn.
        chat_messages = history + [
            ChatMessage(role="user", content=user_message),
        ]
        cumulative: list[str] = []

        def _on_token(delta: str) -> None:
            cumulative.append(delta)
            try:
                self.store.broadcast_event(
                    asst_turn_id, "chat_draft_token",
                    {"text": "".join(cumulative)},
                )
            except Exception:
                pass

        try:
            draft = self.llm.chat_stream(
                system_prompt, chat_messages, on_token=_on_token,
            )
        except Exception as exc:
            self._emit(asst_turn_id, "chat_model_call", {
                "error": f"{type(exc).__name__}: {exc}",
                "system_prompt_length": len(system_prompt),
                "history_messages": len(history),
            })
            raise
        # chat_model_call payload carries provider/model so the UI's Chat
        # Model card can show "anthropic:claude-haiku-4-5" instead of "?:?".
        chat_target = getattr(self.llm, "model", None) or "?"
        provider = "openai" if str(chat_target).startswith("gpt") else "anthropic"
        self._emit(asst_turn_id, "chat_model_call", {
            "provider": provider,
            "model": chat_target,
            "system_prompt_length": len(system_prompt),
            "history_messages": len(history),
            "draft_length": len(draft),
            "response_chars": len(draft),
        })
        # assistant_draft is the durable record of the final draft text;
        # the UI uses it to set the chat bubble content in case
        # chat_draft_token frames were missed (replay / cold load).
        self._emit(asst_turn_id, "assistant_draft", {"content": draft})
        self.store.update_turn_content(
            asst_turn_id, content=draft, original_content=None,
        )

        # Stage 7: assistant extraction
        asst_extraction = self.extractor.extract(
            draft, role="assistant", context=user_message,
        )
        self._emit(asst_turn_id, "assistant_extraction", asst_extraction.to_dict())

        # Stage 8: verify per claim — IN PARALLEL. Each claim hits the
        # walker → fresh verifier independently; running them
        # sequentially turned a 12-claim turn into 12× the latency. The
        # store is thread-safe (RLock around _LockedConnection); LLM
        # calls are network-bound and parallelize cleanly.
        verification_decisions, interventions = self._verify_claims_parallel(
            asst_extraction.valid_facts, asst_turn_id,
        )

        # Stage 8b: aggregate verification event so the Claims step in
        # the UI flips from "verifying…" to "done". Per-claim
        # claim_decision events still fire live during parallel
        # verification; this aggregate is the terminal signal.
        self._emit(asst_turn_id, "verification", {
            "decisions": [vd.to_dict() for vd in verification_decisions],
            "n_claims": len(verification_decisions),
        })

        # Stage 9: correct (single LLM call, batched over all interventions).
        # The corrector receives every intervention; pass_through and noop
        # types are filtered inside the corrector but the trace UI counts
        # them too so the operator sees "12/24 claims required action".
        final_text = self.corrector.apply(
            draft, interventions, user_message=user_message,
        )
        rewrote = final_text != draft
        if rewrote:
            self.store.update_turn_content(
                asst_turn_id, content=final_text, original_content=draft,
            )
        # ALWAYS emit the correction event so the UI's Correction card
        # transitions to "done" even on no-rewrite turns. Payload uses
        # original/corrected (matches the UI's renderCorrectionInline)
        # and carries the full Intervention.to_dict() list so the inline
        # renderer can show each one's claim + reason.
        self._emit(asst_turn_id, "correction", {
            "original": draft,
            "corrected": final_text,
            "rewrote": rewrote,
            "interventions": [iv.to_dict() for iv in interventions],
        })

        # Stage 10: final marker. Carries final_content so the UI's
        # Final Response card can render a preview + the rewrote flag
        # without round-tripping to the chat bubble.
        self._emit(asst_turn_id, "final", {
            "final_content": final_text,
            "final_length": len(final_text),
            "rewrote": rewrote,
            "n_user_claims": len(user_decisions),
            "n_assistant_claims": len(verification_decisions),
            "n_routing_anomalies": len(routing_anomalies),
        })

        return TurnTrace(
            user_turn_id=user_turn_id,
            assistant_turn_id=asst_turn_id,
            final_content=final_text,
            original_content=draft if final_text != draft else None,
            user_extraction=user_extraction.to_dict(),
            user_decisions=[ud.to_dict() for ud in user_decisions],
            assistant_extraction=asst_extraction.to_dict(),
            verification_decisions=[vd.to_dict() for vd in verification_decisions],
            interventions=[iv.to_dict() for iv in interventions],
            routing_anomalies=routing_anomalies,
        )

    # ---- user-side claim handling --------------------------------------

    def _handle_user_claim(
        self, fact: dict, turn_id: int, raw_text: str,
    ) -> UserClaimDecision:
        """Route + store one user-extracted claim."""
        layer2 = self.router.classify(fact, source_turn_id=turn_id)

        if layer2.outcome is RoutingOutcome.ROUTING_ANOMALY:
            return UserClaimDecision(
                claim=fact, layer2=layer2.to_dict(),
                storage_outcome=None, walker=None,
                is_self_attribute=False, is_anomaly=True,
                user_world_dispute=False,
            )

        if is_self_attribute(fact):
            # Tier U write: user microtheory.
            key_slots = KEY_SLOTS_BY_PATTERN.get(fact.get("pattern", ""), [])
            result = _tier_u.store_user_fact(
                fact, self.store,
                current_session=self.session_id,
                key_slot_names=key_slots,
                user_id=self.user_id,
                source_turn_id=turn_id,
                raw_text=raw_text,
            )
            return UserClaimDecision(
                claim=fact, layer2=layer2.to_dict(),
                storage_outcome=result.outcome.value, walker=None,
                is_self_attribute=True, is_anomaly=False,
                user_world_dispute=False,
            )

        # User-stated world claim: walk to verify, dual-write the
        # audit trail (the user's assertion row + the verifier output).
        walker_decision = walk_claim(
            fact, layer2, self.store,
            registry=self.registry,
            predicate_oracle=self.predicate_oracle,
            entity_oracle=self.entity_oracle,
            taxonomy_oracle=self.taxonomy_oracle,
            distribution_oracle=self.distribution_oracle,
            llm=self.llm,
            source_turn_id=turn_id,
            user_id=self.user_id,
            current_session=self.session_id,
            fresh_dispatch=_fresh.dispatch,
        )

        # Persist the user's assertion as an audit-trail row regardless
        # of verifier outcome. asserted_by="user" + verification_status
        # carries the verdict the walker reached.
        self._dual_write_user_world_claim(fact, walker_decision, turn_id)

        # A user-world dispute fires when the walker contradicted the
        # user's claim (Tier W or fresh verifier said opposite). The
        # chat system prompt picks this up as the IMPORTANT — user
        # dispute block.
        is_dispute = (
            walker_decision.outcome is LookupOutcome.CONTRADICTION
            or walker_decision.verification_status == "contradicted"
        )

        return UserClaimDecision(
            claim=fact, layer2=layer2.to_dict(),
            storage_outcome=None,
            walker=walker_decision.to_dict(),
            is_self_attribute=False, is_anomaly=False,
            user_world_dispute=is_dispute,
        )

    def _dual_write_user_world_claim(
        self, fact: dict, walker: WalkerDecision, turn_id: int,
    ) -> None:
        """Persist the user's world-claim assertion as a fact row.

        The verifier output (when verified or contradicted) is already
        written to Tier W by the fresh dispatcher; this method handles
        the user-side audit row only. asserted_by="user",
        verification_status reflects the walker's verdict so the trace
        UI can render "user said X — verifier confirmed/contradicted".
        """
        from src.fact_store import VERIFICATION_STATUSES

        status = walker.verification_status or "unverifiable_pending_implementation"
        if status not in VERIFICATION_STATUSES:
            status = "unverifiable_pending_implementation"

        slots = dict(fact.get("slots") or {})
        polarity = int(fact.get("polarity", 1))
        new_fact = Fact(
            pattern=fact["pattern"],
            predicate=fact["predicate"],
            slots=slots,
            polarity=polarity,
            asserted_by="user",
            verification_status=status,
            source_turn_id=turn_id,
            source_text=fact.get("source_text"),
            user_id=self.user_id,
        )
        self.store.insert_fact(new_fact)

    # ---- assistant-side claim handling ---------------------------------

    # Parallelism cap. The fresh-tier verifier issues outbound HTTP
    # (Wikipedia + LLM judges); 8 in-flight requests is a safe upper
    # bound for a single-user demo without tripping provider rate limits.
    MAX_VERIFIER_WORKERS = 8

    def _verify_claims_parallel(
        self, claims: list[dict], turn_id: int,
    ) -> tuple[list[VerificationDecision], list[Intervention]]:
        """Run ``_verify_assistant_claim`` over ``claims`` in parallel.

        Order in the returned lists matches the input order regardless
        of completion order — the parallel dispatch is purely a latency
        optimization. Each claim's ``claim_decision`` event still fires
        from the worker thread as soon as that claim's verification
        completes, so the UI sees per-claim verdicts arrive live.
        """
        if not claims:
            return [], []
        if len(claims) == 1:
            vd, iv = self._verify_assistant_claim(claims[0], turn_id)
            return [vd], [iv]

        max_workers = min(self.MAX_VERIFIER_WORKERS, len(claims))
        verification_decisions: list[Optional[VerificationDecision]] = (
            [None] * len(claims)
        )
        interventions: list[Optional[Intervention]] = [None] * len(claims)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_idx: dict[concurrent.futures.Future, int] = {}
            for idx, fact in enumerate(claims):
                fut = pool.submit(self._verify_assistant_claim, fact, turn_id)
                future_to_idx[fut] = idx
            for fut in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[fut]
                vd, iv = fut.result()
                verification_decisions[idx] = vd
                interventions[idx] = iv
        # mypy/lint: every slot was filled by the as_completed loop above
        # (futures map 1:1 to indices and exceptions propagate via .result()).
        return (
            [vd for vd in verification_decisions if vd is not None],
            [iv for iv in interventions if iv is not None],
        )

    def _verify_assistant_claim(
        self, fact: dict, turn_id: int,
    ) -> tuple[VerificationDecision, Intervention]:
        # Layer 1.5 — verifiability triage. Cheap rule-based decision
        # on whether this claim warrants the EXPENSIVE verifier path
        # (fresh dispatch — retrieval + LLM judge / code generation).
        # The cheap walker stages (Tier U / W / derivation) ALWAYS
        # run because they're essentially free AND architecturally
        # load-bearing — the user's stored preferences must still
        # contradict the assistant when it gets them wrong, and that
        # contradiction lives in Tier U.
        triage = triage_claim(fact)
        self._emit(turn_id, "verifiability_triage", {
            "claim": dict(fact),
            **triage.to_dict(),
        })

        layer2 = self.router.classify(fact, source_turn_id=turn_id)

        # PASS_THROUGH suppresses fresh dispatch only. If U/W/derivation
        # all miss, the walker returns served_from_tier="fresh" with
        # status="unverifiable_pending_implementation" (the standard
        # walker behavior when fresh_dispatch=None and lookups miss).
        fresh_dispatch_fn = (
            _fresh.dispatch if triage.decision is TriageDecision.VERIFY else None
        )
        walker = walk_claim(
            fact, layer2, self.store,
            registry=self.registry,
            predicate_oracle=self.predicate_oracle,
            entity_oracle=self.entity_oracle,
            taxonomy_oracle=self.taxonomy_oracle,
            distribution_oracle=self.distribution_oracle,
            llm=self.llm,
            source_turn_id=turn_id,
            user_id=self.user_id,
            current_session=self.session_id,
            fresh_dispatch=fresh_dispatch_fn,
        )
        confidence = compute_decision_confidence(walker, store=self.store)
        intervention = plan_intervention(walker, confidence, store=self.store)

        # Bundle the Layer 5 verdict for the live trace UI: chain +
        # three-factor decision confidence + intervention pill all
        # land in one event so the operator sees the per-claim
        # conclusion as soon as it lands.
        self._emit(turn_id, "claim_decision", {
            "claim": dict(fact),
            "walker": walker.to_dict(),
            "confidence": confidence.to_dict(),
            "intervention": intervention.to_dict(),
            "threshold": get_threshold(),
        })

        return (
            VerificationDecision(
                claim=dict(fact),
                layer2=layer2.to_dict(),
                walker=walker.to_dict(),
                confidence=confidence.to_dict(),
                intervention=intervention.to_dict(),
            ),
            intervention,
        )

    # ---- chat context building -----------------------------------------

    def _tier_u_visible_facts(self) -> list[Fact]:
        """All currently-valid Tier U facts visible in the current
        session: cross-session user assertions + session-local
        assertions whose ``session_ids`` includes ``self.session_id``."""
        # find_currently_valid with current_session returns
        # cross-session rows + session-local rows in the active session,
        # ordered session-local-first. We want both, asserted_by="user".
        rows = self.store._conn.execute(
            "SELECT * FROM facts WHERE valid_until IS NULL "
            "AND user_id = ? AND asserted_by = 'user' "
            "AND (is_session_local = 0 OR (is_session_local = 1 AND EXISTS "
            "(SELECT 1 FROM json_each(session_ids) WHERE value = ?))) "
            "ORDER BY is_session_local DESC, id",
            (self.user_id, self.session_id),
        ).fetchall()
        from src.fact_store import _row_to_fact
        return [_row_to_fact(r) for r in rows]

    def _build_chat_system(
        self, user_facts: list[Fact], disputes: list[dict],
    ) -> str:
        facts_block = _format_facts_block(user_facts)
        prompt = CHAT_SYSTEM_TEMPLATE.format(facts_block=facts_block)
        if disputes:
            prompt = prompt + CHAT_USER_DISPUTE_TEMPLATE.format(
                disputes_block=_format_disputes_block(disputes),
            )
        return prompt

    def _build_history(self, prior_to_turn_id: int) -> list[ChatMessage]:
        """Build the chat-history messages from prior turns.

        Returns turns up to (but not including) ``prior_to_turn_id``,
        in insertion order. Includes both user and assistant turns; the
        chat model needs the full alternation to interpret context
        correctly. Skips turns with empty content (the placeholder
        assistant rows that haven't been filled yet).
        """
        turns = self.store.list_turns(user_id=self.user_id)
        msgs: list[ChatMessage] = []
        for t in turns:
            if t["id"] >= prior_to_turn_id:
                break
            content = (t.get("content") or "").strip()
            if not content:
                continue
            role = t.get("role")
            if role not in ("user", "assistant"):
                continue
            msgs.append(ChatMessage(role=role, content=content))
        return msgs

    # ---- event helper --------------------------------------------------

    def _emit(self, turn_id: int, stage: str, data: Any) -> None:
        """Best-effort pipeline_events insert. Failures don't crash
        the turn — observability is best-effort, the turn's correctness
        is the load-bearing write."""
        try:
            self.store.insert_pipeline_event(turn_id, stage, data)
        except Exception:
            pass


# ============================================================================
# Factory
# ============================================================================


def build_pipeline(
    db_path: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    session_id: Optional[str] = None,
    llm: Optional[LLMClient] = None,
) -> Pipeline:
    """Construct a wired-up Pipeline against ``db_path``.

    ``session_id`` defaults to ``AEDOS_SESSION_ID`` env or
    ``DEFAULT_SESSION_ID``. ``llm`` defaults to a fresh ``LLMClient()``;
    tests can pass a stub.
    """
    store = FactStore(db_path)
    registry = load_default_registry()
    if llm is None:
        llm = LLMClient()
    extractor = ClaimExtractor(llm, registry)
    router = Router(store, registry, llm=llm)
    corrector = Corrector(llm)
    predicate_oracle = PredicateEquivalence(store)
    entity_oracle = EntityEquivalence(store)
    taxonomy_oracle = EntityTaxonomy(store)
    distribution_oracle = PredicateDistribution(store)
    sid = session_id or os.getenv("AEDOS_SESSION_ID") or DEFAULT_SESSION_ID
    return Pipeline(
        store=store,
        registry=registry,
        llm=llm,
        extractor=extractor,
        router=router,
        corrector=corrector,
        predicate_oracle=predicate_oracle,
        entity_oracle=entity_oracle,
        taxonomy_oracle=taxonomy_oracle,
        distribution_oracle=distribution_oracle,
        user_id=user_id,
        session_id=sid,
    )
