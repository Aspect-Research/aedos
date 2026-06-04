"""v0.16.3 Batch B (piece 1): generation-time empirical direction validation.

The predicate-metadata oracle cold-starts KB-relational predicates and chooses
the ``slot_to_qualifier`` DIRECTION (does the Aedos subject map to the KB
statement_subject or the statement_value?) nondeterministically — sometimes
inconsistently with its own declared entity-types. The live ``capital`` bug was
exactly this: an inverse map on P36 keyed the lookup on the city (which carries
no P36), so KB grounding silently failed.

This validator decides the direction EMPIRICALLY instead of trusting the oracle:

  1. SOURCE a known-true ``(statement_subject_qid, value_qid)`` example for the
     property from REAL Wikidata data — a curated anchor or the KB's
     ``sample_property_examples`` SPARQL — never from the oracle being validated.
  2. PROBE grounding both keyings against the KB: ``value ∈ lookup_statements(
     subject, P)`` (the statement is keyed on the subject) and the reverse. An
     asymmetric property grounds exactly one keying; a symmetric one (spouse,
     sibling) grounds both → direction-agnostic; neither → property-suspect.
  3. ORIENT which Aedos slot is the statement-subject side using the REAL P31
     types of the example entities cross-referenced with the predicate's declared
     subject/object entity-types (and, as a free fast-path, the property's P2302
     subject/value type constraints).

Soundness (architecture §3.2 — never a confident wrong-direction verify):
  - The direction is decided by real KB grounding + real KB types, NOT by the
    candidate ``slot_to_qualifier`` (no oracle-validates-oracle circularity).
  - Every failure mode — no example, symmetric property, can't orient, KB error,
    neither keying grounds — returns a NON-confirming verdict. The caller keeps
    the oracle direction for positive grounding but suppresses the
    contradiction license (single_valued → 0), so an unvalidated direction can
    never drive a false CONTRADICTED. Resolution/lookup failure is treated as
    inconclusive, never as a direction signal.
  - FAIL-OPEN: any unexpected error degrades to ``unconfirmed`` (never raises),
    so a flaky KB cannot block predicate generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

_STANDARD_SQ = {"subject": "statement_subject", "object": "statement_value"}
_INVERSE_SQ = {"subject": "statement_value", "object": "statement_subject"}

# Curated direction anchors: hand-verified real Wikidata (statement_subject_qid,
# value_qid) pairs, keyed by property. NOT oracle-generated. Used as a
# deterministic primary example source for well-known properties; the live
# `sample_property_examples` SPARQL is the general fallback for everything else.
# `s` is the entity that CARRIES the property statement (s wdt:P v).
_DIRECTION_ANCHORS: dict[str, list[tuple[str, str]]] = {
    # capital (P36): France -> Paris ; Germany -> Berlin
    "P36": [("Q142", "Q90"), ("Q183", "Q64")],
}

_PROBE_EXAMPLE_LIMIT = 4


@dataclass
class DirectionVerdict:
    """Outcome of validating one predicate's KB-relational direction.

    status:
      confirmed   — the oracle's direction grounds a known-true example; keep it.
      corrected   — the OTHER direction is the coherent/grounding one; `direction`
                    carries the corrected slot_to_qualifier.
      symmetric   — the property grounds both keyings (spouse/sibling); direction
                    is agnostic; keep the oracle's.
      suspect     — neither keying grounds the example (likely wrong PROPERTY);
                    do not trust the direction.
      unconfirmed — could not validate (no example / can't orient / KB error /
                    validator not wired); behave as before.
    `direction` is the validated/corrected slot_to_qualifier (or None when no
    confident direction was established). `grounded` is True iff a known-true
    example was found in the KB during probing.
    """

    status: str
    direction: Optional[dict]
    grounded: bool
    reason: str

    @property
    def is_validated(self) -> bool:
        """True iff a confident direction was established (confirmed/corrected/
        symmetric). unconfirmed/suspect are NOT validated — the caller then
        suppresses the contradiction license."""
        return self.status in ("confirmed", "corrected", "symmetric")


def _direction_of(sq: Optional[dict]) -> Optional[str]:
    """Classify a slot_to_qualifier as 'standard' | 'inverse' | None.

    None when the map is absent (treated as standard elsewhere, but here we want
    an explicit signal), qualifier-keyed, or otherwise not a clean
    subject/object direction the verifier's _lookup_targets interprets."""
    if not sq:
        return "standard"  # absent map == standard direction (verifier default)
    subj = sq.get("subject")
    obj = sq.get("object")
    if subj in (None, "statement_subject") and obj in (None, "statement_value"):
        return "standard"
    if subj == "statement_value" and obj == "statement_subject":
        return "inverse"
    return None  # qualifier-keyed / uninterpretable → not direction-probed


def _overlap(a: Optional[list], b: Optional[list]) -> bool:
    """True if two Q-id lists share at least one element.

    KNOWN LIMITATION (adversarial-review [6], SAFE): this is exact-QID
    intersection with NO subclass (P279) closure. If a declared type is a strict
    super/subclass of the example entity's direct P31 (e.g. declared country Q6256
    but the entity's P31 lists only sovereign-state Q3624078), orientation finds no
    overlap and the validator returns 'unconfirmed' (single_valued suppressed) —
    NEVER a wrong direction. So the limitation costs coverage, not soundness; the
    curated anchors and the common case (declared class appears directly in P31)
    keep it useful."""
    if not a or not b:
        return False
    return bool(set(a) & set(b))


class DirectionValidator:
    """Empirically validate a KB-relational predicate's slot_to_qualifier
    direction. FAIL-OPEN and OPTIONAL: when `kb` is None the validator is a no-op
    (every validate() returns `unconfirmed`), so a pipeline that does not wire it
    behaves exactly as before."""

    def __init__(self, kb=None, property_relations=None, anchors=None) -> None:
        # `kb` must expose sample_property_examples, lookup_statements, fetch_types
        # (the WikidataAdapter does; a mock may stub them). Accessed via getattr so
        # a partial mock degrades to unconfirmed rather than raising.
        self._kb = kb
        self._property_relations = property_relations
        self._anchors = anchors if anchors is not None else _DIRECTION_ANCHORS

    def validate(
        self,
        kb_property: Optional[str],
        kb_namespace: Optional[str],
        candidate_sq: Optional[dict],
        subject_entity_types: Optional[list],
        object_entity_types: Optional[list],
    ) -> DirectionVerdict:
        """Validate the direction for one (property, candidate slot_to_qualifier).
        Never raises — any error yields `unconfirmed`."""
        try:
            return self._validate(
                kb_property, kb_namespace, candidate_sq,
                subject_entity_types, object_entity_types,
            )
        except Exception as exc:  # fail-open: generation must never break here
            return DirectionVerdict(
                "unconfirmed", None, False, f"validator_error:{type(exc).__name__}"
            )

    # ------------------------------------------------------------------

    def _validate(
        self, kb_property, kb_namespace, candidate_sq,
        subject_entity_types, object_entity_types,
    ) -> DirectionVerdict:
        if self._kb is None or not kb_property:
            return DirectionVerdict("unconfirmed", None, False, "validator_not_wired")

        cand_dir = _direction_of(candidate_sq)
        if cand_dir is None:
            return DirectionVerdict(
                "unconfirmed", None, False, "non_directional_or_qualifier_keyed"
            )

        examples = self._source_examples(kb_property)
        if not examples:
            return DirectionVerdict("unconfirmed", None, False, "no_example_sourced")

        std_grounds, inv_grounds = self._probe_grounding(kb_property, examples)
        if std_grounds and inv_grounds:
            # Symmetric property (spouse/sibling): both keyings ground, so the
            # direction is genuinely agnostic — neither map can mis-key the lookup.
            return DirectionVerdict("symmetric", candidate_sq, True, "symmetric_property")
        if not std_grounds and not inv_grounds:
            # The example did not ground under EITHER keying — the property itself
            # is suspect (likely the wrong P-id), not merely the direction.
            return DirectionVerdict("suspect", None, False, "neither_direction_grounds")

        # Asymmetric, keyed on the statement-subject. Decide which AEDOS slot that
        # is, from the real types of the example entities + declared entity-types.
        subj_is_statement_subject = self._orient(
            kb_property, kb_namespace, examples,
            subject_entity_types, object_entity_types,
        )
        if subj_is_statement_subject is None:
            return DirectionVerdict(
                "unconfirmed", candidate_sq, True, "cannot_orient_aedos_slot"
            )

        correct_dir = "standard" if subj_is_statement_subject else "inverse"
        correct_sq = _STANDARD_SQ if correct_dir == "standard" else _INVERSE_SQ
        if correct_dir == cand_dir:
            return DirectionVerdict("confirmed", correct_sq, True, "grounded_matches_oracle")
        return DirectionVerdict(
            "corrected", correct_sq, True,
            f"grounded_direction={correct_dir}_overrides_oracle={cand_dir}",
        )

    # ------------------------------------------------------------------

    def _source_examples(self, kb_property: str) -> list[tuple[str, str]]:
        """Real-Wikidata known-true (subject_qid, value_qid) pairs. Curated anchor
        first (deterministic), then the KB's live SPARQL sourcer."""
        anchor = self._anchors.get(kb_property)
        if anchor:
            return list(anchor)[:_PROBE_EXAMPLE_LIMIT]
        sampler = getattr(self._kb, "sample_property_examples", None)
        if not callable(sampler):
            return []
        try:
            pairs = sampler(kb_property, _PROBE_EXAMPLE_LIMIT)
        except Exception:
            return []
        return [
            (s, v) for (s, v) in (pairs or [])
            if isinstance(s, str) and isinstance(v, str)
        ]

    def _probe_grounding(
        self, kb_property: str, examples: list[tuple[str, str]]
    ) -> tuple[bool, bool]:
        """Probe BOTH keyings against the KB. Returns (standard_grounds,
        inverse_grounds): standard = value ∈ lookup_statements(subject, P);
        inverse = subject ∈ lookup_statements(value, P) (true for a SYMMETRIC
        property).

        Adversarial-review fix ([3]): 'symmetric' must be a ROBUST property of the
        sample, not 'any one reverse-grounds' — a lone anomalous reciprocal pair
        (a Wikidata sister-edge / modeling error) must not flip a genuinely
        asymmetric property to symmetric. So `std` latches on ANY forward grounding
        (the example is real), but `inv` is reported only when the reverse keying
        grounds for EVERY example that forward-grounds (unanimous reciprocity).
        With a single example the two collapse to the same thing."""
        lookup = getattr(self._kb, "lookup_statements", None)
        if not callable(lookup):
            return (False, False)
        std = False
        fwd_count = 0
        rev_with_fwd = 0  # examples that forward-ground AND reverse-ground
        for s, v in examples:
            try:
                fwd = {st.value for st in (lookup(s, kb_property) or [])}
                rev = {st.value for st in (lookup(v, kb_property) or [])}
            except Exception:
                continue
            if v in fwd:
                std = True
                fwd_count += 1
                if s in rev:
                    rev_with_fwd += 1
        # Unanimous reciprocity among the grounding examples → symmetric.
        inv = fwd_count > 0 and rev_with_fwd == fwd_count
        return (std, inv)

    def _orient(
        self, kb_property, kb_namespace, examples,
        subject_entity_types, object_entity_types,
    ) -> Optional[bool]:
        """Decide whether the Aedos SUBJECT slot corresponds to the KB
        statement-subject side. Returns True (subject == statement_subject →
        STANDARD), False (subject == statement_value → INVERSE), or None when it
        cannot be determined unambiguously.

        Primary signal: the REAL P31 types of an example's (s, v) entities vs the
        predicate's declared subject/object entity-types. Fallback: the property's
        P2302 subject/value type constraints from the ontology. Both are real
        Wikidata data, not the oracle.

        Adversarial-review fix ([5]): orientation is decided ACROSS ALL examples
        and must be UNANIMOUS — any disagreement returns None (→ unconfirmed →
        single_valued suppressed). First-example-wins was order-dependent and could
        emit a CONFIDENT WRONG correction from one anomalously-typed example."""
        subj_types = subject_entity_types or []
        obj_types = object_entity_types or []
        if not subj_types and not obj_types:
            return None  # nothing to match the example's real types against

        # Real types of each example's entities (cached via fetch_types). Collect
        # every non-None per-example decision; require they all agree.
        fetch_types = getattr(self._kb, "fetch_types", None)
        decisions = set()
        for s, v in examples:
            t_s, t_v = self._types_for(fetch_types, s, v)
            if t_s is None or t_v is None:
                continue
            d = self._decide_orientation(subj_types, obj_types, t_s, t_v)
            if d is not None:
                decisions.add(d)
        if len(decisions) == 1:
            return next(iter(decisions))
        if len(decisions) > 1:
            return None  # examples disagree → cannot confidently orient

        # No example yielded a decision → property P2302 subject/value constraints.
        onto_ts, onto_tv = self._ontology_role_types(kb_property, kb_namespace)
        if onto_ts and onto_tv:
            return self._decide_orientation(subj_types, obj_types, onto_ts, onto_tv)
        return None

    @staticmethod
    def _decide_orientation(
        subj_types: list, obj_types: list, role_s: list, role_v: list
    ) -> Optional[bool]:
        """Given the declared subject/object types and the (statement_subject,
        statement_value) role type-sets, decide whether the Aedos subject is the
        statement_subject side. Requires an UNAMBIGUOUS match (subject matches
        exactly one role) and no contradicting object signal."""
        subj_is_s = _overlap(subj_types, role_s)
        subj_is_v = _overlap(subj_types, role_v)
        obj_is_s = _overlap(obj_types, role_s)
        obj_is_v = _overlap(obj_types, role_v)

        # Subject pins to statement_subject; object (if it speaks) must agree.
        if subj_is_s and not subj_is_v and not obj_is_s:
            return True
        # Subject pins to statement_value; object (if it speaks) must agree.
        if subj_is_v and not subj_is_s and not obj_is_v:
            return False
        # Subject silent/ambiguous — the object may decide ONLY in the safe
        # direction: object cleanly matches the statement-VALUE role (and not the
        # subject role) → standard. We deliberately do NOT infer INVERSE from the
        # object alone: "the object matches the statement-SUBJECT's type" is a weak
        # signal (many properties — e.g. P131 located-in — carry a value-typed
        # entity on the subject side too: a city is located in a region), so
        # without a corroborating subject-type signal it would risk a confident
        # WRONG correction. That case falls through to None → unconfirmed, where
        # the never-CONTRADICT posture applies. The live audit on `stands_on`
        # (P131) exposed exactly this trap.
        if not subj_is_s and not subj_is_v:
            if obj_is_v and not obj_is_s:
                return True   # object is the value side → subject is the subject side
        return None

    @staticmethod
    def _types_for(fetch_types, s, v):
        if not callable(fetch_types):
            return (None, None)
        try:
            types_by_qid, error = fetch_types([s, v])
        except Exception:
            return (None, None)
        if error:
            return (None, None)
        return (types_by_qid.get(s) or [], types_by_qid.get(v) or [])

    def _ontology_role_types(self, kb_property, kb_namespace):
        if self._property_relations is None:
            return (None, None)
        try:
            onto = self._property_relations.fetch(kb_property, kb_namespace or "wikidata")
        except Exception:
            return (None, None)
        return (
            list(getattr(onto, "subject_type_qids", []) or []),
            list(getattr(onto, "value_type_qids", []) or []),
        )
