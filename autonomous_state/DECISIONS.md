# Decision Log

Every non-trivial choice gets an entry. Format: date, what was decided, what
alternatives were considered, brief rationale. Append-only — never edit
existing entries.

---

## 2026-04-27 — Setup

- Branch created: experiment/autonomous-v0.5.x.
- Decision: this is an experimental branch with no merge condition.
  Rationale: the autonomous run is intended to be exploratory and may
  produce changes that aren't appropriate for main even if they pass tests.
- Decision: no stop condition for the autonomous instance. Rationale:
  operator wants continuous progress until rate-limit or intervention.
- Decision: state files initialized but no actual work performed during
  setup. Rationale: keep setup narrow; let the autonomous instance receive
  its actual task list in its own session prompt.
