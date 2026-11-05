"""PHASE 7 — Hypothesis Laboratory tests."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentflow.web import create_app
from agentflow import db, backend_models
from agentflow.agents import AGENT_IDS

class LabBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        self.app = create_app(project=self.project)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def make_run(self, goal="Lab goal"):
        r = self.client.post("/api/runs", json={"goal": goal})
        assert r.status_code == 201, r.get_json()
        return r.get_json()["id"]

    def create_hyp(self, rid, **kw):
        payload = {"title": "H1. Test claim", "description": "details"}
        payload.update(kw)
        r = self.client.post(f"/api/runs/{rid}/hypotheses", json=payload)
        assert r.status_code == 201, r.get_json()
        return r.get_json()

class TestHypothesisLab(LabBase):
    def test_create_manual_full_model(self):
        rid = self.make_run()
        h = self.create_hyp(rid, title="H7. Manual", description="manual desc",
                            origin_agent="generation", evidence="benchmark: 42ms",
                            weaknesses="needs validation", survivability=8.5,
                            elo_rating=1650, ranking=1)
        self.assertEqual(h["title"], "H7. Manual")
        self.assertEqual(h["source_agent"], "generation")
        self.assertEqual(h["evidence"], "benchmark: 42ms")
        self.assertEqual(h["weaknesses"], "needs validation")
        self.assertEqual(h["survivability_score"], 8.5)
        self.assertEqual(h["elo_rating"], 1650.0)
        self.assertEqual(h["status"], "ACTIVE")
        self.assertIsNotNone(h["id"])
        self.assertEqual(h["iteration"], 1)
        # persisted in SQLite
        row = backend_models.get_hypothesis(self.project, h["id"])
        self.assertEqual(row["evidence"], "benchmark: 42ms")

    def test_validation_rejects_fabricated_scores(self):
        rid = self.make_run()
        r = self.client.post(f"/api/runs/{rid}/hypotheses", json={"title": "x", "survivability": 99})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/api/runs/{rid}/hypotheses", json={"title": "x", "elo_rating": 99999})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/api/runs/{rid}/hypotheses", json={"title": "x", "status": "MAYBE"})
        self.assertEqual(r.status_code, 400)

    def test_import_from_generation_output(self):
        rid = self.make_run()
        # save generation with H lines (sequence: supervisor then generation)
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "tasks"})
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "generation", "output": "## Hypotheses\nH1. Solar caching — Score: 8/10\nH2. Wind caching — Score: 5/10"})
        r = self.client.post(f"/api/runs/{rid}/hypotheses/import", json={"agent": "generation"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["parse_ok"])
        self.assertEqual(r.get_json()["imported"], 2)
        hyps = self.client.get(f"/api/runs/{rid}/hypotheses").get_json()["hypotheses"]
        titles = [h["title"] for h in hyps]
        self.assertTrue(any("H1" in t for t in titles))
        # second import is idempotent (no dupes)
        r = self.client.post(f"/api/runs/{rid}/hypotheses/import", json={"agent": "generation"})
        self.assertEqual(r.get_json()["imported"], 0)

    def test_import_parse_failure_manual_hint_no_fabrication(self):
        rid = self.make_run()
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "free text no H lines"})
        # no generation output at all -> import reports manual entry
        r = self.client.post(f"/api/runs/{rid}/hypotheses/import", json={})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["parse_ok"])
        self.assertIn("manual", r.get_json()["message"].lower())
        self.assertEqual(self.client.get(f"/api/runs/{rid}/hypotheses").get_json()["count"], 0)

    def test_edit_and_compare_and_elo(self):
        rid = self.make_run()
        a = self.create_hyp(rid, title="H1. A", description="a", evidence="ev-a", weaknesses="w-a", survivability=9)
        b = self.create_hyp(rid, title="H2. B", description="b", evidence="ev-b", weaknesses="w-b", survivability=6)
        # edit
        r = self.client.patch(f"/api/runs/{rid}/hypotheses/{a['id']}", json={"evidence": "updated evidence", "elo_rating": 1500})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["evidence"], "updated evidence")
        self.client.patch(f"/api/runs/{rid}/hypotheses/{b['id']}", json={"elo_rating": 1500})
        # compare via GET query
        r = self.client.get(f"/api/runs/{rid}/hypotheses/compare?a={a['id']}&b={b['id']}")
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["a"]["id"], a["id"])
        self.assertEqual(d["b"]["we" if False else "evidence"], "ev-b")
        self.assertIn("survivability_diff", d["deltas"])
        self.assertIn("elo_diff", d["deltas"])
        # compare with winner updates ELO
        r = self.client.post(f"/api/runs/{rid}/hypotheses/compare", json={"a": a["id"], "b": b["id"], "winner": a["id"]})
        self.assertEqual(r.status_code, 200, r.get_json())
        wa = backend_models.get_hypothesis(self.project, a["id"])
        lb = backend_models.get_hypothesis(self.project, b["id"])
        self.assertGreater(wa["elo_rating"], 1500)
        self.assertLess(lb["elo_rating"], 1500)

    def test_select_makes_top_available_to_crow(self):
        rid = self.make_run("Crow goal")
        # complete through ranking
        for agent in AGENT_IDS[:6]:
            out = f"{agent}-out"
            if agent == "ranking":
                out = "## Tournament\n- E1 beats E2\nH1. Solar — Score: 8/10\nH2. Wind — Score: 5/10\nTop hypothesis: H1. Solar"
            r = self.client.post(f"/api/runs/{rid}/save", json={"agent": agent, "output": out})
            assert r.status_code == 200, (agent, r.get_json())
        hyps = self.client.get(f"/api/runs/{rid}/hypotheses").get_json()["hypotheses"]
        target = next(h for h in hyps if "H2" in h["title"])
        r = self.client.post(f"/api/runs/{rid}/hypotheses/{target['id']}/select", json={})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["status"], "SELECTED")
        # SQLite: executions.top_hypothesis updated + crow prompt embeds it
        e = db.get_execution(rid, "ranking", project=self.project)
        self.assertIn("H2", e["top_hypothesis"])
        r = self.client.post(f"/api/runs/{rid}/prompt-preview", json={"agent": "crow"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("H2", r.get_json()["prompt"])
        self.assertNotIn("{{TOP_HYPOTHESIS}}", r.get_json()["prompt"])

    def test_archive_hides_from_ranking_and_blocks_select(self):
        rid = self.make_run()
        h = self.create_hyp(rid, title="H9. To archive", description="x")
        r = self.client.post(f"/api/runs/{rid}/hypotheses/{h['id']}/archive", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "ARCHIVED")
        self.assertNotIn(h["id"], [x["id"] for x in self.client.get(f"/api/runs/{rid}/ranking").get_json()["ranking"]])
        r = self.client.post(f"/api/runs/{rid}/hypotheses/{h['id']}/select", json={})
        self.assertEqual(r.status_code, 409)
        # restore makes it active again (not selected)
        r = self.client.post(f"/api/runs/{rid}/hypotheses/{h['id']}/restore", json={})
        self.assertEqual(r.get_json()["status"], "ACTIVE")

    def test_single_selected_invariant(self):
        rid = self.make_run()
        a = self.create_hyp(rid, title="H1. A", description="a")
        b = self.create_hyp(rid, title="H2. B", description="b")
        self.client.post(f"/api/runs/{rid}/hypotheses/{a['id']}/select", json={})
        self.client.post(f"/api/runs/{rid}/hypotheses/{b['id']}/select", json={})
        hyps = self.client.get(f"/api/runs/{rid}/hypotheses").get_json()["hypotheses"]
        selected = [h for h in hyps if h["status"] == "SELECTED"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["id"], b["id"])

if __name__ == "__main__":
    unittest.main()
