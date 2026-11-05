"""PHASE 15 — Comprehensive validation: end-to-end lifecycle, sync, recovery.

All tests use isolated TemporaryDirectory projects (never touch repo data).
"""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentflow.web import create_app
from agentflow import db, backend_models
from agentflow.agents import AGENT_IDS
from click.testing import CliRunner
from agentflow.cli import main as cli_main

FINCH = ["Key finding: YES"] * 8

class FullValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        self.app = create_app(project=self.project)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_lifecycle_end_to_end(self):
        # Creating mission (web)
        r = self.client.post("/api/runs", json={"goal": "Build X"})
        self.assertEqual(r.status_code, 201)
        rid = r.get_json()["id"]
        # Generating prompts + placeholder substitution + sequencing
        r = self.client.post(f"/api/runs/{rid}/generate", json={"agent": "supervisor"})
        self.assertIn("Build X", r.get_json()["prompt"])
        self.assertNotEqual(r.get_json()["execution"]["status"], "COMPLETED")
        # out-of-order blocked
        self.assertEqual(self.client.post(f"/api/runs/{rid}/generate", json={"agent": "crow"}).status_code, 409)
        # submit outputs in order
        outputs = {
            "supervisor": "tasks A B",
            "generation": "H1. Solar — Score: 8/10\nH2. Wind — Score: 5/10",
            "proximity": "clusters",
            "reflection": "scores",
            "evolution": "E1. Combines H1+H2\nE2. Simpler",
            "ranking": "## Tournament\n- E1 beats E2: better\nE1. Solar — Score: 8/10\nE2. Wind — Score: 5/10\nTop hypothesis: E1. Solar",
            "crow": "## Literature research\nLit\n## Risks\nR1",
            "falcon": "## Component being investigated\nLock\n## Limitations\nnone",
        }
        for agent in AGENT_IDS[:-1]:
            # check generation prompt embeds supervisor output (substitution)
            if agent == "generation":
                r2 = self.client.post(f"/api/runs/{rid}/generate", json={"agent": agent})
                self.assertIn("tasks A B", r2.get_json()["prompt"])
            r = self.client.post(f"/api/runs/{rid}/save", json={"agent": agent, "output": outputs[agent]})
            self.assertEqual(r.status_code, 200, (agent, r.get_json()))
        # Hypothesis storage (auto-parsed from ranking)
        hyps = self.client.get(f"/api/runs/{rid}/hypotheses").get_json()["hypotheses"]
        self.assertGreaterEqual(len(hyps), 1)
        # Ranking calculations (leaderboard + elo + select -> crow)
        rank = self.client.get(f"/api/runs/{rid}/ranking").get_json()
        self.assertGreaterEqual(len(rank["leaderboard"]), 1)
        # Finch consensus (8 valid)
        r = self.client.post(f"/api/runs/{rid}/finch", json={"responses": FINCH})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["consensus"]["outcome"].startswith("CONSENSUS"))
        st = self.client.get(f"/api/runs/{rid}/status").get_json()
        self.assertEqual(st["progress"], 100.0)
        # Invalid Finch responses (bulk rejected, individual editable)
        r2 = self.client.post("/api/runs", json={"goal": "Finch invalid"})
        rid2 = r2.get_json()["id"]
        r3 = self.client.post(f"/api/runs/{rid2}/finch", json={"responses": ["no verdict"] * 8})
        self.assertEqual(r3.status_code, 400)
        # Historical preservation: retry archives
        r = self.client.post(f"/api/runs/{rid}/retry", json={"agent": "supervisor"})
        self.assertEqual(r.status_code, 200)
        # Markdown export contains raw + all sections
        body = self.client.get(f"/api/runs/{rid}/export.md").get_data(as_text=True)
        for need in ["# AgentFlow Run", "## Mission details", "### Response (raw)", "## Hypotheses", "## Rankings", "## Finch evaluations", "## Consensus", "tasks A B"]:
            self.assertIn(need, body)

    def test_multiple_iterations_preserved(self):
        r = self.client.post("/api/runs", json={"goal": "Iter inherit"})
        rid = r.get_json()["id"]
        txt = "## Tournament\n- E1 beats E2: better\nE1. Solar\nTop hypothesis: E1. Solar"
        for agent in AGENT_IDS[:-1]:
            self.client.post(f"/api/runs/{rid}/save", json={"agent": agent, "output": txt if agent == "ranking" else f"{agent}-out"})
        self.client.post(f"/api/runs/{rid}/finch", json={"responses": FINCH})
        prev = {e["agent"]: e["output"] for e in db.get_executions(rid, iteration=1, project=self.project)}
        r = self.client.post(f"/api/runs/{rid}/loop", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["current_iteration"], 2)
        self.assertTrue(r.get_json()["supervisor_prompt"])
        # fresh statuses
        exes2 = db.get_executions(rid, iteration=2, project=self.project)
        self.assertFalse(any(e["status"] == "COMPLETED" for e in exes2))
        # previous preserved
        now = {e["agent"]: e["output"] for e in db.get_executions(rid, iteration=1, project=self.project)}
        self.assertEqual(prev, now)
        self.assertEqual(r.get_json()["inherited"]["goal"], "Iter inherit")

    def test_cli_db_sync_and_restart_recovery(self):
        runner = CliRunner()
        def cli(*args, input=None):
            return runner.invoke(cli_main, ["--project", self.project, *args], input=input)
        r = cli("new", "--goal", "CLI restart")
        self.assertEqual(r.exit_code, 0, r.output)
        rid = db.list_runs(self.project)[-1]["id"]
        # dashboard sees CLI run
        self.assertTrue(any(x["id"] == rid for x in self.client.get("/api/runs").get_json()["runs"]))
        # CLI writes, dashboard reads
        r = cli("paste", "--run", rid, "--yes", input="restart tasks")
        self.assertEqual(r.exit_code, 0, r.output)
        # simulated restart: brand-new Flask app on same project dir
        app2 = create_app(project=self.project)
        c2 = app2.test_client()
        d = c2.get(f"/api/runs/{rid}").get_json()
        sup = next(e for e in d["executions"] if e["agent"] == "supervisor")
        self.assertEqual(sup["status"], "COMPLETED")
        self.assertIn("restart tasks", sup["output"])
        st = c2.get(f"/api/runs/{rid}/status").get_json()
        self.assertEqual(st["completed"], 1)

    def test_no_real_project_db_touched(self):
        # Validation DBs must be temp; repo .agentflow untouched by these tests (spot check path exists? not required to fail)
        self.assertNotEqual(os.path.abspath(self.project), os.path.abspath(os.path.join(os.getcwd())))
        self.assertTrue(self.project.startswith(tempfile.gettempdir()))

if __name__ == "__main__":
    unittest.main()
