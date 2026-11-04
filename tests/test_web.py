import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentflow.web import create_app
from agentflow.agents import AGENT_IDS, load_template
from agentflow import db

FINCH_OK = ["Key finding: YES\nanalysis"] * 8


class WebTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(project=self.tmp.name)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def make_run(self, goal="Build X", **extra):
        r = self.client.post("/api/runs", json={"goal": goal, **extra})
        assert r.status_code == 201, r.get_json()
        return r.get_json()["id"]

    def complete_all(self, rid, upto="finch"):
        for agent in AGENT_IDS:
            if agent == "finch":
                break
            if agent == "ranking":
                out = ("## Tournament\n- E1 beats E2: better\n"
                       "H1. Solar caching — Score: 8/10\n"
                       "H2. Wind caching — Score: 5/10\n"
                       "Top hypothesis: H1. Solar caching")
                r = self.client.post(f"/api/runs/{rid}/save",
                                     json={"agent": agent, "output": out,
                                           "top_hypothesis": "H1. Solar caching"})
            else:
                r = self.client.post(f"/api/runs/{rid}/save",
                                     json={"agent": agent,
                                           "output": f"{agent} output"})
            assert r.status_code == 200, (agent, r.status_code, r.get_json())


class TestWeb(WebTestBase):
    def test_index_and_health(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"AgentFlow", r.data)
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "ok")
        # frontend static, if present alongside the backend
        r = self.client.get("/static/js/app.js")
        self.assertIn(r.status_code, (200, 404))

    def test_runs_crud_and_meta(self):
        r = self.client.post("/api/runs", json={"goal": "Build X",
                                                "name": "Mission 1",
                                                "description": "desc"})
        self.assertEqual(r.status_code, 201)
        rid = r.get_json()["id"]
        self.assertTrue(rid.startswith("AF-"))
        self.assertEqual(r.get_json()["name"], "Mission 1")
        r = self.client.get("/api/runs")
        self.assertEqual(len(r.get_json()["runs"]), 1)
        r = self.client.get(f"/api/runs/{rid}")
        d = r.get_json()
        self.assertEqual(d["run"]["description"], "desc")
        self.assertEqual(len(d["executions"]), 9)
        self.assertIsNone(d["consensus"])
        # goal required / typed
        self.assertEqual(self.client.post("/api/runs", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/runs",
                                          json={"goal": 42}).status_code, 400)
        self.assertEqual(self.client.get("/api/runs/AF-NOPE").status_code, 404)
        # same DB the CLI uses
        self.assertIsNotNone(db.get_run(rid, project=self.tmp.name))

    def test_generate_save_sequence(self):
        rid = self.make_run()
        # out-of-order generate / save are rejected
        r = self.client.post(f"/api/runs/{rid}/generate",
                             json={"agent": "crow"})
        self.assertEqual(r.status_code, 409)
        r = self.client.post(f"/api/runs/{rid}/generate",
                             json={"agent": "bogus"})
        self.assertEqual(r.status_code, 400)
        # supervisor: generate does NOT complete it
        r = self.client.post(f"/api/runs/{rid}/generate",
                             json={"agent": "supervisor"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Build X", r.get_json()["prompt"])
        self.assertEqual(r.get_json()["execution"]["status"], "PENDING")
        r = self.client.get(f"/api/runs/{rid}/execution/supervisor")
        self.assertIn("Build X", r.get_json()["prompt"])
        # agent_id alias works too
        r = self.client.post(f"/api/runs/{rid}/generate",
                             json={"agent_id": "supervisor"})
        self.assertEqual(r.status_code, 200)
        # save out of order -> 409, in order -> COMPLETED
        r = self.client.post(f"/api/runs/{rid}/save",
                             json={"agent": "generation", "output": "x"})
        self.assertEqual(r.status_code, 409)
        r = self.client.post(f"/api/runs/{rid}/save",
                             json={"agent": "supervisor", "output": "tasks"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["execution"]["status"], "COMPLETED")
        # status progress
        r = self.client.get(f"/api/runs/{rid}/status")
        d = r.get_json()
        self.assertEqual(d["completed"], 1)
        self.assertAlmostEqual(d["progress"], 100 / 9, places=1)
        self.assertEqual(d["next_agent"], "generation")
        # generation prompt now contains supervisor output
        r = self.client.post(f"/api/runs/{rid}/generate",
                             json={"agent": "generation"})
        self.assertIn("tasks", r.get_json()["prompt"])

    def test_agents_and_templates(self):
        r = self.client.get("/api/agents")
        self.assertEqual([a["id"] for a in r.get_json()["agents"]], AGENT_IDS)
        r = self.client.get("/api/agents/supervisor/template")
        self.assertEqual(r.status_code, 200)
        self.assertIn("GOAL", r.get_json()["placeholders"])
        self.assertEqual(self.client.get(
            "/api/agents/bogus/template").status_code, 404)
        path = os.path.join(os.path.dirname(__file__), "..", "agentflow",
                            "prompts", "supervisor.txt")
        original = open(path).read()
        try:
            r = self.client.put("/api/agents/supervisor/template",
                                json={"template": "Goal {{GOAL}} {{BOGUS}}"})
            self.assertEqual(r.status_code, 400)
            r = self.client.put("/api/agents/supervisor/template",
                                json={"template": "Goal {{GOAL}} {{ITERATION}}"})
            self.assertEqual(r.status_code, 200)
            r = self.client.get("/api/agents/supervisor/template")
            self.assertEqual(r.get_json()["template"],
                             "Goal {{GOAL}} {{ITERATION}}\n")
            self.assertEqual(load_template("supervisor"),
                             "Goal {{GOAL}} {{ITERATION}}\n")
        finally:
            open(path, "w").write(original)

    def test_finch_consensus_and_loop(self):
        rid = self.make_run()
        self.complete_all(rid)
        r = self.client.post(f"/api/runs/{rid}/finch",
                             json={"responses": FINCH_OK})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["consensus"]["outcome"].startswith("CONSENSUS"))
        self.assertEqual(r.get_json()["consensus"]["yes"], 8)
        r = self.client.get(f"/api/runs/{rid}/finch")
        self.assertEqual(len(r.get_json()["finch_runs"]), 8)
        self.assertEqual(r.get_json()["votes"], ["YES"] * 8)
        # invalid votes are rejected and save nothing extra
        r = self.client.post(f"/api/runs/{rid}/finch",
                             json={"responses": ["no verdict"] * 8})
        self.assertEqual(r.status_code, 400)
        # wrong count rejected
        r = self.client.post(f"/api/runs/{rid}/finch",
                             json={"responses": FINCH_OK[:7]})
        self.assertEqual(r.status_code, 400)
        # multiblock form on a second run
        rid2 = self.make_run("Build Y")
        multi = "\n---\n".join(["Key finding: NO"] * 8)
        r = self.client.post(f"/api/runs/{rid2}/finch",
                             json={"multiblock": multi})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["consensus"]["outcome"], "CONSENSUS: NO")
        # loop only after consensus
        rid3 = self.make_run("Build Z")
        self.assertEqual(self.client.post(
            f"/api/runs/{rid3}/loop").status_code, 409)
        r = self.client.post(f"/api/runs/{rid}/loop")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["current_iteration"], 2)
        r = self.client.get(f"/api/runs/{rid}")
        self.assertEqual(len(r.get_json()["executions"]), 9)
        self.assertTrue(all(e["status"] == "PENDING"
                            for e in r.get_json()["executions"]))

    def test_hypotheses_parse_manual_patch_ranking(self):
        rid = self.make_run()
        # empty before ranking
        r = self.client.get(f"/api/runs/{rid}/hypotheses")
        self.assertEqual(r.get_json()["hypotheses"], [])
        # manual add + patch (item & collection forms)
        r = self.client.post(f"/api/runs/{rid}/hypotheses",
                             json={"title": "Manual idea",
                                   "description": "details", "score": 7.5,
                                   "origin_agent": "generation"})
        self.assertEqual(r.status_code, 201, r.get_json())
        hid = r.get_json()["id"]
        self.assertEqual(r.get_json()["text"], "details")
        r = self.client.patch(f"/api/runs/{rid}/hypotheses/{hid}",
                              json={"notes": "looks good", "is_survivor": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["notes"], "looks good")
        self.assertTrue(r.get_json()["is_survivor"])
        r = self.client.patch(f"/api/runs/{rid}/hypotheses",
                              json={"id": hid, "score": 9})
        self.assertEqual(r.get_json()["score"], 9.0)
        # validation
        self.assertEqual(self.client.post(
            f"/api/runs/{rid}/hypotheses", json={"score": "hi"}).status_code, 400)
        self.assertEqual(self.client.post(
            f"/api/runs/{rid}/hypotheses", json={}).status_code, 400)
        self.assertEqual(self.client.patch(
            f"/api/runs/{rid}/hypotheses/99999", json={"notes": "x"}).status_code, 404)
        # parse from ranking output on a fresh run
        rid2 = self.make_run("Parse me")
        self.complete_all(rid2)
        r = self.client.get(f"/api/runs/{rid2}/hypotheses")
        hyps = r.get_json()["hypotheses"]
        self.assertEqual(len(hyps), 2, hyps)
        self.assertEqual(hyps[0]["label"], "H1")
        self.assertEqual(hyps[0]["score"], 8.0)
        self.assertTrue(hyps[0]["is_survivor"])
        self.assertFalse(hyps[1]["is_survivor"])
        # second read does not duplicate
        r = self.client.get(f"/api/runs/{rid2}/hypotheses")
        self.assertEqual(len(r.get_json()["hypotheses"]), 2)
        # ranking endpoint: list + raw output
        r = self.client.get(f"/api/runs/{rid2}/ranking")
        d = r.get_json()
        self.assertEqual(len(d["ranking"]), 2)
        self.assertEqual(d["ranking"][0]["rank"], 1)
        self.assertIn("Tournament", d["ranking_output"])
        self.assertEqual(d["top_hypothesis"], "H1. Solar caching")

    def test_timeline_export_diff_savetop(self):
        rid = self.make_run("Build X")
        rid2 = self.make_run("Build Y")
        self.client.post(f"/api/runs/{rid}/save",
                         json={"agent": "supervisor", "output": "tasks"})
        r = self.client.post(f"/api/runs/{rid}/save-top",
                             json={"top_hypothesis": "Winner text"})
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/api/runs/{rid}/timeline")
        types = [e["type"] for e in r.get_json()["timeline"]]
        self.assertIn("run_created", types)
        self.assertIn("agent_completed", types)
        r = self.client.get(f"/api/runs/{rid}/export.md")
        self.assertEqual(r.status_code, 200)
        self.assertIn("# AgentFlow Run", r.get_data(as_text=True))
        self.assertIn("tasks", r.get_data(as_text=True))
        r = self.client.get(f"/api/runs/{rid}/diff/{rid2}")
        d = r.get_json()
        self.assertEqual(d["differences"]["goal_same"], False)
        self.assertEqual(d["differences"]["completed_a"], 1)
        self.assertEqual(d["differences"]["completed_b"], 0)
        self.assertEqual(len(d["agents"]), 9)
        self.assertEqual(self.client.get(
            f"/api/runs/{rid}/diff/AF-NOPE").status_code, 404)


if __name__ == "__main__":
    unittest.main()
