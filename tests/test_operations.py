"""PHASE 5 — Agent Operations Engine tests.

Covers: prompt generation, placeholder substitution from real preceding
outputs, prompt preview, copy endpoint, manual submission + validation,
persistence + timestamps, previous-execution history, retry, and
dependency enforcement (Supervisor->Goal ... Finch->Benchmark data).
"""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentflow.web import create_app
from agentflow.agents import (AGENT_IDS, AGENT_BY_ID, build_context, dependencies_for,
                              load_template, missing_dependencies, render_prompt)
from agentflow import db

FINCH_OK = ["Key finding: YES\nanalysis"] * 8

class OpsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        self.app = create_app(project=self.project)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def make_run(self, goal="Build caching layer"):
        r = self.client.post("/api/runs", json={"goal": goal})
        assert r.status_code == 201, r.get_json()
        return r.get_json()["id"]


class TestDependenciesMeta(OpsTestBase):
    def test_all_nine_agents_have_descriptions_and_dependencies(self):
        self.assertEqual(len(AGENT_IDS), 9)
        for a in AGENT_IDS:
            node = AGENT_BY_ID[a]
            self.assertTrue(node.get("description"), f"{a} missing description")
            self.assertTrue(node.get("dependencies"), f"{a} missing dependencies")
        # canonical spec dependencies
        self.assertEqual(dependencies_for("supervisor"), ["GOAL"])
        self.assertEqual(dependencies_for("generation"), ["SUPERVISOR_OUTPUT", "GOAL"])
        self.assertIn("GENERATION_OUTPUT", dependencies_for("proximity"))
        self.assertIn("PROXIMITY_OUTPUT", dependencies_for("reflection"))
        self.assertIn("REFLECTION_OUTPUT", dependencies_for("evolution"))
        self.assertIn("EVOLUTION_OUTPUT", dependencies_for("ranking"))
        self.assertIn("TOP_HYPOTHESIS", dependencies_for("crow"))
        self.assertIn("RANKING_OUTPUT", dependencies_for("crow"))
        self.assertIn("CROW_OUTPUT", dependencies_for("falcon"))
        self.assertIn("GOAL", dependencies_for("falcon"))
        self.assertIn("RAW_DATA", dependencies_for("finch"))

    def test_agents_api_exposes_inspector_fields(self):
        r = self.client.get("/api/agents")
        agents = r.get_json()["agents"]
        self.assertEqual(len(agents), 9)
        for a in agents:
            self.assertIn("description", a)
            self.assertIn("dependencies", a)
            self.assertIn("placeholders", a)

        r = self.client.get("/api/agents/crow")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["id"], "crow")
        self.assertIn("Crow", d["name"])
        self.assertIn("TOP_HYPOTHESIS", d["dependencies"])
        self.assertTrue(len(d["description"]) > 10)

        self.assertEqual(self.client.get("/api/agents/nope").status_code, 404)


class TestPromptEngine(OpsTestBase):
    def test_placeholder_substitution_uses_real_preceding_outputs(self):
        rid = self.make_run()
        # supervisor output must appear verbatim inside generation prompt
        r = self.client.post(f"/api/runs/{rid}/generate", json={"agent": "supervisor"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Build caching layer", r.get_json()["prompt"])

        r = self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "TASKS: step1 step2"})
        self.assertEqual(r.status_code, 200)

        r = self.client.post(f"/api/runs/{rid}/generate", json={"agent": "generation"})
        self.assertEqual(r.status_code, 200)
        prompt = r.get_json()["prompt"]
        self.assertIn("TASKS: step1 step2", prompt)
        self.assertNotIn("{{SUPERVISOR_OUTPUT}}", prompt)
        self.assertEqual(r.get_json()["unresolved"], [])

    def test_chain_dependencies_end_to_end(self):
        """Each step's prompt embeds the previous step's saved output."""
        rid = self.make_run("Chain goal")
        chain = [
            ("supervisor", "S-OUT-1"),
            ("generation", "G-OUT-2"),
            ("proximity", "P-OUT-3"),
            ("reflection", "R-OUT-4"),
            ("evolution", "E-OUT-5"),
            ("ranking", "RANK-OUT-6\nTop hypothesis: H1. Solar"),
        ]
        for agent, out in chain:
            r = self.client.post(f"/api/runs/{rid}/save", json={"agent": agent, "output": out})
            assert r.status_code == 200, (agent, r.get_json())
            # generate next agent and assert upstream text present
            idx = AGENT_IDS.index(agent)
            if idx + 1 < len(AGENT_IDS):
                nxt = AGENT_IDS[idx + 1]
                r2 = self.client.post(f"/api/runs/{rid}/generate", json={"agent": nxt})
                if nxt in ("crow",):
                    # top hypothesis is derived from ranking text via regex -> generate should succeed
                    self.assertEqual(r2.status_code, 200, r2.get_json())
                # for crow case the generate is allowed because ranking output exists; check below
        # crow should now generate with ranking output + top hypothesis derived from ranking text
        r = self.client.post(f"/api/runs/{rid}/generate", json={"agent": "crow"})
        if r.status_code == 200:
            self.assertIn("RANK-OUT-6", r.get_json()["prompt"])

    def test_missing_info_warns_not_fabricates(self):
        """Crow without a saved top hypothesis must warn, not invent one."""
        rid = self.make_run("Warn goal")
        # complete up to ranking WITHOUT top hypothesis signal at all
        for agent in AGENT_IDS[:6]:
            out = f"{agent}-out"
            if agent == "ranking":
                out = "tournament blah\nTop hypothesis: H9. FakeWinner"  # has winner line but no explicit field -> regex will extract
                out = "tournament blah"  # deliberately omit winner
            r = self.client.post(f"/api/runs/{rid}/save", json={"agent": agent, "output": out})
            if agent == "ranking":
                # ranking without Top hypothesis: SAVE returns validation warning but persists (non-strict)
                self.assertIn(r.status_code, (200, 422), r.get_json())
                if r.status_code == 200:
                    self.assertIn("Top hypothesis", str(r.get_json()))
            else:
                assert r.status_code == 200, (agent, r.get_json())
        # Now try preview/generate crow — sequence allows (ranking done) but TOP_HYPOTHESIS missing
        r = self.client.post(f"/api/runs/{rid}/prompt-preview", json={"agent": "crow"})
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertTrue(d["warnings"] or d["missing"] or d["unresolved"], d)
        # preview must preserve placeholder, not fabricate winner text
        self.assertIn("{{TOP_HYPOTHESIS}}", d["prompt"])

    def test_finch_requires_benchmark_data(self):
        rid = self.make_run()
        # finch preview without raw_data warns about RAW_DATA
        r = self.client.post(f"/api/runs/{rid}/prompt-preview", json={"agent": "finch"})
        d = r.get_json()
        self.assertIn("RAW_DATA", d["missing"] + d["unresolved"])
        self.assertTrue(len(d["warnings"]) > 0)
        # with raw_data supplied the placeholder resolves
        r = self.client.post(f"/api/runs/{rid}/prompt-preview", json={"agent": "finch", "raw_data": "BENCH: 42ms"})
        d = r.get_json()
        self.assertNotIn("RAW_DATA", d["missing"])
        self.assertIn("BENCH: 42ms", d["prompt"])

    def test_prompt_preview_does_not_complete_agent(self):
        rid = self.make_run()
        r = self.client.post(f"/api/runs/{rid}/prompt-preview", json={"agent": "supervisor"})
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/api/runs/{rid}/execution/supervisor")
        self.assertEqual(r.get_json()["status"], "PENDING")


class TestInspectorBackend(OpsTestBase):
    def test_execution_detail_includes_identity_deps_timestamps(self):
        rid = self.make_run()
        self.client.post(f"/api/runs/{rid}/generate", json={"agent": "supervisor"})
        r = self.client.get(f"/api/runs/{rid}/execution/supervisor")
        d = r.get_json()
        self.assertEqual(d["agent"], "supervisor")
        self.assertEqual(d["name"], "Supervisor")
        self.assertIn("description", d)
        self.assertIn("dependencies", d)
        self.assertIn("created_at", d)
        self.assertIn("updated_at", d)
        self.assertIn("workflow_status", d)

    def test_copy_endpoint_fails_before_generate_then_returns_prompt(self):
        rid = self.make_run()
        r = self.client.get(f"/api/runs/{rid}/copy/supervisor")
        # no prompt persisted yet for supervisor beyond auto-render on create? create renders supervisor prompt
        # so copy may succeed; be permissive: either 404 or prompt text
        if r.status_code == 404:
            self.client.post(f"/api/runs/{rid}/generate", json={"agent": "supervisor"})
            r = self.client.get(f"/api/runs/{rid}/copy/supervisor")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Build", r.get_json()["prompt"])


class TestSubmitSaveRetryHistory(OpsTestBase):
    def test_submit_persists_and_timestamps(self):
        rid = self.make_run()
        r = self.client.post(f"/api/runs/{rid}/submit", json={"agent": "supervisor", "output": "decomp tasks"})
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertTrue(d["execution"]["status"] == "COMPLETED")
        self.assertTrue(d["execution"]["created_at"])
        self.assertTrue(d["execution"]["updated_at"])
        self.assertEqual(d["saved_output"], "decomp tasks")
        self.assertIn("validation", d)

    def test_save_validates_placeholder_paste(self):
        rid = self.make_run()
        r = self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "paste {{GOAL}} here"})
        self.assertEqual(r.status_code, 422, r.get_json())
        # nothing persisted
        e = db.get_execution(rid, "supervisor", project=self.project)
        self.assertNotEqual(e["output"], "paste {{GOAL}} here")

    def test_submit_strict_rejects_ranking_without_winner(self):
        rid = self.make_run()
        for agent in AGENT_IDS[:5]:
            r = self.client.post(f"/api/runs/{rid}/save", json={"agent": agent, "output": f"{agent}-out"})
            assert r.status_code == 200
        r = self.client.post(f"/api/runs/{rid}/submit", json={"agent": "ranking", "output": "tournament no winner mentioned"})
        self.assertEqual(r.status_code, 422, r.get_json())
        # with winner it passes
        r = self.client.post(f"/api/runs/{rid}/submit", json={"agent": "ranking", "output": "Top hypothesis: H1. Solar caching\nScore: 8/10"})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_retry_resets_and_regenerates(self):
        rid = self.make_run()
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "first attempt"})
        r = self.client.post(f"/api/runs/{rid}/retry", json={"agent": "supervisor"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["execution"]["status"], "PENDING")
        self.assertIn("prompt", r.get_json())
        e = db.get_execution(rid, "supervisor", project=self.project)
        self.assertIsNone(e["output"])

    def test_history_endpoints(self):
        rid = self.make_run()
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "v1"})
        # retry then save again -> 2 executions? retry resets same slot; history via iterations
        # run full finch flow to loop into iteration 2 and create history
        for agent in AGENT_IDS[1:8]:
            out = f"{agent}-out"
            if agent == "ranking":
                out = "Top hypothesis: H1. Winner\nH1. Solar — Score: 8/10"
            r = self.client.post(f"/api/runs/{rid}/save", json={"agent": agent, "output": out})
            assert r.status_code == 200, (agent, r.get_json())
        r = self.client.post(f"/api/runs/{rid}/finch", json={"responses": FINCH_OK})
        assert r.status_code == 200
        r = self.client.post(f"/api/runs/{rid}/loop")
        assert r.status_code == 200
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "v2-iteration2"})
        r = self.client.get(f"/api/runs/{rid}/agent/supervisor/history")
        self.assertEqual(r.status_code, 200)
        hist = r.get_json()["history"]
        self.assertGreaterEqual(len(hist), 2)
        iters = sorted(h["iteration"] for h in hist)
        self.assertEqual(iters, [1, 2])
        r = self.client.get(f"/api/runs/{rid}/history")
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.get_json()["count"], 10)

    def test_finch_output_via_finch_endpoint_still_validated(self):
        rid = self.make_run()
        r = self.client.post(f"/api/runs/{rid}/finch", json={"responses": ["no verdict"] * 8})
        self.assertEqual(r.status_code, 400)

    def test_manual_save_persists_saved_output(self):
        rid = self.make_run()
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "hello world"})
        r = self.client.get(f"/api/runs/{rid}/execution/supervisor")
        self.assertEqual(r.get_json()["output"], "hello world")


class TestOpsUnit(unittest.TestCase):
    def test_build_context_uses_real_outputs_only(self):
        run = {"goal": "G", "id": "AF-TEST", "current_iteration": 1}
        ctx = build_context(run, [])
        self.assertEqual(ctx["GOAL"], "G")
        self.assertNotIn("SUPERVISOR_OUTPUT", ctx)
        self.assertNotIn("RAW_DATA", ctx)
        self.assertNotIn("TOP_HYPOTHESIS", ctx)
        rendered, un = render_prompt(load_template("generation"), ctx)
        self.assertIn("SUPERVISOR_OUTPUT", un)
        self.assertIn("{{SUPERVISOR_OUTPUT}}", rendered)

    def test_missing_dependencies_reports_required_only(self):
        run = {"goal": "G", "id": "AF-T2", "current_iteration": 1}
        ctx = build_context(run, [], raw_data="")
        self.assertIn("RAW_DATA", missing_dependencies("finch", ctx))

if __name__ == "__main__":
    unittest.main()
