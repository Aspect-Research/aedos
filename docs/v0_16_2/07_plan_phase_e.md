# v0.16.2 — Phase E Plan (4 verification-quality fixes)

From a live chat ("the pope"): the draft named the wrong/former pope and Aedos
couldn't verify a true, simple birth date. Four fixes, scoped soundness-first
(§3.2: never false-verify, never false-contradict; abstain is safe). Branch
`v0.16.2`. Build-verify-build, with adversarial review of the §3.2-critical ones
(E2, E4).

## E1 — raise the chat walker budget (cure over-abstention)
**Now:** `deploy/backend/settings.py` `walker_wall_clock_seconds=12.0` (engine
default 30 in `config.py`), env `AEDOS_WALKER_WALL_CLOCK_SECONDS` default "12".
The birth-date walk hit `budget_wall_clock` at 12 s. **Cause:** I lowered it in
Phase B for chat responsiveness — but Phase C made verification PARALLEL, so a
turn's wall-time is ~max(per-claim), not the sum; and the project rule is
"latency is not a goal; over-abstention is the disease." **Fix:** restore the
default to 30 (engine default). Low risk; just more time per concurrent walk.

## E2 — natural-language date normalization (precision-aware) §3.2
**Now:** `kb_verifier._value_matches`/`_normalize_date_value` only handle bare
4-digit years + ISO; the object "December 17, 1936" never matches Wikidata
`1936-12-17`, so even with budget it abstains (or the C2-FC1 parse guard fires).
`dateutil` 2.9 IS available. **Fix:** a precision-aware date relation used by the
verify + contradiction paths:
- Parse claim & KB values to `(year, month?, day?)` + a precision (year/month/day)
  via dateutil (+ the approx/`before X` markers handled as today).
- `min_prec = min(claim_prec, kb_prec)`; compare at `min_prec`.
- **VERIFY** iff equal at `min_prec` AND `claim_prec <= kb_prec` (KB is at least as
  precise as the claim and agrees). A claim FINER than the KB (claim day vs KB
  year-only) is **incomparable → abstain** (KB can't confirm the finer part — NOT
  a false-verify).
- **MISMATCH (contradict-eligible)** iff they DISAGREE at `min_prec` where BOTH
  assert that precision (claim "Dec 18 1936" vs KB "1936-12-17" → differ at day →
  mismatch; claim "1994" vs KB "1998-..." → differ at year → mismatch). Differing
  only at a precision one side doesn't assert (coarsening) is NOT a mismatch →
  abstain. Preserves the approx-year + multi-value + value-type guards.
- This subsumes today's bare-year behavior and fixes the pope birth date
  (day==day → verify). **Soundness:** verify only when KB ≥ precise & agrees;
  contradict only on a both-asserted-precision disagreement; else abstain.

## E3 — selection always includes the identity/role claim (safe)
**Now:** `claim_selection.py` LLM picks central claims; it dropped "Pope Francis
holds_role Pope" as peripheral, so the wrong-pope claim was never verified.
**Fix:** instruct the selector (prompt — knowledge in prompt, per "no hardcoded
mappings") to ALWAYS include the claim(s) establishing the answer's core subject
IDENTITY / ROLE / TITLE (who/what the answer is about), even when the question is
about a detail. Including more is always §3.2-safe. Test a respond() case where
the role claim is always central.

## E4 — temporal currency for role/state claims §3.2-CRITICAL (introduces a contradiction)
**Now:** `holds_role`→P39 (position held), MULTI-VALUED, with P580/P582 mapped as
start/end qualifiers (`seeds/predicate_translation.json`). `_scope_compatible`
returns **True for a present-tense claim (valid_from/until None) against an ENDED
statement** (P582 in the past) — so "Francis holds_role Pope" VERIFIES off his
ended P39=pope statement. **Fix, two levels:**
1. **Never verify ended-as-current (safe baseline):** in `_scope_compatible`, a
   PRESENT-tense claim (valid_until is None or `BEFORE_PRESENT`) is NOT compatible
   with a statement whose P582 end is provably in the past (precision-aware
   compare vs `current_time`). Result: the ended role no longer matches → if no
   current matching statement, **abstain** (no more false-verify of "is pope").
2. **Contradict when provably not-current (the flag):** a present-tense
   role/position/state claim where the value MATCHED a statement, but ALL matching
   statements have ENDED (P582 < now) and NONE is current → **CONTRADICT** with
   the end date as the contradicting value ("no longer holds the role; ended
   <date>"). This is what catches the wrong pope.
   **Soundness gates (all required to contradict):** (a) claim genuinely
   present-tense (no future/explicit valid_until); (b) the value resolved &
   matched a statement; (c) every matching statement has P582 strictly < now
   (parsed, not string-naive); (d) NO matching statement is current (un-ended or
   end in the future). Any uncertainty → level-1 abstain. Enumerated false-
   contradict risks (future end, multiple statements, ongoing role, death vs
   resignation, historical/“was” claims which carry a temporal marker → not
   present-tense) each map to abstain.
   If adversarial review finds level-2 unsafe, ship level-1 (abstain) alone — it
   already removes the false "verified: Francis is the pope".

## Sequencing
E1 (trivial) → E3 (prompt) → E2 (date relation + tests) → E4 (temporal, level 1
then gated level 2 + tests) → adversarial review of E2+E4 → patch → full gated
suite (offline regression net is the soundness gate) + targeted live smoke
(pope birth date verifies; "Francis is the pope" no longer verifies / is flagged).
Commits per fix; no tag/push.
