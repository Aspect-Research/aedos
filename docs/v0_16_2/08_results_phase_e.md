# v0.16.2 — Phase E Results (4 verification-quality fixes + adversarial hardening)

Phase E started from a live "the pope" chat with two visible failures: the draft
named a former/wrong pope and Aedos could not verify a true, simple birth date.
Four fixes were scoped soundness-first (§3.2: never false-verify, never
false-contradict; abstain is safe), then the two §3.2-critical ones (E2, E4) went
through an adversarial multi-agent review that surfaced **five** additional
soundness holes — all fixed before commit. Plan: `07_plan_phase_e.md`.

## The four fixes

### E1 — chat walker budget restored to 30 s (cure over-abstention)
`deploy/backend/settings.py`: `walker_wall_clock_seconds` 12 → 30 (env default
`AEDOS_WALKER_WALL_CLOCK_SECONDS` "12" → "30"). Phase B lowered it for chat
responsiveness, but Phase C made verification PARALLEL — a turn's wall-time is
~max(per-claim), not the sum — so the lower budget only bought `budget_wall_clock`
abstains on simple lookups (the birth-date walk timed out at 12 s). Project order
is soundness > coverage > simplicity > **latency**.

### E3 — selector always keeps the identity/role claim central
`src/aedos/deployment/claim_selection.py`: the central-claim selector had dropped
"Pope Francis holds_role Pope" as peripheral, so the wrong-pope claim was never
verified. The selector prompt now ALWAYS includes the claim(s) establishing the
answer's core identity / role / title / office, because the correctness of the
rest of the answer depends on it. Knowledge lives in the prompt (no hardcoded
allowlist). Pinned by a prompt-contract test.

### E2 — precision-aware natural-language date comparison §3.2
`src/aedos/layer4_sources/kb_verifier.py`: a date object like "December 17, 1936"
never matched Wikidata `1936-12-17`, so even with budget the birth date abstained.
New `_date_parts` (dateutil, with precision = which of year/month/day the string
actually specifies), `_date_precision`, and `_date_relation`, consumed by
`_value_matches`. Rules (§3.2-safe):
- **match** when the claim is no finer than the KB's *trustworthy* precision and
  agrees at every precision the claim asserts ("December 17, 1936" vs `1936-12-17`;
  "1998" vs `1998-09-04`).
- **mismatch** (contradiction-eligible) ONLY on a YEAR disagreement — Wikidata
  years are never placeholders.
- **abstain** otherwise — a month/day difference (KB may be a placeholder), a claim
  finer than the KB, comparison phrases ("before 1800"), or non-dates.

### E4 — temporal currency for role/state claims §3.2-CRITICAL
`src/aedos/layer4_sources/kb_verifier.py`: `holds_role`→P39 is multi-valued with
P580/P582 (start/end) qualifiers. `_scope_compatible` used to return True for a
present-tense claim against an ENDED statement, so "Francis holds_role Pope"
verified off his ended P39. Two levels (both gated to entity role/state values):
- **Level 1** (`_scope_compatible`): a present-currency claim cannot verify off a
  statement whose end is not provably in the future, nor off one whose start is
  provably in the future. Past claims (`valid_until == BEFORE_PRESENT`) still verify
  off ended statements ("X was the pope").
- **Level 2** (`_compare_positive`): a strictly present-tense (fully unscoped) claim
  whose value matched ONLY provably-ended statements, with no current match, is
  CONTRADICTED with the end date — the wrong-pope catch. Strict gates close each
  enumerated false-contradict risk (future end, multiple periods, ongoing role,
  historical claim, value resolution).

Helpers `_date_bounds` / `_end_provably_past` / `_end_provably_future` /
`_start_provably_future` do precision-aware ordering vs `current_time` (a
year-precision end is provably past only once the whole year has elapsed).

## Adversarial review of E2 + E4 — five confirmed holes, all fixed

A 4-lens find→refute workflow (E4-L1 false-verify, E4-L2 false-contradict, E2 date
relation, cross-cutting) probed 7 findings; 5 survived independent refutation. All
were patched and pinned with tests before commit.

| # | Direction | Hole | Fix |
|---|---|---|---|
| 1 | **false-verify** (critical) | E4 L1 only checked the END (P582); a statement with a **future START** (P580) — a role not yet begun (announced succession, scheduled term) — verified a present claim | `_start_provably_future` + a start-side gate in `_scope_compatible` |
| 2,5 | **false-contradict** | E4 L2 wasn't gated to entity object_type; a date/quantity predicate carrying a stray P582 on a value-MATCHING statement could contradict a TRUE claim | gate currency logic (L1 + L2 + tracking) to `object_type == "entity"`; a date predicate ignores stray P580/P582 |
| 3 | **false-verify** (critical) | BCE era sign dropped: `-1200` (1200 BC) matched `1200` (CE), ~2400 yr apart, because dateutil discards the leading `-` | `_date_parts` carries a NEGATIVE year for BCE; `_date_bounds` rejects year < 1 |
| 4 | **false-verify** (high) | a day/month-precise claim ("January 1, 2020") verified against a Wikidata precision **placeholder** (`2020-01-01` may really be year-precision) | `_date_relation` caps KB *effective* precision down over placeholder-coincident components (day == 1 → month; +month == 1 → year) |

The two dismissed findings were refuted as safe-abstain / unreachable.

Note on the placeholder cap (E2/finding 4): because the SPARQL path does not capture
`wikibase:timePrecision`, the cap conservatively distrusts a KB day of 1 (and month
of 1), so a claim of exactly "January 1" / a month's 1st against such a value
abstains. This is bounded over-abstention (only day-1 / Jan-1 KB dates) traded for
closing a false-verify — the §3.2-safe direction. Capturing `wikibase:timePrecision`
end-to-end (SPARQL + Statement + verifier) is the cleaner long-term fix; deferred,
since the seeded offline substrate would also need the precision field.

## Verification

- Full offline gated suite (the §3.2 regression net):
  `py -3 -m pytest tests/unit tests/integration tests/deploy --ignore=tests/integration/live -q`
  → **1720 passed, 1 xfailed, 1 xpassed** (the xfail/xpass are the pre-existing v0.15
  sandbox encoded-dunder boundaries, unrelated to Phase E).
- New tests: `TestNaturalLanguageDateMatching`, `TestBceEra`, `TestDatePlaceholderCap`,
  `TestEndDateOrdering`, `TestTemporalCurrencyRoles` (incl. future-start, fully-future
  interval, past-claim-vs-future-start, date-predicate-stray-end, negated present
  role), plus the E3 prompt-contract test and the E2 pope-birthdate end-to-end pin.

### Live-data smoke (real Wikidata, no LLM)
The LLM extraction/translation layer is unchanged by Phase E; the changed verdict
logic was exercised against LIVE Wikidata SPARQL for the actual reported subject:
- Benedict XVI P569 birth = `1927-04-16T00:00:00Z`; pope (Q19546) P39 =
  `P580 2005-04-19 → P582 2013-02-28` (an ended role) — exactly the data shape the
  fixes target.
- **E2**: "April 16, 1927" (NL) and "1927" both VERIFY against the live KB date;
  "April 17, 1927" abstains. The original "couldn't verify the birth date" failure
  is fixed.
- **E4**: with the live ended-role qualifiers, a present "is the pope" claim →
  CONTRADICTED; a past "was the pope" claim → VERIFIED. The wrong-pope false-verify
  is fixed without contradicting history.
(The full live chat smoke — extraction + draft — needs an LLM key, absent in this
environment; the offline gated suite is the soundness gate and the live SPARQL
smoke confirms the real data shape.)

## Commits (branch v0.16.2; NOT tagged / NOT pushed — awaiting operator)
- E1 — restore chat walker budget to 30 s
- E3 — selector always keeps the identity/role claim central
- E2 + E4 + the five review fixes — precision-aware dates + temporal currency
