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
    const detail = await resp.text();
    throw new Error(`${method} ${path} failed (${resp.status}): ${detail}`);
  }
  return resp.json();
}

function triplify(claim) {
  const pol = claim.polarity === 1
    ? el("span", { className: "pol-pos", textContent: "+" })
    : el("span", { className: "pol-neg", textContent: "−" });
  const triple = el("span", { className: "triple" }, [
    document.createTextNode(`(${claim.subject}, `),
    el("span", { className: "pred", textContent: claim.predicate }),
    document.createTextNode(`, ${claim.object})`),
  ]);
  const src = el("span", { className: "src", textContent: `"${claim.source_text || ""}"` });
  return el("div", { className: "claim" }, [triple, pol, el("span"), src]);
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
  // Promote routing-anomaly events to a banner at the top of the trace
  // so they are impossible to miss.
  const anomalies = events.filter((e) => e.stage === "routing_anomaly_detected");
  anomalies.forEach((ev) => traceEl.appendChild(renderAnomalyBanner(ev)));

  events
    .filter((e) => e.stage !== "routing_anomaly_detected")
    .forEach((ev) => traceEl.appendChild(renderStage(ev)));
}

function renderAnomalyBanner(event) {
  const d = event.data || {};
  const claim = d.claim || {};
  const banner = el("div", { className: "anomaly-banner" });
  banner.appendChild(el("strong", {
    textContent: "⚠ Routing anomaly detected — likely extractor error",
  }));
  const body = el("div");
  body.appendChild(document.createTextNode("Predicate "));
  body.appendChild(el("code", { textContent: claim.predicate || "?" }));
  body.appendChild(document.createTextNode(" was asserted about non-user subject "));
  body.appendChild(el("code", { textContent: claim.subject || "?" }));
  body.appendChild(document.createTextNode(
    ". This usually means the extractor chose the wrong predicate. " +
    "Consider whether the source phrasing should map to a different predicate.",
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
  if (data.valid_claims && data.valid_claims.length) {
    body.appendChild(el("div", { textContent: "Valid claims:" }));
    data.valid_claims.forEach((c) => body.appendChild(triplify(c)));
  } else {
    body.appendChild(el("div", { className: "hint", textContent: "(no valid claims extracted)" }));
  }
  if (data.rejected_claims && data.rejected_claims.length) {
    const rej = el("div", { className: "rejected-claims" });
    rej.appendChild(el("strong", { textContent: "Rejected:" }));
    data.rejected_claims.forEach((r) => {
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

function renderRetrievalBlock(rr) {
  const node = el("div", { className: "retrieval-block" });
  if (rr.query) {
    node.appendChild(el("div", { className: "query", textContent: `query: ${rr.query}` }));
  }
  if (rr.verdict) {
    const v = rr.verdict;
    const verdictEl = el("div", { className: `verdict verdict-${v.verdict}` });
    verdictEl.textContent = `judge: ${v.verdict}`;
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
  if (rr.from_cache) {
    node.appendChild(el("span", {
      className: "decision-meta",
      textContent: " (cached)",
    }));
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

    if (d.verifier_result) {
      node.appendChild(
        el("div", {
          className: "verifier-explanation",
          textContent: `verifier: ${d.verifier_result.outcome} — ${d.verifier_result.explanation}`,
        }),
      );
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
  const subj = $("#f-subject").value.trim();
  const pred = $("#f-predicate").value.trim();
  const ab = $("#f-asserted-by").value;
  const st = $("#f-status").value;
  const onlyValid = $("#f-only-valid").checked;
  if (subj) p.set("subject", subj);
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
    "id", "subject", "predicate", "object", "pol", "confidence", "asserted_by", "status", "valid_until", "turn", "src",
  ].map((h) => el("th", { textContent: h })));
  table.appendChild(head);
  facts.forEach((f) => {
    const tr = el("tr");
    if (f.valid_until) tr.classList.add("closed");
    if (f.verification_status === "contradicted") tr.classList.add("contradicted");
    if (f.verification_status === "verified") tr.classList.add("verified");

    const cells = [
      el("td", { textContent: String(f.id) }),
      el("td", { textContent: String(f.subject) }),
      el("td", { textContent: String(f.predicate) }),
      el("td", { className: "mono", textContent: String(f.object) }),
      el("td", { textContent: String(f.polarity) }),
      el("td"),                   // confidence (rendered below)
      el("td", { textContent: String(f.asserted_by) }),
      el("td"),                   // status badge (below)
      el("td", { textContent: f.valid_until || "—" }),
      el("td", { textContent: String(f.source_turn_id ?? "") }),
      el("td", { className: "mono", textContent: String(f.source_text ?? "") }),
    ];
    // Confidence: render as a colored bar plus numeric.
    if (typeof f.confidence === "number") {
      const bar = el("span", { className: "confidence-bar" });
      const overlay = el("span");
      overlay.style.width = `${Math.max(0, (1 - f.confidence) * 100)}%`;
      bar.appendChild(overlay);
      cells[5].appendChild(bar);
      cells[5].appendChild(el("span", {
        className: "confidence-text",
        textContent: f.confidence.toFixed(2),
      }));
    } else {
      cells[5].textContent = String(f.confidence);
    }
    cells[7].appendChild(statusBadge(f.verification_status));

    cells.forEach((c) => tr.appendChild(c));
    table.appendChild(tr);
  });
  container.appendChild(table);
}

["#f-subject", "#f-predicate"].forEach((s) => {
  $(s).addEventListener("keydown", (e) => { if (e.key === "Enter") refreshFacts(); });
});
["#f-asserted-by", "#f-status", "#f-only-valid"].forEach((s) => {
  $(s).addEventListener("change", refreshFacts);
});
$("#facts-refresh").addEventListener("click", refreshFacts);

// ---- predicates inspector ---------------------------------

async function refreshPredicates() {
  const preds = await api("GET", "/api/predicates");
  const container = $("#predicates-table");
  container.innerHTML = "";
  const byMethod = {};
  preds.forEach((p) => {
    (byMethod[p.verification_method] = byMethod[p.verification_method] || []).push(p);
  });
  // Group order: user_authoritative → python → retrieval → unverifiable
  const ORDER = ["user_authoritative", "python", "retrieval", "unverifiable"];
  ORDER.forEach((method) => {
    const entries = byMethod[method];
    if (!entries) return;
    container.appendChild(el("h3", { textContent: `verification_method: ${method}` }));
    const table = el("table");
    const headers = ["name", "object_type", "python_verifier", "retrieval_query_template", "description", "example"];
    table.appendChild(el("tr", {}, headers.map((h) => el("th", { textContent: h }))));
    entries.forEach((p) => {
      const tr = el("tr", {}, [
        el("td", { textContent: p.name }),
        el("td", { textContent: p.object_type }),
        el("td", { className: "mono", textContent: p.python_verifier || "—" }),
        el("td", { className: "mono", textContent: p.retrieval_query_template || "—" }),
        el("td", { textContent: p.description }),
        el("td", { textContent: p.example }),
      ]);
      table.appendChild(tr);
    });
    container.appendChild(table);
  });
}

// ---- reset ------------------------------------------------

$("#reset-btn").addEventListener("click", async () => {
  if (!confirm("Wipe every fact, turn, and pipeline event. This is not reversible. Proceed?")) return;
  await api("POST", "/api/reset");
  messagesEl.innerHTML = "";
  traceEl.innerHTML = "";
  traceEl.appendChild(el("p", { className: "hint", textContent: "Database reset. Send a message to start fresh." }));
  const active = $(".tab.active")?.dataset.tab;
  if (active === "facts") refreshFacts();
});
