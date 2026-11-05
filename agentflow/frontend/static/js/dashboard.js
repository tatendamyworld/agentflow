/* AgentFlow Visual — dashboard.js: overview, hypotheses, ranking, finch, timeline, history */
(function () {
  "use strict";
  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }

  /* Parse hypotheses out of free-text agent output (numbered / bullet lines). */
  function parseHypothesesFromText(text) {
    if (!text || !String(text).trim()) return [];
    var lines = String(text).split(/\r?\n/).map(function (l) { return l.trim(); }).filter(Boolean);
    var picked = [];
    lines.forEach(function (l) {
      var m = l.match(/^(?:\d+[.)]|[-*•])\s+(.{12,})$/);
      if (m) picked.push(m[1].trim());
    });
    if (picked.length >= 2) return picked.slice(0, 12);
    if (picked.length === 1 && lines.length <= 8) return picked;
    /* fallback: treat short whole output as one hypothesis */
    var whole = String(text).trim();
    if (whole.length > 0 && whole.length <= 600 && lines.length <= 6) return [whole];
    return picked;
  }

  function consensusOf(state) {
    var c = state.consensus;
    if (c && typeof c === "object") return c;
    return null;
  }

  function init(ctx) {
    var hypState = { selected: {}, compare: {}, notes: {}, edits: {}, survivorId: null, topId: null };

    /** ---------- overview + timeline ---------- */
    function renderOverview() {
      var run = ctx.state.run || {}, exes = ctx.state.executions || [];
      var counts = { completed: 0, ready: 0, waiting: 0, pending: 0, failed: 0, blocked: 0, archived: 0 };
      var nextId = null;
      for (var i = 0; i < exes.length; i++) {
        var s = String(exes[i].status || "PENDING").toUpperCase();
        if ((s === "PENDING" || s === "FAILED" || s === "BLOCKED" || s === "READY" || s === "WAITING_FOR_INPUT" || s === "WAITING") && !nextId) nextId = exes[i].agent;
      }
      exes.forEach(function (e) { var n = ctx.normalize(e.status, e.agent === nextId); if (counts[n] == null) counts[n] = 0; counts[n]++; });
      var total = Math.max(exes.length, 1), pct = Math.round((counts.completed / total) * 100);
      $("pct-complete").textContent = pct + "%";
      $("progress-fill").style.width = pct + "%";
      $("progress-bar").setAttribute("aria-valuenow", String(pct));
      $("stat-completed").textContent = counts.completed + " completed";
      $("stat-ready").textContent = counts.ready + " ready";
      $("stat-waiting").textContent = counts.waiting + " waiting";
      $("stat-pending").textContent = counts.pending + " pending";
      $("stat-failed").textContent = counts.failed + " failed" + ((counts.blocked || 0) ? " · " + counts.blocked + " blocked" : "");
      $("meta-goal").textContent = run.goal || "—";
      $("meta-status").textContent = run.derived_status || run.canonical_status || run.status || "—";
      var iter = run.current_iteration || run.iteration || 1;
      $("meta-iteration").textContent = String(iter);
      $("meta-created").textContent = run.created_at || "—";
      $("overview-updated").textContent = run.updated_at ? ("updated " + run.updated_at) : "";
      /* top nav */
      $("nav-mission").textContent = run.goal ? (run.goal.length > 60 ? run.goal.slice(0, 60) + "…" : run.goal) : "No mission selected";
      $("nav-run-id").textContent = ctx.state.runId || "";
      $("nav-status").textContent = run.derived_status || run.canonical_status || run.status || "—";
      $("nav-iteration").textContent = "Iter " + iter;
      /* timeline */
      var tl = $("timeline-list"), evs = (ctx.state.executions || []).slice().sort(function (a, b) {
        return String(a.updated_at || "").localeCompare(String(b.updated_at || ""));
      });
      if (!evs.length) { tl.innerHTML = '<li class="muted">No events yet.</li>'; return; }
      tl.innerHTML = evs.map(function (e) {
        var st = ctx.normalize(e.status, e.agent === nextId);
        return '<li class="ev-' + st + '"><strong>' + esc(e.agent) + "</strong> — " + esc(e.status || "Pending") +
          (e.updated_at ? "<time>" + esc(e.updated_at) + "</time>" : "") + "</li>";
      }).join("");
    }

    /** ---------- hypotheses ---------- */
    function collectHypotheses() {
      var nested = ctx.state.hypotheses;
      if (Array.isArray(nested) && nested.length) {
        return nested.map(function (h, i) {
          return { id: h.id || ("h" + i), text: h.text || h.hypothesis || h.output || JSON.stringify(h),
            score: h.score, source: h.agent || h.source || "api", survivor: !!h.survivor, top: !!h.top || !!h.is_top };
        });
      }
      var out = [], exes = ctx.state.executions || [];
      ["generation", "ranking", "evolution", "reflection"].forEach(function (agent) {
        exes.filter(function (e) { return e.agent === agent; }).forEach(function (e) {
          parseHypothesesFromText(e.output).forEach(function (t, i) {
            out.push({ id: agent + "-" + i, text: t, score: null, source: agent, survivor: false,
              top: e.top_hypothesis && e.top_hypothesis === t });
          });
          if (e.top_hypothesis && !out.some(function (h) { return h.text === e.top_hypothesis; })) {
            out.push({ id: agent + "-top", text: e.top_hypothesis, score: null, source: agent, survivor: false, top: true });
          }
        });
      });
      return out;
    }

    function renderHypotheses() {
      var list = collectHypotheses();
      var wrap = $("hyp-cards");
      $("hyp-count").textContent = list.length ? (list.length + " hypotheses") : "";
      $("hyp-empty").hidden = list.length > 0;
      wrap.innerHTML = list.map(function (h) {
        var id = esc(h.id);
        var text = hypState.edits[h.id] != null ? hypState.edits[h.id] : h.text;
        var isTop = h.top || hypState.topId === h.id, isSurv = h.survivor || hypState.survivorId === h.id;
        return '<article class="hyp-card' + (hypState.selected[h.id] ? " is-selected" : "") + (isSurv ? " is-survivor" : "") + (isTop ? " is-top" : "") + '" data-id="' + id + '">' +
          '<div class="hyp-card-head"><span class="hyp-card-title">Hypothesis ' + esc(h.id) + "</span>" +
          (isSurv ? '<span class="badge badge-survivor">Survivor</span>' : "") + (isTop ? '<span class="badge badge-top">Top</span>' : "") + "</div>" +
          '<p class="hyp-text">' + esc(text) + "</p>" +
          '<p class="muted small">Source: ' + esc(h.source || "—") + (h.score != null ? " · Score: " + esc(h.score) : "") + "</p>" +
          '<label><input type="checkbox" data-hyp-select="' + id + '"' + (hypState.selected[h.id] ? " checked" : "") + "> Select</label>" +
          '<label><input type="checkbox" data-hyp-compare="' + id + '"' + (hypState.compare[h.id] ? " checked" : "") + "> Compare</label>" +
          '<label for="note-' + id + '">Notes</label>' +
          '<textarea id="note-' + id + '" data-hyp-note="' + id + '" placeholder="Add a note…">' + esc(hypState.notes[h.id] || "") + "</textarea>" +
          '<div class="btn-row"><button type="button" class="btn btn-ghost btn-small" data-hyp-edit="' + id + '">Edit</button>' +
          '<button type="button" class="btn btn-ghost btn-small" data-hyp-survivor="' + id + '">Survivor</button>' +
          '<button type="button" class="btn btn-ghost btn-small" data-hyp-top="' + id + '">Mark top</button></div></article>';
      }).join("");
    }

    function renderHypCompare() {
      var list = collectHypotheses().filter(function (h) { return hypState.compare[h.id]; });
      var box = $("hyp-compare");
      if (list.length < 2) { box.hidden = true; box.innerHTML = ""; return; }
      box.hidden = false;
      box.innerHTML = "<h3>Comparison (" + list.length + ")</h3><table><thead><tr><th>Hypothesis</th><th>Text</th><th>Score</th><th>Source</th><th>Notes</th></tr></thead><tbody>" +
        list.map(function (h) {
          return "<tr><td>" + esc(h.id) + "</td><td>" + esc(hypState.edits[h.id] != null ? hypState.edits[h.id] : h.text) +
            "</td><td>" + esc(h.score != null ? h.score : "—") + "</td><td>" + esc(h.source || "—") + "</td><td>" +
            esc(hypState.notes[h.id] || "—") + "</td></tr>";
        }).join("") + "</tbody></table>";
    }

    var hypWrap = $("hyp-cards");
    if (hypWrap) {
      hypWrap.addEventListener("change", function (ev) {
        var t = ev.target;
        if (t.matches("[data-hyp-select]")) hypState.selected[t.dataset.hypSelect] = t.checked;
        if (t.matches("[data-hyp-compare]")) { hypState.compare[t.dataset.hypCompare] = t.checked; renderHypCompare(); }
        if (t.matches("[data-hyp-note]")) { hypState.notes[t.dataset.hypNote] = t.value; renderHypCompare(); }
      });
      hypWrap.addEventListener("click", function (ev) {
        var b = ev.target.closest("button"); if (!b) return;
        if (b.dataset.hypEdit) {
          var id = b.dataset.hypEdit, card = b.closest(".hyp-card"), txt = card.querySelector(".hyp-text");
          var cur = hypState.edits[id] != null ? hypState.edits[id] : (collectHypotheses().filter(function (h) { return h.id === id; })[0] || {}).text || "";
          var next = window.prompt("Edit hypothesis text:", cur);
          if (next != null) { hypState.edits[id] = next; renderHypotheses(); renderHypCompare(); }
        }
        if (b.dataset.hypSurvivor) { hypState.survivorId = b.dataset.hypSurvivor; renderHypotheses(); }
        if (b.dataset.hypTop) {
          hypState.topId = b.dataset.hypTop;
          var h = collectHypotheses().filter(function (x) { return x.id === hypState.topId; })[0];
          renderHypotheses();
          if (h && ctx.state.runId) {
            ctx.api.saveTop(ctx.state.runId, hypState.edits[h.id] != null ? hypState.edits[h.id] : h.text).catch(function () { /* optional endpoint */ });
          }
        }
      });
    }
    var cmpBtn = $("hyp-compare-btn");
    if (cmpBtn) cmpBtn.addEventListener("click", renderHypCompare);

    /** ---------- ranking table ---------- */
    function renderRanking() {
      var tb = $("ranking-table").querySelector("tbody");
      var rows = [];
      var nested = ctx.state.ranking;
      if (Array.isArray(nested) && nested.length) {
        rows = nested.map(function (r, i) { return { rank: r.rank || i + 1, text: r.text || r.hypothesis || "", score: r.score, source: r.agent || r.source || "", status: r.status || "" }; });
      } else {
        var ranked = collectHypotheses().slice().sort(function (a, b) {
          return ((b.score != null ? b.score : -Infinity) - (a.score != null ? a.score : -Infinity));
        });
        rows = ranked.map(function (h, i) { return { rank: i + 1, text: hypState.edits[h.id] != null ? hypState.edits[h.id] : h.text, score: h.score, source: h.source, status: h.top ? "TOP" : "" }; });
      }
      if (!rows.length) { tb.innerHTML = '<tr><td colspan="5" class="muted">No ranking data yet.</td></tr>'; $("rank-note").textContent = ""; return; }
      $("rank-note").textContent = rows.length + " ranked";
      tb.innerHTML = rows.map(function (r) {
        return "<tr><td>" + esc(r.rank) + "</td><td>" + esc(r.text) + "</td><td>" + esc(r.score != null ? r.score : "—") +
          "</td><td>" + esc(r.source || "—") + "</td><td>" + esc(r.status || "—") + "</td></tr>";
      }).join("");
    }

    /** ---------- crow / falcon + finch ---------- */
    function renderCrowFalcon() {
      var byId = {};
      (ctx.state.executions || []).forEach(function (e) { byId[e.agent] = e; });
      $("crow-output").textContent = (byId.crow && byId.crow.output) || "—";
      $("falcon-output").textContent = (byId.falcon && byId.falcon.output) || "—";
    }

    function renderFinch() {
      var runs = ctx.state.finchRuns || [], cons = consensusOf(ctx.state);
      $("finch-count").textContent = runs.length ? (runs.length + "/8 runs") : "0/8 runs";
      var banner = $("consensus-banner");
      if (cons && cons.outcome) {
        var isCons = String(cons.outcome).toUpperCase().indexOf("CONSENSUS") === 0;
        banner.className = "consensus-banner " + (isCons ? "banner-consensus" : "banner-split");
        banner.textContent = String(cons.outcome) + (cons.yes != null ? " — YES " + cons.yes + " · NO " + cons.no + " · INCONCLUSIVE " + cons.inconclusive : "");
      } else {
        banner.className = "consensus-banner banner-neutral";
        banner.textContent = runs.length ? "Finch votes in — awaiting consensus." : "No consensus yet.";
      }
      /* bar chart */
      var counts = cons ? { YES: cons.yes || 0, NO: cons.no || 0, INCONCLUSIVE: cons.inconclusive || 0 }
        : runs.reduce(function (a, r) { var v = String(r.parsed_vote || r.vote || "INVALID").toUpperCase(); if (a[v] != null) a[v]++; return a; }, { YES: 0, NO: 0, INCONCLUSIVE: 0 });
      var total = Math.max((cons ? (cons.yes + cons.no + cons.inconclusive) : runs.length), 1);
      var order = [["YES", "fill-yes"], ["NO", "fill-no"], ["INCONCLUSIVE", "fill-inc"]];
      $("finch-chart").innerHTML = order.map(function (pair) {
        var pct = Math.round(((counts[pair[0]] || 0) / total) * 100);
        return '<div class="chart-row"><span>' + pair[0] + '</span><div class="chart-track"><div class="chart-fill ' + pair[1] + '" style="width:' + pct + '%"></div></div><span>' + (counts[pair[0]] || 0) + " (" + pct + "%)</span></div>";
      }).join("");
      /* 8 cards */
      var cards = [];
      for (var i = 0; i < 8; i++) {
        var r = runs[i] || {};
        var vote = String(r.parsed_vote || r.vote || (r.raw_response ? "—" : "—")).toUpperCase();
        var text = r.raw_response || r.output || r.text || "Pending…";
        cards.push('<article class="finch-card"><div class="finch-card-head"><strong>Finch Run ' + (i + 1) + '</strong><span class="vote-' + vote + '">' + vote + "</span></div><p>" + esc(text) + "</p></article>");
      }
      $("finch-cards").innerHTML = cards.join("");
    }

    /** ---------- history + compare ---------- */
    function renderHistory() {
      var runs = ctx.state.runs || [], list = $("history-list");
      var selA = $("compare-a"), selB = $("compare-b");
      var opts = runs.map(function (r) { return '<option value="' + esc(r.id) + '">' + esc(r.id) + " — " + esc(String(r.goal || "").slice(0, 40)) + "</option>"; }).join("");
      selA.innerHTML = opts; selB.innerHTML = opts;
      if (runs.length > 1) selB.selectedIndex = 1;
      if (!runs.length) { list.innerHTML = '<p class="muted">No history loaded.</p>'; return; }
      list.innerHTML = runs.map(function (r) {
        return '<div class="history-item' + (r.id === ctx.state.runId ? " is-active" : "") + '" data-run="' + esc(r.id) + '" tabindex="0" role="button" aria-label="Open run ' + esc(r.id) + '">' +
          '<span class="goal"><strong>' + esc(r.id) + "</strong> · " + esc(r.goal || "—") + "</span>" +
          '<span class="badge">' + esc(r.status || "—") + "</span><span class=\"muted small\">Iter " + esc(r.current_iteration || r.iteration || 1) + "</span></div>";
      }).join("");
    }

    var histList = $("history-list");
    if (histList) {
      histList.addEventListener("click", function (ev) {
        var item = ev.target.closest(".history-item");
        if (item && item.dataset.run) ctx.selectRun(item.dataset.run);
      });
      histList.addEventListener("keydown", function (ev) {
        var item = ev.target.closest(".history-item");
        if (item && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); ctx.selectRun(item.dataset.run); }
      });
    }
    var arcBtn = $("archive-btn");
    if (arcBtn) arcBtn.addEventListener("click", function () {
      var msgEl = $("archive-msg");
      if (!ctx.state.runId) { if (msgEl) msgEl.textContent = "No mission selected."; return; }
      if (msgEl) msgEl.textContent = "Archiving…";
      ctx.api.archive(ctx.state.runId).then(function () {
        if (msgEl) msgEl.textContent = "Archived.";
        ctx.refresh();
      }).catch(function (err) { if (msgEl) msgEl.textContent = "Archive failed: " + err.message; });
    });
    var cmp2 = $("compare-btn");
    if (cmp2) cmp2.addEventListener("click", function () {
      var a = (ctx.state.runs || []).filter(function (r) { return r.id === $("compare-a").value; })[0] || {};
      var b = (ctx.state.runs || []).filter(function (r) { return r.id === $("compare-b").value; })[0] || {};
      var box = $("history-compare"); box.hidden = false;
      function row(k, va, vb) { return "<tr><th>" + k + "</th><td>" + esc(va || "—") + "</td><td>" + esc(vb || "—") + "</td></tr>"; }
      box.innerHTML = "<h3>Run comparison</h3><table><thead><tr><th>Field</th><th>" + esc(a.id || "A") + "</th><th>" + esc(b.id || "B") + "</th></tr></thead><tbody>" +
        row("Goal", a.goal, b.goal) + row("Status", a.status, b.status) +
        row("Iteration", a.current_iteration || a.iteration, b.current_iteration || b.iteration) +
        row("Created", a.created_at, b.created_at) + row("Updated", a.updated_at, b.updated_at) + "</tbody></table>";
    });

    /** ---------- finch form ---------- */
    var ff = $("finch-form");
    if (ff) ff.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (!ctx.state.runId) { $("finch-msg").textContent = "No mission selected."; return; }
      var parts = $("finch-responses").value.split(/^\s*---\s*$/m).map(function (s) { return s.trim(); }).filter(Boolean);
      if (parts.length !== 8) { $("finch-msg").textContent = "Need exactly 8 responses, got " + parts.length + ". Separate with a --- line."; return; }
      $("finch-msg").textContent = "Submitting…";
      ctx.api.finch(ctx.state.runId, parts).then(function () { $("finch-msg").textContent = "Finch consensus submitted."; ctx.refresh(); })
        .catch(function (err) { $("finch-msg").textContent = "Finch failed: " + err.message; });
    });

    ctx.renderDashboard = function () { renderOverview(); renderHypotheses(); renderHypCompare(); renderRanking(); renderCrowFalcon(); renderFinch(); renderHistory(); };
  }

  window.AgentFlow = window.AgentFlow || {};
  window.AgentFlow.dashboard = { init: init };
})();
