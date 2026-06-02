import { useState } from "react";
import { chat, type ChatResponse } from "../api";
import Observability from "./Observability";

interface Turn {
  user: string;
  response?: ChatResponse;
  error?: string;
}

export default function Chat() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  async function send() {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setBusy(true);
    const idx = turns.length;
    setTurns((t) => [...t, { user: message }]);
    try {
      const response = await chat(message);
      setTurns((t) => t.map((turn, i) => (i === idx ? { ...turn, response } : turn)));
    } catch (e) {
      const error = e instanceof Error ? e.message : String(e);
      setTurns((t) => t.map((turn, i) => (i === idx ? { ...turn, error } : turn)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mode">
      <div className="transcript">
        {turns.length === 0 && (
          <p className="hint">
            Say something with a factual claim — e.g. “Paris is the capital of France.”
            Aedos extracts the claims, grounds each against Tier-U / KB / Python, and
            replies. Premises you assert become part of your session context.
          </p>
        )}
        {turns.map((turn, i) => (
          <div key={i} className="turn">
            <div className="msg msg-user">{turn.user}</div>
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
                <Observability entries={turn.response.observability} />
              </div>
            )}
          </div>
        ))}
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
