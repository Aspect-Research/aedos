import { useState } from "react";
import { chatStream, stepToObs, type ChatResponse, type StepEvent } from "../api";
import Observability from "./Observability";
import StepLog from "./StepLog";

interface Turn {
  user: string;
  steps: StepEvent[];
  response?: ChatResponse;
  error?: string;
  busy: boolean;
}

export default function Chat({ onTurnComplete }: { onTurnComplete?: () => void }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  function patch(idx: number, fn: (t: Turn) => Turn) {
    setTurns((ts) => ts.map((t, i) => (i === idx ? fn(t) : t)));
  }

  async function send() {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setBusy(true);
    const idx = turns.length;
    setTurns((ts) => [...ts, { user: message, steps: [], busy: true }]);
    try {
      await chatStream(message, {
        onStep: (s) => patch(idx, (t) => ({ ...t, steps: [...t.steps, s] })),
        onResult: (response) => patch(idx, (t) => ({ ...t, response, busy: false })),
        onError: (error) => patch(idx, (t) => ({ ...t, error, busy: false })),
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      patch(idx, (t) => ({ ...t, error: msg, busy: false }));
    } finally {
      setBusy(false);
      onTurnComplete?.();
    }
  }

  return (
    <div className="mode">
      <div className="transcript">
        {turns.length === 0 && (
          <p className="hint">
            Say something with a factual claim — e.g. “Paris is the capital of France.”
            Aedos extracts the claims, verifies them in parallel against Tier-U / KB /
            Python, and replies. Each claim’s verdict and reasoning trace appears below
            as its check completes; premises you assert become session context (see the
            Inspector).
          </p>
        )}
        {turns.map((turn, i) => {
          const claims = turn.steps.filter((s) => s.phase === "verdict").map(stepToObs);
          return (
            <div key={i} className="turn">
              <div className="msg msg-user">{turn.user}</div>
              <StepLog steps={turn.steps} busy={turn.busy} />
              <Observability entries={claims} />
              {turn.error && <div className="msg msg-error">error — {turn.error}</div>}
              {turn.response && (
                <div className="msg msg-aedos">
                  <div className="final">{turn.response.final_message}</div>
                  <div className="meta-row">
                    <span className="pill">{turn.response.intervention_type}</span>
                    {turn.response.given_assertion.count > 0 && (
                      <span className="pill pill-conditional">
                        {turn.response.given_assertion.count} given-assertion
                      </span>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
      <div className="composer">
        <textarea
          value={input}
          placeholder="Type a message…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <button onClick={() => void send()} disabled={busy || !input.trim()}>
          {busy ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}
