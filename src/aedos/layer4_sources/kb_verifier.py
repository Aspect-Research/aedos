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

        if meta.routing_hint != "kb_resolvable" or not meta.kb_property:
            return KBVerdict(verdict=KBVerdictType.NO_KB_PATH, trace={"reason": "not_kb_resolvable"})

        # Step 2: map the claim's slots onto KB statement positions (D19). An
        # inverse predicate keys its statement on the claim's *object*, so the
        # lookup entity and the expected value are swapped vs a standard one.
        targets = _lookup_targets(claim, meta)
        if targets is None:
            # A slot_to_qualifier shape the verifier cannot interpret. Abstain
            # with a clear trace note — never guess a direction, never crash.
            return KBVerdict(
                verdict=KBVerdictType.NO_KB_PATH,
                trace={
                    "reason": "unsupported_slot_to_qualifier",
                    "slot_to_qualifier": meta.slot_to_qualifier,
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
            expected_entity_types=_types_for_slot(meta, lookup_slot),
            source_text=source_text,
            claim_subject=claim.subject,
            claim_predicate=claim.predicate,
            claim_object=claim.object,
            claim_id=claim.claim_id,
        )
        lookup_subject_id = self._resolver.select(
            self._resolver.resolve(lookup_ref, lookup_ctx), lookup_ctx
        )
        if lookup_subject_id is None:
            return KBVerdict(
                verdict=KBVerdictType.NO_MATCH,
                trace={
                    "reason": "subject_resolution_failed",
                    "reference": lookup_ref,
                    "abstention_reason": "lookup_subject_unresolved",
                    "lookup_inverted": lookup_inverted,
                },
            )

        # Step 4: resolve the expected-value entity — compared against the
        # looked-up statement values (M4's object resolution, now applied to
        # whichever Aedos slot is the KB statement value). Falls back to the raw
        # string for literal comparison.
        expected_value = expected_ref
        value_resolved = False
        if meta.object_type == "entity":
            value_ctx = LocalContext(
                predicate=claim.predicate,
                slot_position=value_slot,
                asserting_party=claim.asserting_party,
                expected_entity_types=_types_for_slot(meta, value_slot),
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
        statements = self._kb.lookup_statements(lookup_subject_id, meta.kb_property)
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
                    },
                )
            # NO_MATCH is polarity-invariant — absence of evidence is not evidence.
            return KBVerdict(
                verdict=KBVerdictType.NO_MATCH,
                subject_kb_id=lookup_subject_id,
                trace={
                    "reason": "no_statements_found",
                    "entity": lookup_subject_id,
                    "property": meta.kb_property,
                    "abstention_reason": "no_statements",
                    "lookup_inverted": lookup_inverted,
                },
            )

        # Step 6: verdict for the claim's *positive* content (polarity-agnostic).
        # _compare_positive is direction-agnostic — it compares the expected
        # value against the statement values regardless of which Aedos slot the
        # expected value came from.
        pos_verdict, statement, abstention_reason = self._compare_positive(
            statements, claim, expected_value, value_resolved, meta, current_time
        )

        # Step 7: apply claim polarity (C1). A negated claim asserts the triple
        # is false, so a KB-supported triple makes it CONTRADICTED, and vice versa.
        final_verdict = _apply_polarity(pos_verdict, claim.polarity)

        trace = {
            "entity": lookup_subject_id,
            "property": meta.kb_property,
            "value_entity": expected_value,
            "value_resolved": value_resolved,
            "polarity": claim.polarity,
            "positive_verdict": pos_verdict.value,
            "single_valued": meta.single_valued,
            "lookup_inverted": lookup_inverted,
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

    def _compare_positive(
        self,
        statements: list[Statement],
        claim: Claim,
        expected_value,
        value_resolved: bool,
        meta,
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

        if scope_mismatch is not None and meta.single_valued:
            if value_unresolved:
                # N1: the expected-value reference never resolved — the mismatch
                # is a resolution failure, not a contradiction. Abstain, not lie.
                return KBVerdictType.NO_MATCH, None, "value_unresolved"
            return KBVerdictType.CONTRADICTED, scope_mismatch, None

        # Phase 10.5 Step 6 Batch 11 (Tier A1): conservative DISJOINT
        # verdict for non-functional location predicates. Two paths:
        #
        # (a) Continent-level (CONTINENT_QIDS) — claim asserts a continent
        # (e.g. "Thames in Asia", "Vatican in Africa") and KB statement
        # value's containment chain (P131/P361/P30/P206/P17) confidently
        # subsumes under a DIFFERENT continent. Positive KB evidence of
        # disjoint location.
        #
        # (b) Country-level — claim asserts a country (e.g. "Rome in
        # Germany"), KB statement value is also a country (e.g. Italy),
        # and KB confirms mutual non-containment via subsumption returning
        # `unrelated` in BOTH directions. Countries at the same admin
        # level are pairwise disjoint when neither contains the other,
        # so unrelated-both-ways IS positive evidence of distinct
        # countries (unlike the brittle general "unrelated means
        # disjoint" trap — countries are a structurally clean case).
        #
        # Both paths fall through to NO_MATCH (abstain) on uncertainty.
        if (
            scope_mismatch is not None
            and meta.object_type == "entity"
            and value_resolved
            and isinstance(scope_mismatch.value, str)
            and isinstance(expected_value, str)
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


def _lookup_targets(claim: Claim, meta) -> Optional[tuple[str, str, bool]]:
    """Map a claim's slots onto KB statement positions via slot_to_qualifier (D19).

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
    slot_map = meta.slot_to_qualifier
    if not slot_map:
        return (claim.subject, claim.object, False)
    subject_slot = slot_map.get("subject")
    object_slot = slot_map.get("object")
    if subject_slot in (None, "statement_subject") and object_slot in (None, "statement_value"):
        return (claim.subject, claim.object, False)
    if subject_slot == "statement_value" and object_slot in (None, "statement_subject"):
        return (claim.object, claim.subject, True)
    return None


def _types_for_slot(meta, slot: str) -> list[str]:
    """Phase G D33: pick the entity-types list that corresponds to the Aedos
    slot being resolved. Returns an empty list when no types are configured
    for that slot (the adapter then skips its post-filter)."""
    if slot == "subject":
        return list(meta.subject_entity_types or [])
    if slot == "object":
        return list(meta.object_entity_types or [])
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
