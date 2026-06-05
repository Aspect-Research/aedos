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
        # Aggregate the walker's directed-over-enumerate signals across ALL bindings
        # (OR), so the final NO_MATCH trace reflects "any binding had a known value"
        # rather than just the LAST binding's — otherwise a later no-match binding
        # would clobber the P19 binding's signal and the walker would re-fan-out.
        agg_value_known_entity = False
        agg_functional_value_known = False

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
                    "found_values": outcome.trace.get("found_values"),
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
            agg_value_known_entity = agg_value_known_entity or bool(
                outcome.trace.get("value_known_entity")
            )
            agg_functional_value_known = agg_functional_value_known or bool(
                outcome.trace.get("functional_value_known")
            )

        # METADATA-derived directed-over-enumerate signal, INDEPENDENT of whether
        # statements were found. A FUNCTIONAL ENTITY predicate (entity object + a
        # single_valued binding) has exactly one KB grounding path — the directed
        # subsumption upgrade (value ⊆ object) inside _compare_positive — so the
        # walker's neighbor ENUMERATION (descending the object's part_of children /
        # ascending the subject's classes) is futile and can be skipped. Unlike the
        # value_known_entity / functional_value_known aggregates above (which are
        # `bool(statements) and ...`, so they vanish on the NO_MATCH paths that never
        # looked up statements — subject_resolution_failed, no_statements), this is
        # read off binding metadata, so it is present on EVERY abstain trace. That is
        # the fix for the live "Obama born_in Kenya" fanout: "Obama" resolved
        # ambiguously / carried no P19, so statements were empty and the
        # statements-based signals were False — yet the predicate is still provably a
        # functional entity predicate, so the enumeration is still futile.
        # Abstain-only: _discover_chains only runs AFTER the direct verify abstained,
        # so skipping enumeration can never create or alter a verify/contradict (§3.2);
        # the one true case ("born_in USA") verifies at the direct lookup first.
        # `all` (not `any`): only skip when EVERY KB binding agrees the predicate is
        # functional — over-abstention is the disease to cure, so a predicate with a
        # mixed binding set (one functional property + a non-functional one that may
        # legitimately ground via enumeration) keeps its enumeration. `had_kb_path`
        # guards the vacuous-true empty case (no kb binding → handled as NO_KB_PATH
        # above, never reaches the signal consumers).
        functional_entity_predicate = (
            meta.object_type == "entity"
            and had_kb_path
            and all(bool(b.single_valued) for b in meta.bindings if b.kb_property)
        )

        # No binding carried an actual KB property — identical to the pre-v0.16
        # `not meta.kb_property` abstention.
        if not had_kb_path:
            return KBVerdict(verdict=KBVerdictType.NO_KB_PATH, trace={"reason": "not_kb_resolvable"})

        # Arbitration order: a positive grounding wins (Decision 1); else a
        # sound single_valued contradiction; else the last abstention.
        if verified_outcome is not None:
            chosen, trace = verified_outcome
            trace["bindings_tried"] = bindings_tried
            # Surface the predicate-level metadata signal on grounded verdicts too
            # (it is a property of the predicate, valid regardless of verdict) so the
            # observability record carries it uniformly. Read-only; no verdict path
            # consumes it on the grounded branch.
            trace["functional_entity_predicate"] = functional_entity_predicate
            return KBVerdict(
                verdict=chosen.verdict,
                matched_statement=chosen.matched_statement,
                subject_kb_id=chosen.subject_kb_id,
                trace=trace,
            )
        if contradicted_outcome is not None:
            chosen, trace = contradicted_outcome
            trace["bindings_tried"] = bindings_tried
            trace["functional_entity_predicate"] = functional_entity_predicate
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
            # Use the cross-binding aggregate, not just this (last) binding's value,
            # so the walker's directed-over-enumerate skip sees a known value found
            # by ANY binding.
            trace["value_known_entity"] = agg_value_known_entity
            trace["functional_value_known"] = agg_functional_value_known
            # The metadata signal is present even when statements were never found —
            # this is what gates the skip for the live born_in (statements empty) case.
            trace["functional_entity_predicate"] = functional_entity_predicate
            return KBVerdict(
                verdict=last_no_match.verdict,
                matched_statement=last_no_match.matched_statement,
                subject_kb_id=last_no_match.subject_kb_id,
                trace=trace,
            )
        # Defensive: had a kb_property but produced no outcome (all vetoed).
        return KBVerdict(
            verdict=KBVerdictType.NO_MATCH,
            trace={
                "reason": "no_binding_grounded",
                "bindings_tried": bindings_tried,
                "functional_entity_predicate": functional_entity_predicate,
            },
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

        # v0.16.3 Defect-2 (observability): map the lookup/value resolutions back
        # onto the claim's AEDOS slots, so the durable audit labels resolved QIDs
        # by claim subject/object — never by KB statement position. Under an
        # INVERSE binding the KB lookup entity is the claim's OBJECT and the
        # expected value is the claim's SUBJECT, so a position-keyed stamp (the
        # pre-fix `subject_kb_id` → resolved_subject_qid) mislabels the slots.
        # `_slot_trace` reads these locals at return time and merges the
        # direction-correct slot facts into EVERY returned trace (incl. abstain),
        # so the walker can stamp the right slot regardless of inversion. The KB
        # statement-position fields (`entity`/`value_entity`/`subject_kb_id`) are
        # left untouched — they correctly describe the statement, which the
        # premise/grounding edges key on. §3.2-neutral: no verdict path reads
        # these keys (only verification_store + the trace renderer do).
        # `value_cache_row_id` / `resolved_value_qid_slot` initialize to None so
        # the early subject-resolution-failed return (value not yet resolved)
        # still produces a well-formed slot trace.
        value_cache_row_id: Optional[int] = None
        resolved_value_qid_slot: Optional[KBEntityID] = None

        def _slot_trace(t: dict) -> dict:
            if lookup_inverted:
                t["aedos_subject_qid"] = resolved_value_qid_slot
                t["aedos_object_qid"] = lookup_subject_id
                t["aedos_subject_cache_row_id"] = value_cache_row_id
                t["aedos_object_cache_row_id"] = resolution_cache_row_id
            else:
                t["aedos_subject_qid"] = lookup_subject_id
                t["aedos_object_qid"] = resolved_value_qid_slot
                t["aedos_subject_cache_row_id"] = resolution_cache_row_id
                t["aedos_object_cache_row_id"] = value_cache_row_id
            return t

        if lookup_subject_id is None:
            return KBVerdict(
                verdict=KBVerdictType.NO_MATCH,
                trace=_slot_trace({
                    "reason": "subject_resolution_failed",
                    "reference": lookup_ref,
                    "abstention_reason": "lookup_subject_unresolved",
                    "lookup_inverted": lookup_inverted,
                    "resolution_cache_row_id": resolution_cache_row_id,
                }),
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
            # v0.16.3 Defect-2: capture the value resolution's cache row + QID for
            # the AEDOS-slot stamp, immediately after the resolve (the resolver's
            # request-scoped last_cache_row_id reflects THIS resolve now). Only the
            # successfully-resolved QID becomes a slot fact — an unresolved value
            # leaves resolved_value_qid_slot None, so the abstain stamp is honest.
            _v_last_row_id = getattr(self._resolver, "last_cache_row_id", None)
            value_cache_row_id = _v_last_row_id() if callable(_v_last_row_id) else None
            if resolved_value is not None:
                expected_value = resolved_value
                value_resolved = True
                resolved_value_qid_slot = resolved_value

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
                    trace=_slot_trace({
                        "entity": lookup_subject_id,
                        "value_entity": expected_value,
                        "value_resolved": True,
                        "polarity": claim.polarity,
                        "positive_verdict": pos_verdict.value,
                        "lookup_inverted": lookup_inverted,
                        "no_statements_subsumption_fallback": True,
                        "resolution_cache_row_id": resolution_cache_row_id,
                    }),
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
                    trace=_slot_trace({
                        "entity": lookup_subject_id,
                        "value_entity": expected_value,
                        "value_resolved": True,
                        "polarity": claim.polarity,
                        "positive_verdict": pos_verdict.value,
                        "lookup_inverted": lookup_inverted,
                        "no_statements_disjoint_fallback": True,
                        "resolution_cache_row_id": resolution_cache_row_id,
                    }),
                )
            # NO_MATCH is polarity-invariant — absence of evidence is not evidence.
            return KBVerdict(
                verdict=KBVerdictType.NO_MATCH,
                subject_kb_id=lookup_subject_id,
                trace=_slot_trace({
                    "reason": "no_statements_found",
                    "entity": lookup_subject_id,
                    "property": binding.kb_property,
                    "abstention_reason": "no_statements",
                    "lookup_inverted": lookup_inverted,
                    "resolution_cache_row_id": resolution_cache_row_id,
                }),
            )

        # Step 6: verdict for the claim's *positive* content (polarity-agnostic).
        # _compare_positive is direction-agnostic — it compares the expected
        # value against the statement values regardless of which Aedos slot the
        # expected value came from.
        pos_verdict, statement, abstention_reason = self._compare_positive(
            statements, claim, expected_value, value_resolved, meta, binding, current_time
        )

        # v0.16.4 present-fact-with-too-early-start fallback. _compare_positive
        # abstains when a value-matching statement is scope-incompatible — including
        # the case where the entity CURRENTLY holds the claimed value but the
        # claim's lower temporal bound precedes the value's actual start ("X is
        # president since 2022" vs a KB term that began in 2024). The COMPOUND claim
        # (value + since-date) isn't grounded, but the PRESENT base fact IS. Surface
        # VERIFIED for the present fact and flag `temporal_scope_unconfirmed`, so
        # composition asserts the current fact and drops/flags the unconfirmed date
        # rather than refusing an answerable question. SOUND: gated to
        # PRESENT-reaching entity claims (a past-scoped "X was president in 1990" is
        # never rescued by a current statement), and the composition never asserts
        # the claimed too-early date.
        temporal_scope_unconfirmed = False
        if pos_verdict == KBVerdictType.NO_MATCH and value_resolved:
            early = _present_value_match_too_early(
                statements, claim, expected_value, current_time, meta.object_type
            )
            if early is not None:
                pos_verdict = KBVerdictType.VERIFIED
                statement = early
                abstention_reason = None
                temporal_scope_unconfirmed = True

        # RESOLVED-ENTITY NAME-MATCH (§3.2): a single_valued ENTITY contradiction
        # can be a famous-entity QID tangle, not a real conflict. The value surface
        # form ("Tokyo") resolved to a DIFFERENT same-named QID (e.g. the special-
        # wards "Tokyo") than the one the KB statement holds (Q1490, the metropolis
        # that Japan's P36 points to) — typically because the value-type filter
        # excluded the KB's QID (Q1490 is typed prefecture/metropolis, not "city").
        # If the contradicting KB value is itself NAMED by the claim's value surface
        # form, the claim refers to that very entity, so the statement VERIFIES the
        # claim rather than contradicting it. Fires only for a resolved entity claim
        # whose KB value's canonical label equals the surface form; a genuine name
        # mismatch (Honolulu vs New York City) does not match and is unaffected, and
        # a label-fetch failure leaves the verdict as-is (fail-closed). This flips
        # BEFORE the copula value-type gate below so the now-VERIFIED verdict is not
        # re-evaluated as a contradiction.
        entity_name_match = False
        if (
            pos_verdict == KBVerdictType.CONTRADICTED
            and meta.object_type == "entity"
            and value_resolved
            and statement is not None
            # Only a VALUE-MISMATCH contradiction (the QID tangle) may be rescued by
            # a name match. A contradiction where the KB value ALREADY value-matched
            # the claim is not about the value — it is the E4 temporal-currency
            # contradiction (a present-tense role whose matching statement has
            # ENDED, e.g. "Obama is the President" / "Francis is the pope"). The KB
            # value is the very entity the claim names there too, so a name match
            # would wrongly erase the sound "role ended" contradiction and
            # FALSE-VERIFY. Excluding matched-value contradictions (this check runs
            # BEFORE the label fetch, so it also short-circuits that work) keeps the
            # E4 wrong-pope / ended-role catch intact.
            and not _value_matches(getattr(statement, "value", None), expected_value)
            and self._value_surface_names_kb_entity(
                expected_ref, getattr(statement, "value", None)
            )
        ):
            pos_verdict = KBVerdictType.VERIFIED
            abstention_reason = None
            entity_name_match = True

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
                    trace=_slot_trace({
                        "entity": lookup_subject_id,
                        "property": binding.kb_property,
                        "value_entity": expected_value,
                        "value_resolved": value_resolved,
                        "polarity": claim.polarity,
                        "lookup_inverted": lookup_inverted,
                        "abstention_reason": "value_type_incompatible_binding",
                        "resolution_cache_row_id": resolution_cache_row_id,
                    }),
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
                    trace=_slot_trace({
                        "entity": lookup_subject_id,
                        "property": binding.kb_property,
                        "value_entity": expected_value,
                        "value_resolved": value_resolved,
                        "polarity": claim.polarity,
                        "lookup_inverted": lookup_inverted,
                        "abstention_reason": "value_type_unconfirmed_positive_gate",
                        "resolution_cache_row_id": resolution_cache_row_id,
                    }),
                )

        # v0.16.4 unconfirmed-start on the VERIFIED path. A present-reaching claim
        # that asserts a START ("since <date>") but value-matched a statement with
        # NO start qualifier (P580) is grounded for the PRESENT fact only — the KB
        # holds the value but records no start, so it cannot confirm the claimed
        # since-date. Flag it `temporal_scope_unconfirmed` (composition asserts the
        # present fact and DROPS the date) instead of presenting an unverifiable
        # date as verified. A statement that DOES carry a P580 reaching here passed
        # _scope_compatible, i.e. its start is at/before the claimed date — which
        # positively confirms "since <date>" — so it is NOT flagged. (The too-LATE
        # P580 case is the NO_MATCH fallback above.)
        if (
            pos_verdict == KBVerdictType.VERIFIED
            and not temporal_scope_unconfirmed
            and statement is not None
            and _present_start_unconfirmed(claim, statement, meta.object_type)
        ):
            temporal_scope_unconfirmed = True

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
            # The walker's directed-over-enumerate signal: True when this is a
            # FUNCTIONAL entity predicate whose subject value(s) are KNOWN (the
            # lookup found statements). On a NO_MATCH, the directed subsumption
            # upgrade above already tested every held value against the claim, so
            # neighbor ENUMERATION cannot ground the claim — the walker skips the
            # fanout. (Set regardless of verdict; only read on the NO_MATCH path.)
            "functional_value_known": bool(statements)
            and bool(binding.single_valued)
            and meta.object_type == "entity",
            # The DIRECTED-part_of signal, INDEPENDENT of single_valued: an entity
            # predicate whose subject value(s) are KNOWN. The all-values part_of
            # subsumption upgrade above already tested every held value's geographic
            # containment against the claim object, so the walker can skip the
            # part_of neighbor enumeration (the "descend into Kenya's P17 children"
            # fanout) even when the (often cold-started) predicate was not marked
            # single_valued — e.g. the extractor's "was born in" vs the seed's
            # born_in. (is_a enumeration is still gated on functional_value_known,
            # since a non-functional predicate may legitimately ground via is_a.)
            "value_known_entity": bool(statements) and meta.object_type == "entity",
            # v0.16.x observability: the KB statement values examined for this
            # property, so a NO_MATCH can show WHAT was found vs the claimed value
            # ("the KB lists [v1, v2]; none matched"). Observability-only; the raw
            # value (a QID or literal) coerced to str for JSON.
            "found_values": [
                str(getattr(s, "value", ""))
                for s in (statements or [])
                if getattr(s, "value", None) is not None
            ],
        }
        # When the verdict is an abstention (NO_MATCH), record *why* —
        # debugging needs to tell a resolution failure apart from a genuine
        # absence of evidence.
        if abstention_reason is not None:
            trace["abstention_reason"] = abstention_reason
        if entity_name_match:
            # Observability: this VERIFIED came from the resolved-entity name-match
            # (the KB value is the same-named entity the claim refers to), not a
            # direct Q-id equality in the value-match loop.
            trace["entity_name_match"] = True
        if temporal_scope_unconfirmed:
            # v0.16.4: the present base fact is verified, but the claim's lower
            # temporal bound (its "since <date>") precedes the value's actual start
            # and could NOT be confirmed. Composition asserts the present fact and
            # drops/flags the date; it never asserts the claimed date.
            trace["temporal_scope_unconfirmed"] = True
        _slot_trace(trace)

        return KBVerdict(
            verdict=final_verdict,
            matched_statement=statement,
            subject_kb_id=lookup_subject_id,
            trace=trace,
        )

    def _value_surface_names_kb_entity(self, surface, kb_value) -> bool:
        """True when the claim's value SURFACE FORM names the KB statement's entity
        value — its canonical KB label equals the surface (trimmed, case-folded).

        This recognizes the famous-entity QID tangle that would otherwise drive a
        §3.2 false-contradict: the resolver selected a DIFFERENT same-named QID for
        the claim's value than the QID the KB statement holds (e.g. "Tokyo" →
        the special-wards node, while Japan's P36 is Q1490 the metropolis), so a
        Q-id-equality compare reports a spurious mismatch even though the KB value
        IS what the claim names.

        Fails CLOSED (returns False) on a missing surface form, a non-entity KB
        value, an adapter without `fetch_label`, an empty label, or any fetch
        error — leaving the existing verdict untouched. Never raises."""
        if not surface or not isinstance(kb_value, str):
            return False
        if _QID_RE.fullmatch(kb_value.strip()) is None:
            return False  # KB value is not an entity Q-id → not the tangle case
        fetch = getattr(self._kb, "fetch_label", None)
        if not callable(fetch):
            return False
        try:
            label = fetch(kb_value.strip())
        except Exception:
            return False
        if not label or not isinstance(label, str):
            return False
        return label.strip().casefold() == str(surface).strip().casefold()

    def _property_value_type_constraint(self, binding) -> list:
        """The KB property's OWN value-type constraint classes (Wikidata P2302
        value-type constraint → the allowed value classes), via the adapter's
        `fetch_property_ontology`. This is the constraint-validation layer's data
        source: when the oracle/seed left a binding's value-type undeclared, we
        read it from the property itself so the contradiction value-type guard
        still applies. FAIL-OPEN: returns [] for a missing kb_property, an adapter
        without `fetch_property_ontology` (mock/stub KBs), any fetch error, or a
        malformed result — the caller then permits as before. Never raises."""
        prop = getattr(binding, "kb_property", None)
        fetch = getattr(self._kb, "fetch_property_ontology", None)
        if not prop or not callable(fetch):
            return []
        try:
            onto = fetch(prop)
        except Exception:
            return []
        if not isinstance(onto, dict):
            return []
        return [c for c in (onto.get("value_type_qids") or []) if isinstance(c, str)]

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
            # The oracle/seed declared no value-type for this binding. Fall back to
            # the KB PROPERTY's OWN value-type constraint (Wikidata P2302), so this
            # contradiction guard still fires for predicates the oracle left
            # untyped (e.g. authored_by → P50, whose value-type constraint is
            # "human"). The constraint thus "falls out of the data" rather than the
            # oracle's guess — strengthening §3.2 without per-predicate seeding.
            # Still fail-open: an empty/unavailable constraint permits as before.
            value_types = self._property_value_type_constraint(binding)
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
        # NR (all-values directed upgrade): the DISTINCT scope-compatible ENTITY
        # values the subject holds that the claim did not value-match, keyed by
        # value so the subsumption upgrade below can try EVERY held value, not just
        # the first iterated one. A multi-valued subject (Obama P19 = {hospital,
        # Honolulu}) may have its container chain on a SIBLING value, so checking
        # only the first scope_mismatch could miss a true "born_in USA".
        entity_mismatch_by_value: "dict[str, Statement]" = {}
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
            if meta.object_type == "entity" and isinstance(stmt.value, str):
                entity_mismatch_by_value.setdefault(stmt.value, stmt)
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

        # Try the DIRECTED subsumption upgrade on EVERY held scope-compatible
        # mismatch value — functional or not (NR: all-values, not just the first).
        # A KB statement value that is a specialization of the claimed value (e.g.
        # Honolulu when the claim says "United States"; Île-de-France when the claim
        # says "France") VERIFIES the claim — the more-specific KB fact entails the
        # more-general claim. Looping over all distinct held values makes the result
        # independent of statement iteration order: Obama P19 = {hospital, Honolulu}
        # verifies "born_in USA" via Honolulu ⊆ USA even if the hospital iterated
        # first. Only for entity-typed values that resolved to KB IDs; literal
        # comparisons (numbers, dates, strings) don't subsume. This is the DIRECTED
        # grounding primitive the walker's neighbor-enumeration fanout otherwise
        # re-derives by generate-and-test (see _discover_chains' functional skip).
        if (
            meta.object_type == "entity"
            and value_resolved
            and isinstance(expected_value, str)
        ):
            for mismatch_value, mismatch_stmt in entity_mismatch_by_value.items():
                # part_of ONLY (§3.2): a held VALUE subsumed by the claim object
                # entails the claim only via GEOGRAPHIC containment (born in a place
                # within O ⇒ born in O). Including `is_a` here would FALSE-VERIFY a
                # non-distributing predicate whose held value happens to be is_a the
                # claimed object (e.g. an occupation value is_a 'river'), bypassing
                # the multi_valued_single_valued_predicate abstain guard below.
                if self._subsumption_upgrades(
                    mismatch_value, expected_value, relations=("part_of",)
                ):
                    return KBVerdictType.VERIFIED, mismatch_stmt, None

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
            # ENTITY-vs-LITERAL cross-kind guard (§3.2): S3 above PERMITS a
            # `literal` KB value to contradict an `entity` predicate — that
            # allowance is for literal-vs-literal external-id compares (ISBN),
            # where the object did NOT resolve (value_resolved is False). But when
            # the claim's object RESOLVED to a KB entity (value_resolved →
            # expected_value is a Q-id) and the contradicting statement holds a
            # non-entity literal, the comparison is resolved-entity-vs-literal:
            # the predicate is mis-mapped to a string/literal-datatype property and
            # the resolver nonetheless resolved the object to an entity (e.g.
            # `birth_name` → a monolingual-text property, object "Jorge Mario
            # Bergoglio" resolved to the person Q-id, then compared against the
            # literal birth-name string of the SAME surface form → a spurious
            # CONTRADICTED). A resolved entity can never soundly contradict a
            # literal of a different kind, so abstain.
            if value_resolved and not _is_entity_value(scope_mismatch):
                return KBVerdictType.NO_MATCH, None, "entity_claim_vs_literal_value"
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

    def _subsumption_upgrades(
        self, kb_value: str, expected_value: str,
        relations: tuple = ("part_of", "is_a"),
    ) -> bool:
        """Query the KB for whether the
        KB statement value (specific) is subsumed by the claim's expected
        value (general). Tries the given `relations` — `part_of` (geographic /
        location containment, Wikidata P131/P30/P17) and/or `is_a` (taxonomic,
        Wikidata P31/P279). The first that returns `a_subsumed_by_b` or
        `equivalent` upgrades the verdict to VERIFIED.

        `relations` defaults to both, but a caller MUST restrict it to the
        relation(s) over which the PREDICATE actually distributes. The
        in-statements value upgrade passes `("part_of",)` only: the claim object
        subsuming a held VALUE entails the claim only for geographic-containment
        (born_in/located_in: born in a place ⊆ a country ⇒ born in the country).
        An `is_a` value subsumption does NOT entail an arbitrary predicate (a held
        value that is_a the claimed object — e.g. an occupation that happens to be
        is_a 'river' — would FALSE-VERIFY), so it is excluded there.

        Fails closed on error — unknown relation types, invalid Q-IDs, or KB
        outages fall through to no-upgrade, preserving the prior CONTRADICTED
        verdict. Never promotes to VERIFIED on uncertainty (architecture §3.2
        soundness-over-completeness).
        """
        if kb_value == expected_value:
            return True
        for relation_type in relations:
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


# Anchored end ($) as defense-in-depth: it is used with .fullmatch() below (which
# already anchors both ends), but the explicit $ keeps a real entity Q-id from
# being misread if this is ever reused with .match()/.search().
_QID_RE = re.compile(r"Q\d+$")


def _is_entity_value(stmt) -> bool:
    """True when a KB statement holds an ENTITY (item) value rather than a
    literal. Prefers the adapter's value_type tag (the Wikidata adapter emits
    "entity" iff the value is an entity URI, else "literal"); for an UNTAGGED
    value falls back to the Q-id surface pattern so a real entity value (e.g.
    "Q18094") is never misread as a literal."""
    vt = getattr(stmt, "value_type", None)
    if vt == "entity":
        return True
    if vt:  # any other explicit tag (literal/date/quantity/string/…) is non-entity
        return False
    v = getattr(stmt, "value", None)
    return isinstance(v, str) and _QID_RE.fullmatch(v.strip()) is not None


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


def _present_value_match_too_early(
    statements, claim, expected_value, current_time: str, object_type: str
):
    """v0.16.4: return the CURRENT value-matching statement when the entity holds
    the claimed value NOW but the claim's lower temporal bound precedes the value's
    actual start ("X is president since 2022" vs a KB term that began in 2024) —
    i.e. the present base fact is grounded and only the claimed start date is not.
    Returns None when it does not apply.

    SOUND gating: only for PRESENT-reaching ENTITY claims carrying an explicit
    valid_from. A past-scoped claim ("X was president in 1990") is NEVER rescued by
    a current statement — its current value says nothing about a past window. The
    matching statement must itself be CURRENT (not provably ended; start not in the
    future), and the sole scope conflict must be the claimed start preceding the
    actual start."""
    if (
        object_type != "entity"
        or not getattr(claim, "valid_from", None)
        or not _claim_reaches_present(claim)
    ):
        return None
    for stmt in statements:
        if not _value_matches(stmt.value, expected_value):
            continue
        stmt_from = stmt.qualifiers.get("P580")
        stmt_until = stmt.qualifiers.get("P582")
        if stmt_until and _end_provably_past(stmt_until, current_time):
            continue  # the value is no longer held
        if stmt_from and _start_provably_future(stmt_from, current_time):
            continue  # the value has not begun
        # Genuinely too early: the claimed lower bound precedes the actual start
        # AND is not merely a coarser-grained CONTAINMENT of it. "since 2024" vs a
        # start of "2024-03-05" is the SAME year (containment) — consistent, NOT
        # too early — so it does NOT trip this fallback (it stays a plain NO_MATCH,
        # unchanged). "since 2022" vs "2024-03-05" is an earlier year — genuinely
        # too early. The prefix test handles ISO granularity (year / year-month /
        # full) without date parsing.
        if (
            stmt_from
            and claim.valid_from < stmt_from
            and not _norm_date_str(stmt_from).startswith(_norm_date_str(claim.valid_from))
        ):
            return stmt  # currently held; only the claimed start is genuinely early
    return None


def _norm_date_str(value) -> str:
    """Normalize an ISO-ish date for prefix comparison: drop a leading Wikidata
    '+' sign so "+2024-03-05T..." and a claim's "2024" share a prefix."""
    return str(value).lstrip("+")


def _present_start_unconfirmed(claim, stmt, object_type: str) -> bool:
    """True when a present-reaching ENTITY claim asserts a start ("since <date>")
    that the matched VERIFIED statement does not confirm — because the statement
    carries NO start qualifier (P580). The KB grounds the present fact but records
    no start, so the claimed since-date is unverifiable. (A statement WITH a P580
    that reached VERIFIED already passed the scope check — its start is at/before
    the claim's, which confirms the since-date — so it is not flagged here.)"""
    if (
        object_type != "entity"
        or not getattr(claim, "valid_from", None)
        or not _claim_reaches_present(claim)
    ):
        return False
    return not (stmt.qualifiers.get("P580") if stmt is not None else None)
