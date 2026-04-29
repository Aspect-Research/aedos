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
const modelSelect = $("#model-select");

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

function appendUserMessage(text) {
  const node = el("div", { className: "msg user" });
  node.appendChild(el("div", { className: "msg-body", textContent: text }));
  messagesEl.appendChild(node);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendHydratedAssistant(turn) {
  const node = el("div", { className: "msg assistant" });
  finalizeBubble(node, turn.content, turn.original_content);
  messagesEl.appendChild(node);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ---- word-level inline diff (LCS) ----

function diffWords(oldText, newText) {
  const tokenize = (s) => s.split(/(\s+)/).filter((t) => t.length > 0);
  const a = tokenize(oldText || "");
  const b = tokenize(newText || "");
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
  // Coalesce adjacent same-op tokens for cleaner span output.
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
  diffWords(oldText, newText).forEach(({ op, t }) => {
    if (op === "=") container.appendChild(document.createTextNode(t));
    else container.appendChild(el("span", {
      className: op === "+" ? "diff-ins" : "diff-del", textContent: t,
    }));
  });
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
      if (t.role === "user") appendUserMessage(t.content);
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
  const bubble = makeAssistantBubble();
  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;

  let assistantTurnId = null;
  const liveEvents = [];
  const requestStartMs = Date.now();
  flowStatus.textContent = "running…";
  expandedSteps.clear();
  expandedClaims.clear();
  renderFlow(null, [], { running: true, requestStartMs });

  try {
    await streamChat(
      { message: text, model: modelSelect.value || null },
      {
        onEvent: (ev) => {
          if (assistantTurnId === null) assistantTurnId = ev.turn_id;
          const arrivedMs = Date.now();
          liveEvents.push({
            turn_id: ev.turn_id, stage: ev.stage, data: ev.data,
            arrivedMs, created_at: new Date(arrivedMs).toISOString(),
          });
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
    kind: "correction",
    stage: "correction",
    title: "Correction",
    metaFn: (ev) => {
      const n = ((ev.data || {}).interventions || []).length;
      return n === 0 ? "no corrections needed" : `${n} intervention${n === 1 ? "" : "s"} applied`;
    },
  },
  {
    kind: "final",
    stage: "final",
    title: "Final Response",
    metaFn: (ev) => ((ev.data || {}).content || "").slice(0, 80),
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
  if (displayStatus === "verified") return "verified";
  if (displayStatus === "contradicted") return "contradicted";
  if (displayStatus === "inconclusive") return "inconclusive";
  return "not_applicable";
}

// Persistent expansion state: per-step (chat / correction / final) and
// per-claim (each row in the Claims card). Survive re-renders during
// SSE streaming; cleared when a new turn starts.
const expandedSteps = new Set();
const expandedClaims = new Set();

function renderFlow(turnId, events, { running = false, requestStartMs = null } = {}) {
  flowContainer.innerHTML = "";
  flowContainer.appendChild(buildFlowChart(events || [], turnId, running, requestStartMs));
}

// Determine state for a given step from the event map.
//   "done"      — primary event present (and, for claims, verification fired)
//   "in_flight" — claims-only: extraction fired but verification hasn't
//   "pending"   — nothing yet
function stepState(step, stageMap) {
  if (step.kind === "claims") {
    if (stageMap["verification"]) return "done";
    if (stageMap["assistant_extraction"]) return "in_flight";
    return "pending";
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
  const ev = stageMap[step.stage];
  return ev && typeof ev.arrivedMs === "number" ? ev.arrivedMs : null;
}

function buildFlowChart(events, turnId, running, requestStartMs) {
  const stageMap = {};
  events.forEach((e) => { stageMap[e.stage] = e; });

  // Index annotation events (everything not a primary step event) by
  // the step they belong to. With the combined Claims card,
  // extraction- and verification-related annotations both bucket
  // under "claims".
  const primaryStages = new Set(["chat_model_call", "assistant_extraction",
                                 "verification", "correction", "final"]);
  const annotationsByStep = { chat_model_call: [], claims: [], correction: [], final: [] };
  events.forEach((e) => {
    if (primaryStages.has(e.stage)) return;
    const bucket = annotationStepFor(e.stage);
    if (bucket && annotationsByStep[bucket]) annotationsByStep[bucket].push(e);
  });

  // Per-step duration = (this step's end) − (previous step's end). The
  // very first step's "previous" is the request-start timestamp.
  const durations = {};
  let prevMs = requestStartMs;
  for (const step of PIPELINE_STEPS) {
    const endMs = stepEndMs(step, stageMap);
    if (endMs != null && prevMs != null) durations[step.kind] = endMs - prevMs;
    if (endMs != null) prevMs = endMs;
  }

  // Routing decisions (per-claim, fire during verification dispatch).
  // Used by the Claims card's in-flight view to flip individual
  // claim rows from "pending" → "in flight".
  const routingEvents = events.filter((e) => e.stage === "routing_decision");

  const chart = el("div", { className: "flow-chart" });

  // Render each step in order. Done/in-flight steps render their full
  // node. The first "pending" step renders as a thinking placeholder
  // (only while the turn is running); later pending steps don't render.
  let renderedAny = false;
  for (const step of PIPELINE_STEPS) {
    const state = stepState(step, stageMap);
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
      annotations: annotationsByStep[step.kind] || [],
      routingEvents,
    }));
    renderedAny = true;
  }

  if (!renderedAny) {
    chart.appendChild(el("p", { className: "hint",
      textContent: "Send a message — each stage will appear here as it lands." }));
  }
  return chart;
}

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
  // Single-event step (chat / correction / final).
  return renderEventStepNode(step, stageMap[step.stage], ctx.annotations, ctx.duration);
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

function renderEventStepNode(step, event, annotations, durationMs) {
  const wrapper = el("div", { className: "flow-step-wrapper" });
  const node = el("div", { className: "flow-step flow-step-done", dataset: { stage: step.stage } });
  node.appendChild(buildStepHeader(step.title, durationMs));
  const meta = step.metaFn ? step.metaFn(event) : "";
  if (meta) node.appendChild(el("div", { className: "flow-step-meta", textContent: meta }));

  const detail = el("div", { className: "flow-step-detail" });
  if (expandedSteps.has(step.kind)) detail.classList.add("expanded");
  renderStepDetail(detail, step, event, annotations);

  node.addEventListener("click", () => {
    if (expandedSteps.has(step.kind)) {
      expandedSteps.delete(step.kind);
      detail.classList.remove("expanded");
    } else {
      expandedSteps.add(step.kind);
      detail.classList.add("expanded");
    }
  });

  wrapper.appendChild(node);
  wrapper.appendChild(detail);
  return wrapper;
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
  const decisions = (verificationEvent && verificationEvent.data ? verificationEvent.data.decisions : []) || [];

  // Map decisions by claim key for quick lookup when verification is done.
  const decisionByKey = {};
  decisions.forEach((d) => { decisionByKey[claimKey(d.claim || {})] = d; });

  // Set of claim keys that have already routed (used while in-flight).
  const routedKeys = new Set(ctx.routingEvents.map((e) => claimKey((e.data || {}).claim || {})));

  const wrapper = el("div", { className: "flow-step-wrapper" });
  const node = el("div", {
    className: "flow-step flow-step-claims" + (state === "in_flight" ? " flow-step-thinking" : " flow-step-done"),
    dataset: { stage: "claims" },
  });

  // Header: title · N claims, with duration on the right (or
  // "verifying…" while in-flight).
  let summary = `${valid.length} claim${valid.length === 1 ? "" : "s"}`;
  let extraRight = null;
  if (state === "in_flight") {
    extraRight = el("span", { className: "flow-step-duration thinking-dots" }, [
      document.createTextNode("verifying"),
      el("span", { className: "thinking-anim" }),
    ]);
  }
  node.appendChild(buildStepHeader(`Claims · ${summary}`, ctx.duration, extraRight));

  if (valid.length === 0) {
    node.appendChild(el("div", { className: "flow-step-meta",
      textContent: "no claims extracted from the draft" }));
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

  // Card-level annotations (substitution warnings, turn-of-claim cache
  // events that didn't carry a claim). Tucked at the bottom so they
  // don't dominate the card.
  if (ctx.annotations.length > 0) {
    const annoWrap = el("div", { className: "claims-annotations" });
    const header = el("div", { className: "annotations-header",
      textContent: `${ctx.annotations.length} pipeline event${ctx.annotations.length === 1 ? "" : "s"}` });
    annoWrap.appendChild(header);
    const body = el("div", { className: "claims-annotations-body" });
    ctx.annotations.forEach((a) => body.appendChild(renderAnnotation(a)));
    annoWrap.appendChild(body);
    header.style.cursor = "pointer";
    header.addEventListener("click", (ev) => {
      ev.stopPropagation();
      annoWrap.classList.toggle("expanded");
    });
    node.appendChild(annoWrap);
  }

  wrapper.appendChild(node);
  return wrapper;
}

// One claim row inside the Claims card. The COLLAPSED row shows:
//   [dot]  source-text-or-summary  [pattern badge]  [verifier badge]
// The EXPANDED detail (only when row.classList contains 'expanded')
// shows ONLY this claim's Decision payload — slots, routing reason,
// code-gen / retrieval blocks, correction info, notes.
function renderClaimItem(claim, state, decision) {
  const key = claimKey(claim);
  const isExpandable = state === "done";
  const row = el("li", {
    className: `claim-row claim-${claimRowClass(state, decision)}`
      + (decision && decision.served_from_cache ? " claim-cached" : "")
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
    : ((decision && decision.routing_decision || {}).method || "?");
  summaryLine.appendChild(el("span", { className: "claim-method", textContent: methodText }));
  if (decision && decision.served_from_cache) {
    summaryLine.appendChild(el("span", { className: "cache-badge cache-badge-hit",
      title: "served from Tier 2 cache", textContent: "↺ cached" }));
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

function claimRowClass(state, decision) {
  if (state === "pending") return "pending";
  if (state === "in_flight") return "in-flight";
  return flowEdgeClass((decision && decision.display_status) || "inconclusive");
}
function claimDotState(state) {
  if (state === "pending") return "pending";
  if (state === "in_flight") return "pulsing";
  return "solid";
}

// Plain-text rendering of a claim. Prefer the source_text the
// extractor pulled from the message; fall back to a synthesized
// "subject predicate object" sentence if it's missing.
function claimDisplayText(claim) {
  if (claim.source_text && claim.source_text.trim()) return claim.source_text.trim();
  const slots = claim.slots || {};
  const subject = slots.subject || slots.holder || slots.entity || "";
  const object = slots.object || slots.role || slots.value || slots.target || "";
  const pred = (claim.predicate || "").replace(/_/g, " ");
  const polarity = claim.polarity === 0 ? "not " : "";
  const parts = [subject, polarity + pred, object].filter((p) => p && String(p).trim());
  return parts.length ? parts.join(" ") : "(unrecognized claim)";
}

function renderClaimDetailBody(container, claim, d) {
  // Per-claim Decision detail. Mirrors what the old "Verification"
  // detail block showed for one decision, restricted to this claim.
  const meta = [];
  if (typeof d.confidence === "number") meta.push(`conf=${d.confidence.toFixed(2)}`);
  if (d.stored_fact_id != null) meta.push(`stored fact id=${d.stored_fact_id}`);
  if (d.boosted_fact_id != null) meta.push(`boosted fact id=${d.boosted_fact_id}`);
  if (meta.length) container.appendChild(el("div", { className: "decision-meta", textContent: meta.join(" · ") }));

  // Status badges (verification + display).
  const badges = el("div", { className: "decision-header" }, [
    el("span", { className: `outcome outcome-${d.outcome}`, textContent: d.outcome }),
    el("span", { className: `display-status display-${d.display_status || "inconclusive"}`,
      textContent: d.display_status || d.verification_status }),
  ]);
  container.appendChild(badges);

  // Claim shape (slots, polarity).
  container.appendChild(renderClaimBlock(claim));

  // Routing decision, code-gen, retrieval, correction, notes.
  if (d.routing_decision) container.appendChild(renderRoutingBlock(d.routing_decision));
  if (d.code_gen_result) container.appendChild(renderCodeGenBlock(d.code_gen_result));
  if (d.retrieval_result) container.appendChild(renderRetrievalBlock(d.retrieval_result));
  if (d.correction) {
    container.appendChild(el("div", { className: "correction-block",
      textContent: `correction: ${JSON.stringify(d.correction.original_object)} → ${JSON.stringify(d.correction.corrected_object)}` }));
  }
  (d.notes || []).forEach((n) => {
    container.appendChild(el("div", { className: "verifier-explanation", textContent: n }));
  });
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

function renderStepDetail(container, step, event, annotations) {
  const data = event.data || {};
  // Per-step primary detail. The "claims" step has no whole-card
  // detail — it dispatches per-claim instead.
  if (step.kind === "chat_model_call") renderChatModelCallDetail(container, data);
  else if (step.kind === "correction") renderCorrectionDetail(container, data);
  else if (step.kind === "final") renderFinalDetail(container, data);

  if (annotations.length > 0) {
    const annoHeader = el("div", { className: "annotations-header",
      textContent: `${annotations.length} annotation${annotations.length === 1 ? "" : "s"}` });
    container.appendChild(annoHeader);
    annotations.forEach((a) => container.appendChild(renderAnnotation(a)));
  }
}

function renderChatModelCallDetail(container, data) {
  const tbl = el("dl", { className: "kv" });
  for (const [k, v] of Object.entries(data || {})) {
    tbl.appendChild(el("dt", { textContent: k }));
    tbl.appendChild(el("dd", { textContent: typeof v === "object" ? JSON.stringify(v) : String(v) }));
  }
  container.appendChild(tbl);
}

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
    textContent: `routed to ${rd.method} (conf=${(rd.confidence ?? 0).toFixed(2)})` }));
  if (rd.reason) wrap.appendChild(el("div", { className: "routing-reason", textContent: rd.reason }));
  return wrap;
}

function renderCodeGenBlock(cg) {
  const wrap = el("details", { className: "code-gen-block" });
  const summary = el("summary", { textContent: `code-gen: ${cg.status} (conf=${(cg.confidence ?? 0).toFixed(2)})` });
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

function renderCorrectionDetail(container, data) {
  if (!data.interventions || data.interventions.length === 0) {
    container.appendChild(el("p", { className: "hint", textContent: "no interventions applied" }));
    return;
  }
  data.interventions.forEach((iv) => {
    const node = el("div", { className: `intervention intervention-${iv.intervention_type}` });
    node.appendChild(el("span", { className: `intervention-type intervention-type-${iv.intervention_type}`,
      textContent: iv.intervention_type }));
    node.appendChild(el("div", { className: "intervention-reason", textContent: iv.reason }));
    if (iv.verified_value !== undefined && iv.verified_value !== null) {
      node.appendChild(el("div", { className: "intervention-value",
        textContent: `verified_value = ${JSON.stringify(iv.verified_value)}` }));
    }
    container.appendChild(node);
  });
  if (data.original && data.corrected && data.original !== data.corrected) {
    container.appendChild(el("h5", { textContent: "Inline diff" }));
    const diff = el("div", { className: "diff-box" });
    renderInlineDiff(diff, data.original, data.corrected);
    container.appendChild(diff);
  } else if (data.original && data.corrected) {
    container.appendChild(el("p", { className: "hint",
      textContent: "interventions planned but rewrite was identical to draft" }));
  }
}

function renderFinalDetail(container, data) {
  container.appendChild(el("div", { className: "draft-box", textContent: data.content || "" }));
}

// ---- annotation renderer (the long tail) ----

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
  else {
    // Unknown / generic: dump JSON. Forward-compat with new event types.
    wrap.appendChild(el("pre", { className: "json-dump",
      textContent: JSON.stringify(data, null, 2).slice(0, 800) }));
  }
  return wrap;
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
      textContent: `scope=${dec.scope || "?"} (conf=${(dec.confidence ?? 0).toFixed(2)}) — ${dec.reason || ""}`,
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
      textContent: `${dec.stability_class || "?"} (conf=${(dec.confidence ?? 0).toFixed(2)}) · ${ttl} — ${dec.reason || ""}`,
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
  // Refresh whichever tab is active.
  const active = $(".inspector-tab.active")?.dataset.inspectorTab;
  if (active === "facts") refreshFacts();
  else if (active === "patterns") refreshPatterns();
  else if (active === "cache") refreshCache();
}

function closeInspector() {
  inspector.classList.remove("open");
  inspectorBackdrop.classList.remove("open");
}

function selectInspectorTab(name) {
  $$(".inspector-tab").forEach((b) => b.classList.toggle("active", b.dataset.inspectorTab === name));
  $$(".inspector-panel").forEach((p) => p.classList.toggle("active", p.id === `inspector-${name}`));
  if (name === "facts") refreshFacts();
  if (name === "patterns") refreshPatterns();
  if (name === "cache") refreshCache();
}

$("#inspector-btn").addEventListener("click", () => openInspector());
$("#inspector-close").addEventListener("click", closeInspector);
inspectorBackdrop.addEventListener("click", closeInspector);
$$(".inspector-tab").forEach((btn) => {
  btn.addEventListener("click", () => selectInspectorTab(btn.dataset.inspectorTab));
});

// ---- Facts ----

async function refreshFacts() {
  const params = new URLSearchParams();
  const pat = $("#f-pattern").value;
  const pred = $("#f-predicate").value.trim();
  const ab = $("#f-asserted-by").value;
  const st = $("#f-status").value;
  if (pat) params.set("pattern", pat);
  if (pred) params.set("predicate", pred);
  if (ab) params.set("asserted_by", ab);
  if (st) params.set("verification_status", st);
  if ($("#f-only-valid").checked) params.set("only_valid", "true");

  const facts = await api("GET", "/api/facts?" + params.toString());
  const container = $("#facts-table");
  container.innerHTML = "";
  if (!facts.length) {
    container.appendChild(el("p", { className: "hint", textContent: "(no facts match)" }));
    return;
  }
  const table = el("table");
  table.appendChild(el("tr", {}, [
    "id", "pattern", "predicate", "slots", "pol", "conf", "asserted_by", "status", "valid_until", "turn",
  ].map((h) => el("th", { textContent: h }))));
  facts.forEach((f) => {
    const row = el("tr", {});
    if (f.valid_until) row.classList.add("closed");
    if (f.verification_status === "verified") row.classList.add("verified");
    if (f.verification_status === "contradicted") row.classList.add("contradicted");
    [
      f.id, f.pattern, f.predicate,
      JSON.stringify(f.slots), f.polarity,
      (f.confidence ?? 0).toFixed(2),
      f.asserted_by, f.verification_status,
      f.valid_until || "", f.source_turn_id,
    ].forEach((v) => row.appendChild(el("td", { textContent: String(v) })));
    table.appendChild(row);
  });
  container.appendChild(table);
}

["#f-predicate"].forEach((s) => {
  $(s).addEventListener("keydown", (e) => { if (e.key === "Enter") refreshFacts(); });
});
["#f-pattern", "#f-asserted-by", "#f-status", "#f-only-valid"].forEach((s) => {
  $(s).addEventListener("change", refreshFacts);
});
$("#facts-refresh").addEventListener("click", refreshFacts);

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

// ---- Cache ----

async function refreshCache() {
  const data = await api("GET", "/api/cache");
  const stats = data.stats || {};
  const statsEl = $("#cache-stats");
  statsEl.innerHTML = "";
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

  const container = $("#cache-table");
  container.innerHTML = "";
  const entries = data.entries || [];
  if (!entries.length) {
    container.appendChild(el("p", { className: "hint",
      textContent: "Cache is empty. Run some retrieval-territory turns and successful verdicts will land here automatically." }));
    return;
  }
  const table = el("table");
  table.appendChild(el("tr", {}, [
    "id", "verdict", "pattern", "predicate", "stability", "hits", "expires", "key",
  ].map((h) => el("th", { textContent: h }))));
  entries.forEach((e) => {
    const row = el("tr", {});
    if (e.is_expired) row.classList.add("closed");
    if (e.verdict === "verified") row.classList.add("verified");
    if (e.verdict === "contradicted") row.classList.add("contradicted");
    [
      e.id, e.verdict, e.pattern, e.predicate, e.stability_class,
      e.hit_count ?? 0,
      e.expires_at ? (e.is_expired ? `${e.expires_at} (EXPIRED)` : e.expires_at) : "(never)",
      e.canonical_key || "",
    ].forEach((v, i) => {
      const td = el("td", { textContent: String(v) });
      if (i >= 6) td.className = "mono";
      row.appendChild(td);
    });
    table.appendChild(row);
  });
  container.appendChild(table);
}

$("#cache-refresh").addEventListener("click", refreshCache);

// =====================================================================
// 7. model selector + reset
// =====================================================================

const MODEL_STORAGE_KEY = "aedos.selected_model";

async function populateModelSelect() {
  try {
    const data = await api("GET", "/api/models");
    modelSelect.innerHTML = "";
    (data.models || []).forEach((m) => {
      const opt = el("option", { textContent: m.label + (m.available ? "" : " — unavailable") });
      opt.value = m.id;
      if (!m.available) opt.disabled = true;
      modelSelect.appendChild(opt);
    });
    const saved = localStorage.getItem(MODEL_STORAGE_KEY);
    const ids = (data.models || []).map((m) => m.id);
    if (saved && ids.includes(saved)) {
      const opt = data.models.find((m) => m.id === saved);
      modelSelect.value = (opt && opt.available) ? saved : data.default;
    } else {
      modelSelect.value = data.default;
    }
  } catch (e) {
    console.error("populateModelSelect failed:", e);
  }
}
populateModelSelect();
modelSelect.addEventListener("change", () => {
  localStorage.setItem(MODEL_STORAGE_KEY, modelSelect.value);
});

$("#reset-btn").addEventListener("click", async () => {
  if (!confirm("Wipe every fact, turn, and pipeline event. This is not reversible. Proceed?")) return;
  await api("POST", "/api/reset");
  messagesEl.innerHTML = "";
  flowContainer.innerHTML = "";
  flowContainer.appendChild(el("p", { className: "hint", textContent: "Database reset. Send a message to start fresh." }));
  flowStatus.textContent = "idle";
  expandedSteps.clear();
});
