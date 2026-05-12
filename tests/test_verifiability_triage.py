"""Tests for src.layer1_extraction.verifiability_triage (Layer 1.5).

Pure-function unit tests; no LLM, no store. Cover all five VERIFY
rules + the default PASS_THROUGH path + the architectural invariants
(decision is deterministic; same claim → same decision).
"""

from __future__ import annotations

from src.layer1_extraction.verifiability_triage import (
    TriageDecision,
    triage_claim,
)


# ---- Rule 1: numeric value present ----------------------------------


def test_numeric_value_in_slot_triggers_verify():
    claim = {
        "pattern": "quantitative", "predicate": "wild_lifespan_years",
        "polarity": 1,
        "slots": {"subject": "baboon", "property": "lifespan", "value": 30},
        "source_text": "baboons live 30 years",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "numeric_slot"


def test_numeric_string_value_in_slot_triggers_verify():
    claim = {
        "pattern": "quantitative", "predicate": "has_count",
        "polarity": 1,
        "slots": {"subject": "Saturn", "property": "moons", "value": "274"},
        "source_text": "Saturn has 274 moons",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY


# ---- Rule 2: date / temporal scope -----------------------------------


def test_date_slot_triggers_verify():
    claim = {
        "pattern": "role_assignment", "predicate": "served_as",
        "polarity": 1,
        "slots": {"agent": "Trump", "role": "45th President",
                  "valid_from": "2017", "valid_until": "2021"},
        "source_text": "Trump served as the 45th president from 2017 to 2021",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "date_slot"


def test_year_in_value_triggers_verify():
    claim = {
        "pattern": "event", "predicate": "founded",
        "polarity": 1,
        "slots": {"event_type": "company_founding",
                  "participants": ["Anthropic"],
                  "occurred_at": "2021"},
        "source_text": "Anthropic was founded in 2021",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY


# ---- Rule 3: multiple specific-referent slots (v0.14.6) --------------


def test_two_named_entities_triggers_verify():
    """Marie Curie + Pierre Curie — both proper nouns, both specific.
    The predicate co_authored_paper_with is intentionally NOT in
    relational's verify-allow-list so Rule 6 doesn't preempt; Rule 3
    is the one that fires."""
    claim = {
        "pattern": "relational", "predicate": "co_authored_paper_with",
        "polarity": 1,
        "slots": {"subject": "Marie Curie", "object": "Pierre Curie"},
        "source_text": "Marie Curie co-authored a paper with Pierre Curie",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "multiple_specific_slots"


def test_two_lowercase_common_nouns_trigger_verify():
    """v0.14.6 loosening: lowercase common nouns ("cats", "mice") now
    qualify as specific referents under Rule 3. Wikipedia handles
    'Cat' and 'Mouse' fine; the v0.14.5 capitalized-only heuristic
    was systematically PASS_THROUGH'ing claims about animals,
    materials, foods, etc."""
    claim = {
        "pattern": "relational", "predicate": "hunts",
        "polarity": 1,
        "slots": {"subject": "cats", "object": "mice"},
        "source_text": "cats hunt mice",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "multiple_specific_slots"


def test_two_vague_nouns_still_pass_through():
    """The vague-noun stopword list catches the cases where the
    loosened heuristic shouldn't have accepted both slots: pronouns,
    'thing', 'behavior', etc. Both subject and object must be
    stopword-listed for Rule 3 to abstain."""
    claim = {
        "pattern": "relational", "predicate": "affects",
        "polarity": 1,
        "slots": {"subject": "behavior", "object": "things"},
        "source_text": "behavior affects things",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.PASS_THROUGH
    assert r.rule == "no_falsifiability_signal"


def test_one_specific_one_vague_does_not_trigger_rule_3():
    """When only one slot clears the specificity check (the other is
    a vague placeholder like 'things' / 'behavior'), Rule 3's
    ≥2-specific-slots requirement isn't met. The claim falls through
    to the anchor / categorical / default rules."""
    claim = {
        "pattern": "relational", "predicate": "exhibits",
        "polarity": 1,
        "slots": {"subject": "baboons", "object": "behavior"},
        "source_text": "baboons exhibit behavior",
    }
    r = triage_claim(claim)
    # baboons is specific; behavior is in the vague-noun stopword.
    # No anchor, not categorical, not numeric → PASS_THROUGH.
    assert r.decision is TriageDecision.PASS_THROUGH


# ---- Rule 5: anchor + specific predicate -----------------------------


def test_anchor_with_specific_predicate_triggers_verify():
    """Anchor + specific (non-vague) predicate, with vague slot
    contents that don't trip Rule 3 themselves. The anchor is the
    falsifiability signal here — the extractor saw enough topical
    context that the claim is grounded even when the slot values
    aren't standalone-verifiable."""
    claim = {
        "pattern": "relational", "predicate": "passes_across",
        "polarity": 1,
        "slots": {"subject": "behavior", "object": "things"},
        "source_text": "behavior passes across things",
        "anchor_entity": "baboon",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "anchored_specific_predicate"


def test_anchor_with_vague_predicate_does_not_trigger():
    """Anchor present but predicate is generic ('is', 'has') — not
    enough signal to verify."""
    claim = {
        "pattern": "relational", "predicate": "is",
        "polarity": 1,
        "slots": {"subject": "behavior", "object": "complex"},
        "source_text": "behavior is complex",
        "anchor_entity": "baboon",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.PASS_THROUGH


# ---- Default PASS_THROUGH --------------------------------------------


def test_vague_qualitative_falls_through():
    """The canonical case the gate is for — a claim with no
    falsifiability signal."""
    claim = {
        "pattern": "categorical", "predicate": "is_a",
        "polarity": 1,
        "slots": {"entity": "baboons", "category": "intelligent"},
        "source_text": "baboons are intelligent",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.PASS_THROUGH
    assert r.rule == "no_falsifiability_signal"


def test_preference_claim_with_named_object_triggers_verify():
    """v0.14.1 design: preference patterns are NOT always-pass-through.
    Tier U is the cheap source of truth and the walker still runs.
    A preference claim with a named-entity object gets VERIFY (the
    walker hits Tier U and may match/contradict the user's stored
    preference)."""
    claim = {
        "pattern": "preference", "predicate": "loves",
        "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "you love olives",
    }
    # Single string slot 'olives' — not multiple named entities. No
    # numeric, no date. PASS_THROUGH is fine — the walker still runs
    # for cheap Tier U lookup, just no fresh dispatch fallthrough.
    r = triage_claim(claim)
    assert r.decision is TriageDecision.PASS_THROUGH
    # Confirm the architectural invariant: PASS_THROUGH only suppresses
    # fresh dispatch, not Tier U / W / derivation.


# ---- Rule 6: computable predicate allow-list -------------------------


def test_current_time_predicate_triggers_verify():
    """Regression: the Cairo / New York current_time claim used to
    fall through to PASS_THROUGH because '2:56 am' doesn't parse as a
    number and the slots have only one named entity. Rule 6 catches
    it via the predicate name — current_time is in
    quantitative.triage_verify_predicates in patterns.yaml."""
    from src.layer1_extraction.pattern_registry import (
        load_default_registry, reset_cache,
    )
    reset_cache()
    registry = load_default_registry()
    claim = {
        "pattern": "quantitative", "predicate": "current_time",
        "polarity": 1,
        "slots": {"subject": "Cairo", "property": "time", "value": "9:56 am"},
        "source_text": "it would be 9:56 am in Cairo",
    }
    r = triage_claim(claim, registry=registry)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "verify_predicate"


def test_letter_count_predicate_triggers_verify():
    from src.layer1_extraction.pattern_registry import (
        load_default_registry, reset_cache,
    )
    reset_cache()
    registry = load_default_registry()
    claim = {
        "pattern": "quantitative", "predicate": "letter_count",
        "polarity": 1,
        "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
        "source_text": "Strawberry has 3 r's",
    }
    r = triage_claim(claim, registry=registry)
    assert r.decision is TriageDecision.VERIFY
    # numeric_slot would also match here (value=3); rule order says
    # verify_predicate fires first when both apply.
    assert r.rule == "verify_predicate"


def test_located_in_predicate_triggers_verify_via_schema():
    """v0.14.3 regression: the Cairo timezone case — schema declares
    spatial_temporal.triage_verify_predicates includes located_in,
    so a claim like 'Cairo is in Egypt Standard Time (UTC+2)' lands
    on VERIFY through the schema's per-pattern allow-list."""
    from src.layer1_extraction.pattern_registry import (
        load_default_registry, reset_cache,
    )
    reset_cache()
    registry = load_default_registry()
    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {
            "entity": "Cairo",
            "location": "Egypt Standard Time (UTC+2)",
            "relation_kind": "timezone",
        },
        "source_text": "Cairo is in Egypt Standard Time (UTC+2)",
    }
    r = triage_claim(claim, registry=registry)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "verify_predicate"


def test_unknown_predicate_does_not_trigger_rule_6():
    """A predicate not in the per-pattern allow-list doesn't get the
    free pass — the other rules have to settle it."""
    from src.layer1_extraction.pattern_registry import (
        load_default_registry, reset_cache,
    )
    reset_cache()
    registry = load_default_registry()
    claim = {
        "pattern": "relational", "predicate": "knows_about",
        "polarity": 1,
        "slots": {"subject": "behavior", "object": "topic"},
        "source_text": "behavior knows about topic",
    }
    r = triage_claim(claim, registry=registry)
    assert r.decision is TriageDecision.PASS_THROUGH
    assert r.rule == "no_falsifiability_signal"


# ---- Rule 7: named-entity subject + value slot -----------------------


def test_named_subject_with_value_triggers_verify():
    """Cairo current_time(subject=Cairo, value='2:56 am') has only
    one specific slot (Rule 3 fails on count alone) and a string
    value (Rule 1 fails). Rule 7 catches it via the
    specific-subject + explicit-value combination — the value is
    the falsifiable thing being asserted.

    Note: in practice this claim ALSO hits Rule 6 first via the
    current_time allow-list, so this test isolates Rule 7 by using a
    different predicate so we can verify the rule fires
    independently."""
    claim = {
        "pattern": "quantitative", "predicate": "stock_price",
        "polarity": 1,
        "slots": {"subject": "Apple", "property": "closing_price",
                  "value": "$175.50"},
        "source_text": "Apple closed at $175.50",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "specific_subject_with_value"


def test_value_slot_without_named_entity_does_not_trigger_rule_7():
    """When EVERY slot value is a vague-noun stopword, Rule 7's
    'any-specific-slot' check fails and the claim falls through.
    With the v0.14.6 loosening this requires actually-vague values
    (the prior 'feeling level high' shape now has 'feeling' as a
    specific referent — Wikipedia has an article on 'Feeling')."""
    claim = {
        "pattern": "quantitative", "predicate": "happiness_level",
        "polarity": 1,
        "slots": {"subject": "thing", "property": "level",
                  "value": "high"},
        "source_text": "thing level is high",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.PASS_THROUGH


# ---- Determinism -----------------------------------------------------


def test_triage_is_deterministic():
    """Same claim → same decision every time. No store, no LLM, no
    randomness."""
    claim = {
        "pattern": "quantitative", "predicate": "has_count",
        "polarity": 1,
        "slots": {"subject": "x", "property": "y", "value": 42},
        "source_text": "x has 42 y",
    }
    r1 = triage_claim(claim)
    r2 = triage_claim(claim)
    r3 = triage_claim(claim)
    assert r1.decision == r2.decision == r3.decision
    assert r1.rule == r2.rule == r3.rule


# ---- Rule 8: concrete categorical (v0.14.6) --------------------------


def test_concrete_categorical_lowercase_common_nouns_triggers_verify():
    """The canonical regression: 'cats are mammals' under v0.14.5 fell
    into PASS_THROUGH because both slots were lowercase common nouns
    that the orthography-biased _looks_named_entity heuristic
    rejected. Wikipedia handles this fine; v0.14.6's Rule 8 catches
    it via the per-slot specificity check on categorical claims."""
    claim = {
        "pattern": "categorical", "predicate": "is_a",
        "polarity": 1,
        "slots": {"entity": "cats", "category": "mammals"},
        "source_text": "cats are mammals",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "concrete_categorical"


def test_concrete_categorical_with_capitalized_entity_still_triggers():
    """Capitalized + lowercase combination — proper noun entity, common
    noun category — also catches Rule 8 (and would have caught Rule 3
    via the v0.14.5 path too, but Rule 8 fires first when both are
    specific)."""
    claim = {
        "pattern": "categorical", "predicate": "is_a",
        "polarity": 1,
        "slots": {"entity": "Tokyo", "category": "city"},
        "source_text": "Tokyo is a city",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    # Rule 8 wins over Rule 3 because Rule 8 is checked AFTER Rule 3
    # in the new ordering — but Rule 3 requires both slots to clear
    # specificity with cap >=2; "Tokyo" passes, "city" length 4 not
    # in stopword passes. So Rule 3 fires first.
    assert r.rule in ("multiple_specific_slots", "concrete_categorical")


def test_vague_categorical_falls_through():
    """When the category is a vague qualitative descriptor
    ('intelligent', 'complex', 'advanced'), Rule 8 abstains. This
    pins the discipline that the loosening is bounded — the
    extractor's hard-claim filter already rejects most of these,
    but Rule 8 is the second line of defense."""
    claim = {
        "pattern": "categorical", "predicate": "is_a",
        "polarity": 1,
        "slots": {"entity": "baboons", "category": "intelligent"},
        "source_text": "baboons are intelligent",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.PASS_THROUGH
    assert r.rule == "no_falsifiability_signal"


def test_categorical_with_thing_category_falls_through():
    """The placeholder-noun trap. 'X is a thing' must always
    PASS_THROUGH — there's no Wikipedia article that can settle 'X
    is a thing' for any X."""
    claim = {
        "pattern": "categorical", "predicate": "is_a",
        "polarity": 1,
        "slots": {"entity": "concept", "category": "thing"},
        "source_text": "concept is a thing",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.PASS_THROUGH


def test_categorical_with_pronoun_entity_falls_through():
    """Pronouns ('it', 'this', 'they') don't anchor a Wikipedia
    query — they're context-dependent referents. Rule 8 abstains."""
    claim = {
        "pattern": "categorical", "predicate": "is_a",
        "polarity": 1,
        "slots": {"entity": "it", "category": "mammal"},
        "source_text": "it is a mammal",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.PASS_THROUGH


# ---- _looks_specific helper edge cases (v0.14.6) ---------------------
#
# Direct unit tests on the specificity helper. Belt-and-suspenders for
# the rules above — the helper's contract is what makes each rule's
# loosening bounded.


def test_looks_specific_strips_leading_article():
    """The vague-noun lookup runs after stripping a leading article
    so 'the cat' / 'a cat' / 'an apple' tokenize to their head noun
    for the stopword check. This matters because the extractor often
    preserves articles in slot values."""
    from src.layer1_extraction.verifiability_triage import _looks_specific
    assert _looks_specific("the cat") is True
    assert _looks_specific("a cat") is True
    assert _looks_specific("an apple") is True
    # Still rejects when the stripped head noun is a stopword.
    assert _looks_specific("the thing") is False
    assert _looks_specific("a behavior") is False


def test_looks_specific_multi_word_bypasses_stopword():
    """Multi-word phrases qualify regardless of head noun — a
    modifier always carries some information. 'vague behavior' /
    'social system' / 'simple thing' beat the bare-head-noun
    rejection because the modifier narrows the referent."""
    from src.layer1_extraction.verifiability_triage import _looks_specific
    assert _looks_specific("operating system") is True
    assert _looks_specific("social behavior") is True
    assert _looks_specific("the United States") is True


def test_looks_specific_rejects_short_tokens():
    """Single-token values shorter than 3 chars (after article strip)
    don't qualify even if they're not in the stopword list. Catches
    short tokens like 'a' / 'is' that would otherwise sneak through
    the stopword check."""
    from src.layer1_extraction.verifiability_triage import _looks_specific
    assert _looks_specific("ab") is False
    assert _looks_specific("a") is False
    assert _looks_specific("") is False
    assert _looks_specific("   ") is False


def test_looks_specific_rejects_non_strings():
    """Defensive: numeric / None / list values aren't strings and
    can't be referent names. The helper returns False rather than
    raising, so triage doesn't crash on extractor weirdness."""
    from src.layer1_extraction.verifiability_triage import _looks_specific
    assert _looks_specific(None) is False
    assert _looks_specific(42) is False
    assert _looks_specific(["cat"]) is False
    assert _looks_specific({"name": "cat"}) is False


def test_looks_specific_accepts_lowercase_common_nouns():
    """The headline change. Lowercase common nouns of 3+ chars that
    aren't stopwords now qualify. This is what enables the ≥2-slot
    Rule 3 and the concrete-categorical Rule 8 to fire on textbook
    common-knowledge claims."""
    from src.layer1_extraction.verifiability_triage import _looks_specific
    assert _looks_specific("cat") is True
    assert _looks_specific("pizza") is True
    assert _looks_specific("mammal") is True
    assert _looks_specific("oxygen") is True


def test_looks_specific_rejects_user_token():
    """The 'user' token is the chatting user's placeholder per the
    extractor convention (first-person 'I' / 'me' canonicalize to
    'user'). It's intentionally in the stopword list so preference /
    propositional_attitude claims with agent='user' don't trip the
    multi-specific-slot rule on the agent slot alone."""
    from src.layer1_extraction.verifiability_triage import _looks_specific
    assert _looks_specific("user") is False
    assert _looks_specific("me") is False
    assert _looks_specific("i") is False
