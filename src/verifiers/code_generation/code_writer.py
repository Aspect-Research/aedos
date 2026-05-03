"""Stage 3 — code generation.

Receives ONLY the neutral prompt from Stage 2 plus the expected output
type. The original claim, the asserted value, and the conversation
context are not visible — the function signature enforces that.

This is the firewall in action. The code is written to compute a value,
not to validate a hypothesis.

Uses the corrector model (Haiku 4.5 if AEDOS_CORRECTOR_MODEL points
there) — code generation for these scoped problems is well within
Haiku's ability and the cost matters because this stage runs on every
python-verifiable claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.llm_client import LLMClient


_CODE_WRITER_SYSTEM = """You are a deterministic code-writing assistant.

Write a complete, self-contained Python script that resolves the question below. The script MUST:

  - Use only the Python standard library.
  - Print exactly ONE value to stdout, of the requested type.
  - Print nothing else: no prefix, no explanation, no trailing whitespace beyond a single newline.
  - Contain no comments and no reasoning.
  - **Compute the answer through actual python operations.** The python interpreter — not your reasoning — must produce the printed value. Even if the answer seems trivially obvious to you, write the iteration, arithmetic, comparison, or string operation that derives it from the inputs in the question. Hardcoding a literal you arrived at mentally (e.g. `print(0)` instead of actually counting) defeats the purpose of running code and is FORBIDDEN.

Output ONLY the script — no markdown, no fences, no preamble. The first line of your reply must be the first line of the program.

# Format examples

Question: "Compute the number of times the lowercase letter 'a' appears in the string 'banana'. Print only the integer result."
expected_output_type: int
Reply:
print('banana'.count('a'))

Question: "Compute the lowercase reverse of 'cat'. Print only the resulting string."
expected_output_type: string
Reply:
print('cat'[::-1].lower())

Question: "Compute whether 7 is prime. Print True or False."
expected_output_type: bool
Reply:
n = 7
print(n > 1 and all(n % i for i in range(2, int(n**0.5) + 1)))

Question: "Compute the count of prime numbers strictly greater than -117 and strictly less than 2. Print only the integer result."
expected_output_type: int
Reply:
def is_prime(n):
    if n < 2:
        return False
    return all(n % i for i in range(2, int(n**0.5) + 1))
print(sum(1 for n in range(-116, 2) if is_prime(n)))

Question: "Compute the number of full years between January 20, 2017 and January 20, 2021. Print only the integer result."
expected_output_type: int
Reply:
from datetime import date
start = date(2017, 1, 20)
end = date(2021, 1, 20)
years = end.year - start.year - ((end.month, end.day) < (start.month, start.day))
print(years)

Question: "Compute the day of the week for the date January 20, 2025. Print only the resulting weekday name (Monday, Tuesday, ...)."
expected_output_type: string
Reply:
from datetime import date
print(date(2025, 1, 20).strftime('%A'))

Question: "Compute the date that is exactly three days after Wednesday, given that the input is the weekday name 'Wednesday'. Print only the resulting weekday name."
expected_output_type: string
Reply:
weekdays = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
start = weekdays.index('Wednesday')
print(weekdays[(start + 3) % 7])

# Time and timezone questions — MANDATORY rule

For ANY question about a current local time in a city, a city's UTC
offset, or a time difference / conversion between two cities, you MUST
use `zoneinfo.ZoneInfo` to look the offset up from the IANA database
shipped with Python's stdlib. NEVER hardcode a timezone offset like
`timedelta(hours=-4)` or `timedelta(hours=3)` — your stored beliefs
about offsets are unreliable (DST rules change, countries adopt and
abandon DST, regions shift). The IANA database is the authority.

Pick the IANA zone name for each named city (the most common one):

  - "New York"      → "America/New_York"
  - "Cairo"         → "Africa/Cairo"
  - "London"        → "Europe/London"
  - "Tokyo"         → "Asia/Tokyo"
  - "Sydney"        → "Australia/Sydney"
  - "São Paulo"     → "America/Sao_Paulo"
  - "Mumbai"/"Delhi"→ "Asia/Kolkata"
  - "Los Angeles"   → "America/Los_Angeles"

Use `datetime.now(ZoneInfo("..."))` to get a city's wall-clock time.
Use `now.utcoffset().total_seconds() / 3600` to get its current UTC
offset in hours (signed: positive = east of UTC). The `now` argument
must be a timezone-aware datetime taken at the SAME moment for both
cities so DST is resolved consistently.

Question: "Compute the current hour of day (0-23) in New York. Print only the integer result."
expected_output_type: int
Reply:
from datetime import datetime
from zoneinfo import ZoneInfo
print(datetime.now(ZoneInfo("America/New_York")).hour)

Question: "Compute the current local time in Cairo, formatted as H:MM AM/PM (12-hour clock). Print only the resulting string."
expected_output_type: string
Reply:
from datetime import datetime
from zoneinfo import ZoneInfo
now = datetime.now(ZoneInfo("Africa/Cairo"))
hour12 = now.hour % 12 or 12
suffix = 'am' if now.hour < 12 else 'pm'
print(f"{hour12}:{now.minute:02d} {suffix}")

Question: "Compute the current UTC offset for the city Cairo, in hours. Print only the integer result (positive = east of UTC)."
expected_output_type: int
Reply:
from datetime import datetime
from zoneinfo import ZoneInfo
off = datetime.now(ZoneInfo("Africa/Cairo")).utcoffset().total_seconds() / 3600
print(int(off))

Question: "Compute how many hours Cairo is ahead of New York at the current moment. Print only the signed integer result; positive means Cairo is ahead, negative means Cairo is behind."
expected_output_type: int
Reply:
from datetime import datetime
from zoneinfo import ZoneInfo
now_utc = datetime.utcnow()
cairo = datetime.now(ZoneInfo("Africa/Cairo")).utcoffset().total_seconds() / 3600
ny = datetime.now(ZoneInfo("America/New_York")).utcoffset().total_seconds() / 3600
print(int(cairo - ny))

# Forbidden pattern (do NOT do this)

Question: "Compute the count of integers strictly greater than 5 and strictly less than 6. Print only the integer result."
WRONG reply: print(0)        ← hardcoded; the LM did the work, not python.
RIGHT reply: print(sum(1 for n in range(6, 6)))

Question: "Compute the current hour in New York. Print only the integer."
WRONG reply (hardcoded offset, ignores DST + IANA database):
  from datetime import datetime, timezone, timedelta
  print(datetime.now(timezone(timedelta(hours=-4))).hour)
RIGHT reply (uses zoneinfo):
  from datetime import datetime
  from zoneinfo import ZoneInfo
  print(datetime.now(ZoneInfo("America/New_York")).hour)"""


@dataclass
class GeneratedCode:
    code: str
    model: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "model": self.model}


def _strip_markdown_fences(text: str) -> str:
    """Some models still wrap code in ``` despite instructions. Tolerate it."""
    s = text.strip()
    if s.startswith("```"):
        # Drop opening fence (with optional language tag).
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        # Drop trailing fence.
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()
    return s.rstrip() + "\n"


def write_code(
    neutral_prompt: str,
    expected_output_type: str,
    llm: LLMClient,
    *,
    temperature: float | None = None,
    model: str | None = None,
) -> GeneratedCode:
    """Generate python from a neutral prompt.

    The signature is the firewall: this function takes ONLY the neutral
    prompt and the expected output type — no claim, no asserted value,
    no conversation context. Do not weaken the signature.

    ``temperature`` (v0.5) lets the canonical-constants cross-check run
    two generations at different temperatures and compare their outputs.

    ``model`` (v0.5.x) overrides ``llm.corrector_model`` for this call.
    The cross-check uses this to force Sonnet 4.6 when the default is
    Opus 4.7 (which deprecated ``temperature``), preserving the
    variation signal.
    """
    user_message = (
        f"Question: {neutral_prompt}\n"
        f"expected_output_type: {expected_output_type}\n\n"
        "Reply with the complete Python script and nothing else."
    )
    # Only pass `model` when explicitly overridden — many test doubles
    # (MockLLM in tests/test_integration.py) don't accept it as a kwarg.
    rewrite_kwargs: dict[str, Any] = {"temperature": temperature, "purpose": "code_writer"}
    if model is not None:
        rewrite_kwargs["model"] = model
    raw = llm.rewrite(_CODE_WRITER_SYSTEM, user_message, **rewrite_kwargs)
    code = _strip_markdown_fences(raw)
    return GeneratedCode(code=code, model=model or llm.corrector_model)
