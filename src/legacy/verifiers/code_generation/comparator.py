"""Comparator — verdict from code's stdout vs the claim's asserted value.

Parsing depends on the expected_output_type the prompt builder declared:

    int     → int(stdout.strip())
    float   → float(stdout.strip()), compared with math.isclose
    string  → stdout.rstrip("\\n") (preserve internal whitespace)
    bool    → "true"/"1"/"yes" → True; "false"/"0"/"no" → False
    list    → json.loads(stdout.strip())

Comparison happens deterministically here, NOT inside an LLM. That's
the second half of the firewall: the LLM never compares the claim's
asserted value to the computed value.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Literal

from src.legacy.verifiers.code_generation.claim_value import extract_claimed_value


Verdict = Literal["verified", "contradicted", "comparison_error"]


@dataclass
class ComparisonResult:
    verdict: Verdict
    claimed_value: Any = None
    computed_value: Any = None
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "claimed_value": self.claimed_value,
            "computed_value": self.computed_value,
            "explanation": self.explanation,
        }


def _parse_stdout(stdout: str, expected_type: str) -> Any:
    """Parse a single value out of stdout for the expected type.

    Raises ValueError on failure — the caller wraps it.
    """
    raw = stdout
    if expected_type == "string":
        # Preserve internal whitespace; strip only the final newline.
        if raw.endswith("\r\n"):
            return raw[:-2]
        if raw.endswith("\n"):
            return raw[:-1]
        return raw

    s = raw.strip()
    if not s:
        raise ValueError("empty stdout")
    if expected_type == "int":
        # Tolerate "42.0" → 42.
        try:
            return int(s)
        except ValueError:
            f = float(s)
            if f.is_integer():
                return int(f)
            raise ValueError(f"expected int, got fractional: {s!r}")
    if expected_type == "float":
        return float(s)
    if expected_type == "bool":
        low = s.lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        raise ValueError(f"expected bool, got {s!r}")
    if expected_type == "list":
        parsed = json.loads(s)
        if not isinstance(parsed, list):
            raise ValueError(f"expected list, got {type(parsed).__name__}")
        return parsed
    raise ValueError(f"unknown expected_type {expected_type!r}")


def _values_equal(a: Any, b: Any, expected_type: str) -> bool:
    if expected_type == "float":
        try:
            return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
        except (TypeError, ValueError):
            return False
    if expected_type == "string":
        return str(a) == str(b)
    if expected_type == "list":
        if not isinstance(a, list) or not isinstance(b, list):
            return False
        return list(a) == list(b)
    if expected_type == "bool":
        return bool(a) is bool(b)
    if expected_type == "int":
        try:
            return int(a) == int(b)
        except (TypeError, ValueError):
            return False
    return a == b


def compare(claim: dict, computed_stdout: str, expected_type: str) -> ComparisonResult:
    """Produce a verdict from the code's stdout."""
    try:
        computed = _parse_stdout(computed_stdout, expected_type)
    except (ValueError, json.JSONDecodeError) as e:
        return ComparisonResult(
            verdict="comparison_error",
            claimed_value=None,
            computed_value=None,
            explanation=(
                f"could not parse stdout as {expected_type}: "
                f"{type(e).__name__}: {e}; raw stdout = {computed_stdout!r}"
            ),
        )

    claimed = extract_claimed_value(claim)
    if claimed is None:
        return ComparisonResult(
            verdict="comparison_error",
            claimed_value=None,
            computed_value=computed,
            explanation=(
                f"could not extract a claimed value from "
                f"pattern={claim.get('pattern')!r}, "
                f"predicate={claim.get('predicate')!r}; comparator does "
                "not know which slot carries the asserted answer"
            ),
        )

    # For booleans (and only booleans), the claimed value is the *positive*
    # answer (True). Polarity tells us whether the claim is asserting that
    # the relation holds (1) or doesn't (0). Equality of computed and the
    # positive answer corresponds to "the relation holds"; we then check
    # whether that matches what the claim asserts.
    polarity = int(claim.get("polarity", 1))
    claim_asserts_positive = polarity == 1

    is_equal = _values_equal(computed, claimed, expected_type)
    relation_holds = is_equal  # "the computed value matches the asserted positive answer"
    verdict: Verdict = "verified" if relation_holds == claim_asserts_positive else "contradicted"

    pol_note = "" if claim_asserts_positive else " (claim asserted negation)"
    explanation = (
        f"claimed {claimed!r}; computed {computed!r}; equal={is_equal}{pol_note} → "
        f"{verdict}"
    )
    return ComparisonResult(
        verdict=verdict,
        claimed_value=claimed,
        computed_value=computed,
        explanation=explanation,
    )
