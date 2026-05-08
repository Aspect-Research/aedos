"""Code-generated verification (v0.4 + v0.5).

A python-verifiable claim is resolved by two LLM calls plus a sandbox
execution and a deterministic comparator. The two LLM calls are
arranged as a deliberate firewall:

    Stage 1 (prompt builder) — articulate a NEUTRAL question that does
                                NOT reveal the claimed answer.
    Stage 2 (code writer)    — write python that answers the question.
                                Sees only the neutral question; never
                                sees the claim or the asserted value.

The code is then executed in a sandbox; the comparator compares the
computed value to the claim's asserted value; the verdict is verified,
contradicted, or comparison_error.

(v0.5) The triage stage was removed — the LLM router decides
python-verifiability before this pipeline runs. False positives surface
as ``code_execution_failed`` or ``comparison_error``.

The firewall depends on the prompt builder producing a leak-free
prompt. We scan the prompt for stringifications of the claimed value
and retry once on detection — see ``prompt_builder``.

(v0.5) ``CodeGenerationVerifier.verify_with_cross_check`` runs the
pipeline twice at different temperatures for ``python_with_canonical_
constants`` claims and compares — a cheap guard against the LLM
fabricating a stable canonical reference.
"""

from src.legacy.verifiers.code_generation.pipeline import (
    CodeGenerationVerifier,
    CodeGenVerificationResult,
    verify_via_code_generation,
)

__all__ = [
    "CodeGenerationVerifier",
    "CodeGenVerificationResult",
    "verify_via_code_generation",
]
