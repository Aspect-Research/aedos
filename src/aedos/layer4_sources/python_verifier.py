"""Python verifier for Aedos.

Layer 4 source for the `python` route (architecture §6.3). Generates
Python verification code via the LLM and executes it in the sandbox
defined in `aedos.utils.sandbox`. See that module's docstring for the
threat model and the explicit list of what the sandbox blocks and does
not block.

**Security boundary in writing.** The sandbox is designed against
LLM-generated wrong code (the common case), not against an active
attacker crafting input to escape the sandbox. Production deployments
handling adversarial input must upgrade to a stronger sandbox (see
`aedos.utils.sandbox` for the upgrade path).

The walker gates invocation of this verifier on the predicate's
`routing_hint == "python"` (architecture §6.5 step 3).
The structural test
(`tests/unit/test_layer4_routing_invariants.py`) enforces the gate as
a CI invariant.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..layer1_extraction.extractor import Claim
from ..utils.sandbox import run_code
from .walker import _parse_quantity


PYTHON_VERIFY_TOOL: dict[str, Any] = {
    "name": "generate_python_verify",
    "description": (
        "Generate a Python function to verify a factual claim via computation. "
        "Use only allowed stdlib: datetime, math, decimal, fractions, statistics, "
        "re, unicodedata, string."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "Python code defining: def verify(subject: str, predicate: str, obj: str, "
                    "premises: dict) -> Optional[bool]. `premises` is a dict of FETCHED facts "
                    "keyed by slot name ('subject', 'object'), each a {'value': <str>} entry — "
                    "e.g. {'subject': {'value': '1643'}, 'object': {'value': '1879'}} for a "
                    "born-before comparison. When premises is empty, compute from the three "
                    "literal slots alone. Return True if the claim deterministically holds, "
                    "False if it deterministically does not, or None if verification is "
                    "inherently uncertain — speculative numerical estimates, time-varying "
                    "values without timestamps, contested claims, a MISSING/empty required "
                    "premise, or anything you cannot compute from the allowed stdlib alone. "
                    "Phase 10.5 §3.2 soundness invariant: prefer None over a guessed "
                    "True/False — a missing premise MUST return None, never a guess."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "Brief explanation of the verification approach.",
            },
        },
        "required": ["code", "reasoning"],
    },
}

_SYSTEM_PROMPT = (
    "You are a Python code generator for factual claim verification. "
    "Given a claim (subject, predicate, object), write a Python function "
    "that returns True if the claim holds, False if it does not, or "
    "None if the claim is inherently uncertain. Examples of None-eligible "
    "claims: speculative numerical estimates (\"grains of sand exceeds 7 "
    "quintillion\"), time-varying values without a timestamp (\"current "
    "stock price\"), or anything you cannot deterministically compute. "
    "Soundness invariant: prefer None over a guessed True/False — the "
    "downstream system will route uncertainty to abstention, which is "
    "always safer than a fabricated verdict. "
    "Allowed imports: datetime, math, decimal, fractions, statistics, re, unicodedata, string. "
    "No other imports. Function signature: "
    "def verify(subject: str, predicate: str, obj: str, premises: dict) -> Optional[bool]. "
    "`premises` carries FETCHED facts the comparison needs (keyed by slot name, each "
    "{'value': <str>}); it is empty when the claim is computable from the three literal "
    "slots alone. A required premise that is missing or empty MUST yield None (abstain) — "
    "never fabricate or guess a premise value."
)

_SANDBOX_TIMEOUT = 5


@dataclass
class PythonVerdict:
    verdict: str  # verified | contradicted | no_terminal_result
    generated_code: str = ""
    inputs: dict = field(default_factory=dict)
    output: Any = None
    runtime_metadata: dict = field(default_factory=dict)


def _extract_code_block(text: str) -> str:
    """Strip markdown fences if present; return raw code."""
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


# ---------------------------------------------------------------------------
# v0.16.1 WS6 — deterministic front-end (item 6b)
#
# A narrow, exact-parse evaluator tried BEFORE the LLM codegen for the common
# self-contained comparison / arithmetic / date-ordering claims the Python tier
# exists for (e.g. "100 greater_than 50", "10 squared 100"). Today those rely on
# one-shot LLM codegen that is flaky run-to-run; this grounds them
# deterministically. §3.2 paramount: a verdict is returned ONLY on a TOTAL,
# UNAMBIGUOUS parse over an EXACT computation (real arithmetic — it can never
# false-verify). ANYTHING not fully parsed returns None, and verify() then
# proceeds to the existing LLM-codegen path exactly as before (the None-eligible
# / fail-open framing is preserved untouched).
# ---------------------------------------------------------------------------

# A STRICT numeric operand: the whole (trimmed) string must be a number with an
# optional recognized magnitude word, and nothing else. This is the totality
# gate the front-end requires — the shared `_parse_quantity` (reused from the
# walker's kb_quantitative path for the "2 million" / "60 million" suffix logic)
# is intentionally LENIENT (it grabs the leading numeric token, so "Q123" -> 123,
# "144 apples" -> 144), which is right for a KB threshold but UNSOUND for an
# operand. We anchor-match first, then delegate the value computation (incl. the
# suffix multipliers) to `_parse_quantity` so the magnitude handling stays in one
# place and cannot drift.
# Either plain digits with an optional fractional part, OR digit-groups joined
# by PROPER thousands separators (groups of exactly 3), optionally followed by a
# single recognized magnitude word. Crucially this REJECTS a comma-separated
# list like "1,2,3,4,5" (groups are not 3 digits) — that must fall through to
# codegen, never be read as the single number 12345.
_STRICT_NUMBER_RE = re.compile(
    r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
    r"(?:million|billion|thousand|hundred|m|bn|k)?",
    re.IGNORECASE,
)

_BARE_YEAR_RE = re.compile(r"[-+]?\d{1,4}")


def _strict_number(text: Optional[str]) -> Optional[float]:
    """Parse `text` to a number ONLY if the ENTIRE trimmed string is a number
    (with an optional magnitude suffix); else None. Reuses `_parse_quantity`
    for the value/suffix computation, adding the anchored totality gate the
    deterministic front-end needs so a partial / contaminated operand never
    yields a (mis)computed verdict."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    if not _STRICT_NUMBER_RE.fullmatch(s):
        return None
    return _parse_quantity(s)


def _strict_year(text: Optional[str]) -> Optional[int]:
    """Parse a bare 1-4 digit year ONLY if the entire trimmed string is that
    year (optionally signed); else None. Deliberately stricter than a free-text
    date parser — date/year ordering is sound only when both operands are
    unambiguous bare years."""
    if text is None:
        return None
    s = str(text).strip()
    if not s or not _BARE_YEAR_RE.fullmatch(s):
        return None
    try:
        return int(s)
    except ValueError:
        return None


# Comparator vocabulary. Mirrors the walker's kb_quantitative comparator parsing
# (greater_than/more_than/above -> gt; less_than/below/fewer_than -> lt) and
# extends it with the equals / at_least / at_most family and the `measure_`
# prefix variants the metadata oracle emits, so the deterministic front-end
# stays consistent with how comparators are named elsewhere. Ordered longest /
# most-specific first; the substring search is on the lowercased predicate.
#   gt  : strictly greater          lt  : strictly less
#   ge  : greater-or-equal          le  : less-or-equal
#   eq  : equal
_COMPARATORS: tuple[tuple[str, Optional[str]], ...] = (
    ("greater_than_or_equal", "ge"),
    ("less_than_or_equal", "le"),
    ("at_least", "ge"),
    ("at_most", "le"),
    ("greater_than", "gt"),
    ("less_than", "lt"),
    ("more_than", "gt"),
    ("fewer_than", "lt"),
    ("not_equal", None),  # explicitly UNsupported -> fall through (None)
    ("equals", "eq"),
    ("equal_to", "eq"),
    ("above", "gt"),
    ("below", "lt"),
)

_COMPARE = {
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
    "ge": lambda a, b: a >= b,
    "le": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}


# Arithmetic-operator tokens. A predicate carrying any of these is an
# arithmetic predicate (e.g. "plus_30_equals", "birth_year_plus_30"), NOT a pure
# comparator — so the comparator path must NOT hijack it on an incidental
# `equals` substring. Such predicates route to `_arithmetic_verdict`, which only
# returns a verdict on the unary forms it can parse totally and otherwise
# abstains (None -> codegen).
_ARITH_TOKENS = ("squared", "cubed", "plus", "minus", "times", "multiplied", "divided")


def _comparator_of(pred_lower: str) -> Optional[str]:
    """Return the comparator key ('gt'/'lt'/'ge'/'le'/'eq') named by the
    predicate, or None if the predicate names none unambiguously. The
    `not_equal` family maps to None on purpose: "==" floats are exact but "!="
    over the strict-number path is left to codegen to avoid surprising the
    existing corpus; over-abstention here is safe. A predicate that ALSO carries
    an arithmetic-operator token is treated as arithmetic, not comparison
    (returns None here), so "plus_30_equals" never matches the `equals`
    comparator on the bare subject/object."""
    if any(tok in pred_lower for tok in _ARITH_TOKENS):
        return None
    for token, comp in _COMPARATORS:
        if token in pred_lower:
            return comp  # may be None (explicitly unsupported)
    return None


# before/after over two years. "before" -> subject year strictly < object year.
def _date_order_comparator(pred_lower: str) -> Optional[str]:
    if "before" in pred_lower or "earlier_than" in pred_lower or "older_than" in pred_lower:
        return "lt"
    if "after" in pred_lower or "later_than" in pred_lower or "younger_than" in pred_lower:
        return "gt"
    return None


# Simple arithmetic over the predicate, e.g. predicate "squared" / "plus_5" or
# "N squared is K". We support ONLY the closed, unambiguous operator set below
# and require an exact integer/float match. Anything else -> None.
_ARITH_OPS = {
    "squared": lambda n, _m: n * n,
    "cubed": lambda n, _m: n * n * n,
    "plus": lambda n, m: n + m,
    "minus": lambda n, m: n - m,
    "times": lambda n, m: n * m,
    "multiplied_by": lambda n, m: n * m,
    "divided_by": lambda n, m: (n / m) if m != 0 else None,
}


def _arithmetic_verdict(subject: str, predicate: str, obj: str) -> Optional[str]:
    """Evaluate "N <op> [M] is K" forms where N=subject, K=object, and the
    operator (+ optional embedded operand M) is named by the predicate, e.g.
    predicate="squared" ("10 squared 100"), "plus_30_equals"? -> NO (M embedded
    in the predicate name as a separate token is NOT parsed here — that path has
    an ambiguous operand and is left to codegen). We handle:
      * unary ops (squared/cubed): subject and object both strict numbers.
      * binary ops where the SECOND operand is the predicate-embedded literal IS
        intentionally NOT supported (ambiguous tokenization) -> None.
    Returns 'verified'/'contradicted'/None."""
    n = _strict_number(subject)
    k = _strict_number(obj)
    if n is None or k is None:
        return None
    pred = predicate.lower()
    # Strip trailing result-connectives so "squared_equals"/"squared_is" match.
    op_token = None
    for token in _ARITH_OPS:
        if token in pred:
            op_token = token
            break
    if op_token is None:
        return None
    # Unary ops only (squared/cubed): a binary op needs a second operand, but the
    # ONLY operands present (subject, object) are already N and K — a binary form
    # like "N plus M is K" would need M, which lives nowhere unambiguous here, so
    # binary ops abstain (None -> codegen) unless the op is unary.
    if op_token not in ("squared", "cubed"):
        return None
    fn = _ARITH_OPS[op_token]
    result = fn(n, None)
    if result is None:
        return None
    holds = result == k
    return "verified" if holds else "contradicted"


# Exact string-property counting (vowel / consonant / letter / character / word
# count over the SUBJECT string) — the deterministic counterpart of LLM codegen
# for "the word 'superstrawberry' has 4 vowels". The subject is the string, the
# object the claimed count. §3.2: CONTRADICT only when the claimed count matches
# NO reasonable interpretation — the y-as-vowel/consonant and spaces-in-character
# ambiguities are absorbed into a SET of acceptable counts, so a claim correct
# under any common reading is never false-contradicted.
_VOWELS = frozenset("aeiou")
_VOWELS_Y = frozenset("aeiouy")
_COUNT_MEASURES = ("vowel", "consonant", "character", "letter", "word")
_DESCRIPTOR_RE = re.compile(r"(?i)^the\s+(?:word|phrase|string|name|term|text)\s+(.*)$")


def _count_measure_of(pred_lower: str) -> Optional[str]:
    """The measure named by a COUNT predicate (`vowel_count`, `count_vowels`,
    `number_of_letters`, …), or None when it is not a recognized string-count
    predicate. Strict: an arbitrary predicate that merely contains 'letter' (e.g.
    'wrote_letter') does NOT match. `syllable_count` is deliberately absent —
    syllable counting is heuristic, not exact, so it is left to codegen (where it
    typically abstains)."""
    for m in _COUNT_MEASURES:
        if pred_lower in (
            f"{m}_count", f"count_{m}", f"count_{m}s",
            f"number_of_{m}s", f"num_{m}s", f"{m}s_count",
        ):
            return m
    return None


def _count_target(subject: Optional[str]) -> str:
    """The string to count over: strip a 'the word/phrase/string "X"' descriptor
    and surrounding quotes, so a subject of `the word 'superstrawberry'` counts
    over `superstrawberry`, not the wrapper. The extractor emits the bare word;
    this is defense for the wrapped shape."""
    s = (subject or "").strip()
    m = _DESCRIPTOR_RE.match(s)
    if m:
        s = m.group(1).strip()
    return s.strip("'\"“”‘’").strip()


def _claimed_count(obj: Optional[str]) -> Optional[int]:
    """The leading non-negative integer of the object slot ('4' or '4 vowels' →
    4); None when the object has no leading integer ('four' → codegen)."""
    m = re.match(r"\s*(\d+)", str(obj or ""))
    return int(m.group(1)) if m else None


def _acceptable_counts(measure: str, text: str) -> set:
    """The set of exact counts that make the claim TRUE, spanning the benign
    ambiguities (y as vowel/consonant; characters with/without spaces)."""
    low = text.lower()
    if measure == "vowel":
        return {sum(c in _VOWELS for c in low), sum(c in _VOWELS_Y for c in low)}
    if measure == "consonant":
        alpha = [c for c in low if c.isalpha()]
        return {
            sum(c not in _VOWELS for c in alpha),    # y counts as a consonant
            sum(c not in _VOWELS_Y for c in alpha),  # y counts as a vowel
        }
    if measure == "letter":
        return {sum(1 for c in text if c.isalpha())}
    if measure == "character":
        return {len(text), sum(1 for c in text if not c.isspace())}
    if measure == "word":
        return {len(text.split())}
    return set()


def _string_count_verdict(claim: Claim) -> Optional[str]:
    """'verified'/'contradicted' for a string-count claim, computed EXACTLY over
    the subject literal; None (→ codegen) when it is not a count predicate, the
    target is empty, or the object carries no integer count. Counting is always
    over the claim's literal slots, never a fetched premise."""
    measure = _count_measure_of((claim.predicate or "").lower())
    if measure is None:
        return None
    target = _count_target(claim.subject)
    if not target:
        return None
    claimed = _claimed_count(claim.object)
    if claimed is None:
        return None
    acceptable = _acceptable_counts(measure, target)
    if not acceptable:
        return None
    return "verified" if claimed in acceptable else "contradicted"


def _deterministic_verdict(claim: Claim, premises: Optional[dict] = None) -> Optional[str]:
    """Try a TOTAL, EXACT, deterministic parse+evaluation of `claim`. Returns
    'verified' / 'contradicted' on a fully-parsed exact computation, else None
    (the caller then proceeds to the existing LLM codegen). §3.2: this can never
    false-verify — every returned verdict is real arithmetic over operands that
    parsed in their ENTIRETY; any ambiguity (partial parse, unrecognized
    comparator/op, non-numeric operand) returns None and falls through.

    Polarity is honored via the same flip the walker uses elsewhere: a negated
    claim ("X is NOT greater than Y") inverts the computed verdict.

    `premises` (WS3b) may carry fetched operand values keyed by slot
    ('subject'/'object', each {'value': <str>}); when present and parseable they
    are used IN PLACE OF the literal slot for the comparison/ordering paths
    (e.g. born_before resolves both birth years as premises). A premise that is
    present but does NOT parse strictly is treated as "no usable premise" for
    that slot -> the path abstains (None), never guesses."""
    predicate = (claim.predicate or "")
    pred_lower = predicate.lower()

    # Resolve operand values: prefer a strictly-parseable premise value for the
    # slot, else the literal slot. (None when neither parses for the path that
    # needs it.)
    def _premise_raw(slot: str) -> Optional[str]:
        if isinstance(premises, dict):
            entry = premises.get(slot)
            if isinstance(entry, dict) and "value" in entry:
                return str(entry["value"])
        return None

    subj_raw = _premise_raw("subject")
    obj_raw = _premise_raw("object")
    subject = subj_raw if subj_raw is not None else claim.subject
    obj = obj_raw if obj_raw is not None else claim.object

    verdict: Optional[str] = None

    # 1) Numeric comparison (greater_than / less_than / at_least / at_most /
    #    equals / measure_* family). Both operands must be STRICT numbers.
    comp = _comparator_of(pred_lower)
    if comp is not None:
        a = _strict_number(subject)
        b = _strict_number(obj)
        if a is not None and b is not None:
            holds = _COMPARE[comp](a, b)
            verdict = "verified" if holds else "contradicted"

    # 2) Date / year ordering (before / after) over two STRICT years. Only tried
    #    when the predicate did not already name a numeric comparator (so a
    #    predicate like "population_greater_than" never enters the year path).
    if verdict is None:
        order = _date_order_comparator(pred_lower)
        if order is not None:
            ya = _strict_year(subject)
            yb = _strict_year(obj)
            if ya is not None and yb is not None:
                holds = _COMPARE[order](ya, yb)
                verdict = "verified" if holds else "contradicted"

    # 3) Simple exact arithmetic ("N squared K", "N cubed K").
    if verdict is None:
        verdict = _arithmetic_verdict(subject, predicate, obj)

    # 4) Exact string-property counting (vowel/consonant/letter/character/word
    #    count over the SUBJECT literal vs the claimed object count). Uses the
    #    claim's literal slots — counting is never over a fetched premise.
    if verdict is None:
        verdict = _string_count_verdict(claim)

    if verdict is None:
        return None

    # Honor polarity (a negated comparison inverts the deterministic verdict).
    if getattr(claim, "polarity", 1) == 0:
        verdict = "contradicted" if verdict == "verified" else "verified"
    return verdict


class PythonVerifier:
    def __init__(self, sandbox=None, llm_client=None) -> None:
        self._sandbox = sandbox  # unused — module-level run_code() used instead
        self._llm = llm_client

    def verify(self, claim: Claim, premises: Optional[dict] = None) -> PythonVerdict:
        """Generate and run a Python verifier for `claim`.

        v0.16.1 WS3b (premise -> Python channel): `premises` is an OPTIONAL dict
        of FETCHED facts the comparison computes over, keyed by slot name
        ('subject' / 'object'), each value a small JSON-serializable dict
        (`{'value': <str>, 'source': ..., 'kb_property': ...}`). The walker
        gathers these from KB/Tier-U for a `routing_hint='python'` comparison
        predicate whose metadata declares `premise_properties`. The resolved
        premise values are threaded into BOTH the codegen prompt AND the
        generated `def verify(subject, predicate, object, premises)` call, so
        the generated code can compute over fetched facts (e.g. two birth
        years). premises=None (the default) preserves the EXACT prior behavior:
        the generated verify() sees only the claim's three literal slots and an
        empty premises dict. The generated code stays None-eligible — a missing
        premise / exception still routes to abstain, never a fabricated verdict.
        """
        inputs = {
            "subject": claim.subject,
            "predicate": claim.predicate,
            "object": claim.object,
        }
        # Only JSON-serializable premise dicts survive into the sandbox literal;
        # a non-dict / non-serializable premises arg is treated as "no premises"
        # (fail-safe — never crash the verifier on a malformed premise channel).
        premises_payload: dict = {}
        if isinstance(premises, dict):
            try:
                json.dumps(premises)
                premises_payload = premises
            except (TypeError, ValueError):
                premises_payload = {}
        if premises_payload:
            inputs["premises"] = premises_payload

        # v0.16.1 WS6: deterministic front-end, tried FIRST. For the common
        # self-contained comparison / arithmetic / date-ordering claims this
        # grounds the verdict by EXACT computation (real arithmetic over fully-
        # parsed operands), bypassing the flaky one-shot LLM codegen. §3.2: it
        # returns None for ANYTHING not totally + unambiguously parsed, in which
        # case verify() proceeds to the existing codegen path below exactly as
        # before. It needs no LLM, so it runs even when self._llm is None.
        det = _deterministic_verdict(claim, premises_payload or None)
        if det is not None:
            return PythonVerdict(
                verdict=det,
                inputs=inputs,
                output=det,
                runtime_metadata={"deterministic": True},
            )

        if self._llm is None:
            return PythonVerdict(verdict="no_terminal_result", inputs=inputs)

        # LLM code generation
        premise_line = ""
        if premises_payload:
            premise_line = (
                f"\nFetched premises (compute over these, keyed by slot): "
                f"{json.dumps(premises_payload)}\n"
                "If any premise your computation needs is missing or empty, return None."
            )
        user_msg = (
            f"Claim: subject={claim.subject!r}, predicate={claim.predicate!r}, object={claim.object!r}"
            f"{premise_line}\n"
            "Generate the verify() function."
        )
        try:
            tool_result = self._llm.extract_with_tool(
                _SYSTEM_PROMPT,
                user_msg,
                PYTHON_VERIFY_TOOL,
                max_tokens=1024,
                purpose="python_verifier",
            )
        except Exception as exc:
            return PythonVerdict(
                verdict="no_terminal_result",
                inputs=inputs,
                runtime_metadata={"exception_info": str(exc)},
            )

        raw_code = tool_result.get("code", "")
        if not raw_code:
            return PythonVerdict(verdict="no_terminal_result", inputs=inputs, generated_code="")

        code = _extract_code_block(raw_code)

        # Build harness — distinguishes None (uncertain → no_terminal_result)
        # from truthy (verified) and falsy non-None (contradicted) so the
        # verify function can honestly abstain on speculative or unverifiable
        # claims. Pre-Phase-10.5 behavior treated None as falsy → contradicted,
        # which forced the generator into a fabricated False on uncertainty
        # (§3.2 soundness violation); the None branch corrects that.
        #
        # v0.16.1 WS3b: the harness ADAPTIVELY passes the fetched `premises`
        # dict as a 4th positional arg when the generated verify() accepts it
        # (co_argcount >= 4). Legacy 3-arg code (the entire existing test
        # corpus and any prompt the model answers with the old signature) is
        # called with exactly three args, so premises=None reproduces the prior
        # behavior byte-for-byte. The premises literal is JSON (validated
        # serializable above), embedded as a Python dict literal.
        harness = (
            f"{code}\n"
            f"_premises = {premises_payload!r}\n"
            f"_args = ({claim.subject!r}, {claim.predicate!r}, {claim.object!r})\n"
            "_argc = getattr(getattr(verify, '__code__', None), 'co_argcount', 3)\n"
            "if _argc >= 4:\n"
            "    _result = verify(*_args, _premises)\n"
            "else:\n"
            "    _result = verify(*_args)\n"
            "if _result is None:\n"
            "    print('NONE')\n"
            "elif _result:\n"
            "    print('TRUE')\n"
            "else:\n"
            "    print('FALSE')\n"
        )

        sandbox_result = run_code(harness, timeout_seconds=_SANDBOX_TIMEOUT)

        runtime_metadata: dict[str, Any] = {
            "runtime_ms": sandbox_result.duration_ms,
        }
        if sandbox_result.import_violation:
            runtime_metadata["import_violation"] = sandbox_result.import_violation
            return PythonVerdict(
                verdict="no_terminal_result",
                generated_code=code,
                inputs=inputs,
                output=None,
                runtime_metadata=runtime_metadata,
            )
        if sandbox_result.timed_out:
            runtime_metadata["timed_out"] = True
            return PythonVerdict(
                verdict="no_terminal_result",
                generated_code=code,
                inputs=inputs,
                output=None,
                runtime_metadata=runtime_metadata,
            )
        if not sandbox_result.success:
            runtime_metadata["exception_info"] = sandbox_result.stderr.strip()
            return PythonVerdict(
                verdict="no_terminal_result",
                generated_code=code,
                inputs=inputs,
                output=sandbox_result.stdout,
                runtime_metadata=runtime_metadata,
            )

        raw_out = sandbox_result.stdout.strip()
        if raw_out == "TRUE":
            verdict = "verified"
        elif raw_out == "FALSE":
            verdict = "contradicted"
        elif raw_out == "NONE":
            verdict = "no_terminal_result"
        else:
            verdict = "no_terminal_result"
            runtime_metadata["unexpected_output"] = raw_out

        return PythonVerdict(
            verdict=verdict,
            generated_code=code,
            inputs=inputs,
            output=raw_out,
            runtime_metadata=runtime_metadata,
        )
