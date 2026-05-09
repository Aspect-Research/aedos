// Aedos UI — single-pane chat + live progressive flow chart, with a
// slide-out inspector drawer for Facts / Patterns / Cache.
//
// Sections:
//   1. el / api helpers
//   2. SSE consumer (streamChat)
//   3. Chat form + message list
//   4. Live flow chart (5 stages, progressive, click-to-expand)
//   5. Stage detail rendering (the long tail of pipeline_event types)
//   6. Inspector drawer (Facts / Patterns / Cache)
//   7. Model selector + Reset
//
// Conventions:
//   * No build step, no framework. textContent everywhere — never
//     innerHTML on user-controlled text so model output can't inject
//     HTML.
//   * Every backend response renders verbatim — the point is to see
//     what the pipeline produced, not a polished view.

// =====================================================================
// 1. helpers
// =====================================================================

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function el(tag, opts = {}, children = []) {
  const n = document.createElement(tag);
  if (opts.className) n.className = opts.className;
  if (opts.title) n.title = opts.title;
  if (opts.textContent !== undefined) n.textContent = opts.textContent;
  if (opts.dataset) for (const k in opts.dataset) n.dataset[k] = opts.dataset[k];
  if (opts.style) n.style.cssText = opts.style;
  children.forEach((c) => n.appendChild(c));
  return n;
}

const SVG_NS = "http://www.w3.org/2000/svg";

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let detail;
    try { detail = await resp.json(); } catch (_) { detail = await resp.text(); }
    const msg = typeof detail === "object"
      ? (detail.detail?.error_message || detail.detail?.error || JSON.stringify(detail))
      : detail;
    throw new Error(`${resp.status} ${msg}`);
  }
  return resp.json();
}

// =====================================================================
// 2. SSE consumer
// =====================================================================
//
// POST /api/chat/stream returns Server-Sent Events. We read the
// response body as a stream and parse SSE frames manually
// (EventSource only supports GET).

function parseSseFrame(block) {
  const out = { event: "message", data: "" };
  for (const line of block.split("\n")) {
    if (!line) continue;
    const colon = line.indexOf(":");
    if (colon < 0) continue;
    const key = line.slice(0, colon).trim();
    const value = line.slice(colon + 1).replace(/^ /, "");
    if (key === "event") out.event = value;
    else if (key === "data") out.data += value;
  }
  return out;
}

async function streamChat(body, handlers) {
  const resp = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    let detail = "";
    try { detail = JSON.stringify(await resp.json()); } catch (_) {}
    throw new Error(`stream rejected (${resp.status}): ${detail}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      if (!block.trim()) continue;
      const frame = parseSseFrame(block);
      if (!frame.data) continue;
      let parsed;
      try { parsed = JSON.parse(frame.data); }
      catch (_) { continue; }
      if (frame.event === "pipeline_event" && handlers.onEvent) handlers.onEvent(parsed);
      else if (frame.event === "done" && handlers.onDone) handlers.onDone(parsed);
      else if (frame.event === "error" && handlers.onError) handlers.onError(parsed);
    }
  }
}

// =====================================================================
// 3. chat form + message list
// =====================================================================

const messagesEl = $("#messages");
const flowContainer = $("#flow-container");
const flowStatus = $("#flow-status");
const form = $("#chat-form");
const input = $("#input");

// Enter sends; Shift+Enter inserts a newline (standard chat-app
// idiom). The textarea otherwise eats Enter as a literal newline,
// forcing the user to click Send.
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    if (input.value.trim()) {
      form.requestSubmit();
    }
  }
});

// Click any chat bubble (user or assistant) → swap the Flow View
// to that turn's pipeline. Lets the operator scroll back through
// earlier turns and inspect their pipeline events without losing
// the conversation context. Click on the live one re-renders from
// cache. User bubbles surface user-side verification (the v0.10.0
// user-world-claim path's routing + code-gen events); assistant
// bubbles surface model-side verification + correction.
messagesEl.addEventListener("click", async (e) => {
  // Don't hijack clicks on links, buttons (e.g. show-diff toggle),
  // or anything inside an <a>/<button>.
  if (e.target.closest("a, button")) return;
  const bubble = e.target.closest(".msg.clickable-flow");
  if (!bubble) return;
  const turnId = parseInt(bubble.dataset.turnId, 10);
  if (!Number.isFinite(turnId)) return;
  await loadFlowForTurn(turnId);
});

async function loadFlowForTurn(turnId) {
  flowStatus.textContent = `loading turn ${turnId}…`;
  try {
    // The clicked turn might be either the user's message or the
    // assistant's reply. The user-side verification events
    // (routing_decision, code_*, etc. for user-world claims) live
    // on the USER turn id; the assistant's own pipeline lives on
    // the assistant turn id. To show the operator a complete
    // turn-pair view, fetch BOTH turns when an assistant bubble is
    // clicked, OR just the user turn when a user bubble is clicked.
    const turns = await api("GET", "/api/turns");
    const target = turns.find((t) => t.id === turnId);
    let toFetch = [turnId];
    let label;
    if (target && target.role === "assistant") {
      // Walk backwards to find the user turn that fed this reply.
      const idx = turns.indexOf(target);
      for (let i = idx - 1; i >= 0; i--) {
        if (turns[i].role === "user") {
          toFetch = [turns[i].id, turnId];
          break;
        }
      }
      label = toFetch.length === 2
        ? `viewing user turn ${toFetch[0]} → assistant turn ${turnId}`
        : `viewing assistant turn ${turnId}`;
    } else {
      label = `viewing user turn ${turnId}`;
    }
    const allEvents = await Promise.all(
      toFetch.map((id) => api("GET", `/api/trace/${id}`).catch(() => [])),
    );
    const merged = [];
    allEvents.forEach((events) => {
      events.forEach((ev, i) => {
        merged.push({
          turn_id: ev.turn_id,
          stage: ev.stage,
          data: ev.data,
          arrivedMs: Date.parse(ev.created_at) || (Date.now() + merged.length + i),
          created_at: ev.created_at,
        });
      });
    });
    expandedClaims.clear();
    renderFlow(turnId, merged, { running: false });
    flowStatus.textContent = `${label} · ${merged.length} event${merged.length === 1 ? "" : "s"}`;
    // Visual emphasis on which bubble's flow is currently shown.
    $$(".msg.clickable-flow").forEach((b) => {
      b.classList.toggle("flow-active", parseInt(b.dataset.turnId, 10) === turnId);
    });
  } catch (err) {
    flowStatus.textContent = `error loading turn ${turnId}`;
    console.error(err);
  }
}

// ---- chat bubble helpers ----
//
// Lifecycle for an assistant bubble during streaming:
//   1. Created as `.msg.assistant.draft-pending` showing "…"
//   2. assistant_draft event arrives → bubble shows DRAFT text, faded
//      (.msg.assistant.draft-faded). The user sees what the model
//      *wanted* to say while verification runs.
//   3. final/done arrives → bubble swaps to FINAL text, un-faded. If
//      final !== draft, a "show diff" toggle is appended that swaps
//      the bubble body between final-only and inline diff view.

function makeAssistantBubble() {
  const node = el("div", { className: "msg assistant draft-pending" });
  const body = el("div", { className: "msg-body", textContent: "…" });
  node.appendChild(body);
  return node;
}

function setBubbleDraft(bubble, draftText) {
  bubble.classList.remove("draft-pending");
  bubble.classList.add("draft-faded");
  const body = bubble.querySelector(".msg-body");
  renderMarkdown(body, draftText || "");
}

// Lay out a finalized assistant bubble. Final children, in order:
//   1. .msg-body         — markdown-rendered final text
//   2. .diff-view        — inline diff (only when corrected)
//   3. .show-diff-btn    — toggle button (always LAST so it stays at the
//                          bottom whether the body or the diff is shown)
function finalizeBubble(bubble, finalText, originalText) {
  bubble.classList.remove("draft-pending", "draft-faded", "diff-open");
  // Wipe and rebuild children so DOM order is canonical regardless of
  // what state the bubble was in (mid-stream draft, prior render, etc.).
  bubble.innerHTML = "";
  const body = el("div", { className: "msg-body" });
  renderMarkdown(body, finalText || "");
  bubble.appendChild(body);

  if (originalText && originalText !== finalText) {
    const diffView = el("div", { className: "diff-view" });
    renderInlineDiff(diffView, originalText, finalText);
    bubble.appendChild(diffView);
    const btn = el("button", { className: "show-diff-btn", textContent: "show diff" });
    btn.addEventListener("click", () => {
      const showing = bubble.classList.toggle("diff-open");
      btn.textContent = showing ? "hide diff" : "show diff";
    });
    bubble.appendChild(btn);
  }
}

function appendUserMessage(text, turnId) {
  const node = el("div", { className: "msg user" });
  node.appendChild(el("div", { className: "msg-body", textContent: text }));
  // Make user bubbles clickable too — their turn id carries the
  // user-side verification events (v0.10.0 user-world-claim path),
  // which the operator otherwise can't reach from the chat pane.
  if (turnId != null) {
    node.dataset.turnId = String(turnId);
    node.classList.add("clickable-flow");
    node.title = "Click to view this turn's pipeline";
  }
  messagesEl.appendChild(node);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendHydratedAssistant(turn) {
  const node = el("div", { className: "msg assistant" });
  finalizeBubble(node, turn.content, turn.original_content);
  // Tag with the turn id so clicking the bubble swaps the Flow View
  // to that turn's events. Lets the operator scroll back through a
  // long conversation and inspect any prior turn's pipeline.
  if (turn.id != null) {
    node.dataset.turnId = String(turn.id);
    node.classList.add("clickable-flow");
    node.title = "Click to view this turn's pipeline";
  }
  messagesEl.appendChild(node);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ---- sentence/block-level diff (LCS) ----
//
// Word-level inline diffing produced unreadable output when the LLM
// rewrote most of a paragraph: deleted-word fragments and inserted-
// word fragments interleaved into a red-green tangle ("BaboonsI don't
// haveapersonal fascinatingpreferences…"). Block-level diff fixes that:
//   * Split both texts into sentence-sized chunks (sentence terminators
//     plus hard newlines).
//   * LCS over chunks; equal chunks render as plain text in document
//     order; replaced runs render as a deletion BLOCK (struck through,
//     red bg) followed by an insertion BLOCK (green bg).
// The operator can read each side as continuous prose and compare.

function _diffSplitSentences(text) {
  // Match in order: a sentence-shaped fragment ending in .!? plus
  // trailing whitespace; OR a hard line break (preserved as its own
  // chunk so paragraph structure survives diff alignment); OR a
  // trailing fragment with no terminator (LLM cut mid-thought).
  if (!text) return [];
  const out = [];
  const re = /([^\n.!?]+[.!?]+["')\]]*\s*)|(\n+)|([^\n]+)/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    out.push(m[0]);
  }
  return out;
}

function diffSentences(oldText, newText) {
  const a = _diffSplitSentences(oldText || "");
  const b = _diffSplitSentences(newText || "");
  const m = a.length, n = b.length;
  const dp = Array.from({ length: m + 1 }, () => new Int32Array(n + 1));
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] = a[i - 1] === b[j - 1]
        ? dp[i - 1][j - 1] + 1
        : Math.max(dp[i - 1][j], dp[i][j - 1]);
    }
  }
  const ops = [];
  let i = m, j = n;
  while (i > 0 && j > 0) {
    if (a[i - 1] === b[j - 1]) { ops.push({ op: "=", t: a[i - 1] }); i--; j--; }
    else if (dp[i - 1][j] >= dp[i][j - 1]) { ops.push({ op: "-", t: a[i - 1] }); i--; }
    else { ops.push({ op: "+", t: b[j - 1] }); j--; }
  }
  while (i > 0) { ops.push({ op: "-", t: a[i - 1] }); i--; }
  while (j > 0) { ops.push({ op: "+", t: b[j - 1] }); j--; }
  ops.reverse();
  // Coalesce same-op adjacent runs so each "replacement" group becomes
  // one delete + one insert pair instead of many tiny fragments.
  const merged = [];
  for (const o of ops) {
    const last = merged[merged.length - 1];
    if (last && last.op === o.op) last.t += o.t;
    else merged.push({ op: o.op, t: o.t });
  }
  return merged;
}

function renderInlineDiff(container, oldText, newText) {
  container.innerHTML = "";
  const ops = diffSentences(oldText, newText);
  // Group consecutive non-equal ops into a single "replacement" so
  // the deletion block always renders before the insertion block —
  // even if the raw LCS produced them in opposite order.
  let idx = 0;
  while (idx < ops.length) {
    const op = ops[idx];
    if (op.op === "=") {
      container.appendChild(document.createTextNode(op.t));
      idx++;
      continue;
    }
    const buf = [];
    while (idx < ops.length && ops[idx].op !== "=") { buf.push(ops[idx]); idx++; }
    const delText = buf.filter((x) => x.op === "-").map((x) => x.t).join("");
    const insText = buf.filter((x) => x.op === "+").map((x) => x.t).join("");
    if (delText.trim()) {
      container.appendChild(el("div", {
        className: "diff-block-del",
        textContent: delText.replace(/\s+$/, ""),
      }));
    }
    if (insText.trim()) {
      container.appendChild(el("div", {
        className: "diff-block-ins",
        textContent: insText.replace(/\s+$/, ""),
      }));
    }
  }
}

// ---- minimal markdown renderer for chat bubbles ----
//
// Handles the subset LLMs actually emit: headings (#..####), **bold**,
// *italic*, `code`, ```fenced code```, - / 1. lists, [text](url),
// > blockquotes, paragraphs separated by blank lines, soft line breaks.
// All text inserted via textContent — never innerHTML on model output.

function renderMarkdown(container, text) {
  container.innerHTML = "";
  if (!text) return;
  const lines = String(text).replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    // Fenced code block
    if (/^```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;  // skip closing fence (or EOF)
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      code.textContent = buf.join("\n");
      pre.appendChild(code);
      container.appendChild(pre);
      continue;
    }
    // Heading
    const h = line.match(/^(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (h) {
      const node = document.createElement(`h${h[1].length}`);
      renderInlineMarkdown(node, h[2]);
      container.appendChild(node);
      i++;
      continue;
    }
    // Blockquote (one or more consecutive `> ` lines)
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^>\s?/, "")); i++;
      }
      const bq = document.createElement("blockquote");
      renderInlineMarkdown(bq, buf.join(" "));
      container.appendChild(bq);
      continue;
    }
    // Lists (consecutive lines all matching the same marker)
    const ulMatch = line.match(/^[-*+]\s+(.+)$/);
    const olMatch = line.match(/^\d+[.)]\s+(.+)$/);
    if (ulMatch || olMatch) {
      const ordered = !!olMatch;
      const re = ordered ? /^\d+[.)]\s+(.+)$/ : /^[-*+]\s+(.+)$/;
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(re);
        if (!m) break;
        items.push(m[1]);
        i++;
      }
      const list = document.createElement(ordered ? "ol" : "ul");
      items.forEach((it) => {
        const li = document.createElement("li");
        renderInlineMarkdown(li, it);
        list.appendChild(li);
      });
      container.appendChild(list);
      continue;
    }
    // Blank line — skip
    if (line.trim() === "") { i++; continue; }
    // Paragraph: collect contiguous non-blank, non-block lines
    const buf = [line];
    i++;
    while (i < lines.length) {
      const l = lines[i];
      if (l.trim() === "") break;
      if (/^(```|#{1,6}\s|>\s?|[-*+]\s|\d+[.)]\s)/.test(l)) break;
      buf.push(l);
      i++;
    }
    const p = document.createElement("p");
    renderInlineMarkdown(p, buf.join(" "));
    container.appendChild(p);
  }
}

function renderInlineMarkdown(parent, text) {
  // Order matters: code first (eats anything inside backticks), then
  // links, then bold, then italic. All non-greedy to avoid runaway.
  const pattern = /(`[^`\n]+`)|(\[([^\]]+)\]\(([^)\s]+)\))|(\*\*([^*\n]+)\*\*)|(__([^_\n]+)__)|(\*([^*\n]+)\*)|(_([^_\n]+)_)/g;
  let last = 0;
  let m;
  while ((m = pattern.exec(text)) !== null) {
    if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    if (m[1]) {  // `code`
      const node = document.createElement("code");
      node.textContent = m[1].slice(1, -1);
      parent.appendChild(node);
    } else if (m[2]) {  // [text](url)
      const a = document.createElement("a");
      a.href = m[4];
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = m[3];
      parent.appendChild(a);
    } else if (m[5]) {  // **bold**
      const node = document.createElement("strong");
      node.textContent = m[6];
      parent.appendChild(node);
    } else if (m[7]) {  // __bold__
      const node = document.createElement("strong");
      node.textContent = m[8];
      parent.appendChild(node);
    } else if (m[9]) {  // *italic*
      const node = document.createElement("em");
      node.textContent = m[10];
      parent.appendChild(node);
    } else if (m[11]) {  // _italic_
      const node = document.createElement("em");
      node.textContent = m[12];
      parent.appendChild(node);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

async function hydrate() {
  try {
    const turns = await api("GET", "/api/turns");
    messagesEl.innerHTML = "";
    turns.forEach((t) => {
      if (t.role === "user") appendUserMessage(t.content, t.id);
      else appendHydratedAssistant(t);
    });
    if (turns.length) {
      const lastAsst = [...turns].reverse().find((t) => t.role === "assistant");
      if (lastAsst) {
        const events = await api("GET", `/api/trace/${lastAsst.id}`);
        renderFlow(lastAsst.id, events);
      }
    }
  } catch (e) {
    console.error(e);
  }
}
hydrate();

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  const sendBtn = form.querySelector("button");
  sendBtn.disabled = true;
  input.value = "";

  appendUserMessage(text);
  // Grab the just-added user bubble so we can tag it with its turn
  // id once the trace comes back on `done`.
  const userBubble = messagesEl.lastElementChild;
  const bubble = makeAssistantBubble();
  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;

  let assistantTurnId = null;
  const liveEvents = [];
  const requestStartMs = Date.now();
  flowStatus.textContent = "running…";
  expandedClaims.clear();
  renderFlow(null, [], { running: true, requestStartMs });

  try {
    await streamChat(
      { message: text },
      {
        onEvent: (ev) => {
          if (assistantTurnId === null) assistantTurnId = ev.turn_id;
          const arrivedMs = Date.now();
          liveEvents.push({
            turn_id: ev.turn_id, stage: ev.stage, data: ev.data,
            arrivedMs, created_at: new Date(arrivedMs).toISOString(),
          });
          // v0.9.0: chat_draft_token streams partial drafts AS they
          // arrive from the chat backend (Anthropic / OpenAI both
          // stream). data.text is the cumulative buffer so the UI
          // doesn't have to splice deltas itself.
          if (ev.stage === "chat_draft_token" && ev.data && ev.data.text) {
            setBubbleDraft(bubble, ev.data.text);
            messagesEl.scrollTop = messagesEl.scrollHeight;
            // chat_draft_token frames are not persisted to
            // pipeline_events — drop them from the in-memory liveEvents
            // log so the Flow View doesn't render one row per token.
            liveEvents.pop();
            return;
          }
          // Update the assistant bubble when the draft text becomes available.
          if (ev.stage === "assistant_draft" && ev.data && ev.data.content) {
            setBubbleDraft(bubble, ev.data.content);
            messagesEl.scrollTop = messagesEl.scrollHeight;
          }
          renderFlow(assistantTurnId, liveEvents, { running: true, requestStartMs });
          flowStatus.textContent = `running… (${liveEvents.length} events)`;
        },
        onDone: (trace) => {
          finalizeBubble(bubble, trace.final_content, trace.original_content);
          // Tag both bubbles with their turn ids so either can be
          // clicked to swap the Flow View — the user turn carries
          // the user-world-claim verification events; the assistant
          // turn carries the model-side pipeline.
          bubble.dataset.turnId = String(trace.assistant_turn_id);
          bubble.classList.add("clickable-flow");
          bubble.title = "Click to view this turn's pipeline";
          if (userBubble && trace.user_turn_id != null) {
            userBubble.dataset.turnId = String(trace.user_turn_id);
            userBubble.classList.add("clickable-flow");
            userBubble.title = "Click to view this turn's pipeline (user-side verification)";
          }
          messagesEl.scrollTop = messagesEl.scrollHeight;
          renderFlow(trace.assistant_turn_id, liveEvents, { running: false, requestStartMs });
          flowStatus.textContent = `done · ${liveEvents.length} events · turn ${trace.assistant_turn_id}`;
        },
        onError: (errInfo) => {
          const body = bubble.querySelector(".msg-body");
          body.textContent = `⚠ ${errInfo.error_type}: ${errInfo.error_message}`;
          bubble.classList.remove("draft-faded", "draft-pending");
          bubble.style.color = "var(--bad)";
          flowStatus.textContent = "error";
        },
      },
    );
  } catch (err) {
    const body = bubble.querySelector(".msg-body");
    body.textContent = `⚠ ${err.message}`;
    bubble.classList.remove("draft-faded", "draft-pending");
    bubble.style.color = "var(--bad)";
    flowStatus.textContent = "error";
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
});

// =====================================================================
// 4. live flow chart
// =====================================================================
//
// Four progressive steps. Click any step to expand its detail INLINE.
// The middle "Claims" step combines extraction + verification: each
// claim is its own row in plain language, individually expandable.

const PIPELINE_STEPS = [
  {
    // Virtual user-side step. Consumes user_extraction (claim seeds)
    // AND user_storage (per-claim decisions including v0.10.0 user-
    // world-claim verdicts). Renders for both user-only flows
    // (clicking a user bubble) AND turn-pair flows (clicking an
    // assistant bubble — the user-side events come from the merged
    // preceding user turn).
    kind: "user_claims",
    title: "User Message",
  },
  {
    kind: "chat_model_call",
    stage: "chat_model_call",
    title: "Chat Model",
    metaFn: (ev) => formatChatMeta(ev.data || {}),
  },
  {
    // Virtual step. Consumes assistant_extraction (claim seeds) AND
    // verification (decisions) from the event stream. State:
    //   * verification landed       → "done" (final per-claim colors)
    //   * extraction landed only    → "in_flight" (pending dots that
    //                                  flip to in-flight as routing
    //                                  decisions arrive per claim)
    //   * neither                   → "pending"
    kind: "claims",
    title: "Claims",
  },
  {
    // Substrate writes + warm consultations this turn. Renders only
    // when the turn actually engaged the substrate (any *_write event,
    // any tier_w_write, OR any *_hit when chain_walked); otherwise
    // skipped via the "absent" state. Sits between Claims (where the
    // substrate is consulted) and Correction (where the verdicts feed
    // the corrector).
    kind: "substrate",
    title: "Substrate",
  },
  {
    kind: "correction",
    stage: "correction",
    title: "Correction",
    metaFn: (ev) => {
      const ivs = ((ev.data || {}).interventions || []);
      const total = ivs.length;
      // pass_through and noop don't trigger the corrector LLM, so they
      // don't count as "applied". The remaining set (replace + hedge +
      // soften) is what the operator actually cares about.
      const active = ivs.filter((iv) =>
        iv.intervention_type !== "pass_through"
        && iv.intervention_type !== "noop");
      const rewrote = (ev.data || {}).rewrote;
      if (active.length === 0) {
        return total === 0 ? "no corrections needed"
                           : `${total} pass-through · no corrections needed`;
      }
      const verb = rewrote ? "applied" : "planned (no text change)";
      return `${active.length} of ${total} intervention${total === 1 ? "" : "s"} ${verb}`;
    },
  },
  {
    kind: "final",
    stage: "final",
    title: "Final Response",
    metaFn: (ev) => {
      const d = ev.data || {};
      const n = d.n_assistant_claims ?? 0;
      const parts = [`${n} claim${n === 1 ? "" : "s"} verified`];
      if (d.rewrote) parts.push("response rewritten");
      if (d.n_routing_anomalies) parts.push(`${d.n_routing_anomalies} anomaly`);
      return parts.join(" · ");
    },
  },
];

function formatChatMeta(d) {
  if (!d) return "(no chat data)";
  if (d.error) {
    return `⚠ ${d.provider || "?"}:${d.model || "?"} — ERROR: ${(d.error || "").slice(0, 80)}`;
  }
  const status = d.status_code ? ` http=${d.status_code}` : "";
  const respc = d.response_chars != null ? `, ${d.response_chars}c` : "";
  return `${d.provider || "?"}:${d.model || "?"}${status}${respc}`;
}

function fmtDurationMs(ms) {
  if (ms == null || !isFinite(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

// Stable identity key for a claim — must match between extraction's
// valid_facts and routing_decision's claim payload (both come from the
// same fact dict; same json shape).
function claimKey(claim) {
  if (!claim) return "";
  return `${claim.pattern || ""}|${claim.predicate || ""}|${claim.polarity ?? ""}|${JSON.stringify(claim.slots || {})}`;
}

function flowEdgeClass(displayStatus) {
  // display_status: verified / contradicted / inconclusive / not_applicable
  // / skipped (v0.14.1 — triage gate suppressed verification)
  if (displayStatus === "verified") return "verified";
  if (displayStatus === "contradicted") return "contradicted";
  if (displayStatus === "inconclusive") return "inconclusive";
  if (displayStatus === "skipped") return "skipped";
  return "not_applicable";
}

// Persistent expansion state — per-claim only now. Each row in the
// Claims card can expand independently. Survives re-renders during
// SSE streaming; cleared when a new turn starts.
const expandedClaims = new Set();

function renderFlow(turnId, events, { running = false, requestStartMs = null } = {}) {
  flowContainer.innerHTML = "";
  flowContainer.appendChild(buildFlowChart(events || [], turnId, running, requestStartMs));
}

// Stages whose presence indicates the substrate did something this
// turn. The substrate is the FOUR ORACLES (predicate_equivalence,
// entity_equivalence, entity_taxonomy, predicate_distribution) plus
// derivation walks that consult them. Tier W writes are world-cache
// activity, not substrate activity — they live in Memory tab → World
// scope; including them here was a category error.
const SUBSTRATE_STAGES = new Set([
  "predicate_equivalence_write",
  "entity_equivalence_write",
  "entity_taxonomy_write",
  "predicate_distribution_write",
  "derivation_walk_completed",
]);

// Determine state for a given step from the event map.
//   "done"      — primary event present (and, for claims, verification fired)
//   "in_flight" — claims-only: extraction fired but verification hasn't
//   "pending"   — nothing yet
//   "absent"    — substrate-only: no substrate activity this turn,
//                 step is skipped entirely
function stepState(step, stageMap) {
  if (step.kind === "claims") {
    if (stageMap["verification"]) return "done";
    if (stageMap["assistant_extraction"]) return "in_flight";
    return "pending";
  }
  if (step.kind === "user_claims") {
    if (stageMap["user_storage"]) return "done";
    if (stageMap["user_extraction"]) return "in_flight";
    return "pending";
  }
  if (step.kind === "substrate") {
    // Skip the card entirely on turns that didn't touch the substrate.
    // Otherwise, the card transitions to "done" the moment any
    // substrate write/walk lands; there's no in_flight state because
    // the substrate work is interleaved with claim verification (which
    // already shows in_flight on the Claims card).
    let touched = false;
    for (const stage of SUBSTRATE_STAGES) {
      if (stageMap[stage]) { touched = true; break; }
    }
    if (!touched) return "absent";
    return stageMap["verification"] ? "done" : "in_flight";
  }
  return stageMap[step.stage] ? "done" : "pending";
}

// When did a step finish (ms)? Used to compute the NEXT step's duration.
function stepEndMs(step, stageMap) {
  if (step.kind === "claims") {
    const v = stageMap["verification"];
    if (v && typeof v.arrivedMs === "number") return v.arrivedMs;
    const e = stageMap["assistant_extraction"];
    return e && typeof e.arrivedMs === "number" ? e.arrivedMs : null;
  }
  if (step.kind === "user_claims") {
    const s = stageMap["user_storage"];
    if (s && typeof s.arrivedMs === "number") return s.arrivedMs;
    const e = stageMap["user_extraction"];
    return e && typeof e.arrivedMs === "number" ? e.arrivedMs : null;
  }
  if (step.kind === "substrate") {
    // Substrate work is interleaved with verification; treat the
    // verification aggregate's arrival as the substrate end too. The
    // duration shown on the next step (Correction) thus measures
    // post-verification latency only.
    const v = stageMap["verification"];
    return v && typeof v.arrivedMs === "number" ? v.arrivedMs : null;
  }
  const ev = stageMap[step.stage];
  return ev && typeof ev.arrivedMs === "number" ? ev.arrivedMs : null;
}

function buildFlowChart(events, turnId, running, requestStartMs) {
  const stageMap = {};
  events.forEach((e) => { stageMap[e.stage] = e; });

  // Per-step duration = (this step's end) − (previous step's end). The
  // very first step's "previous" is the request-start timestamp.
  const durations = {};
  let prevMs = requestStartMs;
  for (const step of PIPELINE_STEPS) {
    const endMs = stepEndMs(step, stageMap);
    if (endMs != null && prevMs != null) durations[step.kind] = endMs - prevMs;
    if (endMs != null) prevMs = endMs;
  }

  // LLM call ledger comes from the turn_cost event (lands at the end
  // of the turn). Each call has {purpose, model, duration_ms, total_usd}.
  // Bucketed per pipeline step via STEP_PURPOSES.
  const turnCost = stageMap["turn_cost"];
  const allCalls = (turnCost && turnCost.data ? turnCost.data.calls : []) || [];
  const callsByStep = {};
  for (const step of PIPELINE_STEPS) {
    const purposes = new Set(STEP_PURPOSES[step.kind] || []);
    callsByStep[step.kind] = allCalls.filter((c) => purposes.has(c.purpose));
  }

  // Non-LLM ops (cache lookups, retrieval queries, sandbox execs) —
  // counted from the existing per-stage events. All belong to the
  // Claims card.
  const opsByStep = { claims: opsCountFromEvents(events) };

  // Routing decisions (per-claim, fire during verification dispatch).
  // Used by the Claims card's in-flight view to flip individual
  // claim rows from "pending" → "in flight".
  const routingEvents = events.filter((e) => e.stage === "routing_decision");
  // v0.14 per-claim live signals. Each claim's verification fires its
  // own claim_decision event from the worker thread (parallel
  // dispatch); the aggregate `verification` event is the terminal
  // marker. Per-claim events are what flip individual rows from
  // in_flight → done as each verification lands.
  const claimDecisionEvents = events.filter((e) => e.stage === "claim_decision");
  const tierUStorageEvents = events.filter((e) => e.stage === "tier_u_storage");
  const walkerDecisionEvents = events.filter((e) => e.stage === "walker_decision");
  // v0.14.1 — verifiability triage events (one per assistant claim,
  // pre-walker). Used by the per-claim row to surface PASS_THROUGH
  // verdicts with their rule + reason.
  const triageEvents = events.filter((e) => e.stage === "verifiability_triage");

  const chart = el("div", { className: "flow-chart" });

  // Render each step in order. Done/in-flight steps render their full
  // node. The first "pending" step renders as a thinking placeholder
  // (only while the turn is running); later pending steps don't render.
  let renderedAny = false;
  for (const step of PIPELINE_STEPS) {
    const state = stepState(step, stageMap);
    if (state === "absent") continue;  // optional step, skip entirely
    if (state === "pending") {
      if (running) {
        if (renderedAny) chart.appendChild(arrowDown());
        chart.appendChild(renderThinkingNode(step));
      }
      break;
    }
    if (renderedAny) chart.appendChild(arrowDown());
    chart.appendChild(renderStep(step, state, stageMap, {
      duration: durations[step.kind],
      calls: callsByStep[step.kind] || [],
      ops: opsByStep[step.kind] || null,
      routingEvents,
      claimDecisionEvents,
      tierUStorageEvents,
      walkerDecisionEvents,
      triageEvents,
      events,  // full events list — renderSubstrateNode walks it
      turnCost,
    }));
    renderedAny = true;
  }

  if (!renderedAny) {
    chart.appendChild(el("p", { className: "hint",
      textContent: "Send a message — each stage will appear here as it lands." }));
  }
  return chart;
}

// Map a step kind → the LLM-call purposes that belong to it.
// Calls without a recognized purpose ("unknown") fall through to
// the Final card so they're at least visible somewhere.
const STEP_PURPOSES = {
  // v0.10.0 user-side step: only the extractor:user call is uniquely
  // attributable. The verifier-side purposes (router, prompt_builder,
  // code_writer, etc.) are SHARED with the assistant-side claims
  // step — same purpose names whether they fire from _route_user_
  // world_claim or _route_model — so listing them here would
  // double-count. Until the LLM ledger carries a per-call origin
  // tag, they're attributed to the assistant claims card by default.
  // Net effect on a user-only-extracts-zero-claims turn: the User
  // Message card is empty (correct), no longer shows bogus calls.
  user_claims: ["extractor:user"],
  chat_model_call: ["chat"],
  claims: ["extractor:assistant", "router",
           "cache_classify", "cache_scoping", "cache_stability",
           "prompt_builder", "code_writer", "retrieval_judge"],
  correction: ["corrector"],
  final: ["unknown"],
};

// Count non-LLM operations from the raw event stream — cache lookups,
// retrieval HTTP calls, sandbox executions. These are all pipeline-
// internal "operations" the user wants to see at a glance, even though
// they don't cost LLM tokens.
function opsCountFromEvents(events) {
  let cacheHit = 0, cacheSemHit = 0, cacheMiss = 0, cacheWrite = 0;
  let retrievalQueries = 0;
  let sandboxExecs = 0;
  events.forEach((e) => {
    if (e.stage === "cache_lookup") {
      const r = (e.data || {}).result;
      if (r === "hit") cacheHit++;
      else if (r === "semantic_hit") cacheSemHit++;
      else if (r === "miss") cacheMiss++;
    } else if (e.stage === "cache_write") {
      cacheWrite++;
    } else if (e.stage === "retrieval_query_attempt") {
      retrievalQueries++;
    } else if (e.stage === "code_executed") {
      sandboxExecs++;
    }
  });
  return { cacheHit, cacheSemHit, cacheMiss, cacheWrite,
           retrievalQueries, sandboxExecs };
}

// Group a list of LLM calls by (purpose, model). Used to produce the
// "extractor × 1 — Opus 4.7 — 0.5s — $0.0008" lines on each card.
function summarizeCalls(calls) {
  const byKey = new Map();
  for (const c of calls) {
    const key = `${c.purpose || "?"}|${c.model || "?"}`;
    const slot = byKey.get(key) || {
      purpose: c.purpose || "?",
      model: c.model || "?",
      count: 0,
      total_usd: 0,
      total_ms: 0,
    };
    slot.count++;
    slot.total_usd += c.total_usd || 0;
    if (c.duration_ms) slot.total_ms += c.duration_ms;
    byKey.set(key, slot);
  }
  return Array.from(byKey.values())
    .sort((a, b) => (b.total_usd - a.total_usd) || a.purpose.localeCompare(b.purpose));
}

// One-line readable cost. "$0.0021" for non-trivial, "—" for free.
function fmtCost(usd) {
  if (!usd || usd <= 0) return "—";
  if (usd >= 0.01) return `$${usd.toFixed(3)}`;
  if (usd >= 0.0001) return `$${usd.toFixed(4)}`;
  return `$${usd.toExponential(1)}`;
}

// Render the call + ops surface for a single pipeline card. Returns
// null when there's nothing to show (so the caller can skip the
// container entirely).
function renderCallsBlock(calls, ops) {
  const groups = summarizeCalls(calls);
  const opLines = ops ? formatOpLines(ops) : [];
  if (groups.length === 0 && opLines.length === 0) return null;

  const block = el("div", { className: "calls-block" });
  groups.forEach((g) => {
    const row = el("div", { className: "call-row" });
    row.appendChild(el("span", { className: "call-purpose",
      textContent: friendlyPurpose(g.purpose) }));
    if (g.count > 1) row.appendChild(el("span", { className: "call-count",
      textContent: `× ${g.count}` }));
    row.appendChild(el("span", { className: "call-model", textContent: g.model }));
    row.appendChild(el("span", { className: "call-meta",
      textContent: fmtDurationMs(g.total_ms) || "—" }));
    row.appendChild(el("span", { className: "call-cost", textContent: fmtCost(g.total_usd) }));
    block.appendChild(row);
  });
  opLines.forEach((line) => {
    const row = el("div", { className: `op-row op-row-${line.kind}` });
    // Dot is rendered via CSS pseudo-element keyed off op-row-{kind}.
    row.appendChild(el("span", { className: "op-label", textContent: line.label }));
    row.appendChild(el("span", { className: "op-detail", textContent: line.detail }));
    block.appendChild(row);
  });
  return block;
}

function formatOpLines(ops) {
  const lines = [];
  const cacheTotal = ops.cacheHit + ops.cacheSemHit + ops.cacheMiss;
  if (cacheTotal > 0) {
    const parts = [];
    if (ops.cacheHit) parts.push(`${ops.cacheHit} hit`);
    if (ops.cacheSemHit) parts.push(`${ops.cacheSemHit} semantic-hit`);
    if (ops.cacheMiss) parts.push(`${ops.cacheMiss} miss`);
    lines.push({ kind: "cache", label: "cache lookup", detail: parts.join(", ") });
  }
  if (ops.cacheWrite) {
    lines.push({ kind: "cache", label: "cache write", detail: String(ops.cacheWrite) });
  }
  if (ops.retrievalQueries) {
    lines.push({ kind: "retrieval", label: "web retrieval",
      detail: `${ops.retrievalQueries} quer${ops.retrievalQueries === 1 ? "y" : "ies"}` });
  }
  if (ops.sandboxExecs) {
    lines.push({ kind: "sandbox", label: "sandbox exec",
      detail: `${ops.sandboxExecs} run${ops.sandboxExecs === 1 ? "" : "s"}` });
  }
  return lines;
}

// Convert raw purpose tags into the labels the user sees in the UI.
const PURPOSE_LABELS = {
  "chat": "assistant chat",
  "extractor:user": "extract user claims",
  "extractor:assistant": "extract assistant claims",
  "router": "route claim",
  "cache_scoping": "cache: scope",
  "cache_stability": "cache: stability",
  "prompt_builder": "code: prompt",
  "code_writer": "code: write",
  "retrieval_judge": "retrieval judge",
  "corrector": "corrector",
  "unknown": "(unlabeled call)",
};
function friendlyPurpose(p) { return PURPOSE_LABELS[p] || p; }

function annotationStepFor(stage) {
  // Map non-primary events to a step bucket.
  if (stage === "assistant_draft") return "chat_model_call";
  if (stage === "user_extraction" || stage === "user_storage"
      || stage === "extractor_substitution_warning") return "claims";
  if (stage === "routing_decision" || stage === "routing_anomaly_detected"
      || stage === "verifier_failure" || stage === "retrieval_query_attempt"
      || stage === "code_prompt_built" || stage === "code_prompt_leakage_detected"
      || stage === "code_generated" || stage === "code_executed"
      || stage === "code_unusual_behavior" || stage === "code_comparison"
      || stage === "canonical_constants_cross_check"
      || stage === "canonical_constants_disagreement"
      || stage === "cache_scoping_decision" || stage === "cache_stability_decision"
      || stage === "cache_lookup" || stage === "cache_write") return "claims";
  if (stage === "turn_cost") return "final";
  return null;
}

function renderStep(step, state, stageMap, ctx) {
  if (step.kind === "claims") return renderClaimsNode(state, stageMap, ctx);
  if (step.kind === "user_claims") return renderUserClaimsNode(state, stageMap, ctx);
  if (step.kind === "substrate") return renderSubstrateNode(state, stageMap, ctx);
  return renderEventStepNode(step, stageMap[step.stage], ctx);
}

// v0.10.0: user-side claims card. Mirrors renderClaimsNode but reads
// from user_extraction (claim seeds the user message contained) and
// user_storage (per-claim Decision: stored as user_asserted, or
// verified/contradicted via the user-world-claim path).
function renderUserClaimsNode(state, stageMap, ctx) {
  const extractionEvent = stageMap["user_extraction"];
  const storageEvent = stageMap["user_storage"];
  const valid = (extractionEvent && extractionEvent.data
                  ? extractionEvent.data.valid_facts : []) || [];
  // User-side decisions: aggregate user_storage event holds the
  // terminal list. Per-claim signals (tier_u_storage for self-attribute
  // self-attribute writes, walker_decision for user-stated world claims)
  // arrive live during the user-claim-handling loop.
  const aggregateDecisions = (storageEvent && storageEvent.data
                               ? storageEvent.data.decisions : []) || [];
  const decisionByKey = {};
  (ctx.tierUStorageEvents || []).forEach((e) => {
    const d = e.data || {};
    const claim = d.claim || (d.fact && {
      pattern: d.fact.pattern, predicate: d.fact.predicate,
      polarity: d.fact.polarity, slots: d.fact.slots,
    });
    if (!claim) return;
    decisionByKey[claimKey(claim)] = {
      claim,
      is_self_attribute: true,
      storage_outcome: d.outcome,
      walker: null,
      // Adapter fields for the v0.14 user-claim row rendering.
      display_status: "verified",
      outcome: "stored",
      verification_status: "user_asserted",
    };
  });
  (ctx.walkerDecisionEvents || []).forEach((e) => {
    const w = e.data || {};
    if (!w.claim) return;
    decisionByKey[claimKey(w.claim)] = {
      claim: w.claim,
      walker: w,
      is_self_attribute: false,
      display_status: _walkerDisplayStatus(w),
      outcome: w.outcome,
      verification_status: w.verification_status,
    };
  });
  // Aggregate wins (carries the full UserClaimDecision shape with
  // routing layer2 etc.).
  aggregateDecisions.forEach((d) => {
    decisionByKey[claimKey(d.claim || {})] = _adaptUserClaimDecision(d);
  });
  const routedKeys = new Set(
    (ctx.routingEvents || []).map((e) =>
      claimKey((e.data || {}).claim || {})),
  );

  const wrapper = el("div", { className: "flow-step-wrapper" });
  const node = el("div", {
    className: "flow-step flow-step-claims" + (state === "in_flight"
      ? " flow-step-thinking" : " flow-step-done"),
    dataset: { stage: "user_claims" },
  });

  const doneCount = Object.keys(decisionByKey).length;
  let summary = `${valid.length} claim${valid.length === 1 ? "" : "s"}`;
  let extraRight = null;
  if (state === "in_flight") {
    extraRight = el("span", { className: "flow-step-duration thinking-dots" }, [
      document.createTextNode(
        valid.length > 0
          ? `processing ${doneCount} of ${valid.length}`
          : "processing"
      ),
      el("span", { className: "thinking-anim" }),
    ]);
  }
  node.appendChild(buildStepHeader(`User Message · ${summary}`,
                                   ctx.duration, extraRight));

  const callsBlock = renderCallsBlock(ctx.calls || [], ctx.ops || null);
  if (callsBlock) node.appendChild(callsBlock);

  if (valid.length === 0) {
    node.appendChild(el("div", { className: "flow-step-meta",
      textContent: "no claims extracted from the user message" }));
  } else {
    const list = el("ul", { className: "claim-list claim-list-detailed" });
    valid.forEach((claim) => {
      const decision = decisionByKey[claimKey(claim)] || null;
      let rowState;
      if (decision) rowState = "done";
      else if (routedKeys.has(claimKey(claim))) rowState = "in_flight";
      else rowState = "pending";
      list.appendChild(renderClaimItem(claim, rowState, decision));
    });
    node.appendChild(list);
  }

  wrapper.appendChild(node);
  return wrapper;
}

// ---- Substrate step ------------------------------------------------
//
// Surfaces what the substrate did this turn: new oracle rows created
// by the LLM-driven classifiers, new Tier W cache writes from the
// fresh verifier, and (count-only) the warm consultations that
// short-circuited via SQL hits. Per-claim attribution is omitted in
// v0.14.1 — the walker doesn't tag substrate writes with the
// originating claim_key, especially under parallel verification.
// "Best-effort by event ordering" gets too misleading too fast under
// concurrency; v0.15 can add explicit tagging.

const SUBSTRATE_WRITE_ORACLES = [
  { stage: "predicate_equivalence_write",   label: "predicate equivalence" },
  { stage: "entity_equivalence_write",      label: "entity equivalence" },
  { stage: "entity_taxonomy_write",         label: "entity taxonomy" },
  { stage: "predicate_distribution_write",  label: "predicate distribution" },
];

// Warm-consultation stages — counted only, not listed individually.
// Mirrors SUBSTRATE_WRITE_ORACLES but with _hit suffix.
const SUBSTRATE_HIT_STAGES = new Set([
  "predicate_equivalence_hit",
  "entity_equivalence_hit",
  "entity_taxonomy_hit",
  "predicate_distribution_hit",
]);

function renderSubstrateNode(state, stageMap, ctx) {
  const events = ctx.events || [];
  const wrap = el("div", { className: "flow-step-wrapper" });
  const node = el("div", {
    className: "flow-step flow-step-substrate"
      + (state === "in_flight" ? " flow-step-thinking" : " flow-step-done"),
    dataset: { stage: "substrate" },
  });

  // Aggregate counts. Substrate = four oracles + derivation walks.
  // Tier W writes are tracked separately by the Memory tab's World
  // scope; not surfaced here.
  const writesByOracle = {};   // stage -> [event...]
  let hitCount = 0;
  let derivationCount = 0;

  events.forEach((e) => {
    if (e.stage.endsWith("_write") && e.stage !== "tier_w_write"
        && e.stage !== "routing_memo_write") {
      (writesByOracle[e.stage] ||= []).push(e);
    } else if (SUBSTRATE_HIT_STAGES.has(e.stage)) {
      hitCount++;
    } else if (e.stage === "derivation_walk_completed") {
      derivationCount++;
    }
  });

  const totalWrites = SUBSTRATE_WRITE_ORACLES.reduce(
    (acc, o) => acc + (writesByOracle[o.stage] || []).length, 0,
  );

  // Header line summary.
  const summaryParts = [];
  if (totalWrites) {
    summaryParts.push(`${totalWrites} new row${totalWrites === 1 ? "" : "s"}`);
  }
  if (hitCount) {
    summaryParts.push(`${hitCount} warm consultation${hitCount === 1 ? "" : "s"}`);
  }
  if (derivationCount) {
    summaryParts.push(`${derivationCount} derivation walk${derivationCount === 1 ? "" : "s"}`);
  }
  const summary = summaryParts.length
    ? summaryParts.join(" · ")
    : "no substrate activity";

  let extraRight = null;
  if (state === "in_flight") {
    extraRight = el("span", { className: "flow-step-duration thinking-dots" }, [
      document.createTextNode("walking substrate"),
      el("span", { className: "thinking-anim" }),
    ]);
  }
  node.appendChild(buildStepHeader(`Substrate · ${summary}`,
                                   ctx.duration, extraRight));

  if (totalWrites === 0 && hitCount === 0 && derivationCount === 0) {
    node.appendChild(el("div", { className: "flow-step-meta",
      textContent: "(no rows written, no oracles consulted)" }));
    wrap.appendChild(node);
    return wrap;
  }

  // Per-oracle write groups. Each group lists every new row with its
  // identity tuple, label, and the LLM's reason for that classification.
  SUBSTRATE_WRITE_ORACLES.forEach((oracle) => {
    const writes = writesByOracle[oracle.stage] || [];
    if (!writes.length) return;
    node.appendChild(_renderSubstrateOracleGroup(oracle, writes));
  });

  // Warm-consultation count, by-oracle (substrate oracles only).
  const hitsByOracle = {};
  events.forEach((e) => {
    if (!SUBSTRATE_HIT_STAGES.has(e.stage)) return;
    const k = e.stage.replace(/_hit$/, "");
    hitsByOracle[k] = (hitsByOracle[k] || 0) + 1;
  });
  if (Object.keys(hitsByOracle).length) {
    const hits = el("div", { className: "substrate-warm-hits" });
    hits.appendChild(el("span", { className: "substrate-warm-label",
      textContent: "warm consultations:" }));
    Object.entries(hitsByOracle).sort().forEach(([k, n]) => {
      const lbl = k.replace(/_/g, " ");
      hits.appendChild(el("span", { className: "substrate-warm-pill",
        textContent: `${lbl} × ${n}` }));
    });
    node.appendChild(hits);
  }

  wrap.appendChild(node);
  return wrap;
}

function _renderSubstrateOracleGroup(oracle, writes) {
  const group = el("div", { className: "substrate-oracle-group" });
  const head = el("div", { className: "substrate-oracle-head" });
  head.appendChild(el("span", { className: "substrate-oracle-name",
    textContent: oracle.label }));
  head.appendChild(el("span", { className: "substrate-oracle-count",
    textContent: `${writes.length} new row${writes.length === 1 ? "" : "s"}` }));
  group.appendChild(head);
  writes.forEach((e) => {
    const d = e.data || {};
    const row = el("div", { className: "substrate-write-row" });
    row.appendChild(el("span", { className: "substrate-write-identity mono",
      textContent: _substrateIdentityText(oracle.stage, d) }));
    row.appendChild(el("span", { className: "substrate-write-arrow",
      textContent: "→" }));
    row.appendChild(el("span", { className: "substrate-write-label",
      textContent: d.label || "?" }));
    if (d.reason) {
      row.appendChild(el("div", { className: "substrate-write-reason",
        textContent: d.reason }));
    }
    group.appendChild(row);
  });
  return group;
}

function _substrateIdentityText(stage, d) {
  // Shape varies by oracle. Predicate / entity equivalences are
  // symmetric pairs; taxonomy is directional; distribution is a
  // 4-tuple.
  if (stage === "predicate_equivalence_write") {
    return `(${d.pattern}) ${d.predicate_a} ↔ ${d.predicate_b}`;
  }
  if (stage === "entity_equivalence_write") {
    return `${d.entity_a} ↔ ${d.entity_b}`;
  }
  if (stage === "entity_taxonomy_write") {
    return `${d.child} → ${d.parent} (${d.relation_type})`;
  }
  if (stage === "predicate_distribution_write") {
    return `(${d.pattern}) ${d.predicate}/${d.polarity} under ${d.taxonomy_relation_type}`;
  }
  return JSON.stringify(d).slice(0, 80);
}

// ---- step header (title + duration pill) ----

function buildStepHeader(title, durationMs, extraRight) {
  const header = el("div", { className: "flow-step-header" });
  header.appendChild(el("span", { className: "flow-step-title", textContent: title }));
  if (extraRight) header.appendChild(extraRight);
  else if (durationMs != null && isFinite(durationMs)) {
    header.appendChild(el("span", { className: "flow-step-duration",
      textContent: fmtDurationMs(durationMs), title: `${Math.round(durationMs)} ms` }));
  }
  return header;
}

// ---- generic single-event step (chat / correction / final) ----

function renderEventStepNode(step, event, ctx) {
  const wrapper = el("div", { className: "flow-step-wrapper" });
  const node = el("div", { className: "flow-step flow-step-done flow-step-static",
    dataset: { stage: step.stage } });
  node.appendChild(buildStepHeader(step.title, ctx.duration));
  const meta = step.metaFn ? step.metaFn(event) : "";
  if (meta) node.appendChild(el("div", { className: "flow-step-meta", textContent: meta }));

  // Surface the LLM calls + ops for this step. The complicated
  // per-event detail lives in the Inspector → Pipeline events tab.
  const callsBlock = renderCallsBlock(ctx.calls || [], ctx.ops || null);
  if (callsBlock) node.appendChild(callsBlock);

  // Step-specific inline content (no click-to-expand — the user
  // explicitly asked for high-level cards with no raw-data dumps).
  if (step.kind === "correction") {
    renderCorrectionInline(node, (event && event.data) || {});
  } else if (step.kind === "final") {
    // Response preview lives above the cost line so the operator sees
    // what landed at a glance — the chat bubble's correctness is the
    // load-bearing rendering, but the Final card now mirrors it for
    // continuity in the flow chart.
    const finalData = (event && event.data) || {};
    if (finalData.final_content) {
      node.appendChild(renderFinalResponsePreview(finalData));
    }
    if (ctx.turnCost) {
      node.appendChild(renderFinalSummary(ctx.turnCost.data || {}));
    }
  }

  wrapper.appendChild(node);
  return wrapper;
}

// What ran during the Correction step: the interventions the
// corrector planned (each one shows WHAT claim it's acting on, not
// just the action type), and an inline word-level diff if the
// rewrite actually changed the draft.
function renderCorrectionInline(container, data) {
  // v0.14.3 — only show interventions the corrector actually acts
  // on (replace / hedge / soften). pass_through and noop are no-ops
  // by definition; listing them all turns the Correction box into a
  // wall of "nothing happened" rows. Skipped/triaged claims live
  // behind the Claims card's drop-down already.
  const interventions = (data.interventions || []).filter((iv) =>
    iv.intervention_type !== "pass_through"
    && iv.intervention_type !== "noop"
  );
  if (interventions.length > 0) {
    const ivWrap = el("div", { className: "interventions-inline" });
    interventions.forEach((iv) => {
      const row = el("div", { className: `intervention intervention-${iv.intervention_type}` });

      // Header line: action type pill + the actual claim being touched.
      const head = el("div", { className: "intervention-head" });
      head.appendChild(el("span", {
        className: `intervention-type intervention-type-${iv.intervention_type}`,
        textContent: iv.intervention_type,
      }));
      head.appendChild(el("span", { className: "intervention-target",
        textContent: claimDisplayText(iv.claim || {}) }));
      row.appendChild(head);

      // For REPLACE: show the original → verified-value transition
      // explicitly. The diff below covers the textual change but this
      // gives the structured before/after at a glance.
      if (iv.intervention_type === "replace" && iv.verified_value !== undefined && iv.verified_value !== null) {
        const slots = (iv.claim && iv.claim.slots) || {};
        const original = slots.object ?? slots.value ?? slots.role ?? slots.target ?? "";
        row.appendChild(el("div", { className: "intervention-replace-line",
          textContent: `${formatValue(original)} → ${formatValue(iv.verified_value)}` }));
      }

      // Reason: the LLM's explanation. Indented under the action.
      if (iv.reason) {
        row.appendChild(el("div", { className: "intervention-reason", textContent: iv.reason }));
      }
      ivWrap.appendChild(row);
    });
    container.appendChild(ivWrap);
  }
  if (data.original && data.corrected && data.original !== data.corrected) {
    const diff = el("div", { className: "diff-box" });
    renderInlineDiff(diff, data.original, data.corrected);
    container.appendChild(diff);
  }
}

function formatValue(v) {
  if (v === null || v === undefined) return "(none)";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function renderFinalSummary(d) {
  const wrap = el("div", { className: "final-summary" });
  const totalUsd = (d.total_usd ?? 0);
  const calls = d.total_calls ?? 0;
  wrap.appendChild(el("div", { className: "final-summary-line",
    textContent: `Total · ${calls} LLM call${calls === 1 ? "" : "s"} · ${fmtCost(totalUsd)} · ${(d.total_input_tokens ?? 0)} / ${(d.total_output_tokens ?? 0)} tok in/out` }));
  return wrap;
}

// Truncated preview of the final response text inside the Final card.
// Markdown rendering (so headings/lists look right) with a soft cap on
// length — past 600 chars the operator can scroll the chat bubble for
// the full text. textContent on the truncation marker keeps the cap
// XSS-safe even though the source is an LLM string.
function renderFinalResponsePreview(d) {
  const wrap = el("div", { className: "final-response-preview" });
  const label = el("div", { className: "final-response-label",
    textContent: d.rewrote ? "final (rewritten)" : "final" });
  wrap.appendChild(label);
  const body = el("div", { className: "final-response-body" });
  const text = String(d.final_content || "");
  const MAX = 600;
  if (text.length <= MAX) {
    renderMarkdown(body, text);
  } else {
    renderMarkdown(body, text.slice(0, MAX));
    body.appendChild(el("div", { className: "final-response-truncation",
      textContent: `… (${text.length - MAX} more chars · scroll the chat bubble for the full response)` }));
  }
  wrap.appendChild(body);
  return wrap;
}

// ---- combined Claims card ----
//
// state === "in_flight": extraction landed, verification hasn't.
//   Claims show as pending dots; flip to in-flight as their
//   routing_decision events arrive. Card row expansion is disabled
//   (no decision yet to show).
// state === "done": both events landed. Claims show final colors.
//   Each row is independently expandable — expanding one hides
//   nothing else, but ONLY that row's expanded body shows the
//   per-claim Decision detail.
function renderClaimsNode(state, stageMap, ctx) {
  const extractionEvent = stageMap["assistant_extraction"];
  const verificationEvent = stageMap["verification"];
  const valid = (extractionEvent && extractionEvent.data ? extractionEvent.data.valid_facts : []) || [];

  // Decisions arrive in two waves under v0.14:
  //   * per-claim claim_decision events fire from the worker thread
  //     during parallel verification (live updates)
  //   * the aggregate verification event lands at the end (terminal)
  // Merge so each claim row shows its verdict the moment its worker
  // finishes — a 12-claim turn no longer waits for the slowest one.
  const aggregateDecisions = (verificationEvent && verificationEvent.data
                               ? verificationEvent.data.decisions : []) || [];
  // v0.14.1 — verifiability triage events arrive BEFORE the per-claim
  // decision events. Index by claim key so we can attach the triage
  // verdict (rule + reason) to each decision when the worker finishes.
  // PASS_THROUGH triage on a U/W-miss claim shows up as a
  // "Skipped — verifiability triage" verdict in the per-claim row.
  const triageByKey = {};
  (ctx.triageEvents || []).forEach((e) => {
    const d = e.data || {};
    if (d.claim) triageByKey[claimKey(d.claim)] = d;
  });
  const decisionByKey = {};
  (ctx.claimDecisionEvents || []).forEach((e) => {
    const d = e.data || {};
    if (d.claim) {
      const adapted = _adaptClaimDecision(d);
      adapted.triage = triageByKey[claimKey(d.claim)] || null;
      decisionByKey[claimKey(d.claim)] = adapted;
    }
  });
  // Aggregate wins over per-claim (carries layer2 too) when both present.
  aggregateDecisions.forEach((d) => {
    const adapted = _adaptClaimDecision(d);
    adapted.triage = triageByKey[claimKey(d.claim || {})] || null;
    decisionByKey[claimKey(d.claim || {})] = adapted;
  });

  // Set of claim keys that have already routed (used while in-flight).
  const routedKeys = new Set(ctx.routingEvents.map((e) => claimKey((e.data || {}).claim || {})));

  const wrapper = el("div", { className: "flow-step-wrapper" });
  const node = el("div", {
    className: "flow-step flow-step-claims" + (state === "in_flight" ? " flow-step-thinking" : " flow-step-done"),
    dataset: { stage: "claims" },
  });

  // Header: title · N claims, with duration on the right (or
  // "verifying X of Y" while in-flight). The X-of-Y form makes
  // parallel progress visible — the operator can see claims
  // landing as workers finish.
  const doneCount = Object.keys(decisionByKey).length;
  let summary = `${valid.length} claim${valid.length === 1 ? "" : "s"}`;
  let extraRight = null;
  if (state === "in_flight") {
    extraRight = el("span", { className: "flow-step-duration thinking-dots" }, [
      document.createTextNode(
        valid.length > 0
          ? `verifying ${doneCount} of ${valid.length}`
          : "verifying"
      ),
      el("span", { className: "thinking-anim" }),
    ]);
  }
  node.appendChild(buildStepHeader(`Claims · ${summary}`, ctx.duration, extraRight));

  // LLM calls + ops surface for this step. Sits between header and
  // claim rows so the user sees what work happened before the
  // verdicts. Pipeline-event raw data lives in the Inspector instead.
  const callsBlock = renderCallsBlock(ctx.calls || [], ctx.ops || null);
  if (callsBlock) node.appendChild(callsBlock);

  if (valid.length === 0) {
    node.appendChild(el("div", { className: "flow-step-meta",
      textContent: "no claims extracted from the draft" }));
  } else {
    // v0.14.3 — partition claims into visible (verified / contradicted
    // / inconclusive / anomaly / in-flight / pending) and skipped
    // (triage gate suppressed verification AND walker missed all
    // tiers). Skipped claims clutter the chat-side view; hide them
    // behind a drop-down at the bottom.
    const visibleClaims = [];
    const skippedClaims = [];
    valid.forEach((claim) => {
      const decision = decisionByKey[claimKey(claim)] || null;
      let rowState;
      if (decision) rowState = "done";
      else if (routedKeys.has(claimKey(claim))) rowState = "in_flight";
      else rowState = "pending";
      const isSkipped = (
        rowState === "done"
        && decision
        && _isTriageSkipped(decision)
      );
      (isSkipped ? skippedClaims : visibleClaims).push({
        claim, rowState, decision,
      });
    });

    if (visibleClaims.length) {
      const list = el("ul", { className: "claim-list claim-list-detailed" });
      visibleClaims.forEach(({ claim, rowState, decision }) => {
        list.appendChild(renderClaimItem(claim, rowState, decision));
      });
      node.appendChild(list);
    } else if (skippedClaims.length) {
      // All claims skipped — show a hint so the operator knows the
      // claims came in but were triaged out.
      node.appendChild(el("div", { className: "flow-step-meta",
        textContent: "all extracted claims were triaged out (none had a "
                   + "falsifiable surface area worth verifying)" }));
    }

    if (skippedClaims.length) {
      node.appendChild(_renderSkippedClaimsDropdown(skippedClaims));
    }
  }

  wrapper.appendChild(node);
  return wrapper;
}

// v0.14.3 — A claim is "triage-skipped" iff the triage gate emitted
// PASS_THROUGH AND the walker landed on the no-information statuses
// (unverifiable_pending_implementation / retrieval_failed) — i.e.,
// fresh dispatch was suppressed and U/W/derivation also missed. The
// new pass_through intervention with the triage reason is the
// canonical signal.
function _isTriageSkipped(decision) {
  const triage = decision && decision.triage;
  if (!triage || triage.decision !== "pass_through") return false;
  const walker = decision.walker || {};
  const status = walker.verification_status;
  return walker.served_from_tier === "fresh"
    && (status === "unverifiable_pending_implementation"
        || status === "retrieval_failed");
}

// Drop-down at the bottom of the Claims card listing the triage-
// skipped claims. Collapsed by default; expanded reveals each
// skipped claim with a muted style. Click on the header toggles.
function _renderSkippedClaimsDropdown(skippedClaims) {
  const wrap = el("div", { className: "claims-skipped-dropdown" });
  const header = el("button", {
    className: "claims-skipped-toggle",
    textContent: `▸ ${skippedClaims.length} claim${skippedClaims.length === 1 ? "" : "s"} skipped (verifiability triage)`,
    title: "Triage gate decided these claims weren't worth verifying. "
         + "No text change applied; model's wording stands.",
  });
  const body = el("ul", {
    className: "claim-list claim-list-detailed claims-skipped-list",
  });
  skippedClaims.forEach(({ claim, rowState, decision }) => {
    body.appendChild(renderClaimItem(claim, rowState, decision));
  });
  let open = false;
  header.addEventListener("click", () => {
    open = !open;
    header.textContent =
      `${open ? "▾" : "▸"} ${skippedClaims.length} claim${skippedClaims.length === 1 ? "" : "s"} skipped (verifiability triage)`;
    body.style.display = open ? "" : "none";
  });
  body.style.display = "none";
  wrap.appendChild(header);
  wrap.appendChild(body);
  return wrap;
}

// One claim row inside the Claims card. The COLLAPSED row shows:
//   [dot]  source-text-or-summary  [pattern badge]  [routing method]
//   [tier badge]
// The EXPANDED detail (only when row.classList contains 'expanded')
// shows ONLY this claim's full Layer 5 verdict: walker pills, three-
// factor decision confidence, intervention, derivation chain (if any),
// and evidence blocks (retrieval snippets / generated code) when the
// fresh tier resolved the claim.
function renderClaimItem(claim, state, decision) {
  const key = claimKey(claim);
  const isExpandable = state === "done";
  const row = el("li", {
    className: `claim-row claim-${claimRowClass(state, decision)}`
      + (isExpandable ? " claim-expandable" : ""),
  });
  if (expandedClaims.has(key)) row.classList.add("expanded");

  // ----- collapsed (always-visible) summary line -----
  const summaryLine = el("div", { className: "claim-summary" });
  summaryLine.appendChild(el("span", { className: `claim-dot claim-dot-${claimDotState(state)}` }));
  summaryLine.appendChild(el("span", {
    className: "claim-text",
    textContent: claimDisplayText(claim),
  }));
  summaryLine.appendChild(el("span", {
    className: "pattern-badge claim-pattern",
    textContent: claim.pattern || "?",
  }));
  const methodText = state === "pending" ? "queued"
    : state === "in_flight" ? "verifying…"
    : (decision && decision.routing_method) || "?";
  summaryLine.appendChild(el("span", { className: "claim-method", textContent: methodText }));
  // v0.14: tier badge tells the operator at a glance which tier
  // resolved the claim (u / w / derivation / fresh).
  const tier = decision && decision.walker && decision.walker.served_from_tier;
  if (tier) {
    const tierLabels = {
      u: "Tier U", w: "Tier W",
      derivation: "derived", fresh: "fresh",
      routing_anomaly: "anomaly",
    };
    summaryLine.appendChild(el("span", {
      className: `tier-badge tier-badge-${tier}`,
      title: `Resolved at ${tierLabels[tier] || tier}`,
      textContent: tierLabels[tier] || tier,
    }));
  }
  if (isExpandable) {
    summaryLine.appendChild(el("span", { className: "claim-toggle",
      textContent: row.classList.contains("expanded") ? "▾" : "▸" }));
  }
  row.appendChild(summaryLine);

  // ----- expanded body (only this claim's full detail) -----
  if (isExpandable) {
    const body = el("div", { className: "claim-detail" });
    renderClaimDetailBody(body, claim, decision);
    row.appendChild(body);
    summaryLine.style.cursor = "pointer";
    summaryLine.addEventListener("click", () => {
      const wasExpanded = expandedClaims.has(key);
      if (wasExpanded) expandedClaims.delete(key);
      else expandedClaims.add(key);
      row.classList.toggle("expanded");
      const tog = summaryLine.querySelector(".claim-toggle");
      if (tog) tog.textContent = row.classList.contains("expanded") ? "▾" : "▸";
    });
  }

  return row;
}

// ---- v0.14 decision-shape adapters ----

// Map walker.outcome + walker.verification_status → display_status enum
// the UI uses for color coding ("verified" / "contradicted" /
// "inconclusive" / "not_applicable" / "skipped").
//
// Triage-skipped (PASS_THROUGH that found nothing in U/W/derivation)
// gets a distinct "skipped" class — it's not inconclusive (the
// verifier didn't run), it's not verified (no evidence), and it's
// not a routing anomaly (no validator failure). Neutral display.
function _walkerDisplayStatus(walker, triage) {
  if (!walker) return "inconclusive";
  if (triage && triage.decision === "pass_through"
      && walker.served_from_tier === "fresh"
      && (walker.verification_status === "unverifiable_pending_implementation"
          || walker.verification_status === "retrieval_failed")) {
    return "skipped";
  }
  const status = walker.verification_status;
  const outcome = walker.outcome;
  if (status === "verified" || status === "user_asserted") return "verified";
  if (status === "contradicted" || outcome === "contradiction") return "contradicted";
  if (status === "routing_anomaly" || status === "unverifiable_in_principle") return "not_applicable";
  return "inconclusive";
}

// Per-claim claim_decision event → adapted decision object the row
// renderers consume. Carries the v0.14 walker/confidence/intervention
// triplet plus derived display_status + routing_method.
function _adaptClaimDecision(eventData) {
  const walker = eventData.walker || {};
  const conf = eventData.confidence || {};
  const iv = eventData.intervention || {};
  const layer2 = eventData.layer2 || {};
  return {
    claim: eventData.claim || {},
    walker,
    confidence: conf,
    intervention: iv,
    layer2,
    threshold: eventData.threshold,
    routing_method: walker.routing_method
                    || layer2.method
                    || (layer2.decision && layer2.decision.method)
                    || "?",
    display_status: _walkerDisplayStatus(walker),
  };
}

// Aggregate user_storage decision → adapted shape. The aggregate
// holds UserClaimDecision dicts (with layer2 + walker + storage_outcome).
function _adaptUserClaimDecision(d) {
  const walker = d.walker || null;
  const layer2 = d.layer2 || {};
  let display = "verified";
  if (d.is_anomaly) display = "not_applicable";
  else if (d.user_world_dispute) display = "contradicted";
  else if (walker) display = _walkerDisplayStatus(walker);
  return {
    claim: d.claim || {},
    walker,
    layer2,
    is_self_attribute: !!d.is_self_attribute,
    is_anomaly: !!d.is_anomaly,
    user_world_dispute: !!d.user_world_dispute,
    storage_outcome: d.storage_outcome,
    routing_method: layer2.method
                    || (layer2.decision && layer2.decision.method)
                    || (walker && walker.routing_method)
                    || "?",
    display_status: display,
  };
}

function claimRowClass(state, decision) {
  if (state === "pending") return "pending";
  if (state === "in_flight") return "in-flight";
  // v0.14.1 — recompute display_status with triage in scope so
  // triage-skipped claims get the "skipped" class even when the
  // adapter ran before the triage event was attached.
  const walker = decision && decision.walker;
  const triage = decision && decision.triage;
  const status = walker ? _walkerDisplayStatus(walker, triage)
                        : (decision && decision.display_status) || "inconclusive";
  return flowEdgeClass(status);
}
function claimDotState(state) {
  if (state === "pending") return "pending";
  if (state === "in_flight") return "pulsing";
  return "solid";
}

// Plain-text rendering of a claim.
//
// The extractor often pulls source_text as a minimal noun-phrase span
// ("matrilineal hierarchies", "30 years", "hold grudges") rather than
// a full clause. Showing those bare fragments to the operator hides
// what the claim actually asserts. We synthesize a "<subject>
// <predicate> <object>" sentence from the slots first, since it
// surfaces the full proposition. source_text becomes a fallback for
// patterns where synthesis is incomplete.
function claimDisplayText(claim) {
  const synthesized = _synthesizeClaimText(claim);
  if (synthesized) return synthesized;
  const fragment = claim.source_text && claim.source_text.trim();
  return fragment || "(unrecognized claim)";
}

// Build a "<subject> <predicate> <object>" sentence from a claim.
// Uses humanizeFact when the pattern is in _SLOT_ORDER_BY_PATTERN
// (covers all 9 v0.14 patterns); falls back to the v1 subject-search
// for unknown shapes.
function _synthesizeClaimText(claim) {
  const slots = claim.slots || {};
  // For patterns in the ordered set, use the same humanizer the
  // Memory tab uses — keeps the wording consistent across the UI.
  if (claim.pattern && _SLOT_ORDER_BY_PATTERN[claim.pattern]) {
    const sentence = humanizeFact({
      pattern: claim.pattern,
      predicate: claim.predicate,
      slots,
      polarity: claim.polarity,
    });
    return sentence && sentence.trim() ? sentence : null;
  }
  // Generic subject-search fallback for unrecognized patterns.
  const subject = slots.subject || slots.holder || slots.entity
                || slots.agent || slots.part || slots.event_type || "";
  const object = slots.object || slots.role || slots.value
              || slots.target || slots.location || slots.category
              || slots.whole || slots.proposition || "";
  const pred = (claim.predicate || "").replace(/_/g, " ");
  const polarity = claim.polarity === 0 ? "not " : "";
  const parts = [subject, polarity + pred, object]
    .map((p) => String(p).trim())
    .filter((p) => p);
  return parts.length ? parts.join(" ") : null;
}

// Reimagined per-claim detail (v0.14.1, post-feedback):
//
//   1. Verdict headline — one plain-English sentence describing what
//      happened to this claim, plus an intervention pill on the right.
//      Replaces the walker-tier/outcome/status three-pill cluster and
//      the decision-confidence breakdown box for the per-claim view.
//   2. Evidence block — retrieval snippets / generated code / matching
//      stored fact / derivation chain / routing-anomaly explanation.
//      This is the operationally interesting bit; it leads.
//   3. Claim shape — pattern + slot table at the bottom for
//      grounding.
//
// What's NOT here anymore:
//   * decision-confidence breakdown box (path × chain × evidence) —
//     that math is the same all-defaults numerology for fresh-DB
//     turns and obscures which claim was actually verified vs
//     inconclusive. Lives in Inspector → Pipeline events for
//     operators who want it.
//   * walker tier/outcome/status pills — collapsed into the verdict.
//   * substrate consultation `via` list — folded into the new
//     Substrate pipeline step (per-turn aggregate).
function renderClaimDetailBody(container, claim, d) {
  const walker = d.walker || {};
  const iv = d.intervention || {};
  const conf = d.confidence || {};
  const T = d.threshold !== undefined ? d.threshold : 0.5;

  // 1. Verdict headline.
  container.appendChild(_renderVerdictHeadline(walker, iv, conf, T, d.triage));

  // 2. Evidence block (varies by tier).
  const evidenceNode = _renderClaimEvidence(walker, claim);
  if (evidenceNode) container.appendChild(evidenceNode);

  // 3. Claim shape — slots + polarity at the bottom.
  container.appendChild(renderClaimBlock(claim));
}

// Verdict synthesis: walker + intervention → one plain sentence + pill.
function _renderVerdictHeadline(walker, intervention, conf, T, triage) {
  const wrap = el("div", { className: "claim-verdict-headline" });
  const text = verdictSummary(walker, triage);
  const verdictClass = _walkerDisplayStatus(walker, triage);
  const verdictNode = el("span", {
    className: `claim-verdict-text claim-verdict-${verdictClass}`,
    textContent: text,
  });
  wrap.appendChild(verdictNode);
  // Intervention pill + (optional conf annotation when soft verdict).
  const ivType = intervention.intervention_type;
  if (ivType) {
    const ivWrap = el("span", { className: "claim-verdict-iv" });
    ivWrap.appendChild(el("span", {
      className: `v2-iv-pill v2-iv-pill-${ivType}`,
      title: intervention.reason || "intervention",
      textContent: ivType,
    }));
    if (typeof conf.value === "number"
        && (ivType === "hedge" || ivType === "soften")) {
      ivWrap.appendChild(el("span", {
        className: "claim-verdict-conf",
        title: "decision confidence vs threshold T",
        textContent: ` conf ${conf.value.toFixed(2)} < T=${T.toFixed(2)}`,
      }));
    }
    wrap.appendChild(ivWrap);
  }
  if (intervention.reason) {
    wrap.appendChild(el("div", { className: "claim-verdict-reason",
      textContent: intervention.reason }));
  }
  return wrap;
}

// Plain-English verdict from walker shape. Routing-anomaly + each tier
// + each verification_status combination has a tailored phrase. The
// optional `triage` argument carries the verifiability_triage event
// data for this claim — when present and the walker landed on
// fresh/unverifiable_pending (the canonical "everything missed and
// triage suppressed fresh dispatch" case), we override the verdict
// with a triage-skipped explanation.
function verdictSummary(walker, triage) {
  const tier = walker.served_from_tier;
  const outcome = walker.outcome;
  const status = walker.verification_status;
  const method = walker.routing_method;

  // v0.14.1 — triage-skipped takes precedence when the walker had
  // nothing useful to say (lookups missed and fresh was suppressed).
  if (triage && triage.decision === "pass_through"
      && tier === "fresh"
      && (status === "unverifiable_pending_implementation"
          || status === "retrieval_failed")) {
    return `Skipped — verifiability triage (${triage.rule || "no_signal"}); model trusted`;
  }

  if (tier === "routing_anomaly") {
    return "Routing anomaly — Layer 2 validator rejected this claim";
  }
  if (tier === "u") {
    if (outcome === "match" && status === "user_asserted") {
      return "Match — your earlier assertion (Tier U)";
    }
    if (outcome === "contradiction") {
      return "Contradicted — disagrees with your earlier assertion";
    }
  }
  if (tier === "w") {
    if (outcome === "match") {
      return `Match — cached verifier verdict (Tier W, ${status || "verified"})`;
    }
    if (outcome === "contradiction") {
      return "Contradicted — cached verifier verdict says otherwise";
    }
  }
  if (tier === "derivation") {
    const n = (walker.derivation_path || []).length;
    if (outcome === "match") {
      return `Derived — ${n}-step substrate chain supports the claim`;
    }
    if (outcome === "contradiction") {
      return `Contradicted by derivation (${n}-step substrate chain)`;
    }
  }
  if (tier === "fresh") {
    const verifier = method === "retrieval" ? "Wikipedia retrieval"
                   : method === "python" ? "Python verifier"
                   : method === "python_with_canonical_constants" ? "Python verifier (canonical constants)"
                   : "fresh verifier";
    if (status === "verified")               return `Verified by ${verifier}`;
    if (status === "contradicted")           return `Contradicted by ${verifier}`;
    if (status === "retrieval_inconclusive") return `Inconclusive — judge couldn't decide on the evidence`;
    if (status === "retrieval_failed")       return `Verifier failed — no evidence retrieved`;
    if (status === "unverifiable_in_principle") return `Unverifiable in principle (preference / aesthetic / counterfactual)`;
    if (status === "unverifiable_pending_implementation") return `Unverifiable — verifier-side error`;
  }
  // Catch-all.
  return `${status || outcome || tier || "unknown"}`;
}

// Evidence block — picks the right renderer based on what the walker
// actually surfaced (matching fact for U/W, derivation chain for
// derived, snippets/code for fresh, validator failure for anomaly).
// Returns null when there's nothing meaningful to show (rare —
// walker.evidence is usually populated).
function _renderClaimEvidence(walker, claim) {
  const ev = walker.evidence;
  const tier = walker.served_from_tier;

  // Routing anomaly: render the validator's failed-invariant explanation.
  if (tier === "routing_anomaly") {
    const notes = walker.notes || [];
    if (notes.length) {
      const wrap = el("div", { className: "claim-evidence claim-evidence-anomaly" });
      wrap.appendChild(el("div", { className: "claim-evidence-label",
        textContent: "validator rejection" }));
      notes.forEach((n) => {
        wrap.appendChild(el("div", { className: "claim-evidence-anomaly-line",
          textContent: n }));
      });
      return wrap;
    }
    return null;
  }

  // Derivation: render the chain (trace.js helper).
  if (tier === "derivation"
      && walker.derivation_path && walker.derivation_path.length) {
    if (window.Aedos && window.Aedos.renderChain) {
      const wrap = el("div", { className: "claim-evidence claim-evidence-derivation" });
      wrap.appendChild(window.Aedos.renderChain(
        walker.derivation_path, walker.chain_reliability,
      ));
      return wrap;
    }
  }

  // Tier U match: show the matching fact's identity (we have row id;
  // detailed slot diff requires a fetch, deferred to v0.15).
  if (tier === "u" && walker.matching_fact_id != null) {
    const wrap = el("div", { className: "claim-evidence claim-evidence-tier-u" });
    wrap.appendChild(el("div", { className: "claim-evidence-label",
      textContent: "matched user fact" }));
    wrap.appendChild(el("div", { className: "claim-evidence-row mono",
      textContent: `Tier U row #${walker.matching_fact_id}` }));
    if (walker.notes && walker.notes.length) {
      walker.notes.forEach((n) => {
        wrap.appendChild(el("div", { className: "claim-evidence-note",
          textContent: n }));
      });
    }
    return wrap;
  }
  if (tier === "u" && walker.contradicting_fact_id != null) {
    const wrap = el("div", { className: "claim-evidence claim-evidence-tier-u" });
    wrap.appendChild(el("div", { className: "claim-evidence-label",
      textContent: "contradicting user fact" }));
    wrap.appendChild(el("div", { className: "claim-evidence-row mono",
      textContent: `Tier U row #${walker.contradicting_fact_id}` }));
    return wrap;
  }

  // Tier W match: cache row id + cached verdict.
  if (tier === "w" && (walker.matching_w_row_id != null
                      || walker.contradicting_w_row_id != null)) {
    const rowId = walker.matching_w_row_id ?? walker.contradicting_w_row_id;
    const wrap = el("div", { className: "claim-evidence claim-evidence-tier-w" });
    wrap.appendChild(el("div", { className: "claim-evidence-label",
      textContent: "cached verifier verdict" }));
    wrap.appendChild(el("div", { className: "claim-evidence-row mono",
      textContent: `Tier W row #${rowId}` }));
    if (ev && typeof ev === "object") {
      const evDump = el("details", { className: "claim-evidence-evdump" });
      evDump.appendChild(el("summary", { textContent: "cached evidence" }));
      evDump.appendChild(el("pre", { className: "json-dump",
        textContent: JSON.stringify(ev, null, 2).slice(0, 2000) }));
      wrap.appendChild(evDump);
    }
    return wrap;
  }

  // Fresh tier: dispatch by evidence shape (retrieval vs python).
  if (ev) {
    if (ev.attempts || ev.snippets || ev.verdict) {
      const wrap = el("div", { className: "claim-evidence claim-evidence-fresh" });
      wrap.appendChild(el("div", { className: "claim-evidence-label",
        textContent: "retrieval evidence" }));
      wrap.appendChild(renderRetrievalBlock(ev));
      return wrap;
    }
    if (ev.code || ev.execution || ev.trace) {
      const wrap = el("div", { className: "claim-evidence claim-evidence-fresh" });
      wrap.appendChild(el("div", { className: "claim-evidence-label",
        textContent: "python verifier" }));
      wrap.appendChild(renderCodeGenBlock(ev));
      return wrap;
    }
  }

  // Walker notes (last-resort textual evidence — e.g., when the
  // verifier returned no structured payload but the walker's notes
  // explain the verdict).
  if (walker.notes && walker.notes.length) {
    const wrap = el("div", { className: "claim-evidence claim-evidence-notes" });
    walker.notes.forEach((n) => {
      wrap.appendChild(el("div", { className: "claim-evidence-note",
        textContent: n }));
    });
    return wrap;
  }
  return null;
}

function renderThinkingNode(step) {
  const node = el("div", { className: "flow-step flow-step-thinking", dataset: { stage: step.kind } });
  node.appendChild(el("div", { className: "flow-step-title", textContent: step.title }));
  const meta = el("div", { className: "flow-step-meta thinking-dots" });
  meta.appendChild(document.createTextNode("thinking"));
  meta.appendChild(el("span", { className: "thinking-anim" }));
  node.appendChild(meta);
  return node;
}

function arrowDown() {
  return el("div", { className: "flow-arrow", textContent: "↓" });
}

// =====================================================================
// 5. inline stage detail (replaces the standalone Trace tab)
// =====================================================================
//
// One renderStepDetail dispatches to the right per-step renderer.
// Annotations (cache events, routing decisions, code-gen sub-stages,
// substitution warnings) render below the step's primary detail as
// collapsible blocks.

function renderClaimBlock(claim) {
  const wrap = el("div", { className: "claim-block" });
  wrap.appendChild(el("div", { className: "claim-header" }, [
    el("span", { className: "pattern-badge", textContent: claim.pattern || "?" }),
    el("span", { className: "pred", textContent: claim.predicate || "?" }),
    el("span", { className: claim.polarity === 1 ? "pol-pos" : "pol-neg",
                 textContent: claim.polarity === 1 ? "+" : "−" }),
  ]));
  const slots = claim.slots || {};
  if (Object.keys(slots).length) {
    const tbl = el("div", { className: "slots-table" });
    for (const [k, v] of Object.entries(slots)) {
      tbl.appendChild(el("span", { className: "slot-name", textContent: k }));
      tbl.appendChild(el("span", { className: "slot-value",
        textContent: typeof v === "object" ? JSON.stringify(v) : String(v) }));
    }
    wrap.appendChild(tbl);
  }
  if (claim.source_text) {
    wrap.appendChild(el("div", { className: "src", textContent: `"${claim.source_text}"` }));
  }
  return wrap;
}

function renderRoutingBlock(rd) {
  const wrap = el("div", { className: "routing-block" });
  wrap.appendChild(el("div", { className: "routing-method",
    textContent: `routed to ${rd.method}` }));
  if (rd.reason) wrap.appendChild(el("div", { className: "routing-reason", textContent: rd.reason }));
  return wrap;
}

function renderCodeGenBlock(cg) {
  const wrap = el("details", { className: "code-gen-block" });
  const summary = el("summary", { textContent: `code-gen: ${cg.status}` });
  wrap.appendChild(summary);
  const tr = cg.trace || {};
  if (tr.prompt) {
    wrap.appendChild(el("h5", { textContent: "Neutral prompt" }));
    wrap.appendChild(el("pre", { textContent: tr.prompt.text || JSON.stringify(tr.prompt) }));
  }
  if (tr.code) {
    wrap.appendChild(el("h5", { textContent: "Generated code" }));
    wrap.appendChild(el("pre", { textContent: tr.code.code || "" }));
  }
  if (tr.execution) {
    wrap.appendChild(el("h5", { textContent: "Execution" }));
    const ex = tr.execution;
    wrap.appendChild(el("pre", {
      textContent: `stdout: ${ex.stdout || ""}\nstderr: ${ex.stderr || ""}\nexit=${ex.exit_code} duration=${ex.duration_ms}ms`,
    }));
  }
  if (cg.actual_value !== undefined) {
    wrap.appendChild(el("div", { className: "kv",
      textContent: `actual_value = ${JSON.stringify(cg.actual_value)}` }));
  }
  if (cg.explanation) wrap.appendChild(el("div", { className: "verifier-explanation", textContent: cg.explanation }));
  return wrap;
}

function renderRetrievalBlock(rr) {
  const wrap = el("details", { className: "retrieval-block" });
  wrap.appendChild(el("summary", { textContent: `retrieval: ${rr.outcome}` }));
  (rr.attempts || []).forEach((a, i) => {
    const row = el("div", { className: "query-attempt" });
    row.appendChild(el("span", { className: "att-q", textContent: `#${i + 1} ${a.query}` }));
    row.appendChild(el("span", { textContent: `${a.result_count} results` }));
    if (a.used) row.appendChild(el("span", { className: "att-used", textContent: "✓ used" }));
    if (a.error) row.appendChild(el("span", { className: "att-error", textContent: a.error }));
    wrap.appendChild(row);
  });
  (rr.snippets || []).forEach((s) => {
    const sn = el("div", { className: "snippet" });
    sn.appendChild(el("div", { className: "snippet-title", textContent: s.title }));
    sn.appendChild(el("div", { className: "snippet-body", textContent: s.snippet }));
    sn.appendChild(el("div", { className: "snippet-url", textContent: s.url }));
    wrap.appendChild(sn);
  });
  if (rr.verdict) {
    wrap.appendChild(el("div", { className: `verdict verdict-${rr.verdict.verdict}`,
      textContent: `${rr.verdict.verdict}: ${rr.verdict.justification}` }));
  }
  if (rr.error_flag) {
    wrap.appendChild(el("div", { className: "error-flag", textContent: `⚠ ${rr.error_flag}: ${rr.explanation || ""}` }));
  }
  return wrap;
}

// ---- annotation renderer (used by the Inspector → Pipeline events tab) ----

function renderAnnotation(event) {
  const wrap = el("div", { className: `annotation annotation-${event.stage}` });
  wrap.appendChild(el("div", { className: "annotation-stage", textContent: event.stage }));
  const data = event.data || {};
  // Specialized renderers for the known annotation types.
  if (event.stage === "cache_lookup") renderCacheLookupAnnotation(wrap, data);
  else if (event.stage === "cache_write") renderCacheWriteAnnotation(wrap, data);
  else if (event.stage === "cache_scoping_decision") renderCacheScopingAnnotation(wrap, data);
  else if (event.stage === "cache_stability_decision") renderCacheStabilityAnnotation(wrap, data);
  else if (event.stage === "extractor_substitution_warning") renderSubstitutionAnnotation(wrap, data);
  else if (event.stage === "routing_anomaly_detected") renderRoutingAnomalyAnnotation(wrap, data);
  else if (event.stage === "verifier_failure") renderVerifierFailureAnnotation(wrap, data);
  else if (event.stage === "turn_cost") renderTurnCostAnnotation(wrap, data);
  // v0.14 specialized renderers
  else if (event.stage === "routing_decision") renderRoutingDecisionAnnotation(wrap, data);
  else if (event.stage === "tier_u_storage") renderTierUStorageAnnotation(wrap, data);
  else if (event.stage === "walker_decision") renderWalkerDecisionAnnotation(wrap, data);
  else if (event.stage === "claim_decision") renderClaimDecisionAnnotation(wrap, data);
  else if (event.stage === "user_extraction" || event.stage === "assistant_extraction") renderExtractionAnnotation(wrap, data);
  else {
    // Unknown / generic: dump JSON. Forward-compat with new event types.
    wrap.appendChild(el("pre", { className: "json-dump",
      textContent: JSON.stringify(data, null, 2).slice(0, 800) }));
  }
  return wrap;
}

// ---- v0.14 specialized renderers ----

function renderRoutingDecisionAnnotation(wrap, d) {
  const method = d.method || (d.decision || {}).method || "?";
  wrap.appendChild(el("span", { className: `routing-method routing-method-${method}`,
    textContent: method }));
  if (d.memo_hit !== undefined) {
    wrap.appendChild(el("span", { className: "routing-memo-tag",
      textContent: d.memo_hit ? "memo hit" : "memo miss" }));
  }
  const reason = (d.decision || {}).reason || d.reason;
  if (reason) {
    wrap.appendChild(el("div", { className: "routing-reason", textContent: reason }));
  }
}

function renderTierUStorageAnnotation(wrap, d) {
  const outcome = d.outcome || "?";
  wrap.appendChild(el("span", { className: `tier-u-outcome tier-u-outcome-${outcome}`,
    textContent: outcome }));
  const sids = d.session_ids_after || [];
  if (d.is_session_local) {
    wrap.appendChild(el("span", { className: "tier-u-tag",
      textContent: `session-local (${sids.join(", ") || "—"})` }));
  } else if (sids.length) {
    wrap.appendChild(el("span", { className: "tier-u-tag",
      textContent: `cross-session (${sids.length} session${sids.length === 1 ? "" : "s"})` }));
  } else {
    wrap.appendChild(el("span", { className: "tier-u-tag", textContent: "cross-session" }));
  }
  if (d.affirmed_count_after !== undefined) {
    wrap.appendChild(el("span", { className: "tier-u-counts",
      textContent: `affirmed=${d.affirmed_count_after}` }));
  }
  if (d.marker_detected_phrase) {
    wrap.appendChild(el("div", { className: "tier-u-marker",
      textContent: `marker: "${d.marker_detected_phrase}"` }));
  }
}

function renderWalkerDecisionAnnotation(wrap, d) {
  const tier = d.served_from_tier || "?";
  const outcome = d.outcome || "?";
  const status = d.verification_status || "?";
  wrap.appendChild(el("span", { className: `walker-tier walker-tier-${tier}`,
    textContent: `tier: ${tier}` }));
  wrap.appendChild(el("span", { className: `walker-outcome walker-outcome-${outcome}`,
    textContent: outcome }));
  wrap.appendChild(el("span", { className: `walker-status walker-status-${status}`,
    textContent: status }));
  const via = d.via || [];
  if (via.length) {
    const viaBox = el("div", { className: "walker-via" });
    viaBox.appendChild(el("span", { className: "walker-via-label", textContent: "via:" }));
    via.forEach((v) => {
      viaBox.appendChild(el("span", { className: "walker-via-pill", textContent: v }));
    });
    wrap.appendChild(viaBox);
  }
  if (d.derivation_path && d.derivation_path.length && window.Aedos && window.Aedos.renderChain) {
    wrap.appendChild(window.Aedos.renderChain(d.derivation_path, d.chain_reliability));
  }
}

function renderClaimDecisionAnnotation(wrap, d) {
  // Per-claim Layer 5 verdict: walker + decision_confidence +
  // intervention bundled. The most operator-useful event because it
  // surfaces the entire per-claim conclusion in one place.
  const claim = d.claim || {};
  const walker = d.walker || {};
  const conf = d.confidence || {};
  const iv = d.intervention || {};
  const T = d.threshold !== undefined ? d.threshold : 0.5;

  // Top-line claim summary.
  const claimLine = el("div", { className: "claim-summary mono" });
  const pol = claim.polarity === 0 ? "NOT " : "";
  const slots = Object.entries(claim.slots || {})
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
  claimLine.textContent = `${pol}${claim.pattern}.${claim.predicate}(${slots})`;
  wrap.appendChild(claimLine);

  // Walker line.
  const tier = walker.served_from_tier || "?";
  const outcome = walker.outcome || "?";
  const status = walker.verification_status || "?";
  const walkerLine = el("div", { className: "claim-walker-line" });
  walkerLine.appendChild(el("span", { className: `walker-tier walker-tier-${tier}`,
    textContent: tier }));
  walkerLine.appendChild(el("span", { className: `walker-outcome walker-outcome-${outcome}`,
    textContent: outcome }));
  walkerLine.appendChild(el("span", { className: `walker-status walker-status-${status}`,
    textContent: status }));
  wrap.appendChild(walkerLine);

  // Decision confidence + intervention pill (via trace.js helper).
  if (window.Aedos && window.Aedos.renderDecisionConfidence) {
    wrap.appendChild(window.Aedos.renderDecisionConfidence(conf, T, iv));
  }

  // Derivation chain when applicable.
  if (walker.derivation_path && walker.derivation_path.length
      && window.Aedos && window.Aedos.renderChain) {
    wrap.appendChild(window.Aedos.renderChain(
      walker.derivation_path, walker.chain_reliability));
  }
}

function renderExtractionAnnotation(wrap, d) {
  const facts = d.valid_facts || d.facts || [];
  if (!facts.length) {
    wrap.appendChild(el("span", { className: "extraction-empty",
      textContent: "no facts extracted" }));
    return;
  }
  wrap.appendChild(el("span", { className: "extraction-count",
    textContent: `${facts.length} fact${facts.length === 1 ? "" : "s"}` }));
  const list = el("ul", { className: "extraction-list mono" });
  facts.forEach((f) => {
    const pol = f.polarity === 0 ? "NOT " : "";
    const slots = Object.entries(f.slots || {})
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
    list.appendChild(el("li", {
      textContent: `${pol}${f.pattern}.${f.predicate}(${slots})`,
    }));
  });
  wrap.appendChild(list);
  if (d.rejected_facts && d.rejected_facts.length) {
    wrap.appendChild(el("div", { className: "extraction-rejected hint",
      textContent: `${d.rejected_facts.length} rejected` }));
  }
}

function renderCacheLookupAnnotation(wrap, d) {
  const result = d.result || (d.error ? "error" : "?");
  wrap.classList.add(`anno-${result}`);
  wrap.appendChild(el("span", {
    className: `cache-badge cache-badge-${result}`,
    textContent: result === "semantic_hit" ? "SEMANTIC HIT" : result.toUpperCase(),
  }));
  if (result === "hit" || result === "semantic_hit") {
    wrap.appendChild(document.createTextNode(
      ` ${d.verdict || "?"}` + (d.hit_count != null ? ` · hits=${d.hit_count}` : "")
      + (d.score != null ? ` · score=${d.score}` : "")));
  } else if (result === "miss") {
    wrap.appendChild(document.createTextNode(" no cached verdict"));
  } else if (d.error) {
    wrap.appendChild(document.createTextNode(` ${d.error}`));
  }
  if (d.canonical_key) {
    wrap.appendChild(el("div", { className: "mono cache-key",
      textContent: `key=${d.canonical_key.slice(0, 100)}` }));
  }
  if (d.matched_key && d.matched_key !== d.canonical_key) {
    wrap.appendChild(el("div", { className: "mono cache-key",
      textContent: `matched=${d.matched_key.slice(0, 100)}` }));
  }
}

function renderCacheWriteAnnotation(wrap, d) {
  if (d.error) {
    wrap.appendChild(el("span", { className: "cache-badge cache-badge-error", textContent: "WRITE ERR" }));
    wrap.appendChild(document.createTextNode(` ${d.error}`));
    return;
  }
  wrap.appendChild(el("span", { className: "cache-badge cache-badge-write", textContent: "WROTE" }));
  const ttl = d.ttl_seconds === null ? "never expires"
    : d.ttl_seconds != null ? `ttl=${d.ttl_seconds}s` : "no ttl";
  wrap.appendChild(document.createTextNode(` ${d.verdict || "?"} · ${d.stability_class || "?"} · ${ttl}`));
}

function renderCacheScopingAnnotation(wrap, d) {
  wrap.appendChild(renderCacheClaimHeader(d.claim));
  if (d.error) wrap.appendChild(el("div", { className: "anno-error", textContent: d.error }));
  else {
    const dec = d.decision || {};
    wrap.appendChild(el("div", {
      textContent: `scope=${dec.scope || "?"} — ${dec.reason || ""}`,
    }));
  }
}

function renderCacheStabilityAnnotation(wrap, d) {
  wrap.appendChild(renderCacheClaimHeader(d.claim));
  if (d.error) wrap.appendChild(el("div", { className: "anno-error", textContent: d.error }));
  else {
    const dec = d.decision || {};
    const ttl = dec.ttl_seconds === null ? "never expires"
      : dec.ttl_seconds === 0 ? "don't cache (volatile)"
      : `ttl=${dec.ttl_seconds}s`;
    wrap.appendChild(el("div", {
      textContent: `${dec.stability_class || "?"} · ${ttl} — ${dec.reason || ""}`,
    }));
  }
}

function renderCacheClaimHeader(claim) {
  const w = el("div", { className: "cache-claim-header" });
  if (!claim || (!claim.pattern && !claim.predicate)) {
    w.appendChild(el("span", { className: "cache-claim-missing", textContent: "(claim not recorded)" }));
    return w;
  }
  w.appendChild(el("span", { className: "pattern-badge", textContent: claim.pattern || "?" }));
  w.appendChild(el("span", { className: "cache-claim-pred", textContent: "." + (claim.predicate || "?") }));
  const slots = claim.slots || {};
  const slotKeys = Object.keys(slots);
  if (slotKeys.length) {
    const slotPairs = slotKeys.map((k) => {
      const v = slots[k];
      const vstr = typeof v === "object" ? JSON.stringify(v) : String(v);
      const trimmed = vstr.length > 40 ? vstr.slice(0, 40) + "…" : vstr;
      return `${k}=${trimmed}`;
    }).join(", ");
    w.appendChild(el("span", { className: "cache-claim-slots", textContent: ` (${slotPairs})` }));
  }
  return w;
}

function renderSubstitutionAnnotation(wrap, d) {
  const w = d.warning || {};
  const fact = d.fact || {};
  wrap.appendChild(el("strong", { textContent: "⚠ Extractor substitution" }));
  wrap.appendChild(el("div", { textContent: w.detail || "source_text doesn't match input" }));
  if (fact.source_text) {
    wrap.appendChild(el("div", { className: "mono",
      textContent: `extractor wrote: ${JSON.stringify(fact.source_text)}` }));
  }
}

function renderRoutingAnomalyAnnotation(wrap, d) {
  wrap.appendChild(el("strong", { textContent: "⚠ Routing anomaly" }));
  wrap.appendChild(el("div", { textContent: d.warning || "" }));
}

function renderVerifierFailureAnnotation(wrap, d) {
  const claim = d.claim || {};
  wrap.appendChild(el("strong", { textContent: "⚡ Verifier failure" }));
  wrap.appendChild(el("div", {
    textContent: `${claim.pattern}/${claim.predicate} — claim NOT hedged (verifier failure isn't evidence of uncertainty)`,
  }));
}

function renderTurnCostAnnotation(wrap, d) {
  const totalUsd = (d.total_usd ?? 0).toFixed(6);
  const totalCalls = d.total_calls ?? 0;
  wrap.appendChild(el("strong", { textContent: `$${totalUsd}` }));
  wrap.appendChild(document.createTextNode(
    ` · ${totalCalls} call(s) · ${d.total_input_tokens ?? 0}/${d.total_output_tokens ?? 0} tok in/out`));
  const byModel = d.by_model || {};
  Object.keys(byModel).sort().forEach((m) => {
    const slot = byModel[m];
    wrap.appendChild(el("div", { className: "mono",
      textContent: `${m}: $${(slot.total_usd ?? 0).toFixed(6)} (${slot.calls} calls)` }));
  });
}

// =====================================================================
// 6. inspector drawer
// =====================================================================

const inspector = $("#inspector");
const inspectorBackdrop = $("#inspector-backdrop");

function openInspector(initialTab) {
  inspector.classList.add("open");
  inspectorBackdrop.classList.add("open");
  if (initialTab) selectInspectorTab(initialTab);
  const active = $(".inspector-tab.active")?.dataset.inspectorTab;
  if (active === "patterns") refreshPatterns();
  else if (active === "memory") refreshMemory();
  else if (active === "pipeline") refreshPipelineEvents();
}

function closeInspector() {
  inspector.classList.remove("open");
  inspectorBackdrop.classList.remove("open");
}

function selectInspectorTab(name) {
  $$(".inspector-tab").forEach((b) => b.classList.toggle("active", b.dataset.inspectorTab === name));
  $$(".inspector-panel").forEach((p) => p.classList.toggle("active", p.id === `inspector-${name}`));
  if (name === "patterns") refreshPatterns();
  if (name === "memory") refreshMemory();
  if (name === "pipeline") refreshPipelineEvents();
  if (name === "routing-memo") refreshRoutingMemo();
}

// Re-trigger the active inspector tab's refresh — invoked after the
// "Reset DB" button so a Memory panel that was open before the wipe
// reflects the now-empty store immediately, no manual Refresh click.
function refreshActiveInspector() {
  if (!inspector.classList.contains("open")) return;
  const active = $(".inspector-tab.active")?.dataset.inspectorTab;
  if (active === "patterns") refreshPatterns();
  else if (active === "memory") refreshMemory();
  else if (active === "pipeline") refreshPipelineEvents();
  else if (active === "routing-memo") refreshRoutingMemo();
}

$("#inspector-btn").addEventListener("click", () => openInspector());
$("#inspector-close").addEventListener("click", closeInspector);
inspectorBackdrop.addEventListener("click", closeInspector);
$$(".inspector-tab").forEach((btn) => {
  btn.addEventListener("click", () => selectInspectorTab(btn.dataset.inspectorTab));
});

// ---- Patterns ----

async function refreshPatterns() {
  const patterns = await api("GET", "/api/patterns");
  const container = $("#predicates-table");
  container.innerHTML = "";
  patterns.forEach((p) => {
    const card = el("div", { className: "stage" });
    card.appendChild(el("div", { className: "stage-header" }, [
      el("span", { className: "pattern-badge", textContent: p.name }),
    ]));
    const body = el("div", { className: "stage-body" });
    body.appendChild(el("div", { textContent: p.description }));
    body.appendChild(el("h4", { textContent: "Slots" }));
    const slotsTbl = el("table");
    slotsTbl.appendChild(el("tr", {}, ["name", "type", "required"].map(
      (h) => el("th", { textContent: h }))));
    (p.slots || []).forEach((s) => {
      slotsTbl.appendChild(el("tr", {}, [
        el("td", { className: "mono", textContent: s.name }),
        el("td", { textContent: s.type }),
        el("td", { textContent: s.required ? "✓" : "" }),
      ]));
    });
    body.appendChild(slotsTbl);
    if ((p.example_predicates || []).length) {
      body.appendChild(el("h4", { textContent: "Example predicates" }));
      body.appendChild(el("div", { className: "mono",
        textContent: p.example_predicates.join(", ") }));
    }
    if ((p.query_strategy || []).length) {
      body.appendChild(el("h4", { textContent: "Query strategy" }));
      const ol = el("ol");
      p.query_strategy.forEach((q) => ol.appendChild(el("li", { className: "mono", textContent: q })));
      body.appendChild(ol);
    }
    card.appendChild(body);
    container.appendChild(card);
  });
}

// ---- Memory tab — four-scope view -------------------------------------
//
// Single home for everything fact-shaped. Mirrors the tiered
// verifier (microtheory → user store → world cache) plus a Model
// bucket for the assistant's own claims.
//
//   * Conversation = user-asserted facts with session_id  (Tier 1)
//   * User         = user-asserted facts, session_id NULL (Tier 2)
//   * Model        = facts with asserted_by=model
//   * World        = verification_cache rows               (Tier 3)
//
// Loads facts + cache in parallel so switching scopes is instant.

let _memoryFactsByScope = { conversation: [], user: [], model: [] };

// Render a fact-shaped row as a natural-language-ish sentence + a
// "see details" expander. Replaces the dense "id | pattern |
// predicate | slots | pol | conf | status | valid_until | turn"
// table, which buried the actual claim under metadata.
function _renderMemoryFactsTable(facts, container) {
  container.innerHTML = "";
  if (!facts.length) {
    container.appendChild(el("p", { className: "hint",
      textContent: "(no facts in this scope yet)" }));
    return;
  }
  const table = el("table", { className: "memory-facts-table" });
  table.appendChild(el("tr", {}, [
    "claim", "status", "conf", "asserted by", "turn",
  ].map((h) => el("th", { textContent: h }))));
  facts.forEach((f) => {
    const row = el("tr", {});
    if (f.valid_until) row.classList.add("closed");
    if (f.verification_status === "verified") row.classList.add("verified");
    if (f.verification_status === "contradicted") row.classList.add("contradicted");
    const claimCell = el("td", { className: "memory-claim-cell" });
    claimCell.appendChild(el("span", { className: "memory-claim-text",
      textContent: humanizeFact(f) }));
    if (f.valid_until) {
      claimCell.appendChild(el("span", { className: "memory-claim-meta",
        textContent: ` · valid until ${f.valid_until}` }));
    }
    row.appendChild(claimCell);
    [
      f.verification_status,
      (f.confidence ?? 0).toFixed(2),
      f.asserted_by,
      f.source_turn_id,
    ].forEach((v) => row.appendChild(el("td", { textContent: String(v) })));
    table.appendChild(row);
  });
  container.appendChild(table);
}

function renderMemoryConversation() {
  const container = $("#memory-conversation-table");
  container.innerHTML = "";
  const facts = _memoryFactsByScope.conversation;
  if (!facts.length) {
    container.appendChild(el("p", { className: "hint",
      textContent: "No session-local user facts yet. Tier U session-local facts are the user's \"let's say for this conversation\" hypotheticals, scoped to one session by the marker check." }));
    return;
  }
  // Group by session id (each session-local fact has exactly one entry
  // in session_ids). Cross-session facts go to the User scope, not
  // here.
  const bySession = {};
  facts.forEach((f) => {
    const sids = f.session_ids || [];
    const k = sids.length ? sids[0] : "(unset)";
    (bySession[k] ||= []).push(f);
  });
  Object.keys(bySession).sort().forEach((sid) => {
    const group = el("div", { className: "memory-session-group" });
    const header = el("div", { className: "memory-session-header" });
    header.appendChild(el("span", { textContent: "session" }));
    header.appendChild(el("span", { className: "memory-session-id",
      textContent: sid }));
    header.appendChild(el("span", {
      textContent: `${bySession[sid].length} fact(s)` }));
    group.appendChild(header);
    const tableHolder = el("div");
    _renderMemoryFactsTable(bySession[sid], tableHolder);
    group.appendChild(tableHolder);
    container.appendChild(group);
  });
}

function renderMemoryUser() {
  _renderMemoryFactsTable(
    _memoryFactsByScope.user, $("#memory-user-table"),
  );
}

function renderMemoryModel() {
  _renderMemoryFactsTable(
    _memoryFactsByScope.model, $("#memory-model-table"),
  );
}

function selectMemoryScope(scope) {
  $$(".memory-scope-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.memoryScope === scope));
  $$(".memory-section").forEach((s) =>
    s.classList.toggle("active", s.id === `memory-scope-${scope}`));
}

function _setScopeCount(scope, n) {
  const badge = document.querySelector(`[data-memory-count="${scope}"]`);
  if (badge) badge.textContent = String(n);
}

async function refreshMemory() {
  // Two API calls in parallel — keeps switching scopes instant.
  const [allFacts, cacheData] = await Promise.all([
    api("GET", "/api/facts"),
    api("GET", "/api/cache"),
  ]);
  // v0.14 model: Tier U has both session-local + cross-session
  // user-asserted facts. Anything asserted_by != "user" is the model-
  // claims dual-write trail (asserted_by=model for the model's own
  // assertion + asserted_by=python_verifier for verifier-corrected
  // values).
  //
  //   Conversation = is_session_local=1 (Tier U session-local)
  //   User         = asserted_by="user" + is_session_local=0 (Tier U cross-session)
  //   Model        = asserted_by != "user" (model + python_verifier rows)
  //   World        = Tier W (verification_cache, served via /api/cache)
  _memoryFactsByScope.conversation = allFacts.filter(
    (f) => f.asserted_by === "user" && f.is_session_local === 1);
  _memoryFactsByScope.user = allFacts.filter(
    (f) => f.asserted_by === "user" && f.is_session_local !== 1);
  _memoryFactsByScope.model = allFacts.filter(
    (f) => f.asserted_by !== "user");
  _setScopeCount("conversation", _memoryFactsByScope.conversation.length);
  _setScopeCount("user", _memoryFactsByScope.user.length);
  _setScopeCount("model", _memoryFactsByScope.model.length);
  _setScopeCount("world", (cacheData.entries || []).length);
  renderMemoryConversation();
  renderMemoryUser();
  renderMemoryModel();
  renderWorldCache(cacheData);
}

$$(".memory-scope-tab").forEach((btn) => {
  btn.addEventListener("click", () =>
    selectMemoryScope(btn.dataset.memoryScope));
});
$("#memory-refresh").addEventListener("click", refreshMemory);

// ---- humanize helpers — pull a readable sentence out of a fact /
// cache entry so the table leads with the claim itself rather than
// metadata.

// Per-pattern slot ordering for readable subject-first sentences.
// Mirrors KEY_SLOTS_BY_PATTERN in src/router/constants.py — kept in
// sync manually since the UI doesn't fetch it.
const _SLOT_ORDER_BY_PATTERN = {
  "preference":             ["agent", "object"],
  "propositional_attitude": ["agent", "proposition"],
  "spatial_temporal":       ["entity", "location"],
  "categorical":            ["entity", "category"],
  "role_assignment":        ["agent", "role", "org"],
  "relational":             ["subject", "object"],
  "quantitative":           ["subject", "property"],
  "event":                  ["event_type", "occurred_at"],
  // v0.14 added mereological as a separate pattern (constitutive
  // parthood; locational containment stays in spatial_temporal).
  "mereological":           ["part", "whole"],
};

function _humanizePredicate(pred) {
  return String(pred || "").replace(/_/g, " ");
}

function _orderedSlotValues(pattern, slots) {
  const order = _SLOT_ORDER_BY_PATTERN[pattern]
    || Object.keys(slots || {}).sort();
  const out = [];
  const seen = new Set();
  order.forEach((k) => {
    if (slots && slots[k] != null && String(slots[k]).length) {
      out.push(String(slots[k]));
      seen.add(k);
    }
  });
  // Append any slots we didn't have an order for, alphabetically.
  Object.keys(slots || {}).sort().forEach((k) => {
    if (!seen.has(k) && slots[k] != null && String(slots[k]).length) {
      out.push(String(slots[k]));
    }
  });
  return out;
}

// Format a Fact (from /api/facts) as "<subject> <predicate> <obj>".
// Polarity 0 prepends "NOT ". Falls back to the JSON-y form if slots
// are missing.
function humanizeFact(f) {
  const pred = _humanizePredicate(f.predicate);
  const values = _orderedSlotValues(f.pattern, f.slots);
  const not = (f.polarity === 0 || f.polarity === "0") ? "NOT " : "";
  if (values.length === 0) return `${not}${pred}`;
  if (values.length === 1) return `${not}${values[0]} ${pred}`;
  const [subj, ...rest] = values;
  return `${not}${subj} ${pred} ${rest.join(" · ")}`;
}

// Parse a canonical_key like
//   pattern|predicate|p=POL|t=TENSE|slot1=v1&slot2=v2
// and produce the same humanized sentence. Cache rows don't carry a
// `slots` dict — only the canonical key — so we rebuild it here.
function humanizeCacheKey(key, pattern, predicate) {
  const parts = String(key || "").split("|");
  if (parts.length < 5) return key || "";
  const polarity = parts[2];   // "p=1" or "p=0"
  const slotsBlock = parts.slice(4).join("|");
  const slots = {};
  slotsBlock.split("&").forEach((kv) => {
    if (!kv) return;
    const eq = kv.indexOf("=");
    if (eq < 0) return;
    slots[kv.slice(0, eq)] = kv.slice(eq + 1);
  });
  return humanizeFact({
    pattern: pattern || parts[0],
    predicate: predicate || parts[1],
    slots,
    polarity: polarity === "p=0" ? 0 : 1,
  });
}

// ---- World cache (Tier 3) — the original /api/cache view ------------

async function renderWorldCache(data) {
  const stats = data.stats || {};
  const health = data.health || {};
  const recent = data.recent_invalidations || [];
  const statsEl = $("#cache-stats");
  statsEl.innerHTML = "";

  // Top-line totals (existing).
  statsEl.appendChild(el("div", { className: "cache-totals",
    textContent: `${stats.total_entries || 0} entries · ${stats.immutable_entries || 0} immutable · ${stats.total_hits || 0} per-entry hits accumulated` }));
  const lookups = stats.lookups || 0;
  if (lookups > 0) {
    const rate = stats.hit_rate;
    const ratePct = rate !== null && rate !== undefined ? `${(rate * 100).toFixed(1)}%` : "—";
    const rateLine = el("div", { className: "cache-hit-rate" });
    rateLine.appendChild(el("strong", { textContent: `Hit rate: ${ratePct}` }));
    rateLine.appendChild(document.createTextNode(
      ` · ${lookups} lookups (${stats.lookup_hits || 0} hits, ${stats.lookup_misses || 0} misses${stats.lookup_errors ? `, ${stats.lookup_errors} errors` : ""})`));
    statsEl.appendChild(rateLine);
    const byStab = stats.hits_by_stability || {};
    const stabKeys = Object.keys(byStab).sort();
    if (stabKeys.length) {
      statsEl.appendChild(el("div", { className: "cache-by-stability",
        textContent: "  Hits by class: " + stabKeys.map((k) => `${k}=${byStab[k]}`).join(" · ") }));
    }
  }

  // Health line — contradictions + refreshes + oldest entry.
  const everC = health.ever_contradicted_entries ?? 0;
  const totC = health.total_contradictions ?? 0;
  const totR = health.total_refreshes ?? 0;
  if (everC || totR || health.oldest_cached_at) {
    const healthLine = el("div", { className: "cache-health" });
    healthLine.appendChild(el("span", {
      textContent: `${everC} entries ever contradicted (${totC} total flips)` }));
    healthLine.appendChild(document.createTextNode(" · "));
    healthLine.appendChild(el("span", {
      textContent: `${totR} refreshes confirmed` }));
    if (health.oldest_cached_at) {
      healthLine.appendChild(document.createTextNode(" · "));
      healthLine.appendChild(el("span", {
        className: "cache-oldest",
        textContent: `oldest entry: ${health.oldest_cached_at.slice(0, 10)}` }));
    }
    statsEl.appendChild(healthLine);
  }

  // Recent invalidations (collapsed audit log).
  if (recent.length > 0) {
    const audit = el("details", { className: "cache-audit" });
    audit.appendChild(el("summary", {
      textContent: `Recent invalidations (${recent.length})` }));
    recent.forEach((inv) => {
      const row = el("div", { className: "cache-audit-row" });
      const when = (inv.created_at || "").slice(0, 19).replace("T", " ");
      row.appendChild(el("span", { className: "mono", textContent: when }));
      row.appendChild(el("span", { className: `cache-audit-reason cache-audit-${inv.reason}`,
        textContent: inv.reason }));
      row.appendChild(el("span", { className: "mono cache-audit-key",
        textContent: (inv.primary_key || "").slice(0, 80) }));
      const propN = (inv.propagated_to_keys || []).length;
      if (propN > 0) {
        row.appendChild(el("span", { className: "cache-audit-prop",
          textContent: `+${propN} cascaded` }));
      }
      audit.appendChild(row);
    });
    statsEl.appendChild(audit);
  }

  const container = $("#cache-table");
  container.innerHTML = "";

  // Search filter row.
  const filterBar = el("div", { className: "cache-filter-bar" });
  const searchInput = el("input", {
    className: "cache-search-input",
    title: "Filter entries by canonical key, pattern, or predicate",
  });
  searchInput.placeholder = "filter (pattern / predicate / key substring)";
  searchInput.value = _cacheFilterText;
  filterBar.appendChild(searchInput);
  searchInput.addEventListener("input", () => {
    _cacheFilterText = searchInput.value.toLowerCase();
    renderCacheTable();
  });
  container.appendChild(filterBar);

  _cacheEntries = data.entries || [];
  renderCacheTable();
}

let _cacheEntries = [];
let _cacheFilterText = "";

function renderCacheTable() {
  const container = $("#cache-table");
  // Wipe everything below the filter bar.
  container.querySelectorAll("table, .hint:not(.cache-no-entries-initial)").forEach((n) => n.remove());

  const text = _cacheFilterText;
  const filtered = _cacheEntries.filter((e) => {
    if (!text) return true;
    return ((e.canonical_key || "").toLowerCase().includes(text)
         || (e.pattern || "").toLowerCase().includes(text)
         || (e.predicate || "").toLowerCase().includes(text));
  });
  if (!filtered.length) {
    container.appendChild(el("p", { className: "hint",
      textContent: _cacheEntries.length === 0
        ? "Cache is empty. Run some retrieval-territory turns and successful verdicts will land here automatically."
        : "(no entries match the current filter)" }));
    return;
  }
  const table = el("table", { className: "memory-facts-table" });
  table.appendChild(el("tr", {}, [
    "claim", "verdict", "stability", "hits", "expires", "actions",
  ].map((h) => el("th", { textContent: h }))));
  filtered.forEach((e) => {
    const row = el("tr", {});
    if (e.is_expired) row.classList.add("closed");
    if (e.verdict === "verified") row.classList.add("verified");
    if (e.verdict === "contradicted") row.classList.add("contradicted");

    // Claim cell: the natural-language sentence parsed out of the
    // canonical_key, with the raw key still available via title.
    const claimCell = el("td", { className: "memory-claim-cell" });
    const sentence = humanizeCacheKey(
      e.canonical_key, e.pattern, e.predicate,
    );
    claimCell.appendChild(el("span", {
      className: "memory-claim-text", textContent: sentence,
      title: e.canonical_key || "",
    }));
    const refresh = e.refresh_count ?? 0;
    const contradictions = e.contradiction_count ?? 0;
    if (refresh || contradictions) {
      const meta = [];
      if (refresh) meta.push(`${refresh} refresh${refresh === 1 ? "" : "es"}`);
      if (contradictions) meta.push(`${contradictions} flip${contradictions === 1 ? "" : "s"}`);
      claimCell.appendChild(el("span", { className: "memory-claim-meta",
        textContent: ` · ${meta.join(" · ")}` }));
    }
    row.appendChild(claimCell);

    [
      e.verdict,
      e.stability_class,
      String(e.hit_count ?? 0),
      e.expires_at ? (e.is_expired ? `${e.expires_at} (EXP)` : e.expires_at) : "(never)",
    ].forEach((v) => row.appendChild(el("td", { textContent: String(v) })));

    // Per-row actions are v0.15 work (cascade-invalidation discipline);
    // the world cache is read-only for v0.14.x. Operators wipe the
    // entire store via the header's Reset DB button when needed.
    table.appendChild(row);
  });
  container.appendChild(table);
}

// ---- Pipeline events (raw event log for the latest assistant turn) ----
//
// All the per-event detail that used to clutter the in-card flow lives
// here. Chronological list of every pipeline_events row for the most
// recent assistant turn, grouped by stage, with the data dict shown
// inline via the existing renderAnnotation helpers.

async function refreshPipelineEvents() {
  const container = $("#pipeline-events");
  const stats = $("#pipeline-stats");
  container.innerHTML = "";
  stats.textContent = "loading…";
  try {
    // Show ALL turns — user AND assistant. User turns carry the
    // v0.10.0 user-world-claim verification events (routing_decision,
    // code_generated, code_executed) since _route_user logs to the
    // user's turn_id. Filtering to assistant-only hid those events.
    const turns = await api("GET", "/api/turns");
    if (!turns.length) {
      stats.textContent = "no turns yet";
      container.appendChild(el("p", { className: "hint",
        textContent: "Send a message — pipeline events will appear here." }));
      return;
    }
    const allEvents = await Promise.all(
      turns.map((t) => api("GET", `/api/trace/${t.id}`).catch(() => [])),
    );
    const totalEvents = allEvents.reduce((acc, e) => acc + e.length, 0);
    const userN = turns.filter((t) => t.role === "user").length;
    const asstN = turns.filter((t) => t.role === "assistant").length;
    stats.textContent =
      `${userN} user · ${asstN} assistant turn${asstN === 1 ? "" : "s"}` +
      ` · ${totalEvents} event${totalEvents === 1 ? "" : "s"} total`;
    // Newest-first so the most recent turn-pair is on top. Latest
    // assistant turn AND its preceding user turn open by default;
    // older turns collapse so long conversations don't drown the
    // panel. Pair them by walking backwards.
    const reversed = [...turns].reverse();
    const newestAsstId = (turns.filter((t) => t.role === "assistant").pop() || {}).id;
    let openLatestPair = newestAsstId != null;
    let userTurnFollowsAsst = false;
    reversed.forEach((turn, i) => {
      const events = allEvents[turns.indexOf(turn)];
      const block = el("details", { className: "pipeline-turn-block" });
      // Open: the newest assistant turn AND the user turn directly
      // above it (so the user-claim verification events are visible
      // alongside the assistant's pipeline).
      if (turn.id === newestAsstId) {
        block.setAttribute("open", "");
        userTurnFollowsAsst = true;
      } else if (userTurnFollowsAsst && turn.role === "user") {
        block.setAttribute("open", "");
        userTurnFollowsAsst = false;
      } else {
        userTurnFollowsAsst = false;
      }
      const summary = el("summary", { className: "pipeline-turn-summary" });
      const ts = (turn.created_at || "").slice(0, 19).replace("T", " ");
      summary.appendChild(el("span", {
        className: `pipeline-turn-role pipeline-turn-role-${turn.role}`,
        textContent: turn.role,
      }));
      summary.appendChild(el("span", {
        className: "pipeline-turn-id",
        textContent: `turn ${turn.id}`,
      }));
      summary.appendChild(el("span", {
        className: "pipeline-turn-when",
        textContent: ts,
      }));
      summary.appendChild(el("span", {
        className: "pipeline-turn-count",
        textContent: `${events.length} event${events.length === 1 ? "" : "s"}`,
      }));
      const preview = (turn.content || "").trim();
      if (preview) {
        summary.appendChild(el("span", {
          className: "pipeline-turn-preview",
          textContent: preview.length > 90
            ? preview.slice(0, 87) + "…"
            : preview,
        }));
      }
      block.appendChild(summary);
      if (events.length === 0) {
        block.appendChild(el("p", { className: "hint",
          textContent: "(no events for this turn)" }));
      } else {
        events.forEach((ev, eidx) => {
          const node = el("div", { className: "pipeline-event-row" });
          node.appendChild(el("span", { className: "pipeline-event-idx",
            textContent: `#${eidx + 1}` }));
          node.appendChild(el("span", { className: "pipeline-event-stage",
            textContent: ev.stage }));
          const body = el("div", { className: "pipeline-event-body" });
          body.appendChild(renderAnnotation(ev));
          node.appendChild(body);
          block.appendChild(node);
        });
      }
      container.appendChild(block);
    });
  } catch (err) {
    stats.textContent = "error";
    container.appendChild(el("p", { className: "hint", textContent: String(err) }));
  }
}

$("#pipeline-refresh").addEventListener("click", refreshPipelineEvents);

// =====================================================================
// 6.5. Routing memo inspector
// =====================================================================

async function refreshRoutingMemo() {
  const container = $("#routing-memo-table");
  const stats = $("#routing-memo-stats");
  container.innerHTML = "";
  stats.textContent = "loading…";
  try {
    const resp = await api("GET", "/api/routing-memo");
    const rows = resp.rows || [];
    if (!rows.length) {
      stats.textContent = "no memo rows yet";
      container.appendChild(el("p", { className: "hint",
        textContent: "Layer 2's classification cache fills as the LLM router classifies (pattern, predicate) pairs. Send some messages to populate it." }));
      return;
    }
    stats.textContent = `${rows.length} row${rows.length === 1 ? "" : "s"}`;
    const table = el("table", { className: "routing-memo-table" });
    const thead = el("thead");
    const headRow = el("tr");
    [
      "pattern", "predicate", "method", "reason", "✓", "✗",
      "created", "last consulted",
    ].forEach((h) => headRow.appendChild(el("th", { textContent: h })));
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el("tbody");
    rows.forEach((r) => {
      const tr = el("tr");
      const ts = (s) => (s || "").slice(0, 19).replace("T", " ");
      [
        r.pattern, r.predicate, r.method, r.reason || "—",
        String(r.affirmed_count || 0),
        String(r.contradicted_count || 0),
        ts(r.created_at), ts(r.last_consulted_at),
      ].forEach((v) => tr.appendChild(el("td", { textContent: String(v) })));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    container.appendChild(table);
  } catch (err) {
    stats.textContent = "error";
    container.appendChild(el("p", { className: "hint", textContent: String(err) }));
  }
}

$("#routing-memo-refresh").addEventListener("click", refreshRoutingMemo);

// =====================================================================
// 7. reset
// =====================================================================

$("#reset-btn").addEventListener("click", async () => {
  if (!confirm("Wipe every fact, turn, pipeline event, and cache entry. This is not reversible. Proceed?")) return;
  await api("POST", "/api/reset");
  messagesEl.innerHTML = "";
  flowContainer.innerHTML = "";
  flowContainer.appendChild(el("p", { className: "hint", textContent: "Database reset. Send a message to start fresh." }));
  flowStatus.textContent = "idle";
  expandedClaims.clear();
  // Re-load whichever inspector tab is open so a Memory panel that
  // was visible during the wipe reflects the empty store immediately
  // — no manual Refresh required.
  refreshActiveInspector();
});
