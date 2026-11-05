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

    /** ---------- hypotheses — PHASE 7 Hypothesis Laboratory ---------- */
    function hypMsg(text) { var m = $("hyp-msg"); if (m) m.textContent = text || ""; }

    function mapHyp(h, i) {
      /* h may be API payload or offline fallback */
      if (h == null || typeof h !== "object") return null;
      var raw = h.id != null ? h.id : ("h" + i);
      return {
        id: raw,
        title: h.title || h.text || h.hypothesis || ("Hypothesis " + raw),
        desc: h.description || h.text || h.hypothesis || "",
        text: h.text || h.hypothesis || h.description || h.title || "",
        source: h.source_agent || h.agent || h.source || h.origin_agent || "manual",
        evidence: h.evidence || h.supporting_evidence || h.strengths || "",
        strengths: h.strengths || h.evidence || "",
        weaknesses: h.weaknesses || "",
        score: h.score,
        surv: h.survivability != null ? h.survivability : h.survivability_score,
        elo: h.elo_rating != null ? h.elo_rating : h.elo,
        status: h.status || h.selection || (h.survivor || h.is_top || h.is_survivor ? "SELECTED" : "ACTIVE"),
        iteration: h.iteration,
        ranking: h.ranking != null ? h.ranking : h.rank,
        survivor: !!(h.survivor || h.is_top || h.is_survivor),
        top: !!((h.top || h.is_top) && (h.status || "ACTIVE") !== "ARCHIVED") || h.status === "SELECTED",
        raw: h
      };
    }

    function collectHypotheses() {
      var nested = ctx.state.hypotheses;
      if (Array.isArray(nested) && nested.length) {
        var mapped = nested.map(mapHyp).filter(Boolean);
        return mapped;
      }
      var out = [], exes = ctx.state.executions || [];
      ["generation", "ranking", "evolution", "reflection"].forEach(function (agent) {
        exes.filter(function (e) { return e.agent === agent; }).forEach(function (e) {
          parseHypothesesFromText(e.output).forEach(function (t, i) {
            out.push({ id: agent + "-" + i, title: t, desc: t, text: t, score: null, source: agent, evidence: "", weaknesses: "", surv: null, elo: null, status: "ACTIVE", survivor: false, top: e.top_hypothesis === t });
          });
          if (e.top_hypothesis && !out.some(function (h) { return h.text === e.top_hypothesis; })) {
            out.push({ id: agent + "-top", title: e.top_hypothesis, desc: e.top_hypothesis, text: e.top_hypothesis, score: null, source: agent, evidence: "", weaknesses: "", surv: null, elo: null, status: "ACTIVE", survivor: false, top: true });
          }
        });
      });
      return out;
    }

    function numericOrNull(v) {
      if (v === "" || v == null) return null;
      var n = Number(v);
      return isNaN(n) ? null : n;
    }

    function syncCompareSelectors(list) {
      var aSel = $("hyp-a"), bSel = $("hyp-b");
      if (!aSel || !bSel) return;
      var prevA = aSel.value, prevB = bSel.value;
      var opts = list.filter(function (h) { return Number.isInteger(Number(h.id)) && String(h.status || "ACTIVE") !== "ARCHIVED"; });
      var html = opts.map(function (h) {
        var label = "#" + h.id + " " + (h.title || h.text || "").slice(0, 60);
        return '<option value="' + esc(h.id) + '">' + esc(label) + "</option>";
      }).join("");
      aSel.innerHTML = html; bSel.innerHTML = html;
      if (opts.length >= 2) { aSel.value = prevA && opts.some(function(o){return String(o.id)===prevA;}) ? prevA : opts[0].id; bSel.value = prevB && opts.some(function(o){return String(o.id)===prevB;}) ? prevB : opts[1].id; }
      else if (opts.length === 1) { aSel.value = opts[0].id; bSel.value = opts[0].id; }
    }

    function renderHypotheses() {
      var list = collectHypotheses();
      var wrap = $("hyp-cards");
      if (!wrap) return;
      $("hyp-count").textContent = list.length ? (list.length + " hypotheses") : "";
      $("hyp-empty").hidden = list.length > 0;
      syncCompareSelectors(list);
      wrap.innerHTML = list.map(function (h) {
        var id = esc(h.id);
        var st = h.status || (h.survivor ? "SELECTED" : "ACTIVE");
        var isArch = st === "ARCHIVED";
        var isTop = st === "SELECTED" || h.top || hypState.topId === h.id;
        var isSurv = h.survivor || hypState.survivorId === h.id;
        var badge = isArch ? '<span class="badge badge-archived">Archived</span>' : (isTop ? '<span class="badge badge-top">Selected / Top</span>' : '<span class="badge badge-active">Active</span>');
        var editable = Number.isInteger(Number(h.id)) && String(h.id) !== "undefined";
        return '<article class="hyp-card' + (isSurv ? " is-survivor" : "") + (isTop ? " is-top" : "") + (isArch ? " is-archived" : "") + '" data-id="' + id + '">' +
          '<div class="hyp-card-head"><span class="hyp-card-title">#' + esc(h.id) + " " + esc(h.title || "") + "</span>" + badge + "</div>" +
          '<p class="hyp-text">' + esc(h.desc || h.text || "") + "</p>" +
          '<p class="muted small">Source: ' + esc(h.source || "—") + " · Iter " + esc(h.iteration != null ? h.iteration : "—") + (h.ranking != null ? " · Rank " + esc(h.ranking) : "") + "</p>" +
          '<dl class="insp-meta">' +
            '<div><dt>Evidence</dt><dd>' + esc(h.evidence || "—") + "</dd></div>" +
            '<div><dt>Weaknesses</dt><dd>' + esc(h.weaknesses || "—") + "</dd></div>" +
            '<div><dt>Survivability</dt><dd>' + esc(h.surv != null ? h.surv : "—") + "</dd></div>" +
            '<div><dt>ELO</dt><dd>' + esc(h.elo != null ? Math.round(h.elo) : "—") + "</dd></div>" +
            '<div><dt>Score</dt><dd>' + esc(h.score != null ? h.score : "—") + "</dd></div>" +
          "</dl>" +
          (editable ? '<div><label>Notes <input type="text" data-hyp-note-inline="' + id + '" value="' + esc(h.raw && h.raw.notes || hypState.notes[h.id] || "") + '" placeholder="note"></label></div>' : "") +
          '<div class="btn-row">' +
            '<button type="button" class="btn btn-ghost btn-small" data-hyp-edit="' + id + '">Edit</button>' +
            (isArch ? '<button type="button" class="btn btn-ghost btn-small" data-hyp-restore="' + id + '">Restore</button>'
              : '<button type="button" class="btn btn-primary btn-small" data-hyp-select-top="' + id + '">Select as Top</button>' +
                '<button type="button" class="btn btn-ghost btn-small" data-hyp-archive="' + id + '">Archive</button>') +
          "</div></article>";
      }).join("");
    }

    function renderHypCompare() {
      var b = $("hyp-compare"); if (!b) return;
      var list = collectHypotheses().filter(function (h) { return hypState.compare[h.id]; });
      if (list.length < 2) { b.hidden = true; b.innerHTML = ""; return; }
      b.hidden = false;
      b.innerHTML = "<h3>Comparison (" + list.length + ")</h3><table><thead><tr><th>Hypothesis</th><th>Text</th><th>Score</th><th>Source</th><th>Notes</th></tr></thead><tbody>" +
        list.map(function (h) {
          return "<tr><td>" + esc(h.id) + "</td><td>" + esc(hypState.edits[h.id] != null ? hypState.edits[h.id] : h.text) +
            "</td><td>" + esc(h.score != null ? h.score : "—") + "</td><td>" + esc(h.source || "—") + "</td><td>" +
            esc(hypState.notes[h.id] || "—") + "</td></tr>";
        }).join("") + "</tbody></table><p class='muted small'>Tick Compare on 2+ cards, then use the A-vs-B panel for the full side-by-side.</p>";
    }

    function renderCompareResult(cmp) {
      var box = $("hyp-compare");
      if (!box || !cmp) return;
      box.hidden = false;
      function cell(h) { return esc(h.title || h.text || "—"); }
      function row(label, get) { return "<tr><th>" + label + "</th><td>" + esc(get(cmp.a)) + "</td><td>" + esc(get(cmp.b)) + "</td></tr>"; }
      var deltas = cmp.deltas || {};
      box.innerHTML = "<h3>Hypothesis A vs B</h3><table><thead><tr><th>Field</th><th>A (#" + esc(cmp.a.id) + ")</th><th>B (#" + esc(cmp.b.id) + ")</th></tr></thead><tbody>" +
        row("Title", function (h) { return h.title || h.text || "—"; }) +
        row("Description", function (h) { return h.desc || h.description || h.text || "—"; }) +
        row("Source agent", function (h) { return h.source_agent || h.agent || h.source || "—"; }) +
        row("Strengths (evidence)", function (h) { return h.evidence || h.strengths || "—"; }) +
        row("Weaknesses", function (h) { return h.weaknesses || "—"; }) +
        row("Survivability", function (h) { return h.survivability != null ? h.survivability : (h.survivability_score != null ? h.survivability_score : "—"); }) +
        row("ELO rating", function (h) { return h.elo_rating != null ? Math.round(h.elo_rating) : "—"; }) +
        row("Ranking", function (h) { return h.ranking != null ? h.ranking : (h.rank != null ? h.rank : "—"); }) +
        row("Selection status", function (h) { return h.status || h.selection || (h.is_survivor ? "SELECTED" : "ACTIVE"); }) +
        row("Iteration", function (h) { return h.iteration != null ? h.iteration : "—"; }) +
        "</tbody></table>" +
        "<p class='muted small'>Survivability A-B: " + esc(deltas.survivability_diff != null ? deltas.survivability_diff : "n/a") +
          " · ELO A-B: " + esc(deltas.elo_diff != null ? deltas.elo_diff : "n/a") +
          (cmp.verdict && cmp.verdict.winner ? " · Winner #" + esc(cmp.verdict.winner) + " — ELO updated (K=32)." : " · No winner declared — verdict is not fabricated.") +
        "</p>";
    }

    var hypWrap = $("hyp-cards");
    if (hypWrap) {
      hypWrap.addEventListener("change", function (ev) {
        var t = ev.target;
        if (t.matches("[data-hyp-note-inline]")) { hypState.notes[t.dataset.hypNoteInline] = t.value; }
      });
      hypWrap.addEventListener("click", function (ev) {
        var b = ev.target.closest("button"); if (!b) return;
        function apiId(id) { var n = Number(id); return Number.isInteger(n) ? n : null; }
        if (b.dataset.hypEdit) {
          var id = b.dataset.hypEdit;
          var hid = apiId(id);
          var h = collectHypotheses().filter(function (x) { return String(x.id) === String(id); })[0] || {};
          if (hid == null) { alert("Offline hypothesis — cannot edit until backend is online."); return; }
          var nt = window.prompt("Title:", h.title || "");
          if (nt == null) return;
          var nd = window.prompt("Description:", h.desc || "");
          if (nd == null) return;
          var ne = window.prompt("Supporting evidence (blank = unknown):", h.evidence || "");
          if (ne == null) return;
          var nw = window.prompt("Weaknesses:", h.weaknesses || "");
          if (nw == null) return;
          var ns = window.prompt("Survivability 0-10 (blank = unknown):", h.surv != null ? String(h.surv) : "");
          if (ns == null) return;
          var nelo = window.prompt("ELO 0-4000 (blank = unrated):", h.elo != null ? String(Math.round(h.elo)) : "");
          if (nelo == null) return;
          var payload = { title: nt, description: nd, evidence: ne, weaknesses: nw };
          var surv = numericOrNull(ns); if (surv != null) payload.survivability = surv;
          var elo = numericOrNull(nelo); if (elo != null) payload.elo_rating = elo;
          hypMsg("Saving edit…");
          ctx.api.updateHyp(ctx.state.runId, hid, payload).then(function () { hypMsg("Hypothesis edited and saved to SQLite."); ctx.refresh(); })
            .catch(function (err) { hypMsg("Edit failed: " + err.message); });
        }
        if (b.dataset.hypSelectTop) {
          var hid2 = apiId(b.dataset.hypSelectTop);
          if (hid2 == null) return;
          hypMsg("Selecting as Top (Crow will receive it)…");
          ctx.api.selectHyp(ctx.state.runId, hid2).then(function (d) {
            hypMsg("Top selected: " + (d.top_hypothesis || "") + " — saved to SQLite, available to Crow.");
            ctx.refresh();
          }).catch(function (err) { hypMsg("Select failed: " + err.message); });
        }
        if (b.dataset.hypArchive) {
          var hid3 = apiId(b.dataset.hypArchive);
          if (hid3 == null) return;
          ctx.api.archiveHyp(ctx.state.runId, hid3).then(function () { hypMsg("Archived."); ctx.refresh(); })
            .catch(function (err) { hypMsg("Archive failed: " + err.message); });
        }
        if (b.dataset.hypRestore) {
          var hid4 = apiId(b.dataset.hypRestore);
          if (hid4 == null) return;
          ctx.api.restoreHyp(ctx.state.runId, hid4).then(function () { hypMsg("Restored to Active."); ctx.refresh(); })
            .catch(function (err) { hypMsg("Restore failed: " + err.message); });
        }
      });
    }
    var cmpBtn = $("hyp-compare-btn");
    if (cmpBtn) cmpBtn.addEventListener("click", function () {
      var a = $("hyp-a").value, bsel = $("hyp-b").value;
      if (!a || !bsel) { hypMsg("Pick A and B first (or tick Compare then Compare selected)."); renderHypCompare(); return; }
      hypMsg("Comparing…");
      ctx.api.compareHyps(ctx.state.runId, a, bsel, null).then(function (cmp) {
        renderCompareResult(cmp); hypMsg("Compared #" + a + " vs #" + bsel + ".");
      }).catch(function (err) { hypMsg("Compare failed: " + err.message); });
    });
    var cmpGo = $("hyp-compare-go");
    if (cmpGo) cmpGo.addEventListener("click", function () {
      var a = $("hyp-a").value, bsel = $("hyp-b").value;
      if (!a || !bsel || a === bsel) { hypMsg("Compare needs two different hypotheses."); return; }
      var w = $("hyp-winner").value;
      var winner = w === "a" ? a : (w === "b" ? bsel : null);
      hypMsg(winner ? "Comparing + recording winner (ELO update)…" : "Comparing…");
      ctx.api.compareHyps(ctx.state.runId, a, bsel, winner).then(function (cmp) {
        renderCompareResult(cmp); hypMsg(winner ? ("Winner #" + winner + " — ELO updated, saved to SQLite.") : "Compared — no winner declared.");
        ctx.refresh();
      }).catch(function (err) { hypMsg("Compare failed: " + err.message); });
    });
    var impBtn = $("hyp-import-btn");
    if (impBtn) impBtn.addEventListener("click", function () {
      hypMsg("Importing from Generation/Evolution/Ranking outputs…");
      ctx.api.importHyps(ctx.state.runId, {}).then(function (d) {
        hypMsg(d.message + (d.parse_ok ? "" : " Use + Manual hypothesis below."));
        ctx.refresh();
      }).catch(function (err) { hypMsg("Import failed: " + err.message); });
    });
    var newToggle = $("hyp-new-toggle");
    if (newToggle) newToggle.addEventListener("click", function () {
      var w = $("hyp-new-wrap");
      w.hidden = !w.hidden;
      newToggle.setAttribute("aria-expanded", w.hidden ? "false" : "true");
    });
    var newForm = $("hyp-new-form");
    if (newForm) newForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      function v(id) { return $(id).value.trim(); }
      var payload = { title: v("nh-title"), description: v("nh-desc"), origin_agent: $("nh-source").value, status: $("nh-status").value };
      if (v("nh-evidence")) payload.evidence = v("nh-evidence");
      if (v("nh-weak")) payload.weaknesses = v("nh-weak");
      var surv = numericOrNull(v("nh-surv")); if (surv != null) payload.survivability = surv;
      var elo = numericOrNull(v("nh-elo")); if (elo != null) payload.elo_rating = elo;
      var iter = numericOrNull(v("nh-iter")); if (iter != null) payload.iteration = iter;
      if (payload.status === "SELECTED") payload.is_survivor = true;
      ctx.api.createHyp(ctx.state.runId, payload).then(function () {
        hypMsg("Hypothesis created and saved to SQLite.");
        newForm.reset(); $("hyp-new-wrap").hidden = true; $("hyp-new-toggle").setAttribute("aria-expanded", "false");
        ctx.refresh();
      }).catch(function (err) { hypMsg("Create failed: " + err.message); });
    });

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
