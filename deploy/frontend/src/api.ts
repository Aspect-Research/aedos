// Aedos deploy API client. The session_id (a client-generated UUID persisted in
// localStorage) IS the Tier-U party (A+ model); the access key is the shared
// internal-testing secret sent as X-Aedos-Key.

const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? "http://localhost:8000";

const KEY_STORAGE = "aedos.accessKey";
const SESSION_STORAGE = "aedos.sessionId";

export function getSessionId(): string {
  let sid = localStorage.getItem(SESSION_STORAGE);
  if (!sid) {
    sid = crypto.randomUUID();
    localStorage.setItem(SESSION_STORAGE, sid);
  }
  return sid;
}

export function newSessionId(): string {
  const sid = crypto.randomUUID();
  localStorage.setItem(SESSION_STORAGE, sid);
  return sid;
}

export function getAccessKey(): string {
  return localStorage.getItem(KEY_STORAGE) ?? "";
}

export function setAccessKey(key: string): void {
  localStorage.setItem(KEY_STORAGE, key);
}

export interface ObsEntry {
  claim_id: string;
  subject: string;
  predicate: string;
  object: string;
  polarity: number;
  verdict: string;
  base_verdict: string;
  conditional: boolean;
  abstention_reason: string | null;
  contradicting_value: unknown;
  trace_human: string | null;
}

export interface GivenAssertion {
  count: number;
  claim_ids: string[];
}

export interface PerClaimAction {
  claim_id: string;
  action_type: string;
  annotation: string | null;
}

export interface ChatResponse {
  final_message: string;
  intervention_type: string;
  per_claim_actions: PerClaimAction[];
  verification_id: string;
  observability: ObsEntry[];
  given_assertion: GivenAssertion;
}

export interface ExtractedClaim {
  claim_id: string;
  subject: string;
  predicate: string;
  object: string;
  polarity: number;
  abstention_reason: string | null;
}

export interface VerifyResponse {
  extracted_claims: ExtractedClaim[];
  observability: ObsEntry[];
  given_assertion: GivenAssertion;
  note?: string;
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Aedos-Key": getAccessKey(),
      // The session token is the Tier-U party. Sent as a header (never the URL
      // or a logged body field) so it stays out of access logs / history.
      "X-Aedos-Session": getSessionId(),
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return (await res.json()) as T;
}

export function chat(message: string): Promise<ChatResponse> {
  return postJSON<ChatResponse>("/chat", { message });
}

export function verifyText(text: string): Promise<VerifyResponse> {
  return postJSON<VerifyResponse>("/verify", { text });
}

export function resetSession(): Promise<{ rows_cleared: number }> {
  return postJSON<{ rows_cleared: number }>("/session/reset", {});
}

// --- live (SSE) streaming over fetch (keeps auth/session in headers) --------

export interface StepEvent {
  phase: string;
  detail: string;
  verdict?: string;
  subject?: string;
  predicate?: string;
  object?: string;
  index?: number;
  total?: number;
}

export interface StreamHandlers<R> {
  onStep: (s: StepEvent) => void;
  onResult: (r: R) => void;
  onError: (message: string) => void;
}

async function streamSSE<R>(
  path: string,
  body: unknown,
  h: StreamHandlers<R>,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Aedos-Key": getAccessKey(),
        "X-Aedos-Session": getSessionId(),
      },
      body: JSON.stringify(body),
    });
  } catch (e) {
    h.onError(e instanceof Error ? e.message : String(e));
    return;
  }
  // Auth / validation / rate-limit errors arrive before the stream opens.
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      /* ignore */
    }
    h.onError(`${res.status}: ${detail}`);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let kind = "";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) kind = line.slice(7);
        else if (line.startsWith("data: ")) data = line.slice(6);
      }
      if (!kind) continue;
      let payload: unknown = null;
      if (data) {
        try {
          payload = JSON.parse(data);
        } catch {
          h.onError("malformed stream frame");
          return;
        }
      }
      if (kind === "step") h.onStep(payload as StepEvent);
      else if (kind === "result") h.onResult(payload as R);
      else if (kind === "error") h.onError((payload as { detail?: string })?.detail ?? "stream error");
    }
  }
}

export function chatStream(message: string, h: StreamHandlers<ChatResponse>): Promise<void> {
  return streamSSE<ChatResponse>("/chat/stream", { message }, h);
}

export function verifyStream(text: string, h: StreamHandlers<VerifyResponse>): Promise<void> {
  return streamSSE<VerifyResponse>("/verify/stream", { text }, h);
}

// --- context inspector ------------------------------------------------------

export interface ContextRow {
  id: number;
  subject: string;
  predicate: string;
  object: string;
  polarity: number;
  status: string;
  valid_from: string | null;
  valid_until: string | null;
  asserted_at: string;
}

export interface ContextResponse {
  count: number;
  rows: ContextRow[];
}

export async function getContext(): Promise<ContextResponse> {
  const res = await fetch(`${API_BASE}/session/context`, {
    headers: { "X-Aedos-Key": getAccessKey(), "X-Aedos-Session": getSessionId() },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return (await res.json()) as ContextResponse;
}
