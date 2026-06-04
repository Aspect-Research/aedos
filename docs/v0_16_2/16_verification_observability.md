# v0.16.2 — Verification observability: durable store + enriched `GET /verification/{id}`

"Effective observability" mode: the deployment backend now persists the FULL per-claim
walk result to SQLite at verify time and serves it losslessly, replay-free, surviving
restart — so any frontend has everything the engine produced for a verification. The
frontend is intentionally unchanged; this is a backend-completeness change.

## What changed (vs. before)
Before: `/verification/{id}` returned `per_claim_verdicts` + `aggregate_metadata` +
`claim_observability(verbose=True)` (trace + provenance), from an **in-memory** store
(lost on restart) that **re-walked** stale claims live. Party-scoping was a separate
in-memory dict. `/verify` runs minted no id and were not retrievable.

After: a durable SQLite store captures the full walk at verify time; the endpoint reads
it back with **no re-walk**, survives restart, and is a strict **superset** of the old
payload. `/verify` now mints + returns a `verification_id`. Every JustificationTrace
field round-trips; the record additionally carries resolved QIDs, the directed-over-
enumerate signals, per-claim budget, the retraction premise footprint, templated abstain
lines, the full extraction (incl. extraction-abstained claims), and per-claim
intervention actions.

## Schema (4 tables, additive `CREATE TABLE IF NOT EXISTS` in `database.py`)
- **`verification`** — core, party-scoped: `verification_id` PK, `asserting_party`,
  `created_at`, `source_kind` (chat|verify), `user_message`/`draft_message`/
  `final_message`/`intervention_type`, `aggregate_metadata`, `consistency_warnings`,
  `audit_log_entries`, `not_assessed_claims`, `selection_summary`, `extracted_claims`
  (every extracted claim incl. abstained), `per_claim_actions` (intervention notes).
- **`verification_claim`** — per-claim: denormalized Claim + temporal fields, verdict /
  base_verdict / is_given_assertion / abstention_reason / contradicting_value(_type),
  resolved_subject_qid / resolved_subject_cache_row_id / resolved_value_qid, per-claim
  budget (wall_clock_ms, llm_calls), and the three signals (functional_value_known,
  value_known_entity, functional_entity_predicate). PK `(verification_id, claim_id)`.
- **`verification_trace`** — `trace_json` (lossless `trace_to_json_lossless`) + a row-id-
  free `trace_human`. PK `(verification_id, claim_id)`.
- **`verification_premise`** — the retraction reverse-index: one row per distinct
  provenance literal `(source, source_table, source_row_id, premise_status,
  is_assertion)`, indexed by `(source_table, source_row_id)`.

## `GET /verification/{id}` payload (everything)
Top level: `verification_id`, `asserting_party`, `created_at`, `source_kind`,
`text_input{message, draft}`, `final_message`, `intervention_type`, `selection_summary`,
`not_assessed[]`, `extracted_claims[]`, `per_claim_actions[]`, `aggregate_metadata{}`,
`consistency_warnings[]`, `audit_log_entries[]`, and `claims[]`. Each claim:
identity + temporal, `verdict`/`base_verdict`/`conditional`, `abstention_reason` +
**`abstention_line`** (templated from the closed bucket-set), `contradicting_value(_type)`,
`resolved_subject_qid`/`resolved_subject_cache_row_id`/`resolved_value_qid`, `signals{}`,
`budget{}`, `trace_human`, the full lossless **`trace`** (root, edges + open metadata,
polarity_trace, source_breakdown, walk_metadata, chain_includes_assertion,
budget_consumption), **`provenance`** (AND/OR term with `(table,row_id)` literals), and
**`premises[]`** (the retraction footprint).

## Design notes / invariants
- **Replay-free + restart-safe.** The endpoint reads only SQLite; party-scoping is the
  PERSISTED `asserting_party` (the in-memory party map didn't survive restart). Same 404
  for missing vs. other-party (no existence oracle). The durable record is the
  **verify-time** record — a faithful historical audit; stale-re-derivation is a separate
  live concern and intentionally not folded into the audit record.
- **Idempotent.** `persist()` is delete-then-insert per `verification_id` in one
  `transaction()` — a re-persist leaves no orphan premise/claim/trace rows.
- **Single connection.** The store wraps the shared pipeline connection
  (`check_same_thread=False`, WAL); every write runs under the deploy `engine_lock`.
- **§3.2-neutral.** The walker `walk_metadata` stamp and the kb_verifier
  `functional_entity_predicate` stamp are observability-only — no verdict path reads
  them. Verdicts are unchanged. (Adversarially reviewed: 4 lenses, soundness clean.)
- **Best-effort + robust.** Persist is wrapped so a store failure never breaks a turn;
  `json.dumps(default=str)` guarantees an open metadata value can't raise and silently
  drop the record; a per-claim try/except degrades one bad claim to a partial record
  (logged), never total loss; a `/verify` id is returned only on a confirmed persist.
- **Closed abstain bucket-set.** `abstention_templates.py` maps every reason
  (extraction enum + walker + KB-verifier + aggregator) to a human line, pinned by a CI
  guard test that fails if a new reason ships without a template.

## Tests
`tests/unit/test_verification_store.py` (round-trip of every field; idempotent re-persist
/ no orphans; survives-restart on a fresh connection; full-extraction + per-claim-actions;
`default=str` robustness; partial-record degradation), `tests/unit/test_abstention_templates.py`
(closed-set guard + renderer), updated `tests/deploy/test_backend.py` (party-scoped read,
survives-restart, `/verify` id resolvable). Full gated suite: 1801 passed. Live end-to-end
(real walks vs. Wikidata) confirmed the payload is fully populated (resolved QIDs, signals,
budget, premises, templated lines) from real traces.
