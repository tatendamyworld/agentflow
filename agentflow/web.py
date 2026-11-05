"""AgentFlow Visual — Flask backend sharing the CLI SQLite database.

Run manually::

    python -m agentflow.web          # binds 127.0.0.1:5001

or programmatically::

    from agentflow.web import create_app
    app = create_app(project=".")
    app.run(host="127.0.0.1", port=5001)

No shell execution, no network calls: every endpoint is pure
validation + SQLite via :mod:`agentflow.db` / :mod:`agentflow.backend_models`.
"""
import json
import pathlib
import re

from flask import Flask, Response, jsonify, request, send_file

from agentflow import backend_models, db
from agentflow.agents import (
    AGENTS, AGENT_BY_ID, AGENT_IDS, PLACEHOLDERS, build_context, dependencies_for,
    load_template, missing_dependencies, render_prompt, validate_sequence,
)
from agentflow.consensus import (
    evaluate_consensus, parse_finch_vote, validate_finch_responses,
)
try:
    from agentflow import workflow as _wf
except ImportError:
    _wf = None
try:
    from agentflow import ops as _ops
except ImportError:
    _ops = None

HOST = "127.0.0.1"
PORT = 5001

_MAX_GOAL = 5000
_MAX_TEXT = 100_000
_MAX_TEMPLATE = 50_000
_MAX_FINCH = 20_000

_FALLBACK_INDEX = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>AgentFlow Visual</title></head>
<body><h1>AgentFlow Visual</h1>
<p>Backend is running. The visual frontend (<code>agentflow/frontend/</code>)
is served here when present; the JSON API lives under <code>/api/</code>.</p>
<ul><li><a href="/api/runs">/api/runs</a></li>
<li><a href="/api/agents">/api/agents</a></li>
<li><a href="/api/health">/api/health</a></li></ul></body></html>"""


def create_app(project="."):
    """Flask app factory.  ``project`` is the dir holding ``.agentflow/``."""
    project = str(project)
    pkg_dir = pathlib.Path(__file__).resolve().parent
    frontend_dir = pkg_dir / "frontend"
    static_dir = frontend_dir / "static"
    has_static = static_dir.is_dir()

    app = Flask(
        __name__,
        static_folder=str(static_dir) if has_static else None,
        static_url_path="/static" if has_static else None,
    )
    app.config["PROJECT"] = project
    app.config["JSON_SORT_KEYS"] = False

    try:
        backend_models.init_db(project)
    except Exception:
        # A read-only / not-yet-created project dir must not prevent the
        # app factory from building; init is retried lazily per request.
        pass

    # -- helpers -----------------------------------------------------------

    def err(message, code=400):
        return jsonify(error=str(message)), code

    def body():
        """Parsed JSON object body; ``{}`` for an empty body."""
        data = request.get_json(silent=True)
        if data is None:
            if request.data and request.data.strip():
                return None  # present but unparseable / not JSON
            return {}
        return data if isinstance(data, dict) else None

    def valid_str(value, field, required=True, max_len=_MAX_TEXT):
        if value is None:
            if required:
                return None, f"{field} is required"
            return None, None
        if not isinstance(value, str):
            return None, f"{field} must be a string"
        value = value.strip()
        if required and not value:
            return None, f"{field} must not be empty"
        if len(value) > max_len:
            return None, f"{field} exceeds {max_len} characters"
        return value, None

    def valid_agent(value):
        if not isinstance(value, str) or value.strip() not in AGENT_IDS:
            return None, f"agent must be one of {AGENT_IDS}"
        return value.strip(), None

    def get_run_or_404(run_id):
        run = db.get_run(run_id, project)
        if run:
            return run, None
        return None, err(f"run {run_id} not found", 404)

    def run_payload(run, exes=None):
        meta = backend_models.get_run_meta(project, run["id"]) or {}
        payload = dict(run)
        payload["run_id"] = run["id"]
        payload["name"] = meta.get("name")
        payload["description"] = meta.get("description")
        # canonical workflow state: stored + derived (dashboard reflects DB truth)
        try:
            prog = db.progress_for(run["id"], project) if _wf is not None else None
            if prog:
                payload["derived_status"] = prog["status"]
                payload["canonical_status"] = prog["status"]
                payload["progress"] = prog["percentage"]
                payload["percentage"] = prog["percentage"]
                payload["completed_count"] = prog["completed"]
                payload["total_agents"] = prog["total"]
        except Exception:
            pass
        return payload

    def current_exes(run):
        return db.get_executions(
            run["id"], iteration=run["current_iteration"], project=project)

    def consensus_payload(row):
        if not row:
            return None
        out = dict(row)
        detail = {}
        if row.get("detail"):
            try:
                parsed = json.loads(row["detail"])
                if isinstance(parsed, dict):
                    detail = parsed
            except (ValueError, TypeError):
                detail = {}
        yes = detail.get("yes", row.get("yes_count"))
        no = detail.get("no", row.get("no_count"))
        inc = detail.get("inconclusive", row.get("inconclusive_count"))
        inv = detail.get("invalid", row.get("invalid_count"))
        out.update({
            "yes": yes, "no": no, "inconclusive": inc, "invalid": inv,
            "yes_count": yes, "no_count": no,
            "inconclusive_count": inc, "invalid_count": inv,
            "outcome": row.get("outcome") or detail.get("outcome"),
            "percentages": detail.get("percentages"),
            "votes": detail.get("votes"),
            "result": {"yes": yes, "no": no, "inconclusive": inc,
                       "invalid": inv, "outcome": out["outcome"],
                       "percentages": detail.get("percentages"),
                       "votes": detail.get("votes")},
        })
        return out

    def consensus_for(run):
        row = db.get_consensus(run["id"], project=project)
        if row is None and run["current_iteration"] > 1:
            row = db.get_consensus(
                run["id"], iteration=run["current_iteration"] - 1,
                project=project)
        return consensus_payload(row)

    def top_hypothesis_for(run, exes):
        for e in exes:
            if e.get("top_hypothesis"):
                return e["top_hypothesis"]
        # Phase 7: SELECTED hypothesis in SQLite wins over ranking regex
        try:
            for h in backend_models.list_hypotheses(project, run["id"], iteration=run["current_iteration"]):
                hp = hypothesis_payload(h)
                if hp.get("status") == "SELECTED" and (hp.get("title") or hp.get("description")):
                    return hp.get("title") or hp.get("description")
        except Exception:
            pass
        ranking = next((e for e in exes if e["agent"] == "ranking"), None)
        if ranking and ranking.get("output"):
            m = re.search(r"Top hypothesis:\s*(.+)", ranking["output"], re.I)
            if m:
                return m.group(1).strip()
        return ""

    def next_agent(exes):
        if _wf is not None:
            return _wf.next_agent(exes)
        for e in exes:
            if e["status"] in ("PENDING", "READY", "WAITING", "WAITING_FOR_INPUT", "FAILED", "BLOCKED"):
                return e["agent"]
        return None

    def progress_for(run, exes):
        if _wf is not None:
            prog = _wf.compute_progress(run, exes)
            return prog["completed"], prog["total"], prog["percentage"]
        total = len(exes) or len(AGENT_IDS)
        done = sum(1 for e in exes if e["status"] == "COMPLETED")
        pct = round(done / total * 100, 1) if total else 0.0
        return done, total, pct

    def completed_agents(exes):
        return [e["agent"] for e in exes if e["status"] == "COMPLETED"]

    def check_sequence(exes, agent, allow_completed=True):
        """400/409-style message if ``agent`` is not reachable yet."""
        done = completed_agents(exes)
        idx = AGENT_IDS.index(agent)
        if done == AGENT_IDS[:idx]:
            return None
        if allow_completed and done == AGENT_IDS[:idx + 1]:
            return None  # already completed: regenerate / overwrite allowed
        try:
            validate_sequence(done)
        except ValueError as exc:
            return str(exc)
        return (f"agent {agent} is not next: expected "
                f"{AGENT_IDS[len(done)] if len(done) < len(AGENT_IDS) else 'none'} "
                f"(completed so far: {done})")

    def render_and_save(run, agent, raw_data="", top_hypothesis=""):
        exes = current_exes(run)
        cons = consensus_for(run)
        context = build_context(
            run, exes, consensus=cons, top_hypothesis=top_hypothesis,
            raw_data=raw_data, iteration=run["current_iteration"])
        rendered, unresolved = render_prompt(load_template(agent), context)
        db.save_prompt(run["id"], agent, rendered, project)
        return rendered, unresolved

    def hypothesis_payload(h):
        out = dict(h)
        text = out.get("description") or out.get("title") or ""
        status = out.get("status") or ("SELECTED" if out.get("is_survivor") else "ACTIVE")
        out["status"] = status
        out["selection"] = status
        out["selected"] = status == "SELECTED"
        out["archived"] = status == "ARCHIVED"
        out["text"] = text
        out["hypothesis"] = text
        out["hypothesis_id"] = out.get("id")
        out["agent"] = out.get("origin_agent")
        out["source"] = out.get("origin_agent")
        out["source_agent"] = out.get("origin_agent")
        out["origin_agent"] = out.get("origin_agent")
        ev = out.get("evidence") or out.get("strengths") or ""
        out["evidence"] = ev
        out["supporting_evidence"] = ev
        out["strengths"] = out.get("strengths") or ev
        out["weaknesses"] = out.get("weaknesses") or ""
        out["survivability_score"] = out.get("survivability")
        out["elo_rating"] = out.get("elo_rating")
        out["elo"] = out.get("elo_rating")
        out["rank"] = out.get("ranking")
        out["survivor"] = bool(out.get("is_survivor"))
        out["top"] = bool(out.get("is_survivor")) if status != "ARCHIVED" else False
        out["is_top"] = bool(out.get("is_survivor")) if status != "ARCHIVED" else False
        out["is_survivor"] = bool(out.get("is_survivor")) and status != "ARCHIVED"
        m = re.match(r"^\s*(H\d+|E\d+)\b", out.get("title") or "", re.I)
        out["label"] = m.group(1).upper() if m else None
        return out

    def hypotheses_for(run):
        """Stored hypotheses, parsing Ranking output on first read."""
        exes = current_exes(run)
        ranking = next((e for e in exes if e["agent"] == "ranking"), None)
        rows = backend_models.sync_hypotheses_from_ranking(
            project, run["id"], run["current_iteration"],
            (ranking or {}).get("output") or "")
        return [hypothesis_payload(h) for h in rows]

    def ranked_payload(run):
        # Leaderboard: stored ranking + elo/surv tiebreak, elo_pct, rank_change
        # Ensure Ranking-output parse is synced first (first read creates rows)
        try:
            hypotheses_for(run)
        except Exception:
            pass
        try:
            board = backend_models.get_leaderboard(project, run["id"], run["current_iteration"])
            items = [hypothesis_payload(h) for h in board]
        except Exception:
            items = [h for h in hypotheses_for(run) if h.get("status") != "ARCHIVED"]
            items = sorted(items, key=lambda h: (
                h.get("ranking") is None, h.get("ranking") or 0))
            for i, h in enumerate(items, start=1):
                if h.get("ranking") is None:
                    h["ranking"] = i
        ranked = []
        for i, h in enumerate(items, start=1):
            row = dict(h)
            row["rank"] = h.get("ranking") or i
            row["ranking"] = row["rank"]
            row["status"] = h.get("status") or ("SELECTED" if h.get("is_survivor") else "ACTIVE")
            if row["status"] == "SELECTED":
                row["status"] = "TOP"
            ranked.append(row)
        return ranked

    def export_markdown(run):
        meta = backend_models.get_run_meta(project, run["id"]) or {}
        lines = [f"# AgentFlow Run {run['id']}"]
        if meta.get("name"):
            lines.append(f"Name: {meta['name']}")
        lines += [f"Goal: {run['goal']}", f"Status: {run['status']}",
                  f"Iteration: {run['current_iteration']}", ""]
        if meta.get("description"):
            lines += [meta["description"], ""]
        for e in db.get_executions(run["id"], project=project):
            lines += [f"## Iteration {e['iteration']} — {e['agent']}",
                      f"Status: {e['status']}", "### Prompt", "```",
                      e["prompt"] or "(none)", "```", "### Output", "```",
                      e["output"] or "(none)", "```", ""]
            if e.get("top_hypothesis"):
                lines += [f"Top hypothesis: {e['top_hypothesis']}", ""]
        lines += ["## Finch (individual)", ""]
        for f in db.get_finch_runs(run["id"], project=project):
            lines += [f"### Run {f['run_index']}",
                      f"Vote: {f['parsed_vote']}", "```", f["raw_response"] or "",
                      "```"]
        cons = consensus_for(run)
        if cons:
            lines += ["## Consensus", f"Outcome: {cons['outcome']}",
                      f"YES:{cons['yes']} NO:{cons['no']} "
                      f"INCONCLUSIVE:{cons['inconclusive']} "
                      f"INVALID:{cons['invalid']}"]
        hyps = backend_models.list_hypotheses(project, run["id"])
        if hyps:
            lines += ["## Hypotheses", ""]
            for h in hyps:
                lines.append(f"- {h.get('title') or ''}: "
                             f"{h.get('description') or ''}")
        return "\n".join(lines) + "\n"

    @app.errorhandler(404)
    def _not_found(exc):
        if request.path.startswith("/api/"):
            return jsonify(error="not found"), 404
        return exc

    # -- index / health -----------------------------------------------------

    @app.route("/")
    def index():
        candidates = [
            frontend_dir / "templates" / "index.html",
            pkg_dir / "templates" / "index.html",
            pkg_dir / "static" / "index.html",
            pathlib.Path(project) / "templates" / "index.html",
            pathlib.Path(project) / "static" / "index.html",
            pathlib.Path(project) / "index.html",
        ]
        for path in candidates:
            if path.is_file():
                return send_file(str(path))
        return Response(_FALLBACK_INDEX, mimetype="text/html")

    @app.route("/api/health")
    def health():
        return jsonify(status="ok", ok=True, project=project)

    # -- agents -------------------------------------------------------------

    @app.route("/api/agents")
    def list_agents():
        agents = []
        for a in AGENTS:
            try:
                tmpl = load_template(a["id"])
                placeholders = sorted(set(re.findall(r"\{\{(\w+)\}\}", tmpl)))
            except FileNotFoundError:
                placeholders = []
            agents.append({"id": a["id"], "name": a["name"], "order": a["order"],
                           "agent": a["id"],
                           "description": a.get("description", ""),
                           "dependencies": a.get("dependencies", []),
                           "inputs": a.get("inputs", a.get("dependencies", [])),
                           "placeholders": placeholders})
        return jsonify(agents=agents)

    @app.route("/api/agents/<agent_id>", methods=["GET"])
    def get_agent_detail(agent_id):
        if agent_id not in AGENT_IDS:
            return err(f"unknown agent {agent_id}", 404)
        a = AGENT_BY_ID[agent_id]
        try:
            tmpl = load_template(agent_id)
            placeholders = sorted(set(re.findall(r"\{\{(\w+)\}\}", tmpl)))
        except FileNotFoundError:
            tmpl, placeholders = "", []
        return jsonify({"id": agent_id, "agent": agent_id, "name": a["name"],
                        "order": a["order"], "description": a.get("description", ""),
                        "dependencies": a.get("dependencies", []),
                        "inputs": a.get("inputs", a.get("dependencies", [])),
                        "template": tmpl, "content": tmpl,
                        "placeholders": placeholders})

    @app.route("/api/agents/<agent_id>/template", methods=["GET"])
    def get_template(agent_id):
        if agent_id not in AGENT_IDS:
            return err(f"unknown agent {agent_id}", 404)
        try:
            template = load_template(agent_id)
        except FileNotFoundError:
            return err(f"template missing for {agent_id}", 404)
        placeholders = sorted(set(re.findall(r"\{\{(\w+)\}\}", template)))
        return jsonify(agent=agent_id, id=agent_id,
                       name=next(a["name"] for a in AGENTS if a["id"] == agent_id),
                       template=template, content=template,
                       placeholders=placeholders)

    @app.route("/api/agents/<agent_id>/template", methods=["PUT"])
    def put_template(agent_id):
        if agent_id not in AGENT_IDS:
            return err(f"unknown agent {agent_id}", 404)
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        raw = data.get("template", data.get("content", data.get("text")))
        template, problem = valid_str(raw, "template", max_len=_MAX_TEMPLATE)
        if problem:
            return err(problem)
        found = re.findall(r"\{\{(\w+)\}\}", template)
        unknown = sorted(set(found) - set(PLACEHOLDERS))
        if unknown:
            return err(f"unknown placeholders: {', '.join(unknown)} "
                       f"(allowed: {', '.join(PLACEHOLDERS)})")
        residue = re.sub(r"\{\{\w+\}\}", "", template)
        if "{{" in residue or "}}" in residue:
            return err("unbalanced placeholder braces in template")
        path = pkg_dir / "prompts" / f"{agent_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template if template.endswith("\n") else template + "\n",
                        encoding="utf-8")
        return jsonify(agent=agent_id, id=agent_id, template=template,
                       content=template, placeholders=sorted(set(found)))

    # -- runs ---------------------------------------------------------------

    @app.route("/api/runs", methods=["GET"])
    def list_runs():
        out = []
        for r in db.list_runs(project):
            payload = run_payload(r)
            exes = current_exes(r)
            if _wf is not None:
                prog = _wf.compute_progress(r, exes)
                payload["progress"] = prog["percentage"]
                payload["completed"] = prog["completed"]
                payload["total"] = prog["total"]
                payload["next_agent"] = prog["next_agent"]
            out.append(payload)
        return jsonify(runs=out)

    @app.route("/api/runs", methods=["POST"])
    def create_run():
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        goal, problem = valid_str(data.get("goal"), "goal", max_len=_MAX_GOAL)
        if problem:
            return err(problem)
        name, problem = valid_str(data.get("name"), "name", required=False,
                                  max_len=200)
        if problem:
            return err(problem)
        description, problem = valid_str(data.get("description"), "description",
                                         required=False, max_len=_MAX_TEXT)
        if problem:
            return err(problem)
        run_id = db.create_run(goal, project)
        if name is not None or description is not None:
            backend_models.set_run_meta(project, run_id, name=name,
                                        description=description)
        run = db.get_run(run_id, project)
        try:  # CLI parity: supervisor prompt ready on creation
            render_and_save(run, "supervisor")
        except Exception:
            pass
        payload = run_payload(db.get_run(run_id, project))
        return jsonify({**payload, "run": payload}), 201

    @app.route("/api/runs/<run_id>", methods=["GET"])
    def get_run_detail(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        exes = current_exes(run)
        payload = run_payload(run, exes)
        prog = _wf.compute_progress(run, exes) if _wf is not None else {}
        return jsonify({
            **payload, "run": payload, "executions": exes,
            "finch_runs": db.get_finch_runs(run_id, project=project),
            "consensus": consensus_for(run),
            "progress": prog, "derived_status": prog.get("status") if prog else run.get("status"),
        })

    @app.route("/api/runs/<run_id>/status", methods=["GET"])
    def run_status(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        exes = current_exes(run)
        if _wf is not None:
            prog = _wf.compute_progress(run, exes, consensus=consensus_for(run))
        else:
            done, total, pct = progress_for(run, exes)
            prog = {"completed": done, "total": total, "percentage": pct,
                    "next_agent": next_agent(exes), "status": run["status"]}
        payload = run_payload(run, exes)
        return jsonify({
            "run_id": run_id, "id": run_id, "status": prog["status"],
            "stored_status": run["status"], "derived_status": prog["status"],
            "run_status": prog["status"], "progress": prog["percentage"], "percentage": prog["percentage"],
            "percent": prog["percentage"], "completed": prog["completed"], "completed_count": prog["completed"],
            "total": prog["total"], "total_agents": prog["total"],
            "pending": prog.get("pending", 0), "failed": prog.get("failed", 0), "blocked": prog.get("blocked", 0),
            "current_iteration": run["current_iteration"],
            "iteration": run["current_iteration"],
            "next_agent": prog.get("next_agent"),
            "completed_agents": prog.get("completed_agents", completed_agents(exes)),
            "pending_agents": prog.get("pending_agents", [e["agent"] for e in exes if e["status"] != "COMPLETED"]),
            "blocked_agents": [e["agent"] for e in exes if e["status"] == "BLOCKED"],
            "failed_agents": [e["agent"] for e in exes if e["status"] == "FAILED"],
            "consensus": consensus_for(run), "run": payload, "progress_detail": prog,
        })

    @app.route("/api/runs/<run_id>/archive", methods=["POST"])
    def archive_run(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        exes = current_exes(run)
        derived = _wf.derive_run_status(run, exes) if _wf is not None else run.get("status")
        if derived != "COMPLETED":
            return err(f"can only archive a COMPLETED run (current derived: {derived})", 409)
        try:
            db.set_run_status(run_id, "ARCHIVED", project)
        except ValueError as exc:
            return err(str(exc), 409)
        payload = run_payload(db.get_run(run_id, project), current_exes(db.get_run(run_id, project)))
        return jsonify({**payload, "run": payload})

    # -- generate / save ------------------------------------------------------

    @app.route("/api/runs/<run_id>/prompt-preview", methods=["POST", "GET"])
    def prompt_preview(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        data = body() if request.method == "POST" else {}
        agent, problem = valid_agent((data or {}).get("agent", (data or {}).get("agent_id", request.args.get("agent", request.args.get("agent_id")))))
        if problem:
            return err(problem)
        raw_data, problem = valid_str((data or {}).get("raw_data", request.args.get("raw_data")), "raw_data", required=False)
        if problem:
            return err(problem)
        top_arg, problem = valid_str((data or {}).get("top_hypothesis", request.args.get("top_hypothesis")), "top_hypothesis", required=False)
        if problem:
            return err(problem)
        exes = current_exes(run)
        # Sequence check first (clear warning, not fabricated)
        seq_problem = check_sequence(exes, agent)
        try:
            if _ops is not None:
                payload = _ops.build_prompt_preview(agent, run, exes, raw_data=raw_data or "", top_hypothesis=top_arg or top_hypothesis_for(run, exes), save=False, project=project)
            else:
                rendered, unresolved = render_and_save(run, agent, raw_data=raw_data or "", top_hypothesis=top_hypothesis_for(run, exes))
                payload = {"prompt": rendered, "unresolved": unresolved}
        except FileNotFoundError as exc:
            return err(str(exc), 404)
        execution = db.get_execution(run_id, agent, project=project)
        return jsonify(run_id=run_id, agent=agent,
                       prompt=payload.get("prompt"), preview=payload.get("prompt"),
                       unresolved=payload.get("unresolved", []),
                       missing=payload.get("missing", []),
                       warnings=payload.get("warnings", []),
                       dependencies=payload.get("dependencies", []),
                       placeholders=payload.get("placeholders", []),
                       sequence_ok=seq_problem is None,
                       sequence_warning=seq_problem,
                       execution=execution)

    @app.route("/api/runs/<run_id>/generate", methods=["POST"])
    def generate(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        agent, problem = valid_agent(data.get("agent", data.get("agent_id")))
        if problem:
            return err(problem)
        raw_data, problem = valid_str(data.get("raw_data"), "raw_data",
                                      required=False)
        if problem:
            return err(problem)
        exes = current_exes(run)
        problem = check_sequence(exes, agent)
        if problem:
            # Dependencies not satisfied -> BLOCKED (recoverable), no fabrication
            try:
                cur = db.get_execution(run_id, agent, project=project)
                if cur and cur.get("status") not in ("COMPLETED", "ARCHIVED"):
                    db.set_execution_status(run_id, agent, "BLOCKED", project)
            except Exception:
                pass
            return err(f"BLOCKED: {problem}", 409)
        # context-level dependency check (e.g. Crow without TOP_HYPOTHESIS)
        try:
            if _ops is not None:
                _preview = _ops.build_prompt_preview(agent, run, exes, raw_data=raw_data or "", top_hypothesis=top_hypothesis_for(run, exes), save=False, project=project)
                if _preview.get("missing"):
                    try:
                        cur = db.get_execution(run_id, agent, project=project)
                        if cur and cur.get("status") not in ("COMPLETED",):
                            # keep PENDING but report blocked; do not fabricate prompt as complete
                            pass
                    except Exception:
                        pass
                    return jsonify(error="BLOCKED: missing required dependencies", agent=agent,
                                   missing=_preview.get("missing", []), warnings=_preview.get("warnings", []),
                                   prompt=_preview.get("prompt"), ok=False), 409
        except Exception:
            pass
        try:
            if _ops is not None:
                payload = _ops.build_prompt_preview(agent, run, exes, raw_data=raw_data or "", top_hypothesis=top_hypothesis_for(run, exes), save=True, project=project)
                rendered = payload["prompt"]; unresolved = payload["unresolved"]
            else:
                rendered, unresolved = render_and_save(
                    run, agent, raw_data=raw_data or "",
                    top_hypothesis=top_hypothesis_for(run, exes))
                payload = {"warnings": [], "missing": unresolved, "dependencies": dependencies_for(agent)}
        except FileNotFoundError as exc:
            return err(str(exc), 404)
        execution = db.get_execution(run_id, agent, project=project)
        # Missing required dependencies: surface clear warning but still return prompt
        # with unresolved placeholders preserved — never fabricate.
        warnings = payload.get("warnings", [])
        missing = payload.get("missing", []) or unresolved
        if unresolved:
            for u in unresolved:
                if u not in missing:
                    warnings.append(f"Unresolved placeholder {{{{{u}}}}} — missing upstream output.")
        return jsonify(agent=agent, prompt=rendered, preview=rendered, unresolved=unresolved,
                       missing=missing, warnings=warnings,
                       dependencies=payload.get("dependencies", dependencies_for(agent)),
                       ok=len(missing) == 0,
                       execution=execution, run_id=run_id,
                       generated_at=execution.get("updated_at") if execution else None)

    @app.route("/api/runs/<run_id>/copy/<agent_id>", methods=["GET"])
    def copy_prompt(run_id, agent_id):
        """Return prompt text for client-side clipboard (COPY PROMPT)."""
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        if agent_id not in AGENT_IDS:
            return err(f"unknown agent {agent_id}", 404)
        execution = db.get_execution(run_id, agent_id, project=project)
        if execution is None or not execution.get("prompt"):
            return err(f"no prompt generated for {agent_id} — call GENERATE PROMPT first", 404)
        return jsonify(agent=agent_id, prompt=execution["prompt"],
                       copied=False, note="Client copies via navigator.clipboard. This endpoint supplies the source text.",
                       run_id=run_id)

    @app.route("/api/runs/<run_id>/retry", methods=["POST"])
    def retry_step(run_id):
        """RETRY STEP: reset slot to PENDING and regenerate a fresh prompt."""
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        agent, problem = valid_agent(data.get("agent", data.get("agent_id")))
        if problem:
            return err(problem)
        exes = current_exes(run)
        done = completed_agents(exes)
        idx = AGENT_IDS.index(agent)
        # RETRY allowed when agent is next (pending) OR already completed
        # (re-opening invalidates downstream — retry_execution truncates).
        # Only block when sequence itself is broken or agent is far ahead.
        try:
            validate_sequence(done)
        except ValueError as exc:
            return err(str(exc), 409)
        if idx > len(done):
            return err(f"agent {agent} is not reachable yet: expected {AGENT_IDS[len(done)]} next", 409)
        try:
            reset = db.retry_execution(run_id, agent, project)
        except AssertionError as exc:
            return err(str(exc), 404)
        # regenerate fresh prompt for the reset slot
        fresh_run = db.get_run(run_id, project)
        exes2 = current_exes(fresh_run)
        raw_data = ""
        try:
            if _ops is not None:
                payload = _ops.build_prompt_preview(agent, fresh_run, exes2, raw_data=raw_data, top_hypothesis=top_hypothesis_for(fresh_run, exes2), save=True, project=project)
                rendered = payload["prompt"]; unresolved = payload["unresolved"]
                warnings = payload.get("warnings", [])
            else:
                rendered, unresolved = render_and_save(fresh_run, agent, raw_data="", top_hypothesis=top_hypothesis_for(fresh_run, exes2))
                warnings = []
        except FileNotFoundError as exc:
            return err(str(exc), 404)
        execution = db.get_execution(run_id, agent, project=project)
        return jsonify(agent=agent, prompt=rendered, unresolved=unresolved,
                       warnings=warnings, execution=execution,
                       reset=reset, run_id=run_id)

    @app.route("/api/runs/<run_id>/history", methods=["GET"])
    def run_history(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        history = db.get_execution_history(run_id, project=project)
        return jsonify(history=history, executions=history, run_id=run_id,
                       count=len(history))

    @app.route("/api/runs/<run_id>/agent/<agent_id>/history", methods=["GET"])
    def agent_history(run_id, agent_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        if agent_id not in AGENT_IDS:
            return err(f"unknown agent {agent_id}", 404)
        history = db.get_execution_history(run_id, agent=agent_id, project=project)
        return jsonify(agent=agent_id, history=history, executions=history,
                       run_id=run_id, count=len(history))


    def _validated_save(run_id, run, data, strict_submit=False):
        """Shared core for SAVE OUTPUT / SUBMIT RESPONSE. Returns (payload, err)."""
        agent, problem = valid_agent(data.get("agent", data.get("agent_id")))
        if problem:
            return None, err(problem)
        output, problem = valid_str(data.get("output"), "output")
        if problem:
            return None, err(problem)
        top, problem = valid_str(data.get("top_hypothesis"), "top_hypothesis",
                                 required=False)
        if problem:
            return None, err(problem)
        raw_data, problem = valid_str(data.get("raw_data"), "raw_data", required=False)
        if problem:
            return None, err(problem)
        exes = current_exes(run)
        problem = check_sequence(exes, agent)
        if problem:
            try:
                cur = db.get_execution(run_id, agent, project=project)
                if cur and cur.get("status") not in ("COMPLETED",):
                    db.set_execution_status(run_id, agent, "BLOCKED", project)
            except Exception:
                pass
            return None, err(f"BLOCKED: {problem}", 409)
        overwrite = bool((data or {}).get("overwrite"))
        # Response validation: ranking needs winner signal if no top passed
        val_agent = agent
        ok, errors = db.validate_output(val_agent, output)
        # Ranking passes validation if caller supplied top_hypothesis explicitly
        if agent == "ranking" and top:
            if any("Top hypothesis" in e for e in errors):
                errors = [e for e in errors if "Top hypothesis" not in e]
                ok = len(errors) == 0
        if strict_submit and not ok:
            try:
                db.mark_execution_failed(run_id, agent, reason=";".join(errors), project=project)
            except Exception:
                pass
            return None, (jsonify(error="Response validation failed", agent=agent, errors=errors, ok=False), 422)
        if not strict_submit and not ok:
            hard = [e for e in errors if "placeholder" in e.lower() or "finch" in e.lower() or "empty" in e.lower()]
            if hard:
                try:
                    db.mark_execution_failed(run_id, agent, reason=";".join(errors), project=project)
                except Exception:
                    pass
                return None, (jsonify(error="Response validation failed", agent=agent, errors=errors, ok=False), 422)
            warnings = errors
        else:
            warnings = []
        try:
            db.save_output(run_id, agent, output, project, top_hypothesis=top, raw_data=raw_data, overwrite=overwrite)
            if agent in ("crow", "falcon"):
                try:
                    backend_models.sync_workspace_from_output(project, run_id, agent, run["current_iteration"], output)
                except Exception:
                    pass
        except ValueError as exc:
            return None, err(str(exc), 409)
        if top and agent == "ranking":
            db.save_top_hypothesis(run_id, top, project)
        execution = db.get_execution(run_id, agent, project=project)
        fresh = db.get_run(run_id, project)
        payload = {"agent": agent, "output": output, "execution": execution,
                   "saved_output": output,
                   "validation": {"ok": ok, "errors": errors, "warnings": warnings},
                   "ok": ok, "warnings": warnings,
                   "created_at": execution.get("created_at") if execution else None,
                   "updated_at": execution.get("updated_at") if execution else None,
                   "run": run_payload(fresh),
                   "next_agent": next_agent(current_exes(fresh)),
                   "run_id": run_id}
        return payload, None

    @app.route("/api/runs/<run_id>/save", methods=["POST"])
    def save(run_id):
        """SAVE OUTPUT: validate + persist the pasted agent response."""
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        payload, failure2 = _validated_save(run_id, run, data, strict_submit=False)
        if failure2:
            return failure2
        return jsonify(payload)

    @app.route("/api/runs/<run_id>/submit", methods=["POST"])
    def submit(run_id):
        """SUBMIT RESPONSE: strict validation then persist (manual submission)."""
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        payload, failure2 = _validated_save(run_id, run, data, strict_submit=True)
        if failure2:
            return failure2
        return jsonify(payload)


    @app.route("/api/runs/<run_id>/execution/<agent_id>", methods=["GET"])
    def get_execution(run_id, agent_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        if agent_id not in AGENT_IDS:
            return err(f"unknown agent {agent_id}", 404)
        execution = db.get_execution(run_id, agent_id, project=project)
        if execution is None:
            return err(f"no execution for {agent_id} in run {run_id}", 404)
        meta = AGENT_BY_ID.get(agent_id, {})
        exes = current_exes(run)
        try:
            tmpl = load_template(agent_id)
            placeholders = sorted(set(re.findall(r"\{\{(\w+)\}\}", tmpl)))
        except FileNotFoundError:
            tmpl, placeholders = "", []
        payload = {"id": agent_id, "agent": agent_id, "name": meta.get("name", agent_id),
                   "description": meta.get("description", ""),
                   "dependencies": meta.get("dependencies", []),
                   "placeholders": placeholders, "template": tmpl,
                   "status": execution.get("status"), "workflow_status": execution.get("status"),
                   "prompt": execution.get("prompt"), "output": execution.get("output"),
                   "saved_output": execution.get("output"),
                   "created_at": execution.get("created_at"), "updated_at": execution.get("updated_at"),
                   "iteration": execution.get("iteration"), "run_id": run_id,
                   "execution": execution}
        if _ops is not None:
            try:
                preview = _ops.build_prompt_preview(agent_id, run, exes, save=False, project=project)
                payload["preview"] = preview.get("prompt")
                payload["unresolved"] = preview.get("unresolved", [])
                payload["missing"] = preview.get("missing", [])
                payload["warnings"] = preview.get("warnings", [])
            except Exception:
                pass
        return jsonify(payload)

    @app.route("/api/runs/<run_id>/save-top", methods=["POST"])
    def save_top(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        top, problem = valid_str(
            data.get("top_hypothesis", data.get("text", data.get("hypothesis"))),
            "top_hypothesis")
        if problem:
            return err(problem)
        db.save_top_hypothesis(run_id, top, project)
        return jsonify(top_hypothesis=top, run_id=run_id,
                       execution=db.get_execution(run_id, "ranking",
                                                  project=project))

    # -- finch ----------------------------------------------------------------

    @app.route("/api/runs/<run_id>/finch", methods=["POST"])
    def post_finch(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        responses = data.get("responses")
        if responses is None:
            multi = data.get("multiblock", data.get("text", data.get("raw")))
            if not isinstance(multi, str) or not multi.strip():
                return err("provide {responses: [8 strings]} or "
                           "{multiblock: '---'-separated text}")
            parts = re.split(r"(?m)^[ \t]*---[ \t]*$", multi)
            if len(parts) == 1 and "---" in multi:
                parts = multi.split("---")
            responses = [p.strip() for p in parts if p.strip()]
        elif isinstance(responses, str):
            parts = re.split(r"(?m)^[ \t]*---[ \t]*$", responses)
            responses = [p.strip() for p in parts if p.strip()]
        if not isinstance(responses, list):
            return err("responses must be a list of 8 strings")
        if len(responses) != 8:
            return err(f"exactly 8 Finch responses required, "
                       f"got {len(responses)}")
        for i, r in enumerate(responses, 1):
            if not isinstance(r, str) or not r.strip():
                return err(f"Finch response {i}/8 is empty or not a string")
            if len(r) > _MAX_FINCH:
                return err(f"Finch response {i}/8 exceeds {_MAX_FINCH} characters")
        try:
            validate_finch_responses(responses)
        except ValueError as exc:
            return err(str(exc))
        votes = [parse_finch_vote(r) for r in responses]
        bad = [i + 1 for i, v in enumerate(votes) if v == "INVALID"]
        if bad:  # CLI parity: nothing is saved on invalid votes
            return err("invalid Finch responses at indices "
                       f"{bad} — each must contain "
                       "'Key finding: YES|NO|INCONCLUSIVE'")
        # Mark the finch execution completed *before* consensus so the
        # final run status comes from save_consensus.
        finch_exe = db.get_execution(run_id, "finch", project=project)
        if finch_exe is not None and finch_exe["status"] != "COMPLETED":
            db.save_output(run_id, "finch", "\n---\n".join(responses), project)
        db.save_finch_runs(run_id, responses, project)
        result = evaluate_consensus(votes)
        db.save_consensus(run_id, {
            "yes": result["yes"], "no": result["no"],
            "inconclusive": result["inconclusive"],
            "invalid": result["invalid"], "outcome": result["outcome"],
            "detail": json.dumps(result)}, project)
        return jsonify(consensus=consensus_payload(
            db.get_consensus(run_id, project=project)),
            finch_runs=db.get_finch_runs(run_id, project=project),
            outcome=result["outcome"], votes=votes, run_id=run_id)

    @app.route("/api/runs/<run_id>/finch", methods=["GET"])
    def get_finch(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        rows = db.get_finch_runs(run_id, project=project)
        lab = db.get_finch_lab(run_id, project=project)
        enriched = []
        for r in rows:
            row = dict(r)
            row["run_number"] = r.get("run_index")
            row["vote"] = r.get("parsed_vote")
            row["output"] = r.get("raw_response")
            row["text"] = r.get("raw_response")
            row["validation_status"] = r.get("validation_status") or ("VALID" if r.get("parsed_vote") in ("YES","NO","INCONCLUSIVE") else "INVALID")
            enriched.append(row)
        cons = consensus_for(run)
        finalized = bool(cons and cons.get("outcome"))
        outcome = (cons or {}).get("outcome") or lab.get("outcome") or ("INCOMPLETE — " + str(lab.get("valid_count", 0)) + "/8 valid" if not lab.get("complete") else lab.get("outcome"))
        return jsonify(finch_runs=enriched, finchRuns=enriched, finch=enriched,
                       runs=lab["runs"],
                       responses=[r.get("raw_response") for r in rows],
                       votes=[r.get("parsed_vote") for r in rows],
                       totals=lab["totals"], percentages=lab["percentages"],
                       valid_count=lab["valid_count"], invalid_count=lab["invalid_count"], complete=lab["complete"],
                       finalized=finalized, outcome=outcome,
                       consensus=cons, run_id=run_id)

    @app.route("/api/runs/<run_id>/finch/run/<int:run_index>", methods=["PUT", "PATCH", "POST"])
    def put_finch_run(run_id, run_index):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        if run_index < 1 or run_index > 8:
            return err(f"run_index must be 1-8, got {run_index}", 404)
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        raw = data.get("response", data.get("output", data.get("text", data.get("raw_response"))))
        if raw is None or not isinstance(raw, str) or not raw.strip():
            return err("response must be a non-empty string")
        if len(raw) > 20000:
            return err("response exceeds 20000 characters")
        try:
            saved = db.save_finch_run(run_id, run_index, raw, project)
        except ValueError as exc:
            return err(str(exc), 400)
        lab, result = db.recalc_consensus(run_id, project)
        payload_run = next((r for r in lab["runs"] if r["run_index"] == run_index), None)
        cons = consensus_for(db.get_run(run_id, project))
        return jsonify(run=payload_run, evaluation=payload_run, lab={k: lab[k] for k in ("totals", "percentages", "valid_count", "invalid_count", "complete", "outcome")},
                       finalized=bool(result), consensus=cons, run_id=run_id)

    @app.route("/api/runs/<run_id>/finch/recalc", methods=["POST"])
    def recalc_finch(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        lab, result = db.recalc_consensus(run_id, project)
        if result is None:
            cons = consensus_for(run)
            return jsonify(run_id=run_id, finalized=False, complete=False, valid_count=lab["valid_count"], invalid_count=lab["invalid_count"],
                           totals=lab["totals"], percentages=lab["percentages"],
                           outcome="INCOMPLETE — " + str(lab["valid_count"]) + "/8 valid (edit invalid runs, then recalculate)",
                           consensus=cons, lab=lab)
        return jsonify(run_id=run_id, finalized=True, complete=True, outcome=result["outcome"],
                       totals=lab["totals"], percentages=lab["percentages"],
                       consensus=consensus_for(db.get_run(run_id, project)), lab=lab, result=result)

    @app.route("/api/runs/<run_id>/finch/history", methods=["GET"])
    def finch_history(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        hist = db.get_finch_history(run_id, project=project)
        return jsonify(run_id=run_id, **hist)

    # -- hypotheses / ranking -------------------------------------------------

    @app.route("/api/runs/<run_id>/hypotheses", methods=["GET"])
    def list_hypotheses(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        hyps = hypotheses_for(run)
        return jsonify(hypotheses=hyps, run_id=run_id, count=len(hyps))

    def _validate_hypothesis_fields(data, partial=False):
        """Returns (fields, error).  Unknown keys are ignored."""
        fields = {}
        # evidence aliases
        if "supporting_evidence" in data and "evidence" not in data:
            data = dict(data); data["evidence"] = data["supporting_evidence"]
        if "source_agent" in data and "origin_agent" not in data:
            data = dict(data); data["origin_agent"] = data["source_agent"]
        if "survivability_score" in data and "survivability" not in data:
            data = dict(data); data["survivability"] = data["survivability_score"]
        for key in ("title", "description", "evidence", "strengths", "weaknesses", "notes"):
            if key in data and data[key] is not None:
                val, problem = valid_str(data[key], key, required=False, max_len=50000)
                if problem:
                    return None, problem
                fields[key] = val
        if "origin_agent" in data and data["origin_agent"] is not None:
            v = data["origin_agent"]
            if v == "manual":
                fields["origin_agent"] = "manual"
            else:
                agent, problem = valid_agent(v)
                if problem:
                    return None, "origin_agent must be a known agent or manual"
                fields["origin_agent"] = agent
        for key in ("score", "survivability"):
            if key in data and data[key] is not None:
                val = data[key]
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    return None, f"{key} must be a number"
                fval = float(val)
                if key == "survivability" and not (0 <= fval <= 10):
                    return None, "survivability must be 0-10 (no multi-agent scores fabricated)"
                fields[key] = fval
        elo_raw = data.get("elo_rating", data.get("elo"))
        if elo_raw is not None:
            if isinstance(elo_raw, bool) or not isinstance(elo_raw, (int, float)):
                return None, "elo_rating must be a number"
            fval = float(elo_raw)
            if not (0 <= fval <= 4000):
                return None, "elo_rating must be 0-4000"
            fields["elo_rating"] = fval
        if "status" in data and data["status"] is not None:
            st = str(data["status"]).strip().upper()
            if st not in backend_models.HYPOTHESIS_STATUSES:
                return None, f"status must be one of {backend_models.HYPOTHESIS_STATUSES}"
            fields["status"] = st
        if "ranking" in data and data["ranking"] is not None:
            val = data["ranking"]
            if isinstance(val, bool) or not isinstance(val, int):
                return None, "ranking must be an integer"
            fields["ranking"] = val
        if "is_survivor" in data and data["is_survivor"] is not None:
            val = data["is_survivor"]
            if not isinstance(val, (bool, int)) or val not in (True, False, 0, 1):
                return None, "is_survivor must be a boolean"
            fields["is_survivor"] = bool(val)
        if "iteration" in data and data["iteration"] is not None:
            val = data["iteration"]
            if isinstance(val, bool) or not isinstance(val, int) or val < 1:
                return None, "iteration must be a positive integer"
            fields["iteration"] = val
        if not partial and not fields.get("title") and not fields.get("description"):
            return None, "title (or description) is required"
        return fields, None

    @app.route("/api/runs/<run_id>/hypotheses", methods=["POST"])
    def add_hypothesis(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        fields, problem = _validate_hypothesis_fields(data)
        if problem:
            return err(problem)
        fields.setdefault("iteration", run["current_iteration"])
        if not fields.get("title"):
            fields["title"] = fields.get("description", "")
        created = backend_models.create_hypothesis(project, run_id, **fields)
        if (fields.get("status") or ("SELECTED" if fields.get("is_survivor") else "ACTIVE")) == "SELECTED":
            try:
                created, _top = backend_models.select_hypothesis(project, run_id, created["id"])
            except ValueError as exc:
                return err(str(exc), 409)
        return jsonify({**hypothesis_payload(created),
                        "hypothesis": hypothesis_payload(created)}), 201

    def _patch_hypothesis(run_id, hyp_id, data):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        existing = backend_models.get_hypothesis(project, hyp_id)
        if existing is None or existing["run_id"] != run_id:
            return err(f"hypothesis {hyp_id} not found in run {run_id}", 404)
        fields, problem = _validate_hypothesis_fields(data, partial=True)
        if problem:
            return err(problem)
        if not fields:
            return err("no editable fields provided")
        want_selected = (fields.get("status") == "SELECTED") or (fields.get("is_survivor") is True)
        archived_cur = (existing.get("status") or ("SELECTED" if existing.get("is_survivor") else "ACTIVE")) == "ARCHIVED"
        if want_selected and archived_cur:
            return err("archived hypotheses cannot be selected — restore first", 409)
        if want_selected:
            base = {k: v for k, v in fields.items() if k not in ("status", "is_survivor")}
            if base:
                backend_models.update_hypothesis(project, hyp_id, base)
            try:
                updated, _top = backend_models.select_hypothesis(project, run_id, hyp_id)
            except ValueError as exc:
                return err(str(exc), 409)
            payload = hypothesis_payload(updated)
            return jsonify({**payload, "hypothesis": payload})
        updated = backend_models.update_hypothesis(project, hyp_id, fields)
        payload = hypothesis_payload(updated)
        return jsonify({**payload, "hypothesis": payload})

    @app.route("/api/runs/<run_id>/hypotheses", methods=["PATCH"])
    def patch_hypothesis_collection(run_id):
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        hyp_id = data.get("id", data.get("hypothesis_id"))
        if isinstance(hyp_id, bool) or not isinstance(hyp_id, int):
            return err("hypothesis id is required for PATCH on the collection")
        return _patch_hypothesis(run_id, hyp_id, data)

    @app.route("/api/runs/<run_id>/hypotheses/<int:hyp_id>",
                methods=["PATCH", "PUT"])
    def patch_hypothesis_item(run_id, hyp_id):
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        return _patch_hypothesis(run_id, hyp_id, data)

    @app.route("/api/runs/<run_id>/hypotheses/<int:hyp_id>", methods=["GET"])
    def get_hypothesis_item(run_id, hyp_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        existing = backend_models.get_hypothesis(project, hyp_id)
        if existing is None or existing["run_id"] != run_id:
            return err(f"hypothesis {hyp_id} not found in run {run_id}", 404)
        payload = hypothesis_payload(existing)
        return jsonify({**payload, "hypothesis": payload})

    @app.route("/api/runs/<run_id>/hypotheses/import", methods=["POST"])
    def import_hypotheses(run_id):
        """Import from agent outputs; parse failure -> manual-entry guidance."""
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        exes = db.get_executions(run_id, iteration=run["current_iteration"], project=project)
        requested = data.get("agent") or data.get("agent_id")
        it = data.get("iteration", run["current_iteration"])
        if isinstance(it, bool) or not isinstance(it, int) or it < 1:
            return err("iteration must be a positive integer")
        if requested:
            if requested not in AGENT_IDS:
                return err(f"unknown agent {requested}", 404)
            row = next((e for e in exes if e["agent"] == requested), None)
            if not row or row.get("status") != "COMPLETED" or not row.get("output"):
                return jsonify(imported=0, parse_ok=False, found=0, per_agent=[],
                               message=f"No completed output for {requested} — save that agent's response first, or enter hypotheses manually (no data fabricated).")
            report = backend_models.import_from_agent_output(project, run_id, it, requested, row["output"])
            return jsonify(run_id=run_id, agent=requested, **report)
        report = backend_models.import_hypotheses_from_outputs(project, run_id, it, exes)
        return jsonify(run_id=run_id, **report)

    @app.route("/api/runs/<run_id>/hypotheses/compare", methods=["GET", "POST"])
    def compare_hypotheses(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        b = body() if request.method == "POST" else {}
        q = dict(request.args)
        src = (b or {}).copy()
        for k in ("a", "b"):
            if k not in src and q.get(k) is not None:
                src[k] = q.get(k)
        def _int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        a_id, b_id = _int(src.get("a", src.get("a_id"))), _int(src.get("b", src.get("b_id")))
        if a_id is None or b_id is None:
            return err("provide both a and b hypothesis IDs (?a=1&b=2 or {a,b})")
        ha = backend_models.get_hypothesis(project, a_id)
        hb = backend_models.get_hypothesis(project, b_id)
        if ha is None or ha["run_id"] != run_id or hb is None or hb["run_id"] != run_id:
            return err(f"hypothesis not found in run {run_id}", 404)
        winner = (b or {}).get("winner", (b or {}).get("winner_id"))
        pa, pb = hypothesis_payload(ha), hypothesis_payload(hb)
        cmp_payload = backend_models.compare_hypotheses(pa, pb)
        updated = {}
        if winner is not None:
            wid = _int(winner)
            if wid not in (a_id, b_id):
                return err("winner must be a or b")
            lid = b_id if wid == a_id else a_id
            try:
                w, l = backend_models.record_compare_win(project, wid, lid)
            except ValueError as exc:
                return err(str(exc), 404)
            cmp_payload = backend_models.compare_hypotheses(hypothesis_payload(w), hypothesis_payload(l))
            cmp_payload["verdict"] = {"winner": wid, "loser": lid, "note": "ELO updated by K=32 for explicit pairwise win (no auto-tournament)"}
            updated = {"winner": hypothesis_payload(w), "loser": hypothesis_payload(l)}
        return jsonify(run_id=run_id, a=cmp_payload["a"], b=cmp_payload["b"], deltas=cmp_payload["deltas"],
                       verdict=cmp_payload["verdict"], comparison=cmp_payload, **updated)

    @app.route("/api/runs/<run_id>/hypotheses/<int:hyp_id>/select", methods=["POST"])
    def select_hypothesis_item(run_id, hyp_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        try:
            updated, top_text = backend_models.select_hypothesis(project, run_id, hyp_id)
        except ValueError as exc:
            return err(str(exc), 409)
        payload = hypothesis_payload(updated)
        return jsonify({**payload, "hypothesis": payload,
                        "top_hypothesis": top_text, "run_id": run_id,
                        "crow_available": bool(top_text),
                        "note": "TOP_HYPOTHESIS saved to SQLite — Crow prompt will embed it."})

    @app.route("/api/runs/<run_id>/hypotheses/<int:hyp_id>/archive", methods=["POST"])
    def archive_hypothesis_item(run_id, hyp_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        existing = backend_models.get_hypothesis(project, hyp_id)
        if existing is None or existing["run_id"] != run_id:
            return err(f"hypothesis {hyp_id} not found in run {run_id}", 404)
        if (existing.get("status") or ("SELECTED" if existing.get("is_survivor") else "ACTIVE")) == "ARCHIVED":
            return err("hypothesis already archived", 409)
        was_selected = (existing.get("status") or ("SELECTED" if existing.get("is_survivor") else "ACTIVE")) == "SELECTED"
        updated = backend_models.archive_hypothesis(project, hyp_id)
        payload = hypothesis_payload(updated)
        # If archived top, clear Crow top hint so Crow does not embed stale winner
        if was_selected:
            try:
                ranking = db.get_execution(run_id, "ranking", project=project)
                if ranking and ranking.get("top_hypothesis"):
                    # only clear if it references this hypothesis title
                    if (updated.get("title") or "") in (ranking.get("top_hypothesis") or ""):
                        import sqlite3
                        con = db._connect(project)
                        con.execute("UPDATE executions SET top_hypothesis=NULL WHERE run_id=? AND agent='ranking'", (run_id,))
                        con.commit(); con.close()
            except Exception:
                pass
        return jsonify({**payload, "hypothesis": payload})

    @app.route("/api/runs/<run_id>/hypotheses/<int:hyp_id>/restore", methods=["POST"])
    def restore_hypothesis_item(run_id, hyp_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        existing = backend_models.get_hypothesis(project, hyp_id)
        if existing is None or existing["run_id"] != run_id:
            return err(f"hypothesis {hyp_id} not found in run {run_id}", 404)
        try:
            updated = backend_models.restore_hypothesis(project, hyp_id)
        except ValueError as exc:
            return err(str(exc), 409)
        payload = hypothesis_payload(updated)
        return jsonify({**payload, "hypothesis": payload})

    @app.route("/api/runs/<run_id>/ranking", methods=["GET"])
    def get_ranking(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        exes = current_exes(run)
        ranking_exe = next((e for e in exes if e["agent"] == "ranking"), None)
        ranked = ranked_payload(run)
        # ELO visualization from actual stored values (unrated -> pct null)
        elo_visual = []
        for h in ranked:
            elo = h.get("elo_rating")
            elo_visual.append({"id": h.get("id"), "label": h.get("label"),
                               "title": h.get("title"), "elo": h.get("elo_rating"),
                               "elo_rating": h.get("elo_rating"), "elo_pct": h.get("elo_pct"),
                               "rank": h.get("rank")})
        # comparative results from Ranking tournament lines (persisted)
        try:
            matches = backend_models.sync_ranking_matches(project, run_id, run["current_iteration"],
                                                          (ranking_exe or {}).get("output") or "")
        except Exception:
            matches = []
        # ranking changes + history
        try:
            history = backend_models.get_ranking_history(project, run_id)
        except Exception:
            history = {"snapshots": [], "events": [], "matches": matches}
        events = history.get("events", [])
        # winning hypothesis: SELECTED row else top_hypothesis text
        winning = None
        for h in ranked:
            if h.get("status") == "TOP" or h.get("status") == "SELECTED":
                winning = h
                break
        top_text = top_hypothesis_for(run, exes) or None
        if winning is None and top_text:
            winning = {"title": top_text, "top_hypothesis": top_text}
        elif winning is not None:
            winning = dict(winning)
            winning["top_hypothesis"] = top_text or winning.get("title")
        # Crow availability: prompt context actually contains top (not placeholder)
        crow_ok = bool(top_text)
        return jsonify({
            "run_id": run_id, "agent": "ranking",
            "ranking": ranked, "ranked": ranked, "hypotheses": ranked,
            "leaderboard": ranked,
            "elo_visual": elo_visual,
            "ranking_changes": events,
            "leaderboard_changes": events,
            "comparative_results": matches,
            "comparative": matches,
            "winning_hypothesis": winning,
            "winner": winning,
            "top_hypothesis": top_text,
            "crow_available": crow_ok,
            "crow": {"available": crow_ok, "top_hypothesis": top_text},
            "ranking_history": history,
            "history": history,
            "ranking_output": (ranking_exe or {}).get("output"),
            "output": (ranking_exe or {}).get("output"),
            "prompt": (ranking_exe or {}).get("prompt"),
            "execution": ranking_exe,
            "correction_hint": "Manual correction: PATCH /hypotheses/<id> {ranking|elo_rating|score|survivability} or POST /ranking/reorder {order:[ids]}. All changes recorded in ranking_changes.",
        })

    @app.route("/api/runs/<run_id>/ranking/reorder", methods=["POST"])
    def ranking_reorder(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        order = data.get("order") or data.get("ids") or data.get("ordered_ids")
        if not isinstance(order, list) or not order:
            return err("provide {order: [hypothesis ids in new rank order]}")
        cleaned = []
        for x in order:
            if isinstance(x, bool) or not isinstance(x, int):
                return err("order must be a list of hypothesis ids (integers)")
            cleaned.append(x)
        it = data.get("iteration", run["current_iteration"])
        if isinstance(it, bool) or not isinstance(it, int) or it < 1:
            return err("iteration must be a positive integer")
        try:
            updated = backend_models.reorder_hypotheses(project, run_id, it, cleaned)
        except ValueError as exc:
            return err(str(exc), 400)
        ranked = ranked_payload(db.get_run(run_id, project))
        return jsonify(run_id=run_id, ranking=ranked, ranked=ranked, hypotheses=ranked,
                       reordered=[u["id"] for u in updated])

    @app.route("/api/runs/<run_id>/ranking/history", methods=["GET"])
    def ranking_history(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        try:
            hist = backend_models.get_ranking_history(project, run_id)
        except Exception as exc:
            return err(str(exc), 500)
        # ensure matches synced for current iteration
        exes = current_exes(run)
        ranking_exe = next((e for e in exes if e["agent"] == "ranking"), None)
        try:
            backend_models.sync_ranking_matches(project, run_id, run["current_iteration"], (ranking_exe or {}).get("output") or "")
            hist = backend_models.get_ranking_history(project, run_id)
        except Exception:
            pass
        return jsonify(run_id=run_id, **hist)

    # -- Crow / Falcon workspaces (PHASE 9) ---------------------------------
    def _workspace_payload(run, workspace):
        it = run["current_iteration"]
        notes, ts = backend_models.get_workspace_notes(project, run["id"], workspace, it)
        exe = db.get_execution(run["id"] if isinstance(run, dict) else run.get("id"), workspace, project=project) if isinstance(run, dict) else None
        exes = current_exes(run) if isinstance(run, dict) else []
        top = top_hypothesis_for(run, exes) if isinstance(run, dict) else ""
        # Crow selected hypothesis: SELECTED review first, else top text
        selected = top or None
        labels = backend_models.WORKSPACE_LABELS.get(workspace, {})
        sections = [{"field": k, "label": labels.get(k, k), "value": notes.get(k, ""), "updated_at": ts.get(k)} for k in backend_models.WORKSPACE_FIELDS.get(workspace, [])]
        return {"workspace": workspace, "run_id": run["id"] if isinstance(run, dict) else None,
                "iteration": it, "goal": run.get("goal") if isinstance(run, dict) else None,
                "selected_hypothesis": selected if workspace == "crow" else (top or None),
                "top_hypothesis": top or None,
                "sections": sections, "notes": notes,
                "prompt": (exe or {}).get("prompt"), "raw_output": (exe or {}).get("output"),
                "saved_output": (exe or {}).get("output"), "status": (exe or {}).get("status", "PENDING"),
                "created_at": (exe or {}).get("created_at"), "updated_at": (exe or {}).get("updated_at"),
                "history": db.get_execution_history(run["id"], agent=workspace, project=project) if isinstance(run, dict) else []}

    @app.route("/api/runs/<run_id>/workspace/<workspace>", methods=["GET"])
    def get_workspace(run_id, workspace):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        if workspace not in backend_models.WORKSPACE_FIELDS:
            return err(f"unknown workspace {workspace}", 404)
        return jsonify(_workspace_payload(run, workspace))

    @app.route("/api/runs/<run_id>/workspace/<workspace>/parse", methods=["POST"])
    def parse_workspace(run_id, workspace):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        if workspace not in backend_models.WORKSPACE_FIELDS:
            return err(f"unknown workspace {workspace}", 404)
        data = body()
        text = (data or {}).get("output") if data else None
        exe = db.get_execution(run_id, workspace, project=project)
        text = text or (exe or {}).get("output") or ""
        report = backend_models.sync_workspace_from_output(project, run_id, workspace, run["current_iteration"], text)
        return jsonify(run_id=run_id, workspace=workspace, **report, payload=_workspace_payload(run, workspace))

    @app.route("/api/runs/<run_id>/workspace/<workspace>", methods=["PATCH"])
    def patch_workspace(run_id, workspace):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        if workspace not in backend_models.WORKSPACE_FIELDS:
            return err(f"unknown workspace {workspace}", 404)
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        notes = data.get("sections") or data.get("notes") or data
        cleaned = {}
        allowed = set(backend_models.WORKSPACE_FIELDS[workspace])
        for k, v in (notes or {}).items():
            if k not in allowed:
                continue
            if v is not None and not isinstance(v, str):
                return err(f"{k} must be a string")
            if v is not None and len(v) > 100000:
                return err(f"{k} exceeds 100000 characters")
            cleaned[k] = v or ""
        if not cleaned:
            return err("no editable sections provided")
        notes2, _ = backend_models.set_workspace_notes(project, run_id, workspace, cleaned, run["current_iteration"])
        return jsonify(run_id=run_id, workspace=workspace, notes=notes2, payload=_workspace_payload(run, workspace))

    @app.route("/api/runs/<run_id>/workspace/<workspace>/generate", methods=["POST"])
    def generate_workspace(run_id, workspace):
        # Delegate to core generate checks (sequence + missing deps), then return workspace view
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        if workspace not in backend_models.WORKSPACE_FIELDS:
            return err(f"unknown workspace {workspace}", 404)
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        exes = current_exes(run)
        problem = check_sequence(exes, workspace)
        if problem:
            try:
                cur = db.get_execution(run_id, workspace, project=project)
                if cur and cur.get("status") not in ("COMPLETED",):
                    db.set_execution_status(run_id, workspace, "BLOCKED", project)
            except Exception:
                pass
            return err(f"BLOCKED: {problem}", 409)
        try:
            payload = _ops.build_prompt_preview(workspace, run, exes, raw_data="", top_hypothesis=top_hypothesis_for(run, exes), save=True, project=project)
        except FileNotFoundError as exc:
            return err(str(exc), 404)
        return jsonify(run_id=run_id, workspace=workspace, prompt=payload["prompt"], unresolved=payload["unresolved"], warnings=payload["warnings"], missing=payload["missing"], payload=_workspace_payload(run, workspace))



    # -- timeline / export / loop / diff --------------------------------------

    @app.route("/api/runs/<run_id>/timeline", methods=["GET"])
    def timeline(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        events = [{"timestamp": run.get("created_at"), "type": "run_created",
                   "agent": None, "iteration": run["current_iteration"],
                   "status": run["status"],
                   "message": f"Run {run_id} created"}]
        for e in db.get_executions(run_id, project=project):
            events.append({
                "timestamp": e.get("updated_at") or e.get("created_at"),
                "type": ("agent_completed" if e["status"] == "COMPLETED"
                         else "agent_created"),
                "agent": e["agent"], "iteration": e["iteration"],
                "status": e["status"],
                "message": f"{e['agent']} (iteration {e['iteration']}) "
                           f"{e['status'].lower()}"})
        for f in db.get_finch_runs(run_id, project=project):
            events.append({
                "timestamp": f.get("created_at"), "type": "finch_run",
                "agent": "finch", "iteration": f["iteration"],
                "status": f.get("parsed_vote"),
                "message": f"Finch run {f['run_index']}: "
                           f"{f.get('parsed_vote')}"})
        cons = consensus_for(run)
        if cons:
            events.append({
                "timestamp": cons.get("created_at"), "type": "consensus",
                "agent": "finch", "iteration": cons.get("iteration"),
                "status": cons.get("outcome"),
                "message": f"Consensus: {cons.get('outcome')}"})
        for h in backend_models.list_hypotheses(project, run_id):
            events.append({
                "timestamp": h.get("created_at"), "type": "hypothesis",
                "agent": h.get("origin_agent"), "iteration": h.get("iteration"),
                "status": "survivor" if h.get("is_survivor") else "created",
                "message": h.get("title") or "Hypothesis"})
        events.sort(key=lambda e: e.get("timestamp") or "")
        return jsonify(timeline=events, events=events, run_id=run_id)

    @app.route("/api/runs/<run_id>/export.md", methods=["GET"])
    def export_md(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        return Response(export_markdown(run), mimetype="text/markdown",
                        headers={"Content-Disposition":
                                 f"inline; filename=export-{run_id}.md"})

    @app.route("/api/runs/<run_id>/loop", methods=["POST"])
    def loop(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        if db.get_consensus(run_id, project=project) is None:
            return err("cannot loop before Finch consensus", 409)
        prev_it = run["current_iteration"]
        # inheritance captured BEFORE increment (previous findings preserved, statuses NOT copied)
        prev_summary = backend_models.iteration_summary(project, run_id, prev_it)
        prev_exes = db.get_executions(run_id, iteration=prev_it, project=project)
        inherited_top = prev_summary.get("top_hypothesis") or top_hypothesis_for(run, prev_exes) or ""
        inherited_cons = consensus_payload(db.get_consensus(run_id, project=project)) if db.get_consensus(run_id, project=project) else None
        try:
            new_it = db.new_iteration(run_id, project)
        except AssertionError as exc:
            return err(str(exc), 409)
        fresh = db.get_run(run_id, project)
        # Fresh Supervisor prompt for the new iteration (inherits goal + previous consensus + top)
        prompt, unresolved, sup_payload = "", [], None
        try:
            fresh_exes = current_exes(fresh)
            if _ops is not None:
                sup_payload = _ops.build_prompt_preview("supervisor", fresh, fresh_exes, save=True, project=project)
                prompt, unresolved = sup_payload["prompt"], sup_payload["unresolved"]
            else:
                prompt, unresolved = render_and_save(fresh, "supervisor")
        except Exception:
            pass
        fresh_status = db.get_execution(run_id, "supervisor", project=project)
        payload = run_payload(fresh)
        return jsonify({**payload, "run": payload, "iteration": new_it,
                        "current_iteration": new_it,
                        "inherited": {"goal": fresh["goal"], "previous_iteration": prev_it,
                                      "previous_consensus": inherited_cons, "previous_top_hypothesis": inherited_top,
                                      "previous_unresolved": prev_summary.get("unresolved", [])},
                        "supervisor_prompt": prompt, "supervisor_unresolved": unresolved,
                        "supervisor_status": (fresh_status or {}).get("status"),
                        "preserved": {"previous_completed": prev_summary.get("completed", []), "previous_hypothesis_count": len(prev_summary.get("hypotheses", []))}})

    @app.route("/api/runs/<run_id>/iterations", methods=["GET"])
    def list_iterations(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        its = backend_models.list_iterations(project, run_id)
        out = []
        for it in its:
            exes = db.get_executions(run_id, iteration=it, project=project)
            done = sum(1 for e in exes if e["status"] == "COMPLETED")
            out.append({"iteration": it, "current": it == run["current_iteration"], "completed": done,
                        "total": len(exes), "consensus": consensus_payload(db.get_consensus(run_id, iteration=it, project=project)) if db.get_consensus(run_id, iteration=it, project=project) else None,
                        "summary": {"top_hypothesis": backend_models.iteration_summary(project, run_id, it).get("top_hypothesis"), "hypothesis_count": len(backend_models.list_hypotheses(project, run_id, iteration=it))}})
        return jsonify(run_id=run_id, iterations=out)

    @app.route("/api/runs/<run_id>/iterations/switch", methods=["POST"])
    def switch_iteration(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        it = data.get("iteration", data.get("to", data.get("current_iteration")))
        if isinstance(it, bool) or not isinstance(it, int) or it < 1:
            return err("iteration must be a positive integer")
        its = backend_models.list_iterations(project, run_id)
        if it not in its:
            return err(f"iteration {it} not found (available: {its})", 404)
        con = db._connect(project)
        con.execute("UPDATE runs SET current_iteration=?, updated_at=? WHERE id=?", (it, db._now(), run_id))
        con.commit(); con.close()
        fresh = db.get_run(run_id, project)
        payload = run_payload(fresh)
        return jsonify({**payload, "run": payload, "iteration": it, "current_iteration": it})

    @app.route("/api/runs/<run_id>/iterations/compare", methods=["GET", "POST"])
    def compare_iterations_ep(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        src = dict(request.args)
        if request.method == "POST":
            b = body() or {}
            src.update({k: v for k, v in b.items() if v is not None})
        def _int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        a = _int(src.get("a", src.get("iteration_a")))
        b = _int(src.get("b", src.get("iteration_b")))
        its = backend_models.list_iterations(project, run_id)
        if a is None or b is None:
            return err(f"provide a and b iteration numbers (available: {its})")
        if a not in its or b not in its:
            return err(f"iterations {a},{b} not found (available: {its})", 404)
        cmp_payload = backend_models.compare_iterations(project, run_id, a, b)
        # frontend-friendly flat
        return jsonify(run_id=run_id, a=a, b=b,
                       hypothesis_changes=cmp_payload["hypothesis_changes"],
                       ranking_changes=cmp_payload["ranking_changes"],
                       consensus_change=cmp_payload["consensus_change"],
                       new_findings=cmp_payload["new_findings"],
                       unresolved=cmp_payload["unresolved"],
                       comparison=cmp_payload)

    @app.route("/api/runs/<run_id>/diff/<other_id>", methods=["GET"])
    def diff(run_id, other_id):
        run_a, failure = get_run_or_404(run_id)
        if failure:
            return failure
        run_b, failure = get_run_or_404(other_id)
        if failure:
            return failure
        exes_a = current_exes(run_a)
        exes_b = current_exes(run_b)
        by_agent_b = {e["agent"]: e for e in exes_b}
        agents_cmp = []
        for e in exes_a:
            other = by_agent_b.get(e["agent"], {})
            agents_cmp.append({
                "agent": e["agent"],
                "a_status": e["status"], "b_status": other.get("status"),
                "a_output": e.get("output"), "b_output": other.get("output"),
                "same": (e.get("output") or "") == (other.get("output") or ""),
            })
        cons_a, cons_b = consensus_for(run_a), consensus_for(run_b)
        payload_a, payload_b = run_payload(run_a), run_payload(run_b)
        return jsonify({
            "run_a": payload_a, "run_b": payload_b, "a": payload_a,
            "b": payload_b,
            "differences": {
                "goal_same": run_a["goal"] == run_b["goal"],
                "status_a": run_a["status"], "status_b": run_b["status"],
                "iteration_a": run_a["current_iteration"],
                "iteration_b": run_b["current_iteration"],
                "completed_a": sum(1 for e in exes_a
                                   if e["status"] == "COMPLETED"),
                "completed_b": sum(1 for e in exes_b
                                   if e["status"] == "COMPLETED"),
                "consensus_a": (cons_a or {}).get("outcome"),
                "consensus_b": (cons_b or {}).get("outcome"),
            },
            "agents": agents_cmp, "executions_a": exes_a,
            "executions_b": exes_b,
        })

    return app


def main():
    app = create_app(project=".")
    app.run(host=HOST, port=PORT)


if __name__ == "__main__":
    main()
