"""F-009 regression guard.

Phase F1 surfaced that four call-site `purpose=` strings did not match
any key in `DEFAULT_MODEL_BY_PURPOSE` — the four substrate/verifier
purposes fell through to the chat-default model in deployment instead
of using the documented `gpt-4.1-mini` default. This test ensures the
defect cannot recur: every `purpose=` literal in `src/aedos/` (except
the implicit `chat` fallback) must be a key in the table.

See docs/phase_F/deployment_readiness_audit.md F-009 for the project-
level account. The test is the CI-runnable check D26 specifies for the
v0.16 standing-pass methodology (the documented configuration must match
the code's actual call-site purposes).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aedos.llm.client import DEFAULT_MODEL_BY_PURPOSE


_SRC_DIR = Path(__file__).parent.parent.parent / "src" / "aedos"
_PURPOSE_PATTERN = re.compile(r'purpose=["\']([^"\']+)["\']')


def _collect_call_site_purposes() -> dict[str, list[str]]:
    """Scan src/aedos/ for `purpose=` literals. Returns {purpose: [files]}.
    Excludes `llm/client.py` (the client itself defines the routing layer;
    its `purpose=` references are forwarding parameters, not call-site
    purposes that get resolved against the table)."""
    found: dict[str, list[str]] = {}
    for path in _SRC_DIR.rglob("*.py"):
        if "llm" in path.parts and path.name == "client.py":
            continue
        text = path.read_text(encoding="utf-8")
        for match in _PURPOSE_PATTERN.finditer(text):
            purpose = match.group(1)
            found.setdefault(purpose, []).append(str(path.relative_to(_SRC_DIR.parent.parent)))
    return found


def test_every_call_site_purpose_is_in_default_table():
    """Every `purpose=` string used in src/aedos/ must be a key in
    DEFAULT_MODEL_BY_PURPOSE. `chat` is allowed as the implicit fallback
    (a `purpose=None` call also falls through to chat); other purposes
    must be explicit."""
    found = _collect_call_site_purposes()
    table_keys = set(DEFAULT_MODEL_BY_PURPOSE.keys())

    missing: dict[str, list[str]] = {}
    for purpose, files in found.items():
        if purpose == "chat":
            continue  # implicit fallback
        if purpose not in table_keys:
            missing[purpose] = files

    assert not missing, (
        f"F-009 regression: purposes used in src/aedos/ but not in "
        f"DEFAULT_MODEL_BY_PURPOSE: {missing}. Either add the purpose to "
        f"the table or rename the call-site to match an existing key."
    )


def test_expected_purposes_have_call_sites_or_are_reserved():
    """The inverse direction: every key in DEFAULT_MODEL_BY_PURPOSE
    either has a call site in src/aedos/ or is explicitly reserved.

    Reserved keys are kept in the table for architectural reasons even
    when no current call site uses them — see the comment on
    `extractor:assistant` in `llm/client.py`. This test enforces that
    *new* dead keys are not silently introduced.
    """
    found = _collect_call_site_purposes()
    used_purposes = set(found.keys()) | {"chat"}  # chat is always used implicitly

    reserved_purposes = {"extractor:assistant"}  # architecturally pinned, no call site

    table_keys = set(DEFAULT_MODEL_BY_PURPOSE.keys())
    unused = table_keys - used_purposes - reserved_purposes

    assert not unused, (
        f"Keys in DEFAULT_MODEL_BY_PURPOSE with no call site and not "
        f"in the reserved set: {unused}. Either remove the key, add a "
        f"call site, or add the key to `reserved_purposes` in this test "
        f"with a comment explaining why it is pinned."
    )
