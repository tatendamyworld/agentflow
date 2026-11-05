"""PHASE 6 — Workflow State Management: reliable state machine.

Canonical run states (spec): CREATED, READY, IN_PROGRESS,
WAITING_FOR_INPUT, COMPLETED, FAILED, BLOCKED, ARCHIVED.

Legacy/web aliases kept for CLI + historical DB rows:
  WAITING_FOR_TOP_HYPOTHESIS -> WAITING_FOR_INPUT
  WAITING_FOR_FINCH          -> WAITING_FOR_INPUT
  CONSENSUS_REACHED          -> COMPLETED
  SPLIT                      -> BLOCKED

Execution states: PENDING/CREATED (initial), READY, IN_PROGRESS,
WAITING_FOR_INPUT (WAITING legacy), COMPLETED, FAILED, BLOCKED, ARCHIVED.

Rules enforced:
  1. An agent cannot execute (generate/save/submit/retry) before its
     dependencies are satisfied (sequence prefix + context check).
  2. Generating a prompt does NOT complete an agent.
  3. Saving a *validated* response completes its step; invalid saves
     never persist output and leave the step recoverable.
  4. Previous outputs are never silently overwritten (overwrite flag).
  5. Retrying archives the previous execution before resetting.
  6. Dashboard progress is derived from the database on every read.
No network, no LLM calls — pure functions over SQLite rows.
"""
from agentflow.agents import AGENT_IDS, dependencies_for, missing_dependencies

# --- canonical run states (Phase 6 spec) -----------------------------------
RUN_STATES = ["CREATED", "READY", "IN_PROGRESS", "WAITING_FOR_INPUT",
              "COMPLETED", "FAILED", "BLOCKED", "ARCHIVED"]
LEGACY_RUN_ALIASES = {
    "WAITING_FOR_TOP_HYPOTHESIS": "WAITING_FOR_INPUT",
    "WAITING_FOR_FINCH": "WAITING_FOR_INPUT",
    "CONSENSUS_REACHED": "COMPLETED",
    "SPLIT": "BLOCKED",
}
# All strings accepted in existing DB rows / CLI code
KNOWN_RUN_STATES = sorted(set(RUN_STATES) | set(LEGACY_RUN_ALIASES))

# execution states — PENDING is the historic "CREATED" for a step
EXEC_STATES = ["PENDING", "READY", "IN_PROGRESS", "WAITING_FOR_INPUT",
               "WAITING", "COMPLETED", "FAILED", "BLOCKED", "ARCHIVED"]
KNOWN_EXEC_STATES = set(EXEC_STATES)

# Legal run transitions (canonical + legacy sources/targets)
RUN_TRANSITIONS = {
    "CREATED": {"READY", "IN_PROGRESS", "WAITING_FOR_INPUT", "BLOCKED", "FAILED", "ARCHIVED", "CREATED"},
    "READY": {"IN_PROGRESS", "WAITING_FOR_INPUT", "BLOCKED", "FAILED", "ARCHIVED", "CREATED"},
    "IN_PROGRESS": {"WAITING_FOR_INPUT", "COMPLETED", "FAILED", "BLOCKED", "ARCHIVED",
                    "WAITING_FOR_TOP_HYPOTHESIS", "WAITING_FOR_FINCH", "CONSENSUS_REACHED", "SPLIT"},
    "WAITING_FOR_INPUT": {"IN_PROGRESS", "BLOCKED", "FAILED", "COMPLETED", "ARCHIVED",
                          "WAITING_FOR_TOP_HYPOTHESIS", "WAITING_FOR_FINCH", "CONSENSUS_REACHED", "SPLIT"},
    "WAITING_FOR_TOP_HYPOTHESIS": {"IN_PROGRESS", "WAITING_FOR_INPUT", "BLOCKED", "FAILED", "COMPLETED", "ARCHIVED"},
    "WAITING_FOR_FINCH": {"IN_PROGRESS", "WAITING_FOR_INPUT", "BLOCKED", "FAILED", "COMPLETED", "ARCHIVED"},
    "CONSENSUS_REACHED": {"COMPLETED", "CREATED", "ARCHIVED", "IN_PROGRESS"},
    "SPLIT": {"IN_PROGRESS", "WAITING_FOR_INPUT", "FAILED", "BLOCKED", "ARCHIVED", "CREATED"},
    "COMPLETED": {"CREATED", "ARCHIVED", "IN_PROGRESS", "READY"},
    "FAILED": {"IN_PROGRESS", "READY", "WAITING_FOR_INPUT", "BLOCKED", "ARCHIVED", "CREATED"},
    "BLOCKED": {"IN_PROGRESS", "READY", "WAITING_FOR_INPUT", "FAILED", "ARCHIVED", "CREATED"},
    "ARCHIVED": {"ARCHIVED"},
}

EXEC_TRANSITIONS = {
    "PENDING": {"READY", "IN_PROGRESS", "WAITING", "WAITING_FOR_INPUT", "COMPLETED", "BLOCKED", "FAILED", "ARCHIVED"},
    "READY": {"PENDING", "IN_PROGRESS", "WAITING", "WAITING_FOR_INPUT", "COMPLETED", "BLOCKED", "FAILED"},
    "WAITING": {"PENDING", "READY", "IN_PROGRESS", "WAITING_FOR_INPUT", "COMPLETED", "BLOCKED", "FAILED"},
    "WAITING_FOR_INPUT": {"PENDING", "READY", "IN_PROGRESS", "COMPLETED", "BLOCKED", "FAILED"},
    "IN_PROGRESS": {"PENDING", "READY", "WAITING", "WAITING_FOR_INPUT", "COMPLETED", "BLOCKED", "FAILED"},
    "COMPLETED": {"PENDING", "READY", "ARCHIVED"},  # PENDING only via retry (archived)
    "FAILED": {"PENDING", "READY", "WAITING", "WAITING_FOR_INPUT", "IN_PROGRESS", "BLOCKED", "COMPLETED"},
    "BLOCKED": {"PENDING", "READY", "WAITING", "WAITING_FOR_INPUT", "IN_PROGRESS", "FAILED", "COMPLETED"},
    "ARCHIVED": {"ARCHIVED"},
}


def normalize_run_status(status):
    """Legacy -> canonical; unknown passes through uppercased."""
    if not status:
        return "CREATED"
    s = str(status).strip().upper()
    return LEGACY_RUN_ALIASES.get(s, s)


def normalize_exec_status(status):
    if not status:
        return "PENDING"
    return str(status).strip().upper()


def can_transition_run(current, target):
    cur = normalize_run_status(current)
    # Keep original key for lookup (legacy sources registered verbatim)
    src = str(current).strip().upper() if current else "CREATED"
    # Try verbatim src first, then normalized
    allowed = RUN_TRANSITIONS.get(src, RUN_TRANSITIONS.get(cur, set()))
    return str(target).strip().upper() in allowed


def can_transition_exec(current, target):
    allowed = EXEC_TRANSITIONS.get(normalize_exec_status(current), set())
    return str(target).strip().upper() in allowed


def validate_run_transition(current, target):
    """Return error string or None."""
    if not can_transition_run(current, target):
        return f"Illegal run transition {current} -> {target}"
    return None


def validate_exec_transition(current, target):
    if not can_transition_exec(current, target):
        return f"Illegal execution transition {current} -> {target}"
    return None


def completed_agents(executions):
    return [e["agent"] for e in executions if normalize_exec_status(e.get("status")) == "COMPLETED"]


def next_agent(executions):
    """First non-completed agent in canonical order (PENDING/READY/WAITING/FAILED/BLOCKED...)."""
    by_order = {e["agent"]: e for e in executions}
    for a in AGENT_IDS:
        e = by_order.get(a)
        if e is None:
            continue
        if normalize_exec_status(e.get("status")) != "COMPLETED":
            return a
    return None


def can_execute(agent_id, executions, context=None):
    """Dependencies check: agent must be the next incomplete slot AND all
    required context values present. Returns (ok, reason)."""
    if agent_id not in AGENT_IDS:
        return False, f"unknown agent {agent_id}"
    idx = AGENT_IDS.index(agent_id)
    done = completed_agents(executions)
    # Sequence rule: all prior agents COMPLETED
    expected_prefix = AGENT_IDS[:idx]
    # allow completed agent itself (regenerate/overwrite path) — caller decides
    is_already_done = agent_id in done
    if done != expected_prefix and not is_already_done:
        # Distinguish "blocked" (future) from "already done"
        if len(done) < idx:
            return False, f"dependencies not satisfied for {agent_id}: expected {expected_prefix} completed, got {done}"
        return False, f"sequence broken for {agent_id}: completed={done}"
    if context is not None:
        missing = missing_dependencies(agent_id, context)
        if missing:
            return False, f"missing required input {{{{{', '.join(missing)}}}}} for {agent_id} — no fabricated data injected"
    return True, ""


def derive_run_status(run, executions, consensus=None, stored=None):
    """Canonical run state derived from database truth (not stored hint).

    Priority:
      ARCHIVED (explicit) > COMPLETED (all 9) > FAILED (next step FAILED)
      > BLOCKED (next step BLOCKED or deps missing) > WAITING_FOR_INPUT
      (next step has prompt awaiting paste) > IN_PROGRESS (any COMPLETED)
      > READY (fresh, supervisor executable) > CREATED.
    """
    stored_s = normalize_run_status((stored if stored is not None else (run or {}).get("status", "CREATED")))
    if stored_s == "ARCHIVED":
        return "ARCHIVED"
    exes = executions or []
    if not exes:
        return stored_s if stored_s in RUN_STATES else "CREATED"
    # Consensus-completed counts as COMPLETED even if stored legacy
    all_done = len(completed_agents(exes)) == len(AGENT_IDS) == 9
    if all_done:
        return "COMPLETED"
    nxt_id = next_agent(exes)
    nxt = next((e for e in exes if e.get("agent") == nxt_id), {}) if nxt_id else {}
    nxt_status = normalize_exec_status(nxt.get("status", "PENDING"))
    if nxt_status == "FAILED":
        return "FAILED"
    if nxt_status == "BLOCKED":
        return "BLOCKED"
    # A prompt already generated & awaiting paste -> WAITING_FOR_INPUT
    if nxt.get("prompt") and nxt_status in ("PENDING", "READY", "WAITING", "WAITING_FOR_INPUT", "IN_PROGRESS"):
        return "WAITING_FOR_INPUT"
    if any(normalize_exec_status(e.get("status")) == "COMPLETED" for e in exes):
        return "IN_PROGRESS"
    if stored_s in ("FAILED", "BLOCKED"):
        # recoverable: FAILED/BLOCKED with no completion still maps to
        # READY (fresh work) unless execution evidence says otherwise
        # above; keep stored hint if no evidence of progress.
        return stored_s
    if stored_s in RUN_STATES:
        # CREATED -> READY when supervisor is executable (deps = Goal)
        if stored_s == "CREATED":
            return "READY"
        return stored_s
    return "READY"


def compute_progress(run, executions, consensus=None):
    """Workflow progress calculation — single source of truth."""
    exes = executions or []
    total = len(exes) if exes else len(AGENT_IDS)
    completed = sum(1 for e in exes if normalize_exec_status(e.get("status")) == "COMPLETED")
    failed = sum(1 for e in exes if normalize_exec_status(e.get("status")) == "FAILED")
    blocked = sum(1 for e in exes if normalize_exec_status(e.get("status")) == "BLOCKED")
    pending = sum(1 for e in exes if normalize_exec_status(e.get("status")) in ("PENDING", "READY", "WAITING", "WAITING_FOR_INPUT", "IN_PROGRESS"))
    pct = round(completed / total * 100, 1) if total else 0.0
    derived = derive_run_status(run, exes, consensus=consensus)
    return {
        "completed": completed, "completed_count": completed,
        "total": total, "total_agents": total,
        "pending": pending, "pending_count": pending,
        "failed": failed, "failed_count": failed,
        "blocked": blocked, "blocked_count": blocked,
        "progress": pct, "percentage": pct, "percent": pct,
        "next_agent": next_agent(exes),
        "completed_agents": completed_agents(exes),
        "pending_agents": [e["agent"] for e in exes if normalize_exec_status(e.get("status")) != "COMPLETED"],
        "status": derived, "derived_status": derived,
        "run_status": derived,
        "iteration": (run.get("current_iteration") if isinstance(run, dict) else 1) or 1,
    }
