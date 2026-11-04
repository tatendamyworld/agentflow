"""Visual-backend persistence helpers for AgentFlow.

Shares the CLI's SQLite database (``.agentflow/state.db``) via
:mod:`agentflow.db`.  Only *adds* two new tables (``hypotheses`` and
``run_meta``); existing CLI tables are never altered, so CLI-created
databases keep working unchanged.

Hypotheses may also be parsed out of a completed Ranking execution's
output (``H1.`` / ``E1.`` style lines) — see
:func:`parse_hypotheses_from_text` and
:func:`sync_hypotheses_from_ranking`.
"""
import datetime
import re

from agentflow import db

HYPOTHESIS_COLUMNS = (
    "id", "run_id", "iteration", "title", "description", "origin_agent",
    "score", "strengths", "weaknesses", "survivability", "ranking",
    "notes", "is_survivor", "created_at",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  iteration INTEGER NOT NULL DEFAULT 1,
  title TEXT,
  description TEXT,
  origin_agent TEXT,
  score REAL,
  strengths TEXT,
  weaknesses TEXT,
  survivability REAL,
  ranking INTEGER,
  notes TEXT,
  is_survivor INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS run_meta (
  run_id TEXT PRIMARY KEY,
  name TEXT,
  description TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hypotheses_run
  ON hypotheses(run_id, iteration);
"""


def _now():
    return datetime.datetime.utcnow().isoformat()


def init_db(project="."):
    """Create the visual-backend tables if they do not exist yet.

    Idempotent; safe to call on every app boot / helper call.  Running
    ``db._connect`` first also creates the CLI schema + DB file if needed.
    """
    con = db._connect(project)
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def _connect(project="."):
    init_db(project)
    return db._connect(project)


# ---------------------------------------------------------------------------
# run meta (optional name / description for runs; CLI runs table has none)
# ---------------------------------------------------------------------------

def get_run_meta(project, run_id):
    con = _connect(project)
    try:
        row = con.execute(
            "SELECT * FROM run_meta WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def set_run_meta(project, run_id, name=None, description=None):
    """Upsert run meta.  ``None`` leaves the stored value unchanged."""
    init_db(project)
    existing = get_run_meta(project, run_id) or {}
    name = existing.get("name") if name is None else name
    description = existing.get("description") if description is None else description
    con = _connect(project)
    try:
        con.execute(
            "INSERT INTO run_meta(run_id, name, description, updated_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET name=excluded.name, "
            "description=excluded.description, updated_at=excluded.updated_at",
            (run_id, name, description, _now()),
        )
        con.commit()
    finally:
        con.close()
    return get_run_meta(project, run_id) or {
        "run_id": run_id, "name": name, "description": description}


# ---------------------------------------------------------------------------
# hypotheses
# ---------------------------------------------------------------------------

def _row_to_dict(row):
    d = dict(row)
    if d.get("is_survivor") is not None:
        d["is_survivor"] = bool(d["is_survivor"])
    return d


def list_hypotheses(project, run_id, iteration=None):
    con = _connect(project)
    try:
        if iteration is None:
            rows = con.execute(
                "SELECT * FROM hypotheses WHERE run_id=? "
                "ORDER BY iteration, ranking IS NULL, ranking, id",
                (run_id,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM hypotheses WHERE run_id=? AND iteration=? "
                "ORDER BY ranking IS NULL, ranking, id",
                (run_id, iteration),
            ).fetchall()
    finally:
        con.close()
    return [_row_to_dict(r) for r in rows]


def get_hypothesis(project, hyp_id):
    con = _connect(project)
    try:
        row = con.execute("SELECT * FROM hypotheses WHERE id=?", (hyp_id,)).fetchone()
    finally:
        con.close()
    return _row_to_dict(row) if row else None


def create_hypothesis(project, run_id, iteration=1, title=None, description=None,
                      origin_agent=None, score=None, strengths=None,
                      weaknesses=None, survivability=None, ranking=None,
                      notes=None, is_survivor=False):
    con = _connect(project)
    try:
        cur = con.execute(
            "INSERT INTO hypotheses(run_id, iteration, title, description, "
            "origin_agent, score, strengths, weaknesses, survivability, "
            "ranking, notes, is_survivor, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, iteration, title, description, origin_agent, score,
             strengths, weaknesses, survivability, ranking, notes,
             1 if is_survivor else 0, _now()),
        )
        con.commit()
        new_id = cur.lastrowid
    finally:
        con.close()
    return get_hypothesis(project, new_id)


def update_hypothesis(project, hyp_id, fields):
    """Update whitelisted columns only.  Returns the row or ``None``."""
    allowed = {c: fields[c] for c in HYPOTHESIS_COLUMNS
               if c in fields and c not in ("id", "run_id", "created_at")}
    if "is_survivor" in allowed:
        allowed["is_survivor"] = 1 if allowed["is_survivor"] else 0
    if not allowed:
        return get_hypothesis(project, hyp_id)
    assignments = ", ".join(f"{c}=?" for c in allowed)
    con = _connect(project)
    try:
        cur = con.execute(
            f"UPDATE hypotheses SET {assignments} WHERE id=?",
            (*allowed.values(), hyp_id),
        )
        con.commit()
        if cur.rowcount == 0:
            return None
    finally:
        con.close()
    return get_hypothesis(project, hyp_id)


# ---------------------------------------------------------------------------
# parsing hypotheses out of Ranking output
# ---------------------------------------------------------------------------

_HYP_LINE_RE = re.compile(
    r"^[ \t]*[-*•]?[ \t]*(H\d+|E\d+)\b[ \t]*[\.\)\:\-]?[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_SCORE_RE = re.compile(
    r"\b(?:score|survivability)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(?:/\s*(\d+(?:\.\d+)?))?",
    re.IGNORECASE,
)
_SCORE_FRAC_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*/\s*10\b")
_WINNER_RE = re.compile(
    r"\b(?:top\s+hypothesis|winner)\s*[:\-]\s*(H\d+|E\d+)?\s*(.*)",
    re.IGNORECASE,
)


def _extract_score(text):
    m = _SCORE_FRAC_RE.search(text or "")
    if m:
        return float(m.group(1))
    m = _SCORE_RE.search(text or "")
    if m:
        return float(m.group(1))
    return None


def _extract_survivability(text):
    m = re.search(r"\bsurvivability\s*[:=]?\s*(\d+(?:\.\d+)?)",
                  text or "", re.IGNORECASE)
    return float(m.group(1)) if m else None


def parse_hypotheses_from_text(text, iteration=1):
    """Parse ``H1.``/``E1.`` lines from Ranking output.

    Returns a list of hypothesis field dicts (ready for
    :func:`create_hypothesis`), or ``[]`` when nothing parseable is found.
    The winner mentioned in a ``Top hypothesis: ...`` line (if any) is
    marked ``is_survivor``.
    """
    if not text or not str(text).strip():
        return []
    winner = None
    wm = _WINNER_RE.search(str(text))
    if wm and wm.group(1):
        winner = wm.group(1).upper()

    parsed = []
    seen = set()
    for m in _HYP_LINE_RE.finditer(str(text)):
        label = m.group(1).upper()
        body = (m.group(2) or "").strip()
        if label in seen:
            continue
        seen.add(label)
        # Skip lines that are clearly not a hypothesis statement.
        if not body and label == (winner or ""):
            continue
        # Skip tournament lines like "E1 beats E2: reason" — not a
        # hypothesis statement.
        if re.match(r"beats\b", body, re.IGNORECASE) or " beats " in body.lower():
            continue
        title = body[:160] if body else label
        origin = "generation" if label.startswith("H") else "evolution"
        parsed.append({
            "iteration": iteration,
            "title": f"{label} — {title}" if body else label,
            "description": body,
            "origin_agent": origin,
            "score": _extract_score(body),
            "strengths": None,
            "weaknesses": None,
            "survivability": _extract_survivability(body),
            "ranking": len(parsed) + 1,
            "notes": None,
            "is_survivor": bool(winner and label == winner),
        })
    return parsed


def sync_hypotheses_from_ranking(project, run_id, iteration, ranking_output):
    """Return stored hypotheses, parsing+persisting Ranking output if none.

    Persisting on first parse lets the UI PATCH/edit parsed rows by id.
    Repeated calls are no-ops once rows exist.
    """
    existing = list_hypotheses(project, run_id, iteration=iteration)
    if existing:
        return existing
    parsed = parse_hypotheses_from_text(ranking_output, iteration=iteration or 1)
    created = []
    for p in parsed:
        created.append(create_hypothesis(project, run_id, **p))
    return created
