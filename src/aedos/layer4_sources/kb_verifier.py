from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from ..layer1_extraction.extractor import Claim
from ..layer1_extraction.temporal import BEFORE_PRESENT
from ..layer3_substrate.predicate_translation import PredicateTranslation, PredicateTranslationError
from ..layer3_substrate.resolver import EntityResolver
from .kb_protocol import KBEntityID, KBProtocol, LocalContext, Statement

_NOW = lambda: datetime.now(timezone.utc).isoformat()

# Phase 10.5 Step 6 Batch 11 (Tier A1): canonical Q-ids for the seven
# continents + the principal supercontinent groupings the medium-bar
# test set uses. Used by _disjoint_continent to confidently flag KB-
# grounded "X is in [wrong continent]" claims as CONTRADICTED rather
# than abstaining on the non-functional-predicate NO_MATCH path.
# Hand-validated against Wikidata's canonical labels:
#   Europe         Q46
#   Asia           Q48
#   Africa         Q15
#   North America  Q49
#   South America  Q18
#   Oceania        Q55643
#   Antarctica     Q51
#   Australia      Q3960   (continent; the country is Q408)
CONTINENT_QIDS: frozenset[str] = frozenset([
    "Q46", "Q48", "Q15", "Q49", "Q18", "Q55643", "Q51", "Q3960",
])

# Phase 10.5 Step 6 Batch 11 (Tier A1) + B2 fix: KB properties whose
# semantics are GEOGRAPHIC location-containment, for which the
# location-disjoint check is sound. Non-geographic relational
# predicates (employed_by P108, member_of P463, child_of P40, etc.)
# must NOT use the disjoint check — two distinct entities can both
# satisfy a multi-valued relational predicate without contradicting
# each other.
_LOCATION_KB_PROPERTIES: frozenset[str] = frozenset([
    "P131",  # located in the administrative territorial entity
    "P17",   # country
    "P30",   # continent
    "P361",  # part of (used for geographic part_of)
    "P206",  # located in body of water
    "P276",  # location
])

# Geographic-container entity types that the per-predicate object-type lists
# (e.g. located_in = [country, city, settlement, …]) historically omit. A
# claim like "Paris is in Europe" needs to resolve "Europe" to the continent
# (Q46, instance-of continent Q5107); without continent in the accepted-type
# set the resolver's D33 type filter rejects Q46 and lands on a non-continent
# homonym, so the containment subsumption (France ⊂ Europe via P30) can never
# match. `_object_resolution_types` widens the object's type filter with these
# only for geographic-location predicates (kb_property in
# _LOCATION_KB_PROPERTIES). Continents are a closed 7-member set, so admitting
# them cannot over-broaden resolution. (Region Q82794 is deliberately NOT
# included — it is open-ended and the cases that would need it also need the
# trimmed P361 subsumption path; see Run-7 follow-up notes.)
_GEO_CONTAINER_TYPES: frozenset[str] = frozenset([
    "Q5107",  # continent
])


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
        exception_cache=None,
    ) -> None:
        self._kb = kb_protocol
        self._resolver = entity_resolver
        self._pt = predicate_translation
        # v0.16 WS1: the bounded NOGOOD cache (substrate_exceptions). It is
        # OPTIONAL — the SubstrateExceptionCache itself is WS3, so the consult
        # below is a guarded no-op until that lands. When present and exposing
        # a `vetoes(predicate, property_path, subject_qid)` predicate, the
        # binding loop skips a binding the nogood vetoes.
        self._exception_cache = exception_cache

    def verify(
        self,
        claim: Claim,
        current_time: Optional[str] = None,
        source_text: Optional[str] = None,
    ) -> KBVerdict:
        """Full KB verification: translate → map slots → resolve → lookup → compare.

        Honors claim polarity (C1): a negated claim inverts the KB's positive-
        content verdict. Resolves the value entity, not just the lookup
        subject (M4), and only treats a value mismatch as a contradiction for
        functional (single_valued) predicates (M4).

        Honors the slot_to_qualifier lookup direction (D19). For a standard
        predicate the KB statement is keyed on the claim's subject; for an
        inverse predicate (capital_of on P36, mother_of on P25 — whose seed maps
        the Aedos subject to ``statement_value``) the statement is keyed on the
        claim's *object*, so the lookup and the expected value are swapped.
        ``_lookup_targets`` decides the direction. The trace records it as
        ``lookup_inverted``; the other trace fields use direction-neutral names
        for the KB *statement* positions — ``entity`` is the statement subject,
        ``value_entity`` / ``value_resolved`` describe the statement value, and
        the abstention reasons are ``lookup_subject_unresolved`` /
        ``value_unresolved`` (R2).
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

            # v0.16 WS1: NOGOOD gate. Consult the optional exception cache for
            # a cached "binding does not hold here" before this binding can
            # drive a verdict. Guarded no-op until WS3 lands the cache.
            if self._binding_vetoed(claim, binding):
                bindings_tried.append(
                    {
                        "property": binding.kb_property,
                        "source": binding.source,
                        "verdict": KBVerdictType.NO_MATCH.value,
                        "abstention_reason": "nogood_veto",
                    }
                )
                continue

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

    def _binding_vetoed(self, claim: Claim, binding) -> bool:
        """v0.16 WS1 NOGOOD gate. Consult the optional SubstrateExceptionCache
        (WS3) for a cached "this binding does not hold for this subject". Until
        WS3 lands the cache this is a guarded no-op: an absent cache, or a cache
        without a `vetoes` predicate, never vetoes. Fails open (any error →
        not vetoed) — a flaky cache must never suppress a sound verdict."""
        cache = self._exception_cache
        if cache is None:
            return False
        vetoes = getattr(cache, "vetoes", None)
        if vetoes is None:
            return False
        try:
            return bool(
                vetoes(claim.predicate, binding.kb_property, claim.subject)
            )
        except Exception:
            return False

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
        # Step 2: map the claim's slots onto KB statement positions (D19). An
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
        # Phase G D33: pass entity types for the Aedos slot being resolved;
        # the wikidata adapter post-filters candidates by P31 ∩ types.
        # Phase H D47: thread the source text + immediate-claim context to
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
        # v0.16 WS3 D13: capture the entity_resolution_cache row id the
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
        # looked-up statement values (M4's object resolution, now applied to
        # whichever Aedos slot is the KB statement value). Falls back to the raw
        # string for literal comparison.
        expected_value = expected_ref
        value_resolved = False
        if meta.object_type == "entity":
            # (geo fix) For a geographic-location predicate, widen the object's
            # accepted-type filter to admit continents — the per-predicate type
            # lists omit them, which otherwise blocks "X is in Europe" from
            # resolving "Europe" to the continent (see _GEO_CONTAINER_TYPES).
            # Only widens an existing non-empty filter; an open (None/empty)
            # filter is left open.
            value_types = _types_for_slot(binding, value_slot)
            if value_types and binding.kb_property in _LOCATION_KB_PROPERTIES:
                value_types = list(dict.fromkeys([*value_types, *_GEO_CONTAINER_TYPES]))
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
            # Phase 10.5 Step 6 Tier A3: no-statements subsumption fallback.
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

        # Step 7: apply claim polarity (C1). A negated claim asserts the triple
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
        # When the verdict is an abstention (NO_MATCH), record *why* — Phase 10.5
        # debugging needs to tell a resolution failure apart from a genuine
        # absence of evidence (N1).
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
        for stmt in statements:
            if not _scope_compatible(stmt, claim, current_time):
                continue
            if _value_matches(stmt.value, expected_value):
                return KBVerdictType.VERIFIED, stmt, None
            if scope_mismatch is None:
                scope_mismatch = stmt

        value_unresolved = meta.object_type == "entity" and not value_resolved

        # Phase 10.5 Step 5 root-cause: try subsumption upgrade on any
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
            # S3 generalization: never contradict on a type-mismatched mapping.
            # If the looked-up statement's datatype is incompatible with the
            # predicate's object_type, the predicate is likely mis-mapped to the
            # wrong KB property; abstain rather than fabricate a contradiction.
            if not _contradiction_value_type_ok(
                getattr(scope_mismatch, "value_type", None), meta.object_type
            ):
                return KBVerdictType.NO_MATCH, None, "value_type_object_type_mismatch"
            return KBVerdictType.CONTRADICTED, scope_mismatch, None

        # Phase 10.5 Step 6 Batch 11 (Tier A1): conservative DISJOINT
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
            and binding.kb_property in _LOCATION_KB_PROPERTIES
            and self._location_disjoint(scope_mismatch.value, expected_value)
        ):
            return KBVerdictType.CONTRADICTED, scope_mismatch, None

        reason = "value_unresolved" if value_unresolved else "no_matching_statement"
        return KBVerdictType.NO_MATCH, None, reason

    def _location_disjoint(self, kb_value: str, expected_value: str) -> bool:
        """Phase 10.5 Step 6 Batch 11 (Tier A1): True when KB confirms
        the KB statement value is geographically disjoint from the
        claim's expected value.

        Two paths, both requiring positive KB evidence (a continent
        ancestor confirmed by subsumption):

        (a) Continent-level. expected_value is itself a known continent
            (CONTINENT_QIDS) and the KB value is subsumed by a DIFFERENT
            continent. Direct evidence of disjoint continent.
            Targets "Thames in Asia" / "Vatican in Africa".

        (b) Shared-continent sub-region. Both values are subsumed by the
            SAME continent AND subsumption is `unrelated` in both
            directions between them. Two sub-regions sharing a continent
            ancestor with no mutual containment are structurally disjoint
            within that continent (Italy and Germany are both in Europe;
            neither contains the other; therefore they're disjoint
            countries). Targets "Rome in Germany" — KB returns Rome's
            P131 = Lazio (a sub-region of Italy), Italy's continent is
            Europe, Germany's continent is Europe, and Lazio is unrelated
            to Germany in both subsumption directions.

        Fails closed on error: any uncertainty preserves NO_MATCH
        (abstain). §3.2 soundness-over-completeness.
        """
        if not isinstance(kb_value, str) or not isinstance(expected_value, str):
            return False
        if kb_value == expected_value:
            return False

        # (a) Continent-level path
        if expected_value in CONTINENT_QIDS:
            for continent in CONTINENT_QIDS:
                if continent == expected_value:
                    continue
                try:
                    r = self._kb.subsumption(kb_value, continent, "part_of")
                except Exception:
                    continue
                if r.verdict in ("a_subsumed_by_b", "equivalent"):
                    return True
            return False

        # (b) Shared-continent sub-region path
        for continent in CONTINENT_QIDS:
            try:
                kb_in = self._kb.subsumption(kb_value, continent, "part_of").verdict
                exp_in = self._kb.subsumption(expected_value, continent, "part_of").verdict
            except Exception:
                continue
            kb_in_ok = kb_in in ("a_subsumed_by_b", "equivalent")
            exp_in_ok = exp_in in ("a_subsumed_by_b", "equivalent")
            if not (kb_in_ok and exp_in_ok):
                continue
            # Both confirmed in the same continent; check mutual non-containment
            try:
                fwd = self._kb.subsumption(kb_value, expected_value, "part_of").verdict
                rev = self._kb.subsumption(expected_value, kb_value, "part_of").verdict
            except Exception:
                return False
            return fwd == "unrelated" and rev == "unrelated"
        return False

    def _subsumption_upgrades(self, kb_value: str, expected_value: str) -> bool:
        """Phase 10.5 Step 5 root-cause helper: query the KB for whether the
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
    """Map a claim's slots onto KB statement positions via slot_to_qualifier (D19).

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
    pre-D19 default, preserved so every non-inverse predicate behaves exactly as
    before and inline-generated rows without an explicit map keep working.

    Returns ``None`` for a ``slot_to_qualifier`` the verifier cannot interpret
    (a qualifier-keyed or contradictory subject/object map). ``verify`` turns
    that into a ``NO_KB_PATH`` abstention with a trace note — it never guesses a
    direction and never crashes. The v0.15 seed pack has no such map (verified
    in ``docs/v0.15_build_log/fixup3_scope.md``); this branch guards only against
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
    """Phase G D33: pick the entity-types list that corresponds to the Aedos
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
    """Apply claim polarity to a positive-content verdict (C1).

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


# Phase 10.5 Step 6 generalization (S3): which KB statement value-types may
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


def _normalize_date_value(value: str) -> Optional[str]:
    """Phase 10.5 Step 6 sub-cause C / Pattern C fix: extract the year
    from a date-shaped value. Returns the 4-digit year string for inputs
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


def _value_matches(kb_value, claim_object: str) -> bool:
    """Loose equality: Q-number match, case-insensitive string match, or
    date-year match. Phase 10.5 Step 6: year-aware date comparison —
    when both values normalize to a 4-digit year, compare years rather
    than literal strings ('1998' vs '1998-09-04T00:00:00Z' should match
    when the claim only specifies the year)."""
    if kb_value is None:
        return False
    kb_str = str(kb_value).strip()
    claim_str = claim_object.strip()
    if kb_str.lower() == claim_str.lower():
        return True
    # Date-year normalized comparison: only fire when the claim looks
    # like a bare year (4 digits) — that's the common pattern in the
    # medium-bar's "founded in YYYY", "born in YYYY", "occurred in YYYY".
    # Comparing two full ISO timestamps via year-only would be incorrect
    # (1998-09-04 ≠ 1998-01-01 for precise temporal claims).
    if _BARE_YEAR_RE.match(claim_str):
        kb_year = _normalize_date_value(kb_str)
        if kb_year == claim_str.lstrip("+").lstrip("-"):
            return True
    return False


def _scope_compatible(stmt: Statement, claim: Claim, current_time: str) -> bool:
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

    # Claim has explicit valid_from → must not precede statement start
    if claim.valid_from and stmt_from:
        if claim.valid_from < stmt_from:
            return False

    # Claim has explicit valid_until → must not exceed statement end
    if claim.valid_until and claim.valid_until != BEFORE_PRESENT and stmt_until:
        if claim.valid_until > stmt_until:
            return False

    return True
