"""PHASE 11 — Iteration Engine tests."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentflow.web import create_app
from agentflow import db, backend_models
from agentflow.agents import AGENT_IDS

FINCH = ["Key finding: YES"] * 8

class IterBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        self.app = create_app(project=self.project)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def finish_iter1(self, rid, top="E1. Solar"):
        txt = f"## Tournament\n- E1 beats E2: better\nE1. Solar — Score: 8/10\nE2. Wind — Score: 5/10\nTop hypothesis: {top}\n## Unknowns\n- What about X?"
        for agent in AGENT_IDS[:-1]:
            out = txt if agent == "ranking" else f"{agent}-iter1-out"
            # supervisor include unknowns for unresolved parsing
            if agent == "supervisor":
                out = "## Tasks\n1. do\n## Unknowns\n- What about X?\n- Define scope"
            r = self.client.post(f"/api/runs/{rid}/save", json={"agent": agent, "output": out})
            assert r.status_code == 200, (agent, r.get_json())
        r = self.client.post(f"/api/runs/{rid}/finch", json={"responses": FINCH})
        assert r.status_code == 200

class TestIterations(IterBase):
    def test_loop_inherits_and_fresh_supervisor(self):
        r = self.client.post("/api/runs", json={"goal": "Inherit goal"})
        rid = r.get_json()["id"]
        self.finish_iter1(rid)
        prev_rows = {e["agent"]: dict(e) for e in db.get_executions(rid, iteration=1, project=self.project)}
        r = self.client.post(f"/api/runs/{rid}/loop", json={})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["current_iteration"], 2)
        # inherited goal + previous consensus + top + unresolved
        self.assertEqual(r.get_json()["inherited"]["goal"], "Inherit goal")
        self.assertIsNotNone(r.get_json()["inherited"]["previous_consensus"])
        self.assertIn("E1", r.get_json()["inherited"]["previous_top_hypothesis"])
        self.assertTrue(r.get_json()["supervisor_prompt"])
        # fresh statuses not copied
        exes2 = db.get_executions(rid, iteration=2, project=self.project)
        self.assertTrue(all(e["status"] in ("PENDING", "WAITING_FOR_INPUT") for e in exes2))
        self.assertNotEqual(exes2[0]["status"], "COMPLETED")
        # supervisor prompt embeds previous consensus/outcome
        self.assertTrue(len(r.get_json()["supervisor_prompt"]) > 10)
        # previous iteration preserved untouched
        now_rows = {e["agent"]: e for e in db.get_executions(rid, iteration=1, project=self.project)}
        for a, before in prev_rows.items():
            self.assertEqual(now_rows[a]["output"], before["output"])

    def test_independent_records_and_history(self):
        r = self.client.post("/api/runs", json={"goal": "G2"})
        rid = r.get_json()["id"]; self.finish_iter1(rid)
        self.client.post(f"/api/runs/{rid}/loop", json={})
        exes1 = db.get_executions(rid, iteration=1, project=self.project)
        exes2 = db.get_executions(rid, iteration=2, project=self.project)
        self.assertEqual(len(exes1), 9); self.assertEqual(len(exes2), 9)
        self.assertTrue(all(e["status"] == "COMPLETED" for e in exes1))
        self.assertTrue(not any(e["status"] == "COMPLETED" for e in exes2))
        # history union preserves
        h = db.get_execution_history(rid, project=self.project)
        self.assertGreaterEqual(len(h), 18)

    def test_iteration_selector_switch(self):
        r = self.client.post("/api/runs", json={"goal": "G3"})
        rid = r.get_json()["id"]; self.finish_iter1(rid)
        self.client.post(f"/api/runs/{rid}/loop", json={})
        lst = self.client.get(f"/api/runs/{rid}/iterations").get_json()
        self.assertEqual([i["iteration"] for i in lst["iterations"]], [1, 2])
        self.assertTrue(any(i["current"] for i in lst["iterations"]))
        # switch to 1 -> detail shows iter1 completed
        r = self.client.post(f"/api/runs/{rid}/iterations/switch", json={"iteration": 1})
        self.assertEqual(r.get_json()["current_iteration"], 1)
        d = self.client.get(f"/api/runs/{rid}").get_json()
        self.assertTrue(all(e["status"] == "COMPLETED" for e in d["executions"]))
        # switch back
        self.client.post(f"/api/runs/{rid}/iterations/switch", json={"iteration": 2})
        d = self.client.get(f"/api/runs/{rid}").get_json()
        self.assertFalse(any(e["status"] == "COMPLETED" for e in d["executions"]))

    def test_compare_iterations(self):
        r = self.client.post("/api/runs", json={"goal": "G4"})
        rid = r.get_json()["id"]; self.finish_iter1(rid)
        self.client.post(f"/api/runs/{rid}/loop", json={})
        # iteration 2: add a hypothesis then complete ranking differently
        self.client.post(f"/api/runs/{rid}/hypotheses", json={"title": "H9. New idea", "description": "new", "origin_agent": "generation"})
        cmpd = self.client.get(f"/api/runs/{rid}/iterations/compare?a=1&b=2").get_json()
        self.assertGreaterEqual("added" in cmpd["hypothesis_changes"], True)
        self.assertIn("ranking_changes", cmpd)
        self.assertIn("consensus_change", cmpd)
        self.assertIn("new_findings", cmpd)
        self.assertIn("unresolved", cmpd)
        # unresolved from iter1 supervisor unknowns present
        self.assertTrue(any("What about X" in u for u in cmpd["unresolved"]["a"]))

    def test_loop_before_consensus_blocked(self):
        r = self.client.post("/api/runs", json={"goal": "G5"})
        rid = r.get_json()["id"]
        self.assertEqual(self.client.post(f"/api/runs/{rid}/loop", json={}).status_code, 409)

if __name__ == "__main__":
    unittest.main()
