"""PHASE 9 — Crow / Falcon workspaces tests."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentflow.web import create_app
from agentflow import db, backend_models
from agentflow.agents import AGENT_IDS

class WsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        self.app = create_app(project=self.project)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def make_crow_ready(self):
        r = self.client.post("/api/runs", json={"goal": "Build X"})
        rid = r.get_json()["id"]
        ranking_txt = "## Tournament\n- E1 beats E2: better\nE1. Solar — Score: 8/10\nTop hypothesis: E1. Solar"
        for agent in AGENT_IDS[:6]:
            out = f"{agent}-out" if agent != "ranking" else ranking_txt
            self.client.post(f"/api/runs/{rid}/save", json={"agent": agent, "output": out})
        return rid

class TestWorkspaces(WsBase):
    def test_crow_fields_present(self):
        rid = self.make_crow_ready()
        d = self.client.get(f"/api/runs/{rid}/workspace/crow").get_json()
        self.assertEqual(d["workspace"], "crow")
        fields = [s["field"] for s in d["sections"]]
        for f in ["literature_research","technical_feasibility","implementation_strategy","dependencies","risks","recommendations"]:
            self.assertIn(f, fields)
        self.assertIn("SOLAR", (d["selected_hypothesis"] or "").upper())
        # prompt generation works via workspace endpoint
        r = self.client.post(f"/api/runs/{rid}/workspace/crow/generate", json={})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn("E1", r.get_json()["prompt"])

    def test_falcon_fields_present(self):
        rid = self.make_crow_ready()
        d = self.client.get(f"/api/runs/{rid}/workspace/falcon").get_json()
        fields = [s["field"] for s in d["sections"]]
        for f in ["component_investigated","technical_architecture","detailed_analysis","implementation_requirements","testing_strategy","limitations"]:
            self.assertIn(f, fields)

    def test_manual_sections_patch(self):
        rid = self.make_crow_ready()
        r = self.client.patch(f"/api/runs/{rid}/workspace/crow", json={"sections": {"risks": "latency risk", "recommendations": "ship it"}})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["notes"]["risks"], "latency risk")
        # persisted
        n,_ = backend_models.get_workspace_notes(self.project, rid, "crow", 1)
        self.assertEqual(n["risks"], "latency risk")

    def test_save_pastes_parse_into_sections(self):
        rid = self.make_crow_ready()
        crow_text = "## Literature research\nLit A\n## Risks\nRISK-1\n## Recommendations\nShip"
        r = self.client.post(f"/api/runs/{rid}/save", json={"agent": "crow", "output": crow_text})
        self.assertEqual(r.status_code, 200)
        d = self.client.get(f"/api/runs/{rid}/workspace/crow").get_json()
        self.assertEqual(d["notes"]["literature_research"], "Lit A")
        self.assertEqual(d["notes"]["risks"], "RISK-1")
        # history available
        self.assertGreaterEqual(len(d["history"]), 1)

    def test_parse_without_headings_manual_guidance(self):
        rid = self.make_crow_ready()
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "crow", "output": "free prose no headings"})
        d = self.client.get(f"/api/runs/{rid}/workspace/crow").get_json()
        # raw preserved, sections empty, no fabrication
        self.assertIn("free prose", d["saved_output"] or d["raw_output"])
        r = self.client.post(f"/api/runs/{rid}/workspace/crow/parse", json={})
        self.assertFalse(r.get_json()["parse_ok"])

    def test_falcon_parse(self):
        rid = self.make_crow_ready()
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "crow", "output": "plan"})
        fal_text = "## Component being investigated\nLock\n## Technical architecture\nREST\n## Detailed analysis\ndeep\n## Implementation requirements\nreq\n## Testing strategy\njest\n## Limitations\nnone"
        r = self.client.post(f"/api/runs/{rid}/save", json={"agent": "falcon", "output": fal_text})
        self.assertEqual(r.status_code, 200)
        d = self.client.get(f"/api/runs/{rid}/workspace/falcon").get_json()
        self.assertEqual(d["notes"]["component_investigated"], "Lock")
        self.assertEqual(d["notes"]["technical_architecture"], "REST")

    def test_new_templates_request_sections(self):
        from agentflow.agents import load_template
        crow = load_template("crow")
        for h in ["Literature research","Technical feasibility","Implementation strategy","Dependencies","Risks","Recommendations"]:
            self.assertIn(h, crow)
        fal = load_template("falcon")
        for h in ["Component being investigated","Technical architecture","Detailed analysis","Implementation requirements","Testing strategy","Limitations"]:
            self.assertIn(h, fal)

if __name__ == "__main__":
    unittest.main()
