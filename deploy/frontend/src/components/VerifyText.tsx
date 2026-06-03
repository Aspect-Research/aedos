import { useState } from "react";
import { verifyStream, type StepEvent, type VerifyResponse } from "../api";
import Observability from "./Observability";
import StepLog from "./StepLog";

export default function VerifyText() {
  const [text, setText] = useState("");
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [result, setResult] = useState<VerifyResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run() {
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    setError(null);
    setResult(null);
    setSteps([]);
    try {
      await verifyStream(value, {
        onStep: (s) => setSteps((prev) => [...prev, s]),
        onResult: (r) => setResult(r),
        onError: (msg) => setError(msg),
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mode">
      <p className="hint">
        Paste text and run Aedos on it directly — every extracted claim is grounded
        and returned with its verdict and trace, with the process shown live.
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
      <StepLog steps={steps} busy={busy} />
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
        </div>
      )}
    </div>
  );
}
