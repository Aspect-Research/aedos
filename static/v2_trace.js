// Aedos v0.14 Phase 8.5 — trace UI for the v2 stack.
//
// Three deliverables, all additive — no v1 styles or behaviors are
// modified.
//
//   1. v2 Substrate inspector — sortable per-oracle list + per-row
//      detail with affirm/contradict buttons (operator-action endpoints).
//   2. v2 Walk panel — drives /v2/api/dispatch-one with a structured
//      claim, renders WalkerDecision + DecisionConfidence + Intervention
//      + chain visualization.
//   3. Helpers shared by both: fetch wrappers, DOM builder reuse.
//
// Conventions match v1's static/app.js: vanilla JS, no framework,
// textContent everywhere on user / model data, no innerHTML on
// untrusted input. The `el(...)` builder is shared (defined on window
// by app.js); we re-declare a local-scoped fallback just in case
// load order shifts.

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
  // Inspector tab wiring (additive — does not replace v1's wiring).
  // =================================================================

  // v1's selectInspectorTab knows about memory / patterns / pipeline.
  // It hides every .inspector-panel that doesn't match the active
  // tab, which works correctly for our two new panels because they
  // also have the .inspector-panel class. We just need to react when
  // our tabs are clicked: refresh the substrate panel on demand,
  // load the walk panel's form once.

  function selectInspectorTabV2(name) {
    $$(".inspector-tab").forEach(function (b) {
      b.classList.toggle("active", b.dataset.inspectorTab === name);
    });
    $$(".inspector-panel").forEach(function (p) {
      p.classList.toggle("active", p.id === ("inspector-" + name));
    });
    if (name === "v2-substrate") {
      // Activate whichever oracle tab is currently selected.
      var active = document.querySelector(".v2-oracle-tab.active");
      var slug = active ? active.dataset.v2Oracle : "predicate-equivalence";
      loadOracleList(slug);
    } else if (name === "v2-walk") {
      // Form is static; nothing to load.
    }
  }

  // Patch the v1 click handlers so v2 tabs route through our handler.
  // v1's app.js attaches listeners to .inspector-tab; ours runs after,
  // so an additional listener (per-button) is the cleanest hook.
  $$(".inspector-tab").forEach(function (btn) {
    if (btn.dataset.inspectorTab === "v2-substrate"
        || btn.dataset.inspectorTab === "v2-walk") {
      btn.addEventListener("click", function () {
        selectInspectorTabV2(btn.dataset.inspectorTab);
      });
    } else {
      // For the v1 tabs, also delegate so clicking a v1 tab AFTER
      // a v2 tab correctly hides the v2 panel.
      btn.addEventListener("click", function () {
        $$(".inspector-panel").forEach(function (p) {
          p.classList.toggle("active",
            p.id === ("inspector-" + btn.dataset.inspectorTab));
        });
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
      var resp = await api("GET", "/v2/api/substrate/" + slug);
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
      api("POST", "/v2/api/substrate/" + slug + "/" + row.id + "/" + action)
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
  // 8.5a — decision-confidence breakdown.
  //
  // Pure rendering function. Takes a DecisionConfidence dict (path_prior,
  // chain_reliability, evidence_strength, value, explanation) plus the
  // threshold T. Renders product as headline, three factor rows with
  // bars showing where each falls relative to 1.0, threshold marker,
  // intervention type pill below.
  // =================================================================

  var INTERVENTION_LABELS = {
    pass_through: { tip: "Verified above threshold; assistant text passes unchanged." },
    replace:      { tip: "A trusted source contradicts the claim; the corrector swaps in the verified value." },
    hedge:        { tip: "Evidence is thin; corrector adds an 'I'm not sure' qualifier." },
    soften:       { tip: "Claim is unverifiable in principle (preference, attitude); corrector reframes it as a perspective." },
    noop:         { tip: "No intervention warranted (or routing anomaly flagged for operator)." },
  };

  function renderDecisionConfidence(dc, threshold, intervention) {
    var T = threshold !== undefined ? threshold : 0.5;
    var box = el("div", { className: "v2-dc-box" });

    box.appendChild(el("div", { className: "v2-dc-title", textContent: "decision confidence" }));

    // Headline: the product, color-coded above/below T.
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

    // Three factor rows.
    var factors = [
      { name: "path_prior", value: dc.path_prior,
        explainer: factorExplainer("path_prior", dc, intervention) },
      { name: "chain_reliability", value: dc.chain_reliability,
        explainer: factorExplainer("chain_reliability", dc, intervention) },
      { name: "evidence_strength", value: dc.evidence_strength,
        explainer: factorExplainer("evidence_strength", dc, intervention) },
    ];
    factors.forEach(function (f) {
      box.appendChild(renderDcFactor(f.name, f.value, f.explainer));
    });

    // Intervention type pill, with tooltip.
    if (intervention) {
      var ivBox = el("div", { className: "v2-dc-iv" });
      var pill = el("span", {
        className: "v2-iv-pill v2-iv-pill-" + intervention.intervention_type,
        textContent: intervention.intervention_type,
        title: (INTERVENTION_LABELS[intervention.intervention_type] || {}).tip
               || "intervention",
      });
      ivBox.appendChild(el("span", { className: "v2-iv-label",
        textContent: "intervention:" }));
      ivBox.appendChild(pill);
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

  function renderDcFactor(name, value, explainer) {
    var row = el("div", { className: "v2-dc-factor" });
    row.appendChild(el("span", { className: "v2-dc-factor-name", textContent: name }));
    var bar = el("div", { className: "v2-dc-bar" });
    var fill = el("div", { className: "v2-dc-bar-fill",
      style: "width:" + (Math.max(0, Math.min(1, value)) * 100).toFixed(1) + "%;" });
    bar.appendChild(fill);
    row.appendChild(bar);
    row.appendChild(el("span", { className: "v2-dc-factor-value", textContent: fmt3(value) }));
    if (explainer) {
      row.appendChild(el("span", { className: "v2-dc-factor-explain", textContent: explainer }));
    }
    return row;
  }

  function factorExplainer(name, dc, intervention) {
    if (name === "path_prior") {
      // Re-derive a short label from the value bands the v2 confidence
      // module commits to (Python ≈ 0.99, retrieval ≈ 0.85,
      // user-authoritative = 1.0, routing-anomaly = 0.0).
      if (dc.path_prior === 0) return "(routing anomaly)";
      if (dc.path_prior >= 0.99) return "(python verifier or user-authoritative)";
      if (dc.path_prior >= 0.85) return "(retrieval verifier)";
      return "";
    }
    if (name === "chain_reliability") {
      if (dc.chain_reliability >= 1.0) return "(direct match — no chain)";
      if (dc.chain_reliability >= 0.5) return "(min-link Beta posterior across oracle rows)";
      return "(below cold-start prior — fresh oracle row)";
    }
    if (name === "evidence_strength") {
      return "(Phase-8 contract: 1.0; graded score lands in v0.15+)";
    }
    return "";
  }

  // =================================================================
  // 8.5c — derivation chain visualization.
  //
  // Takes a derivation_path (list of ChainEdge dicts) plus the
  // chain_reliability and an array of the turn's pipeline events so
  // active-classification badges can be cross-referenced.
  // =================================================================

  var MIN_CHAIN_RELIABILITY = 0.4; // matches src/aedos_v2/layer4_lookup/derivation.py

  function renderChain(derivationPath, chainReliability, events) {
    var box = el("div", { className: "v2-chain-box" });
    box.appendChild(el("div", { className: "v2-chain-title",
      textContent: "derivation chain (" + derivationPath.length + " edge"
                   + (derivationPath.length === 1 ? "" : "s") + ")" }));

    // Chain reliability headline + bar.
    var head = el("div", {
      className: "v2-chain-head "
        + (chainReliability >= MIN_CHAIN_RELIABILITY
            ? "v2-chain-pass" : "v2-chain-fail"),
    });
    head.appendChild(el("span", { className: "v2-chain-head-label",
      textContent: "chain_reliability" }));
    var rel = el("div", { className: "v2-chain-rel-bar" });
    var fill = el("div", { className: "v2-chain-rel-fill",
      style: "width:" + (Math.max(0, Math.min(1, chainReliability)) * 100).toFixed(1) + "%;" });
    rel.appendChild(fill);
    var floor = el("div", { className: "v2-chain-rel-floor",
      style: "left:" + (MIN_CHAIN_RELIABILITY * 100).toFixed(1) + "%;" });
    floor.title = "minimum chain reliability floor: " + MIN_CHAIN_RELIABILITY;
    rel.appendChild(floor);
    head.appendChild(rel);
    head.appendChild(el("span", { className: "v2-chain-head-value",
      textContent: fmt3(chainReliability) }));
    box.appendChild(head);

    if (!derivationPath.length) {
      box.appendChild(el("p", { className: "hint",
        textContent: "(no derivation chain — claim resolved at a lookup tier)" }));
      return box;
    }

    // Build a set of (oracle, key) tuples that were freshly classified
    // during THIS walk so we can badge those edges.
    var freshlyClassified = collectFreshClassifications(events || []);

    var list = el("ol", { className: "v2-chain-list" });
    derivationPath.forEach(function (edge, i) {
      list.appendChild(renderChainEdge(edge, i, freshlyClassified));
    });
    box.appendChild(list);
    return box;
  }

  function collectFreshClassifications(events) {
    // Build a Set of canonical "oracle|key" strings from
    // derivation_walk_active_classification events. Edges whose
    // (oracle, row_id) match any of these get a "newly classified"
    // badge — though row_id isn't on the active-classification event,
    // we match heuristically on oracle name + key fields.
    var out = new Set();
    events.forEach(function (e) {
      if (e.stage === "derivation_walk_active_classification" && e.data) {
        var key = e.data.key || {};
        var sig = e.data.oracle + "|"
          + (key.pattern || "") + "|"
          + (key.predicate || "") + "|"
          + (key.polarity != null ? key.polarity : "") + "|"
          + (key.taxonomy_relation_type || "");
        out.add(sig);
      }
    });
    return out;
  }

  function edgeFreshSig(edge) {
    // Reverse-lookup signature for predicate_distribution edges.
    // Other oracles don't fire active-classification events in v0.14.
    if (edge.oracle !== "predicate_distribution") return null;
    var f = edge.from_state || {};
    var t = edge.to_state || {};
    // The from/to states carry the (pattern, predicate, polarity,
    // relation_type) the walker consulted with.
    var pattern = f.pattern || t.pattern || "";
    var predicate = f.predicate || t.predicate || "";
    var polarity = (f.polarity != null) ? f.polarity
                                        : (t.polarity != null ? t.polarity : "");
    var relType = f.taxonomy_relation_type || t.taxonomy_relation_type
                   || edge.relation_type || "";
    return "predicate_distribution|" + pattern + "|" + predicate + "|"
           + polarity + "|" + relType;
  }

  function renderChainEdge(edge, idx, freshlyClassified) {
    var li = el("li", { className: "v2-chain-edge" });
    var head = el("div", { className: "v2-chain-edge-head" });
    head.appendChild(el("span", { className: "v2-chain-edge-step",
      textContent: "step " + (idx + 1) }));
    head.appendChild(el("span", { className: "v2-chain-edge-oracle",
      textContent: edge.oracle }));
    if (edge.row_id != null) {
      var slug = oracleSlugFromName(edge.oracle);
      var link = el("a", {
        className: "v2-chain-edge-link",
        textContent: "row #" + edge.row_id,
        title: "Open this row in the v2 Substrate inspector",
      });
      link.href = "#";
      link.addEventListener("click", function (e) {
        e.preventDefault();
        if (slug) {
          // Switch to the substrate inspector tab for this oracle and
          // the operator can scroll to the row id.
          selectInspectorTabV2("v2-substrate");
          $$(".v2-oracle-tab").forEach(function (b) {
            b.classList.toggle("active", b.dataset.v2Oracle === slug);
          });
          loadOracleList(slug).then(function () {
            scrollToOracleRow(edge.row_id);
          });
        }
      });
      head.appendChild(link);
    }
    head.appendChild(el("span", { className: "v2-chain-edge-label",
      textContent: edge.label || "—" }));
    head.appendChild(el("span", { className: "v2-chain-edge-conf",
      textContent: "conf " + fmt2(edge.confidence) }));
    var sig = edgeFreshSig(edge);
    if (sig && freshlyClassified.has(sig)) {
      head.appendChild(el("span", { className: "v2-chain-edge-fresh",
        title: "Cold cell classified during this walk (active-classification budget)",
        textContent: "newly classified" }));
    }
    li.appendChild(head);

    // State diff: show what changed between from_state and to_state.
    var diff = stateDiff(edge.from_state || {}, edge.to_state || {});
    if (diff.length) {
      var diffWrap = el("div", { className: "v2-chain-edge-diff" });
      diff.forEach(function (d) {
        diffWrap.appendChild(el("div", {
          className: "v2-chain-edge-diff-row",
          textContent: d.field + ": " + jsonish(d.from) + " → " + jsonish(d.to),
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

  function jsonish(v) {
    if (v === undefined || v === null) return "—";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }

  function stateDiff(a, b) {
    // Show fields that exist in either side and differ.
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

  function oracleSlugFromName(oracleName) {
    // The walker's edges carry oracle names with underscores; the
    // inspector URLs use dashes.
    var map = {
      predicate_equivalence: "predicate-equivalence",
      entity_equivalence: "entity-equivalence",
      entity_taxonomy: "entity-taxonomy",
      predicate_distribution: "predicate-distribution",
    };
    return map[oracleName] || null;
  }

  function scrollToOracleRow(rowId) {
    // Best-effort scroll; the row may need a few ms to land.
    setTimeout(function () {
      var tr = document.querySelector(".v2-oracle-row[data-row-id='" + rowId + "']");
      if (tr) {
        tr.scrollIntoView({ block: "center", behavior: "smooth" });
        tr.classList.add("v2-oracle-row-highlight");
        setTimeout(function () { tr.classList.remove("v2-oracle-row-highlight"); }, 2000);
      }
    }, 60);
  }

  // =================================================================
  // v2 Walk panel — drives /v2/api/dispatch-one.
  // =================================================================

  var walkForm = $("#v2-walk-form");
  if (walkForm) {
    walkForm.addEventListener("submit", function (e) {
      e.preventDefault();
      submitWalk();
    });
  }

  async function submitWalk() {
    var pattern = ($("#v2-walk-pattern").value || "").trim();
    var predicate = ($("#v2-walk-predicate").value || "").trim();
    var polarity = parseInt($("#v2-walk-polarity").value, 10);
    var slotsRaw = ($("#v2-walk-slots").value || "").trim();
    var source = ($("#v2-walk-source").value || "").trim();
    var fresh = $("#v2-walk-fresh").checked;

    var slots = {};
    if (slotsRaw) {
      try {
        slots = JSON.parse(slotsRaw);
      } catch (err) {
        renderWalkError("slots JSON parse failed: " + err.message);
        return;
      }
    }

    var body = {
      claim: {
        pattern: pattern, predicate: predicate, polarity: polarity,
        slots: slots,
      },
      run_fresh: fresh,
    };
    if (source) body.claim.source_text = source;

    var result = $("#v2-walk-result");
    result.innerHTML = "";
    result.appendChild(el("div", { className: "v2-walk-loading",
      textContent: "dispatching…" }));

    try {
      var resp = await api("POST", "/v2/api/dispatch-one", body);
      result.innerHTML = "";
      result.appendChild(renderWalkResult(resp));
    } catch (err) {
      renderWalkError(err.message);
    }
  }

  function renderWalkError(msg) {
    var result = $("#v2-walk-result");
    result.innerHTML = "";
    result.appendChild(el("div", { className: "v2-walk-error", textContent: msg }));
  }

  function renderWalkResult(resp) {
    var box = el("div", { className: "v2-walk-result-box" });

    // Top-line summary
    var wd = resp.walker_decision || {};
    var iv = resp.intervention || {};
    var dc = resp.decision_confidence || {};

    var summary = el("div", { className: "v2-walk-summary" });
    summary.appendChild(el("span", {
      className: "v2-walk-tier v2-walk-tier-" + wd.served_from_tier,
      textContent: "tier: " + wd.served_from_tier,
    }));
    summary.appendChild(el("span", {
      className: "v2-walk-outcome v2-walk-outcome-" + wd.outcome,
      textContent: wd.outcome,
    }));
    summary.appendChild(el("span", {
      className: "v2-walk-status v2-walk-status-" + (wd.verification_status || "unknown"),
      textContent: wd.verification_status || "—",
    }));
    if (wd.routing_method) {
      summary.appendChild(el("span", { className: "v2-walk-method",
        textContent: "routing: " + wd.routing_method }));
    }
    summary.appendChild(el("span", { className: "v2-walk-turn",
      textContent: "turn " + resp.turn_id }));
    box.appendChild(summary);

    // Decision confidence panel (8.5a)
    box.appendChild(renderDecisionConfidence(dc, resp.threshold, iv));

    // Derivation chain (8.5c) — only when served_from_tier === 'derivation'
    if (wd.served_from_tier === "derivation"
        && wd.derivation_path && wd.derivation_path.length) {
      box.appendChild(renderChain(wd.derivation_path,
                                   wd.chain_reliability,
                                   resp.events || []));
    } else if (wd.via && wd.via.length) {
      // Non-derivation paths still record a `via` list (oracle-mediated
      // resolution chain in U or W). Show it as a compact pill row.
      var viaBox = el("div", { className: "v2-walk-via" });
      viaBox.appendChild(el("span", { className: "v2-walk-via-label",
        textContent: "via:" }));
      wd.via.forEach(function (v) {
        viaBox.appendChild(el("span", { className: "v2-walk-via-pill",
          textContent: v }));
      });
      box.appendChild(viaBox);
    }

    // Notes
    if (wd.notes && wd.notes.length) {
      var notes = el("div", { className: "v2-walk-notes" });
      notes.appendChild(el("div", { className: "v2-walk-notes-label",
        textContent: "notes:" }));
      wd.notes.forEach(function (n) {
        notes.appendChild(el("div", { className: "v2-walk-note", textContent: n }));
      });
      box.appendChild(notes);
    }

    // Events list (collapsible)
    var details = el("details", { className: "v2-walk-events" });
    details.appendChild(el("summary", {
      textContent: (resp.events || []).length + " pipeline event"
                   + ((resp.events || []).length === 1 ? "" : "s"),
    }));
    (resp.events || []).forEach(function (ev) {
      var row = el("div", { className: "v2-walk-event-row" });
      row.appendChild(el("span", { className: "v2-walk-event-stage",
        textContent: ev.stage }));
      row.appendChild(el("pre", { className: "v2-walk-event-data",
        textContent: JSON.stringify(ev.data, null, 2) }));
      details.appendChild(row);
    });
    box.appendChild(details);

    return box;
  }

  // =================================================================
  // Public exports — mainly for hand-testing in the dev console.
  // =================================================================

  window.AedosV2 = {
    renderDecisionConfidence: renderDecisionConfidence,
    renderChain: renderChain,
    loadOracleList: loadOracleList,
    submitWalk: submitWalk,
  };
})();
