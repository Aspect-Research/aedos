import type { StepEvent } from "../api";

function stepClass(phase: string): string {
  if (phase === "verdict") return "step step-verdict";
  if (phase === "verifying") return "step step-verifying";
  return "step";
}

// The live "thinking" trace — Aedos's process, shown as it happens.
export default function StepLog({ steps, busy }: { steps: StepEvent[]; busy: boolean }) {
  if (!steps.length && !busy) return null;
  return (
    <div className="steplog">
      {steps.map((s, i) => (
        <div key={i} className={stepClass(s.phase)}>
          <span className="step-phase">{s.phase}</span>
          <span className="step-detail">{s.detail}</span>
        </div>
      ))}
      {busy && (
        <div className="step step-busy">
          <span className="spinner" /> working…
        </div>
      )}
    </div>
  );
}
