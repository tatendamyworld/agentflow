/* AgentFlow Visual — graph.js: SVG node network (vanilla, offline) */
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var AGENTS = [
    { id: "supervisor", name: "SUPERVISOR", order: 1 },
    { id: "generation", name: "GENERATION", order: 2 },
    { id: "proximity", name: "PROXIMITY", order: 3 },
    { id: "reflection", name: "REFLECTION", order: 4 },
    { id: "evolution", name: "EVOLUTION", order: 5 },
    { id: "ranking", name: "RANKING", order: 6 },
    { id: "crow", name: "CROW", order: 7 },
    { id: "falcon", name: "FALCON", order: 8 },
    { id: "finch", name: "FINCH", order: 9 }
  ];
  var COLORS = { pending: "#64748b", ready: "#38bdf8", waiting: "#fbbf24", completed: "#34d399", failed: "#f87171", blocked: "#fb923c", archived: "#475569", selected: "#a78bfa" };

  function el(tag, attrs, parent, text) {
    var n = document.createElementNS(NS, tag);
    if (attrs) for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  function normalizeStatus(raw, isNextPending) {
    var s = String(raw || "PENDING").toUpperCase();
    if (s === "COMPLETED" || s === "DONE" || s === "SUCCESS" || s === "CONSENSUS_REACHED") return "completed";
    if (s === "FAILED" || s === "ERROR" || s === "INVALID") return "failed";
    if (s === "BLOCKED" || s === "SPLIT") return "blocked";
    if (s === "ARCHIVED") return "archived";
    if (s === "WAITING" || s === "WAITING_FOR_INPUT" || s === "WAITING_FOR_TOP_HYPOTHESIS" || s === "WAITING_FOR_FINCH") return "waiting";
    if (s === "READY" || s === "RUNNING" || s === "IN_PROGRESS" || s === "CREATED") return "ready";
    if (s === "PENDING" || s === "QUEUED") return isNextPending ? "ready" : "pending";
    return "pending";
  }

  function layout(width, height) {
    var pad = 80, y = height / 2 - 10, n = AGENTS.length;
    var xs = [];
    for (var i = 0; i < n; i++) xs.push(pad + (width - pad * 2) * (i / (n - 1)));
    return { xs: xs, y: y };
  }

  function render(svg, executions, selectedId, onSelect) {
    if (!svg) return;
    var W = 1240, H = 300, pos = layout(W, H);
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    var byId = {};
    (executions || []).forEach(function (e) { if (e && e.agent) byId[e.agent] = e; });
    var nextId = null;
    for (var i = 0; i < AGENTS.length; i++) {
      var e0 = byId[AGENTS[i].id];
      var st0 = e0 ? String(e0.status || "PENDING").toUpperCase() : "PENDING";
      if (st0 === "PENDING" || st0 === "FAILED" || st0 === "BLOCKED" || st0 === "READY" || st0 === "WAITING_FOR_INPUT" || st0 === "WAITING") { nextId = AGENTS[i].id; break; }
    }

    /* edges first (under nodes) */
    for (var j = 0; j < AGENTS.length - 1; j++) {
      var a = byId[AGENTS[j].id], b = byId[AGENTS[j + 1].id];
      var done = a && String(a.status).toUpperCase() === "COMPLETED" && b && String(b.status).toUpperCase() === "COMPLETED";
      el("line", { x1: pos.xs[j], y1: pos.y, x2: pos.xs[j + 1], y2: pos.y, "class": "edge" + (done ? " is-done" : "") }, svg);
      var midX = (pos.xs[j] + pos.xs[j + 1]) / 2;
      el("circle", { cx: midX, cy: pos.y, r: 3, fill: done ? COLORS.completed : "#334155" }, svg);
    }

    AGENTS.forEach(function (agent, idx) {
      var exe = byId[agent.id] || {};
      var status = normalizeStatus(exe.status, agent.id === nextId);
      var isSel = selectedId === agent.id;
      var color = isSel ? COLORS.selected : COLORS[status];
      var g = el("g", { "class": "node" + (isSel ? " is-selected" : ""), tabindex: "0", role: "button",
        "aria-label": agent.name + " agent, status " + status + (isSel ? ", selected" : "") }, svg);
      g.dataset.agent = agent.id;
      var x = pos.xs[idx], y = pos.y;
      /* outer ring + inner fill */
      el("circle", { "class": "node-ring", cx: x, cy: y, r: 30, fill: "#0f172a", stroke: color, "stroke-width": isSel ? 4 : 2.5 }, g);
      el("circle", { cx: x, cy: y, r: 8, fill: color }, g);
      var order = el("text", { "class": "node-order", x: x, y: y - 44 }, g, String(agent.order));
      order.setAttribute("text-anchor", "middle");
      el("text", { x: x, y: y + 52 }, g, agent.name);
      el("text", { "class": "node-sub", x: x, y: y + 68 }, g, status);
      g.addEventListener("click", function () { if (onSelect) onSelect(agent.id); });
      g.addEventListener("keydown", function (ev) {
        if ((ev.key === "Enter" || ev.key === " ") && onSelect) { ev.preventDefault(); onSelect(agent.id); }
      });
    });
  }

  window.AgentFlow = window.AgentFlow || {};
  window.AgentFlow.graph = { AGENTS: AGENTS, COLORS: COLORS, render: render, normalizeStatus: normalizeStatus };
})();
