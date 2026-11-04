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
    AGENTS, AGENT_IDS, PLACEHOLDERS, build_context, load_template,
    render_prompt, validate_sequence,
)
from agentflow.consensus import (
    evaluate_consensus, parse_finch_vote, validate_finch_responses,
)

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

    def run_payload(run):
        meta = backend_models.get_run_meta(project, run["id"]) or {}
        payload = dict(run)
        payload["run_id"] = run["id"]
        payload["name"] = meta.get("name")
        payload["description"] = meta.get("description")
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
        ranking = next((e for e in exes if e["agent"] == "ranking"), None)
        if ranking and ranking.get("output"):
            m = re.search(r"Top hypothesis:\s*(.+)", ranking["output"], re.I)
            if m:
                return m.group(1).strip()
        return ""

    def next_agent(exes):
        for e in exes:
            if e["status"] in ("PENDING", "WAITING", "FAILED"):
                return e["agent"]
        return None

    def progress_for(run, exes):
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
        out["text"] = text
        out["hypothesis"] = text
        out["agent"] = out.get("origin_agent")
        out["source"] = out.get("origin_agent")
        out["rank"] = out.get("ranking")
        out["survivor"] = bool(out.get("is_survivor"))
        out["top"] = bool(out.get("is_survivor"))
        out["is_top"] = bool(out.get("is_survivor"))
        # label like "H3" from a parsed title prefix, when present
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
        items = hypotheses_for(run)
        items = sorted(items, key=lambda h: (
            h.get("ranking") is None, h.get("ranking") or 0))
        ranked = []
        for i, h in enumerate(items, start=1):
            row = dict(h)
            row["rank"] = h.get("ranking") or i
            row["ranking"] = row["rank"]
            row["status"] = "TOP" if h.get("is_survivor") else ""
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
        agents = [{"id": a["id"], "name": a["name"], "order": a["order"],
                   "agent": a["id"]} for a in AGENTS]
        return jsonify(agents=agents)

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
        runs = [run_payload(r) for r in db.list_runs(project)]
        return jsonify(runs=runs)

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
        payload = run_payload(run)
        return jsonify({
            **payload, "run": payload, "executions": exes,
            "finch_runs": db.get_finch_runs(run_id, project=project),
            "consensus": consensus_for(run),
        })

    @app.route("/api/runs/<run_id>/status", methods=["GET"])
    def run_status(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        exes = current_exes(run)
        done, total, pct = progress_for(run, exes)
        payload = run_payload(run)
        return jsonify({
            "run_id": run_id, "id": run_id, "status": run["status"],
            "run_status": run["status"], "progress": pct, "percentage": pct,
            "percent": pct, "completed": done, "completed_count": done,
            "total": total, "total_agents": total,
            "current_iteration": run["current_iteration"],
            "iteration": run["current_iteration"],
            "next_agent": next_agent(exes),
            "completed_agents": completed_agents(exes),
            "pending_agents": [e["agent"] for e in exes
                               if e["status"] != "COMPLETED"],
            "consensus": consensus_for(run), "run": payload,
        })

    # -- generate / save ------------------------------------------------------

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
            return err(problem, 409)
        try:
            rendered, unresolved = render_and_save(
                run, agent, raw_data=raw_data or "",
                top_hypothesis=top_hypothesis_for(run, exes))
        except FileNotFoundError as exc:
            return err(str(exc), 404)
        execution = db.get_execution(run_id, agent, project=project)
        return jsonify(agent=agent, prompt=rendered, unresolved=unresolved,
                       execution=execution, run_id=run_id)

    @app.route("/api/runs/<run_id>/save", methods=["POST"])
    def save(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        data = body()
        if data is None:
            return err("request body must be a JSON object")
        agent, problem = valid_agent(data.get("agent", data.get("agent_id")))
        if problem:
            return err(problem)
        output, problem = valid_str(data.get("output"), "output")
        if problem:
            return err(problem)
        top, problem = valid_str(data.get("top_hypothesis"), "top_hypothesis",
                                 required=False)
        if problem:
            return err(problem)
        exes = current_exes(run)
        problem = check_sequence(exes, agent)
        if problem:
            return err(problem, 409)
        db.save_output(run_id, agent, output, project, top_hypothesis=top)
        if top and agent == "ranking":
            db.save_top_hypothesis(run_id, top, project)
        execution = db.get_execution(run_id, agent, project=project)
        fresh = db.get_run(run_id, project)
        return jsonify(agent=agent, output=output, execution=execution,
                       run=run_payload(fresh),
                       next_agent=next_agent(current_exes(fresh)),
                       run_id=run_id)

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
        return jsonify({**execution, "execution": execution, "agent": agent_id,
                        "run_id": run_id})

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
        enriched = []
        for r in rows:
            row = dict(r)
            row["vote"] = r.get("parsed_vote")
            row["output"] = r.get("raw_response")
            row["text"] = r.get("raw_response")
            enriched.append(row)
        return jsonify(finch_runs=enriched, finchRuns=enriched, finch=enriched,
                       responses=[r.get("raw_response") for r in rows],
                       votes=[r.get("parsed_vote") for r in rows],
                       consensus=consensus_for(run), run_id=run_id)

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
        for key in ("title", "description", "strengths", "weaknesses", "notes"):
            if key in data:
                val, problem = valid_str(data[key], key, required=False)
                if problem:
                    return None, problem
                fields[key] = val
        if "origin_agent" in data and data["origin_agent"] is not None:
            agent, problem = valid_agent(data["origin_agent"])
            if problem:
                return None, "origin_agent must be a known agent"
            fields["origin_agent"] = agent
        for key in ("score", "survivability"):
            if key in data and data[key] is not None:
                val = data[key]
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    return None, f"{key} must be a number"
                fields[key] = float(val)
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

    @app.route("/api/runs/<run_id>/ranking", methods=["GET"])
    def get_ranking(run_id):
        run, failure = get_run_or_404(run_id)
        if failure:
            return failure
        exes = current_exes(run)
        ranking_exe = next((e for e in exes if e["agent"] == "ranking"), None)
        ranked = ranked_payload(run)
        return jsonify({
            "run_id": run_id, "agent": "ranking",
            "ranking": ranked, "ranked": ranked, "hypotheses": ranked,
            "ranking_output": (ranking_exe or {}).get("output"),
            "output": (ranking_exe or {}).get("output"),
            "prompt": (ranking_exe or {}).get("prompt"),
            "top_hypothesis": top_hypothesis_for(run, exes) or None,
            "execution": ranking_exe,
        })

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
        try:
            new_it = db.new_iteration(run_id, project)
        except AssertionError as exc:
            return err(str(exc), 409)
        fresh = db.get_run(run_id, project)
        payload = run_payload(fresh)
        return jsonify({**payload, "run": payload, "iteration": new_it,
                        "current_iteration": new_it})

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
