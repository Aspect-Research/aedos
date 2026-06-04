import type { StepEvent } from "../api";

// The live narrative of Aedos's process (the per-claim verdict cards are rendered
// separately, from the "verdict" events, so they can fill in out of order as the
// parallel walks complete).
export default function StepLog({ steps, busy }: { steps: StepEvent[]; busy: boolean }) {
  const narrative = steps.filter(
    (s) => s.phase !== "verdict" && s.phase !== "verifying" && s.phase !== "skipped",
  );
  const total = steps.find((s) => typeof s.total === "number")?.total;
  const done = steps.filter((s) => s.phase === "verdict").length;

  if (!narrative.length && !busy && total === undefined) return null;

  return (
    <div className="steplog">
      {narrative.map((s, i) => (
        <div key={i} className="step">
          <span className="step-phase">{s.phase}</span>
          <span className="step-detail">{s.detail}</span>
        </div>
      ))}
      {total !== undefined && total > 0 && (
        <div className="step step-progress">
          {busy && done < total && <span className="spinner" />}
          <span className="step-detail">
            verified {done}/{total} claims{busy && done < total ? " (in parallel)…" : ""}
          </span>
        </div>
      )}
      {busy && total === undefined && (
        <div className="step step-busy">
          <span className="spinner" /> working…
        </div>
      )}
    </div>
  );
}
