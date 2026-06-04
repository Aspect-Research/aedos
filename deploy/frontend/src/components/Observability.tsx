import { useState } from "react";
import type { ObsEntry } from "../api";

function verdictClass(verdict: string): string {
  const base = verdict.replace(/_given_assertion$/, "");
  if (base === "verified") return "verdict verdict-verified";
  if (base === "contradicted") return "verdict verdict-contradicted";
  return "verdict verdict-abstain";
}

function ClaimCard({ entry }: { entry: ObsEntry }) {
  const [open, setOpen] = useState(false);
  const polarity = entry.polarity === 0 ? "NOT " : "";
  return (
    <div className="claim-card">
      <div className="claim-head">
        <span className={verdictClass(entry.verdict)}>{entry.verdict}</span>
        {entry.conditional && (
          <span
            className="badge badge-conditional"
            title="Conditional on a premise you asserted (given-assertion pass)"
          >
            given assertion
          </span>
        )}
      </div>
      <div className="claim-triple">
        <b>{entry.subject}</b> {polarity}
        <i>{entry.predicate}</i> <b>{entry.object}</b>
      </div>
      {entry.abstention_reason && (
        <div className="claim-meta">abstain: {entry.abstention_reason}</div>
      )}
      {entry.contradicting_value != null && (
        <div className="claim-meta">
          source indicates: {String(entry.contradicting_value)}
        </div>
      )}
      {entry.trace_human && (
        <div className="claim-trace">
          <button className="link" onClick={() => setOpen((v) => !v)}>
            {open ? "hide" : "show"} trace
          </button>
          {open && <pre>{entry.trace_human}</pre>}
        </div>
      )}
    </div>
  );
}

export default function Observability({ entries }: { entries: ObsEntry[] }) {
  if (!entries.length) return null;
  return (
    <div className="observability">
      {entries.map((e) => (
        <ClaimCard key={e.claim_id} entry={e} />
      ))}
    </div>
  );
}
