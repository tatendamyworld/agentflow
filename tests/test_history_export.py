"""PHASE 12 — Research History and Export tests."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentflow.web import create_app
from agentflow.agents import AGENT_IDS

class HistBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        self.app = create_app(project=self.project)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def make_complete(self, goal="Complete goal"):
        r = self.client.post("/api/runs", json={"goal": goal})
        rid = r.get_json()["id"]
        txt = "## Tournament\n- E1 beats E2: better\nE1. Solar — Score: 8/10\nTop hypothesis: E1. Solar"
        for agent in AGENT_IDS[:-1]:
            self.client.post(f"/api/runs/{rid}/save", json={"agent": agent, "output": txt if agent=="ranking" else f"{agent}-raw-response"})
        self.client.post(f"/api/runs/{rid}/finch", json={"responses": ["Key finding: YES"]*8})
        return rid

class TestHistoryExport(HistBase):
    def test_list_all_missions(self):
        a = self.make_complete("A")
        b = self.make_complete("B")
        c = self.client.post("/api/runs", json={"goal": "Unfinished C"}).get_json()["id"]
        runs = self.client.get("/api/runs").get_json()["runs"]
        self.assertGreaterEqual(len(runs), 3)
        self.assertTrue(any(r["goal"] == "Unfinished C" for r in runs))

    def test_open_resume_inspect(self):
        rid = self.make_complete("Resume me")
        self.client.post(f"/api/runs/{rid}/loop", json={})
        # unfinished iter2 resume: status not completed, next pending exists
        st = self.client.get(f"/api/runs/{rid}/status").get_json()
        self.assertLess(st["progress"], 100)
        self.assertIn(st["next_agent"], AGENT_IDS)
        # completed inspection: detail loads
        self.client.post(f"/api/runs/{rid}/iterations/switch", json={"iteration": 1})
        d = self.client.get(f"/api/runs/{rid}").get_json()
        self.assertEqual(len(d["executions"]), 9)

    def test_export_includes_everything_raw(self):
        rid = self.make_complete("Export full")
        body = self.client.get(f"/api/runs/{rid}/export.md").get_data(as_text=True)
        self.assertIn("# AgentFlow Run", body)
        self.assertIn("## Mission details", body)
        self.assertIn("### Prompt", body)
        self.assertIn("### Response (raw)", body)
        self.assertIn("supervisor-raw-response", body)
        self.assertIn("## Hypotheses", body)
        self.assertIn("## Rankings", body)
        self.assertIn("## Finch evaluations", body)
        self.assertIn("## Consensus", body)
        self.assertIn("## Iteration history", body)
        self.assertIn("Key finding: YES", body)  # raw finch response
        # hypothesis detail fields present
        self.assertIn("Survivability", body)

    def test_research_history_endpoint(self):
        rid = self.make_complete("Dossier")
        self.client.post(f"/api/runs/{rid}/loop", json={})
        d = self.client.get(f"/api/runs/{rid}/research-history").get_json()
        self.assertEqual(d["run_id"], rid)
        self.assertIn("iterations", d)
        self.assertGreaterEqual(len(d["iterations"]), 2)
        self.assertIn("goal", d["run"])
        # iterations expose hypotheses/finch
        it1 = next(i for i in d["iterations"] if i["iteration"] == 1)
        self.assertIn("hypotheses", it1)

    def test_compare_missions(self):
        a = self.make_complete("A goal")
        b = self.make_complete("B goal")
        d = self.client.get(f"/api/runs/{a}/diff/{b}").get_json()
        self.assertFalse(d["differences"]["goal_same"])
        self.assertIn("progress_a", d)
        self.assertIn("hypothesis_count_a", d)

if __name__ == "__main__":
    unittest.main()
