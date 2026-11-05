"""Phase 5 — Agent Operations Engine helpers.

Pure functions shared by the Flask API (and reusable by the CLI):
response validation, dependency resolution, prompt preview building.
No network, no shell — SQLite via agentflow.db only.
"""
import re

from agentflow.agents import (AGENT_BY_ID, build_context, dependencies_for,
                              load_template, missing_dependencies, render_prompt)
from agentflow import db as dbmod

MAX_OUTPUT = 100_000

def validate_response(agent: str, output: str):
    """Return (ok, errors[]). Stricter wrapper over db.validate_output."""
    return dbmod.validate_output(agent, output)

def resolve_dependencies(agent_id: str, run, executions, raw_data="", top_hypothesis=""):
    """Build prompt context from REAL preceding outputs only.

    Returns dict with context, missing[] (required deps not satisfied),
    warnings[] (human-readable), ok.
    """
    from agentflow import db
    # consensus is optional context (previous iteration) — fetch best-effort
    consensus = None
    try:
        from agentflow import db as _db
        consensus = _db.get_consensus(run["id"], project=".")
    except Exception:
        consensus = None
    ctx = build_context(run, executions, consensus=consensus,
                        top_hypothesis=top_hypothesis, raw_data=raw_data,
                        iteration=run.get("current_iteration", 1))
    # If caller passed raw_data/top explicitly, they already land in ctx via build_context
    missing = missing_dependencies(agent_id, ctx)
    warnings = []
    if missing:
        a = AGENT_BY_ID.get(agent_id, {})
        for m in missing:
            warnings.append(f"Missing required input {{{{{m}}}}} for {a.get('name', agent_id)} — generate a prompt only after the preceding agent has saved real output. No fabricated data has been inserted.")
    return {"context": ctx, "missing": missing, "warnings": warnings, "ok": len(missing) == 0}

def build_prompt_preview(agent_id: str, run, executions, raw_data="", top_hypothesis="", save=False, project="."):
    """Rendered prompt + unresolved + warnings for an agent.

    When save=True the prompt is persisted via db.save_prompt (generate).
    Preview (save=False) never fabricates: placeholders with no real
    upstream output stay as {{KEY}} and are reported in unresolved.
    """
    template = load_template(agent_id)
    deps = dependencies_for(agent_id)
    placeholders = sorted(set(re.findall(r"\{\{(\w+)\}\}", template)))
    # consensus fetch with project dir
    try:
        consensus_row = dbmod.get_consensus(run["id"] if isinstance(run, dict) else run.id, project=project) if isinstance(run, dict) else None
    except Exception:
        consensus_row = None
    ctx = build_context(run, executions, consensus=consensus_row,
                        top_hypothesis=top_hypothesis or "", raw_data=raw_data or "",
                        iteration=(run["current_iteration"] if isinstance(run, dict) else getattr(run, "current_iteration", 1)))
    rendered, unresolved = render_prompt(template, ctx)
    missing = missing_dependencies(agent_id, ctx)
    warnings = []
    if missing:
        for m in missing:
            warnings.append(f"Missing required input {{{{{m}}}}} — prompt contains unresolved placeholder; no fabricated data injected.")
    if unresolved:
        for u in unresolved:
            if u not in missing:
                warnings.append(f"Template references {{{{{u}}}}} but no real value exists yet — left unresolved.")
    payload = {
        "agent": agent_id,
        "prompt": rendered,
        "template": template,
        "placeholders": placeholders,
        "dependencies": deps,
        "context_keys": sorted(k for k in ctx.keys() if str(ctx.get(k, "")).strip() != ""),
        "unresolved": unresolved,
        "missing": missing,
        "warnings": warnings,
        "ok": len(missing) == 0 and len(unresolved) == 0,
    }
    if save:
        rid = run["id"] if isinstance(run, dict) else run.id
        dbmod.save_prompt(rid, agent_id, rendered, project)
    return payload

def execution_payload(execution, agent_id=None):
    """Inspector-ready execution dict with timestamps + status."""
    if execution is None:
        return None
    out = dict(execution)
    if agent_id:
        out["agent"] = agent_id
    # Normalize timestamp fields explicitly
    out["created_at"] = execution.get("created_at")
    out["updated_at"] = execution.get("updated_at")
    out["status"] = execution.get("status", "PENDING")
    return out

def agent_status_payload(agent_id: str, execution, run):
    """Workflow status + input dependencies for the inspector."""
    a = AGENT_BY_ID.get(agent_id, {})
    ex = execution or {"status": "PENDING", "prompt": None, "output": None,
                       "created_at": None, "updated_at": None, "iteration": run.get("current_iteration", 1) if isinstance(run, dict) else 1}
    deps = dependencies_for(agent_id)
    return {
        "id": agent_id,
        "agent": agent_id,
        "name": a.get("name", agent_id),
        "description": a.get("description", ""),
        "status": ex.get("status", "PENDING"),
        "workflow_status": ex.get("status", "PENDING"),
        "dependencies": deps,
        "inputs": deps,
        "prompt": ex.get("prompt"),
        "output": ex.get("output"),
        "saved_output": ex.get("output"),
        "created_at": ex.get("created_at"),
        "updated_at": ex.get("updated_at"),
        "iteration": ex.get("iteration", run.get("current_iteration") if isinstance(run, dict) else None),
        "top_hypothesis": ex.get("top_hypothesis"),
    }
