"""Session-scope detection (v0.14, ported from v0.7.14).

When the user makes an assertion that's explicitly bounded to the
current conversation ("for this conversation, X = 5"; "let's say Y";
"in our discussion, Z"), the assertion shouldn't pollute the
cross-session user store — it's a session-local microtheory entry
under the v0.14 session model, true here and nowhere else.

Detection is regex-based on source_text. Cheap (no LLM call), fast,
and conservative-leaning:

  * False POSITIVES (treating a cross-session claim as session-scoped)
    just mean the claim doesn't carry over to future sessions. The
    user can re-assert it then. Annoying but safe.
  * False NEGATIVES (missing an explicitly-session phrasing) keep
    the existing cross-session behavior. The session-local pile just
    stays empty for that claim. Safe.

The patterns target unambiguously session-bounded language. We
deliberately don't match things like "today" alone (too common; would
catch every "I went to the store today" as session-scoped).

Phase 6 port: this is a verbatim port of v1's
``src/session_markers.py``. The v1 marker set has been calibrated
against real conversations; re-deriving it from the architecture
document's single example would lose accumulated wisdom. The
``find_session_marker`` helper is new in v2 — Tier U's storage path
needs the matched phrase for the ``tier_u_storage`` event payload.
"""

from __future__ import annotations

import re

# Phrases that explicitly bound an assertion to the current
# conversation. Each must contain a clear "this/our + conversation-
# noun" or a hypothetical/temporary marker.
SESSION_SCOPE_MARKERS = re.compile(
    r"\b("
    # "for this conversation", "in our discussion", etc.
    r"(?:for|in)\s+(?:this|our)\s+(?:conversation|chat|discussion|session|exchange|talk|thread|context)"
    # "let's say X", "let's assume X", "let's pretend X", "let's suppose X"
    r"|let'?s\s+(?:say|assume|pretend|suppose|imagine|pick|use|treat|set)"
    # explicit hypotheticals + temporaries
    r"|hypothetically(?:\s+speaking)?"
    r"|just\s+for\s+(?:now|this|the\s+sake\s+of)"
    r"|temporarily|for\s+the\s+(?:purposes\s+of|moment)"
    # "right now" + "as of this conversation"
    r"|right\s+now\s+(?:i'?m|we'?re|i\s+am|we\s+are)"
    r"|as\s+of\s+(?:this|our)\s+(?:conversation|chat|discussion)"
    # "in this scenario/case/example/setup"
    r"|in\s+this\s+(?:scenario|case|example|setup|setting|context)"
    # "for the next N turns / today's session"
    r"|for\s+the\s+next\s+(?:few\s+)?(?:turn|message|response)"
    r"|today'?s\s+session"
    r")\b",
    re.IGNORECASE,
)


def is_session_scoped(source_text: str | None) -> bool:
    """Return True if the source text contains a session-bounding
    marker. Used by Tier U's storage path to decide whether a user
    assertion is session-local or cross-session.

    Example positives:
      "for this conversation, X = 5"           → True
      "let's say A is the protagonist"         → True
      "hypothetically, the cat is black"       → True
      "in this scenario, the budget is $100"   → True

    Example negatives:
      "I like peanut butter"                   → False
      "Tokyo is a city in Japan"               → False
      "I went to the store today"              → False  (no session noun)
      "Right now I'm tired"                    → True   (right now + i'm)
    """
    if not source_text:
        return False
    return bool(SESSION_SCOPE_MARKERS.search(source_text))


def find_session_marker(source_text: str | None) -> str | None:
    """Return the matched marker phrase, or None if no marker is
    present. Same regex as ``is_session_scoped``; this helper exists
    so Tier U's storage path can record the exact phrase that fired
    in the ``tier_u_storage`` pipeline event for trace-UI debugging.

    A non-None return value implies ``is_session_scoped`` would have
    returned True for the same input.
    """
    if not source_text:
        return None
    m = SESSION_SCOPE_MARKERS.search(source_text)
    return m.group(0) if m else None
