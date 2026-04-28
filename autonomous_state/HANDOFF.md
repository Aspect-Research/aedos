# Handoff Notes

To resume work on this branch, whether you are the same autonomous instance
returning after a context-window flush, a fresh autonomous instance, or the
operator (Asa) checking in:

1. Confirm you're on `experiment/autonomous-v0.5.x` branch.
2. Read these files in order:
   - autonomous_state/MISSION.md — your standing instructions
   - autonomous_state/CURRENT_STATE.md — where things stood
   - autonomous_state/NEXT_STEPS.md — the active queue
   - autonomous_state/DECISIONS.md — choices already made (skim recent)
   - autonomous_state/OBSERVATIONS.md — informal notes (skim recent)
   - autonomous_state/SESSION_LOG.md — prior session summaries (skim recent)
3. Run `pytest` to confirm the test suite passes.
4. Update CURRENT_STATE.md with the timestamp of your resumption and any
   notes about the resumption (e.g., "fresh context window," "after rate
   limit cooldown," etc.).
5. Pick the top item from NEXT_STEPS.md and begin.

If GLM is unreachable (post-April 30 or otherwise), follow the fallback
in MISSION.md: switch AEDOS_CHAT_MODEL_PROVIDER=anthropic and document.
Continue work — the chat-model swap is not a hard dependency for most
improvements.

**As of session 2 (2026-04-28, ongoing):** Modal endpoint has been
unreliable throughout this session — alternating between 503,
ReadTimeout, and 200. Quick check:

    py -c "import os; from dotenv import load_dotenv; load_dotenv(); \
    import httpx; r = httpx.post('https://api.us-west-2.modal.direct/v1/chat/completions', \
    headers={'Content-Type':'application/json', \
             'Authorization':'Bearer '+os.getenv('MODAL_API_KEY')}, \
    json={'model':'zai-org/GLM-5.1-FP8', \
          'messages':[{'role':'user','content':'hi'}],'max_tokens':32}, \
    timeout=300.0); print('status', r.status_code)"

200 → re-run dogfood + corpus to validate the session 2 fixes:

    python scripts/dogfood_glm.py
    python scripts/dogfood_hallucination_corpus.py
    python scripts/summarize_corpus_run.py
    python scripts/analyze_substitutions.py

  Look for the substitution rate (was 24.3% pre-fix; should drop
  significantly after the verbatim rule lands in real LLM calls).

503 / timeout → use Anthropic fallback. Single-prompt validation:

    py scripts/eval_harness.py --provider anthropic --corpus hallucination --limit 1
    py scripts/smoke_test_glm.py --provider anthropic

  Full corpus run against Anthropic costs ~$3-5 in API spend; ask
  operator first.

Open Phase 6 prototype to evaluate (off by default):

    AEDOS_UNIQUE_VALUE_SLOTS=1 python scripts/dogfood_hallucination_corpus.py

  This enables the unique-value-slot detection for spatial_temporal.
  was_born_in (one entry only). The Williamsburg/Williamstown
  scenario should now produce USER_CONTRADICTED_SELF events.

Current /api/health summary (when the dev server is running) shows
exactly which features are enabled by env vars.
