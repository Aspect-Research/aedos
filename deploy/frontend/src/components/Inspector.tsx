import { useEffect, useState } from "react";
import { getContext, type ContextRow } from "../api";

// What Aedos has retained from the conversation: the session's Tier-U premises.
export default function Inspector({ version }: { version: number }) {
  const [rows, setRows] = useState<ContextRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const r = await getContext();
      setRows(r.rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // refetch whenever the parent bumps `version` (after a turn / reset).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);

  return (
    <aside className="inspector">
      <div className="inspector-head">
        <h3>Session context</h3>
        <button className="link" onClick={() => void load()} disabled={loading}>
          {loading ? "…" : "refresh"}
        </button>
      </div>
      <p className="hint small">
        What Aedos has retained from this conversation (Tier-U premises).
      </p>
      {error && <div className="msg msg-error">{error}</div>}
      {!error && rows.length === 0 && (
        <p className="hint small">nothing retained yet.</p>
      )}
      <ul className="ctx-list">
        {rows.map((r) => (
          <li key={r.id} className="ctx-row">
            <span className="ctx-triple">
              <b>{r.subject}</b> {r.polarity === 0 ? "NOT " : ""}
              <i>{r.predicate}</i> <b>{r.object}</b>
            </span>
            <span className="ctx-status">{r.status.replace(/_/g, " ")}</span>
          </li>
        ))}
      </ul>
    </aside>
  );
}
