/* AgentFlow Visual — agents.js: PHASE 5 inspector
   GENERATE PROMPT, COPY PROMPT, SUBMIT RESPONSE, SAVE OUTPUT,
   VIEW HISTORY, RETRY STEP — all drive real backend operations.
   Shows: identity, description, workflow status, input dependencies,
   generated prompt, response editor, saved output, execution history. */
(function () {
  "use strict";
  function $(id) { return document.getElementById(id); }
  function msg(text) { var m = $("insp-msg"); if (m) m.textContent = text || ""; }
  function validationMsg(text) { var m = $("insp-validation"); if (m) m.textContent = text || ""; }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }

  function copyText(text, then) {
    function done() { if (then) then(); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallback(); });
    } else fallback();
    function fallback() {
      var ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch (e) { /* noop */ }
      document.body.removeChild(ta); done();
    }
  }

  var AGENT_META = {
    supervisor: {name:"Supervisor", order:1, desc:"Understands the objective and decomposes it into tasks.", deps:["GOAL"]},
    generation: {name:"Generation", order:2, desc:"Generates 8-10 distinct hypotheses.", deps:["SUPERVISOR_OUTPUT","GOAL"]},
    proximity:  {name:"Proximity", order:3, desc:"Identifies similarities and removes duplicates.", deps:["GENERATION_OUTPUT"]},
    reflection: {name:"Reflection", order:4, desc:"Scores survivability and keeps top 4-6.", deps:["PROXIMITY_OUTPUT"]},
    evolution:  {name:"Evolution", order:5, desc:"Combines promising hypotheses into E1... candidates.", deps:["REFLECTION_OUTPUT"]},
    ranking:    {name:"Ranking", order:6, desc:"ELO-style tournament; crowns TOP_HYPOTHESIS.", deps:["EVOLUTION_OUTPUT"]},
    crow:       {name:"Crow", order:7, desc:"Researches winner + implementation plan.", deps:["TOP_HYPOTHESIS","RANKING_OUTPUT"]},
    falcon:     {name:"Falcon", order:8, desc:"Detailed investigation of selected component.", deps:["CROW_OUTPUT","GOAL"]},
    finch:      {name:"Finch", order:9, desc:"Benchmark analysis — 8 independent runs.", deps:["RAW_DATA"]}
  };
  var OUTPUT_LABEL = {
    SUPERVISOR_OUTPUT:"Supervisor output", GENERATION_OUTPUT:"Generation output",
    PROXIMITY_OUTPUT:"Proximity output", REFLECTION_OUTPUT:"Reflection output",
    EVOLUTION_OUTPUT:"Evolution output", RANKING_OUTPUT:"Ranking output",
    CROW_OUTPUT:"Crow output", FALCON_OUTPUT:"Falcon output",
    TOP_HYPOTHESIS:"Top hypothesis", RAW_DATA:"Benchmark data (RAW_DATA)", GOAL:"Original goal"
  };

  function init(ctx) {
    var generateBtn = $("insp-generate"), copyBtn = $("insp-copy"), submitBtn = $("insp-submit"),
        saveBtn = $("insp-save"), historyBtn = $("insp-history"), retryBtn = $("insp-retry"),
        pasteBtn = $("insp-paste"), out = $("insp-output");

    function needRun() {
      if (!ctx.state.runId) { msg("No mission selected. Create a mission first."); return false; }
      return true;
    }

    function currentExe() {
      return (ctx.state.executions || []).filter(function (e) { return e.agent === ctx.state.selectedAgent; })[0] || null;
    }

    function satisfiedDeps() {
      // A dep is satisfied when a completed earlier agent holds real output,
      // or the run goal / top hypothesis / finite context supplies it.
      var exes = ctx.state.executions || [];
      var byAgent = {};
      exes.forEach(function (e) { byAgent[e.agent] = e; });
      function has(a) { var e = byAgent[a]; return !!(e && e.status === "COMPLETED" && e.output && String(e.output).trim()); }
      var top = null;
      exes.forEach(function (e) { if (e.top_hypothesis) top = e.top_hypothesis; });
      var meta = AGENT_META[ctx.state.selectedAgent] || {deps:[]};
      return meta.deps.map(function (d) {
        var ok = false;
        if (d === "GOAL") ok = !!(ctx.state.run && ctx.state.run.goal);
        else if (d === "SUPERVISOR_OUTPUT") ok = has("supervisor");
        else if (d === "GENERATION_OUTPUT") ok = has("generation");
        else if (d === "PROXIMITY_OUTPUT") ok = has("proximity");
        else if (d === "REFLECTION_OUTPUT") ok = has("reflection");
        else if (d === "EVOLUTION_OUTPUT") ok = has("evolution");
        else if (d === "RANKING_OUTPUT") ok = has("ranking");
        else if (d === "CROW_OUTPUT") ok = has("crow");
        else if (d === "FALCON_OUTPUT") ok = has("falcon");
        else if (d === "TOP_HYPOTHESIS") ok = !!top || ((byAgent.ranking||{}).top_hypothesis != null);
        else if (d === "RAW_DATA") ok = true; // supplied at generate/submit time; treat as operator-provided
        return {key:d, label:OUTPUT_LABEL[d]||d, ok:!!ok};
      });
    }

    function showWarnings(warnings, unresolved) {
      var box = $("insp-warning");
      var list = (warnings || []).slice();
      if ((unresolved || []).length) {
        (unresolved || []).forEach(function (u) {
          if (!list.some(function (w) { return w.indexOf(u) !== -1; })) {
            list.push("Unresolved {{{" + u + "}}} — missing upstream output. No fabricated data has been inserted.");
          }
        });
      }
      if (!box) return;
      if (!list.length) { box.hidden = true; box.innerHTML = ""; return; }
      box.hidden = false;
      box.innerHTML = "<strong>⚠ Missing information — prompt not fully generated:</strong><ul>" +
        list.map(function (w) { return "<li>" + esc(w) + "</li>"; }).join("") + "</ul>";
    }

    function renderHistory() {
      var list = $("insp-history-list");
      if (!list) return;
      var sel = ctx.state.selectedAgent;
      var rows = (ctx.state.executions || []).filter(function (e) { return e.agent === sel; });
      // previous iterations of same agent live in run history endpoint; poll-ish fallback:
      // executions only holds current iteration. Fetch full history via API when button clicked.
      if (!rows.length) { list.innerHTML = '<li class="muted">No history for this agent.</li>'; return; }
      list.innerHTML = rows.map(function (e) {
        return '<li><strong>' + esc(e.agent) + '</strong> · iter ' + esc(e.iteration || 1) +
          ' · ' + esc(e.status || "PENDING") +
          (e.updated_at ? "<time>" + esc(e.updated_at) + "</time>" : "<time>—</time>") +
          (e.output ? '<pre class="output-pre">' + esc(String(e.output).slice(0, 400)) + "</pre>" : "") +
          "</li>";
      }).join("");
    }

    /* GENERATE PROMPT */
    if (generateBtn) generateBtn.addEventListener("click", function () {
      if (!needRun() || !ctx.state.selectedAgent) return;
      msg("Generating prompt…"); showWarnings([], []);
      ctx.api.generate(ctx.state.runId, ctx.state.selectedAgent).then(function (data) {
        var prompt = (data && (data.prompt || (data.execution && data.execution.prompt))) || "";
        if (prompt) { $("insp-prompt").textContent = prompt; }
        showWarnings(data.warnings, data.unresolved);
        if (data.missing && data.missing.length) msg("Generate: missing " + data.missing.join(", ") + " — see warning above.");
        else msg(prompt ? "Prompt generated with real placeholder substitution." : "Generated (no prompt returned).");
        ctx.refresh();
      }).catch(function (err) { msg("Generate failed: " + err.message); showWarnings(["Generate blocked: " + err.message]); });
    });

    /* COPY PROMPT — fetch authoritative text via /copy endpoint, copy to clipboard */
    if (copyBtn) copyBtn.addEventListener("click", function () {
      if (!needRun() || !ctx.state.selectedAgent) return;
      ctx.api.copyPrompt(ctx.state.runId, ctx.state.selectedAgent).then(function (data) {
        var text = (data && data.prompt) || $("insp-prompt").textContent || "";
        if (!text || text === "—") { msg("Nothing to copy — generate a prompt first."); return; }
        copyText(text, function () { msg("Prompt copied to clipboard (" + text.length + " chars)."); });
      }).catch(function (err) {
        // fallback to displayed preview
        var text = $("insp-prompt").textContent || "";
        if (!text || text === "—") { msg("Nothing to copy — generate a prompt first."); return; }
        copyText(text, function () { msg("Prompt copied (preview fallback)."); });
      });
    });

    /* SUBMIT RESPONSE — strict validation + persist */
    function submitResponse(strict) {
      if (!needRun() || !ctx.state.selectedAgent) return;
      var output = (out.value || "").trim();
      if (!output) { msg("Response is empty — nothing submitted."); validationMsg("Validation: output must not be empty."); return; }
      var payload = { agent: ctx.state.selectedAgent, output: output };
      var top = $("insp-top") && $("insp-top").value.trim();
      if (top) payload.top_hypothesis = top;
      msg((strict ? "Submitting" : "Saving") + "…");
      var req = strict ? ctx.api.submit(ctx.state.runId, payload) : ctx.api.save(ctx.state.runId, payload);
      req.then(function (data) {
        var v = data.validation || data;
        if (data.ok === false && data.validation) validationMsg("Validation: " + (data.validation.errors || []).join("; "));
        else validationMsg(v && v.errors && v.errors.length ? ("Warnings: " + v.errors.join("; ")) : "Validation passed.");
        msg("Saved " + ctx.state.selectedAgent + " output ✓ (" + (data.updated_at || "timestamped") + ").");
        ctx.refresh();
      }).catch(function (err) {
        msg((strict ? "Submit failed: " : "Save failed: ") + err.message);
        validationMsg("Response validation failed — output was NOT persisted.");
        ctx.showError(err.message);
      });
    }
    if (submitBtn) submitBtn.addEventListener("click", function () { submitResponse(true); });
    if (saveBtn) saveBtn.addEventListener("click", function () { submitResponse(false); });

    /* VIEW HISTORY — toggle previous execution history (real fetch) */
    if (historyBtn) historyBtn.addEventListener("click", function () {
      var panel = $("insp-history");
      var open = panel.hidden;
      panel.hidden = !open;
      historyBtn.setAttribute("aria-expanded", open ? "true" : "false");
      if (!open) { renderHistory(); return; }
      if (!ctx.state.runId) return;
      msg("Loading execution history…");
      ctx.api.agentHistory(ctx.state.runId, ctx.state.selectedAgent).then(function (data) {
        var hist = (data.history || data.executions || []);
        var list = $("insp-history-list");
        if (!hist.length) { list.innerHTML = '<li class="muted">No history for this agent yet.</li>'; }
        else {
          list.innerHTML = hist.map(function (e) {
            return '<li><strong>' + esc(e.agent) + '</strong> · iter ' + esc(e.iteration || 1) +
              ' · ' + esc(e.status || "PENDING") +
              (e.updated_at ? "<time>" + esc(e.updated_at) + "</time>" : "<time>—</time>") +
              "<div>created " + esc(e.created_at || "—") + "</div>" +
              (e.output ? '<pre class="output-pre">' + esc(String(e.output).slice(0, 600)) + "</pre>" : "") +
              "</li>";
          }).join("");
        }
        msg("History loaded (" + hist.length + " executions).");
      }).catch(function (err) { msg("History failed: " + err.message); renderHistory(); });
    });

    /* RETRY STEP — reset slot and regenerate prompt via /retry */
    if (retryBtn) retryBtn.addEventListener("click", function () {
      if (!needRun() || !ctx.state.selectedAgent) return;
      msg("Retrying (reset + regenerate) " + ctx.state.selectedAgent + "…");
      ctx.api.retry(ctx.state.runId, ctx.state.selectedAgent).then(function (data) {
        var prompt = data.prompt || "";
        if (prompt) $("insp-prompt").textContent = prompt;
        showWarnings(data.warnings, data.unresolved);
        var exe = data.execution || data.reset || {};
        if (exe && exe.created_at) msg("Retry OK — slot reset to PENDING, new prompt generated at " + (exe.updated_at || "now") + ".");
        else msg("Retry prompt regenerated for " + ctx.state.selectedAgent + ".");
        ctx.refresh();
      }).catch(function (err) { msg("Retry failed: " + err.message); });
    });

    if (pasteBtn) pasteBtn.addEventListener("click", function () {
      if (navigator.clipboard && navigator.clipboard.readText) {
        navigator.clipboard.readText().then(function (t) {
          out.value = t; msg("Pasted from clipboard (" + t.length + " chars).");
        }, function () { out.focus(); msg("Clipboard read blocked by browser — paste manually (Ctrl+V)."); });
      } else { out.focus(); msg("Clipboard read unavailable — paste manually (Ctrl+V)."); }
    });

    /* Crow / Falcon copy buttons */
    document.querySelectorAll("[data-cf-copy]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var id = btn.getAttribute("data-cf-copy") === "crow" ? "crow-output" : "falcon-output";
        var t = $(id).textContent || "";
        if (t === "—") return;
        copyText(t, function () { msg(btn.textContent + " copied."); });
      });
    });

    /* Crow / Falcon tabs */
    var tabs = { crow: $("tab-crow"), falcon: $("tab-falcon") }, panels = { crow: $("panel-crow"), falcon: $("panel-falcon") };
    function activate(name) {
      Object.keys(tabs).forEach(function (k) {
        if (!tabs[k] || !panels[k]) return;
        var on = k === name;
        tabs[k].classList.toggle("is-active", on);
        tabs[k].setAttribute("aria-selected", on ? "true" : "false");
        panels[k].hidden = !on;
      });
    }
    if (tabs.crow) tabs.crow.addEventListener("click", function () { activate("crow"); });
    if (tabs.falcon) tabs.falcon.addEventListener("click", function () { activate("falcon"); });

    ctx.renderHistory = renderHistory;

    /* renderInspector: identity, description, workflow, deps, prompt, editor, saved output */
    ctx.renderInspector = function () {
      var sel = ctx.state.selectedAgent;
      var meta = AGENT_META[sel] || {};
      var exe = currentExe() || {};
      /* identity */
      var ident = $("insp-identity");
      if (ident) ident.textContent = sel ? (meta.name || sel) + " (agent #" + (meta.order || "?") + " of 9 · id: " + sel + ")" : "Select a node in the network.";
      var desc = $("insp-desc");
      if (desc) desc.textContent = meta.desc || "—";
      /* status */
      var st = exe.status || "Pending";
      var pill = $("insp-status"); if (pill) pill.textContent = st;
      var wf = $("insp-workflow-status"); if (wf) wf.textContent = st;
      var it = $("insp-iteration");
      if (it) it.textContent = String((exe.iteration != null ? exe.iteration : (ctx.state.run ? (ctx.state.run.current_iteration || ctx.state.run.iteration || 1) : 1)));
      var cr = $("insp-created"); if (cr) cr.textContent = exe.created_at || "—";
      var up = $("insp-updated"); if (up) up.textContent = exe.updated_at || "—";
      /* dependencies */
      var depsEl = $("insp-deps");
      if (depsEl) {
        var sd = satisfiedDeps();
        depsEl.innerHTML = sd.length
          ? sd.map(function (d) { return '<li class="dep-' + (d.ok ? "ok" : "missing") + '">' + (d.ok ? "✓ " : "✗ ") + esc(d.label) + " ({{" + esc(d.key) + "}})" + "</li>"; }).join("")
          : "<li>—</li>";
      }
      /* prompt preview */
      if (exe.prompt) $("insp-prompt").textContent = exe.prompt;
      else if (ctx.state.run && ctx.state.run.prompt && sel === "supervisor") $("insp-prompt").textContent = ctx.state.run.prompt;
      else if (!exe.prompt && !(ctx.state.run && ctx.state.run.prompt)) {
        var cur = $("insp-prompt").textContent;
        if (cur === "—" || !cur) $("insp-prompt").textContent = "No prompt yet — click GENERATE PROMPT.";
      }
      /* saved output */
      var saved = $("insp-saved");
      if (saved) saved.textContent = exe.output || "—";
      if (document.activeElement !== out) out.value = exe.output || "";
      var topWrap = $("insp-top-wrap");
      if (topWrap) { topWrap.hidden = sel !== "ranking"; }
      var topIn = $("insp-top");
      if (topIn && exe.top_hypothesis && document.activeElement !== topIn) topIn.value = exe.top_hypothesis;
      /* legacy alias */
      var legacy = $("insp-meta");
      if (legacy) legacy.textContent = sel ? (sel + " · iteration " + (ctx.state.run ? ctx.state.run.current_iteration || ctx.state.run.iteration || 1 : 1) + (exe.updated_at ? " · updated " + exe.updated_at : "")) : "Select a node in the network.";
      var panel = $("insp-history");
      if (panel && !panel.hidden) renderHistory();
    };
  }

  window.AgentFlow = window.AgentFlow || {};
  window.AgentFlow.agents = { init: init };
})();
