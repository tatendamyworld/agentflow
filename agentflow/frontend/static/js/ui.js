/* AgentFlow Visual — ui.js: polish layer (loading, expand, search, shortcuts, confirms).
   Professional control-room: calm colors, no glow tricks, reduced-motion respected.
   Depends only on DOM ids; never fabricates data — filters act on rendered content. */
(function () {
  "use strict";
  function $(id) { return document.getElementById(id); }

  /* ---------- loading indicators ---------- */
  function setLoading(on, text) {
    var bar = $("loading-bar"), label = $("loading-text");
    if (bar) bar.hidden = !on;
    if (label && text) label.textContent = text;
  }
  function withBusy(btn, label, promise) {
    var orig = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = label || "Working…"; btn.classList.add("is-busy"); }
    setLoading(true, label || "Working…");
    return Promise.resolve(promise).then(function (v) {
      if (btn) { btn.disabled = false; btn.textContent = orig; btn.classList.remove("is-busy"); }
      setLoading(false);
      return v;
    }, function (e) {
      if (btn) { btn.disabled = false; btn.textContent = orig; btn.classList.remove("is-busy"); }
      setLoading(false);
      throw e;
    });
  }

  /* ---------- error toast ---------- */
  function toast(message, kind) {
    var wrap = $("toast-wrap"); if (!wrap) return;
    var el = document.createElement("div");
    el.className = "toast toast-" + (kind || "info");
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    el.textContent = message;
    el.addEventListener("click", function () { el.remove(); });
    wrap.appendChild(el);
    setTimeout(function () { el.classList.add("is-visible"); }, 10);
    setTimeout(function () { el.classList.remove("is-visible"); setTimeout(function () { el.remove(); }, 300); }, 4500);
  }

  /* ---------- expandable panels ---------- */
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest && ev.target.closest("[data-expand]");
    if (!b) return;
    var pre = document.getElementById(b.getAttribute("data-expand"));
    if (!pre) return;
    var open = pre.classList.toggle("is-expanded");
    b.textContent = open ? "Collapse" : "Expand";
  });

  /* ---------- smooth nav + scrollspy ---------- */
  function initNav() {
    var links = Array.prototype.slice.call(document.querySelectorAll("[data-nav]"));
    var map = {};
    links.forEach(function (a) {
      a.addEventListener("click", function (ev) {
        var hash = a.getAttribute("href");
        var target = hash && document.querySelector(hash);
        if (!target) return;
        ev.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        target.setAttribute("tabindex", "-1");
        target.focus({ preventScroll: true });
      });
    });
    /* scrollspy */
    var sections = links.map(function (a) { return document.querySelector(a.getAttribute("href")); }).filter(Boolean);
    if ("IntersectionObserver" in window && sections.length) {
      var obs = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (en.isIntersecting) {
            links.forEach(function (a) { a.classList.toggle("is-active", a.getAttribute("href") === "#" + en.target.id); });
          }
        });
      }, { rootMargin: "-30% 0px -55% 0px" });
      sections.forEach(function (s) { obs.observe(s); });
    }
  }

  /* ---------- search & filtering ---------- */
  function filterHyp() {
    var q = ($("hyp-search") && $("hyp-search").value || "").toLowerCase();
    var st = ($("hyp-status-filter") && $("hyp-status-filter").value || "").toUpperCase();
    var cards = document.querySelectorAll("#hyp-cards .hyp-card");
    var visible = 0;
    cards.forEach(function (card) {
      var txt = (card.textContent || "").toLowerCase();
      var cardSt = "";
      var badge = card.querySelector(".badge");
      if (badge) cardSt = (badge.textContent || "").toUpperCase();
      var okQ = !q || txt.indexOf(q) !== -1;
      var okS = !st || cardSt.indexOf(st) !== -1;
      var show = okQ && okS;
      card.hidden = !show;
      if (show) visible++;
    });
    var lbl = $("hyp-visible");
    if (lbl) lbl.textContent = cards.length ? (visible + "/" + cards.length + " shown") : "";
  }
  function filterHistory() {
    var q = ($("history-search") && $("history-search").value || "").toLowerCase();
    var bucket = ($("history-bucket") && $("history-bucket").value || "").toLowerCase();
    var items = document.querySelectorAll("#history-list .history-item");
    items.forEach(function (item) {
      var txt = (item.textContent || "").toLowerCase();
      var okQ = !q || txt.indexOf(q) !== -1;
      var okB = !bucket || txt.indexOf(bucket) !== -1 || (bucket === "completed" && txt.indexOf("unfinished") === -1);
      item.hidden = !(okQ && okB);
    });
  }

  /* ---------- keyboard shortcuts ---------- */
  function initKeys() {
    var pendingG = false;
    document.addEventListener("keydown", function (ev) {
      var tag = (ev.target && ev.target.tagName) || "";
      if (/INPUT|TEXTAREA|SELECT/.test(tag)) return;
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
      var dlg = $("help-dialog");
      if (ev.key === "Escape") { if (dlg && dlg.open && dlg.close) dlg.close(); pendingG = false; return; }
      if (ev.key === "?") { if (dlg) { if (typeof dlg.showModal === "function") dlg.showModal(); } return; }
      if (ev.key === "/") { ev.preventDefault(); var s = $("hyp-search"); if (s) s.focus(); return; }
      if (/^[1-9]$/.test(ev.key)) {
        var agents = ["supervisor","generation","proximity","reflection","evolution","ranking","crow","falcon","finch"];
        var id = agents[Number(ev.key) - 1];
        window.AgentFlow = window.AgentFlow || {};
        /* dispatch via custom event consumed by dashboard render */
        document.dispatchEvent(new CustomEvent("af:select-agent", { detail: id }));
        var insp = $("inspector"); if (insp) insp.scrollIntoView({ behavior: "smooth", block: "nearest" });
        return;
      }
      if (pendingG) {
        var map = { o: "#mission-overview", n: "#network", h: "#hypotheses", f: "#finch" };
        var sel = map[ev.key.toLowerCase()];
        if (sel) { var t = document.querySelector(sel); if (t) t.scrollIntoView({ behavior: "smooth" }); }
        pendingG = false; return;
      }
      if (ev.key.toLowerCase() === "g") pendingG = true;
    });
    /* agent selection bridge: dashboard sets selectedAgent */
    document.addEventListener("af:select-agent", function (ev) {
      /* best-effort: graph nodes are re-rendered with selected ring; the dashboard
         render loop picks ctx.state.selectedAgent if present */
      try { window.__afSelectedAgent = ev.detail; } catch (e) {}
    });
  }

  /* ---------- boot ---------- */
  function boot() {
    initNav(); initKeys();
    var hs = $("hyp-search"); if (hs) hs.addEventListener("input", filterHyp);
    var hf = $("hyp-status-filter"); if (hf) hf.addEventListener("change", filterHyp);
    var his = $("history-search"); if (his) his.addEventListener("input", filterHistory);
    var hb = $("history-bucket"); if (hb) hb.addEventListener("change", filterHistory);
    var helpBtn = $("help-btn"), helpDlg = $("help-dialog"), helpClose = $("help-close");
    if (helpBtn && helpDlg) helpBtn.addEventListener("click", function () { if (typeof helpDlg.showModal === "function") helpDlg.showModal(); });
    if (helpClose && helpDlg) helpClose.addEventListener("click", function () { if (helpDlg.close) helpDlg.close(); });
    var emptyNew = $("history-empty-new");
    if (emptyNew) emptyNew.addEventListener("click", function () { var b = $("new-mission-btn"); if (b) b.click(); });
    /* keep loading bar in sync with public ctx if provided */
    window.AgentFlow = window.AgentFlow || {};
    window.AgentFlow.ui = { toast: toast, withBusy: withBusy, setLoading: setLoading, filterHyp: filterHyp, filterHistory: filterHistory };
    /* expose filter hooks to dashboard renderer (called after cards render) */
    window.AgentFlow.applyFilters = function () { filterHyp(); filterHistory(); };
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
