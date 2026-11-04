import unittest, sys, os, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentflow import db

class TestDatabase(unittest.TestCase):
    def test_create_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = db.create_run("Build X", project=tmp)
            run = db.get_run(rid, project=tmp)
            self.assertIsNotNone(run); self.assertEqual(run["goal"],"Build X")
    def test_save_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = db.create_run("Build Y", project=tmp)
            db.save_output(rid, "supervisor", "tasks output", project=tmp)
            e = db.get_execution(rid, "supervisor", iteration=1, project=tmp)
            self.assertEqual(e["output"], "tasks output")
    def test_recover_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = db.create_run("Build Z", project=tmp)
            db.save_output(rid, "supervisor", "out", project=tmp)
            # simulate restart: re-open
            run = db.get_run(rid, project=tmp)
            self.assertEqual(run["status"], "IN_PROGRESS")
            nxt = db.next_pending(rid, project=tmp)
            self.assertEqual(nxt["agent"], "generation")
    def test_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = db.create_run("Build T", project=tmp)
            run = db.get_run(rid, project=tmp)
            self.assertIsNotNone(run["created_at"]); self.assertIsNotNone(run["updated_at"])
    def test_iterations_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = db.create_run("Iter", project=tmp)
            db.save_output(rid, "supervisor", "v1", project=tmp)
            db.save_finch_runs(rid, ["Key finding: YES"]*8, project=tmp)
            from agentflow.consensus import evaluate_consensus
            res = evaluate_consensus(["YES"]*8)
            db.save_consensus(rid, {"yes":8,"no":0,"inconclusive":0,"invalid":0,"outcome":res["outcome"],"detail":"{}"}, project=tmp)
            db.new_iteration(rid, project=tmp)
            e1 = db.get_execution(rid, "supervisor", iteration=1, project=tmp)
            e2 = db.get_execution(rid, "supervisor", iteration=2, project=tmp)
            self.assertEqual(e1["output"], "v1"); self.assertIsNone(e2["output"])

if __name__ == "__main__":
    unittest.main()
