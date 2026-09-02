/* Smart Table Analyzer UI — plain vanilla JavaScript (no framework).
 *
 * The server is the source of truth. This file:
 *   - submits exactly one table name,
 *   - streams progress via EventSource (SSE) with automatic reconnect,
 *   - renders the activity feed, stored results and the final report.
 * Only observable actions and stored evidence are displayed; the UI never
 * interprets results and never shows model reasoning.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var TERMINAL_EVENTS = {
    run_completed: "completed",
    run_failed: "failed",
    run_cancelled: "cancelled"
  };
  var TIMELINE_STEPS = ["table", "context", "investigation", "report"];

  var state = {
    runId: null,
    status: null,
    terminal: false,
    eventSource: null
  };

  /* ------------------------------------------------------------------ */
  /* helpers                                                             */
  /* ------------------------------------------------------------------ */

  function formatDuration(durationMs) {
    if (durationMs === null || durationMs === undefined) return "";
    return (durationMs / 1000).toFixed(durationMs < 1000 ? 2 : 1) + "s";
  }

  function clockOf(timestamp) {
    var date = new Date(timestamp);
    return isNaN(date.getTime()) ? "" : date.toLocaleTimeString();
  }

  function paramsText(parameters) {
    if (!parameters) return "";
    var parts = Object.keys(parameters).map(function (key) {
      return key + "=" + JSON.stringify(parameters[key]);
    });
    return parts.length ? " (" + parts.join(", ") + ")" : "";
  }

  async function requestJson(url, options) {
    var response = await fetch(url, options);
    if (!response.ok) {
      var detail = "";
      try { detail = (await response.json()).detail || ""; } catch (err) { /* keep status */ }
      throw new Error(detail || "request failed (" + response.status + ")");
    }
    return response.json();
  }

  /* ------------------------------------------------------------------ */
  /* environment badge                                                   */
  /* ------------------------------------------------------------------ */

  async function loadEnvironment() {
    try {
      var env = await requestJson("/api/environment");
      $("env-badge").textContent = env.badge;
      $("env-badge").dataset.environment = env.environment;
    } catch (err) { /* badge keeps its placeholder */ }
  }

  /* ------------------------------------------------------------------ */
  /* run submission (exactly one table name — nothing else is sent)      */
  /* ------------------------------------------------------------------ */

  function setFormError(message) {
    var el = $("form-error");
    el.hidden = !message;
    el.textContent = message || "";
  }

  function setRunning(running) {
    $("analyze-btn").disabled = running;
    $("cancel-btn").hidden = !running;
    $("table-name").disabled = running;
  }

  async function submitRun(event) {
    event.preventDefault();
    setFormError("");
    var tableName = $("table-name").value.trim();
    try {
      var run = await requestJson("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ table_name: tableName })
      });
      openRun(run.run_id);
    } catch (err) {
      setFormError(err.message || "Could not start the analysis.");
    }
  }

  function openRun(runId) {
    if (state.eventSource) state.eventSource.close();
    state.runId = runId;
    state.status = null;
    state.terminal = false;
    window.location.hash = "#/runs/" + encodeURIComponent(runId);
    $("run-panel").hidden = false;
    $("report-panel").hidden = true;
    $("report").innerHTML = "";
    $("activity").innerHTML = "";
    $("run-table").textContent = $("table-name").value.trim();
    $("run-snapshot").textContent = "";
    $("run-phase").textContent = "";
    setRunStatus("starting");
    resetTimeline();
    setRunning(true);
    connectEvents();
  }

  async function cancelRun() {
    if (!state.runId) return;
    $("cancel-btn").disabled = true;
    try {
      await requestJson("/api/runs/" + encodeURIComponent(state.runId) + "/cancel",
        { method: "POST" });
    } catch (err) { /* the stream reports the final state */ }
    $("cancel-btn").disabled = false;
  }

  /* ------------------------------------------------------------------ */
  /* SSE progress stream (auto-reconnect replays via Last-Event-ID)      */
  /* ------------------------------------------------------------------ */

  function connectEvents() {
    if (state.eventSource) state.eventSource.close();
    var url = "/api/runs/" + encodeURIComponent(state.runId) + "/events";
    var source = new EventSource(url);
    state.eventSource = source;
    source.onmessage = function (message) {
      if (!message.data) return;
      var envelope;
      try { envelope = JSON.parse(message.data); } catch (err) { return; }
      handleEvent(envelope);
    };
    source.onerror = function () {
      /* EventSource reconnects automatically; once terminal the server ends
       * the stream and we close for good. */
      if (state.terminal && state.eventSource === source) source.close();
    };
  }

  function handleEvent(envelope) {
    renderActivity(envelope);
    var data = envelope.data || {};
    if (envelope.type === "table_resolved") {
      setStep("table", "done");
      if (data.snapshot_id) $("run-snapshot").textContent = "snapshot " + data.snapshot_id;
    } else if (envelope.type === "table_context_ready") {
      setStep("context", "done");
      setStep("investigation", "active");
      setRunStatus("running");
      setPhase("investigating");
    } else if (envelope.type === "report_started") {
      setStep("investigation", "done");
      setStep("report", "active");
      setPhase("generating_report");
    } else if (envelope.type === "report_ready") {
      setStep("report", "done");
      loadReport();
    } else if (TERMINAL_EVENTS[envelope.type]) {
      state.terminal = true;
      state.status = TERMINAL_EVENTS[envelope.type];
      setRunStatus(state.status);
      setPhase("");
      setRunning(false);
      if (state.status === "failed") markFailedStep();
      if (state.eventSource) state.eventSource.close();
      if (state.status === "completed") loadReport();
      refreshRunSummary();
    }
  }

  /* ------------------------------------------------------------------ */
  /* activity feed                                                       */
  /* ------------------------------------------------------------------ */

  var ACTIVITY_TEXT = {
    run_started: function () { return "Run started"; },
    table_resolving: function (d) { return "Resolving table " + d.table; },
    table_resolved: function (d) {
      return "Table resolved" + (d.snapshot_id ? " · snapshot " + d.snapshot_id : "");
    },
    snapshot_pinned: function (d) { return "Snapshot pinned · " + d.snapshot_id; },
    table_context_started: function () { return "Building TableContext"; },
    table_context_ready: function (d) {
      return "TableContext ready · " + d.column_count + " columns · full schema " + d.full_schema_ref;
    },
    investigator_started: function () { return "Investigator started"; },
    tool_requested: function (d) { return "Tool requested · " + d.tool + paramsText(d.parameters); },
    query_started: function (d) { return "Query running · " + d.tool; },
    result_stored: function (d) {
      return "Result stored · " + d.result_id + " · " + d.row_count + " row(s)" +
        (d.duration_ms !== null && d.duration_ms !== undefined ? " · " + formatDuration(d.duration_ms) : "");
    },
    tool_failed: function (d) { return "✕ " + d.tool + " failed · " + d.error_class; },
    knowledge_search_completed: function (d) { return "Knowledge search · “" + d.query + "”"; },
    knowledge_read: function (d) { return "Knowledge read · " + d.path; },
    report_started: function () { return "Generating report"; },
    report_ready: function (d) { return "Report ready · " + d.overall_status; },
    run_completed: function () { return "Run completed"; },
    run_failed: function (d) { return "Run failed · " + (d.message || d.error_class); },
    run_cancelled: function () { return "Run cancelled"; }
  };

  function renderActivity(envelope) {
    var render = ACTIVITY_TEXT[envelope.type];
    if (!render) return;
    var item = document.createElement("li");
    if (envelope.type === "tool_failed" || envelope.type === "run_failed") {
      item.className = "status-failed";
    }
    var time = document.createElement("time");
    time.textContent = clockOf(envelope.timestamp);
    var what = document.createElement("span");
    what.className = "what";
    what.textContent = render(envelope.data || {});
    item.appendChild(time);
    item.appendChild(what);
    if (envelope.type === "result_stored") {
      item.appendChild(resultLink(envelope.data.result_id));
    }
    $("activity").appendChild(item);
  }

  function resultLink(resultId) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "link";
    button.textContent = "View " + resultId;
    button.dataset.resultId = resultId;
    button.addEventListener("click", onEvidenceClick);
    return button;
  }

  function onEvidenceClick(event) {
    showResult(event.target.dataset.resultId);
  }

  /* ------------------------------------------------------------------ */
  /* run summary / timeline                                              */
  /* ------------------------------------------------------------------ */

  function setRunStatus(status) {
    state.status = status;
    var el = $("run-status");
    el.textContent = status;
    el.className = "status " + status;
  }

  function setPhase(phase) {
    $("run-phase").textContent = phase ? "phase: " + phase : "";
  }

  function setStep(step, stepState) {
    var el = document.querySelector('#timeline li[data-step="' + step + '"]');
    if (el) el.className = stepState;
  }

  function resetTimeline() {
    TIMELINE_STEPS.forEach(function (step) { setStep(step, ""); });
  }

  function markFailedStep() {
    TIMELINE_STEPS.forEach(function (step) {
      var el = document.querySelector('#timeline li[data-step="' + step + '"]');
      if (el && el.className === "active") el.className = "failed";
    });
  }

  async function refreshRunSummary() {
    try {
      var run = await requestJson("/api/runs/" + encodeURIComponent(state.runId));
      $("run-table").textContent = run.table;
      $("run-snapshot").textContent = run.snapshot_id ? "snapshot " + run.snapshot_id : "";
    } catch (err) { /* the status line already reflects the failure */ }
  }

  /* ------------------------------------------------------------------ */
  /* report rendering                                                    */
  /* ------------------------------------------------------------------ */

  function chip(value) {
    var span = document.createElement("span");
    span.className = "chip " + value;
    span.textContent = String(value).replace(/_/g, " ");
    return span;
  }

  function heading(parent, tag, text) {
    var el = document.createElement(tag);
    el.textContent = text;
    parent.appendChild(el);
  }

  function evidenceRow(refs, knowledge) {
    if ((!refs || !refs.length) && (!knowledge || !knowledge.length)) return null;
    var wrap = document.createElement("div");
    wrap.className = "evidence";
    (refs || []).forEach(function (ref) {
      if (/^R\d+$/.test(ref)) {
        wrap.appendChild(resultLink(ref));
      } else {
        var code = document.createElement("code");
        code.textContent = ref;
        wrap.appendChild(code);
      }
    });
    (knowledge || []).forEach(function (path) {
      var code = document.createElement("code");
      code.textContent = path;
      wrap.appendChild(code);
    });
    return wrap;
  }

  function renderReport(report) {
    var root = $("report");
    root.innerHTML = "";

    var status = document.createElement("div");
    status.className = "report-status";
    status.appendChild(chip(report.overall_status));
    root.appendChild(status);

    if (report.current_issues && report.current_issues.length) {
      heading(root, "h3", "Current issues");
      report.current_issues.forEach(function (finding) {
        var card = document.createElement("div");
        card.className = "finding";
        var header = document.createElement("header");
        header.appendChild(chip(finding.severity));
        header.appendChild(chip(finding.confidence));
        var title = document.createElement("strong");
        title.textContent = finding.finding;
        header.appendChild(title);
        card.appendChild(header);
        var explanation = document.createElement("p");
        explanation.textContent = finding.explanation;
        card.appendChild(explanation);
        var evidence = evidenceRow(finding.evidence, finding.knowledge);
        if (evidence) card.appendChild(evidence);
        root.appendChild(card);
      });
    }

    if (report.immediate_remediation && report.immediate_remediation.length) {
      heading(root, "h3", "Immediate remediation");
      report.immediate_remediation.forEach(function (action) {
        var card = document.createElement("div");
        card.className = "remediation";
        var title = document.createElement("strong");
        title.textContent = action.action;
        card.appendChild(title);
        var reason = document.createElement("p");
        reason.textContent = action.reason;
        card.appendChild(reason);
        var evidence = evidenceRow(action.evidence, action.knowledge);
        if (evidence) card.appendChild(evidence);
        root.appendChild(card);
      });
    }

    var design = report.future_table_design || {};
    heading(root, "h3", "Future table design");
    [["partition_spec", "Partition spec"], ["sort_order", "Sort order"]]
      .forEach(function (pair) {
        var rec = design[pair[0]];
        if (!rec) return;
        var card = document.createElement("div");
        card.className = "recommendation";
        var header = document.createElement("header");
        header.appendChild(chip(rec.status));
        header.appendChild(chip(rec.confidence));
        var title = document.createElement("strong");
        title.textContent = pair[1] + ": " + rec.current +
          (rec.recommendation ? " → " + rec.recommendation : "");
        header.appendChild(title);
        card.appendChild(header);
        var reasoning = document.createElement("p");
        reasoning.textContent = rec.reasoning;
        card.appendChild(reasoning);
        var evidence = evidenceRow(rec.evidence, rec.knowledge);
        if (evidence) card.appendChild(evidence);
        root.appendChild(card);
      });
    (design.table_properties || []).forEach(function (prop) {
      var card = document.createElement("div");
      card.className = "recommendation";
      var title = document.createElement("strong");
      title.textContent = prop.property + ": " + prop.recommendation;
      card.appendChild(title);
      var reasoning = document.createElement("p");
      reasoning.textContent = prop.reasoning;
      card.appendChild(reasoning);
      var evidence = evidenceRow(prop.evidence, prop.knowledge);
      if (evidence) card.appendChild(evidence);
      root.appendChild(card);
    });

    if (report.no_change_decisions && report.no_change_decisions.length) {
      heading(root, "h3", "No-change decisions");
      root.appendChild(listOf(report.no_change_decisions));
    }

    if (report.limitations && report.limitations.length) {
      heading(root, "h3", "Limitations");
      root.appendChild(listOf(report.limitations));
    }

    $("report-panel").hidden = false;
  }

  function listOf(entries) {
    var list = document.createElement("ul");
    entries.forEach(function (entry) {
      var item = document.createElement("li");
      item.textContent = entry;
      list.appendChild(item);
    });
    return list;
  }

  async function loadReport() {
    try {
      var report = await requestJson(
        "/api/runs/" + encodeURIComponent(state.runId) + "/report");
      renderReport(report);
    } catch (err) { /* report_ready may still be in flight; retry on next event */ }
  }

  /* ------------------------------------------------------------------ */
  /* result detail dialog (audit view; no diagnosis)                     */
  /* ------------------------------------------------------------------ */

  function showResult(resultId) {
    $("result-dialog").hidden = false;
    $("result-detail").innerHTML = "<p class='muted'>Loading…</p>";
    requestJson("/api/runs/" + encodeURIComponent(state.runId) +
      "/results/" + encodeURIComponent(resultId))
      .then(renderResult)
      .catch(function (err) {
        $("result-detail").innerHTML = "";
        var message = document.createElement("p");
        message.className = "error";
        message.textContent = err.message;
        $("result-detail").appendChild(message);
      });
  }

  function kvList(definitionList, pairs) {
    pairs.forEach(function (pair) {
      if (pair[1] === null || pair[1] === undefined || pair[1] === "") return;
      var dt = document.createElement("dt");
      dt.textContent = pair[0];
      var dd = document.createElement("dd");
      dd.textContent = typeof pair[1] === "object" ? JSON.stringify(pair[1]) : String(pair[1]);
      definitionList.appendChild(dt);
      definitionList.appendChild(dd);
    });
  }

  function renderResult(result) {
    $("result-title").textContent = result.result_id + " — " + result.tool_name;
    var detail = $("result-detail");
    detail.innerHTML = "";

    var dl = document.createElement("dl");
    dl.className = "kv";
    kvList(dl, [
      ["Tool", result.tool_name],
      ["Snapshot", result.snapshot_id],
      ["Query version", result.query_version],
      ["Parameters", Object.keys(result.parameters || {}).length ? result.parameters : null],
      ["Rows", result.row_count],
      ["Duration", result.duration_ms !== null && result.duration_ms !== undefined
        ? formatDuration(result.duration_ms) : null],
      ["Executed at", result.executed_at]
    ]);
    detail.appendChild(dl);

    if (result.schema && Object.keys(result.schema).length) {
      var columns = document.createElement("p");
      columns.className = "muted";
      columns.textContent = "Columns: " + Object.keys(result.schema).join(", ");
      detail.appendChild(columns);
    }

    var rowsField = findRowsField(result.payload);
    if (rowsField) {
      detail.appendChild(payloadTable(result.payload[rowsField]));
    } else if (result.payload && typeof result.payload === "object") {
      var pre = document.createElement("pre");
      pre.textContent = JSON.stringify(result.payload, null, 2);
      detail.appendChild(pre);
    }
  }

  function findRowsField(payload) {
    if (!payload || typeof payload !== "object") return null;
    var rowsField = null;
    Object.keys(payload).forEach(function (key) {
      var value = payload[key];
      if (Array.isArray(value) && value.length && typeof value[0] === "object") {
        rowsField = key;
      }
    });
    return rowsField;
  }

  function payloadTable(rows) {
    var columns = [];
    rows.slice(0, 50).forEach(function (row) {
      Object.keys(row).forEach(function (key) {
        if (columns.indexOf(key) === -1) columns.push(key);
      });
    });
    var scroll = document.createElement("div");
    scroll.className = "table-scroll";
    var table = document.createElement("table");
    table.className = "results";
    var head = document.createElement("tr");
    columns.forEach(function (column) {
      var th = document.createElement("th");
      th.textContent = column;
      head.appendChild(th);
    });
    table.appendChild(head);
    rows.forEach(function (row) {
      var tr = document.createElement("tr");
      columns.forEach(function (column) {
        var td = document.createElement("td");
        var value = row[column];
        td.textContent = value === null || value === undefined ? "" : String(value);
        tr.appendChild(td);
      });
      table.appendChild(tr);
    });
    scroll.appendChild(table);
    return scroll;
  }

  function closeResultDialog() {
    $("result-dialog").hidden = true;
  }

  /* ------------------------------------------------------------------ */
  /* reopen a run after refresh (Runtime_Environments_UI.md #59)         */
  /* ------------------------------------------------------------------ */

  async function reopenRun(runId) {
    state.runId = runId;
    state.terminal = false;
    $("table-name").disabled = true;
    try {
      var run = await requestJson("/api/runs/" + encodeURIComponent(runId));
      $("table-name").value = run.table;
      $("run-table").textContent = run.table;
      $("run-snapshot").textContent = run.snapshot_id ? "snapshot " + run.snapshot_id : "";
      $("run-panel").hidden = false;
      setRunStatus(run.status);
      resetTimeline();
      var active = run.status === "queued" || run.status === "starting" || run.status === "running";
      setRunning(active);
      /* The server replays all persisted events on a fresh connection. */
      if (active || run.status === "completed" || run.status === "failed" || run.status === "cancelled") {
        connectEvents();
      }
      if (run.status === "completed") loadReport();
    } catch (err) {
      $("run-panel").hidden = false;
      $("activity").innerHTML = "";
      var item = document.createElement("li");
      item.className = "status-failed";
      item.textContent = err.message;
      $("activity").appendChild(item);
      setRunning(false);
      $("table-name").disabled = false;
    }
  }

  /* ------------------------------------------------------------------ */
  /* wiring                                                              */
  /* ------------------------------------------------------------------ */

  function init() {
    $("run-form").addEventListener("submit", submitRun);
    $("cancel-btn").addEventListener("click", cancelRun);
    $("result-close").addEventListener("click", function () {
      $("result-dialog").hidden = true;
    });
    $("result-dialog").addEventListener("click", function (event) {
      if (event.target === $("result-dialog")) $("result-dialog").hidden = true;
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") $("result-dialog").hidden = true;
    });
    loadEnvironment();

    var match = window.location.hash.match(/^#\/runs\/([A-Za-z0-9_-]+)/);
    if (match) reopenRun(match[1]);
  }

  init();
})();