/* AgentFlow Visual — agents.js: inspector pane (generate/copy/paste/save/retry) */
(function () {
  "use strict";
  function $(id) { return document.getElementById(id); }
  function msg(text) { var m = $("insp-msg"); if (m) m.textContent = text || ""; }

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

  function init(ctx) {
    var generateBtn = $("insp-generate"), copyBtn = $("insp-copy"), pasteBtn = $("insp-paste"),
        saveBtn = $("insp-save"), retryBtn = $("insp-retry"), out = $("insp-output");

    function needRun() {
      if (!ctx.state.runId) { msg("No mission selected. Create a mission first."); return false; }
      return true;
    }

    if (generateBtn) generateBtn.addEventListener("click", function () {
      if (!needRun() || !ctx.state.selectedAgent) return;
      msg("Generating prompt…");
      ctx.api.generate(ctx.state.runId, ctx.state.selectedAgent).then(function (data) {
        var prompt = (data && (data.prompt || (data.execution && data.execution.prompt))) || "";
        if (prompt) { $("insp-prompt").textContent = prompt; if (ctx.state.run) ctx.state.run.prompt = prompt; }
        msg(prompt ? "Prompt generated." : "Generated (no prompt returned by backend).");
        ctx.refresh();
      }).catch(function (err) { msg("Generate failed: " + err.message); ctx.showError(err.message); });
    });

    if (copyBtn) copyBtn.addEventListener("click", function () {
      var text = $("insp-prompt").textContent || "";
      if (!text || text === "—") { msg("Nothing to copy — generate a prompt first."); return; }
      copyText(text, function () { msg("Prompt copied to clipboard."); });
    });

    if (pasteBtn) pasteBtn.addEventListener("click", function () {
      if (navigator.clipboard && navigator.clipboard.readText) {
        navigator.clipboard.readText().then(function (t) {
          out.value = t; msg("Pasted from clipboard (" + t.length + " chars).");
        }, function () { out.focus(); msg("Clipboard read blocked by browser — paste manually (Ctrl+V)."); });
      } else { out.focus(); msg("Clipboard read unavailable — paste manually (Ctrl+V)."); }
    });

    if (saveBtn) saveBtn.addEventListener("click", function () {
      if (!needRun() || !ctx.state.selectedAgent) return;
      var output = out.value.trim();
      if (!output) { msg("Output is empty — nothing saved."); return; }
      var payload = { agent: ctx.state.selectedAgent, output: output };
      var top = $("insp-top") && $("insp-top").value.trim();
      if (top) payload.top_hypothesis = top;
      msg("Saving…");
      ctx.api.save(ctx.state.runId, payload).then(function () {
        msg("Saved " + ctx.state.selectedAgent + " output.");
        ctx.refresh();
      }).catch(function (err) { msg("Save failed: " + err.message); ctx.showError(err.message); });
    });

    if (retryBtn) retryBtn.addEventListener("click", function () {
      if (!needRun() || !ctx.state.selectedAgent) return;
      msg("Retrying " + ctx.state.selectedAgent + "…");
      ctx.api.generate(ctx.state.runId, ctx.state.selectedAgent).then(function (data) {
        var prompt = (data && (data.prompt || (data.execution && data.execution.prompt))) || "";
        if (prompt) $("insp-prompt").textContent = prompt;
        ctx.refresh().then(function () { msg("Retry prompt regenerated for " + ctx.state.selectedAgent + "."); });
      }).catch(function (err) { msg("Retry failed: " + err.message); ctx.showError(err.message); });
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
        var on = k === name;
        tabs[k].classList.toggle("is-active", on);
        tabs[k].setAttribute("aria-selected", on ? "true" : "false");
        panels[k].hidden = !on;
      });
    }
    if (tabs.crow) tabs.crow.addEventListener("click", function () { activate("crow"); });
    if (tabs.falcon) tabs.falcon.addEventListener("click", function () { activate("falcon"); });

    /* Public helper: sync inspector view from state */
    ctx.renderInspector = function () {
      var sel = ctx.state.selectedAgent;
      var exe = (ctx.state.executions || []).filter(function (e) { return e.agent === sel; })[0] || {};
      $("insp-status").textContent = String(exe.status || "—");
      $("insp-meta").textContent = sel ? (sel + " · iteration " + (ctx.state.run ? ctx.state.run.current_iteration || ctx.state.run.iteration || 1 : 1) +
        (exe.updated_at ? " · updated " + exe.updated_at : "")) : "Select a node in the network.";
      if (exe.prompt) $("insp-prompt").textContent = exe.prompt;
      else if (ctx.state.run && ctx.state.run.prompt && sel === "supervisor") $("insp-prompt").textContent = ctx.state.run.prompt;
      else if (!exe.prompt) $("insp-prompt").textContent = "No prompt yet — click Generate.";
      if (document.activeElement !== out) out.value = exe.output || "";
      var topWrap = $("insp-top-wrap");
      if (topWrap) { topWrap.hidden = sel !== "ranking"; }
      var topIn = $("insp-top");
      if (topIn && exe.top_hypothesis && document.activeElement !== topIn) topIn.value = exe.top_hypothesis;
    };
  }

  window.AgentFlow = window.AgentFlow || {};
  window.AgentFlow.agents = { init: init };
})();
