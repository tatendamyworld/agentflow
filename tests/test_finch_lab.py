"""PHASE 10 — Finch Consensus Laboratory tests."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentflow.web import create_app
from agentflow import db

def make_run(client):
    r = client.post("/api/runs", json={"goal": "Finch lab"})
    return r.get_json()["id"]


class TestFinchLab(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        self.app = create_app(project=self.project)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_eight_individual_submissions(self):
        rid = make_run(self.client)
        for i in range(1, 9):
            r = self.client.put(f"/api/runs/{rid}/finch/run/{i}", json={"response": "Key finding: YES\nanalysis"})
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assertEqual(r.get_json()["run"]["run_number"], i)
            self.assertEqual(r.get_json()["run"]["validation_status"], "VALID")
            self.assertIsNotNone(r.get_json()["run"]["created_at"] or r.get_json()["run"]["updated_at"])
        lab = self.client.get(f"/api/runs/{rid}/finch").get_json()
        self.assertEqual(len(lab["runs"]), 8)
        self.assertEqual(lab["valid_count"], 8)
        self.assertTrue(lab["finalized"])
        self.assertTrue(lab["consensus"]["outcome"].startswith("CONSENSUS"))

    def test_invalid_marked_and_not_voted_until_edited(self):
        rid = make_run(self.client)
        # 7 valid + 1 invalid -> not finalized, invalid not counted as vote
        for i in range(1, 8):
            self.client.put(f"/api/runs/{rid}/finch/run/{i}", json={"response": "Key finding: YES"})
        r = self.client.put(f"/api/runs/{rid}/finch/run/8", json={"response": "no verdict here"})
        d = r.get_json()
        self.assertFalse(d["finalized"])
        self.assertEqual(d["run"]["validation_status"], "INVALID")
        self.assertEqual(d["run"]["parsed_vote"], "INVALID")
        # edit invalid to valid, recalc finalizes
        r = self.client.put(f"/api/runs/{rid}/finch/run/8", json={"response": "Key finding: NO"})
        self.assertTrue(r.get_json()["finalized"])
        self.assertEqual(self.client.get(f"/api/runs/{rid}/finch").get_json()["totals"]["NO"], 1)

    def test_do_not_finalize_until_all_valid(self):
        rid = make_run(self.client)
        for i in range(1, 5):
            self.client.put(f"/api/runs/{rid}/finch/run/{i}", json={"response": "Key finding: YES"})
        lab, _ = db.recalc_consensus(rid, project=self.project), None
        r = self.client.post(f"/api/runs/{rid}/finch/recalc", json={})
        self.assertFalse(r.get_json()["finalized"])
        self.assertIn("INCOMPLETE", r.get_json()["outcome"])
        # no consensus row should exist yet
        self.assertIsNone(db.get_consensus(rid, project=self.project))

    def test_consensus_rules_at_least_4(self):
        from agentflow.consensus import evaluate_consensus
        self.assertEqual(evaluate_consensus(["YES"]*4 + ["NO"]*2 + ["INCONCLUSIVE"]*2)["outcome"], "CONSENSUS: YES")
        self.assertEqual(evaluate_consensus(["YES"]*3 + ["NO"]*3 + ["INCONCLUSIVE"]*2)["outcome"], "SPLIT — NEEDS MORE DATA")
        self.assertEqual(evaluate_consensus(["INCONCLUSIVE"]*4 + ["YES"]*2 + ["NO"]*2)["outcome"], "CONSENSUS: INCONCLUSIVE")
        self.assertEqual(evaluate_consensus(["NO"]*5 + ["YES"]*2 + ["INCONCLUSIVE"]*1)["outcome"], "CONSENSUS: NO")

    def test_multiblock_still_works(self):
        rid = make_run(self.client)
        multi = "\n---\n".join(["Key finding: NO"] * 8)
        r = self.client.post(f"/api/runs/{rid}/finch", json={"multiblock": multi})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["consensus"]["outcome"], "CONSENSUS: NO")

    def test_invalid_bulk_rejected_but_individual_editable(self):
        rid = make_run(self.client)
        # bulk strict parity preserved
        r = self.client.post(f"/api/runs/{rid}/finch", json={"responses": ["no verdict"] * 8})
        self.assertEqual(r.status_code, 400)
        # individual allows invalid then edit
        self.client.put(f"/api/runs/{rid}/finch/run/1", json={"response": "still invalid"})
        self.assertEqual(self.client.get(f"/api/runs/{rid}/finch").get_json()["runs"][0]["validation_status"], "INVALID")

    def test_evaluation_history(self):
        rid = make_run(self.client)
        for i in range(1, 9):
            self.client.put(f"/api/runs/{rid}/finch/run/{i}", json={"response": "Key finding: YES"})
        # edit run 1 -> history snapshot archived
        self.client.put(f"/api/runs/{rid}/finch/run/1", json={"response": "Key finding: NO"})
        h = self.client.get(f"/api/runs/{rid}/finch/history").get_json()
        self.assertGreaterEqual(len(h["history"]), 1)
        self.assertGreaterEqual(len(h["consensus_history"]), 1)

    def test_visual_data_from_stored(self):
        rid = make_run(self.client)
        for v in ["YES"]*4 + ["NO"]*2 + ["INCONCLUSIVE"]*2:
            self.client.put(f"/api/runs/{rid}/finch/run/1", json={"response": f"Key finding: {v}"})
        # refill all properly in order
        vals = ["YES"]*4 + ["NO"]*2 + ["INCONCLUSIVE"]*2
        for i, v in enumerate(vals, 1):
            self.client.put(f"/api/runs/{rid}/finch/run/{i}", json={"response": f"Key finding: {v}"})
        lab = self.client.get(f"/api/runs/{rid}/finch").get_json()
        self.assertEqual(lab["totals"]["YES"], 4)
        self.assertEqual(lab["totals"]["NO"], 2)
        self.assertEqual(lab["totals"]["INCONCLUSIVE"], 2)
        self.assertEqual(lab["percentages"]["YES"], 50.0)
        # if leader >=4 -> consensus NO? Actually YES 4 wins -> CONSENSUS YES
        self.assertTrue(lab["consensus"]["outcome"].startswith("CONSENSUS"))

if __name__ == "__main__":
    unittest.main()
