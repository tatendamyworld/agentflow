"""PHASE 8 — Ranking Dashboard tests. All values from stored results."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentflow.web import create_app
from agentflow import db, backend_models
from agentflow.agents import AGENT_IDS

class RankBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        self.app = create_app(project=self.project)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def make_ranked_run(self):
        r = self.client.post("/api/runs", json={"goal": "Rank me"})
        rid = r.get_json()["id"]
        ranking_txt = ("## Tournament\n- E1 beats E2: better perf\n- E2 beats E1: cheaper\n"
                       "E1. Solar cache — Score: 8/10\nE2. Wind cache — Score: 5/10\n"
                       "Top hypothesis: E1. Solar cache")
        for agent in AGENT_IDS[:6]:
            out = f"{agent}-out" if agent != "ranking" else ranking_txt
            self.client.post(f"/api/runs/{rid}/save", json={"agent": agent, "output": out})
        return rid

class TestRankingDashboard(RankBase):
    def test_leaderboard_from_stored(self):
        rid = self.make_ranked_run()
        d = self.client.get(f"/api/runs/{rid}/ranking").get_json()
        self.assertGreaterEqual(len(d["leaderboard"]), 1)
        # values originate from SQLite: match hypotheses endpoint count for active
        hyps = self.client.get(f"/api/runs/{rid}/hypotheses").get_json()["hypotheses"]
        active = [h for h in hyps if h["status"] != "ARCHIVED"]
        self.assertEqual(len(d["leaderboard"]), len(active))
        for row in d["leaderboard"]:
            self.assertIn("rank", row)
            self.assertIn("elo_pct", row)

    def test_elo_visualization_matches_stored(self):
        rid = self.make_ranked_run()
        hyps = self.client.get(f"/api/runs/{rid}/hypotheses").get_json()["hypotheses"]
        # set distinct eligible
        for h in hyps:
            elo = 1500.0 + (h["ranking"] or 1) * 10
            self.client.patch(f"/api/runs/{rid}/hypotheses/{h['id']}", json={"elo_rating": elo})
        d = self.client.get(f"/api/runs/{rid}/ranking").get_json()
        viz = d["elo_visual"]
        self.assertEqual(len(viz), len(hyps))
        for v in viz:
            row = backend_models.get_hypothesis(self.project, v["id"])
            self.assertEqual(v["elo"], row["elo_rating"])
        # rated bars have pct, unrated have None (no fabrication)
        rated = [v for v in viz if v["elo"] is not None]
        for v in rated:
            self.assertIsNotNone(v["elo_pct"])

    def test_comparative_results_parsed(self):
        rid = self.make_ranked_run()
        d = self.client.get(f"/api/runs/{rid}/ranking").get_json()
        matches = d["comparative_results"]
        self.assertGreaterEqual(len(matches), 1)
        self.assertTrue(any("beats" in str(m).lower() or m.get("winner_label") for m in matches))
        # persisted, second read idempotent
        d2 = self.client.get(f"/api/runs/{rid}/ranking").get_json()
        self.assertEqual(len(d2["comparative_results"]), len(matches))

    def test_winning_hypothesis_available_to_crow(self):
        rid = self.make_ranked_run()
        d = self.client.get(f"/api/runs/{rid}/ranking").get_json()
        self.assertIsNotNone(d["winning_hypothesis"])
        self.assertTrue(d["crow_available"])
        # Crow preview must contain stored top, not placeholder
        r = self.client.post(f"/api/runs/{rid}/prompt-preview", json={"agent": "crow"})
        self.assertNotIn("{{TOP_HYPOTHESIS}}", r.get_json()["prompt"])

    def test_manual_correction_patch_and_history(self):
        rid = self.make_ranked_run()
        hyps = self.client.get(f"/api/runs/{rid}/hypotheses").get_json()["hypotheses"]
        target = hyps[0]
        before_changes = len(self.client.get(f"/api/runs/{rid}/ranking").get_json()["ranking_changes"])
        r = self.client.patch(f"/api/runs/{rid}/hypotheses/{target['id']}",
                              json={"ranking": 99, "score": 9.5})
        self.assertEqual(r.status_code, 200)
        d = self.client.get(f"/api/runs/{rid}/ranking").get_json()
        self.assertGreater(len(d["ranking_changes"]), before_changes)
        # corrected value reflected
        row = next(h for h in d["leaderboard"] if h["id"] == target["id"])
        self.assertEqual(row["ranking"], 99)

    def test_reorder_bulk_correction(self):
        rid = self.make_ranked_run()
        hyps = self.client.get(f"/api/runs/{rid}/hypotheses").get_json()["hypotheses"]
        ids = [h["id"] for h in hyps]
        reversed_order = list(reversed(ids))
        r = self.client.post(f"/api/runs/{rid}/ranking/reorder", json={"order": reversed_order})
        self.assertEqual(r.status_code, 200, r.get_json())
        board = self.client.get(f"/api/runs/{rid}/ranking").get_json()["leaderboard"]
        self.assertEqual([b["id"] for b in board], reversed_order)
        changes = self.client.get(f"/api/runs/{rid}/ranking").get_json()["ranking_changes"]
        self.assertTrue(any(c["event_type"] == "REORDER" for c in changes) or any(c["event_type"] == "EDIT" for c in changes))

    def test_ranking_history_snapshots(self):
        rid = self.make_ranked_run()
        # loop to iteration 2
        for agent in ["crow", "falcon"]:
            self.client.post(f"/api/runs/{rid}/save", json={"agent": agent, "output": f"{agent} out"})
        self.client.post(f"/api/runs/{rid}/finch", json={"responses": ["Key finding: YES"] * 8})
        self.client.post(f"/api/runs/{rid}/loop", json={})
        hist = self.client.get(f"/api/runs/{rid}/ranking/history").get_json()
        self.assertGreaterEqual(len(hist["snapshots"]), 1)
        self.assertIn("iteration", hist["snapshots"][0])
        self.assertIn("leaderboard", hist["snapshots"][0])

    def test_empty_ranking_no_fabrication(self):
        r = self.client.post("/api/runs", json={"goal": "Empty"})
        rid = r.get_json()["id"]
        d = self.client.get(f"/api/runs/{rid}/ranking").get_json()
        self.assertEqual(d["leaderboard"], [])
        self.assertEqual(d["elo_visual"], [])
        self.assertEqual(d["comparative_results"], [])
        self.assertIsNone(d["winning_hypothesis"])
        self.assertFalse(d["crow_available"])

    def test_rank_change_delta(self):
        rid = self.make_ranked_run()
        # reorder to create a known change
        hyps = backend_models.list_hypotheses(self.project, rid, iteration=1)
        ids = [h["id"] for h in hyps]
        if len(ids) >= 2:
            self.client.post(f"/api/runs/{rid}/ranking/reorder", json={"order": list(reversed(ids))})
            board = self.client.get(f"/api/runs/{rid}/ranking").get_json()["leaderboard"]
            self.assertTrue(any(b.get("rank_change") is not None for b in board))

if __name__ == "__main__":
    unittest.main()
