// Aedos trace UI — Substrate inspector (the four oracle classifiers).
//
// Sortable per-oracle list + per-row detail with affirm/contradict
// buttons (the operator-action endpoints — the only paths that mutate
// substrate counts).
//
// Conventions: vanilla JS, no framework, textContent everywhere on
// user / model data, no innerHTML on untrusted input. The `el(...)`
// builder is shared (defined on window by app.js); we re-declare a
// local-scoped fallback in case load order shifts.

(function () {
  "use strict";

  // -----------------------------------------------------------------
  // helpers — minimal duplicates of app.js so v2_trace.js doesn't
  // depend on app.js's load order. Only what the v2 panels need.
  // -----------------------------------------------------------------

  function $(sel) { return document.querySelector(sel); }
  function $$(sel) { return Array.from(document.querySelectorAll(sel)); }

  function el(tag, opts, children) {
    opts = opts || {};
    children = children || [];
    var n = document.createElement(tag);
    if (opts.className) n.className = opts.className;
    if (opts.title) n.title = opts.title;
    if (opts.textContent !== undefined) n.textContent = opts.textContent;
    if (opts.dataset) for (var k in opts.dataset) n.dataset[k] = opts.dataset[k];
    if (opts.style) n.style.cssText = opts.style;
    if (opts.id) n.id = opts.id;
    if (opts.type) n.type = opts.type;
    children.forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  async function api(method, path, body) {
    var opts = { method: method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    var resp = await fetch(path, opts);
    var detail;
    if (!resp.ok) {
      try { detail = await resp.json(); } catch (_) { detail = await resp.text(); }
      var msg = (typeof detail === "object")
        ? (detail.detail || JSON.stringify(detail))
        : detail;
      throw new Error("HTTP " + resp.status + ": " + msg);
    }
    return resp.json();
  }

  function fmt2(x) {
    if (x === null || x === undefined || !isFinite(x)) return "—";
    return Number(x).toFixed(2);
  }

  function fmt3(x) {
    if (x === null || x === undefined || !isFinite(x)) return "—";
    return Number(x).toFixed(3);
  }

  // =================================================================
  // Inspector tab wiring — Substrate tab loads on activation.
  // =================================================================
  //
  // app.js's selectInspectorTab handles the panel show/hide; we just
  // need to refresh the substrate list when the Substrate tab is
  // clicked.

  $$(".inspector-tab").forEach(function (btn) {
    if (btn.dataset.inspectorTab === "substrate") {
      btn.addEventListener("click", function () {
        var active = document.querySelector(".v2-oracle-tab.active");
        var slug = active ? active.dataset.v2Oracle : "predicate-equivalence";
        loadOracleList(slug);
      });
    }
  });

  // =================================================================
  // 8.5b — per-oracle row inspector.
  //
  // One generic component, four instantiations driven by ORACLE_DEFS.
  // Each entry defines: slug (URL path), display name, list endpoint
  // filter UI (if any), and the row schema (ordered list of
  // {field, header, format} for the table columns).
  //
  // Sort is purely client-side. The list endpoint returns full to_dict()
  // for every row, so we don't need a server-side sort parameter.
  //
  // Affirm/contradict POST hits the row's row_id (the integer id), not
  // the natural key, per Phase 8e's URL convention.
  // =================================================================

  var ORACLE_DEFS = {
    "predicate-equivalence": {
      label: "predicate equivalence",
      cols: [
        { field: "pattern", header: "pattern" },
        { field: "predicate_a", header: "a" },
        { field: "predicate_b", header: "b" },
        { field: "label", header: "label" },
        { field: "slot_reversal", header: "slot reversal" },
        { field: "affirmed_count", header: "✓", format: "int" },
        { field: "contradicted_count", header: "✗", format: "int" },
        { field: "confidence", header: "conf", format: "fmt2" },
        { field: "last_consulted_at", header: "last consulted", format: "shortIso" },
      ],
    },
    "entity-equivalence": {
      label: "entity equivalence",
      cols: [
        { field: "entity_a", header: "a" },
        { field: "entity_b", header: "b" },
        { field: "label", header: "label" },
        { field: "affirmed_count", header: "✓", format: "int" },
        { field: "contradicted_count", header: "✗", format: "int" },
        { field: "confidence", header: "conf", format: "fmt2" },
        { field: "last_consulted_at", header: "last consulted", format: "shortIso" },
      ],
    },
    "entity-taxonomy": {
      label: "entity taxonomy",
      cols: [
        { field: "child", header: "child" },
        { field: "parent", header: "parent" },
        { field: "relation_type", header: "relation" },
        { field: "label", header: "label" },
        { field: "affirmed_count", header: "✓", format: "int" },
        { field: "contradicted_count", header: "✗", format: "int" },
        { field: "confidence", header: "conf", format: "fmt2" },
        { field: "last_consulted_at", header: "last consulted", format: "shortIso" },
      ],
    },
    "predicate-distribution": {
      label: "predicate distribution",
      cols: [
        { field: "pattern", header: "pattern" },
        { field: "predicate", header: "predicate" },
        { field: "polarity", header: "pol" },
        { field: "taxonomy_relation_type", header: "rel" },
        { field: "label", header: "distributes" },
        { field: "affirmed_count", header: "✓", format: "int" },
        { field: "contradicted_count", header: "✗", format: "int" },
        { field: "confidence", header: "conf", format: "fmt2" },
        { field: "last_consulted_at", header: "last consulted", format: "shortIso" },
      ],
    },
  };

  // Sort state per oracle slug so switching tabs preserves the user's
  // last sort.
  var oracleSort = {};

  function formatCell(val, fmt) {
    if (val === null || val === undefined) return "—";
    if (fmt === "fmt2") return fmt2(val);
    if (fmt === "fmt3") return fmt3(val);
    if (fmt === "int") return String(Math.trunc(val));
    if (fmt === "shortIso") return String(val).slice(0, 19).replace("T", " ");
    return String(val);
  }

  async function loadOracleList(slug) {
    var def = ORACLE_DEFS[slug];
    var container = $("#v2-oracle-content");
    container.innerHTML = "";
    container.appendChild(el("div", { className: "v2-loading", textContent: "loading " + def.label + "…" }));
    var rows;
    try {
      var resp = await api("GET", "/api/substrate/" + slug);
      rows = resp.rows || [];
    } catch (err) {
      container.innerHTML = "";
      container.appendChild(el("div", { className: "v2-error",
        textContent: "load failed: " + err.message }));
      return;
    }
    container.innerHTML = "";
    var sortState = oracleSort[slug] || { field: "confidence", desc: true };
    oracleSort[slug] = sortState;
    container.appendChild(renderOracleTable(slug, def, rows, sortState));
  }

  function sortRows(rows, field, desc) {
    var sorted = rows.slice();
    sorted.sort(function (a, b) {
      var av = a[field];
      var bv = b[field];
      if (av === null || av === undefined) av = "";
      if (bv === null || bv === undefined) bv = "";
      if (av < bv) return desc ? 1 : -1;
      if (av > bv) return desc ? -1 : 1;
      return 0;
    });
    return sorted;
  }

  function renderOracleTable(slug, def, rows, sortState) {
    var wrap = el("div", { className: "v2-oracle-table-wrap" });
    if (!rows.length) {
      wrap.appendChild(el("p", { className: "hint",
        textContent: "(no " + def.label + " rows yet — populated by oracle consultations and operator actions)" }));
      return wrap;
    }

    var summary = el("div", { className: "v2-oracle-summary",
      textContent: rows.length + " row" + (rows.length === 1 ? "" : "s") });
    wrap.appendChild(summary);

    var sorted = sortRows(rows, sortState.field, sortState.desc);

    var table = el("table", { className: "v2-oracle-table" });
    var thead = el("thead");
    var headerRow = el("tr");
    def.cols.forEach(function (col) {
      var isActive = sortState.field === col.field;
      var indicator = isActive ? (sortState.desc ? " ↓" : " ↑") : "";
      var th = el("th", {
        className: "v2-oracle-th" + (isActive ? " v2-sort-active" : ""),
        textContent: col.header + indicator,
      });
      th.addEventListener("click", function () {
        if (sortState.field === col.field) sortState.desc = !sortState.desc;
        else { sortState.field = col.field; sortState.desc = true; }
        oracleSort[slug] = sortState;
        var content = $("#v2-oracle-content");
        content.innerHTML = "";
        content.appendChild(renderOracleTable(slug, def, rows, sortState));
      });
      headerRow.appendChild(th);
    });
    headerRow.appendChild(el("th", { textContent: "" })); // detail toggle col
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = el("tbody");
    sorted.forEach(function (row) {
      tbody.appendChild(renderOracleRow(slug, def, row));
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function renderOracleRow(slug, def, row) {
    var tr = el("tr", {
      className: "v2-oracle-row",
      dataset: { rowId: String(row.id) },
    });
    def.cols.forEach(function (col) {
      var cell = el("td", {
        className: "v2-oracle-cell v2-oracle-cell-" + col.field,
        textContent: formatCell(row[col.field], col.format),
      });
      tr.appendChild(cell);
    });
    var toggleCell = el("td", { className: "v2-oracle-cell-toggle" });
    var toggle = el("button", {
      className: "v2-row-toggle",
      textContent: "▸",
      title: "Show detail / operator actions",
    });
    toggleCell.appendChild(toggle);
    tr.appendChild(toggleCell);

    // Detail row sits BELOW the data row when expanded.
    var detailTr = el("tr", { className: "v2-oracle-detail-row" });
    var detailTd = el("td", { className: "v2-oracle-detail-cell" });
    detailTd.colSpan = def.cols.length + 1;
    detailTr.appendChild(detailTd);

    var detailOpen = false;
    toggle.addEventListener("click", function () {
      detailOpen = !detailOpen;
      toggle.textContent = detailOpen ? "▾" : "▸";
      detailTr.classList.toggle("open", detailOpen);
      if (detailOpen) {
        detailTd.innerHTML = "";
        detailTd.appendChild(renderOracleDetail(slug, def, row));
      }
    });

    var frag = document.createDocumentFragment();
    frag.appendChild(tr);
    frag.appendChild(detailTr);
    return frag;
  }

  function renderOracleDetail(slug, def, row) {
    var box = el("div", { className: "v2-oracle-detail-box" });

    // Audit metadata.
    var meta = el("div", { className: "v2-oracle-detail-meta" });
    meta.appendChild(el("span", { textContent: "row id #" + row.id }));
    if (row.created_at) {
      meta.appendChild(el("span", {
        textContent: "created " + String(row.created_at).slice(0, 19).replace("T", " "),
      }));
    }
    if (row.last_consulted_at) {
      meta.appendChild(el("span", {
        textContent: "last consulted " + String(row.last_consulted_at).slice(0, 19).replace("T", " "),
      }));
    }
    box.appendChild(meta);

    if (row.reason) {
      box.appendChild(el("div", {
        className: "v2-oracle-detail-reason",
        textContent: "reason: " + row.reason,
      }));
    }

    // Counts + confidence summary
    var conf = el("div", { className: "v2-oracle-detail-conf" });
    conf.appendChild(el("span", { className: "v2-affirmed",
      textContent: "✓ " + (row.affirmed_count || 0) }));
    conf.appendChild(el("span", { className: "v2-contradicted",
      textContent: "✗ " + (row.contradicted_count || 0) }));
    conf.appendChild(el("span", { className: "v2-conf-value",
      textContent: "confidence " + fmt2(row.confidence) }));
    box.appendChild(conf);

    // Operator actions — affirm / contradict. Native confirm() matches
    // v1's destructive-action pattern (see app.js's reset handler).
    var actions = el("div", { className: "v2-oracle-detail-actions" });
    var affirmBtn = el("button", {
      className: "v2-action-affirm",
      textContent: "Affirm (+1)",
      title: "Increment affirmed_count by 1. NOT idempotent — each click is one independent external evidence event.",
    });
    var contradictBtn = el("button", {
      className: "v2-action-contradict",
      textContent: "Contradict (+1)",
      title: "Increment contradicted_count by 1. NOT idempotent — each click is one independent external evidence event.",
    });
    var status = el("div", { className: "v2-oracle-detail-status" });

    function describeRow(slug) {
      // Short human-readable identity used in the confirm() prompt.
      if (slug === "predicate-equivalence") {
        return "(" + row.pattern + ") " + row.predicate_a + " ↔ " + row.predicate_b
               + " — label=" + row.label;
      }
      if (slug === "entity-equivalence") {
        return row.entity_a + " ↔ " + row.entity_b + " — label=" + row.label;
      }
      if (slug === "entity-taxonomy") {
        return row.child + " → " + row.parent
               + " (" + row.relation_type + ") — label=" + row.label;
      }
      if (slug === "predicate-distribution") {
        return "(" + row.pattern + ") " + row.predicate + "/" + row.polarity
               + " under " + row.taxonomy_relation_type + " — " + row.label;
      }
      return "row #" + row.id;
    }

    function doAction(action, btn) {
      var verb = action === "affirm" ? "AFFIRM" : "CONTRADICT";
      if (!window.confirm(
            verb + " this oracle row?\n\n" + describeRow(slug)
            + "\n\nThis is non-idempotent — one click is one independent "
            + "external evidence event."
          )) return;
      affirmBtn.disabled = true;
      contradictBtn.disabled = true;
      status.textContent = "submitting…";
      api("POST", "/api/substrate/" + slug + "/" + row.id + "/" + action)
        .then(function (resp) {
          // Update the row in place.
          row.affirmed_count = resp.affirmed_count;
          row.contradicted_count = resp.contradicted_count;
          row.confidence = resp.confidence;
          row.last_consulted_at = new Date().toISOString();
          // Refresh the visible cells.
          var tr = btn.closest("tr.v2-oracle-detail-row").previousSibling;
          if (tr) {
            def.cols.forEach(function (col) {
              var cell = tr.querySelector(".v2-oracle-cell-" + col.field);
              if (cell) cell.textContent = formatCell(row[col.field], col.format);
            });
          }
          // Refresh the detail conf row too.
          conf.innerHTML = "";
          conf.appendChild(el("span", { className: "v2-affirmed",
            textContent: "✓ " + row.affirmed_count }));
          conf.appendChild(el("span", { className: "v2-contradicted",
            textContent: "✗ " + row.contradicted_count }));
          conf.appendChild(el("span", { className: "v2-conf-value",
            textContent: "confidence " + fmt2(row.confidence) }));
          status.textContent = verb.toLowerCase() + " applied — counts updated";
          status.classList.remove("v2-error");
        })
        .catch(function (err) {
          status.textContent = "failed: " + err.message;
          status.classList.add("v2-error");
        })
        .then(function () {
          affirmBtn.disabled = false;
          contradictBtn.disabled = false;
        });
    }

    affirmBtn.addEventListener("click", function () { doAction("affirm", affirmBtn); });
    contradictBtn.addEventListener("click", function () { doAction("contradict", contradictBtn); });

    actions.appendChild(affirmBtn);
    actions.appendChild(contradictBtn);
    actions.appendChild(status);
    box.appendChild(actions);
    return box;
  }

  // Wire the oracle sub-tabs.
  $$(".v2-oracle-tab").forEach(function (btn) {
    btn.addEventListener("click", function () {
      $$(".v2-oracle-tab").forEach(function (b) { b.classList.remove("active"); });
      btn.classList.add("active");
      loadOracleList(btn.dataset.v2Oracle);
    });
  });
  var refreshBtn = $("#v2-oracle-refresh");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", function () {
      var active = document.querySelector(".v2-oracle-tab.active");
      var slug = active ? active.dataset.v2Oracle : "predicate-equivalence";
      loadOracleList(slug);
    });
  }

  // =================================================================
  // Decision confidence breakdown (Layer 5).
  //
  // Pure rendering function. Takes a DecisionConfidence dict (path_prior,
  // chain_reliability, evidence_strength, value, explanation) plus the
  // threshold T and the planned Intervention. Renders the product as a
  // headline, three factor rows with bars showing where each falls
  // relative to 1.0, and the intervention type pill.
  // =================================================================

  var INTERVENTION_LABELS = {
    pass_through: "Verified above threshold; assistant text passes unchanged.",
    replace:      "A trusted source contradicts the claim; the corrector swaps in the verified value.",
    hedge:        "Evidence is thin; corrector adds an 'I'm not sure' qualifier.",
    soften:       "Claim is unverifiable in principle (preference, attitude); corrector reframes it as a perspective.",
    noop:         "No intervention warranted (or routing anomaly flagged for operator).",
  };

  function renderDecisionConfidence(dc, threshold, intervention) {
    var T = (threshold !== undefined && threshold !== null) ? threshold : 0.5;
    var box = el("div", { className: "v2-dc-box" });
    box.appendChild(el("div", { className: "v2-dc-title", textContent: "decision confidence" }));

    var hardVerdict = dc.value >= T;
    var headline = el("div", {
      className: "v2-dc-headline " + (hardVerdict ? "v2-dc-hard" : "v2-dc-soft"),
    });
    headline.appendChild(el("span", { className: "v2-dc-headline-value",
      textContent: fmt3(dc.value) }));
    headline.appendChild(el("span", { className: "v2-dc-headline-vs",
      textContent: hardVerdict ? "≥" : "<" }));
    headline.appendChild(el("span", { className: "v2-dc-headline-t",
      textContent: "T=" + fmt2(T) }));
    headline.appendChild(el("span", { className: "v2-dc-headline-tag",
      textContent: hardVerdict ? "hard verdict" : "soft verdict" }));
    box.appendChild(headline);

    [
      { name: "path_prior", value: dc.path_prior },
      { name: "chain_reliability", value: dc.chain_reliability },
      { name: "evidence_strength", value: dc.evidence_strength },
    ].forEach(function (f) {
      box.appendChild(_renderDcFactor(f.name, f.value));
    });

    if (intervention) {
      var ivBox = el("div", { className: "v2-dc-iv" });
      ivBox.appendChild(el("span", { className: "v2-iv-label",
        textContent: "intervention:" }));
      ivBox.appendChild(el("span", {
        className: "v2-iv-pill v2-iv-pill-" + intervention.intervention_type,
        textContent: intervention.intervention_type,
        title: INTERVENTION_LABELS[intervention.intervention_type] || "intervention",
      }));
      if (intervention.flag_operator) {
        ivBox.appendChild(el("span", { className: "v2-iv-flag",
          textContent: "⚑ flagged for operator" }));
      }
      if (intervention.reason) {
        ivBox.appendChild(el("div", { className: "v2-iv-reason",
          textContent: intervention.reason }));
      }
      box.appendChild(ivBox);
    }
    if (dc.explanation) {
      box.appendChild(el("div", { className: "v2-dc-explanation",
        textContent: dc.explanation }));
    }
    return box;
  }

  function _renderDcFactor(name, value) {
    var row = el("div", { className: "v2-dc-factor" });
    row.appendChild(el("span", { className: "v2-dc-factor-name", textContent: name }));
    var bar = el("div", { className: "v2-dc-bar" });
    var fill = el("div", { className: "v2-dc-bar-fill",
      style: "width:" + (Math.max(0, Math.min(1, value || 0)) * 100).toFixed(1) + "%;" });
    bar.appendChild(fill);
    row.appendChild(bar);
    row.appendChild(el("span", { className: "v2-dc-factor-value", textContent: fmt3(value) }));
    return row;
  }

  // =================================================================
  // Derivation chain visualization (Layer 4).
  //
  // Takes a derivation_path (list of ChainEdge dicts) plus the
  // chain_reliability. Renders an ordered list of edges with their
  // oracle, label, confidence, and from→to state diff.
  // =================================================================

  var MIN_CHAIN_RELIABILITY = 0.4;

  function renderChain(derivationPath, chainReliability) {
    var box = el("div", { className: "v2-chain-box" });
    box.appendChild(el("div", { className: "v2-chain-title",
      textContent: "derivation chain (" + derivationPath.length + " edge"
                   + (derivationPath.length === 1 ? "" : "s") + ")" }));

    var head = el("div", {
      className: "v2-chain-head "
        + (chainReliability >= MIN_CHAIN_RELIABILITY
            ? "v2-chain-pass" : "v2-chain-fail"),
    });
    head.appendChild(el("span", { className: "v2-chain-head-label",
      textContent: "chain_reliability" }));
    var rel = el("div", { className: "v2-chain-rel-bar" });
    var fill = el("div", { className: "v2-chain-rel-fill",
      style: "width:" + (Math.max(0, Math.min(1, chainReliability || 0)) * 100).toFixed(1) + "%;" });
    rel.appendChild(fill);
    head.appendChild(rel);
    head.appendChild(el("span", { className: "v2-chain-head-value",
      textContent: fmt3(chainReliability) }));
    box.appendChild(head);

    if (!derivationPath.length) {
      box.appendChild(el("p", { className: "hint",
        textContent: "(no derivation chain — claim resolved at a lookup tier)" }));
      return box;
    }

    var list = el("ol", { className: "v2-chain-list" });
    derivationPath.forEach(function (edge, i) {
      list.appendChild(_renderChainEdge(edge, i));
    });
    box.appendChild(list);
    return box;
  }

  function _renderChainEdge(edge, idx) {
    var li = el("li", { className: "v2-chain-edge" });
    var head = el("div", { className: "v2-chain-edge-head" });
    head.appendChild(el("span", { className: "v2-chain-edge-step",
      textContent: "step " + (idx + 1) }));
    head.appendChild(el("span", { className: "v2-chain-edge-oracle",
      textContent: edge.oracle }));
    if (edge.row_id != null) {
      head.appendChild(el("span", { className: "v2-chain-edge-row",
        textContent: "row #" + edge.row_id }));
    }
    head.appendChild(el("span", { className: "v2-chain-edge-label",
      textContent: edge.label || "—" }));
    head.appendChild(el("span", { className: "v2-chain-edge-conf",
      textContent: "conf " + fmt2(edge.confidence) }));
    li.appendChild(head);

    var diff = _stateDiff(edge.from_state || {}, edge.to_state || {});
    if (diff.length) {
      var diffWrap = el("div", { className: "v2-chain-edge-diff" });
      diff.forEach(function (d) {
        diffWrap.appendChild(el("div", {
          className: "v2-chain-edge-diff-row",
          textContent: d.field + ": " + _jsonish(d.from) + " → " + _jsonish(d.to),
        }));
      });
      li.appendChild(diffWrap);
    }
    if (edge.notes) {
      li.appendChild(el("div", { className: "v2-chain-edge-notes",
        textContent: edge.notes }));
    }
    return li;
  }

  function _jsonish(v) {
    if (v === undefined || v === null) return "—";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }

  function _stateDiff(a, b) {
    var keys = new Set();
    Object.keys(a || {}).forEach(function (k) { keys.add(k); });
    Object.keys(b || {}).forEach(function (k) { keys.add(k); });
    var out = [];
    Array.from(keys).sort().forEach(function (k) {
      var av = JSON.stringify(a ? a[k] : undefined);
      var bv = JSON.stringify(b ? b[k] : undefined);
      if (av !== bv) {
        out.push({ field: k, from: a ? a[k] : undefined, to: b ? b[k] : undefined });
      }
    });
    return out;
  }

  // =================================================================
  // Public exports.
  // =================================================================

  window.Aedos = window.Aedos || {};
  window.Aedos.loadOracleList = loadOracleList;
  window.Aedos.renderDecisionConfidence = renderDecisionConfidence;
  window.Aedos.renderChain = renderChain;
})();
