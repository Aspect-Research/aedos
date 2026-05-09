"""Relevance gating for substrate consultation (v0.14.4).

Architectural problem
=====================

The substrate's alias-broadening behavior pulls every same-pattern
stored fact as a candidate for entity_equivalence comparison. The
cost discipline ("memoize after first call") doesn't reduce
cold-start waste — obviously-unrelated pairs (Cairo ↔ lizard) still
pay one LLM call to learn "different", and the verdict provides
no useful signal for any future verification.

This module adds a CHEAP RULE-BASED pre-gate. Before the
entity_equivalence oracle is consulted on a (claim_entity,
stored_entity) pair, the gate checks whether the candidate's tokens
overlap with the current verification's active context. If no
overlap, the candidate is skipped — no oracle call, no row
written, no warm-cache hits accumulated.

Active context
==============

The "active context" for a verification is the union of:

  * tokens from the claim's slot values
  * tokens from the claim's ``source_text``
  * tokens from the claim's ``anchor_entity`` (when set)
  * tokens from the current user message (the prompt that triggered
    the turn)

Including the user message is what distinguishes "we're talking
about this entity right now" from "this entity exists in the store
from some past turn." A genuine cross-turn alias ("I live in NYC"
turn 5; "you live in New York City" turn 12) shares predicate-name
tokens (`live`/`lives_in`) and surrounding context; the gate
preserves it. An obviously-unrelated pair (Cairo from turn 1, lizard
from turn 2) shares nothing across the active context union; the
gate filters it.

Tokenization
============

Lowercase, alphanumeric-only spans of length ≥ 2, with a tight
~30-word custom stopword set removed. The stopword list was
deliberately kept small so words like "more"/"most" — which can
matter for comparative claims — survive. Standard NLTK lists drop
those.

Architectural fit
=================

Same shape as the v0.14.3 routing reconciler: a cheap rule-based
gate before an expensive LLM call. Each layer should use
cheaply-available signals to skip work that isn't going to produce
useful output. Principle 3 (frequentist confidence from independent
external evidence) gets sharper — the system reserves LLM cost for
comparisons that are conversationally plausible.

Back-compat
===========

Callers that don't supply an active context (active_tokens=None or
empty) get unchanged behavior — no gating, every same-pattern
candidate flows through to the oracle as before. The gate fires
only when the pipeline explicitly opts in by passing a non-empty
token set.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional


# Alphanumeric span tokenizer. Splits on anything else (punctuation,
# whitespace, symbols). Lowercased before splitting so the regex is
# simple. Yields tokens of length ≥ 2 only — single characters carry
# essentially no semantic signal and explode the false-positive rate
# (every single-letter overlap would qualify).
_TOKEN_RE = re.compile(r"[a-z0-9]+")


# Tight custom stopword set — the most common English function words
# whose presence in both sides of an intersection would carry zero
# semantic signal. Deliberately ~30 words, NOT NLTK's ~180. NLTK
# drops words like "more"/"most"/"few"/"many" that are meaningful
# for comparative claims AND words like "is"/"are" that nonetheless
# co-occur in nearly every claim's source_text. The custom set keeps
# the comparative-meaningful words and drops only the connectives /
# articles / generic copulae that contribute pure noise to overlap.
_STOPWORDS: frozenset[str] = frozenset({
    # Articles
    "the", "a", "an",
    # Copula + auxiliary verbs
    "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "has", "have", "had",
    # Prepositions
    "of", "in", "on", "at", "to", "for", "with", "by", "from",
    # Coordinators / negation
    "and", "or", "but", "not", "no",
    # Demonstratives / pronouns
    "this", "that", "these", "those", "it",
})


def tokenize(text: Any) -> list[str]:
    """Tokenize ``text`` into the active-context-comparable form.

    Returns lowercase, alphanumeric-only tokens of length ≥ 2 with
    stopwords dropped. Non-string input → empty list. Order
    preserved for deterministic test snapshots; callers that want
    set semantics should wrap in ``frozenset()``.
    """
    if not isinstance(text, str) or not text:
        return []
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text.lower()):
        t = m.group(0)
        if len(t) < 2:
            continue
        if t in _STOPWORDS:
            continue
        out.append(t)
    return out


def _tokens_from_slot_values(slots: dict[str, Any]) -> list[str]:
    """Collect tokens from every string-valued slot. Lists of strings
    (e.g. ``event.participants``) are flattened. Non-string values
    (numbers, booleans, dicts) yield no tokens — they're not the
    falsifiability signal alias broadening cares about anyway."""
    out: list[str] = []
    for v in slots.values():
        if isinstance(v, str):
            out.extend(tokenize(v))
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    out.extend(tokenize(item))
    return out


def compute_active_context(
    claim: dict, current_user_message: Optional[str] = None,
) -> frozenset[str]:
    """Build the active-context token set for one verification.

    Union of tokens from (in order):
      1. claim slot values (string-typed and lists of strings)
      2. claim ``source_text``
      3. claim ``anchor_entity`` (when set)
      4. ``current_user_message`` (when supplied — the prompt that
         triggered the turn)

    Returns a frozenset (cheap intersection ops downstream) of
    lowercased, ≥2-char, non-stopword tokens.

    When the claim is empty / malformed, returns an empty frozenset.
    Callers should treat empty active context as "no gating
    available" and fall back to current behavior — see
    ``is_candidate_relevant`` for the convention.
    """
    tokens: set[str] = set()
    slots = claim.get("slots") or {}
    if isinstance(slots, dict):
        tokens.update(_tokens_from_slot_values(slots))
    src = claim.get("source_text")
    if isinstance(src, str):
        tokens.update(tokenize(src))
    anchor = claim.get("anchor_entity")
    if isinstance(anchor, str):
        tokens.update(tokenize(anchor))
    if isinstance(current_user_message, str):
        tokens.update(tokenize(current_user_message))
    return frozenset(tokens)


def candidate_tokens(
    slot_values: Iterable[Any], source_text: Optional[str] = None,
) -> frozenset[str]:
    """Build the candidate-token set for one stored-fact candidate.

    ``slot_values`` is an iterable of slot values (typically the
    candidate's identity-slot values pulled from the cache or Tier U
    row). ``source_text`` is the candidate's stored source text when
    available.

    Returns a frozenset of lowercased, ≥2-char, non-stopword tokens.
    """
    tokens: set[str] = set()
    for v in slot_values:
        if isinstance(v, str):
            tokens.update(tokenize(v))
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    tokens.update(tokenize(item))
    if isinstance(source_text, str):
        tokens.update(tokenize(source_text))
    return frozenset(tokens)


def is_candidate_relevant(
    active_tokens: Optional[frozenset[str]],
    candidate_tokens: frozenset[str],
) -> bool:
    """Whether this candidate is relevant to the active verification
    context.

    Convention:
      * ``active_tokens=None`` → True (no gating opted in; preserve
        back-compat for callers that haven't been updated)
      * ``active_tokens`` empty (frozenset()) → True (defensive — a
        claim with no extractable tokens shouldn't gate anything; we
        can't make a relevance call without signal)
      * ``candidate_tokens`` empty → True (candidate has nothing to
        compare; conservative — don't filter what we can't analyze)
      * Both non-empty → True iff intersection is non-empty (at
        least one shared token)

    The two "empty → True" cases are intentional. The gate is about
    using cheaply-available signals to skip OBVIOUSLY-irrelevant
    work; when no signal is available, the gate doesn't fire and
    the system falls back to its pre-gate behavior. Filtering on
    "no signal" would silently break the cross-turn memory the
    walker is supposed to provide.
    """
    if active_tokens is None or not active_tokens:
        return True
    if not candidate_tokens:
        return True
    return bool(active_tokens & candidate_tokens)
