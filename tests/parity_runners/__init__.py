"""Phase 9 parity runners.

Adapters that drive the v1 stack (``src/router``, ``src/extractor``,
``src/verifiers/store_verifier``, ``src/fact_store``) and the v2 stack
(``src/*``) against entries from
``tests/v2/smoke_corpus.jsonl`` and produce a uniform per-stack
``StackVerdict``.

The bucketer composes per-stack verdicts into a 5-bucket assignment
(see ``types.Bucket``). The audit test
(``tests/v2/test_phase9_parity_audit.py``) is the consumer; the
generated artifact is ``phase9_parity_report.md`` in the repo root.

Determinism. The audit avoids live LLM calls entirely:

  * Extraction is bypassed — both runners take the corpus entry's
    ``expected_facts`` as the post-extraction input. Extraction parity
    is covered by ``tests/v2/test_extractor.py`` and
    ``tests/test_extractor.py`` (live-API gates inside).
  * Layer 2 routing uses a stub ``routing_fn`` that returns the
    corpus's ``expected_routing`` (or a sensible default per pattern).
    Cold memo writes therefore record the *intended* method without
    burning an LLM call.
  * Substrate oracles are pre-populated from the corpus's
    ``expected_oracle_classification`` (TWO_TEXT_ORACLE shape) and
    ``expected_label`` (SUBSTRATE_DIRECT shape), plus a tiny canonical
    map for ASSISTANT_LOOKUP entries that depend on a substrate row
    (e.g. cheetahs needs predicate_equivalence(likes, dislikes,
    contradictory); Williamstown derivation needs entity_taxonomy +
    predicate_distribution rows). The map is in ``corpus.py``.
"""
