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

200 → resume `python scripts/dogfood_glm.py --start 6`.
503 → either wait, or run against Anthropic for now:
`python scripts/dogfood_glm.py --provider anthropic --start 6`
(but ask the operator first re: API spend — full 17-prompt run uses
~50-100 LLM calls).
