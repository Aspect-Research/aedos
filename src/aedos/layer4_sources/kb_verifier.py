from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from dateutil import parser as _du_parser

from ..layer1_extraction.extractor import Claim
from ..layer1_extraction.temporal import BEFORE_PRESENT
from ..layer3_substrate.predicate_translation import PredicateTranslation, PredicateTranslationError
from ..layer3_substrate.resolver import EntityResolver
from .kb_protocol import KBEntityID, KBProtocol, LocalContext, Statement

_NOW = lambda: datetime.now(timezone.utc).isoformat()

# v0.16.1 WS5a: the geographic predicate cluster (the closed continent Q-id set,
# the geographic location-property P-ids, the geographic-container entity types,
# and the location-disjoint logic) was RELOCATED out of CORE behind the
# kb_protocol seam into the WikidataAdapter — those are genuine Wikidata facts.
# CORE now consults them only through the protocol's geo accessors
# (`is_location_property`, `geo_container_types`, `geographic_disjoint`), held
# behind the small `_is_location_property` / `_geo_container_types` /
# `_geographic_disjoint` shims below, which FAIL CLOSED (no disjoint / not a
# location property / empty container set => abstain) when the injected KB
# predates WS5a or errors. CORE holds NO continent Q-ids and NO geo P-id
# literals. (`_subsumption_upgrades` stays in CORE: it is the backend-neutral
# value-subsumption dual — it tries the opaque relation_types "part_of" AND
# "is_a", serves the taxonomic upgrade too, and carries no Wikidata literals.)


class KBVerdictType(str, Enum):
    VERIFIED = "verified"
    CONTRADICTED = "contradicted"
    NO_MATCH = "no_match"
    NO_KB_PATH = "no_kb_path"


@dataclass
class KBVerdict:
    verdict: KBVerdictType
    matched_statement: Optional[Statement] = None
    subject_kb_id: Optional[KBEntityID] = None
    trace: dict = field(default_factory=dict)


class KBVerifier:
    def __init__(
        self,
        kb_protocol: KBProtocol,
        entity_resolver: EntityResolver,
        predicate_translation: PredicateTranslation,
    ) -> None:
        self._kb = kb_protocol
        self._resolver = entity_resolver
        self._pt = predicate_translation
        # v0.16.1 WS4: the per-binding NOGOOD veto was REMOVED. A veto that
        # *suppresses* a sound contradiction is the dangerous §3.2 direction, it
        # had no production writer (the only record_nogood writer is the
        # adapter's verify_transitive_path, which writes the 'transitive_path'
        # kind the live walker consults — not a per-binding 'subsumption' veto),
        # and the operator forbids hand-seeded guards. The SubstrateExceptionCache
        # stays wired to its LIVE consumers (the walker's _nogood_vetoes and the
        # adapter's verify_transitive_path); the KB verifier no longer holds it.

    def verify(
        self,
        claim: Claim,
        current_time: Optional[str] = None,
        source_text: Optional[str] = None,
    ) -> KBVerdict:
        """Full KB verification: translate → map slots → resolve → lookup → compare.

        Honors claim polarity: a negated claim inverts the KB's positive-
        content verdict. Resolves the value entity, not just the lookup
        subject, and only treats a value mismatch as a contradiction for
        functional (single_valued) predicates.

        Honors the slot_to_qualifier lookup direction. For a standard
        predicate the KB statement is keyed on the claim's subject; for an
        inverse predicate (capital_of on P36, mother_of on P25 — whose seed maps
        the Aedos subject to ``statement_value``) the statement is keyed on the
        claim's *object*, so the lookup and the expected value are swapped.
        ``_lookup_targets`` decides the direction. The trace records it as
        ``lookup_inverted``; the other trace fields use direction-neutral names
        for the KB *statement* positions — ``entity`` is the statement subject,
        ``value_entity`` / ``value_resolved`` describe the statement value, and
        the abstention reasons are ``lookup_subject_unresolved`` /
        ``value_unresolved``.
        """
        if current_time is None:
            current_time = _NOW()

        # Step 1: get predicate metadata.
        try:
            meta = self._pt.consult(claim.predicate)
        except PredicateTranslationError:
            return KBVerdict(verdict=KBVerdictType.NO_KB_PATH, trace={"reason": "predicate_translation_failed"})

        if meta.routing_hint != "kb_resolvable" or not meta.bindings:
            return KBVerdict(verdict=KBVerdictType.NO_KB_PATH, trace={"reason": "not_kb_resolvable"})

        # v0.16 WS1: the substrate now holds a RANKED LIST of candidate
        # (predicate → KB property) bindings. Evidence arbitrates across them
        # (Decision 1). For the common/legacy/mock case of a single binding,
        # this loop runs exactly once and reproduces the pre-v0.16 single-
        # property path byte-for-byte: the same _lookup_targets / resolve /
        # lookup_statements / _compare_positive sequence on bindings[0].
        #
        # Arbitration:
        #   - VERIFIED if ANY binding grounds positively (we record every
        #     verifying chain in trace['bindings_tried']).
        #   - CONTRADICTED only from a single_valued binding whose resolved
        #     object satisfies that binding's value-type constraint
        #     (_object_satisfies_value_type fails OPEN — see invariants).
        #   - else NO_MATCH (carry the per-binding abstention reasons) or, when
        #     no binding even has a kb_property, NO_KB_PATH.
        bindings_tried: list[dict] = []
        verified_outcome: Optional[tuple[KBVerdict, dict]] = None
        contradicted_outcome: Optional[tuple[KBVerdict, dict]] = None
        last_no_match: Optional[KBVerdict] = None
        had_kb_path = False

        for binding in meta.bindings:
            if not binding.kb_property:
                continue
            had_kb_path = True

            outcome = self._verify_binding(
                claim, meta, binding, current_time, source_text
            )
            bindings_tried.append(
                {
                    "property": binding.kb_property,
                    "source": binding.source,
                    "verdict": outcome.verdict.value,
                    "abstention_reason": outcome.trace.get("abstention_reason"),
                }
            )

            if outcome.verdict == KBVerdictType.VERIFIED and verified_outcome is None:
                verified_outcome = (outcome, dict(outcome.trace))
            elif (
                outcome.verdict == KBVerdictType.CONTRADICTED
                and contradicted_outcome is None
            ):
                contradicted_outcome = (outcome, dict(outcome.trace))
            elif outcome.verdict in (KBVerdictType.NO_MATCH, KBVerdictType.NO_KB_PATH):
                last_no_match = outcome

        # No binding carried an actual KB property — identical to the pre-v0.16
        # `not meta.kb_property` abstention.
        if not had_kb_path:
            return KBVerdict(verdict=KBVerdictType.NO_KB_PATH, trace={"reason": "not_kb_resolvable"})

        # Arbitration order: a positive grounding wins (Decision 1); else a
        # sound single_valued contradiction; else the last abstention.
        if verified_outcome is not None:
            chosen, trace = verified_outcome
            trace["bindings_tried"] = bindings_tried
            return KBVerdict(
                verdict=chosen.verdict,
                matched_statement=chosen.matched_statement,
                subject_kb_id=chosen.subject_kb_id,
                trace=trace,
            )
        if contradicted_outcome is not None:
            chosen, trace = contradicted_outcome
            trace["bindings_tried"] = bindings_tried
            # Decision 5: surface the KB statement value that contradicted the
            # claim so the correction surface can name it.
            stmt = chosen.matched_statement
            if stmt is not None and "contradicting_value" not in trace:
                trace["contradicting_value"] = getattr(stmt, "value", None)
            return KBVerdict(
                verdict=chosen.verdict,
                matched_statement=chosen.matched_statement,
                subject_kb_id=chosen.subject_kb_id,
                trace=trace,
            )
        if last_no_match is not None:
            trace = dict(last_no_match.trace)
            trace["bindings_tried"] = bindings_tried
            return KBVerdict(
                verdict=last_no_match.verdict,
                matched_statement=last_no_match.matched_statement,
                subject_kb_id=last_no_match.subject_kb_id,
                trace=trace,
            )
        # Defensive: had a kb_property but produced no outcome (all vetoed).
        return KBVerdict(
            verdict=KBVerdictType.NO_MATCH,
            trace={"reason": "no_binding_grounded", "bindings_tried": bindings_tried},
        )

    def _verify_binding(
        self,
        claim: Claim,
        meta,
        binding,
        current_time: str,
        source_text: Optional[str],
    ) -> KBVerdict:
        """Verify the claim against ONE candidate binding. This is the per-
        binding equivalent of the pre-v0.16 single-property verify body —
        resolve → lookup → _compare_positive — keyed on the binding's
        kb_property / slot_to_qualifier / entity-types / single_valued."""
        # Step 2: map the claim's slots onto KB statement positions. An
        # inverse predicate keys its statement on the claim's *object*, so the
        # lookup entity and the expected value are swapped vs a standard one.
        targets = _lookup_targets(claim, binding)
        if targets is None:
            # A slot_to_qualifier shape the verifier cannot interpret. Abstain
            # with a clear trace note — never guess a direction, never crash.
            return KBVerdict(
                verdict=KBVerdictType.NO_KB_PATH,
                trace={
                    "reason": "unsupported_slot_to_qualifier",
                    "slot_to_qualifier": binding.slot_to_qualifier,
                    "abstention_reason": "unsupported_slot_to_qualifier",
                },
            )
        lookup_ref, expected_ref, lookup_inverted = targets
        # The Aedos slot each reference came from — keeps the resolver cache key
        # and the LocalContext honest about slot position.
        lookup_slot = "object" if lookup_inverted else "subject"
        value_slot = "subject" if lookup_inverted else "object"

        # Step 3: resolve the KB lookup entity — the entity the statement is
        # keyed on (it becomes the KB statement subject).
        # Pass entity types for the Aedos slot being resolved;
        # the wikidata adapter post-filters candidates by P31 ∩ types.
        # Thread the source text + immediate-claim context to
        # the resolver so the Wikipedia normalizer's Stage 2 can use it.
        lookup_ctx = LocalContext(
            predicate=claim.predicate,
            slot_position=lookup_slot,
            asserting_party=claim.asserting_party,
            expected_entity_types=_types_for_slot(binding, lookup_slot),
            source_text=source_text,
            claim_subject=claim.subject,
            claim_predicate=claim.predicate,
            claim_object=claim.object,
            claim_id=claim.claim_id,
        )
        lookup_subject_id = self._resolver.select(
            self._resolver.resolve(lookup_ref, lookup_ctx), lookup_ctx
        )
        # v0.16 WS3: capture the entity_resolution_cache row id the
        # lookup-subject resolution touched, BEFORE the value-entity resolve
        # below overwrites the resolver's request-scoped state. This is the
        # retractable dependency the KB verdict rests on — a wrong subject
        # resolution is what a correction would retract. `getattr` guards mock
        # resolvers that predate the accessor; None when no cache row was hit.
        _last_row_id = getattr(self._resolver, "last_cache_row_id", None)
        resolution_cache_row_id = _last_row_id() if callable(_last_row_id) else None
        if lookup_subject_id is None:
            return KBVerdict(
                verdict=KBVerdictType.NO_MATCH,
                trace={
                    "reason": "subject_resolution_failed",
                    "reference": lookup_ref,
                    "abstention_reason": "lookup_subject_unresolved",
                    "lookup_inverted": lookup_inverted,
                    "resolution_cache_row_id": resolution_cache_row_id,
                },
            )

        # Step 4: resolve the expected-value entity — compared against the
        # looked-up statement values (object resolution applied to
        # whichever Aedos slot is the KB statement value). Falls back to the raw
        # string for literal comparison.
        expected_value = expected_ref
        value_resolved = False
        if meta.object_type == "entity":
            # For a geographic-location predicate, widen the object's
            # accepted-type filter to admit continents — the per-predicate type
            # lists omit them, which otherwise blocks "X is in Europe" from
            # resolving "Europe" to the continent (the geo-container types live
            # behind the protocol's geo_container_types, relocated WS5a). Only
            # widens an existing non-empty filter; an open (None/empty) filter
            # is left open.
            value_types = _types_for_slot(binding, value_slot)
            if value_types and self._is_location_property(binding.kb_property):
                value_types = list(
                    dict.fromkeys([*value_types, *self._geo_container_types()])
                )
            value_ctx = LocalContext(
                predicate=claim.predicate,
                slot_position=value_slot,
                asserting_party=claim.asserting_party,
                expected_entity_types=value_types,
                source_text=source_text,
                claim_subject=claim.subject,
                claim_predicate=claim.predicate,
                claim_object=claim.object,
                claim_id=claim.claim_id,
            )
            resolved_value = self._resolver.select(
                self._resolver.resolve(expected_ref, value_ctx), value_ctx
            )
            if resolved_value is not None:
                expected_value = resolved_value
                value_resolved = True

        # Step 5: look up KB statements for (lookup entity, kb_property).
        statements = self._kb.lookup_statements(lookup_subject_id, binding.kb_property)
        if not statements:
            # No-statements subsumption fallback.
            # Some entity classes don't carry the specific kb_property the
            # predicate maps to — rivers don't have P131 (located_in admin
            # entity); they're related to geography via P17 (country),
            # P206 (located in body of water), or directly via P30
            # (continent) on the country chain. For an entity-typed
            # resolved expected value, query the subsumption oracle
            # directly: is the lookup_subject geographically subsumed
            # by the claim's expected value? The `part_of` alternation
            # (P131/P361/P30/P206/P17) catches the chain Wikidata
            # actually uses. Soundness: only fires when both subject
            # and expected resolve to KB Q-ids, and subsumption returns
            # a positive verdict (a_subsumed_by_b or equivalent) — never
            # promotes to VERIFIED on uncertainty.
            if (
                meta.object_type == "entity"
                and value_resolved
                and isinstance(lookup_subject_id, str)
                and isinstance(expected_value, str)
                and self._is_location_property(binding.kb_property)
                and self._subsumption_upgrades(lookup_subject_id, expected_value)
            ):
                pos_verdict = KBVerdictType.VERIFIED
                final_verdict = _apply_polarity(pos_verdict, claim.polarity)
                return KBVerdict(
                    verdict=final_verdict,
                    matched_statement=None,
                    subject_kb_id=lookup_subject_id,
                    trace={
                        "entity": lookup_subject_id,
                        "value_entity": expected_value,
                        "value_resolved": True,
                        "polarity": claim.polarity,
                        "positive_verdict": pos_verdict.value,
                        "lookup_inverted": lookup_inverted,
                        "no_statements_subsumption_fallback": True,
                        "resolution_cache_row_id": resolution_cache_row_id,
                    },
                )
            # Symmetric CONTRADICTED arm to the VERIFIED subsumption-upgrade
            # above: when the subject itself is geographically disjoint from the
            # claimed container, that is a sound contradiction even with no
            # statement on this binding's property. Example: "The Vatican is in
            # Africa" — the Vatican carries no P131 statement (only P30=Europe),
            # so the in-statements disjoint check (_compare_positive, gated on a
            # scope_mismatch statement) never fires. This keeps the
            # "X in [wrong continent]" fast contradiction available even when the
            # multi-property binding leaves it only on the in-statements arm, and avoids the
            # open-ended KB-neighbor fan-out (budget_wall_clock abstain) the walk
            # otherwise falls into. Gated identically to the in-statements arm
            # (location property, entity object, both Q-ids, standard direction)
            # and uses the same fail-closed geographic_disjoint protocol op
            # (relocated WS5a), which requires positive KB subsumption into a
            # different continent — no new soundness surface. The VERIFIED arm
            # above runs first, so a true
            # "X in [right continent]" verifies and never reaches this check.
            if (
                meta.object_type == "entity"
                and value_resolved
                and not lookup_inverted
                and isinstance(lookup_subject_id, str)
                and isinstance(expected_value, str)
                and self._is_location_property(binding.kb_property)
                and self._geographic_disjoint(lookup_subject_id, expected_value)
            ):
                pos_verdict = KBVerdictType.CONTRADICTED
                final_verdict = _apply_polarity(pos_verdict, claim.polarity)
                return KBVerdict(
                    verdict=final_verdict,
                    matched_statement=None,
                    subject_kb_id=lookup_subject_id,
                    trace={
                        "entity": lookup_subject_id,
                        "value_entity": expected_value,
                        "value_resolved": True,
                        "polarity": claim.polarity,
                        "positive_verdict": pos_verdict.value,
                        "lookup_inverted": lookup_inverted,
                        "no_statements_disjoint_fallback": True,
                        "resolution_cache_row_id": resolution_cache_row_id,
                    },
                )
            # NO_MATCH is polarity-invariant — absence of evidence is not evidence.
            return KBVerdict(
                verdict=KBVerdictType.NO_MATCH,
                subject_kb_id=lookup_subject_id,
                trace={
                    "reason": "no_statements_found",
                    "entity": lookup_subject_id,
                    "property": binding.kb_property,
                    "abstention_reason": "no_statements",
                    "lookup_inverted": lookup_inverted,
                    "resolution_cache_row_id": resolution_cache_row_id,
                },
            )

        # Step 6: verdict for the claim's *positive* content (polarity-agnostic).
        # _compare_positive is direction-agnostic — it compares the expected
        # value against the statement values regardless of which Aedos slot the
        # expected value came from.
        pos_verdict, statement, abstention_reason = self._compare_positive(
            statements, claim, expected_value, value_resolved, meta, binding, current_time
        )

        # v0.16 WS1: the copula fix. A single_valued binding can drive
        # CONTRADICTED only when the resolved object satisfies that binding's
        # value-type constraint (P2302 value-type). This is the P31-vs-P106
        # case: a copula claim routed ambiguously to P31 (instance-of) vs P106
        # (occupation) — the resolved object (an occupation) satisfies P106's
        # value-type but not P31's, so only the P106 binding can contradict.
        # _object_satisfies_value_type fails OPEN (True when the binding has no
        # value-type constraint, or when the object can't be type-confirmed),
        # so the legacy single_valued contradiction tests are unaffected.
        if pos_verdict == KBVerdictType.CONTRADICTED:
            resolved_obj = (
                expected_value if value_resolved and isinstance(expected_value, str) else None
            )
            if not self._object_satisfies_value_type(resolved_obj, binding):
                return KBVerdict(
                    verdict=KBVerdictType.NO_MATCH,
                    subject_kb_id=lookup_subject_id,
                    trace={
                        "entity": lookup_subject_id,
                        "property": binding.kb_property,
                        "value_entity": expected_value,
                        "value_resolved": value_resolved,
                        "polarity": claim.polarity,
                        "lookup_inverted": lookup_inverted,
                        "abstention_reason": "value_type_incompatible_binding",
                        "resolution_cache_row_id": resolution_cache_row_id,
                    },
                )

        # v0.16.1 WS2 (occupation-copula grounding): FAIL-CLOSED positive gate.
        # A value-type-gated binding (the P106 occupation candidate synthesized
        # for a copula's instance_of/is_a) may yield VERIFIED only when the
        # resolved object is PROVABLY a member of one of the binding's declared
        # value-type classes (a confirmed occupation/profession). This is the new
        # POSITIVE grounding path — so it fails CLOSED (unlike the CONTRADICTED
        # gate above which fails OPEN): any type uncertainty, an unresolved object,
        # a KB error, or a missing constraint blocks the verify and falls through
        # to NO_MATCH, abstaining (and letting the primary P31 binding handle the
        # claim). So "Paris is a city" / "X is a river" — whose object is not a
        # confirmed occupation class — never false-verifies through P106. The
        # primary binding (value_type_gated=False) is untouched. P106 stays
        # single_valued=0, so a wrong occupation never CONTRADICTED above; here a
        # wrong occupation simply doesn't VERIFY → abstain.
        if pos_verdict == KBVerdictType.VERIFIED and getattr(binding, "value_type_gated", False):
            resolved_obj = (
                expected_value if value_resolved and isinstance(expected_value, str) else None
            )
            if not self._object_confirms_value_type(resolved_obj, binding):
                return KBVerdict(
                    verdict=KBVerdictType.NO_MATCH,
                    subject_kb_id=lookup_subject_id,
                    trace={
                        "entity": lookup_subject_id,
                        "property": binding.kb_property,
                        "value_entity": expected_value,
                        "value_resolved": value_resolved,
                        "polarity": claim.polarity,
                        "lookup_inverted": lookup_inverted,
                        "abstention_reason": "value_type_unconfirmed_positive_gate",
                        "resolution_cache_row_id": resolution_cache_row_id,
                    },
                )

        # Step 7: apply claim polarity. A negated claim asserts the triple
        # is false, so a KB-supported triple makes it CONTRADICTED, and vice versa.
        final_verdict = _apply_polarity(pos_verdict, claim.polarity)

        trace = {
            "entity": lookup_subject_id,
            "property": binding.kb_property,
            "value_entity": expected_value,
            "value_resolved": value_resolved,
            "polarity": claim.polarity,
            "positive_verdict": pos_verdict.value,
            "single_valued": binding.single_valued,
            "lookup_inverted": lookup_inverted,
            "resolution_cache_row_id": resolution_cache_row_id,
        }
        # When the verdict is an abstention (NO_MATCH), record *why* —
        # debugging needs to tell a resolution failure apart from a genuine
        # absence of evidence.
        if abstention_reason is not None:
            trace["abstention_reason"] = abstention_reason

        return KBVerdict(
            verdict=final_verdict,
            matched_statement=statement,
            subject_kb_id=lookup_subject_id,
            trace=trace,
        )

    def _object_satisfies_value_type(self, resolved_obj_qid: Optional[str], binding) -> bool:
        """v0.16 WS1 (copula fix). True when the resolved object provably
        satisfies the binding's value-type constraint — or when the binding has
        NO value-type constraint, or the object can't be type-confirmed.

        Fails OPEN (returns True = 'cannot rule out, so this binding MAY
        contradict') in every uncertain case, per the invariant:
          - no binding value-type constraint known  → True (permit)
          - object did not resolve to a Q-id         → True (permit)
          - KB error / unknown subsumption verdict   → True (permit)
        It returns False — blocking the contradiction — only when the binding
        DOES declare a value-type constraint AND the resolved object PROVABLY
        fails it (subsumption says the object is `unrelated` to every declared
        value-type). That is the only case where letting this binding contradict
        would be unsound (the predicate was mis-routed to a value-type-
        incompatible property, e.g. P31 for an occupation claim)."""
        value_types = list(binding.object_entity_types or [])
        if not value_types:
            return True  # no constraint known → permit the contradiction
        if not resolved_obj_qid or not isinstance(resolved_obj_qid, str):
            return True  # object not type-confirmable → permit (fail open)
        # The object satisfies the constraint if it is_a (or equals) ANY of the
        # declared value-types. We only BLOCK when every declared type is
        # provably `unrelated` — any uncertainty (error / unknown) permits.
        any_unrelated_only = True
        for vt in value_types:
            if not isinstance(vt, str):
                any_unrelated_only = True
                continue
            if vt == resolved_obj_qid:
                return True
            try:
                r = self._kb.subsumption(resolved_obj_qid, vt, "is_a")
            except Exception:
                return True  # KB error → permit (fail open)
            if r.verdict in ("a_subsumed_by_b", "equivalent"):
                return True  # provably satisfies the value-type
            if r.verdict != "unrelated":
                # b_subsumed_by_a or any non-`unrelated` verdict is uncertain
                # w.r.t. "object fails the constraint" → permit (fail open).
                any_unrelated_only = False
        # Reached only when no declared value-type was satisfied. Block the
        # contradiction only if EVERY check came back `unrelated` (provable
        # failure); otherwise an uncertain verdict permits.
        return not any_unrelated_only

    def _object_confirms_value_type(self, resolved_obj_qid: Optional[str], binding) -> bool:
        """v0.16.1 WS2 (occupation-copula grounding). FAIL-CLOSED dual of
        ``_object_satisfies_value_type``: True ONLY when the resolved object is
        PROVABLY a member of one of the binding's declared value-type classes
        (subsumption returns ``a_subsumed_by_b`` / ``equivalent``, or the object
        Q-id equals a declared type). Used to gate the POSITIVE grounding of a
        value-type-gated candidate binding (P106 occupation): a positive verify
        is a new grounding surface, so it must fail CLOSED.

        Returns False — blocking the verify, so the claim abstains — in EVERY
        uncertain case, the mirror of the CONTRADICTED gate's fail-open:
          - no declared value-type constraint        → False (cannot confirm)
          - object did not resolve to a Q-id          → False (cannot confirm)
          - KB error on a subsumption probe           → False (cannot confirm)
          - no declared type is provably satisfied     → False (cannot confirm)
        So "X is a river" (object resolves to a river class, not an occupation/
        profession class) does NOT verify through P106 — the type gate abstains
        and the primary P31 binding handles the claim. Only a confirmed
        occupation object can VERIFY via the gated P106 binding."""
        value_types = list(binding.object_entity_types or [])
        if not value_types:
            return False  # no constraint → cannot confirm → fail closed
        if not resolved_obj_qid or not isinstance(resolved_obj_qid, str):
            return False  # object not type-confirmable → fail closed
        for vt in value_types:
            if not isinstance(vt, str):
                continue
            if vt == resolved_obj_qid:
                return True  # the object IS the declared value-type class
            try:
                r = self._kb.subsumption(resolved_obj_qid, vt, "is_a")
            except Exception:
                continue  # KB error on this probe → try the next; never confirm on error
            if r.verdict in ("a_subsumed_by_b", "equivalent"):
                return True  # provably a member of the declared value-type class
        return False  # nothing provably confirmed → fail closed (abstain)

    def _compare_positive(
        self,
        statements: list[Statement],
        claim: Claim,
        expected_value,
        value_resolved: bool,
        meta,
        binding,
        current_time: str,
    ) -> tuple[KBVerdictType, Optional[Statement], Optional[str]]:
        """Verdict for the claim's positive content, ignoring polarity.

        A value match on a scope-compatible statement is VERIFIED. A
        scope-compatible statement whose value does not match is CONTRADICTED
        only for a functional (single_valued) predicate — for a multi-valued
        predicate the KB simply holds other values and the claim's value may
        also be true, so the result is NO_MATCH.

        N1: when the expected value is an entity reference that did not resolve,
        a value mismatch is *not* a contradiction. An unresolved natural-language
        string compared against KB Q-numbers never matches, so a non-match is a
        resolution failure, not evidence of falsity — architecture 3.2 classes
        resolution failure as a false-abstain source, never a false-contradiction
        source. The functional-predicate CONTRADICTED branch is therefore
        suppressed when `meta.object_type == "entity"` and the expected value
        did not resolve; the literal-match VERIFIED path above is unaffected.

        Returns (verdict, statement, abstention_reason). abstention_reason is
        None for VERIFIED/CONTRADICTED and one of "value_unresolved" /
        "no_matching_statement" for NO_MATCH.
        """
        scope_mismatch: Optional[Statement] = None
        # The DISTINCT scope-compatible values the subject holds for this
        # property that the claim did not match. A predicate the oracle marked
        # single_valued may nonetheless hold MULTIPLE distinct values in the KB
        # data (e.g. France P571 inception: 843 West Francia, 1958 Fifth
        # Republic). When that is so, a claim matching NONE of them is not a
        # functional conflict — the KB simply isn't functionally single-valued
        # for this subject — so it can never soundly CONTRADICT (§3.2). We only
        # let the single_valued contradiction fire on a genuine SINGLE distinct
        # value. (The VERIFIED match-any loop below already runs across ALL
        # statements, so a claim that matches ANY held value verifies first.)
        mismatch_values: set = set()
        # E4 level 2: among statements whose VALUE matches the claim, track those
        # that are provably ENDED (P582 < now) vs any that are NOT provably past
        # (still current, future, or an ambiguous same-period end). Only consulted
        # for a strictly present-tense (fully unscoped) claim that matched no
        # CURRENT statement — see the contradiction branch after the loop. Gated to
        # ENTITY-valued role/state predicates: temporal currency ("the role ended")
        # is meaningless for a date/quantity value, and a stray P582 on a date
        # statement must never flip a value-MATCHING (true) date claim to CONTRADICT.
        present_unscoped = _claim_present_unscoped(claim) and meta.object_type == "entity"
        matched_ended: Optional[Statement] = None
        matched_not_past = False
        for stmt in statements:
            value_match = _value_matches(stmt.value, expected_value)
            if present_unscoped and value_match:
                stmt_until = stmt.qualifiers.get("P582")
                if stmt_until and _end_provably_past(stmt_until, current_time):
                    if matched_ended is None:
                        matched_ended = stmt
                else:
                    matched_not_past = True
            if not _scope_compatible(stmt, claim, current_time, meta.object_type):
                continue
            if value_match:
                return KBVerdictType.VERIFIED, stmt, None
            if scope_mismatch is None:
                scope_mismatch = stmt
            # C2S-1: for a date predicate, key the distinctness set on the
            # year-normalized value so two statements that denote the SAME year
            # at differing precision (e.g. P569 '+1879-03-14...' day-precision and
            # '+1879-01-01...' year-precision — a coarsening of one birth fact)
            # collapse to a single distinct value. Without this the multi-value
            # gate below over-fires: a genuinely WRONG-year claim (born_on "1900")
            # would see two raw strings, count as multi-valued, and be downgraded
            # to abstain instead of the correct CONTRADICTED. Genuinely distinct
            # YEARS (France P571 = {0843, 1958}) still normalize to two keys and
            # keep abstaining. Non-date values are keyed verbatim.
            if meta.object_type == "time":
                mismatch_values.add(_normalize_date_value(stmt.value) or stmt.value)
            else:
                mismatch_values.add(stmt.value)

        value_unresolved = meta.object_type == "entity" and not value_resolved

        # Try subsumption upgrade on any
        # scope-compatible mismatch — functional or not. A KB statement value
        # that is a specialization of the claimed value (e.g. Honolulu when
        # the claim says "United States"; Île-de-France when the claim says
        # "France") VERIFIES the claim rather than contradicting / abstaining —
        # the more-specific KB fact entails the more-general claim. Only run
        # for entity-typed values that resolved to KB IDs; literal comparisons
        # (numbers, dates, strings) don't subsume.
        if (
            scope_mismatch is not None
            and meta.object_type == "entity"
            and value_resolved
            and isinstance(scope_mismatch.value, str)
            and isinstance(expected_value, str)
            and self._subsumption_upgrades(scope_mismatch.value, expected_value)
        ):
            return KBVerdictType.VERIFIED, scope_mismatch, None

        if scope_mismatch is not None and binding.single_valued:
            if value_unresolved:
                # N1: the expected-value reference never resolved — the mismatch
                # is a resolution failure, not a contradiction. Abstain, not lie.
                return KBVerdictType.NO_MATCH, None, "value_unresolved"
            # C2-3 (§3.2): a predicate the oracle marked single_valued can still
            # hold MULTIPLE distinct values for this subject in the KB data
            # (France P571 = {843 West Francia, 1958 Fifth Republic, ...}). When
            # the subject presents more than one distinct value and the claim
            # matched none of them above, this is NOT a genuine functional
            # conflict — the data is not functionally single-valued here — so a
            # non-match cannot soundly contradict. Abstain. The genuine
            # single-value contradiction (one distinct KB value differing from a
            # precise claim, e.g. born_on) is preserved: it has exactly one
            # distinct mismatch value and falls through to CONTRADICTED.
            if len(mismatch_values) > 1:
                return KBVerdictType.NO_MATCH, None, "multi_valued_single_valued_predicate"
            # S3 generalization: never contradict on a type-mismatched mapping.
            # If the looked-up statement's datatype is incompatible with the
            # predicate's object_type, the predicate is likely mis-mapped to the
            # wrong KB property; abstain rather than fabricate a contradiction.
            if not _contradiction_value_type_ok(
                getattr(scope_mismatch, "value_type", None), meta.object_type
            ):
                return KBVerdictType.NO_MATCH, None, "value_type_object_type_mismatch"
            # v0.16.1 WS1: an APPROXIMATE-year claim ("c. 1550") that did not
            # year-match the KB value above may NEVER contradict a date predicate.
            # An approximation ("around 1550") cannot soundly contradict a nearby
            # exact KB date — the marker explicitly disclaims precision — so the
            # only sound non-match outcome is abstain. Gated on object_type=="time"
            # (a date/time predicate) so a non-date approximate string never reaches
            # this. A PRECISE wrong year (no marker) still contradicts as before.
            if meta.object_type == "time" and _is_approx_year(str(expected_value)):
                return KBVerdictType.NO_MATCH, None, "approximate_date_no_year_match"
            # C2-FC1 + E2 (§3.2): a date predicate may CONTRADICT only when the
            # claim value and the KB value are precision-aware dates that GENUINELY
            # DISAGREE at a precision BOTH assert (`_date_relation == "mismatch"`):
            # claim "Dec 18 1936" vs KB "1936-12-17" (differ at day) contradicts;
            # claim "1994" vs KB "1998-…" (differ at year) contradicts. Anything
            # the relation finds INCOMPARABLE — an unparseable/comparison-phrase
            # object ("before 1800"), or a claim FINER than the KB so the KB can't
            # confirm it (claim day vs KB year-only, a coarsening) — is a
            # parse/precision gap, NOT falsity → abstain. (The approx-year guard
            # above already handles "c. 1550"-style markers.)
            if meta.object_type == "time":
                if _date_relation(
                    str(expected_value), getattr(scope_mismatch, "value", None)
                ) != "mismatch":
                    return KBVerdictType.NO_MATCH, None, "date_not_a_clean_mismatch"
            return KBVerdictType.CONTRADICTED, scope_mismatch, None

        # Conservative DISJOINT
        # verdict for non-functional LOCATION predicates only. The
        # check uses the part_of subsumption alternation (P131/P361/
        # P30/P206/P17) which is geographic in nature; firing on
        # non-geographic predicates like `employed_by` (P108) or
        # `member_of` (P463) misfires because two distinct entities
        # in the same continent are NOT semantically disjoint with
        # respect to the predicate (Einstein was employed by both
        # IAS in the US AND ETH Zurich in Switzerland; neither
        # contradicts the other). The kb_property gate restricts
        # the disjoint check to location predicates.
        if (
            scope_mismatch is not None
            and meta.object_type == "entity"
            and value_resolved
            and isinstance(scope_mismatch.value, str)
            and isinstance(expected_value, str)
            and self._is_location_property(binding.kb_property)
            and self._geographic_disjoint(scope_mismatch.value, expected_value)
        ):
            return KBVerdictType.CONTRADICTED, scope_mismatch, None

        # E4 level 2 (§3.2): a strictly present-tense claim whose value matched ONLY
        # statements that are PROVABLY ENDED (P582 < now), with NO current matching
        # statement, is FALSE as a present-tense assertion — the fact genuinely
        # ended ("Francis is the pope" after the papacy ended). CONTRADICT with the
        # ended statement (its P582 carries the end date for the explanation).
        # Strict gates, each closing an enumerated false-contradict risk:
        #   • present_unscoped — a bare present claim on an ENTITY role/state value
        #     only; "X WAS the pope" carries BEFORE_PRESENT and is excluded (it
        #     verifies off the ended statement), and a date/quantity value can never
        #     reach here (present_unscoped folds in meta.object_type == "entity").
        #   • we did not VERIFY above — so no scope-compatible (current/future-end)
        #     statement matched the value.
        #   • matched_ended is set AND not matched_not_past — EVERY value-matching
        #     statement is provably past; if any is current/future/ambiguous we
        #     abstain instead (matched_not_past blocks the contradiction).
        # The value genuinely matched (entity values that failed to resolve never
        # match), so this is not a resolution failure.
        if present_unscoped and matched_ended is not None and not matched_not_past:
            return KBVerdictType.CONTRADICTED, matched_ended, None

        reason = "value_unresolved" if value_unresolved else "no_matching_statement"
        return KBVerdictType.NO_MATCH, None, reason

    def _is_location_property(self, kb_property) -> bool:
        """v0.16.1 WS5a: protocol shim. True when the KB property is a
        geographic location-containment property, per the adapter's
        `is_location_property` (which holds the closed P-id set). Optional on
        the protocol — consulted via getattr so a pre-WS5a stub KB keeps
        working; FAILS CLOSED (returns False => the disjoint arm and the
        continent-widening are skipped, i.e. abstain) when the method is absent
        or errors. §3.2 soundness-over-completeness."""
        fn = getattr(self._kb, "is_location_property", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(kb_property))
        except Exception:
            return False

    def _geo_container_types(self) -> frozenset:
        """v0.16.1 WS5a: protocol shim. The geographic-container entity types
        (continent) used to widen a location predicate's object-type filter, per
        the adapter's `geo_container_types`. Optional on the protocol; FAILS
        CLOSED (empty set => no widening) when absent or on error."""
        fn = getattr(self._kb, "geo_container_types", None)
        if not callable(fn):
            return frozenset()
        try:
            return frozenset(fn())
        except Exception:
            return frozenset()

    def _geographic_disjoint(self, kb_value: str, expected_value: str) -> bool:
        """v0.16.1 WS5a: protocol shim for the relocated `_location_disjoint`.
        True when KB confirms `kb_value` is geographically disjoint from
        `expected_value`, per the adapter's `geographic_disjoint` (which holds
        the continent set + the two-path subsumption logic). Optional on the
        protocol; FAILS CLOSED (returns False => no contradiction => abstain)
        when the method is absent or errors — never fabricates a disjoint
        verdict. §3.2 soundness-over-completeness."""
        fn = getattr(self._kb, "geographic_disjoint", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(kb_value, expected_value))
        except Exception:
            return False

    def _subsumption_upgrades(self, kb_value: str, expected_value: str) -> bool:
        """Query the KB for whether the
        KB statement value (specific) is subsumed by the claim's expected
        value (general). Tries `part_of` (geographic / location containment,
        Wikidata P131/P361) and `is_a` (taxonomic, Wikidata P31/P279). The
        first that returns `a_subsumed_by_b` or `equivalent` upgrades the
        verdict to VERIFIED.

        Fails closed on error — unknown relation types, invalid Q-IDs, or KB
        outages fall through to no-upgrade, preserving the prior CONTRADICTED
        verdict. Never promotes to VERIFIED on uncertainty (architecture §3.2
        soundness-over-completeness).
        """
        if kb_value == expected_value:
            return True
        for relation_type in ("part_of", "is_a"):
            try:
                r = self._kb.subsumption(kb_value, expected_value, relation_type)
            except Exception:
                continue
            if r.verdict in ("a_subsumed_by_b", "equivalent"):
                return True
        return False


def _lookup_targets(claim: Claim, binding) -> Optional[tuple[str, str, bool]]:
    """Map a claim's slots onto KB statement positions via slot_to_qualifier.

    v0.16 WS1: ``binding`` is a ``PredicateBinding`` (the per-binding
    slot_to_qualifier). It exposes the same ``slot_to_qualifier`` attribute the
    pre-v0.16 ``meta`` did, so the direction logic is unchanged.

    Returns ``(kb_lookup_ref, expected_value_ref, lookup_inverted)``:

    - ``kb_lookup_ref`` — the claim slot value to resolve and key the
      ``lookup_statements`` call on; it becomes the KB statement *subject*.
    - ``expected_value_ref`` — the claim slot value compared against the
      looked-up statement values; it is the KB statement *value*.
    - ``lookup_inverted`` — True when the claim's *object* is the KB statement
      subject — an inverse predicate, e.g. ``capital_of`` on P36 or
      ``mother_of`` on P25, whose seed maps the Aedos subject to
      ``statement_value``.

    Standard mapping (``subject`` -> ``statement_subject``): the lookup is keyed
    on the claim's subject and the object is the expected value. Inverse mapping
    (``subject`` -> ``statement_value``): the KB stores the statement on the
    other entity, so the lookup is keyed on the claim's object and the subject
    is the expected value.

    A null/absent ``slot_to_qualifier`` is treated as the standard mapping — the
    default, preserved so every non-inverse predicate behaves exactly as
    before and inline-generated rows without an explicit map keep working.

    Returns ``None`` for a ``slot_to_qualifier`` the verifier cannot interpret
    (a qualifier-keyed or contradictory subject/object map). ``verify`` turns
    that into a ``NO_KB_PATH`` abstention with a trace note — it never guesses a
    direction and never crashes. This branch guards only against
    malformed inline-generated rows.
    """
    slot_map = binding.slot_to_qualifier
    if not slot_map:
        return (claim.subject, claim.object, False)
    subject_slot = slot_map.get("subject")
    object_slot = slot_map.get("object")
    if subject_slot in (None, "statement_subject") and object_slot in (None, "statement_value"):
        return (claim.subject, claim.object, False)
    if subject_slot == "statement_value" and object_slot in (None, "statement_subject"):
        return (claim.object, claim.subject, True)
    return None


def _types_for_slot(binding, slot: str) -> list[str]:
    """Pick the entity-types list that corresponds to the Aedos
    slot being resolved. Returns an empty list when no types are configured
    for that slot (the adapter then skips its post-filter).

    v0.16 WS1: ``binding`` is a ``PredicateBinding`` carrying the per-binding
    subject/object entity-types (mirrors the pre-v0.16 ``meta`` accessor)."""
    if slot == "subject":
        return list(binding.subject_entity_types or [])
    if slot == "object":
        return list(binding.object_entity_types or [])
    return []


def _apply_polarity(pos_verdict: KBVerdictType, polarity: int) -> KBVerdictType:
    """Apply claim polarity to a positive-content verdict.

    For an asserted claim (polarity 1) the verdict is unchanged. For a negated
    claim (polarity 0) a KB-verified positive triple makes the negation
    CONTRADICTED and a KB-contradicted positive triple makes it VERIFIED.
    NO_MATCH carries no polarity information and is unchanged.
    """
    if polarity == 1:
        return pos_verdict
    if pos_verdict == KBVerdictType.VERIFIED:
        return KBVerdictType.CONTRADICTED
    if pos_verdict == KBVerdictType.CONTRADICTED:
        return KBVerdictType.VERIFIED
    return pos_verdict


# Which KB statement value-types may
# soundly drive a CONTRADICTED verdict for each predicate object_type. A
# predicate the oracle mis-mapped to a wrong-datatype property (e.g. an
# authorship predicate routed to P585 point-in-time instead of P50 author)
# would otherwise compare an entity claim against date values and could
# fabricate a contradiction. When the looked-up statement's datatype is
# incompatible with the predicate's declared object_type we abstain instead of
# contradicting — never lie on a type-mismatched mapping. Statement.value_type
# ∈ {entity, literal, date, quantity}. `literal` is permitted everywhere
# because external-id / string-valued entity properties (P212 ISBN) legitimately
# come back literal-typed; blocking it would cause false abstains.
_OBJECT_TYPE_COMPATIBLE_VALUE_TYPES = {
    "entity": {"entity", "literal"},
    "entity_list": {"entity", "literal"},
    "time": {"date", "literal"},
    "quantity": {"quantity", "literal"},
}


def _contradiction_value_type_ok(value_type: Optional[str], object_type: str) -> bool:
    """True when a CONTRADICTED verdict driven by a statement of `value_type`
    is type-sound for a predicate whose object_type is `object_type`. Returns
    True (don't block) for object_types we don't constrain (e.g. proposition)
    or when the adapter left value_type untagged — preserving prior behavior."""
    allowed = _OBJECT_TYPE_COMPATIBLE_VALUE_TYPES.get(object_type)
    if allowed is None:
        return True
    if not value_type:
        return True
    return value_type in allowed


_YEAR_RE = re.compile(r"^[+-]?(\d{4})(?:-\d{2}(?:-\d{2})?)?(?:T.*)?$")
_BARE_YEAR_RE = re.compile(r"^[+-]?\d{4}$")

# v0.16.1 WS1: leading approximation markers on a CLAIM-side date value
# ("c. 1550", "circa 1550", "~1550"). Anchored at the start, case-insensitive,
# with trailing whitespace consumed. Ordered longest-first so "circa"/"approximately"
# win before their abbreviations; the bare single-letter "c"/"ca" require a
# following separator (handled by the regex word boundary / optional dot) so a
# real word like "carved" is never stripped. The '~' marker has no boundary.
_APPROX_YEAR_RE = re.compile(
    r"^(?:approximately|approx\.?|circa|about|around|ca\.?|c\.?|~)\s+|^~\s*",
    re.IGNORECASE,
)


def _strip_approx_year(value: str) -> Optional[str]:
    """If `value` is an approximate-year claim ("c. 1550", "circa 1550",
    "~1550"), return the bare 4-digit year remainder ("1550"); otherwise None.

    Strips exactly ONE leading approximation marker (case-insensitive) and the
    whitespace after it, then returns the remainder ONLY when that remainder is
    a bare 4-digit year (matching `_BARE_YEAR_RE`). Used on the CLAIM side of
    `_value_matches` so an approximate year matches the KB on exact year
    equality. Returns None when there is no marker, or when the remainder is not
    a bare year — so a precise approximate date like "c. 1550-03-01" does NOT
    enter the year-only compare (it stays a strict literal compare)."""
    if value is None:
        return None
    s = str(value).strip()
    m = _APPROX_YEAR_RE.match(s)
    if not m:
        return None
    remainder = s[m.end():].strip()
    if _BARE_YEAR_RE.match(remainder):
        return remainder
    return None


def _is_approx_year(value: str) -> bool:
    """True when `value` is an approximate-year claim — i.e. carries a leading
    approximation marker AND its remainder is a bare 4-digit year. Shared with
    `_strip_approx_year` so the verify path (year-equality match) and the
    contradiction-suppression path (an approximation may never CONTRADICT a
    nearby exact date) agree on exactly which claim values are 'approximate'."""
    return _strip_approx_year(value) is not None


def _normalize_date_value(value: str) -> Optional[str]:
    """Extract the year from a date-shaped value.

    Returns the 4-digit year string for inputs
    like '1998', '1998-09-04', '+1998-09-04T00:00:00Z', etc., or None for
    non-date inputs. Used by `_value_matches` to compare year-level
    claims (e.g. 'Google founded in 1994') against KB's precise dates
    (P571 = '1998-09-04T00:00:00Z'). Without normalization the literal
    string compare always fails — even when the years genuinely differ,
    the verifier returns NO_MATCH instead of CONTRADICTED, and the
    walker abstains instead of catching the falsehood.
    """
    if value is None:
        return None
    s = str(value).strip().lstrip("+")
    m = _YEAR_RE.match(s)
    if m:
        year = m.group(1).lstrip("-")
        if len(year) == 4 and year.isdigit():
            return year
    return None


# v0.16.2 E2: distinct sentinel defaults for dateutil precision detection. Parsing
# a partial date ("December 1936") against two DIFFERENT defaults reveals which
# fields the string actually specified (the ones that AGREE) vs defaulted (differ).
_DATE_DEFAULT_A = datetime(2000, 1, 1)
_DATE_DEFAULT_B = datetime(2001, 7, 23)
# Gate: only attempt date parsing on a string carrying a standalone 4-digit year
# token — so quantities ("60000000") and entity labels never get mis-parsed.
_HAS_YEAR_TOKEN = re.compile(r"(?<!\d)\d{4}(?!\d)")


def _date_parts(value) -> Optional[tuple[int, Optional[int], Optional[int]]]:
    """Parse a date-shaped value to (year, month|None, day|None) with PRECISION —
    None for components the value does not specify. Handles ISO
    ('1936-12-17[T..Z]', '1998-09', '1998'), natural language ('December 17, 1936',
    '17 December 1936', 'Dec 17 1936', 'March 2013'), and bare years. Returns None
    for non-dates (no standalone 4-digit year) or anything dateutil cannot parse —
    those are 'incomparable', never a match or a mismatch."""
    if value is None:
        return None
    s = str(value).strip().lstrip("+")
    token = _HAS_YEAR_TOKEN.search(s)
    if not token:
        return None
    # ERA (§3.2): Wikidata serializes a BCE date with a leading '-' ('-0044-03-15'
    # = 44 BC), which dateutil silently DROPS (Python datetime has no year < 1). We
    # capture the sign here and carry it as a NEGATIVE year so a BCE date never
    # collides with the same-magnitude CE date ('-1200' vs '1200' are ~2400 years
    # apart). Callers that build a datetime (_date_bounds) reject the negative year.
    is_bce = s.startswith("-")
    try:
        a = _du_parser.parse(s, default=_DATE_DEFAULT_A)
        b = _du_parser.parse(s, default=_DATE_DEFAULT_B)
    except (ValueError, OverflowError, TypeError):
        return None
    if a.year != b.year:
        return None  # the year was itself defaulted → not actually in the string
    month = a.month if a.month == b.month else None
    day = a.day if (a.day == b.day and month is not None) else None
    year = a.year
    # dateutil applies 2-digit-year expansion to a BARE sub-100 4-digit token
    # ('0079' → 1979, '0044' → 2044). The literal 4-digit token is unambiguous, so
    # trust it whenever dateutil's year disagrees — a zero-padded ancient year then
    # never mis-parses into the wrong year (which would otherwise drive a spurious
    # year 'mismatch'). Full ISO dates ('0079-03-15') parse correctly and agree.
    token_year = int(token.group())
    if year != token_year:
        year = token_year
    return (-year if is_bce else year, month, day)


def _date_precision(parts: tuple[int, Optional[int], Optional[int]]) -> int:
    """3 = day, 2 = month, 1 = year."""
    _, month, day = parts
    return 3 if day is not None else (2 if month is not None else 1)


def _date_relation(claim_value, kb_value) -> Optional[str]:
    """Precision-aware comparison of a claim date vs a KB date.

    Returns 'match' / 'mismatch' / None (incomparable).

    SOUNDNESS (§3.2) — we do NOT capture Wikidata's `wikibase:timePrecision`, and
    Wikidata stores a year-precision date as a Jan-1 placeholder and a
    month-precision date as a day-1 placeholder. So a KB value's month/day cannot
    be trusted as real. The conservative rule:
      - 'mismatch' (contradiction-eligible) ONLY when the YEARS differ — years are
        never placeholders in Wikidata, so a year disagreement is always sound
        ('1994' vs KB '1998-…').
      - 'match' when the claim is no FINER than the KB's TRUSTWORTHY precision and
        agrees at every precision the claim asserts ('December 17, 1936' vs KB
        '1936-12-17'; '1998' vs '1998-09-04').
      - None (abstain) otherwise — a month/day DIFFERENCE (could be a placeholder),
        a claim finer than the KB, or a non-date. Never contradict on a month/day
        difference. (Day-level contradiction would need the timePrecision field —
        deferred; abstaining is the §3.2-safe choice.)

    PLACEHOLDER ASYMMETRY (§3.2): because we infer KB precision from the string and
    Wikidata writes a year-precision date as YYYY-01-01 and a month-precision date
    as YYYY-MM-01, a KB day of 1 (and month of 1) may be a placeholder rather than a
    real value. VERIFY is the dangerous direction, so we treat such masked
    components as UNASSERTED — capping the KB's *effective* precision DOWN — and a
    claim finer than that abstains. (A claim of exactly 'January 1' against a
    year-placeholder must NOT verify.) A real day-precise KB date (day != 1, e.g.
    '1936-12-17') is unaffected.
    """
    c = _date_parts(claim_value)
    k = _date_parts(kb_value)
    if c is None or k is None:
        return None
    cy, cm, cd = c
    ky, km, kd = k
    if cy != ky:
        return "mismatch"
    cp, kp = _date_precision(c), _date_precision(k)
    # Cap the KB's effective precision down over placeholder-coincident components.
    if kd == 1:
        kp = min(kp, 2)              # day of 1 may be a month-precision placeholder
        if km == 1:
            kp = min(kp, 1)          # …and Jan may be a year-precision placeholder
    if cp > kp:
        return None  # claim finer than the KB asserts → can't confirm → abstain
    # Claim no finer than the KB. Agreement at every precision the claim asserts is
    # a match; any disagreement is incomparable (month/day could be a placeholder).
    if cp >= 2 and cm != km:
        return None
    if cp >= 3 and cd != kd:
        return None
    return "match"


def _date_bounds(value) -> Optional[tuple[datetime, datetime]]:
    """The (earliest, latest) instant a date string could denote, given its
    apparent precision. Year-only '2013' spans 2013-01-01 .. 2013-12-31; month
    '2013-05' spans the 1st .. last day; a full day is that day. Used for
    precision-aware ordering against `now`. Returns None when unparseable."""
    parts = _date_parts(value)
    if parts is None:
        return None
    y, m, d = parts
    if y < 1:
        return None  # BCE / year 0 — Python datetime cannot represent it; treat the
        # currency ordering as unknown (callers fail safe to abstain / no-suppress).
    lo = datetime(y, m or 1, d or 1)
    hi_month = m or 12
    if d is not None:
        hi_day = d
    elif m is not None:
        hi_day = calendar.monthrange(y, m)[1]
    else:
        hi_day = 31
    hi = datetime(y, hi_month, hi_day, 23, 59, 59)
    return lo, hi


def _end_provably_past(end_value, ref_iso) -> bool:
    """True iff an end date is UNAMBIGUOUSLY before `ref_iso` — its LATEST possible
    instant precedes the reference's earliest. A year-precision end ('2025') is
    provably past only once the whole year has elapsed. Conservative: an
    unparseable or same-period end returns False (we cannot prove it ended).
    This is the strict gate for the E4 level-2 CONTRADICTION."""
    eb = _date_bounds(end_value)
    rb = _date_bounds(ref_iso)
    if eb is None or rb is None:
        return False
    return eb[1] < rb[0]


def _end_provably_future(end_value, ref_iso) -> bool:
    """True iff an end date is UNAMBIGUOUSLY after `ref_iso` — its EARLIEST possible
    instant follows the reference's latest. A statement with a provably-future end
    is still current, so it may verify a present-tense claim. Anything NOT provably
    future (past, or an ambiguous same-period end) is treated as non-current by the
    E4 level-1 scope check (abstain is safe)."""
    eb = _date_bounds(end_value)
    rb = _date_bounds(ref_iso)
    if eb is None or rb is None:
        return False
    return eb[0] > rb[1]


def _start_provably_future(start_value, ref_iso) -> bool:
    """True iff a statement's START is UNAMBIGUOUSLY after `ref_iso` — its EARLIEST
    possible instant follows the reference's latest. The start-side dual of
    `_end_provably_future`: a statement whose start has not yet arrived describes a
    role/term NOT YET BEGUN (an announced succession, a scheduled term, a future
    contract), so it is not a realized fact and must not verify a present or past
    claim. Conservative: a past or ambiguous same-period start is NOT provably
    future, so a genuinely-ongoing statement (past start, no end) still verifies."""
    sb = _date_bounds(start_value)
    rb = _date_bounds(ref_iso)
    if sb is None or rb is None:
        return False
    return sb[0] > rb[1]


def _claim_reaches_present(claim) -> bool:
    """True iff the claim asserts validity up to NOW — no upper temporal bound of
    any kind. A present-tense claim extracts to a bare scope (valid_until None); a
    PAST claim carries `valid_until == BEFORE_PRESENT` (so it does NOT reach the
    present) and a `before <event>` upper bound sets valid_until_ref. Used by the
    E4 level-1 scope gate: such a claim cannot be satisfied by an ended statement."""
    return not claim.valid_until and not getattr(claim, "valid_until_ref", None)


def _claim_present_unscoped(claim) -> bool:
    """The STRICT present-tense signal for the E4 level-2 contradiction: a fully
    unscoped claim (no valid_from/until/refs at all). The extractor emits exactly
    this for a bare present-tense assertion ('X is the pope'); any explicit scope
    (a date, 'since <year>', a 'was'/past marker → BEFORE_PRESENT, a reference
    bound) disqualifies it, so a historical 'X was the pope' can never be
    contradicted as if it were a present-currency claim."""
    return (
        not claim.valid_from
        and not claim.valid_until
        and not getattr(claim, "valid_from_ref", None)
        and not getattr(claim, "valid_until_ref", None)
        and not getattr(claim, "valid_during_ref", None)
    )


def _value_matches(kb_value, claim_object: str) -> bool:
    """Loose equality: Q-number / case-insensitive string match, or a
    precision-aware date match (a claim date VERIFIES only when the KB date is at
    least as precise and agrees — 'December 17, 1936' matches KB '1936-12-17';
    '1998' matches '1998-09-04'; a day-precise claim is NOT verified by a year-only
    KB value). Natural-language dates are parsed (E2)."""
    if kb_value is None:
        return False
    kb_str = str(kb_value).strip()
    claim_str = claim_object.strip()
    if kb_str.lower() == claim_str.lower():
        return True
    # v0.16.1 WS1: an approximate-year claim ("c. 1550") strips its leading marker
    # on the CLAIM side only, yielding the bare year — it matches ONLY on exact
    # year equality (never a fuzzy window), so it can never false-verify.
    approx_year = _strip_approx_year(claim_str)
    claim_for_date = approx_year if approx_year is not None else claim_str
    return _date_relation(claim_for_date, kb_str) == "match"


def _scope_compatible(
    stmt: Statement, claim: Claim, current_time: str, object_type: str = "entity"
) -> bool:
    """
    Return True if the statement's qualifier scope is compatible with the claim's temporal scope.
    If statement has no P580/P582 qualifiers, it is assumed always-valid.
    If claim has no scope, any statement is compatible.
    """
    stmt_from = stmt.qualifiers.get("P580")
    stmt_until = stmt.qualifiers.get("P582")

    # No qualifier on statement → always valid
    if not stmt_from and not stmt_until:
        return True

    # E4 level 1 (§3.2): temporal CURRENCY (a role/membership/office that begins and
    # ends) is meaningful only for ENTITY-valued role/state predicates. For a date-
    # or quantity-typed predicate, a stray P580/P582 is not a "term" and must not
    # suppress a value match (a birth date does not "end"). So the currency gates
    # below run ONLY for object_type == "entity"; the pre-existing explicit-scope
    # checks (which fire only when the CLAIM carries an explicit bound) are universal.
    if object_type == "entity":
        # END side: a claim asserting present currency (no upper bound — a bare
        # present-tense claim, or "since <year>") CANNOT be satisfied by a statement
        # whose end is NOT provably in the future (P582 exists only on ENDED facts).
        # A PAST claim carries valid_until == BEFORE_PRESENT and does NOT reach the
        # present, so it still verifies off the ended statement ("X was the pope").
        if stmt_until and _claim_reaches_present(claim):
            if not _end_provably_future(stmt_until, current_time):
                return False
        # START side (dual): a role/term whose start is provably in the FUTURE has
        # not begun (an announced succession, a scheduled term), so it is not a
        # realized fact and cannot verify a present OR past claim. A genuinely-
        # ongoing statement (past start, no end) is NOT provably future → still
        # verifies. An explicitly future-scoped claim is out of scope here.
        if (
            stmt_from
            and _start_provably_future(stmt_from, current_time)
            and (_claim_reaches_present(claim) or claim.valid_until == BEFORE_PRESENT)
        ):
            return False

    # Claim has explicit valid_from → must not precede statement start
    if claim.valid_from and stmt_from:
        if claim.valid_from < stmt_from:
            return False

    # Claim has explicit valid_until → must not exceed statement end
    if claim.valid_until and claim.valid_until != BEFORE_PRESENT and stmt_until:
        if claim.valid_until > stmt_until:
            return False

    return True
