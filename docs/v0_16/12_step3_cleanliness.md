# v0.16 — Step 3: repository cleanliness & doc hygiene

A no-functionality-change pass. Every change was verified behavior-neutral (gated suite held
at **1390 passed, 1 xfailed, 1 xpassed** throughout; pyflakes clean), committed in small steps.

## 3A — core vs. Wikidata architecture map (`10039bc`)
Read-only analysis classifying every `src/aedos` module as CORE (backend-independent),
WIKIDATA-CALIBRATED, or BOUNDARY. Result written to
[`docs/architecture_core_vs_wikidata.md`](../architecture_core_vs_wikidata.md). The import
seam is clean (only `pipeline.py`, the composition root, imports the concrete `WikidataAdapter`);
the substrate/result core is backend-agnostic through `kb_protocol`. Three residual
calibration leaks above the seam (`walker.py` P-id table + P580/P582; `kb_verifier.py`
continent/location constants; `wikipedia_normalizer.py` protocol bypass) are documented as
future-refactor candidates — **not changed here** (they would be functional changes).

## 3B — doc archival (`57782cd`)
Archived **87 historical narrative docs** to `docs/archive/` (mirrored paths): the
`v0.15_build_log/`, the Phase A–H plans/reports/validations, the v0.16 change specs
(`v0_16/00–08` + that dir's README), the v0.16 planning docs (synthesis, forward-planning
1&2, `v0.16_planning.md`), and the Phase 10.5 calibration/medium-bar reports. **Zero deletions.**
The non-archived doc set is now 8 current files: `architecture.md`,
`architecture_core_vs_wikidata.md`, `cold_start.md`, `evaluation_methodology.md`,
`phase_10_5_runbook.md` (kept in place — read at runtime by `test_runbook_thresholds.py`),
and the current-session `v0_16/09,10,11`. README refreshed to v0.16 status; its two links to
archived targets were repointed. (Machine-data artifacts — `phase_E/results` JSONs,
`phase_H` logs, `phase_10_5/runs` JSONs — were left in place; they are coupled to eval scripts,
not narrative docs.)

## 3C — dead-code removal (`4cbfd94`)
Removed **18 unused imports** (pyflakes-confirmed) + **1 unwired constant**
(`_ONTOLOGY_CONSTRAINT_KIND_QUALIFIER = "P2306"`, defined but never referenced). A scan of all
167 module-level private symbols found only that one orphan. The round-1 dormant-but-deferred
mechanisms (SLING, `_binding_vetoed`/`vetoes`, `_exception_cache`, `_interval_holds_at`,
`contradiction_tracer`) were **excluded** — they are documented operator-decision deferrals in
[`09_review_round1_resolutions.md`](09_review_round1_resolutions.md), not dead code.

## 3D — comment & docstring hygiene (`78b3141`, `911decb`)
Trimmed stale **pre-v0.16 provenance** (Phase A–H / Phase 10.5 / Cluster N / Batch N,
D-/M-/F-numbers, `v0.15`/`v0.14` stage tags, bare dates, and citations of now-archived docs)
from `#` comments (20 files) and then docstrings (24 files), **preserving the technical
explanation** in every case. Kept: `§3.2` and architecture-section references, current-version
notes (`v0.16`, `WS1..WS6`, `PATCH-A/B/C`), algorithm step outlines, and Wikidata identifiers
that are part of an explanation.

Each pass was proven behavior-neutral, not just test-green:
- `#` comments: a tokenize-equality check confirmed the non-comment token stream (code,
  docstrings, strings) is byte-identical before/after.
- docstrings: an AST check confirmed (a) every non-docstring string Constant (prompts, keys,
  messages, literals — incl. `_SYSTEM_PROMPT`/`_GENERATION_SYSTEM_PROMPT`) is unchanged, and
  (b) the code AST with docstring first-statements removed is identical — so only docstring
  text differs.
  (Both verifications read git-HEAD as raw UTF-8 bytes; an initial locale-decoded comparison
  produced false positives on em-dash/arrow bytes and was corrected.)

## Not done (deliberately, to preserve no-functionality-change)
- The three core/Wikidata seam leaks (3A) — a real refactor with its own tests, out of scope.
- Physical package reorganization — the layered layout already separates concerns; moving
  files would churn imports for no functional gain.
