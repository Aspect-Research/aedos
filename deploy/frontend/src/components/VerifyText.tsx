import { useState } from "react";
import { verifyText, type VerifyResponse } from "../api";
import Observability from "./Observability";

export default function VerifyText() {
  const [text, setText] = useState("");
  const [result, setResult] = useState<VerifyResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run() {
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await verifyText(value));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mode">
      <p className="hint">
        Paste text and run Aedos on it directly — every extracted claim is grounded
        and returned with its verdict and trace. No conversational reply, just the
        verification.
      </p>
      <textarea
        className="verify-input"
        value={text}
        placeholder="Paste text to verify…"
        onChange={(e) => setText(e.target.value)}
      />
      <div className="composer">
        <button onClick={() => void run()} disabled={busy || !text.trim()}>
          {busy ? "Running…" : "Run Aedos"}
        </button>
      </div>
      {error && <div className="msg msg-error">error — {error}</div>}
      {result && (
        <div className="verify-result">
          {result.note && <p className="hint">{result.note}</p>}
          {result.given_assertion.count > 0 && (
            <span className="pill pill-conditional">
              {result.given_assertion.count} given-assertion
            </span>
          )}
          <Observability entries={result.observability} />
          {result.observability.length === 0 && result.extracted_claims.length > 0 && (
            <div className="claim-meta">
              extracted {result.extracted_claims.length} claim(s); none groundable.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
