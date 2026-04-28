// Aedos UI — one file of vanilla JS that talks to the FastAPI backend.
//
// Conventions:
//   * No build step, no framework. Every DOM construction uses document.createElement
//     (never innerHTML on user-controlled text) so model output can't inject HTML.
//   * Every backend response is rendered verbatim — the point of this tool is to see
//     exactly what the pipeline produced, not a polished view.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ---- tabs --------------------------------------------------

$$(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "facts") refreshFacts();
    if (btn.dataset.tab === "predicates") refreshPredicates();
    if (btn.dataset.tab === "flow") refreshFlow();
    if (btn.dataset.tab === "cache") refreshCache();
  });
});

// ---- helpers -----------------------------------------------

function el(tag, opts = {}, children = []) {
  const n = document.createElement(tag);
  if (opts.className) n.className = opts.className;
  if (opts.title) n.title = opts.title;
  if (opts.textContent !== undefined) n.textContent = opts.textContent;
  if (opts.dataset) for (const k in opts.dataset) n.dataset[k] = opts.dataset[k];
  children.forEach((c) => n.appendChild(c));
  return n;
}

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let detail;
    try {
      const j = await resp.json();
      // FastAPI puts the detail at j.detail. Sometimes it's a string,
      // sometimes a dict (our /api/chat 502 returns a dict).
      if (j && typeof j.detail === "object") {
        const d = j.detail;
        detail = `${d.error_type || "Error"}: ${d.error_message || ""}`;
        if (d.hint) detail += ` — ${d.hint}`;
      } else {
        detail = j?.detail || JSON.stringify(j);
      }
    } catch {
      detail = await resp.text();
    }
    throw new Error(`${method} ${path} failed (${resp.status}): ${detail}`);
  }
  return resp.json();
}

function triplify(claim) {
  // v0.3: claims have {pattern, predicate, slots, polarity, source_text}.
  // Render: pattern badge, predicate label, slot key-value table.
  const pol = claim.polarity === 1
    ? el("span", { className: "pol-pos", textContent: "+" })
    : el("span", { className: "pol-neg", textContent: "−" });
  const wrap = el("div", { className: "claim-block" });
  const header = el("div", { className: "claim-header" }, [
    el("span", { className: "pattern-badge", textContent: claim.pattern || "?" }),
    el("span", { className: "pred", textContent: claim.predicate || "?" }),
    pol,
  ]);
  wrap.appendChild(header);

  const slots = claim.slots || {};
  if (Object.keys(slots).length) {
    const tbl = el("div", { className: "slots-table" });
    for (const [k, v] of Object.entries(slots)) {
      tbl.appendChild(el("span", { className: "slot-name", textContent: k }));
      tbl.appendChild(el("span", {
        className: "slot-value",
        textContent: typeof v === "object" ? JSON.stringify(v) : String(v),
      }));
    }
    wrap.appendChild(tbl);
  }
  if (claim.source_text) {
    wrap.appendChild(el("div", {
      className: "src",
      textContent: `"${claim.source_text}"`,
    }));
  }
  return wrap;
}

// ---- chat --------------------------------------------------

const messagesEl = $("#messages");
const traceEl = $("#trace");
const form = $("#chat-form");
const input = $("#input");

// Render everything from the server on load so a refresh doesn't lose the conversation.
async function hydrate() {
  try {
    const turns = await api("GET", "/api/turns");
    messagesEl.innerHTML = "";
    turns.forEach((t) => appendMessage(t));
    if (turns.length) {
      // Fetch trace for the most recent assistant turn we saw.
      const lastAsst = [...turns].reverse().find((t) => t.role === "assistant");
      if (lastAsst) renderTrace(await api("GET", `/api/trace/${lastAsst.id}`));
    }
  } catch (e) {
    console.error(e);
  }
}
hydrate();

function appendMessage(turn) {
  const node = el("div", { className: `msg ${turn.role}`, textContent: turn.content });
  if (turn.original_content && turn.original_content !== turn.content) {
    node.appendChild(
      el("div", { className: "original", textContent: turn.original_content }),
    );
    node.appendChild(
      el("div", { className: "corrected-note", textContent: "↑ corrected by pipeline" }),
    );
  }
  messagesEl.appendChild(node);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  const sendBtn = form.querySelector("button");
  sendBtn.disabled = true;
  input.value = "";

  appendMessage({ role: "user", content: text });
  const thinking = el("div", { className: "msg assistant", textContent: "…" });
  messagesEl.appendChild(thinking);
  messagesEl.scrollTop = messagesEl.scrollHeight;

  try {
    const trace = await api("POST", "/api/chat", { message: text });
    messagesEl.removeChild(thinking);
    appendMessage({
      role: "assistant",
      content: trace.final_content,
      original_content: trace.original_content,
    });
    const events = await api("GET", `/api/trace/${trace.assistant_turn_id}`);
    renderTrace(events);
  } catch (err) {
    thinking.textContent = `⚠ ${err.message}`;
    thinking.style.color = "var(--bad)";
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
});

// ---- trace renderer ---------------------------------------

function renderTrace(events) {
  traceEl.innerHTML = "";
  if (!events || !events.length) {
    traceEl.appendChild(el("p", { className: "hint", textContent: "No trace for this turn." }));
    return;
  }
  // Promote loud events to banners at the top of the trace.
  events
    .filter((e) => e.stage === "routing_anomaly_detected")
    .forEach((ev) => traceEl.appendChild(renderAnomalyBanner(ev)));
  events
    .filter((e) => e.stage === "verifier_failure")
    .forEach((ev) => traceEl.appendChild(renderVerifierFailureBanner(ev)));
  events
    .filter((e) => e.stage === "extractor_substitution_warning")
    .forEach((ev) => traceEl.appendChild(renderSubstitutionWarning(ev)));
  // Bury per-attempt retrieval logs and code-gen sub-stages inside their
  // decisions, not as standalone stages.
  const buriedStages = new Set([
    "routing_anomaly_detected",
    "verifier_failure",
    "extractor_substitution_warning",
    "retrieval_query_attempt",
    // v0.4 / v0.5 code-gen sub-stages — surfaced inside the verification decision.
    "code_triage",  // legacy; v0.5 doesn't emit but old DBs may have it
    "code_prompt_built",
    "code_prompt_leakage_detected",
    "code_generated",
    "code_executed",
    "code_unusual_behavior",
    "code_comparison",
    // v0.5 routing + cross-check stages — surfaced inline on the decision.
    "routing_decision",
    "canonical_constants_cross_check",
    "canonical_constants_disagreement",
  ]);
  events
    .filter((e) => !buriedStages.has(e.stage))
    .forEach((ev) => traceEl.appendChild(renderStage(ev)));
}

function renderAnomalyBanner(event) {
  const d = event.data || {};
  const claim = d.claim || {};
  const slot = d.anomaly_slot || {};
  const banner = el("div", { className: "anomaly-banner" });
  banner.appendChild(el("strong", {
    textContent: "⚠ Routing anomaly detected — likely extractor error",
  }));
  const body = el("div");
  body.appendChild(document.createTextNode("Pattern "));
  body.appendChild(el("code", { textContent: claim.pattern || "?" }));
  body.appendChild(document.createTextNode(" expects slot "));
  body.appendChild(el("code", { textContent: slot.slot || "?" }));
  body.appendChild(document.createTextNode(" = "));
  body.appendChild(el("code", { textContent: String(slot.expected ?? "?") }));
  body.appendChild(document.createTextNode(" for the user-authoritative branch, but got "));
  body.appendChild(el("code", { textContent: String(slot.actual ?? "?") }));
  body.appendChild(document.createTextNode(
    ". This almost always means the extractor mis-bound the slot. "
    + "Consider whether the source phrasing should have mapped to a different pattern."
  ));
  banner.appendChild(body);
  if (d.warning) {
    banner.appendChild(el("div", {
      className: "decision-meta",
      textContent: d.warning,
    }));
  }
  return banner;
}

function renderSubstitutionWarning(event) {
  const d = event.data || {};
  const w = d.warning || {};
  const fact = d.fact || {};
  const banner = el("div", { className: "anomaly-banner",
    style: "background:#fff7e6;border-color:#c87000;color:#8a4d00;" });
  banner.appendChild(el("strong", {
    textContent: "⚠ Extractor substitution detected",
  }));
  banner.appendChild(el("div", {
    textContent: w.detail || "extractor source_text doesn't match input",
  }));
  if (fact.source_text) {
    banner.appendChild(el("div", { className: "decision-meta",
      textContent: `extractor wrote source_text: ${JSON.stringify(fact.source_text)}` }));
  }
  if (d.model_draft) {
    banner.appendChild(el("div", { className: "decision-meta",
      style: "font-size:0.7rem",
      textContent: `actual model draft (first 240): ${d.model_draft.slice(0, 240)}` }));
  }
  return banner;
}


function renderVerifierFailureBanner(event) {
  const d = event.data || {};
  const claim = d.claim || {};
  const banner = el("div", { className: "anomaly-banner", style: "background:#fff7e6;border-color:var(--warn);color:var(--warn);" });
  banner.appendChild(el("strong", {
    textContent: "⚡ Verifier failure — claim NOT hedged",
  }));
  banner.appendChild(el("div", {
    textContent: (
      "The retrieval verifier produced no useful signal for the "
      + (claim.pattern || "?") + "/" + (claim.predicate || "?") + " claim. "
      + "Hedging on verifier failure was a v0.2 bug — adding 'I think' to a "
      + "possibly-true claim is worse than leaving it. The fact is stored "
      + "as retrieval_failed; investigate the search/judge instead."
    ),
  }));
  return banner;
}

function renderStage(event) {
  const stage = el("div", { className: "stage" });
  const header = el("div", { className: "stage-header" }, [
    el("span", { textContent: event.stage }),
    el("span", { className: "turn-role", textContent: `turn ${event.turn_id} · ${event.created_at}` }),
  ]);
  stage.appendChild(header);
  const body = el("div", { className: "stage-body" });
  stage.appendChild(body);

  const d = event.data || {};
  switch (event.stage) {
    case "user_extraction":
    case "assistant_extraction":
      renderExtraction(body, d);
      break;
    case "user_storage":
    case "verification":
      renderDecisions(body, d);
      break;
    case "assistant_draft":
      body.appendChild(el("div", { className: "draft-box", textContent: d.content || "" }));
      break;
    case "cache_lookup": {
      // v0.6 — cache hit/miss event.
      const meta = el("div", { className: "decision-meta" });
      const result = d.result || (d.error ? "error" : "?");
      let line = `${result.toUpperCase()}`;
      if (d.canonical_key) {
        line += ` · key=${d.canonical_key.slice(0, 80)}`;
      }
      if (result === "hit") {
        line += ` · ${d.verdict || "?"}`;
        if (d.hit_count != null) line += ` · hits=${d.hit_count}`;
        if (d.expires_at) line += ` · expires ${d.expires_at}`;
      }
      if (d.error) {
        line += ` · error: ${d.error}`;
      }
      meta.appendChild(el("div", { textContent: line }));
      body.appendChild(meta);
      break;
    }
    case "cache_write": {
      // v0.6 — cache write event.
      const meta = el("div", { className: "decision-meta" });
      let line;
      if (d.error) {
        line = `error: ${d.error}`;
      } else {
        line = `wrote · ${d.verdict || "?"} · ${d.stability_class || "?"}`;
        if (d.ttl_seconds === null) line += " · never expires";
        else if (d.ttl_seconds != null) line += ` · ttl=${d.ttl_seconds}s`;
        if (d.canonical_key) line += ` · key=${d.canonical_key.slice(0, 80)}`;
      }
      meta.appendChild(el("div", { textContent: line }));
      body.appendChild(meta);
      break;
    }
    case "cache_scoping_decision": {
      const meta = el("div", { className: "decision-meta" });
      if (d.error) {
        meta.appendChild(el("div", {
          style: "color:var(--danger)",
          textContent: `error: ${d.error}`,
        }));
      } else {
        const dec = d.decision || {};
        meta.appendChild(el("div", {
          textContent: `scope=${dec.scope || "?"} (conf=${
            (dec.confidence ?? 0).toFixed(2)}) — ${dec.reason || ""}`,
        }));
      }
      body.appendChild(meta);
      break;
    }
    case "cache_stability_decision": {
      const meta = el("div", { className: "decision-meta" });
      if (d.error) {
        meta.appendChild(el("div", {
          style: "color:var(--danger)",
          textContent: `error: ${d.error}`,
        }));
      } else {
        const dec = d.decision || {};
        const ttl = dec.ttl_seconds === null ? "never expires"
          : dec.ttl_seconds === 0 ? "don't cache (volatile)"
          : `ttl=${dec.ttl_seconds}s`;
        meta.appendChild(el("div", {
          textContent: `${dec.stability_class || "?"} (conf=${
            (dec.confidence ?? 0).toFixed(2)}) · ${ttl} — ${dec.reason || ""}`,
        }));
      }
      body.appendChild(meta);
      break;
    }
    case "turn_cost": {
      // v0.6 — end-of-turn cost aggregate. Show total + by-model breakdown.
      const meta = el("div", { className: "decision-meta" });
      const totalUsd = (d.total_usd ?? 0).toFixed(6);
      const totalCalls = d.total_calls ?? 0;
      const totalIn = d.total_input_tokens ?? 0;
      const totalOut = d.total_output_tokens ?? 0;
      meta.appendChild(el("div", {
        textContent: `$${totalUsd} · ${totalCalls} calls · `
          + `${totalIn} in / ${totalOut} out tokens`
          + (d.any_unknown_pricing ? " (some unknown pricing)" : ""),
      }));
      const byModel = d.by_model || {};
      Object.keys(byModel).sort().forEach((m) => {
        const slot = byModel[m];
        meta.appendChild(el("div", {
          className: "mono",
          style: "font-size:0.75rem;padding-left:0.8rem",
          textContent: `${m}: ${slot.calls} calls, $${(slot.total_usd ?? 0).toFixed(6)}`
            + ` (${slot.input_tokens || 0} in / ${slot.output_tokens || 0} out)`,
        }));
      });
      body.appendChild(meta);
      break;
    }
    case "chat_model_call": {
      // v0.5.x: per-turn provenance row for the chat model under test.
      // Whether the chat model was Claude or GLM, we get one of these.
      const meta = el("div", { className: "decision-meta" });
      const provider = d.provider || "?";
      const model = d.model || "?";
      const dur = d.duration_ms != null ? `${(d.duration_ms / 1000).toFixed(2)}s` : "?";
      const status = d.status_code != null ? ` http=${d.status_code}` : "";
      const sysChars = d.system_chars != null ? `, system=${d.system_chars}c` : "";
      const respChars = d.response_chars != null ? `, response=${d.response_chars}c` : "";
      meta.appendChild(el("div", {
        textContent: `${provider}:${model} — ${dur}${status} (msgs=${d.message_count ?? "?"}${sysChars}${respChars})`,
      }));
      if (d.error) {
        meta.appendChild(el("div", {
          style: "color:var(--danger);font-weight:600",
          textContent: `error: ${d.error}`,
        }));
      }
      body.appendChild(meta);
      break;
    }
    case "correction": {
      const diff = el("div", { className: "diff-view" });
      const left = el("div");
      left.appendChild(el("h4", { textContent: "Original" }));
      left.appendChild(el("div", {
        className: "diff-pane diff-original",
        textContent: d.original || "",
      }));
      const right = el("div");
      right.appendChild(el("h4", { textContent: "Corrected" }));
      right.appendChild(el("div", {
        className: "diff-pane diff-corrected",
        textContent: d.corrected || "",
      }));
      diff.appendChild(left);
      diff.appendChild(right);
      const interventions = d.interventions || [];
      if (interventions.length) {
        const list = el("div", { className: "intervention-list" });
        list.appendChild(el("strong", { textContent: "Interventions:" }));
        const ul = el("ul");
        interventions.forEach((iv) => {
          const li = el("li");
          li.appendChild(el("span", {
            className: `intervention-type intervention-type-${iv.intervention_type}`,
            textContent: iv.intervention_type,
          }));
          const claim = iv.claim || {};
          li.appendChild(document.createTextNode(
            `${claim.subject || "?"} · ${claim.predicate || "?"} · ${claim.object || "?"} — ${iv.reason || ""}`,
          ));
          ul.appendChild(li);
        });
        list.appendChild(ul);
        diff.appendChild(list);
      }
      body.appendChild(diff);
      break;
    }
    case "final":
      body.appendChild(el("div", { className: "draft-box", textContent: d.content || "" }));
      break;
    default:
      body.appendChild(el("pre", { textContent: JSON.stringify(d, null, 2) }));
  }
  return stage;
}

function renderExtraction(body, data) {
  if (data.valid_facts && data.valid_facts.length) {
    body.appendChild(el("div", { textContent: "Valid claims:" }));
    data.valid_facts.forEach((c) => body.appendChild(triplify(c)));
  } else {
    body.appendChild(el("div", { className: "hint", textContent: "(no valid claims extracted)" }));
  }
  if (data.rejected_facts && data.rejected_facts.length) {
    const rej = el("div", { className: "rejected-claims" });
    rej.appendChild(el("strong", { textContent: "Rejected:" }));
    data.rejected_facts.forEach((r) => {
      rej.appendChild(
        el("div", { textContent: `• ${r.reason} — ${JSON.stringify(r.claim)}` }),
      );
    });
    body.appendChild(rej);
  }
}

function statusBadge(status) {
  if (!status) return el("span");
  return el("span", {
    className: `status-badge status-${status}`,
    textContent: status,
    title: `verification_status = ${status}`,
  });
}

function renderRoutingDecision(rd) {
  // rd: {method, reason, confidence, python_inputs_self_contained,
  //      retrieval_query_hint, canonical_constants_needed}
  const block = el("div", { className: "routing-block" });
  const header = el("div", { className: "routing-header" });
  header.appendChild(el("span", {
    className: `routing-method routing-method-${rd.method}`,
    textContent: `route → ${rd.method}`,
  }));
  if (typeof rd.confidence === "number") {
    const conf = rd.confidence;
    const lowConf = conf < 0.7;
    header.appendChild(el("span", {
      className: lowConf ? "routing-conf-low" : "routing-conf",
      textContent: `conf=${conf.toFixed(2)}`,
      title: lowConf ? "low-confidence routing decision (< 0.7)" : "",
    }));
    if (lowConf) {
      header.appendChild(el("span", {
        className: "routing-warning",
        textContent: "⚠ low confidence",
      }));
    }
  }
  block.appendChild(header);
  if (rd.reason) {
    block.appendChild(el("div", {
      className: "routing-reason",
      textContent: rd.reason,
    }));
  }
  const meta = [];
  if (rd.python_inputs_self_contained === true) meta.push("inputs self-contained");
  if (rd.python_inputs_self_contained === false) meta.push("inputs require external data");
  if (rd.retrieval_query_hint) meta.push(`query hint: ${rd.retrieval_query_hint}`);
  if (rd.canonical_constants_needed && rd.canonical_constants_needed.length) {
    meta.push(`canonical: ${rd.canonical_constants_needed.join(", ")}`);
  }
  if (meta.length) {
    block.appendChild(el("div", {
      className: "routing-meta",
      textContent: meta.join(" · "),
    }));
  }
  return block;
}


function renderCrossCheckBlock(cc) {
  // cc.a, cc.b each have {status, actual_value, code, execution, explanation}.
  const block = el("div", { className: "crosscheck-block" });
  const header = el("div", { className: "crosscheck-header" });
  const agree = (
    cc.a && cc.b
    && cc.a.status === cc.b.status
    && cc.a.actual_value === cc.b.actual_value
  );
  header.appendChild(el("strong", {
    textContent: agree
      ? "canonical-constants cross-check: AGREE"
      : "⚠ canonical-constants cross-check: DISAGREE",
  }));
  block.appendChild(header);

  const row = el("div", { className: "crosscheck-row" });
  ["a", "b"].forEach((k) => {
    const side = cc[k] || {};
    const col = el("div", { className: "crosscheck-col" });
    col.appendChild(el("h5", {
      textContent: `gen ${k.toUpperCase()} — ${side.status || "?"}`,
    }));
    if (side.code && side.code.code) {
      const pre = el("pre", { className: "codegen-code" });
      pre.textContent = side.code.code;
      col.appendChild(pre);
    }
    if (side.actual_value !== undefined) {
      col.appendChild(el("div", {
        className: "codegen-meta",
        textContent: `computed = ${JSON.stringify(side.actual_value)}`,
      }));
    }
    row.appendChild(col);
  });
  block.appendChild(row);
  return block;
}


function renderCodeGenBlock(result) {
  // result has: status, confidence, explanation, actual_value, trace
  // trace has: triage, prompt (with attempts), code, execution, comparison
  const block = el("div", { className: "codegen-block" });

  // Header — verdict + computed/claimed values, always visible.
  const header = el("div", { className: "codegen-header" });
  header.appendChild(el("span", {
    className: `codegen-status codegen-status-${result.status}`,
    textContent: `code-gen: ${result.status}`,
  }));
  if (result.explanation) {
    header.appendChild(el("span", {
      className: "codegen-explanation",
      textContent: result.explanation,
    }));
  }
  block.appendChild(header);

  const trace = result.trace || {};

  // Warnings — prompt leakage, slow run, stderr.
  const warnings = [];
  const promptInfo = trace.prompt || {};
  const attempts = promptInfo.attempts || [];
  const leakAttempts = attempts.filter((a) => a.leak_detected);
  if (leakAttempts.length > 0) {
    warnings.push(
      promptInfo.compromised
        ? "⚠ leak detected on every attempt — verification compromised"
        : `⚠ leak detected on ${leakAttempts.length} attempt(s); retried successfully`,
    );
  }
  const exec = trace.execution || {};
  if (exec.slow) warnings.push(`⏱ slow run (${exec.duration_ms} ms)`);
  if (exec.stderr) warnings.push("⚠ stderr output (see below)");
  if (exec.timed_out) warnings.push("⛔ sandbox timed out");
  warnings.forEach((w) => {
    block.appendChild(el("div", { className: "codegen-warning", textContent: w }));
  });

  // Expandable details.
  const details = el("details", { className: "codegen-details" });
  const summary = el("summary", {
    textContent: "show pipeline (prompt → code → execution → comparison)",
  });
  details.appendChild(summary);

  // Triage (legacy v0.4 traces only — v0.5 doesn't generate this).
  if (trace.triage) {
    const sec = el("div", { className: "codegen-section" });
    sec.appendChild(el("h4", { textContent: "0. Triage (v0.4 legacy)" }));
    sec.appendChild(el("div", {
      textContent: `verifiable: ${trace.triage.verifiable}`,
    }));
    if (trace.triage.reason) {
      sec.appendChild(el("div", {
        className: "codegen-reason",
        textContent: trace.triage.reason,
      }));
    }
    details.appendChild(sec);
  }

  // Prompt + attempts
  if (trace.prompt) {
    const sec = el("div", { className: "codegen-section" });
    sec.appendChild(el("h4", { textContent: "1. Neutral prompt" }));
    sec.appendChild(el("div", {
      className: "codegen-readonly",
      textContent: trace.prompt.prompt || "",
    }));
    sec.appendChild(el("div", {
      className: "codegen-meta",
      textContent: `expected_output_type: ${trace.prompt.expected_output_type}`,
    }));
    if (attempts.length > 1) {
      const attempts_block = el("div", { className: "codegen-attempts" });
      attempts_block.appendChild(el("strong", { textContent: "attempts:" }));
      attempts.forEach((a, i) => {
        const row = el("div", { className: "codegen-attempt" });
        row.appendChild(el("span", {
          className: a.leak_detected ? "codegen-attempt-leak" : "codegen-attempt-ok",
          textContent: a.leak_detected ? `[${i + 1}] LEAK` : `[${i + 1}] ok`,
        }));
        row.appendChild(document.createTextNode(" "));
        row.appendChild(el("code", { textContent: a.prompt }));
        attempts_block.appendChild(row);
      });
      sec.appendChild(attempts_block);
    }
    details.appendChild(sec);
  }

  // Code
  if (trace.code) {
    const sec = el("div", { className: "codegen-section" });
    sec.appendChild(el("h4", { textContent: "2. Generated code" }));
    sec.appendChild(el("div", {
      className: "codegen-meta",
      textContent: `model: ${trace.code.model || ""}`,
    }));
    const pre = el("pre", { className: "codegen-code" });
    pre.textContent = trace.code.code || "";
    sec.appendChild(pre);
    details.appendChild(sec);
  }

  // Execution
  if (trace.execution) {
    const sec = el("div", { className: "codegen-section" });
    sec.appendChild(el("h4", { textContent: "3. Execution" }));
    sec.appendChild(el("div", {
      className: "codegen-meta",
      textContent: (
        `success=${trace.execution.success} · `
        + `exit_code=${trace.execution.exit_code} · `
        + `duration_ms=${trace.execution.duration_ms} · `
        + `timed_out=${trace.execution.timed_out}`
      ),
    }));
    if (trace.execution.stdout) {
      sec.appendChild(el("h5", { textContent: "stdout" }));
      const pre = el("pre", { className: "codegen-stdout" });
      pre.textContent = trace.execution.stdout;
      sec.appendChild(pre);
    }
    if (trace.execution.stderr) {
      sec.appendChild(el("h5", { textContent: "stderr" }));
      const pre = el("pre", { className: "codegen-stderr" });
      pre.textContent = trace.execution.stderr;
      sec.appendChild(pre);
    }
    details.appendChild(sec);
  }

  // Comparison
  if (trace.comparison) {
    const sec = el("div", { className: "codegen-section" });
    sec.appendChild(el("h4", { textContent: "4. Comparison" }));
    sec.appendChild(el("div", {
      textContent: `verdict: ${trace.comparison.verdict}`,
    }));
    sec.appendChild(el("div", {
      className: "codegen-meta",
      textContent: (
        `claimed=${JSON.stringify(trace.comparison.claimed_value)} · `
        + `computed=${JSON.stringify(trace.comparison.computed_value)}`
      ),
    }));
    if (trace.comparison.explanation) {
      sec.appendChild(el("div", {
        className: "codegen-reason",
        textContent: trace.comparison.explanation,
      }));
    }
    details.appendChild(sec);
  }

  block.appendChild(details);
  return block;
}

function renderRetrievalBlock(rr) {
  const node = el("div", { className: "retrieval-block" });

  // Verdict + temporal mode (current vs historical).
  if (rr.verdict) {
    const v = rr.verdict;
    const verdictEl = el("div", { className: `verdict verdict-${v.verdict}` });
    verdictEl.textContent = `judge (${rr.historical ? "historical" : "current"}): ${v.verdict}`;
    node.appendChild(verdictEl);
    if (v.justification) {
      node.appendChild(el("div", {
        className: "verifier-explanation",
        textContent: v.justification,
      }));
    }
  }
  if (rr.error_flag) {
    node.appendChild(el("span", { className: "error-flag", textContent: rr.error_flag }));
  }

  // Multi-attempt query strategy table — Section 5 surface.
  const attempts = rr.attempts || [];
  if (attempts.length) {
    const wrap = el("div", { className: "query-attempts" });
    wrap.appendChild(el("div", { className: "decision-meta", textContent: "query attempts:" }));
    const tbl = el("table");
    tbl.appendChild(el("tr", {}, ["#", "query", "results", "cache?", "used?", "error"]
      .map((h) => el("th", { textContent: h }))));
    attempts.forEach((a, i) => {
      const tr = el("tr");
      if (a.used) tr.classList.add("attempt-used");
      if (a.error) tr.classList.add("attempt-error");
      [
        String(i + 1),
        a.query,
        String(a.result_count),
        a.from_cache ? "✓" : "",
        a.used ? "✓" : "",
        a.error || "",
      ].forEach((v, j) => {
        const td = el("td", { textContent: v });
        if (j === 1) td.classList.add("att-q");
        tr.appendChild(td);
      });
      tbl.appendChild(tr);
    });
    wrap.appendChild(tbl);
    node.appendChild(wrap);
  }

  (rr.snippets || []).forEach((s) => {
    const sn = el("div", { className: "snippet" });
    if (s.title) sn.appendChild(el("div", { className: "snippet-title", textContent: s.title }));
    if (s.snippet) sn.appendChild(el("div", { textContent: s.snippet }));
    if (s.url) sn.appendChild(el("div", { className: "snippet-url", textContent: s.url }));
    node.appendChild(sn);
  });
  return node;
}

function renderDecisions(body, data) {
  const decisions = data.decisions || [];
  if (!decisions.length) {
    body.appendChild(el("div", { className: "hint", textContent: "(no decisions — nothing routed)" }));
    return;
  }
  decisions.forEach((d) => {
    const node = el("div", { className: "decision" });
    const header = el("div", { className: "decision-header" }, [
      el("span", { className: `outcome outcome-${d.outcome}`, textContent: d.outcome }),
      statusBadge(d.verification_status),
      triplify(d.claim),
    ]);
    node.appendChild(header);
    const meta = [];
    if (typeof d.confidence === "number") meta.push(`conf=${d.confidence.toFixed(2)}`);
    if (d.stored_fact_id != null) meta.push(`stored fact id=${d.stored_fact_id}`);
    if (d.boosted_fact_id != null) meta.push(`boosted fact id=${d.boosted_fact_id}`);
    if (d.closed_fact_ids && d.closed_fact_ids.length) meta.push(`closed=${d.closed_fact_ids.join(",")}`);
    if (d.contradicting_fact_id != null) meta.push(`contradicted id=${d.contradicting_fact_id}`);
    if (d.matching_fact_id != null) meta.push(`matching id=${d.matching_fact_id}`);
    if (meta.length) node.appendChild(el("div", { className: "decision-meta", textContent: meta.join(" · ") }));

    // v0.5: routing decision leads the verification block.
    if (d.routing_decision) {
      node.appendChild(renderRoutingDecision(d.routing_decision));
    }

    if (d.code_gen_result) {
      node.appendChild(renderCodeGenBlock(d.code_gen_result));
      // v0.5: canonical-constants cross-check, when present.
      const cc = (d.code_gen_result.trace || {}).cross_check;
      if (cc) node.appendChild(renderCrossCheckBlock(cc));
    }
    if (d.retrieval_result) {
      node.appendChild(renderRetrievalBlock(d.retrieval_result));
    }
    if (d.correction) {
      node.appendChild(
        el("div", {
          className: "correction-block",
          textContent: `correction: ${d.correction.original_object} → ${d.correction.corrected_object}`,
        }),
      );
    }
    (d.notes || []).forEach((n) => {
      node.appendChild(el("div", { className: "verifier-explanation", textContent: n }));
    });
    body.appendChild(node);
  });
}

// ---- facts inspector --------------------------------------

async function refreshFacts() {
  const p = new URLSearchParams();
  const pat = $("#f-pattern").value;
  const pred = $("#f-predicate").value.trim();
  const ab = $("#f-asserted-by").value;
  const st = $("#f-status").value;
  const onlyValid = $("#f-only-valid").checked;
  if (pat) p.set("pattern", pat);
  if (pred) p.set("predicate", pred);
  if (ab) p.set("asserted_by", ab);
  if (st) p.set("verification_status", st);
  if (onlyValid) p.set("only_valid", "true");

  const facts = await api("GET", "/api/facts?" + p.toString());
  const container = $("#facts-table");
  container.innerHTML = "";
  if (!facts.length) {
    container.appendChild(el("p", { className: "hint", textContent: "(no facts match these filters)" }));
    return;
  }
  const table = el("table");
  const head = el("tr", {}, [
    "id", "pattern", "predicate", "slots", "pol", "confidence", "asserted_by", "status", "valid_until", "turn",
  ].map((h) => el("th", { textContent: h })));
  table.appendChild(head);
  facts.forEach((f) => {
    const tr = el("tr");
    if (f.valid_until) tr.classList.add("closed");
    if (f.verification_status === "contradicted") tr.classList.add("contradicted");
    if (f.verification_status === "verified") tr.classList.add("verified");

    const slotsCell = el("td", { className: "mono" });
    const entries = Object.entries(f.slots || {});
    slotsCell.textContent = entries.length
      ? entries.map(([k, v]) =>
          `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ")
      : "—";

    const confCell = el("td");
    if (typeof f.confidence === "number") {
      const bar = el("span", { className: "confidence-bar" });
      const overlay = el("span");
      overlay.style.width = `${Math.max(0, (1 - f.confidence) * 100)}%`;
      bar.appendChild(overlay);
      confCell.appendChild(bar);
      confCell.appendChild(el("span", {
        className: "confidence-text",
        textContent: f.confidence.toFixed(2),
      }));
    } else {
      confCell.textContent = String(f.confidence);
    }
    const statusCell = el("td");
    statusCell.appendChild(statusBadge(f.verification_status));

    [
      el("td", { textContent: String(f.id) }),
      el("td", {}, [el("span", { className: "pattern-badge", textContent: f.pattern })]),
      el("td", { textContent: String(f.predicate) }),
      slotsCell,
      el("td", { textContent: String(f.polarity) }),
      confCell,
      el("td", { textContent: String(f.asserted_by) }),
      statusCell,
      el("td", { textContent: f.valid_until || "—" }),
      el("td", { textContent: String(f.source_turn_id ?? "") }),
    ].forEach((c) => tr.appendChild(c));
    table.appendChild(tr);
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

// ---- predicates inspector ---------------------------------

async function refreshPredicates() {
  // The "Patterns" tab in v0.3 — kept the function name so the tab handler
  // doesn't need to change. Renders pattern metadata, not the old predicate
  // table.
  const patterns = await api("GET", "/api/patterns");
  const container = $("#predicates-table");
  container.innerHTML = "";
  patterns.forEach((p) => {
    const card = el("div", { className: "stage" });
    const header = el("div", { className: "stage-header" }, [
      el("span", { className: "pattern-badge", textContent: p.name }),
    ]);
    card.appendChild(header);
    const body = el("div", { className: "stage-body" });
    body.appendChild(el("div", { textContent: p.description }));

    // Slots table.
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

    // (v0.5) Per-pattern verification rules and the user-anomaly flag are
    // gone — routing is decided per-claim by the LLM router.

    if ((p.example_predicates || []).length) {
      body.appendChild(el("h4", { textContent: "Example predicates (free-form, not exhaustive)" }));
      body.appendChild(el("div", { className: "mono",
        textContent: p.example_predicates.join(", ") }));
    }
    if ((p.query_strategy || []).length) {
      body.appendChild(el("h4", { textContent: "Query strategy (retrieval)" }));
      const ol = el("ol");
      p.query_strategy.forEach((q) => ol.appendChild(el("li", { className: "mono", textContent: q })));
      body.appendChild(ol);
    }
    if (p.disambiguation_notes) {
      body.appendChild(el("h4", { textContent: "Disambiguation" }));
      body.appendChild(el("div", { className: "draft-box", textContent: p.disambiguation_notes }));
    }
    card.appendChild(body);
    container.appendChild(card);
  });
}

// ---- flow view --------------------------------------------
//
// At-a-glance per-turn flowchart. Vertical SVG. Linear stages stack;
// the router branches into one column per assistant claim and the
// corrector merges them back. Each node is clickable — clicking flips
// to the Chat + Trace tab and scrolls the corresponding stage into view.
//
// Source of truth is the same pipeline_events stream the Detail View
// uses; this renderer just lays it out structurally.

const SVG_NS = "http://www.w3.org/2000/svg";
const FLOW_NODE_W = 340;
const FLOW_CLAIM_W = 170;
const FLOW_NODE_H = 56;
const FLOW_GAP = 32;
const FLOW_MARGIN = 24;

function svgEl(tag, attrs = {}, children = []) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  children.forEach((c) => n.appendChild(c));
  return n;
}

function flowEdgeClass(status) {
  if (status === "verified" || status === "user_asserted") return "verified";
  if (status === "contradicted") return "contradicted";
  if (
    status === "retrieval_inconclusive" ||
    status === "retrieval_failed" ||
    status === "unverifiable_pending_implementation" ||
    status === "routing_anomaly"
  ) return "inconclusive";
  if (status === "unverifiable_in_principle") return "unverifiable";
  return "";
}

async function refreshFlow() {
  const turnSelect = $("#flow-turn-select");
  const status = $("#flow-status");
  const container = $("#flow-container");
  status.textContent = "loading…";

  const turns = await api("GET", "/api/turns");
  const assistantTurns = turns.filter((t) => t.role === "assistant");
  // Refresh the dropdown.
  const prev = turnSelect.value;
  turnSelect.innerHTML = "";
  assistantTurns.forEach((t) => {
    const opt = el("option", {});
    opt.value = String(t.id);
    opt.textContent = `turn ${t.id} — ${(t.content || "").slice(0, 60)}`;
    turnSelect.appendChild(opt);
  });
  if (!assistantTurns.length) {
    container.innerHTML = "";
    container.appendChild(el("p", { className: "hint",
      textContent: "No assistant turns yet. Send a message first." }));
    status.textContent = "";
    return;
  }
  // Default: previously-selected turn if still present, else the latest.
  const desired = prev && assistantTurns.find((t) => String(t.id) === prev)
    ? prev : String(assistantTurns[assistantTurns.length - 1].id);
  turnSelect.value = desired;
  await renderFlowFor(parseInt(desired, 10));
  status.textContent = "";
}

async function renderFlowFor(turnId) {
  const container = $("#flow-container");
  container.innerHTML = "";
  const events = await api("GET", `/api/trace/${turnId}`);
  if (!events || !events.length) {
    container.appendChild(el("p", { className: "hint",
      textContent: "No events for this turn." }));
    return;
  }
  const svg = buildFlowSvg(events, turnId);
  container.appendChild(svg);
}

function buildFlowSvg(events, turnId) {
  // The pipeline emits events for both the user turn and the assistant
  // turn, but the trace endpoint returns only events for the assistant
  // turn. Linear stages we expect to see, in order:
  //
  //   chat_model_call → assistant_extraction → verification → correction → final
  //
  // The user message is fetched separately so we can show it as the entry.
  // For simplicity we read the user side from list_turns when needed.
  const stageMap = {};
  events.forEach((e) => { stageMap[e.stage] = e; });

  const chatEv = stageMap["chat_model_call"];
  const extractEv = stageMap["assistant_extraction"];
  const verificationEv = stageMap["verification"];
  const correctionEv = stageMap["correction"];
  const finalEv = stageMap["final"];

  const claims = (verificationEv?.data?.decisions) || [];
  const totalClaims = claims.length;

  // Linear stages above the router.
  const linearTop = [
    {
      stage: "assistant_draft",
      title: "User → Chat Model",
      meta: chatEv ? formatChatMeta(chatEv.data) : "(legacy turn — no chat_model_call event)",
      jumpStage: "chat_model_call",
    },
    {
      stage: "assistant_extraction",
      title: "Assistant Extraction",
      meta: extractEv
        ? (() => {
            const valid = (extractEv.data?.valid_facts || []).length;
            const rejected = (extractEv.data?.rejected_facts || []).length;
            const warnings = events.filter(
              (e) => e.stage === "extractor_substitution_warning"
                     && e.data?.side !== "user"
            ).length;
            let m = `${valid} valid, ${rejected} rejected`;
            if (warnings > 0) m += ` · ⚠ ${warnings} substitution warning(s)`;
            return m;
          })()
        : "(no extraction event)",
      jumpStage: "assistant_extraction",
    },
    {
      stage: "router",
      title: totalClaims === 0
        ? "Router (no claims to verify)"
        : `Router (${totalClaims} claim${totalClaims === 1 ? "" : "s"})`,
      meta: "LLM router decided per-claim verification method",
      jumpStage: "verification",
    },
  ];

  const linearBottom = [
    {
      stage: "corrector",
      title: correctionEv
        ? `Corrector (${(correctionEv.data?.interventions || []).length} interventions)`
        : "Corrector (no interventions)",
      meta: correctionEv
        ? "draft was rewritten — green = applied"
        : "draft passed through unchanged",
      jumpStage: correctionEv ? "correction" : null,
    },
    {
      stage: "final",
      title: "Final Response",
      meta: ((finalEv?.data?.content || "").slice(0, 80)) || "(no final event)",
      jumpStage: "final",
    },
  ];

  // Layout calculation.
  const branchW = Math.max(
    FLOW_NODE_W,
    totalClaims * FLOW_CLAIM_W + (totalClaims - 1) * 24,
  );
  const totalW = Math.max(FLOW_NODE_W, branchW) + FLOW_MARGIN * 2;
  const linearTopH = linearTop.length * (FLOW_NODE_H + FLOW_GAP);
  const branchH = totalClaims > 0 ? FLOW_NODE_H + FLOW_GAP : 0;
  const linearBottomH = linearBottom.length * (FLOW_NODE_H + FLOW_GAP);
  const totalH = linearTopH + branchH + linearBottomH + FLOW_MARGIN * 2;

  const svg = svgEl("svg", {
    width: String(totalW),
    height: String(totalH),
    viewBox: `0 0 ${totalW} ${totalH}`,
    role: "img",
    "aria-label": `pipeline flow for turn ${turnId}`,
  });

  let y = FLOW_MARGIN;
  const cx = totalW / 2;

  // Linear stages above the router.
  linearTop.forEach((st, i) => {
    addNode(svg, cx - FLOW_NODE_W / 2, y, FLOW_NODE_W, FLOW_NODE_H,
      st.title, st.meta, st.jumpStage);
    if (i < linearTop.length - 1) {
      addEdge(svg, cx, y + FLOW_NODE_H, cx, y + FLOW_NODE_H + FLOW_GAP, "");
    }
    y += FLOW_NODE_H + FLOW_GAP;
  });

  // Branch into claims.
  if (totalClaims > 0) {
    const claimsRowY = y;
    const totalClaimW = totalClaims * FLOW_CLAIM_W + (totalClaims - 1) * 24;
    const startX = cx - totalClaimW / 2;
    claims.forEach((d, idx) => {
      const x = startX + idx * (FLOW_CLAIM_W + 24);
      const status = d.verification_status || "?";
      const method = d.routing_decision?.method || "(no routing)";
      const claim = d.claim || {};
      const label = `${claim.predicate || "?"} (${method})`;
      const cls = flowEdgeClass(status);
      // edge from router-bottom to claim-top
      addEdge(svg,
        cx, claimsRowY - FLOW_GAP,
        x + FLOW_CLAIM_W / 2, claimsRowY,
        cls,
      );
      // claim node with status colorization
      addClaimNode(svg, x, claimsRowY, FLOW_CLAIM_W, FLOW_NODE_H,
        label, status, cls);
      // edge from claim-bottom to corrector-top
      addEdge(svg,
        x + FLOW_CLAIM_W / 2, claimsRowY + FLOW_NODE_H,
        cx, claimsRowY + FLOW_NODE_H + FLOW_GAP,
        cls,
      );
    });
    y = claimsRowY + FLOW_NODE_H + FLOW_GAP;
  } else {
    // No claims: keep the linear edge.
    addEdge(svg, cx, y - FLOW_GAP, cx, y, "");
  }

  // Linear stages below the router.
  linearBottom.forEach((st, i) => {
    addNode(svg, cx - FLOW_NODE_W / 2, y, FLOW_NODE_W, FLOW_NODE_H,
      st.title, st.meta, st.jumpStage);
    if (i < linearBottom.length - 1) {
      addEdge(svg, cx, y + FLOW_NODE_H, cx, y + FLOW_NODE_H + FLOW_GAP, "");
    }
    y += FLOW_NODE_H + FLOW_GAP;
  });

  return svg;
}

function addNode(svg, x, y, w, h, title, meta, jumpStage) {
  const g = svgEl("g", { class: "flow-node" });
  if (jumpStage) g.dataset.jumpStage = jumpStage;
  // Native browser tooltip on hover (SVG <title>). Always full text,
  // even when not truncated — useful for confirming what a node is.
  const fullTip = meta ? `${title}\n${meta}` : title;
  const tipEl = svgEl("title", {});
  tipEl.textContent = fullTip;
  g.appendChild(tipEl);
  g.appendChild(svgEl("rect", {
    x: String(x), y: String(y), width: String(w), height: String(h),
    rx: "6", ry: "6", class: "flow-node-rect",
  }));
  const titleText = svgEl("text", {
    x: String(x + w / 2), y: String(y + 22),
    "text-anchor": "middle", class: "flow-node-text",
  });
  titleText.textContent = title;
  g.appendChild(titleText);
  if (meta) {
    const metaText = svgEl("text", {
      x: String(x + w / 2), y: String(y + 40),
      "text-anchor": "middle", class: "flow-node-meta",
    });
    metaText.textContent = meta.length > 60 ? meta.slice(0, 60) + "…" : meta;
    g.appendChild(metaText);
  }
  if (jumpStage) {
    g.style.cursor = "pointer";
    g.addEventListener("click", () => jumpToStage(jumpStage));
  }
  svg.appendChild(g);
}

function addClaimNode(svg, x, y, w, h, label, status, cls) {
  const g = svgEl("g", { class: "flow-node" });
  g.dataset.jumpStage = "verification";
  // Tooltip carries the un-truncated label + status; the visible text
  // is severely clipped (22 chars) for the small claim rectangles.
  const tipEl = svgEl("title", {});
  tipEl.textContent = `${label}\nstatus: ${status}`;
  g.appendChild(tipEl);
  g.appendChild(svgEl("rect", {
    x: String(x), y: String(y), width: String(w), height: String(h),
    rx: "6", ry: "6", class: `flow-node-rect flow-claim-rect ${cls}`,
  }));
  const labelText = svgEl("text", {
    x: String(x + w / 2), y: String(y + 22),
    "text-anchor": "middle", class: "flow-node-text",
  });
  labelText.textContent = label.length > 22 ? label.slice(0, 22) + "…" : label;
  g.appendChild(labelText);
  const statusText = svgEl("text", {
    x: String(x + w / 2), y: String(y + 40),
    "text-anchor": "middle", class: "flow-node-meta",
  });
  statusText.textContent = status;
  g.appendChild(statusText);
  g.style.cursor = "pointer";
  g.addEventListener("click", () => jumpToStage("verification"));
  svg.appendChild(g);
}

function addEdge(svg, x1, y1, x2, y2, cls) {
  // Slightly curved path from (x1,y1) to (x2,y2). Vertical-dominant.
  const dy = (y2 - y1) / 2;
  const path = `M ${x1} ${y1} C ${x1} ${y1 + dy}, ${x2} ${y2 - dy}, ${x2} ${y2}`;
  svg.appendChild(svgEl("path", {
    d: path, class: `flow-edge ${cls}`,
  }));
}

function formatChatMeta(d) {
  if (!d) return "(no chat data)";
  if (d.error) {
    return `⚠ ${d.provider || "?"}:${d.model || "?"} — ERROR: `
      + d.error.slice(0, 80);
  }
  const dur = d.duration_ms != null ? `${(d.duration_ms / 1000).toFixed(2)}s` : "?";
  const status = d.status_code ? ` http=${d.status_code}` : "";
  const respc = d.response_chars != null ? `, response=${d.response_chars}c` : "";
  return `${d.provider || "?"}:${d.model || "?"} — ${dur}${status}${respc}`;
}

function jumpToStage(stage) {
  // Switch to the Chat + Trace tab and scroll the matching stage into view.
  const chatTab = document.querySelector('.tab[data-tab="chat"]');
  if (chatTab) chatTab.click();
  // Defer one frame so the tab activation has rendered.
  requestAnimationFrame(() => {
    // The Detail View's renderStage labels the header with the stage name
    // verbatim; find the first stage whose first child <span> matches.
    const headers = traceEl.querySelectorAll(".stage-header > span:first-child");
    for (const h of headers) {
      if (h.textContent.trim() === stage) {
        h.parentElement.scrollIntoView({ behavior: "smooth", block: "start" });
        h.parentElement.style.boxShadow = "0 0 0 2px var(--accent)";
        setTimeout(() => { h.parentElement.style.boxShadow = ""; }, 1500);
        return;
      }
    }
  });
}

$("#flow-refresh").addEventListener("click", refreshFlow);
$("#flow-turn-select").addEventListener("change", async (e) => {
  await renderFlowFor(parseInt(e.target.value, 10));
});

// ---- cache inspector --------------------------------------
//
// v0.6 Tier 2 verification cache. Shows aggregate stats + the most
// recent cached entries with verdict, stability class, hit count,
// and expiry status.

async function refreshCache() {
  const data = await api("GET", "/api/cache");
  const stats = data.stats || {};
  const statsEl = $("#cache-stats");
  statsEl.innerHTML = "";

  // Static cache-table totals on the first line.
  const totalsLine = el("div", { className: "cache-totals" });
  totalsLine.textContent = (
    `${stats.total_entries || 0} entries · `
    + `${stats.immutable_entries || 0} immutable · `
    + `${stats.total_hits || 0} per-entry hits accumulated`
  );
  statsEl.appendChild(totalsLine);

  // Live hit-rate from pipeline_events. Only show if there have been
  // lookups — a cache without lookups is pre-deployment.
  const lookups = stats.lookups || 0;
  if (lookups > 0) {
    const rate = stats.hit_rate;
    const ratePct = rate !== null && rate !== undefined
      ? `${(rate * 100).toFixed(1)}%`
      : "—";
    const rateLine = el("div", { className: "cache-hit-rate" });
    rateLine.appendChild(el("strong", { textContent: `Hit rate: ${ratePct}` }));
    rateLine.appendChild(document.createTextNode(
      ` · ${lookups} lookups (${stats.lookup_hits || 0} hits, `
      + `${stats.lookup_misses || 0} misses`
      + (stats.lookup_errors ? `, ${stats.lookup_errors} errors` : "")
      + ")"
    ));
    statsEl.appendChild(rateLine);

    // Per-stability hits, if any. Useful for spotting which class is
    // most cache-effective.
    const byStab = stats.hits_by_stability || {};
    const stabKeys = Object.keys(byStab).sort();
    if (stabKeys.length) {
      const stabLine = el("div", { className: "cache-by-stability" });
      stabLine.textContent = "  Hits by class: " + stabKeys
        .map((k) => `${k}=${byStab[k]}`).join(" · ");
      statsEl.appendChild(stabLine);
    }
  }

  const container = $("#cache-table");
  container.innerHTML = "";
  const entries = data.entries || [];
  if (!entries.length) {
    container.appendChild(el("p", { className: "hint",
      textContent: "Cache is empty. Set AEDOS_CACHE_TIER2=1 (or the "
                   + "granular AEDOS_CACHE_SCOPING / "
                   + "AEDOS_CACHE_STABILITY / AEDOS_CACHE_WRITES "
                   + "flags individually) and run some turns through "
                   + "retrieval-territory questions to populate it." }));
    return;
  }

  const table = el("table");
  table.appendChild(el("tr", {}, [
    "id", "verdict", "stability", "hits", "expires", "key",
  ].map((h) => el("th", { textContent: h }))));
  entries.forEach((e) => {
    const row = el("tr", {});
    if (e.is_expired) row.classList.add("closed");
    if (e.verdict === "verified") row.classList.add("verified");
    if (e.verdict === "contradicted") row.classList.add("contradicted");

    row.appendChild(el("td", { textContent: String(e.id) }));
    row.appendChild(el("td", { textContent: e.verdict || "?" }));
    row.appendChild(el("td", { textContent: e.stability_class || "?" }));
    row.appendChild(el("td", { textContent: String(e.hit_count ?? 0) }));
    const expires = e.expires_at
      ? (e.is_expired ? `${e.expires_at} (EXPIRED)` : e.expires_at)
      : "(never)";
    row.appendChild(el("td", { className: "mono", textContent: expires }));
    row.appendChild(el("td", { className: "mono",
      textContent: e.canonical_key || "" }));
    table.appendChild(row);
  });
  container.appendChild(table);
}

$("#cache-refresh").addEventListener("click", refreshCache);

// ---- reset ------------------------------------------------

$("#reset-btn").addEventListener("click", async () => {
  if (!confirm("Wipe every fact, turn, and pipeline event. This is not reversible. Proceed?")) return;
  await api("POST", "/api/reset");
  messagesEl.innerHTML = "";
  traceEl.innerHTML = "";
  traceEl.appendChild(el("p", { className: "hint", textContent: "Database reset. Send a message to start fresh." }));
  const active = $(".tab.active")?.dataset.tab;
  if (active === "facts") refreshFacts();
  if (active === "flow") refreshFlow();
  if (active === "cache") refreshCache();
});
