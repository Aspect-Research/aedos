"""Code-generated verification (v0.4).

A python-verifiable claim is resolved by three LLM calls plus a sandbox
execution and a deterministic comparator. The three LLM calls are
arranged as a deliberate firewall:

    Stage 1 (triage)         — judge whether the claim can be resolved
                                by deterministic python at all.
    Stage 2 (prompt builder) — articulate a NEUTRAL question that does
                                NOT reveal the claimed answer.
    Stage 3 (code writer)    — write python that answers the question.
                                Sees only the neutral question; never
                                sees the claim or the asserted value.

The code is then executed in a sandbox; the comparator compares the
computed value to the claim's asserted value; the verdict is verified,
contradicted, or comparison_error.

The firewall depends on Stage 2 producing a leak-free prompt. We scan
the prompt for stringifications of the claimed value and retry once on
detection — see ``prompt_builder``.
"""

from src.verifiers.code_generation.pipeline import (
    CodeGenerationVerifier,
    CodeGenVerificationResult,
    verify_via_code_generation,
)

__all__ = [
    "CodeGenerationVerifier",
    "CodeGenVerificationResult",
    "verify_via_code_generation",
]
