import { useEffect, useState } from "react";
import Chat from "./components/Chat";
import Inspector from "./components/Inspector";
import VerifyText from "./components/VerifyText";
import {
  getAccessKey,
  getSessionId,
  newSessionId,
  resetSession,
  setAccessKey,
} from "./api";

type Mode = "chat" | "verify";

export default function App() {
  const [mode, setMode] = useState<Mode>("chat");
  const [accessKey, setKeyState] = useState(getAccessKey());
  const [sessionId, setSessionId] = useState(getSessionId());
  const [status, setStatus] = useState<string | null>(null);
  const [showInspector, setShowInspector] = useState(true);
  const [ctxVersion, setCtxVersion] = useState(0);

  useEffect(() => {
    setAccessKey(accessKey);
  }, [accessKey]);

  const bumpContext = () => setCtxVersion((v) => v + 1);

  async function onReset() {
    try {
      const { rows_cleared } = await resetSession();
      setStatus(`session context cleared (${rows_cleared} row(s))`);
      bumpContext();
    } catch (e) {
      setStatus(e instanceof Error ? e.message : String(e));
    }
  }

  function onNewSession() {
    setSessionId(newSessionId());
    setStatus("started a fresh session");
    bumpContext();
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>Aedos</h1>
        <span className="tagline">soundness-first claim verification</span>
        <div className="controls">
          <input
            className="key-input"
            type="password"
            placeholder="access key"
            value={accessKey}
            onChange={(e) => setKeyState(e.target.value)}
          />
          <span className="session" title={sessionId}>
            session {sessionId.slice(0, 8)}
          </span>
          <button className="ghost" onClick={() => void onReset()}>
            Start fresh
          </button>
          <button className="ghost" onClick={onNewSession}>
            New session
          </button>
          <button
            className={showInspector ? "ghost ghost-on" : "ghost"}
            onClick={() => setShowInspector((v) => !v)}
          >
            Inspector
          </button>
        </div>
      </header>

      <nav className="tabs">
        <button
          className={mode === "chat" ? "tab tab-active" : "tab"}
          onClick={() => setMode("chat")}
        >
          Chat
        </button>
        <button
          className={mode === "verify" ? "tab tab-active" : "tab"}
          onClick={() => setMode("verify")}
        >
          Verify text
        </button>
      </nav>

      {status && <div className="status-bar">{status}</div>}

      <div className={showInspector ? "layout layout-split" : "layout"}>
        <main>
          {mode === "chat" ? <Chat onTurnComplete={bumpContext} /> : <VerifyText />}
        </main>
        {showInspector && <Inspector version={ctxVersion} />}
      </div>
    </div>
  );
}
