/* AgentFlow Visual — app.js: API client, state, polling, new mission (vanilla, offline) */
(function () {
  "use strict";
  function $(id) { return document.getElementById(id); }
  var API_BASE = window.AGENTFLOW_API_BASE || "";
  var POLL_MS = 5000;

  var state = {
    runId: null, run: null, executions: [], finchRuns: [], consensus: null,
    hypotheses: [], ranking: [], rankingDashboard: null, eloVisual: [], rankingChanges: [], comparative: [], winning: null, rankingHistory: null, crowWs: null, falconWs: null, runs: [], selectedAgent: "supervisor", online: false
  };

  /* ---------- fetch helpers ---------- */
  function req(path, opts) {
    opts = opts || {};
    return fetch(API_BASE + path, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (res) {
      if (!res.ok) return res.text().then(function (t) { throw new Error(res.status + " " + res.statusText + (t ? ": " + t.slice(0, 200) : "")); });
      var ct = res.headers.get("content-type") || "";
      if (ct.indexOf("json") !== -1) return res.json();
      return res.text().then(function (t) { try { return JSON.parse(t); } catch (e) { return { text: t }; } });
    });
  }
  function opt(path) { return req(path).catch(function () { return null; }); }

  function pickRun(d) { return (d && (d.run || d)) || null; }
  function pickExecs(d) {
    if (!d) return [];
    if (Array.isArray(d.executions)) return d.executions;
    if (d.run && Array.isArray(d.run.executions)) return d.run.executions;
    return [];
  }
  function pickFinch(d) {
    if (!d) return [];
    if (Array.isArray(d.finch_runs)) return d.finch_runs;
    if (Array.isArray(d.finchRuns)) return d.finchRuns;
    if (Array.isArray(d.finch)) return d.finch;
    return [];
  }
  function pickConsensus(d) {
    if (!d) return null;
    var c = d.consensus || d.consensus_result;
    if (c && typeof c === "object") {
      if (c.yes != null) return c;
      var det = c.detail || c.result;
      if (det && typeof det === "object") return det;
    }
    return c || null;
  }
  function pickRuns(d) {
    if (Array.isArray(d)) return d;
    if (d && Array.isArray(d.runs)) return d.runs;
    return [];
  }

  var api = {
    listIterations: function (id) { return req("/api/runs/" + encodeURIComponent(id) + "/iterations"); },
    switchIteration: function (id, it) { return req("/api/runs/" + encodeURIComponent(id) + "/iterations/switch", { method: "POST", body: { iteration: it } }); },
    compareIterations: function (id, a, b) { return req("/api/runs/" + encodeURIComponent(id) + "/iterations/compare?a=" + encodeURIComponent(a) + "&b=" + encodeURIComponent(b)); },
    loop: function (id) { return req("/api/runs/" + encodeURIComponent(id) + "/loop", { method: "POST", body: {} }); },
    diff: function (a, b) { return req("/api/runs/" + encodeURIComponent(a) + "/diff/" + encodeURIComponent(b)); },
    researchHistory: function (id) { return req("/api/runs/" + encodeURIComponent(id) + "/research-history"); },
    listRuns: function () { return req("/api/runs").then(pickRuns); },
    createRun: function (goal) {
      return req("/api/runs", { method: "POST", body: { goal: goal } }).then(function (d) {
        var r = pickRun(d);
        return (r && (r.id || r.run_id)) || (d && (d.id || d.run_id)) || null;
      });
    },
    getRun: function (id) { return req("/api/runs/" + encodeURIComponent(id)); },
    generate: function (id, agent) {
      /* backend may expect {agent} or {agent_id}; try {agent} first, fall back */
      return req("/api/runs/" + encodeURIComponent(id) + "/generate", { method: "POST", body: { agent: agent } })
        .catch(function (e1) { return req("/api/runs/" + encodeURIComponent(id) + "/generate", { method: "POST", body: { agent_id: agent } }); });
    },
    save: function (id, payload) { return req("/api/runs/" + encodeURIComponent(id) + "/save", { method: "POST", body: payload }); },
    submit: function (id, payload) { return req("/api/runs/" + encodeURIComponent(id) + "/submit", { method: "POST", body: payload }); },
    preview: function (id, agent) { return req("/api/runs/" + encodeURIComponent(id) + "/prompt-preview", { method: "POST", body: { agent: agent } }); },
    copyPrompt: function (id, agent) { return req("/api/runs/" + encodeURIComponent(id) + "/copy/" + encodeURIComponent(agent)); },
    retry: function (id, agent) { return req("/api/runs/" + encodeURIComponent(id) + "/retry", { method: "POST", body: { agent: agent } }); },
    agentHistory: function (id, agent) { return req("/api/runs/" + encodeURIComponent(id) + "/agent/" + encodeURIComponent(agent) + "/history"); },
    runHistory: function (id) { return req("/api/runs/" + encodeURIComponent(id) + "/history"); },
    saveTop: function (id, text) { return req("/api/runs/" + encodeURIComponent(id) + "/save-top", { method: "POST", body: { top_hypothesis: text } }).catch(function () { return req("/api/runs/" + encodeURIComponent(id) + "/save", { method: "POST", body: { agent: "ranking", top_hypothesis: text, output: text } }); }); },
    finch: function (id, responses) { return req("/api/runs/" + encodeURIComponent(id) + "/finch", { method: "POST", body: { responses: responses } }); },
    saveFinchRun: function (id, n, text) { return req("/api/runs/" + encodeURIComponent(id) + "/finch/run/" + encodeURIComponent(n), { method: "PUT", body: { response: text } }); },
    recalcFinch: function (id) { return req("/api/runs/" + encodeURIComponent(id) + "/finch/recalc", { method: "POST", body: {} }); },
    finchHistory: function (id) { return req("/api/runs/" + encodeURIComponent(id) + "/finch/history"); },
    reorder: function (id, order) { return req("/api/runs/" + encodeURIComponent(id) + "/ranking/reorder", { method: "POST", body: { order: order } }); },
    rankingHistory: function (id) { return req("/api/runs/" + encodeURIComponent(id) + "/ranking/history"); },
    getWorkspace: function (id, ws) { return req("/api/runs/" + encodeURIComponent(id) + "/workspace/" + encodeURIComponent(ws)); },
    parseWorkspace: function (id, ws, output) { return req("/api/runs/" + encodeURIComponent(id) + "/workspace/" + encodeURIComponent(ws) + "/parse", { method: "POST", body: { output: output || "" } }); },
    patchWorkspace: function (id, ws, sections) { return req("/api/runs/" + encodeURIComponent(id) + "/workspace/" + encodeURIComponent(ws), { method: "PATCH", body: { sections: sections } }); },
    generateWorkspace: function (id, ws) { return req("/api/runs/" + encodeURIComponent(id) + "/workspace/" + encodeURIComponent(ws) + "/generate", { method: "POST", body: {} }); },
    createHyp: function (id, payload) { return req("/api/runs/" + encodeURIComponent(id) + "/hypotheses", { method: "POST", body: payload }); },
    updateHyp: function (id, hypId, payload) { return req("/api/runs/" + encodeURIComponent(id) + "/hypotheses/" + encodeURIComponent(hypId), { method: "PATCH", body: payload }); },
    importHyps: function (id, payload) { return req("/api/runs/" + encodeURIComponent(id) + "/hypotheses/import", { method: "POST", body: payload || {} }); },
    compareHyps: function (id, a, b, winner) { return req("/api/runs/" + encodeURIComponent(id) + "/hypotheses/compare", { method: "POST", body: { a: a, b: b, winner: winner } }); },
    selectHyp: function (id, hypId) { return req("/api/runs/" + encodeURIComponent(id) + "/hypotheses/" + encodeURIComponent(hypId) + "/select", { method: "POST", body: {} }); },
    archiveHyp: function (id, hypId) { return req("/api/runs/" + encodeURIComponent(id) + "/hypotheses/" + encodeURIComponent(hypId) + "/archive", { method: "POST", body: {} }); },
    restoreHyp: function (id, hypId) { return req("/api/runs/" + encodeURIComponent(id) + "/hypotheses/" + encodeURIComponent(hypId) + "/restore", { method: "POST", body: {} }); },
    archive: function (id) { return req("/api/runs/" + encodeURIComponent(id) + "/archive", { method: "POST", body: {} }); },
    runStatus: function (id) { return req("/api/runs/" + encodeURIComponent(id) + "/status"); },
  };

  /* ---------- error / poll UI ---------- */
  function showError(message) {
    var box = $("api-error");
    if (message) { $("api-error-msg").textContent = message; box.hidden = false; }
    else box.hidden = true;
    var pill = $("nav-api");
    pill.textContent = message ? "API offline" : "API online";
    pill.className = "pill pill-api " + (message ? "bad" : "ok");
  }
  function markPoll() { $("poll-info").textContent = "Last poll: " + new Date().toLocaleTimeString(); }

  /* ---------- data load ---------- */
  function applyDetail(d) {
    var run = pickRun(d);
    if (run) { state.run = run; state.runId = run.id || run.run_id || state.runId; }
    var exes = pickExecs(d);
    if (exes.length) state.executions = exes;
    var fr = pickFinch(d);
    if (fr.length) state.finchRuns = fr;
    if (d && d.runs) state.finchRuns = d.runs;
    if (d && d.totals) state.finchTotals = d.totals;
    if (d && d.percentages) state.finchPct = d.percentages;
    if (d && d.valid_count != null) state.finchValid = d.valid_count;
    var cons = pickConsensus(d);
    if (cons) state.consensus = cons;
    if (d && Array.isArray(d.hypotheses)) state.hypotheses = d.hypotheses;
    if (d && Array.isArray(d.ranking)) state.ranking = d.ranking;
    if (d && Array.isArray(d.runs)) state.runs = d.runs;
    if (d && typeof d === "object" && (d.elo_visual || d.ranking_changes || d.winning_hypothesis || d.ranking_history)) {
      state.rankingDashboard = d;
      state.eloVisual = d.elo_visual || [];
      state.rankingChanges = d.ranking_changes || [];
      state.comparative = d.comparative_results || [];
      state.winning = d.winning_hypothesis || null;
      state.rankingHistory = d.ranking_history || null;
    }
  }

  function loadOptional(id) {
    return Promise.all([
      opt("/api/runs/" + encodeURIComponent(id) + "/hypotheses"),
      opt("/api/runs/" + encodeURIComponent(id) + "/ranking"),
      opt("/api/runs/" + encodeURIComponent(id) + "/workspace/crow"),
      opt("/api/runs/" + encodeURIComponent(id) + "/workspace/falcon")
    ]).then(function (arr) { var pair = [arr[0], arr[1]];
      if (arr[2]) state.crowWs = arr[2];
      if (arr[3]) state.falconWs = arr[3];
      if (pair[0]) state.hypotheses = Array.isArray(pair[0].hypotheses) ? pair[0].hypotheses : pair[0];
      if (pair[1]) { state.ranking = Array.isArray(pair[1].ranking) ? pair[1].ranking : pair[1];
        state.rankingDashboard = pair[1];
        state.eloVisual = pair[1].elo_visual || [];
        state.rankingChanges = pair[1].ranking_changes || [];
        state.comparative = pair[1].comparative_results || [];
        state.winning = pair[1].winning_hypothesis || null;
        state.rankingHistory = pair[1].ranking_history || null; }
    });
  }

  function selectRun(id) {
    state.runId = id;
    return api.getRun(id).then(function (d) {
      applyDetail(d);
      return loadOptional(id);
    }).then(function () { render(); }).catch(function (err) {
      showError("Could not load run " + id + ": " + err.message);
      render();
    });
  }

  function poll() {
    var jobs = [api.listRuns().then(function (runs) { state.runs = runs; }).catch(function (e) { throw e; })];
    if (state.runId) jobs.push(api.getRun(state.runId).then(function (d) { applyDetail(d); return loadOptional(state.runId); }));
    return Promise.all(jobs).then(function () {
      state.online = true; showError(null); markPoll(); render();
    }).catch(function (err) {
      state.online = false; showError(err.message); markPoll();
      if (!state.executions.length && !state.run) render();
    });
  }

  function refresh() { return poll(); }

  /* ---------- demo fallback (offline preview only, clearly labelled via error banner) ---------- */
  function seedDemo() {
    if (state.run || state.executions.length) return;
    state.run = { id: "AF-DEMO", goal: "Demo mission (backend offline)", status: "IN_PROGRESS", current_iteration: 1, created_at: "—", updated_at: "—" };
    state.runId = "AF-DEMO";
    var base = [
      ["supervisor", "COMPLETED", "Break the goal into testable hypotheses.", "You are Supervisor. Goal: {{GOAL}}"],
      ["generation", "COMPLETED", "1. Cached reads cut latency.\n2. Async writes improve throughput.", "Generate hypotheses for {{GOAL}}"],
      ["proximity", "WAITING", "", ""],
      ["reflection", "PENDING", "", ""], ["evolution", "PENDING", "", ""], ["ranking", "PENDING", "", ""],
      ["crow", "PENDING", "", ""], ["falcon", "PENDING", "", ""], ["finch", "PENDING", "", ""]
    ];
    state.executions = base.map(function (r, i) { return { id: i + 1, agent: r[0], status: r[1], output: r[2], prompt: r[3], updated_at: "" }; });
  }

  /* ---------- render ---------- */
  function render() {
    window.AgentFlow.graph.render($("agent-network"), state.executions, state.selectedAgent, function (id) {
      state.selectedAgent = id; render();
    });
    ctx.renderInspector && ctx.renderInspector();
    ctx.renderDashboard && ctx.renderDashboard();
  }

  var ctx = {
    state: state, api: api, refresh: refresh, showError: showError,
    normalize: window.AgentFlow.graph.normalizeStatus,
    selectRun: selectRun
  };

  /* ---------- new mission dialog ---------- */
  function initDialog() {
    var dlg = $("new-mission-dialog");
    $("new-mission-btn").addEventListener("click", function () {
      $("nm-msg").textContent = "";
      if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
    });
    $("nm-cancel").addEventListener("click", function () { dlg.close ? dlg.close() : dlg.removeAttribute("open"); });
    $("new-mission-form").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var goal = $("nm-goal").value.trim();
      if (!goal) return;
      $("nm-msg").textContent = "Creating…";
      api.createRun(goal).then(function (id) {
        if (!id) throw new Error("Backend did not return a run id.");
        state.selectedAgent = "supervisor";
        return selectRun(id);
      }).then(function () {
        if (dlg.close) dlg.close(); else dlg.removeAttribute("open");
        $("nm-goal").value = "";
      }).catch(function (err) { $("nm-msg").textContent = "Create failed: " + err.message; });
    });
    $("api-error-dismiss").addEventListener("click", function () { $("api-error").hidden = true; });
  }

  /* ---------- boot ---------- */
  function boot() {
    window.AgentFlow.agents.init(ctx);
    window.AgentFlow.dashboard.init(ctx);
    initDialog();
    poll().then(function () { return undefined; }).catch(function () { /* handled */ })
      .then(function () { seedDemo(); render(); });
    setInterval(function () { if (!document.hidden) poll(); }, POLL_MS);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
