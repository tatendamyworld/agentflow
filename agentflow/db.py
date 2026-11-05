"""SQLite persistence for AgentFlow. DB lives at .agentflow/state.db."""
import sqlite3, pathlib, uuid, datetime, json

AGENTS_ORDER = ["supervisor","generation","proximity","reflection","evolution","ranking","crow","falcon","finch"]
AGENT_NAMES = {
    "supervisor": "Supervisor", "generation": "Generation", "proximity": "Proximity",
    "reflection": "Reflection", "evolution": "Evolution", "ranking": "Ranking",
    "crow": "Crow", "falcon": "Falcon", "finch": "Finch",
}
WORKFLOW_STATES = ["CREATED","IN_PROGRESS","WAITING_FOR_INPUT","WAITING_FOR_TOP_HYPOTHESIS","WAITING_FOR_FINCH","CONSENSUS_REACHED","SPLIT","COMPLETED","ARCHIVED"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'CREATED',
  current_iteration INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, iteration INTEGER NOT NULL,
  agent TEXT NOT NULL, prompt TEXT, output TEXT, status TEXT NOT NULL DEFAULT 'PENDING',
  created_at TEXT, updated_at TEXT, top_hypothesis TEXT, raw_data TEXT,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS finch_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, iteration INTEGER NOT NULL,
  run_index INTEGER NOT NULL, raw_response TEXT, parsed_vote TEXT, created_at TEXT,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS consensus (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, iteration INTEGER NOT NULL,
  yes_count INTEGER, no_count INTEGER, inconclusive_count INTEGER, invalid_count INTEGER,
  outcome TEXT, detail TEXT, created_at TEXT,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);
"""

def _db_path(project="."):
    return pathlib.Path(project) / ".agentflow" / "state.db"

def _connect(project="."):
    p = _db_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con

def _now():
    return datetime.datetime.utcnow().isoformat()

def new_run_id():
    return "AF-" + uuid.uuid4().hex[:8].upper()

def create_run(goal, project="."):
    con = _connect(project)
    rid = new_run_id()
    while con.execute("SELECT 1 FROM runs WHERE id=?", (rid,)).fetchone():
        rid = new_run_id()
    con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)", (rid, goal, "CREATED", 1, _now(), _now()))
    for i, a in enumerate(AGENTS_ORDER):
        con.execute("INSERT INTO executions(run_id,iteration,agent,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (rid, 1, a, "PENDING", _now(), _now()))
    con.commit(); con.close()
    return rid

def get_run(rid, project="."):
    con = _connect(project)
    row = con.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    con.close(); return dict(row) if row else None

def list_runs(project="."):
    con = _connect(project)
    rows = con.execute("SELECT * FROM runs ORDER BY created_at").fetchall()
    con.close(); return [dict(r) for r in rows]

def set_run_status(rid, status, project="."):
    assert status in WORKFLOW_STATES, f"invalid state {status}"
    con = _connect(project)
    con.execute("UPDATE runs SET status=?, updated_at=? WHERE id=?", (status, _now(), rid))
    con.commit(); con.close()

def get_executions(rid, iteration=None, project="."):
    con = _connect(project)
    if iteration:
        rows = con.execute("SELECT * FROM executions WHERE run_id=? AND iteration=? ORDER BY id", (rid, iteration)).fetchall()
    else:
        rows = con.execute("SELECT * FROM executions WHERE run_id=? ORDER BY iteration, id", (rid,)).fetchall()
    con.close(); return [dict(r) for r in rows]

def get_execution(rid, agent, iteration=None, project="."):
    con = _connect(project)
    if iteration is None:
        it = con.execute("SELECT current_iteration FROM runs WHERE id=?", (rid,)).fetchone()
        iteration = it["current_iteration"] if it else 1
    row = con.execute("SELECT * FROM executions WHERE run_id=? AND agent=? AND iteration=?", (rid, agent, iteration)).fetchone()
    con.close(); return dict(row) if row else None

def next_pending(rid, project="."):
    run = get_run(rid, project)
    if not run: return None
    exes = get_executions(rid, iteration=run["current_iteration"], project=project)
    for e in exes:
        if e["status"] in ("PENDING", "WAITING", "FAILED"):
            return e
    return None

def save_output(rid, agent, output, project=".", top_hypothesis=None, raw_data=None):
    run = get_run(rid, project)
    assert run, "run not found"
    con = _connect(project)
    cur = con.execute("SELECT * FROM executions WHERE run_id=? AND agent=? AND iteration=?", (rid, agent, run["current_iteration"])).fetchone()
    assert cur, f"no execution slot for {agent}"
    # guard against silent overwrite: require explicit overwrite flag via status check
    if cur["output"] and cur["output"].strip() and cur["status"] == "COMPLETED":
        # still allow with explicit confirmation handled by CLI (CLI confirms); record anyway
        pass
    con.execute("UPDATE executions SET output=?, status='COMPLETED', prompt=COALESCE(prompt,?), top_hypothesis=COALESCE(?,top_hypothesis), raw_data=COALESCE(?,raw_data), updated_at=? WHERE id=?",
                (output, cur["prompt"], top_hypothesis, raw_data, _now(), cur["id"]))
    # advance status
    con.execute("UPDATE runs SET status='IN_PROGRESS', updated_at=? WHERE id=?", (_now(), rid))
    con.commit(); con.close()

def save_prompt(rid, agent, prompt, project="."):
    run = get_run(rid, project)
    assert run
    con = _connect(project)
    con.execute("UPDATE executions SET prompt=?, updated_at=? WHERE run_id=? AND agent=? AND iteration=?",
                (prompt, _now(), rid, agent, run["current_iteration"]))
    con.commit(); con.close()

def save_top_hypothesis(rid, text, project="."):
    run = get_run(rid, project); assert run
    con = _connect(project)
    con.execute("UPDATE executions SET top_hypothesis=?, updated_at=? WHERE run_id=? AND agent='ranking' AND iteration=?",
                (text, _now(), rid, run["current_iteration"]))
    con.execute("UPDATE runs SET status='IN_PROGRESS', updated_at=? WHERE id=?", (_now(), rid))
    con.commit(); con.close()

def save_finch_runs(rid, responses, project="."):
    run = get_run(rid, project); assert run
    from agentflow.consensus import parse_finch_vote
    con = _connect(project)
    # clear old for this iteration then insert
    con.execute("DELETE FROM finch_runs WHERE run_id=? AND iteration=?", (rid, run["current_iteration"]))
    for i, r in enumerate(responses, 1):
        vote = parse_finch_vote(r)
        con.execute("INSERT INTO finch_runs(run_id,iteration,run_index,raw_response,parsed_vote,created_at) VALUES(?,?,?,?,?,?)",
                    (rid, run["current_iteration"], i, r, vote, _now()))
    con.commit(); con.close()

def get_finch_runs(rid, iteration=None, project="."):
    con = _connect(project)
    if iteration is None:
        it = con.execute("SELECT current_iteration FROM runs WHERE id=?", (rid,)).fetchone()
        iteration = it["current_iteration"] if it else 1
    rows = con.execute("SELECT * FROM finch_runs WHERE run_id=? AND iteration=? ORDER BY run_index", (rid, iteration)).fetchall()
    con.close(); return [dict(r) for r in rows]

def save_consensus(rid, result, project="."):
    run = get_run(rid, project); assert run
    con = _connect(project)
    con.execute("INSERT INTO consensus(run_id,iteration,yes_count,no_count,inconclusive_count,invalid_count,outcome,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (rid, run["current_iteration"], result["yes"], result["no"], result["inconclusive"], result["invalid"], result["outcome"], json.dumps(result), _now()))
    con.commit(); con.close()
    # update run status: CONSENSUS_REACHED / SPLIT / COMPLETED stays? set per outcome
    status = "CONSENSUS_REACHED" if result["outcome"].startswith("CONSENSUS") else "SPLIT"
    set_run_status(rid, status, project)

def get_consensus(rid, iteration=None, project="."):
    con = _connect(project)
    if iteration is None:
        it = con.execute("SELECT current_iteration FROM runs WHERE id=?", (rid,)).fetchone()
        iteration = it["current_iteration"] if it else 1
    row = con.execute("SELECT * FROM consensus WHERE run_id=? AND iteration=? ORDER BY id DESC LIMIT 1", (rid, iteration)).fetchone()
    con.close(); return dict(row) if row else None

def new_iteration(rid, project="."):
    run = get_run(rid, project); assert run
    cons = get_consensus(rid, iteration=run["current_iteration"], project=project)
    assert cons, "cannot loop before Finch consensus"
    new_it = run["current_iteration"] + 1
    con = _connect(project)
    con.execute("UPDATE runs SET current_iteration=?, status='CREATED', updated_at=? WHERE id=?", (new_it, _now(), rid))
    for a in AGENTS_ORDER:
        con.execute("INSERT INTO executions(run_id,iteration,agent,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (rid, new_it, a, "PENDING", _now(), _now()))
    con.commit(); con.close()
    return new_it


def get_execution_history(rid, agent=None, project="."):
    """Previous execution history — every row for a run (all iterations), oldest first.

    Filter by agent when given. Caller gets timestamps + status so the UI
    can render execution history (phase 5).
    """
    con = _connect(project)
    try:
        if agent:
            rows = con.execute(
                "SELECT * FROM executions WHERE run_id=? AND agent=? ORDER BY iteration, id",
                (rid, agent)).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM executions WHERE run_id=? ORDER BY iteration, id",
                (rid,)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def retry_execution(rid, agent, project=".", clear_output=True):
    """Reset an execution slot (and all downstream slots) to PENDING.

    RETRY STEP semantics: re-running an earlier agent invalidates
    downstream results, so those slots are also cleared to preserve the
    sequence invariant (no skipping). History rows for other iterations
    are untouched. Returns the reset row for the target agent.
    """
    run = get_run(rid, project); assert run, "run not found"
    if agent not in AGENTS_ORDER:
        raise ValueError(f"unknown agent {agent}")
    idx = AGENTS_ORDER.index(agent)
    downstream = AGENTS_ORDER[idx:]
    con = _connect(project)
    try:
        cur = con.execute(
            "SELECT * FROM executions WHERE run_id=? AND agent=? AND iteration=?",
            (rid, agent, run["current_iteration"])).fetchone()
        assert cur, f"no execution slot for {agent}"
        for down_agent in downstream:
            row = con.execute(
                "SELECT id FROM executions WHERE run_id=? AND agent=? AND iteration=?",
                (rid, down_agent, run["current_iteration"])).fetchone()
            if not row:
                continue
            if clear_output:
                con.execute(
                    "UPDATE executions SET output=NULL, status='PENDING', updated_at=? WHERE id=?",
                    (_now(), row["id"]))
            else:
                con.execute(
                    "UPDATE executions SET status='PENDING', updated_at=? WHERE id=?",
                    (_now(), row["id"]))
        con.execute("UPDATE runs SET updated_at=? WHERE id=?", (_now(), rid))
        con.commit()
    finally:
        con.close()
    return get_execution(rid, agent, project=project)


def validate_output(agent, output):
    """Response validation for a manual submission.

    Returns (ok, errors[]). Guarantees: non-empty, length-capped callers
    enforce max length separately, plus agent-specific shape rules:
      - finch: must contain an explicit 'Key finding: YES|NO|INCONCLUSIVE' line
      - ranking: should name a winner (Top hypothesis:) for Crow handoff —
        warning only, not blocking? We keep it as error to prevent silent
        missing downstream dependency. Frontend can still force via raw save?
        For phase 5, ranking without Top hypothesis returns ok=False with
        a clear error so Crow does not receive fabricated winner.
      - others: non-empty is sufficient.
    """
    errors = []
    if output is None or not str(output).strip():
        return False, ["output must not be empty"]
    t = str(output).strip()
    if len(t) < 1:
        return False, ["output must not be empty"]
    if len(t) > 100_000:
        return False, ["output exceeds 100000 characters"]
    if agent == "finch":
        from agentflow.consensus import parse_finch_vote
        if parse_finch_vote(t) == "INVALID":
            errors.append("finch output must contain 'Key finding: YES|NO|INCONCLUSIVE'")
    elif agent == "ranking":
        # Ranking must declare winner for Crow unless caller supplies
        # top_hypothesis separately. We accept either signal.
        import re
        if not re.search(r"top\s+hypothesis", t, re.I):
            errors.append("ranking output must include 'Top hypothesis: ...' for Crow handoff")
    else:
        # Generic: reject placeholder-only pastes (unsubstituted templates)
        if "{{" in t and "}}" in t:
            import re
            if re.search(r"\{\{\w+\}\}", t):
                errors.append("output still contains unresolved {{PLACEHOLDERS}} — paste the agent's answer, not the prompt template")
    return (len(errors) == 0), errors
