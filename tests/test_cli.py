"""PHASE 13 — CLI Integration tests: exact same DB + engine as web."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from click.testing import CliRunner
from agentflow.cli import main
from agentflow.web import create_app
from agentflow import db

class TestCliIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        self.runner = CliRunner()
        self.app = create_app(project=self.project)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, **kw):
        return self.runner.invoke(main, ["--project", self.project, *args], **kw)

    def test_new_visible_in_dashboard(self):
        r = self.cli("new", "--goal", "CLI goal")
        self.assertEqual(r.exit_code, 0, r.output)
        runs = self.client.get("/api/runs").get_json()["runs"]
        self.assertTrue(any(x["goal"] == "CLI goal" for x in runs))

    def test_cli_next_paste_sync_web(self):
        r = self.cli("new", "--goal", "Sync goal")
        assert r.exit_code == 0, r.output
        rid = db.list_runs(self.project)[-1]["id"]
        # paste supervisor via CLI (stdin, --yes to skip confirm)
        r = self.cli("paste", "--run", rid, "--yes", input="tasks one two")
        self.assertEqual(r.exit_code, 0, r.output)
        # web sees COMPLETED output
        d = self.client.get(f"/api/runs/{rid}").get_json()
        sup = next(e for e in d["executions"] if e["agent"] == "supervisor")
        self.assertEqual(sup["status"], "COMPLETED")
        self.assertIn("tasks one two", sup["output"])
        # next generates generation prompt with real supervisor output
        r = self.cli("next", "--run", rid)
        self.assertEqual(r.exit_code, 0, r.output)
        gen_prompt = db.get_execution(rid, "generation", project=self.project)["prompt"]
        self.assertIn("tasks one two", gen_prompt)

    def test_web_to_cli_sync(self):
        r = self.client.post("/api/runs", json={"goal": "Web goal"})
        rid = r.get_json()["id"]
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "web tasks"})
        # CLI status sees same (exit 0, output mentions run id)
        r = self.cli("status", "--run", rid)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn(rid, r.output)
        # CLI paste for next agent works on web-created run
        r = self.cli("paste", "--run", rid, "--yes", input="gen output cli")
        self.assertEqual(r.exit_code, 0, r.output)
        d = self.client.get(f"/api/runs/{rid}").get_json()
        gen = next(e for e in d["executions"] if e["agent"] == "generation")
        self.assertEqual(gen["status"], "COMPLETED")

    def test_cli_paste_validation_failed_recoverable(self):
        r = self.cli("new", "--goal", "Valid goal")
        rid = db.list_runs(self.project)[-1]["id"]
        r = self.cli("paste", "--run", rid, "--yes", input="paste {{GOAL}} template")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("Validation failed", r.output)
        e = db.get_execution(rid, "supervisor", project=self.project)
        self.assertEqual(e["status"], "FAILED")
        self.assertIsNone(e["output"])
        # recover
        self.cli("paste", "--run", rid, "--yes", input="real tasks")
        e = db.get_execution(rid, "supervisor", project=self.project)
        self.assertEqual(e["status"], "COMPLETED")

    def test_export_and_set_top_and_copy_diff_loop(self):
        r = self.cli("new", "--goal", "Export goal")
        rid = db.list_runs(self.project)[-1]["id"]
        self.cli("paste", "--run", rid, "--yes", input="tasks")
        # set-top via CLI (free text)
        r = self.cli("set-top", "--run", rid, "--text", "H1. Winner")
        self.assertEqual(r.exit_code, 0, r.output)
        e = db.get_execution(rid, "ranking", project=self.project)
        self.assertEqual(e["top_hypothesis"], "H1. Winner")
        # copy (prompts printed/copied, exit 0)
        r = self.cli("copy", "supervisor", "--run", rid)
        self.assertEqual(r.exit_code, 0, r.output)
        # export file exists
        r = self.cli("export", "--run", rid, "--format", "md")
        self.assertEqual(r.exit_code, 0, r.output)
        path = os.path.join(self.project, ".agentflow", f"export-{rid}.md")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            body = f.read()
        self.assertIn("tasks", body)
        # diff two runs
        self.cli("new", "--goal", "Other goal")
        rid2 = db.list_runs(self.project)[-1]["id"]
        r = self.cli("diff", rid, rid2)
        self.assertEqual(r.exit_code, 0, r.output)
        # loop before consensus fails
        r = self.cli("loop", "--run", rid)
        self.assertNotEqual(r.exit_code, 0)
        # resume
        r = self.cli("resume", rid)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn(rid, r.output)

if __name__ == "__main__":
    unittest.main()
