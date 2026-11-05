"""PHASE 14 — UI polish checks (file presence + key hooks)."""
import os, sys, pathlib, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "agentflow/frontend/templates/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "agentflow/frontend/static/css/style.css").read_text(encoding="utf-8")
UI = (ROOT / "agentflow/frontend/static/js/ui.js").read_text(encoding="utf-8") if (ROOT / "agentflow/frontend/static/js/ui.js").exists() else ""
APP = (ROOT / "agentflow/frontend/static/js/app.js").read_text(encoding="utf-8")
DASH = (ROOT / "agentflow/frontend/static/js/dashboard.js").read_text(encoding="utf-8")
AGENTS = (ROOT / "agentflow/frontend/static/js/agents.js").read_text(encoding="utf-8")

class TestUiPolish(unittest.TestCase):
    def test_smooth_nav_and_toast(self):
        self.assertIn("scroll-behavior", CSS)
        self.assertIn("toast-wrap", HTML)
        self.assertIn("toast", UI)
        self.assertIn("is-active", UI)

    def test_interactive_agent_selection(self):
        self.assertIn("af:select-agent", UI)
        self.assertIn("1", HTML)  # help dialog lists 1-9 (en-dash)
        self.assertIn("Select agent", HTML)
        self.assertIn("Keyboard", HTML)

    def test_status_animations_respect_prefers(self):
        self.assertIn("prefers-reduced-motion", CSS)
        self.assertIn("@keyframes spin", CSS)  # loading spinner, not glow

    def test_expandable_panels(self):
        self.assertIn("data-expand", HTML)
        self.assertIn("is-expanded", CSS)
        self.assertIn("data-expand", UI)

    def test_empty_states(self):
        self.assertIn("history-empty-new", HTML)
        self.assertIn("No missions yet", UI + HTML)

    def test_loading_indicators(self):
        self.assertIn("loading-bar", HTML)
        self.assertIn("withBusy", UI)
        self.assertIn("setLoading", UI)

    def test_confirmation_dialogs(self):
        joined = AGENTS + DASH
        self.assertIn("window.confirm", joined)
        self.assertIn("Archive", joined)

    def test_responsive_layout(self):
        self.assertIn("@media (max-width:720px)", CSS)
        self.assertIn("@media (max-width:980px)", CSS)

    def test_search_filtering(self):
        self.assertIn("hyp-search", HTML)
        self.assertIn("history-search", HTML)
        self.assertIn("filterHyp", UI)
        self.assertIn("filterHistory", UI)

    def test_keyboard_shortcuts(self):
        self.assertIn("help-dialog", HTML)
        self.assertIn("1–9", HTML)
        self.assertIn("keydown", UI)

    def test_help_dialog_and_no_excess_glow(self):
        # professional: no huge glow shadows
        self.assertNotIn("0 0 40px", CSS.lower())
        self.assertNotIn("text-shadow: 0 0", CSS.lower())

if __name__ == "__main__":
    unittest.main()
