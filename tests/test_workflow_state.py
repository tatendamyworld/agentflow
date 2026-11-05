"""PHASE 6 — Workflow State Management tests.

States: CREATED, READY, IN_PROGRESS, WAITING_FOR_INPUT, COMPLETED,
FAILED, BLOCKED, ARCHIVED. Rules: deps block execution, generate does
not complete, valid save completes, invalid stays recoverable, no silent
overwrite, retry preserves previous execution, dashboard reflects DB,
progress calculation, multiple missions isolated.
"""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentflow.web import create_app
from agentflow import db
from agentflow.workflow import (RUN_STATES, compute_progress, derive_run_status,
                                normalize_run_status, can_transition_run,
                                can_transition_exec, next_agent, completed_agents)
from agentflow.agents import AGENT_IDS

FINCH_OK = ["Key finding: YES\nanalysis"] * 8

class StateTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        self.app = create_app(project=self.project)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def make_run(self, goal="Build X"):
        r = self.client.post("/api/runs", json={"goal": goal})
        assert r.status_code == 201, r.get_json()
        return r.get_json()["id"]


class TestStateMachineUnit(unittest.TestCase):
    def test_canonical_states_present(self):
        for s in ["CREATED","READY","IN_PROGRESS","WAITING_FOR_INPUT","COMPLETED","FAILED","BLOCKED","ARCHIVED"]:
            self.assertIn(s, RUN_STATES)

    def test_legacy_normalization(self):
        self.assertEqual(normalize_run_status("CONSENSUS_REACHED"), "COMPLETED")
        self.assertEqual(normalize_run_status("SPLIT"), "BLOCKED")
        self.assertEqual(normalize_run_status("WAITING_FOR_FINCH"), "WAITING_FOR_INPUT")
        self.assertEqual(normalize_run_status("WAITING_FOR_TOP_HYPOTHESIS"), "WAITING_FOR_INPUT")

    def test_transitions(self):
        self.assertTrue(can_transition_run("CREATED", "READY"))
        self.assertTrue(can_transition_run("READY", "IN_PROGRESS"))
        self.assertTrue(can_transition_run("IN_PROGRESS", "WAITING_FOR_INPUT"))
        self.assertTrue(can_transition_run("WAITING_FOR_INPUT", "COMPLETED"))
        self.assertTrue(can_transition_run("COMPLETED", "ARCHIVED"))
        self.assertFalse(can_transition_run("ARCHIVED", "IN_PROGRESS"))  # terminal
        self.assertTrue(can_transition_exec("PENDING", "WAITING_FOR_INPUT"))
        self.assertTrue(can_transition_exec("WAITING_FOR_INPUT", "COMPLETED"))
        self.assertTrue(can_transition_exec("FAILED", "PENDING"))

    def test_derive_and_progress(self):
        run = {"current_iteration": 1, "status": "CREATED"}
        # fresh -> READY
        exes = [{"agent": a, "status": "PENDING", "prompt": None} for a in AGENT_IDS]
        self.assertEqual(derive_run_status(run, exes), "READY")
        # prompt generated -> WAITING_FOR_INPUT
        exes[0]["prompt"] = "prompt text"
        self.assertEqual(derive_run_status(run, exes), "WAITING_FOR_INPUT")
        # one completed, next pending -> IN_PROGRESS
        exes[0]["status"] = "COMPLETED"
        self.assertEqual(derive_run_status(run, exes), "IN_PROGRESS")
        prog = compute_progress(run, exes)
        self.assertEqual(prog["completed"], 1)
        self.assertEqual(prog["total"], 9)
        self.assertAlmostEqual(prog["percentage"], round(1/9*100, 1))
        self.assertEqual(prog["next_agent"], "generation")
        # failed next -> FAILED
        exes[1]["status"] = "FAILED"
        self.assertEqual(derive_run_status(run, exes), "FAILED")
        # blocked next -> BLOCKED
        exes[1]["status"] = "BLOCKED"
        self.assertEqual(derive_run_status(run, exes), "BLOCKED")
        # all done -> COMPLETED
        for e in exes:
            e["status"] = "COMPLETED"
        self.assertEqual(derive_run_status(run, exes), "COMPLETED")


class TestStateMachineAPI(StateTestBase):
    def test_fresh_run_is_ready_not_completed(self):
        rid = self.make_run("Fresh goal")
        r = self.client.get(f"/api/runs/{rid}/status")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIn(d["status"], ("READY","WAITING_FOR_INPUT","CREATED","IN_PROGRESS"))
        self.assertNotEqual(d["status"], "COMPLETED")
        # supervisor prompt auto-generated -> run waiting, but step NOT completed
        e = db.get_execution(rid, "supervisor", project=self.project)
        self.assertNotEqual(e["status"], "COMPLETED")
        self.assertEqual(e["status"], "WAITING_FOR_INPUT")

    def test_generate_does_not_complete(self):
        rid = self.make_run()
        # reset supervisor to PENDING via retry? Direct: create then check generate idempotent
        r = self.client.post(f"/api/runs/{rid}/generate", json={"agent": "supervisor"})
        self.assertEqual(r.status_code, 200)
        e = db.get_execution(rid, "supervisor", project=self.project)
        self.assertNotEqual(e["status"], "COMPLETED")
        self.assertIsNone(e["output"])

    def test_dependencies_block_execution(self):
        rid = self.make_run()
        r = self.client.post(f"/api/runs/{rid}/generate", json={"agent": "generation"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("BLOCKED", r.get_json()["error"])
        # blocked slot is persisted as BLOCKED and recoverable
        e = db.get_execution(rid, "generation", project=self.project)
        self.assertEqual(e["status"], "BLOCKED")
        # save out-of-order also blocked
        r = self.client.post(f"/api/runs/{rid}/save", json={"agent": "proximity", "output": "x"})
        self.assertEqual(r.status_code, 409)

    def test_valid_save_completes_and_advances_progress(self):
        rid = self.make_run()
        r = self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "tasks"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["execution"]["status"], "COMPLETED")
        st = self.client.get(f"/api/runs/{rid}/status").get_json()
        self.assertEqual(st["completed"], 1)
        self.assertEqual(st["next_agent"], "generation")
        self.assertGreater(st["progress"], 0)

    def test_invalid_response_recoverable_and_not_persisted(self):
        rid = self.make_run()
        # placeholder paste -> 422, FAILED, output not persisted, still executable
        r = self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "paste {{GOAL}}"})
        self.assertEqual(r.status_code, 422)
        e = db.get_execution(rid, "supervisor", project=self.project)
        self.assertEqual(e["status"], "FAILED")
        self.assertIsNone(e["output"])
        st = self.client.get(f"/api/runs/{rid}/status").get_json()
        self.assertEqual(st["failed"], 1)
        self.assertEqual(st["status"], "FAILED")
        # recover: valid save succeeds from FAILED
        r = self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "real tasks"})
        self.assertEqual(r.status_code, 200)
        e = db.get_execution(rid, "supervisor", project=self.project)
        self.assertEqual(e["status"], "COMPLETED")

    def test_no_silent_overwrite(self):
        rid = self.make_run()
        r = self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "original"})
        assert r.status_code == 200
        r = self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "sneaky overwrite"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("overwrite", r.get_json()["error"].lower())
        e = db.get_execution(rid, "supervisor", project=self.project)
        self.assertEqual(e["output"], "original")
        # explicit overwrite allowed and archives previous
        r = self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "revised", "overwrite": True})
        self.assertEqual(r.status_code, 200)
        e = db.get_execution(rid, "supervisor", project=self.project)
        self.assertEqual(e["output"], "revised")
        arch = db.get_archived_executions(rid, agent="supervisor", project=self.project)
        self.assertTrue(any(a.get("output") == "original" for a in arch))

    def test_retry_preserves_previous_execution(self):
        rid = self.make_run()
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "v1"})
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "generation", "output": "g1"})
        r = self.client.post(f"/api/runs/{rid}/retry", json={"agent": "supervisor"})
        self.assertEqual(r.status_code, 200)
        # previous v1 preserved in history (incl downstream generation archived)
        arch_sup = db.get_archived_executions(rid, agent="supervisor", project=self.project)
        self.assertTrue(any(a.get("output") == "v1" for a in arch_sup), arch_sup)
        arch_gen = db.get_archived_executions(rid, agent="generation", project=self.project)
        self.assertTrue(any(a.get("output") == "g1" for a in arch_gen))
        hist = self.client.get(f"/api/runs/{rid}/agent/supervisor/history").get_json()["history"]
        self.assertTrue(any(h.get("output") == "v1" for h in hist))
        # current slot reset to WAITING_FOR_INPUT (prompt regenerated), not COMPLETED
        e = db.get_execution(rid, "supervisor", project=self.project)
        self.assertNotEqual(e["status"], "COMPLETED")

    def test_dashboard_reflects_real_db_state(self):
        rid = self.make_run()
        # DB truth: save directly via db layer
        db.save_output(rid, "supervisor", "direct db tasks", project=self.project)
        # API + dashboard data must reflect it without frontend fabrication
        detail = self.client.get(f"/api/runs/{rid}").get_json()
        sup = next(e for e in detail["executions"] if e["agent"] == "supervisor")
        self.assertEqual(sup["output"], "direct db tasks")
        self.assertEqual(sup["status"], "COMPLETED")
        st = self.client.get(f"/api/runs/{rid}/status").get_json()
        self.assertEqual(st["completed"], 1)
        self.assertEqual(st["completed_agents"], ["supervisor"])

    def test_complete_to_archived(self):
        rid = self.make_run()
        for agent in AGENT_IDS[:-1]:
            out = f"{agent}-out"
            if agent == "ranking":
                out = "Top hypothesis: H1. Winner\nH1. Solar — Score: 8/10"
            r = self.client.post(f"/api/runs/{rid}/save", json={"agent": agent, "output": out})
            assert r.status_code == 200, (agent, r.get_json())
        r = self.client.post(f"/api/runs/{rid}/finch", json={"responses": FINCH_OK})
        assert r.status_code == 200
        st = self.client.get(f"/api/runs/{rid}/status").get_json()
        self.assertEqual(st["status"], "COMPLETED")
        self.assertEqual(st["progress"], 100.0)
        # archive allowed from COMPLETED
        r = self.client.post(f"/api/runs/{rid}/archive", json={})
        self.assertEqual(r.status_code, 200)
        st = self.client.get(f"/api/runs/{rid}/status").get_json()
        self.assertEqual(st["status"], "ARCHIVED")

    def test_multiple_independent_missions(self):
        a = self.make_run("Mission A")
        b = self.make_run("Mission B")
        self.assertNotEqual(a, b)
        self.client.post(f"/api/runs/{a}/save", json={"agent": "supervisor", "output": "A tasks"})
        # B untouched
        e_b = db.get_execution(b, "supervisor", project=self.project)
        self.assertNotEqual(e_b["status"], "COMPLETED")
        sa = self.client.get(f"/api/runs/{a}/status").get_json()
        sb = self.client.get(f"/api/runs/{b}/status").get_json()
        self.assertEqual(sa["completed"], 1)
        self.assertEqual(sb["completed"], 0)
        runs = self.client.get("/api/runs").get_json()["runs"]
        self.assertGreaterEqual(len(runs), 2)
        goals = {r["goal"] for r in runs}
        self.assertIn("Mission A", goals)
        self.assertIn("Mission B", goals)

    def test_progress_calculation(self):
        rid = self.make_run()
        st0 = self.client.get(f"/api/runs/{rid}/status").get_json()
        self.assertEqual(st0["completed"], 0)
        self.assertEqual(st0["progress"], 0.0)
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "supervisor", "output": "t"})
        self.client.post(f"/api/runs/{rid}/save", json={"agent": "generation", "output": "g"})
        st = self.client.get(f"/api/runs/{rid}/status").get_json()
        self.assertEqual(st["completed"], 2)
        self.assertAlmostEqual(st["progress"], 2/9*100, places=1)  # 22.2
        self.assertEqual(st["next_agent"], "proximity")

    def test_run_transition_guard(self):
        rid = self.make_run()
        db.set_run_status(rid, "ARCHIVED", project=self.project, force=True)
        with self.assertRaises(ValueError):
            db.set_run_status(rid, "IN_PROGRESS", project=self.project, force=False)  # terminal: no outgoing

if __name__ == "__main__":
    unittest.main()
