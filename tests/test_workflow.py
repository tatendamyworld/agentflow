import unittest, sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentflow.agents import validate_sequence, render_prompt, build_context, AGENT_IDS
from agentflow import db

class TestWorkflow(unittest.TestCase):
    def test_correct_sequence(self):
        validate_sequence(AGENT_IDS); validate_sequence(AGENT_IDS[:3])
    def test_cannot_skip(self):
        with self.assertRaises(ValueError): validate_sequence(["supervisor","proximity"])
        with self.assertRaises(ValueError): validate_sequence(["generation"])
    def test_placeholder_substitution(self):
        r, u = render_prompt("Goal: {{GOAL}} Prev: {{SUPERVISOR_OUTPUT}}", {"GOAL":"Build X","SUPERVISOR_OUTPUT":"tasks"})
        self.assertEqual(r, "Goal: Build X Prev: tasks"); self.assertEqual(u, [])
    def test_unresolved_preserved(self):
        r, u = render_prompt("Goal: {{GOAL}} Missing: {{RAW_DATA}}", {"GOAL":"G"})
        self.assertIn("{{RAW_DATA}}", r); self.assertIn("RAW_DATA", u)
    def test_iteration_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = db.create_run("Build X", project=tmp)
            # finish all agents via save_output (simplified: mark each completed)
            for a in AGENT_IDS:
                db.save_output(rid, a, f"{a} output", project=tmp)
            db.save_finch_runs(rid, ["Key finding: YES"]*8, project=tmp)
            from agentflow.consensus import evaluate_consensus
            res = evaluate_consensus(["YES"]*8)
            db.save_consensus(rid, {"yes":8,"no":0,"inconclusive":0,"invalid":0,"outcome":res["outcome"],"detail":"{}"}, project=tmp)
            new_it = db.new_iteration(rid, project=tmp)
            self.assertEqual(new_it, 2)
            exes1 = db.get_executions(rid, iteration=1, project=tmp)
            exes2 = db.get_executions(rid, iteration=2, project=tmp)
            self.assertTrue(all(e["output"] for e in exes1))
            self.assertTrue(all(e["status"]=="PENDING" for e in exes2))
            # history preserved
            self.assertEqual(len(exes1), 9); self.assertEqual(len(exes2), 9)

if __name__ == "__main__":
    unittest.main()
