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
  body.textContent = draftText || "";
}

function finalizeBubble(bubble, finalText, originalText) {
  bubble.classList.remove("draft-pending", "draft-faded");
  const body = bubble.querySelector(".msg-body");
  body.textContent = finalText || "";
  // Strip any existing diff toggle from a previous render.
  bubble.querySelectorAll(".show-diff-btn, .diff-view").forEach((n) => n.remove());
  if (originalText && originalText !== finalText) {
    const btn = el("button", { className: "show-diff-btn", textContent: "show diff" });
    const diffView = el("div", { className: "diff-view" });
    renderInlineDiff(diffView, originalText, finalText);
    bubble.appendChild(btn);
    bubble.appendChild(diffView);
    btn.addEventListener("click", () => {
      const showing = bubble.classList.toggle("diff-open");
      btn.textContent = showing ? "hide diff" : "show diff";
    });
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
  node.appendChild(el("div", { className: "msg-body", textContent: turn.content }));
  if (turn.original_content && turn.original_content !== turn.content) {
    finalizeBubble(node, turn.content, turn.original_content);
  }
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
// Five progressive steps + (while running) one animated "thinking"
// bubble for the next expected step. Click any landed step to expand
// its detail INLINE under it (no tab switch — the trace lives where
// it's relevant).

const PIPELINE_STEPS = [
  {
    stage: "chat_model_call",
    title: "Chat Model",
    metaFn: (ev) => formatChatMeta(ev.data || {}),
  },
  {
    stage: "assistant_extraction",
    title: "Extraction",
    isExtractionList: true,
  },
  {
    stage: "verification",
    title: "Verification",
    isVerificationList: true,
  },
  {
    stage: "correction",
    title: "Correction",
    metaFn: (ev) => {
      const n = ((ev.data || {}).interventions || []).length;
      return n === 0 ? "no corrections needed" : `${n} intervention${n === 1 ? "" : "s"} applied`;
    },
  },
  {
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

// Track which steps are expanded so re-renders preserve state.
const expandedSteps = new Set();

function renderFlow(turnId, events, { running = false, requestStartMs = null } = {}) {
  flowContainer.innerHTML = "";
  flowContainer.appendChild(buildFlowChart(events || [], turnId, running, requestStartMs));
}

function buildFlowChart(events, turnId, running, requestStartMs) {
  const stageMap = {};
  events.forEach((e) => { stageMap[e.stage] = e; });

  // Index annotation events (everything not in PIPELINE_STEPS) by
  // their nearest PIPELINE_STEP for inline rendering. The bucketing
  // is approximate but stable.
  const stageNames = new Set(PIPELINE_STEPS.map((s) => s.stage));
  const annotationsByStep = {};
  for (const step of PIPELINE_STEPS) annotationsByStep[step.stage] = [];
  events.forEach((e) => {
    if (stageNames.has(e.stage)) return;
    const bucket = annotationStepFor(e.stage);
    if (bucket && annotationsByStep[bucket]) annotationsByStep[bucket].push(e);
  });

  const landed = [];
  for (const step of PIPELINE_STEPS) {
    if (stageMap[step.stage]) landed.push({ step, event: stageMap[step.stage] });
  }

  // Per-step duration: time between THIS step's event and PREVIOUS
  // step's event = elapsed time spent waiting for this step. For the
  // first landed step, fall back to (arrivedMs - requestStartMs).
  const durations = {};
  let prevMs = requestStartMs;
  for (const { step, event } of landed) {
    const ms = event.arrivedMs;
    if (typeof ms === "number" && typeof prevMs === "number") {
      durations[step.stage] = ms - prevMs;
    }
    if (typeof ms === "number") prevMs = ms;
  }

  let nextStep = null;
  const lastLandedIdx = landed.length > 0
    ? PIPELINE_STEPS.indexOf(landed[landed.length - 1].step)
    : -1;
  if (running && lastLandedIdx + 1 < PIPELINE_STEPS.length) {
    nextStep = PIPELINE_STEPS[lastLandedIdx + 1];
  }

  const chart = el("div", { className: "flow-chart" });

  if (landed.length === 0 && !nextStep) {
    chart.appendChild(el("p", { className: "hint",
      textContent: "Send a message — each stage will appear here as it lands." }));
    return chart;
  }

  // For the verification step's progressive view, we need the
  // extraction event (claim seeds) and any routing_decision events
  // (per-claim "in flight" → "done" transitions).
  const extractionEvent = stageMap["assistant_extraction"];
  const routingEvents = events.filter((e) => e.stage === "routing_decision");

  landed.forEach(({ step, event }, idx) => {
    if (idx > 0) chart.appendChild(arrowDown());
    chart.appendChild(renderStepNode(step, event, annotationsByStep[step.stage] || [], {
      duration: durations[step.stage],
      extractionEvent, routingEvents,
    }));
  });

  if (nextStep) {
    if (landed.length > 0) chart.appendChild(arrowDown());
    // Special-case: if we know what claims will be verified (extraction
    // landed) but verification hasn't, render the verification step
    // PROACTIVELY with claims as pending placeholders. This is the
    // "claims being verified in parallel" view the user wants.
    if (nextStep.stage === "verification" && extractionEvent) {
      chart.appendChild(renderVerificationProgress(nextStep, extractionEvent, routingEvents,
        annotationsByStep["verification"] || []));
    } else {
      chart.appendChild(renderThinkingNode(nextStep));
    }
  }

  return chart;
}

function annotationStepFor(stage) {
  // Map any non-main pipeline event to the step it should render under.
  if (stage === "user_extraction" || stage === "user_storage"
      || stage === "extractor_substitution_warning") return "assistant_extraction";
  if (stage === "assistant_draft") return "chat_model_call";
  if (stage === "routing_decision" || stage === "routing_anomaly_detected"
      || stage === "verifier_failure" || stage === "retrieval_query_attempt"
      || stage === "code_prompt_built" || stage === "code_prompt_leakage_detected"
      || stage === "code_generated" || stage === "code_executed"
      || stage === "code_unusual_behavior" || stage === "code_comparison"
      || stage === "canonical_constants_cross_check"
      || stage === "canonical_constants_disagreement"
      || stage === "cache_scoping_decision" || stage === "cache_stability_decision"
      || stage === "cache_lookup" || stage === "cache_write") return "verification";
  if (stage === "turn_cost") return "final";
  return null;
}

function renderStepNode(step, event, annotations, ctx = {}) {
  if (step.isExtractionList) return renderExtractionNode(step, event, annotations, ctx);
  if (step.isVerificationList) return renderVerificationNode(step, event, annotations, ctx);

  const wrapper = el("div", { className: "flow-step-wrapper" });
  const node = el("div", { className: "flow-step flow-step-done", dataset: { stage: step.stage } });
  node.appendChild(buildStepHeader(step.title, ctx.duration));
  const meta = step.metaFn ? step.metaFn(event) : "";
  if (meta) node.appendChild(el("div", { className: "flow-step-meta", textContent: meta }));

  const detail = el("div", { className: "flow-step-detail" });
  if (expandedSteps.has(step.stage)) detail.classList.add("expanded");
  renderStepDetail(detail, step, event, annotations);

  node.addEventListener("click", () => {
    if (expandedSteps.has(step.stage)) {
      expandedSteps.delete(step.stage);
      detail.classList.remove("expanded");
    } else {
      expandedSteps.add(step.stage);
      detail.classList.add("expanded");
    }
  });

  wrapper.appendChild(node);
  wrapper.appendChild(detail);
  return wrapper;
}

function buildStepHeader(title, durationMs) {
  const header = el("div", { className: "flow-step-header" });
  header.appendChild(el("span", { className: "flow-step-title", textContent: title }));
  if (durationMs != null && isFinite(durationMs)) {
    header.appendChild(el("span", { className: "flow-step-duration",
      textContent: fmtDurationMs(durationMs), title: `${Math.round(durationMs)} ms` }));
  }
  return header;
}

function renderExtractionNode(step, event, annotations, ctx = {}) {
  const data = event.data || {};
  const valid = data.valid_facts || [];
  const rejected = data.rejected_facts || [];
  const wrapper = el("div", { className: "flow-step-wrapper" });
  const node = el("div", { className: "flow-step flow-step-done flow-step-extraction",
    dataset: { stage: step.stage } });
  const summary = `${valid.length} claim${valid.length === 1 ? "" : "s"}` +
    (rejected.length ? ` · ${rejected.length} rejected` : "");
  const header = buildStepHeader(`${step.title} · ${summary}`, ctx.duration);
  node.appendChild(header);

  if (valid.length === 0) {
    node.appendChild(el("div", { className: "flow-step-meta", textContent: "no claims extracted" }));
  } else {
    const list = el("ul", { className: "claim-list" });
    valid.forEach((c) => list.appendChild(renderClaimRow(c, "neutral", null)));
    node.appendChild(list);
  }

  const detail = el("div", { className: "flow-step-detail" });
  if (expandedSteps.has(step.stage)) detail.classList.add("expanded");
  renderStepDetail(detail, step, event, annotations);

  node.addEventListener("click", (e) => {
    if (e.target.closest(".claim-row")) return;
    if (expandedSteps.has(step.stage)) {
      expandedSteps.delete(step.stage);
      detail.classList.remove("expanded");
    } else {
      expandedSteps.add(step.stage);
      detail.classList.add("expanded");
    }
  });

  wrapper.appendChild(node);
  wrapper.appendChild(detail);
  return wrapper;
}

// Build one row inside a claim list. `state` is one of:
//   "pending"  — grey hollow dot (no event yet)
//   "in_flight" — blue pulsing dot (routing fired, verifying)
//   "neutral"  — grey solid dot (extraction step view)
//   { displayStatus, cached } — finalized verification result
function renderClaimRow(claim, state, decision) {
  let cls, dotState, methodText, suffix = "";
  if (state === "pending") {
    cls = "claim-pending"; dotState = "pending"; methodText = "queued";
  } else if (state === "in_flight") {
    cls = "claim-in-flight"; dotState = "pulsing"; methodText = "verifying…";
  } else if (state === "neutral") {
    cls = "claim-neutral"; dotState = "solid"; methodText = claim.pattern || "";
  } else {
    const display = (decision && decision.display_status) || "inconclusive";
    cls = `claim-${flowEdgeClass(display)}`;
    dotState = "solid";
    methodText = (decision && decision.routing_decision || {}).method || "?";
    if (decision && decision.served_from_cache) {
      cls += " claim-cached";
      suffix = "↺ ";
    }
  }
  const row = el("li", {
    className: `claim-row ${cls}`,
    title: `${claim.pattern || "?"}.${claim.predicate || "?"}` +
      (decision ? `\nstatus=${decision.verification_status}` : ""),
  });
  const dot = el("span", { className: `claim-dot claim-dot-${dotState}` });
  row.appendChild(dot);
  row.appendChild(el("span", { className: "claim-label",
    textContent: suffix + (claim.predicate || "?") }));
  row.appendChild(el("span", { className: "claim-method", textContent: methodText }));
  return row;
}

function renderVerificationNode(step, event, annotations, ctx = {}) {
  const decisions = (event.data || {}).decisions || [];
  const wrapper = el("div", { className: "flow-step-wrapper" });
  const node = el("div", { className: "flow-step flow-step-done flow-step-verification",
    dataset: { stage: step.stage } });
  const header = buildStepHeader(
    `${step.title} · ${decisions.length} claim${decisions.length === 1 ? "" : "s"}`,
    ctx.duration);
  node.appendChild(header);

  if (decisions.length === 0) {
    node.appendChild(el("div", { className: "flow-step-meta", textContent: "no claims to verify" }));
  } else {
    const list = el("ul", { className: "claim-list" });
    decisions.forEach((d) => list.appendChild(renderClaimRow(d.claim || {}, d, d)));
    node.appendChild(list);
  }

  const detail = el("div", { className: "flow-step-detail" });
  if (expandedSteps.has(step.stage)) detail.classList.add("expanded");
  renderStepDetail(detail, step, event, annotations);

  node.addEventListener("click", (e) => {
    if (e.target.closest(".claim-row")) return;
    if (expandedSteps.has(step.stage)) {
      expandedSteps.delete(step.stage);
      detail.classList.remove("expanded");
    } else {
      expandedSteps.add(step.stage);
      detail.classList.add("expanded");
    }
  });

  wrapper.appendChild(node);
  wrapper.appendChild(detail);
  return wrapper;
}

// Verification step rendered while the step is in-flight: pre-populate
// claim placeholders from the extraction event, mark each as
// "in flight" once its routing_decision lands. This gives the
// "claims being verified in parallel, results coming in" experience
// even though the backend processes them sequentially.
function renderVerificationProgress(step, extractionEvent, routingEvents, annotations) {
  const wrapper = el("div", { className: "flow-step-wrapper" });
  const node = el("div", {
    className: "flow-step flow-step-thinking flow-step-verification",
    dataset: { stage: step.stage },
  });
  const valid = (extractionEvent.data || {}).valid_facts || [];
  const routed = new Set(routingEvents.map((e) => claimKey((e.data || {}).claim || {})));

  const header = el("div", { className: "flow-step-header" });
  header.appendChild(el("span", { className: "flow-step-title", textContent: step.title }));
  header.appendChild(el("span", { className: "flow-step-duration thinking-dots" }, [
    document.createTextNode("verifying"),
    el("span", { className: "thinking-anim" }),
  ]));
  node.appendChild(header);

  if (valid.length === 0) {
    node.appendChild(el("div", { className: "flow-step-meta", textContent: "no claims to verify" }));
  } else {
    const list = el("ul", { className: "claim-list" });
    valid.forEach((c) => {
      const state = routed.has(claimKey(c)) ? "in_flight" : "pending";
      list.appendChild(renderClaimRow(c, state, null));
    });
    node.appendChild(list);
  }
  wrapper.appendChild(node);
  return wrapper;
}

function renderThinkingNode(step) {
  const node = el("div", { className: "flow-step flow-step-thinking", dataset: { stage: step.stage } });
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
  // Step-specific primary detail.
  if (step.stage === "chat_model_call") renderChatModelCallDetail(container, data);
  else if (step.stage === "assistant_extraction") renderExtractionDetail(container, data);
  else if (step.stage === "verification") renderVerificationDetail(container, data);
  else if (step.stage === "correction") renderCorrectionDetail(container, data);
  else if (step.stage === "final") renderFinalDetail(container, data);

  // Annotations below.
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

function renderExtractionDetail(container, data) {
  const valid = data.valid_facts || [];
  const rejected = data.rejected_facts || [];
  if (valid.length) {
    container.appendChild(el("h4", { textContent: `${valid.length} valid claim(s)` }));
    valid.forEach((f) => container.appendChild(renderClaimBlock(f)));
  }
  if (rejected.length) {
    container.appendChild(el("h4", { textContent: `${rejected.length} rejected` }));
    rejected.forEach((r) => {
      container.appendChild(el("div", { className: "rejected-claim",
        textContent: `${r.reason}: ${JSON.stringify(r.fact)}` }));
    });
  }
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

function renderVerificationDetail(container, data) {
  const decisions = data.decisions || [];
  if (decisions.length === 0) {
    container.appendChild(el("p", { className: "hint", textContent: "no decisions" }));
    return;
  }
  decisions.forEach((d) => {
    const node = el("div", { className: "decision" });
    if (d.served_from_cache) node.classList.add("decision-cached");
    const header = el("div", { className: "decision-header" }, [
      el("span", { className: `outcome outcome-${d.outcome}`, textContent: d.outcome }),
      el("span", { className: `display-status display-${d.display_status || "inconclusive"}`,
        textContent: d.display_status || d.verification_status }),
    ]);
    if (d.served_from_cache) {
      header.appendChild(el("span", { className: "cache-badge cache-badge-hit",
        title: "served from Tier 2 cache", textContent: "↺ CACHED" }));
    }
    header.appendChild(renderClaimBlock(d.claim));
    node.appendChild(header);

    const meta = [];
    if (typeof d.confidence === "number") meta.push(`conf=${d.confidence.toFixed(2)}`);
    if (d.stored_fact_id != null) meta.push(`stored fact id=${d.stored_fact_id}`);
    if (d.boosted_fact_id != null) meta.push(`boosted fact id=${d.boosted_fact_id}`);
    if (meta.length) node.appendChild(el("div", { className: "decision-meta", textContent: meta.join(" · ") }));

    if (d.routing_decision) node.appendChild(renderRoutingBlock(d.routing_decision));
    if (d.code_gen_result) node.appendChild(renderCodeGenBlock(d.code_gen_result));
    if (d.retrieval_result) node.appendChild(renderRetrievalBlock(d.retrieval_result));
    if (d.correction) {
      node.appendChild(el("div", { className: "correction-block",
        textContent: `correction: ${JSON.stringify(d.correction.original_object)} → ${JSON.stringify(d.correction.corrected_object)}` }));
    }
    (d.notes || []).forEach((n) => {
      node.appendChild(el("div", { className: "verifier-explanation", textContent: n }));
    });
    container.appendChild(node);
  });
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
