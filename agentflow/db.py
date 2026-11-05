"""SQLite persistence for AgentFlow. DB lives at .agentflow/state.db."""
import sqlite3, pathlib, uuid, datetime, json

AGENTS_ORDER = ["supervisor","generation","proximity","reflection","evolution","ranking","crow","falcon","finch"]
AGENT_NAMES = {
    "supervisor": "Supervisor", "generation": "Generation", "proximity": "Proximity",
    "reflection": "Reflection", "evolution": "Evolution", "ranking": "Ranking",
    "crow": "Crow", "falcon": "Falcon", "finch": "Finch",
}
WORKFLOW_STATES = ["CREATED","READY","IN_PROGRESS","WAITING_FOR_INPUT","COMPLETED","FAILED","BLOCKED","ARCHIVED","WAITING_FOR_TOP_HYPOTHESIS","WAITING_FOR_FINCH","CONSENSUS_REACHED","SPLIT"]

from agentflow.workflow import (KNOWN_RUN_STATES, KNOWN_EXEC_STATES, normalize_run_status,
    normalize_exec_status, can_transition_run, can_transition_exec)

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
  validation_status TEXT, updated_at TEXT,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS consensus (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, iteration INTEGER NOT NULL,
  yes_count INTEGER, no_count INTEGER, inconclusive_count INTEGER, invalid_count INTEGER,
  outcome TEXT, detail TEXT, created_at TEXT,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS execution_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, iteration INTEGER NOT NULL,
  agent TEXT NOT NULL, prompt TEXT, output TEXT, status TEXT NOT NULL,
  top_hypothesis TEXT, raw_data TEXT, created_at TEXT, updated_at TEXT,
  archived_at TEXT, archive_reason TEXT,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS finch_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, iteration INTEGER NOT NULL,
  total_runs INTEGER, votes_json TEXT, outcome TEXT, created_at TEXT,
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
    # migration: lab columns on older DBs (idempotent)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(finch_runs)").fetchall()]
        if "validation_status" not in cols:
            con.execute("ALTER TABLE finch_runs ADD COLUMN validation_status TEXT")
        if "updated_at" not in cols:
            con.execute("ALTER TABLE finch_runs ADD COLUMN updated_at TEXT")
            con.execute("UPDATE finch_runs SET updated_at=created_at WHERE updated_at IS NULL")
        con.execute("UPDATE finch_runs SET validation_status=CASE WHEN parsed_vote IN ('YES','NO','INCONCLUSIVE') THEN 'VALID' WHEN parsed_vote='INVALID' THEN 'INVALID' ELSE validation_status END WHERE validation_status IS NULL")
        con.commit()
    except Exception:
        pass
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

def set_run_status(rid, status, project=".", force=False):
    """Persist run status with transition validation.

    Transitions are enforced via agentflow.workflow.RUN_TRANSITIONS,
    unless force=True (derived sync / legacy CLI bootstrap paths).
    """
    assert status in WORKFLOW_STATES, f"invalid state {status}"
    run = get_run(rid, project)
    assert run, "run not found"
    cur = run["status"]
    tgt = normalize_run_status(status)
    cur_n = normalize_run_status(cur)
    # ARCHIVED is terminal: only no-op allowed
    if cur_n == "ARCHIVED" and tgt != "ARCHIVED":
        raise ValueError(f"Illegal run transition {cur} -> {status} (ARCHIVED is terminal)")
    # Allow no-op and derive-driven backward moves? Enforce via map but
    # permit sync to derived state even if stored is stale (force internal).
    if not force and cur != status and not can_transition_run(cur, status):
        # Also accept normalized comparison (e.g. CONSENSUS_REACHED->COMPLETED)
        if not can_transition_run(cur_n, tgt):
            raise ValueError(f"Illegal run transition {cur} -> {status}")
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
        if e["status"] in ("PENDING", "READY", "IN_PROGRESS", "WAITING", "WAITING_FOR_INPUT", "FAILED", "BLOCKED"):
            return e
    return None

def save_output(rid, agent, output, project=".", top_hypothesis=None, raw_data=None, overwrite=False):
    """Save a *validated* response -> execution COMPLETED. Invalid callers
    must validate first (db.validate_output) and must NOT call this on
    failure, so invalid responses stay recoverable (FAILED, no output).

    Overwrite guard: a COMPLETED slot with real output refuses silent
    overwrite unless overwrite=True (explicit user confirmation). The
    previous version is archived to execution_history on overwrite.
    Run status is then re-derived (COMPLETED if all 9 done, else IN_PROGRESS).
    """
    run = get_run(rid, project)
    assert run, "run not found"
    con = _connect(project)
    cur = con.execute("SELECT * FROM executions WHERE run_id=? AND agent=? AND iteration=?", (rid, agent, run["current_iteration"])).fetchone()
    assert cur, f"no execution slot for {agent}"
    if cur["output"] and str(cur["output"]).strip() and normalize_exec_status(cur["status"]) == "COMPLETED" and not overwrite:
        con.close()
        raise ValueError(f"refusing to silently overwrite completed {agent} output — use RETRY STEP to preserve it (or pass overwrite=true explicitly)")
    if cur["output"] and str(cur["output"]).strip() and overwrite:
        con.execute("INSERT INTO execution_history(run_id,iteration,agent,prompt,output,status,top_hypothesis,raw_data,created_at,updated_at,archived_at,archive_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cur["run_id"], cur["iteration"], cur["agent"], cur["prompt"], cur["output"], cur["status"], cur["top_hypothesis"], cur["raw_data"], cur["created_at"], cur["updated_at"], _now(), "overwritten"))
    con.execute("UPDATE executions SET output=?, status='COMPLETED', prompt=COALESCE(prompt,?), top_hypothesis=COALESCE(?,top_hypothesis), raw_data=COALESCE(?,raw_data), updated_at=? WHERE id=?",
                (output, cur["prompt"], top_hypothesis, raw_data, _now(), cur["id"]))
    con.commit(); con.close()
    sync_run_status(rid, project)

def save_prompt(rid, agent, prompt, project="."):
    """Persist generated prompt. Generating does NOT complete the agent:
    slot becomes WAITING_FOR_INPUT (awaiting paste), run re-derived."""
    run = get_run(rid, project)
    assert run
    con = _connect(project)
    cur = con.execute("SELECT * FROM executions WHERE run_id=? AND agent=? AND iteration=?",
                      (rid, agent, run["current_iteration"])).fetchone()
    status = cur["status"] if cur else "PENDING"
    # Only advance non-completed slots; never demote COMPLETED via generate
    new_status = status if normalize_exec_status(status) == "COMPLETED" else "WAITING_FOR_INPUT"
    con.execute("UPDATE executions SET prompt=?, status=?, updated_at=? WHERE run_id=? AND agent=? AND iteration=?",
                (prompt, new_status, _now(), rid, agent, run["current_iteration"]))
    con.commit(); con.close()
    sync_run_status(rid, project)

def save_top_hypothesis(rid, text, project="."):
    run = get_run(rid, project); assert run
    con = _connect(project)
    con.execute("UPDATE executions SET top_hypothesis=?, updated_at=? WHERE run_id=? AND agent='ranking' AND iteration=?",
                (text, _now(), rid, run["current_iteration"]))
    con.commit(); con.close()
    sync_run_status(rid, project)

def save_finch_runs(rid, responses, project="."):
    run = get_run(rid, project); assert run
    from agentflow.consensus import parse_finch_vote
    con = _connect(project)
    old = con.execute("SELECT * FROM finch_runs WHERE run_id=? AND iteration=?", (rid, run["current_iteration"])).fetchall()
    if old:
        import json as _json
        votes = [r["parsed_vote"] for r in old]
        try:
            outcome = _json.loads(old[0]["raw_response"]) if False else None
        except Exception:
            outcome = None
        con.execute("INSERT INTO finch_history(run_id,iteration,total_runs,votes_json,outcome,created_at) VALUES(?,?,?,?,?,?)",
                    (rid, run["current_iteration"], len(old), _json.dumps({"votes": votes, "responses": [r["raw_response"] for r in old]}), "superseded", _now()))
    # archive + replace (previous finch votes preserved in finch_history)
    con.execute("DELETE FROM finch_runs WHERE run_id=? AND iteration=?", (rid, run["current_iteration"]))
    for i, r in enumerate(responses, 1):
        vote = parse_finch_vote(r)
        vs = "VALID" if vote in ("YES", "NO", "INCONCLUSIVE") else "INVALID"
        con.execute("INSERT INTO finch_runs(run_id,iteration,run_index,raw_response,parsed_vote,created_at,validation_status,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (rid, run["current_iteration"], i, r, vote, _now(), vs, _now()))
    con.commit(); con.close()

def get_finch_runs(rid, iteration=None, project="."):
    con = _connect(project)
    if iteration is None:
        it = con.execute("SELECT current_iteration FROM runs WHERE id=?", (rid,)).fetchone()
        iteration = it["current_iteration"] if it else 1
    rows = con.execute("SELECT * FROM finch_runs WHERE run_id=? AND iteration=? ORDER BY run_index", (rid, iteration)).fetchall()
    con.close(); return [dict(r) for r in rows]

def save_finch_run(rid, run_index, response, project="."):
    """Single-evaluation upsert (1..8). Stores INVALID rows too (not as votes)
    so they stay editable; consensus finalization is separate."""
    run = get_run(rid, project); assert run
    if not isinstance(run_index, int) or isinstance(run_index, bool) or not (1 <= run_index <= 8):
        raise ValueError("run_index must be 1-8")
    if response is None or not str(response).strip():
        raise ValueError(f"Finch Run {run_index} response must not be empty")
    if len(str(response)) > 20000:
        raise ValueError(f"Finch Run {run_index} exceeds 20000 characters")
    from agentflow.consensus import parse_finch_vote
    vote = parse_finch_vote(response)
    vs = "VALID" if vote in ("YES", "NO", "INCONCLUSIVE") else "INVALID"
    con = _connect(project)
    old = con.execute("SELECT * FROM finch_runs WHERE run_id=? AND iteration=? AND run_index=?",
                      (rid, run["current_iteration"], run_index)).fetchone()
    if old is not None:
        import json as _json
        all_rows = con.execute("SELECT * FROM finch_runs WHERE run_id=? AND iteration=?", (rid, run["current_iteration"])).fetchall()
        con.execute("INSERT INTO finch_history(run_id,iteration,total_runs,votes_json,outcome,created_at) VALUES(?,?,?,?,?,?)",
                    (rid, run["current_iteration"], len(all_rows),
                     _json.dumps({"edited_run": run_index, "before": [old["raw_response"]], "votes_before": [old["parsed_vote"]]}),
                     "edited", _now()))
        con.execute("UPDATE finch_runs SET raw_response=?, parsed_vote=?, validation_status=?, updated_at=? WHERE id=?",
                    (response, vote, vs, _now(), old["id"]))
    else:
        con.execute("INSERT INTO finch_runs(run_id,iteration,run_index,raw_response,parsed_vote,created_at,validation_status,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (rid, run["current_iteration"], run_index, response, vote, _now(), vs, _now()))
    con.commit(); con.close()
    rows = get_finch_runs(rid, project=project)
    return next((r for r in rows if r["run_index"] == run_index), None)


def get_finch_lab(rid, project="."):
    """Laboratory snapshot: 8 slots, totals, percentages, completeness."""
    rows = get_finch_runs(rid, project=project)
    by_index = {r["run_index"]: r for r in rows}
    runs = []
    for i in range(1, 9):
        r = by_index.get(i)
        vote = (r or {}).get("parsed_vote")
        vs = (r or {}).get("validation_status") or ("VALID" if vote in ("YES", "NO", "INCONCLUSIVE") else ("INVALID" if vote == "INVALID" else "EMPTY"))
        runs.append({"run_index": i, "run_number": i, "raw_response": (r or {}).get("raw_response"),
                     "output": (r or {}).get("raw_response"), "parsed_vote": vote, "vote": vote,
                     "validation_status": vs, "created_at": (r or {}).get("created_at"), "updated_at": (r or {}).get("updated_at")})
    valid = [r for r in runs if r["validation_status"] == "VALID"]
    yes = sum(1 for r in runs if r["parsed_vote"] == "YES")
    no = sum(1 for r in runs if r["parsed_vote"] == "NO")
    inc = sum(1 for r in runs if r["parsed_vote"] == "INCONCLUSIVE")
    inv = sum(1 for r in runs if r["validation_status"] == "INVALID")
    totals = {"YES": yes, "NO": no, "INCONCLUSIVE": inc, "INVALID": inv,
              "yes": yes, "no": no, "inconclusive": inc, "invalid": inv}
    pct = {k: round(totals[k] / 8 * 100, 1) for k in ("YES", "NO", "INCONCLUSIVE", "INVALID")}
    valid_count = len(valid)
    complete = valid_count == 8
    outcome = None
    if complete:
        from agentflow.consensus import evaluate_consensus
        outcome = evaluate_consensus([r["parsed_vote"] for r in runs])["outcome"]
    return {"runs": runs, "totals": totals, "percentages": pct, "valid_count": valid_count,
            "invalid_count": inv, "complete": complete, "outcome": outcome,
            "finalized": False}


def recalc_consensus(rid, project="."):
    """Finalize only when 8 VALID rows exist; else returns incomplete (no save)."""
    lab = get_finch_lab(rid, project=project)
    if not lab["complete"]:
        return lab, None
    from agentflow.consensus import evaluate_consensus
    votes = [r["parsed_vote"] for r in lab["runs"]]
    result = evaluate_consensus(votes)
    # mark finch execution completed before consensus (existing parity)
    try:
        exe = get_execution(rid, "finch", project=project)
        if exe is not None and exe["status"] != "COMPLETED":
            responses = [r["raw_response"] for r in lab["runs"]]
            save_output(rid, "finch", "\n---\n".join(str(x) for x in responses), project)
    except Exception:
        pass
    save_consensus(rid, {"yes": result["yes"], "no": result["no"], "inconclusive": result["inconclusive"],
                         "invalid": result["invalid"], "outcome": result["outcome"],
                         "detail": json.dumps(result)}, project)
    lab["finalized"] = True
    lab["outcome"] = result["outcome"]
    return lab, result


def get_finch_history(rid, project="."):
    con = _connect(project)
    try:
        rows = con.execute("SELECT * FROM finch_history WHERE run_id=? ORDER BY id", (rid,)).fetchall()
        cons = con.execute("SELECT * FROM consensus WHERE run_id=? ORDER BY id", (rid,)).fetchall()
    finally:
        con.close()
    return {"history": [dict(r) for r in rows], "consensus_history": [dict(r) for r in cons]}


def save_consensus(rid, result, project="."):
    run = get_run(rid, project); assert run
    con = _connect(project)
    con.execute("INSERT INTO consensus(run_id,iteration,yes_count,no_count,inconclusive_count,invalid_count,outcome,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (rid, run["current_iteration"], result["yes"], result["no"], result["inconclusive"], result["invalid"], result["outcome"], json.dumps(result), _now()))
    con.commit(); con.close()
    # Legacy outcome mapping + canonical sync: CONSENSUS -> COMPLETED path,
    # SPLIT -> BLOCKED (needs more data). Stored legacy string kept for
    # CLI compat; API derives canonical COMPLETED/BLOCKED.
    status = "CONSENSUS_REACHED" if result["outcome"].startswith("CONSENSUS") else "SPLIT"
    stored = get_run(rid, project)
    if stored and normalize_run_status(stored.get("status")) != "ARCHIVED":
        try:
            set_run_status(rid, status, project, force=False)
        except ValueError:
            set_run_status(rid, status, project, force=True)

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
    """Previous execution history — every row for a run (all iterations) PLUS
    archived retry/overwrite snapshots, oldest first. Filter by agent when given."""
    con = _connect(project)
    try:
        current = con.execute(
            "SELECT * FROM executions WHERE run_id=? ORDER BY iteration, id" if not agent else "SELECT * FROM executions WHERE run_id=? AND agent=? ORDER BY iteration, id",
            (rid, agent) if agent else (rid,)).fetchall()
        archived = con.execute(
            "SELECT * FROM execution_history WHERE run_id=? ORDER BY id" if not agent else "SELECT * FROM execution_history WHERE run_id=? AND agent=? ORDER BY id",
            (rid, agent) if agent else (rid,)).fetchall()
    finally:
        con.close()
    merged = [dict(r) for r in current] + [dict(r) for r in archived]
    merged.sort(key=lambda e: (e.get("iteration", 1), e.get("updated_at") or "", e.get("id", 0)))
    return merged


def get_archived_executions(rid, agent=None, project="."):
    con = _connect(project)
    try:
        if agent:
            rows = con.execute("SELECT * FROM execution_history WHERE run_id=? AND agent=? ORDER BY id", (rid, agent)).fetchall()
        else:
            rows = con.execute("SELECT * FROM execution_history WHERE run_id=? ORDER BY id", (rid,)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def archive_execution_row(row, reason, project="."):
    con = _connect(project)
    try:
        con.execute("INSERT INTO execution_history(run_id,iteration,agent,prompt,output,status,top_hypothesis,raw_data,created_at,updated_at,archived_at,archive_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["run_id"], row["iteration"], row["agent"], row["prompt"], row["output"], row["status"], row["top_hypothesis"], row["raw_data"], row["created_at"], row["updated_at"], _now(), reason))
        con.commit()
    finally:
        con.close()


def mark_execution_failed(rid, agent, reason="", project="."):
    """Invalid response -> FAILED (recoverable, output untouched)."""
    run = get_run(rid, project); assert run
    if agent not in AGENTS_ORDER:
        raise ValueError(f"unknown agent {agent}")
    con = _connect(project)
    cur = con.execute("SELECT * FROM executions WHERE run_id=? AND agent=? AND iteration=?",
                      (rid, agent, run["current_iteration"])).fetchone()
    assert cur, f"no execution slot for {agent}"
    if normalize_exec_status(cur["status"]) == "COMPLETED":
        con.close(); raise ValueError(f"cannot mark completed {agent} FAILED — use RETRY")
    con.execute("UPDATE executions SET status='FAILED', updated_at=? WHERE id=?", (_now(), cur["id"]))
    con.commit(); con.close()
    sync_run_status(rid, project)
    return get_execution(rid, agent, project=project)


def set_execution_status(rid, agent, status, project="."):
    run = get_run(rid, project); assert run
    if agent not in AGENTS_ORDER:
        raise ValueError(f"unknown agent {agent}")
    if normalize_exec_status(status) not in KNOWN_EXEC_STATES:
        raise ValueError(f"invalid execution state {status}")
    con = _connect(project)
    cur = con.execute("SELECT * FROM executions WHERE run_id=? AND agent=? AND iteration=?",
                      (rid, agent, run["current_iteration"])).fetchone()
    assert cur
    err = None
    if not can_transition_exec(cur["status"], status):
        # Still allow administrative FAILED/BLOCKED marking from any non-COMPLETED
        if not (normalize_exec_status(cur["status"]) != "COMPLETED" and normalize_exec_status(status) in ("FAILED", "BLOCKED")):
            con.close(); raise ValueError(f"Illegal execution transition {cur['status']} -> {status}")
    con.execute("UPDATE executions SET status=?, updated_at=? WHERE id=?", (status, _now(), cur["id"]))
    con.commit(); con.close()
    sync_run_status(rid, project)
    return get_execution(rid, agent, project=project)


def sync_run_status(rid, project="."):
    """Re-derive run status from DB truth and persist (force, bypasses
    transition map since this is sync, not user transition)."""
    from agentflow.workflow import derive_run_status
    run = get_run(rid, project)
    if not run or normalize_run_status(run.get("status")) == "ARCHIVED":
        return (run or {}).get("status")
    exes = get_executions(rid, iteration=run["current_iteration"], project=project)
    cons = None
    try:
        cons = get_consensus(rid, project=project)
    except Exception:
        cons = None
    derived = derive_run_status(run, exes, consensus=cons, stored=run.get("status"))
    if derived != run.get("status"):
        con = _connect(project)
        con.execute("UPDATE runs SET status=?, updated_at=? WHERE id=?", (derived, _now(), rid))
        con.commit(); con.close()
    return derived


def progress_for(rid, project="."):
    from agentflow.workflow import compute_progress
    run = get_run(rid, project); assert run
    exes = get_executions(rid, iteration=run["current_iteration"], project=project)
    cons = get_consensus(rid, project=project)
    return compute_progress(run, exes, consensus=cons)


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
        # Phase 6: retry must preserve previous execution(s) — archive first
        for arch_agent in downstream:
            row = con.execute("SELECT * FROM executions WHERE run_id=? AND agent=? AND iteration=?",
                              (rid, arch_agent, run["current_iteration"])).fetchone()
            if row and (row["prompt"] or row["output"]):
                con.execute("INSERT INTO execution_history(run_id,iteration,agent,prompt,output,status,top_hypothesis,raw_data,created_at,updated_at,archived_at,archive_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (row["run_id"], row["iteration"], row["agent"], row["prompt"], row["output"], row["status"], row["top_hypothesis"], row["raw_data"], row["created_at"], row["updated_at"], _now(), "retry"))
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
