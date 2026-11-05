"""Complete Markdown export — Phase 12. No content omitted, raw preserved."""
import json

def fence(text):
    s = text or "(empty)"
    longest = max((len(m.group(0)) for m in __import__("re").finditer(r"`{3,}", s)), default=0)
    f = "`" * max(3, longest + 1)
    return f"{f}\n{s}\n{f}"


def build_markdown(project, run_id):
    from agentflow import db, backend_models
    run = db.get_run(run_id, project)
    assert run, "run not found"
    meta = backend_models.get_run_meta(project, run_id) or {}
    exes_all = db.get_executions(run_id, project=project)
    its = sorted({e.get("iteration") for e in exes_all if e.get("iteration") is not None})
    lines = [f"# AgentFlow Run {run_id}"]
    lines += [f"Exported: {db._now() if hasattr(db, '_now') else ''}", ""]
    lines += ["## Mission details",
              f"- ID: {run_id}",
              f"- Goal: {run.get('goal')}",
              f"- Name: {meta.get('name') or '-'}",
              f"- Description: {meta.get('description') or '-'}",
              f"- Stored status: {run.get('status')}",
              f"- Current iteration: {run.get('current_iteration')}",
              f"- Created: {run.get('created_at')} Updated: {run.get('updated_at')}",
              f"- Iterations: {its}"]
    if meta.get("description"):
        lines += ["", str(meta["description"])]
    lines += [""]
    # progress
    try:
        from agentflow.workflow import compute_progress
        cur_exes = [e for e in exes_all if e.get("iteration") == run.get("current_iteration")]
        prog = compute_progress(run, cur_exes)
        lines += ["## Progress (current iteration)", f"- Completed: {prog['completed']}/{prog['total']} ({prog['percentage']}%) - Next: {prog['next_agent']} - Derived status: {prog['status']}", ""]
    except Exception:
        pass
    for it in its:
        lines += [f"# Iteration {it}", ""]
        iter_exes = [e for e in exes_all if e.get("iteration") == it]
        for e in iter_exes:
            lines += [f"## {e['agent'].capitalize()} (iteration {it})",
                      f"Status: {e['status']} Created: {e.get('created_at')} Updated: {e.get('updated_at')}",
                      "### Prompt", fence(e.get("prompt") or "(none)"),
                      "### Response (raw)", fence(e.get("output") or "(none)")]
            if e.get("top_hypothesis"):
                lines += [f"Top hypothesis: {e['top_hypothesis']}"]
            lines += [""]
        # hypotheses (sync ranking parse first so export never omits parsed candidates)
        try:
            rex = next((e for e in [x for x in exes_all if x.get("iteration") == it] if e.get("agent") == "ranking"), None)
            backend_models.sync_hypotheses_from_ranking(project, run_id, it, (rex or {}).get("output") or "")
        except Exception:
            pass
        hyps = backend_models.list_hypotheses(project, run_id, iteration=it)
        if hyps:
            lines += ["## Hypotheses", ""]
            for h in hyps:
                hp = dict(h)
                st = hp.get("status") or ("SELECTED" if hp.get("is_survivor") else "ACTIVE")
                lines += [f"- #{h['id']} {h.get('title') or ''}",
                          f"  Description: {h.get('description') or ''}",
                          f"  Source: {h.get('origin_agent') or ''} Iteration: {h.get('iteration')} Ranking: {h.get('ranking')} Status: {st}",
                          f"  Score: {h.get('score')} Survivability: {h.get('survivability')} ELO: {h.get('elo_rating')}",
                          f"  Evidence: {h.get('evidence') or h.get('strengths') or ''}",
                          f"  Weaknesses: {h.get('weaknesses') or ''}",
                          f"  Created: {h.get('created_at')}"]
            lines += [""]
        # rankings
        board = backend_models.get_leaderboard(project, run_id, it)
        if board:
            lines += ["## Rankings (leaderboard)", ""]
            for b in board:
                lines += [f"- #{b.get('leaderboard_rank') or b.get('ranking')} {b.get('title')} | ELO {b.get('elo_rating')} Survivability {b.get('survivability')} Score {b.get('score')} RankChg {b.get('rank_change')} Status {b.get('status')}"]
            lines += [""]
        matches = backend_models.list_ranking_matches(project, run_id, iteration=it)
        if matches:
            lines += ["## Comparative results (tournament)", ""]
            for m in matches:
                lines += [f"- {m.get('winner_label')} beats {m.get('loser_label')}: {m.get('reason') or ''}"]
            lines += [""]
        events = [e for e in backend_models.list_ranking_events(project, run_id, iteration=it)]
        if events:
            lines += ["## Ranking changes", ""]
            for e in events:
                lines += [f"- Hyp {e.get('hyp_id')} {e.get('event_type')} rank {e.get('prev_rank')}->{e.get('new_rank')} elo {e.get('prev_elo')}->{e.get('new_elo')} at {e.get('created_at')}"]
            lines += [""]
        # workspaces
        for ws in ("crow", "falcon"):
            notes, _ = backend_models.get_workspace_notes(project, run_id, ws, it)
            if any(str(v).strip() for v in notes.values()):
                lines += [f"## {ws.capitalize()} workspace", ""]
                for k, v in notes.items():
                    lines += [f"### {k}", fence(v or "(empty)"), ""]
        # finch
        finch = db.get_finch_runs(run_id, iteration=it, project=project)
        if finch:
            lines += ["## Finch evaluations (raw)", ""]
            for f in finch:
                lines += [f"### Run {f['run_index']}",
                          f"Vote: {f.get('parsed_vote')} Validation: {f.get('validation_status')} Created: {f.get('created_at')} Updated: {f.get('updated_at')}",
                          "Raw response:", fence(f.get("raw_response") or "(empty)"), ""]
        # finch history snapshots for iteration
        fh = db.get_finch_history(run_id, project=project) if hasattr(db, "get_finch_history") else {"history": []}
        hist_it = [h for h in fh.get("history", []) if h.get("iteration") == it]
        if hist_it:
            lines += ["## Finch history (archives)", ""]
            for h in hist_it:
                lines += [f"- {h.get('created_at')} outcome={h.get('outcome')} votes={h.get('votes_json')}"]
            lines += [""]
        # consensus per iteration
        con = db.get_consensus(run_id, iteration=it, project=project)
        if con:
            lines += ["## Consensus", f"Outcome: {con.get('outcome')} YES {con.get('yes_count')} NO {con.get('no_count')} INCONCLUSIVE {con.get('inconclusive_count')} INVALID {con.get('invalid_count')} Created: {con.get('created_at')}", "Detail:", fence(con.get("detail") or ""), ""]
    # iteration history
    lines += ["## Iteration history", f"- Iterations: {its} - Current: {run.get('current_iteration')}", ""]
    # unresolved from summaries
    for it in its:
        s = backend_models.iteration_summary(project, run_id, it)
        lines += [f"### Iteration {it} unresolved", ""]
        lines += [f"- {u}" for u in (s.get("unresolved") or [])] or ["- (none)"]
        lines += ["", f"Top: {s.get('top_hypothesis') or '-'}"]
        lines += [""]
    # execution history archive (retry/overwrite preservation)
    try:
        hist = db.get_execution_history(run_id, project=project)
        archived = [h for h in hist if h.get("archive_reason")]
        if archived:
            lines += ["## Archived executions (retry/overwrite)", ""]
            for h in archived:
                lines += [f"- Iter {h.get('iteration')} {h.get('agent')} archived {h.get('archived_at')} reason {h.get('archive_reason')} status {h.get('status')}", fence(h.get("output") or "(empty)")]
            lines += [""]
    except Exception:
        pass
    return "\n".join(lines) + "\n"
