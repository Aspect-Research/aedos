# Legacy scripts

One-shot dogfooding harnesses retained for historical reference. They
were used during the v0.5/v0.6 autonomous development loops to drive
many turns through the pipeline and capture corpus data.

Active scripts live in the parent `scripts/` directory:

  * `reset_db.py` — wipe + recreate the schema
  * `smoke_test_glm.py` — 3-prompt smoke test against the GLM chat backend
  * `eval_harness.py` — raw vs aedos comparison runs
  * `analyze_costs.py` — turn-cost breakdown from a session DB
  * `analyze_cache.py` — cache hit-rate analysis from a session DB

The scripts here:

  * `dogfood_glm.py` — 17-prompt dogfood (was for v0.5 GLM bring-up)
  * `dogfood_hallucination_corpus.py` — 28 adversarial prompts
  * `summarize_corpus_run.py` — analyzes a corpus run's catches/hedges
  * `analyze_substitutions.py` — measured pre-fix extractor substitution
    rate (the ``value_not_in_source_text`` check it analyzed was
    removed in the v0.7 substitution-warning loosening)

Kept (rather than deleted) so the v0.5/v0.6 development context isn't
lost. Won't be maintained.
