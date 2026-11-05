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
    "evidence", "elo_rating", "status",
)

HYPOTHESIS_STATUSES = ("ACTIVE", "SELECTED", "ARCHIVED")

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
  evidence TEXT,
  elo_rating REAL,
  status TEXT DEFAULT 'ACTIVE',
  FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS run_meta (
  run_id TEXT PRIMARY KEY,
  name TEXT,
  description TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ranking_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  iteration INTEGER NOT NULL DEFAULT 1,
  hyp_id INTEGER,
  event_type TEXT NOT NULL,
  prev_rank INTEGER, new_rank INTEGER,
  prev_elo REAL, new_elo REAL,
  prev_score REAL, new_score REAL,
  created_at TEXT,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS ranking_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  iteration INTEGER NOT NULL DEFAULT 1,
  winner_label TEXT, loser_label TEXT,
  winner_hyp_id INTEGER, loser_hyp_id INTEGER,
  reason TEXT, parsed_at TEXT,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_ranking_events_run ON ranking_events(run_id, iteration);
CREATE INDEX IF NOT EXISTS idx_ranking_matches_run ON ranking_matches(run_id, iteration);
CREATE INDEX IF NOT EXISTS idx_hypotheses_run
  ON hypotheses(run_id, iteration);
"""


def _now():
    return datetime.datetime.utcnow().isoformat()


_MIGRATE_COLS = (("evidence", "TEXT"), ("elo_rating", "REAL"), ("status", "TEXT DEFAULT 'ACTIVE'"))

def _ensure_columns(con):
    cols = [r[1] for r in con.execute("PRAGMA table_info(hypotheses)").fetchall()]
    for name, ddl in _MIGRATE_COLS:
        if name not in cols:
            con.execute(f"ALTER TABLE hypotheses ADD COLUMN {name} {ddl}")
    # backfill status from is_survivor for legacy rows
    con.execute("UPDATE hypotheses SET status='SELECTED' WHERE status IS NULL AND is_survivor=1")
    con.execute("UPDATE hypotheses SET status='ACTIVE' WHERE status IS NULL")

def init_db(project="."):
    """Create + migrate visual-backend tables (idempotent)."""
    con = db._connect(project)
    try:
        con.executescript(SCHEMA)
        _ensure_columns(con)
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
    if not d.get("status"):
        d["status"] = "SELECTED" if d.get("is_survivor") else "ACTIVE"
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
                      notes=None, is_survivor=False, evidence=None,
                      elo_rating=None, status=None):
    # evidence alias: strengths fallback (never fabricate lines beyond input)
    if evidence is None:
        evidence = strengths
    st = status or ("SELECTED" if is_survivor else "ACTIVE")
    con = _connect(project)
    try:
        cur = con.execute(
            "INSERT INTO hypotheses(run_id, iteration, title, description, "
            "origin_agent, score, strengths, weaknesses, survivability, "
            "ranking, notes, is_survivor, created_at, evidence, elo_rating, status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, iteration, title, description, origin_agent, score,
             strengths, weaknesses, survivability, ranking, notes,
             1 if st == "SELECTED" else 0, _now(), evidence, elo_rating, st),
        )
        con.commit()
        new_id = cur.lastrowid
    finally:
        con.close()
    return get_hypothesis(project, new_id)


def update_hypothesis(project, hyp_id, fields):
    """Update whitelisted columns only.  Returns the row or ``None``.

    Keeps ``status`` <-> ``is_survivor`` consistent so selection state has
    one source of truth (SQLite), not frontend state.
    """
    allowed = {c: fields[c] for c in HYPOTHESIS_COLUMNS
               if c in fields and c not in ("id", "run_id", "created_at")}
    # evidence alias: keep legacy strengths mirror when evidence set
    if "evidence" in allowed and "strengths" not in allowed:
        allowed["strengths"] = allowed["evidence"]
    if "status" not in allowed and "is_survivor" in allowed:
        allowed["status"] = "SELECTED" if allowed["is_survivor"] else "ACTIVE"
    if "status" in allowed and "is_survivor" not in allowed:
        allowed["is_survivor"] = (allowed["status"] == "SELECTED")
    if "is_survivor" in allowed:
        allowed["is_survivor"] = 1 if allowed["is_survivor"] else 0
    if not allowed:
        return get_hypothesis(project, hyp_id)
    before = get_hypothesis(project, hyp_id)
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
    after = get_hypothesis(project, hyp_id)
    # ranking history: record only when scoring fields changed
    if before is not None and after is not None:
        changed = any(before.get(f) != after.get(f) for f in ("ranking", "elo_rating", "score", "survivability", "status"))
        if changed:
            try:
                _record_event(project, before.get("run_id"), before.get("iteration"),
                              hyp_id, "EDIT", before.get("ranking"), after.get("ranking"),
                              before.get("elo_rating"), after.get("elo_rating"),
                              before.get("score"), after.get("score"))
            except Exception:
                pass
    return after


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
        # Skip tournament lines like "E1 beats E2: reason" BEFORE marking
        # the label seen — otherwise beats lines consume E1/E2 and the real
        # evolved-candidate list below is silently dropped.
        if re.match(r"beats\b", body, re.IGNORECASE) or " beats " in body.lower():
            continue
        if label in seen:
            continue
        seen.add(label)
        # Skip lines that are clearly not a hypothesis statement.
        if not body and label == (winner or ""):
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
            "evidence": None,
            "elo_rating": None,
            "status": None,
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


# ---------------------------------------------------------------------------
# Hypothesis Laboratory (PHASE 7): import, compare, select, archive
# ---------------------------------------------------------------------------

def _label_of(title):
    m = re.match(r"^\s*(H\d+|E\d+)\b", title or "", re.I)
    return m.group(1).upper() if m else None


def import_from_agent_output(project, run_id, iteration, agent_id, output_text):
    """Import hypotheses from one agent's completed output.

    Returns dict {imported, created_ids, found, parse_ok, message}.
    On parse failure (0 lines) nothing is fabricated — caller/UX must
    offer manual entry. Dedupe by H/E label within run+iteration.
    """
    parsed = parse_hypotheses_from_text(output_text or "", iteration=iteration or 1)
    # Honour actual source agent for generation/evolution outputs; ranking
    # summaries keep H/E inference (generation/evolution).
    for entry in parsed:
        if agent_id in ("generation", "evolution", "reflection", "proximity"):
            entry["origin_agent"] = agent_id
    if not parsed:
        return {"imported": 0, "created_ids": [], "found": 0, "parse_ok": False,
                "message": f"No parseable H#/E# hypotheses in {agent_id} output — use manual entry (no data fabricated)."}
    existing = list_hypotheses(project, run_id, iteration=iteration)
    seen_labels = set(_label_of(h.get("title") or "") for h in existing)
    seen_labels.discard(None)
    nxt_rank = (max((h.get("ranking") or 0) for h in existing) if existing else 0) + 1
    created = []
    for entry in parsed:
        label = _label_of(entry.get("title") or "")
        if label and label in seen_labels:
            continue
        if entry.get("ranking") is None:
            entry["ranking"] = nxt_rank
        created.append(create_hypothesis(project, run_id, **entry))
        if label:
            seen_labels.add(label)
        nxt_rank += 1
    return {"imported": len(created), "created_ids": [c["id"] for c in created],
            "found": len(parsed), "parse_ok": True,
            "message": f"Imported {len(created)}/{len(parsed)} hypotheses from {agent_id}."}


def import_hypotheses_from_outputs(project, run_id, iteration, executions, agent_ids=None):
    """Scan completed executions (default: generation/evolution/ranking) and import."""
    allowed = agent_ids or ["generation", "evolution", "ranking"]
    per_agent, total_new = [], 0
    for e in executions or []:
        if e.get("agent") not in allowed:
            continue
        if e.get("status") != "COMPLETED" or not e.get("output"):
            continue
        report = import_from_agent_output(project, run_id, iteration, e["agent"], e["output"])
        per_agent.append({"agent": e["agent"], **report})
        total_new += report.get("imported", 0)
    all_ok = any(r.get("parse_ok") for r in per_agent)
    if not per_agent:
        return {"imported": 0, "parse_ok": False, "per_agent": [],
                "message": "No completed generation/evolution/ranking output — save agent responses first, or enter hypotheses manually."}
    return {"imported": total_new, "parse_ok": total_new > 0, "per_agent": per_agent,
            "message": (f"Imported {total_new} hypotheses." if total_new else "No parseable hypotheses found — use manual entry (no data fabricated).")}


def compare_hypotheses(a_row, b_row):
    """Structured side-by-side payload — no fabricated verdict."""
    def num(x):
        return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None
    sa, sb = num(a_row.get("survivability")), num(b_row.get("survivability"))
    ea, eb = num(a_row.get("elo_rating")), num(b_row.get("elo_rating"))
    return {
        "a": a_row, "b": b_row,
        "deltas": {
            "survivability_a": sa, "survivability_b": sb,
            "survivability_diff": (round(sa - sb, 2) if sa is not None and sb is not None else None),
            "elo_a": ea, "elo_b": eb,
            "elo_diff": (round(ea - eb, 2) if ea is not None and eb is not None else None),
        },
        "verdict": None,  # verdict only via explicit winner parameter
    }


def record_compare_win(project, winner_id, loser_id, k=32.0):
    """ELO-style update after an explicit pairwise win. Unrated starts at 1500."""
    w = get_hypothesis(project, winner_id)
    l = get_hypothesis(project, loser_id)
    if w is None or l is None:
        raise ValueError("winner/loser hypothesis not found")
    wr, lr = (float(w["elo_rating"]) if w.get("elo_rating") is not None else 1500.0,
              float(l["elo_rating"]) if l.get("elo_rating") is not None else 1500.0)
    exp_w = 1.0 / (1.0 + 10 ** ((lr - wr) / 400.0))
    exp_l = 1.0 - exp_w
    new_wr = wr + k * (1.0 - exp_w)
    new_lr = lr + k * (0.0 - exp_l)
    update_hypothesis(project, winner_id, {"elo_rating": new_wr})
    update_hypothesis(project, loser_id, {"elo_rating": new_lr})
    return get_hypothesis(project, winner_id), get_hypothesis(project, loser_id)


def select_hypothesis(project, run_id, hyp_id):
    """Select as top for Crow. Persists SQLite + Crow TOP_HYPOTHESIS.

    Only one SELECTED per run+iteration. Archived cannot be selected.
    Returns (hypothesis_row, top_text).
    """
    hyp = get_hypothesis(project, hyp_id)
    if hyp is None or hyp["run_id"] != run_id:
        raise ValueError(f"hypothesis {hyp_id} not found in run {run_id}")
    if _row_to_dict(hyp).get("status") == "ARCHIVED":
        raise ValueError("archived hypotheses cannot be selected — restore first")
    if hyp.get("iteration") is None:
        raise ValueError("selected hypothesis has no iteration")
    run = db.get_run(run_id, project)
    assert run, "run not found"
    # Clear previous selection in this run+iteration
    for h in list_hypotheses(project, run_id, iteration=hyp["iteration"]):
        if h["id"] != hyp_id and _row_to_dict(h).get("status") == "SELECTED":
            update_hypothesis(project, h["id"], {"status": "ACTIVE", "is_survivor": False})
    updated = update_hypothesis(project, hyp_id, {"status": "SELECTED", "is_survivor": True})
    top_text = updated.get("title") or updated.get("description") or ""
    # Persist to Crow's prompt source (executions.top_hypothesis on ranking slot
    # when it exists; else via run current ranking slot if present)
    try:
        db.save_top_hypothesis(run_id, str(top_text), project)
    except AssertionError:
        # No ranking execution slot (edge: very early selection) — store on
        # hypotheses row only; Crow generation will surface missing warning.
        pass
    return updated, str(top_text)


def archive_hypothesis(project, hyp_id):
    hyp = get_hypothesis(project, hyp_id)
    if hyp is None:
        raise ValueError(f"hypothesis {hyp_id} not found")
    if _row_to_dict(hyp).get("status") == "SELECTED":
        # Clearing selection must also clear Crow top if it references this hyp
        pass
    return update_hypothesis(project, hyp_id, {"status": "ARCHIVED", "is_survivor": False})


def restore_hypothesis(project, hyp_id):
    hyp = get_hypothesis(project, hyp_id)
    if hyp is None:
        raise ValueError(f"hypothesis {hyp_id} not found")
    if _row_to_dict(hyp).get("status") != "ARCHIVED":
        raise ValueError(f"hypothesis {hyp_id} is not archived")
    return update_hypothesis(project, hyp_id, {"status": "ACTIVE", "is_survivor": False})


# ---------------------------------------------------------------------------
# Ranking Dashboard (PHASE 8): matches, history, leaderboard, reorder
# ---------------------------------------------------------------------------

_BEATS_RE = re.compile(r"(?i)(H\d+|E\d+)\s+beats\s+(H\d+|E\d+)\s*(?:[:\-—]\s*(.*))?")
_WINNER_LABEL_RE = re.compile(r"(?i)top\s+hypothesis\s*[:\-]\s*(?:(H\d+|E\d+)\b\s*[\.\-]?\s*)?(.*)")

def parse_tournament_results(text):
    """Parse 'X beats Y: reason' lines from Ranking output. No fabrication: honest [] on none."""
    if not text or not str(text).strip():
        return []
    out = []
    for m in _BEATS_RE.finditer(str(text)):
        winner = m.group(1).upper()
        loser = m.group(2).upper()
        reason = (m.group(3) or "").strip()
        out.append({"winner_label": winner, "loser_label": loser, "reason": reason,
                    "winner": winner, "loser": loser})
    # dedupe preserve order
    seen = set()
    uniq = []
    for x in out:
        k = (x["winner_label"], x["loser_label"], x["reason"])
        if k in seen:
            continue
        seen.add(k); uniq.append(x)
    return uniq


def _record_event(project, run_id, iteration, hyp_id, event_type, prev_rank, new_rank, prev_elo, new_elo, prev_score, new_score):
    if run_id is None:
        return None
    con = _connect(project)
    try:
        cur = con.execute(
            "INSERT INTO ranking_events(run_id,iteration,hyp_id,event_type,prev_rank,new_rank,prev_elo,new_elo,prev_score,new_score,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, iteration or 1, hyp_id, event_type, prev_rank, new_rank, prev_elo, new_elo, prev_score, new_score, _now()),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def list_ranking_events(project, run_id, iteration=None):
    con = _connect(project)
    try:
        if iteration is None:
            rows = con.execute("SELECT * FROM ranking_events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM ranking_events WHERE run_id=? AND iteration=? ORDER BY id", (run_id, iteration)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def list_ranking_matches(project, run_id, iteration=None):
    con = _connect(project)
    try:
        if iteration is None:
            rows = con.execute("SELECT * FROM ranking_matches WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM ranking_matches WHERE run_id=? AND iteration=? ORDER BY id", (run_id, iteration)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def sync_ranking_matches(project, run_id, iteration, ranking_output, hypotheses=None):
    """Persist parsed 'X beats Y' results once (idempotent). Resolves labels to hyp ids when possible."""
    if list_ranking_matches(project, run_id, iteration=iteration):
        return list_ranking_matches(project, run_id, iteration=iteration)
    parsed = parse_tournament_results(ranking_output)
    if not parsed:
        return []
    hyps = hypotheses or list_hypotheses(project, run_id, iteration=iteration)
    by_label = {}
    for h in hyps:
        lab = _label_of(h.get("title") or "")
        if lab:
            by_label[lab] = h["id"]
    con = _connect(project)
    try:
        for m in parsed:
            con.execute("INSERT INTO ranking_matches(run_id,iteration,winner_label,loser_label,winner_hyp_id,loser_hyp_id,reason,parsed_at) VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, iteration or 1, m["winner_label"], m["loser_label"], by_label.get(m["winner_label"]), by_label.get(m["loser_label"]), m["reason"], _now()))
        con.commit()
    finally:
        con.close()
    return list_ranking_matches(project, run_id, iteration=iteration)


def reorder_hypotheses(project, run_id, iteration, ordered_ids):
    """Manual correction: assign ranking=1..n in given order, record events."""
    if not ordered_ids:
        raise ValueError("order must not be empty")
    hyps = {h["id"]: h for h in list_hypotheses(project, run_id, iteration=iteration)}
    for hid in ordered_ids:
        if hid not in hyps:
            raise ValueError(f"hypothesis {hid} not in run {run_id} iteration {iteration}")
    updated = []
    for idx, hid in enumerate(ordered_ids, start=1):
        before = hyps[hid]
        if before.get("ranking") != idx:
            after = update_hypothesis(project, hid, {"ranking": idx})
            # update_hypothesis already recorded EDIT event; add REORDER tag via extra event
            _record_event(project, run_id, iteration, hid, "REORDER", before.get("ranking"), idx, before.get("elo_rating"), after.get("elo_rating") if after else None, before.get("score"), after.get("score") if after else None)
            updated.append(after)
        else:
            updated.append(before)
    return updated


def get_leaderboard(project, run_id, iteration):
    """Leaderboard sorted by stored ranking (then ELO desc, surv desc). Archived excluded."""
    rows_h = [h for h in list_hypotheses(project, run_id, iteration=iteration) if _row_to_dict(h).get("status") != "ARCHIVED"]
    rows_h.sort(key=lambda h: (
        h.get("ranking") is None, h.get("ranking") or 9999,
        -(h.get("elo_rating") or -1), -(h.get("survivability") or -1)))
    # elo viz scaling among leaderboard (actual values, min-max among rated)
    rated = [h["elo_rating"] for h in rows_h if h.get("elo_rating") is not None]
    lo, hi = (min(rated), max(rated)) if rated else (None, None)
    board = []
    for i, h in enumerate(rows_h, start=1):
        elo = h.get("elo_rating")
        if elo is None:
            pct = None
        elif lo == hi:
            pct = 100.0
        else:
            pct = round((float(elo) - lo) / (hi - lo) * 100, 1)
        # latest rank change from history
        evs = [e for e in list_ranking_events(project, run_id, iteration=iteration) if e.get("hyp_id") == h["id"]]
        change = None
        if evs:
            last = evs[-1]
            if last.get("prev_rank") is not None and last.get("new_rank") is not None:
                change = int(last["prev_rank"]) - int(last["new_rank"])
        item = _row_to_dict(h)
        item["leaderboard_rank"] = i if h.get("ranking") is None else h.get("ranking")
        item["rank_change"] = change
        item["elo_pct"] = pct
        item["elo_min"] = lo
        item["elo_max"] = hi
        board.append(item)
    return board


def get_ranking_history(project, run_id):
    """Ranking history: per-iteration execution snapshot + ranked lists + change events."""
    # iterations present in executions
    iters = set()
    try:
        for e in db.get_executions(run_id, project=project):
            iters.add(e.get("iteration"))
    except Exception:
        pass
    iters = sorted([i for i in iters if i is not None])
    snaps = []
    for it in iters:
        rex = None
        try:
            for e in db.get_executions(run_id, iteration=it, project=project):
                if e.get("agent") == "ranking":
                    rex = e
        except Exception:
            pass
        board = get_leaderboard(project, run_id, it) if it else []
        snaps.append({"iteration": it,
                      "output": (rex or {}).get("output") if rex else None,
                      "top_hypothesis": (rex or {}).get("top_hypothesis") if rex else None,
                      "leaderboard": [{k: b.get(k) for k in ("id", "title", "elo_rating", "survivability", "score", "ranking", "status", "leaderboard_rank", "rank_change", "created_at")} for b in board]})
    events = list_ranking_events(project, run_id)
    matches = list_ranking_matches(project, run_id)
    return {"snapshots": snaps, "events": events, "matches": matches}
