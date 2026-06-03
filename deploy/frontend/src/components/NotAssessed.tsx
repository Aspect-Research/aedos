import { useState } from "react";

interface SkippedClaim {
  subject?: string;
  predicate?: string;
  object?: string;
}

// Phase D: claims from the draft that were NOT central to the question, so they
// were passed through unverified. Shown muted + collapsible — transparency about
// what was (and wasn't) checked.
export default function NotAssessed({ claims }: { claims: SkippedClaim[] }) {
  const [open, setOpen] = useState(false);
  if (!claims.length) return null;
  return (
    <div className="not-assessed">
      <button className="link na-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "▾" : "▸"} {claims.length} claim{claims.length > 1 ? "s" : ""} not
        assessed (not central to your question)
      </button>
      {open && (
        <ul className="na-list">
          {claims.map((c, i) => (
            <li key={i} className="na-row">
              <b>{c.subject}</b> <i>{c.predicate}</i> <b>{c.object}</b>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
