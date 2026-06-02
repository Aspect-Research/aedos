# v0.16.2 — Pre-implementation Investigation (read-only)

Answers to the live-deployment scoping investigation, grounded in code (file:line).
Captured 2026-06-02 against branch `v0.16.2` @ `53a6245` (tag `v0.16.1`).

## Sandbox mechanism (Python verifier)

1. **Execution.** Model-generated code runs in a **fresh Python subprocess** —
   `subprocess.run([sys.executable, "-c", code], env=minimal_env, cwd=<tempdir>,
   timeout=5s)` (`utils/sandbox.py:246-255`; called from
   `layer4_sources/python_verifier.py:495`, `_SANDBOX_TIMEOUT=5`). Not in-process
   exec, not RestrictedPython, not a container. No `-I/-S/-E` isolation flags.
2. **Allowed surface.** One static AST scan (`_check_sandbox_violations`,
   `sandbox.py:162-207`): import allow-list (`datetime, math, decimal, fractions,
   statistics, re, unicodedata, string, typing`), blocked builtin Names
   (`__import__, eval, exec, open, compile, __builtins__`), blocked dunder attrs
   (`__class__, __subclasses__, __globals__, …`). **No** AST-node allowlist, **no**
   memory/CPU/fd limit, only a 5 s wall-clock. `os/sys/subprocess/socket/requests`
   are blocked only by *absence from the import allow-list* — i.e. only against
   *literal* import/Name/attr forms.
3. **Env exposure — keys are safe.** The child is spawned with `env=minimal_env`,
   which is `{}` on Linux and a 5-var OS-infra subset on Windows
   (`sandbox.py:238-243`). **`ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` are never
   placed in the child env** → unreadable via `os.environ` inside the sandbox.
   The guarantee rests **entirely** on `env=minimal_env` (no `-E/-I`); if that
   arg were dropped the child would inherit the full parent env.
4. **The two bypass tests** (`tests/unit/test_sandbox.py`):
   - `test_fully_encoded_dunder_chain_bypass` → **XFAIL** (escape **open**):
     builds `__class__/__base__/__subclasses__` at runtime from `chr(95)` so the
     AST sees no literal dunder attr → reaches `object.__subclasses__()` →
     **arbitrary code execution in the child** (full filesystem + network
     capability). It does **not** reach the parent's API keys (env is scrubbed),
     but it *does* reach any file the server user can read (incl. a `.env` on disk).
   - `test_indirect_eval_via_globals` → **XPASS** (currently **blocked**): only
     because it uses the literal `__builtins__` Name, which the Name check catches;
     a variant avoiding that token would not be caught.

## Deployment posture of `src/aedos/app.py`

5. **Auth: ABSENT.** No auth/API-key/bearer gate on any route. 7 routes, all
   unauthenticated: `GET /health`, `GET /audit/{substrate-rows,consistency-checks,
   circuit-breakers,retractions}`, `POST /chat`, `GET /verification/{id}`. An
   unauthenticated caller chooses `asserting_party_id`.
6. **CORS: ABSENT.** No `CORSMiddleware`/`add_middleware`/`allow_origins`.
7. **Rate limiting: ABSENT** for inbound HTTP. (`utils/rate_limit.py` throttles
   only *outbound* Wikidata/Wikipedia calls.)
   - Run: `uvicorn aedos.app:app --port 8000` (default host 127.0.0.1; no `__main__`).
   - The shipped `static/` UI calls `/api/*` routes that **do not exist** →
     non-functional → the v0.16.2 frontend is greenfield.

## Session plumbing (A+)

8. **`asserting_party_id` → Tier U keying: CONFIRMED end-to-end.** HTTP body
   (`app.py:99-103`) → `ctx` (`app.py:138`) → `ChatWrapper.respond` unpacks to
   `asserting_party` (`chat_wrapper.py:336-337`) → `ExtractionContext /
   VerificationContext.asserting_party` → `Claim.asserting_party`
   (`extractor.py:762`) → every Tier U read/write SQL `WHERE asserting_party=?`
   (`tier_u.py:211-218, 306-315, 732-740`; column `database.py:13`, index
   `database.py:167`). **Isolation between distinct parties is enforced by the
   keying** — A+ is sound. Promote-then-walk promotes user claims as
   `asserted_unverified` keyed by the same party (`chat_wrapper.py:372` →
   `promotion.py:83`).
9. **Tier U reset: NO existing path.** No `DELETE FROM tier_u` / clear / purge
   anywhere; `TierU.retract(row_id)` is a per-row *soft* delete by id (zero
   production callers, fires retraction propagation). A per-party reset is a
   **new write path** — cleanest as a scoped `clear_party(asserting_party)`.

## Noticed (relevant to scoping)
- Non-leakage of keys depends solely on `env=minimal_env`; harden by also passing
  `-I` and making the scrub explicit/symmetric + a regression test.
- No memory/CPU/fd cap on the sandbox child (only 5 s wall-clock); a successful
  escape is bounded only by env+cwd+timeout.
- `GET /verification/{id}` does no party check (uuid4 ids, but still).
- Default `asserting_party_id="user"` → all field-omitting callers share one
  partition (fine once the deploy layer assigns per-session ids).
