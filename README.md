# AgentFlow — Persistent Multi-Agent CLI Orchestration

Manual copy-paste orchestrator for nine agents (Supervisor → Generation → Proximity → Reflection → Evolution → Ranking → Crow → Falcon → Finch). No LLM API calls, no browser automation, no network during normal operation.

## Architecture
- `agentflow/cli.py` — Click commands (Rich UI)
- `agentflow/db.py` — SQLite at `.agentflow/state.db` (runs, executions, finch_runs, consensus)
- `agentflow/agents.py` — agent list, sequence validation, placeholder engine
- `agentflow/consensus.py` — Finch 8-run majority voting (>=4 wins)
- `agentflow/prompts/*.txt` — 9 editable templates with {{PLACEHOLDER}} syntax
- `agentflow/utils/` — Rich formatting, clipboard (pyperclip optional)

## Installation
```bash
pip install -e .          # Windows: py -m pip install -e .
agentflow --help
```

### Windows setup
Python 3.11+ from python.org, `py -m pip install -e .`, launch `agentflow`. Ctrl+Z+Enter for stdin paste.
### Linux setup
`pip install -e .`, launch `agentflow`. Ctrl+D for stdin paste. Set `$EDITOR` for editor paste.

## Example workflow
```bash
agentflow new --goal "Build X"
# copy Supervisor prompt → external AI → paste back
agentflow paste
agentflow next              # generates Generation prompt
agentflow paste
# ... through Ranking, then:
agentflow set-top           # save winning hypothesis (or --text "...")
agentflow next              # Crow prompt uses {{TOP_HYPOTHESIS}}
agentflow paste
agentflow next              # Falcon
agentflow paste
agentflow next              # Finch instructions
agentflow finch             # 8 interactive runs (or --multiblock)
agentflow status
agentflow export --format md
agentflow loop              # next iteration (archives previous)
```

## All CLI commands
`new --goal`, `paste [--editor]`, `next`, `status`, `resume RUN_ID`, `export --format md`, `set-top [--text]`, `copy STEP`, `diff A B`, `loop`, `finch [--multiblock]`

## Editing prompts
Edit `agentflow/prompts/<agent>.txt`. Placeholders: {{GOAL}} {{SUPERVISOR_OUTPUT}} {{GENERATION_OUTPUT}} {{PROXIMITY_OUTPUT}} {{REFLECTION_OUTPUT}} {{EVOLUTION_OUTPUT}} {{RANKING_OUTPUT}} {{CROW_OUTPUT}} {{FALCON_OUTPUT}} {{RAW_DATA}} {{TOP_HYPOTHESIS}} {{PREVIOUS_CONSENSUS}} {{ITERATION}}

## Finch consensus
Requires exactly 8 responses, each with `Key finding: YES|NO|INCONCLUSIVE` (case-insensitive). Winner needs >=4 votes else `SPLIT — NEEDS MORE DATA`. Counts/percentage/outcome saved.

## Iterations
`agentflow loop` after consensus creates iteration N+1 with fresh PENDING slots; previous iteration rows stay untouched. Supervisor gets {{PREVIOUS_CONSENSUS}}.

## Database
`.agentflow/state.db` — tables runs/executions/finch_runs/consensus. Never overwrites without confirmation; every step recoverable via `resume`.

## Troubleshooting
- Unresolved placeholders on `next` → missing prior output for that agent.
- Finch INVALID → add explicit `Key finding:` line.
- No clipboard → prompt is printed instead.

## Security limitations
Operates only inside project dir. No system-dir scanning, no registry, no PowerShell, no background processes, no browser automation, no network, no AI API calls, never executes pasted output as code.

## AGENTFLOW VISUAL (dashboard)
```bash
pip install -e .
python run.py          # 127.0.0.1:5001 — dark dashboard, SVG 9-node network
python -m agentflow.web
```
Shares `.agentflow/state.db` with CLI. No internet needed, bind is 127.0.0.1 only.

## PHASE 5 — Agent Operations Engine (visual inspector)

Each of the nine agents now runs the full operations workflow via the dashboard + HTTP API
(sharing the CLI SQLite DB; no LLM calls, no network, bind 127.0.0.1:5001):

- **GENERATE PROMPT** — `POST /api/runs/:id/generate` renders the template with automatic
  placeholder substitution from *real* preceding outputs only. Missing inputs are preserved
  as `{{PLACEHOLDER}}` and returned in `unresolved`/`missing`/`warnings` — never fabricated.
- **Prompt preview** — `POST /api/runs/:id/prompt-preview` renders without completing the step.
- **COPY PROMPT** — `GET /api/runs/:id/copy/:agent`; frontend copies to clipboard.
- **SUBMIT RESPONSE** — `POST /api/runs/:id/submit` (strict validation + persist).
- **SAVE OUTPUT** — `POST /api/runs/:id/save` (validate + persist; blocks placeholder-paste / invalid Finch).
  Both return `validation`, `created_at`/`updated_at` timestamps, and `next_agent`.
- **VIEW HISTORY** — `GET /api/runs/:id/history` and `GET /api/runs/:id/agent/:agent/history`
  (previous execution history across iterations).
- **RETRY STEP** — `POST /api/runs/:id/retry` resets the step (and downstream) to PENDING and
  regenerates a fresh prompt.

Dependency chain enforced (real prior outputs only):

```
Supervisor -> Goal
Generation -> Supervisor output (+ Goal)
Proximity -> Generation output
Reflection -> Proximity output
Evolution -> Reflection output
Ranking -> Evolution output
Crow -> Selected top hypothesis + Ranking output
Falcon -> Crow output + Original goal
Finch -> Benchmark data (RAW_DATA, supplied at generate/submit)
```

Inspector (right rail) shows: Agent identity, Description, Workflow status
(status/iteration/created/updated timestamps), Input dependencies (satisfied/missing),
Generated prompt preview (with missing-info warning banner), Response editor,
Saved output, Execution history.

Response validation:
- All agents: non-empty, length-capped, no unresolved `{{PLACEHOLDERS}}` pasted as output.
- Finch: must contain `Key finding: YES|NO|INCONCLUSIVE`.
- Ranking strict-submit requires `Top hypothesis:` so Crow cannot receive a fabricated winner.

Tests: `tests/test_operations.py` (18 tests) covers dependencies, substitution, warnings,
preview, copy, submit/save validation, timestamps, history, retry. Full suite: `pytest tests/` (46 passed).

## PHASE 6 — Workflow State Management

Reliable state machine in `agentflow/workflow.py`. Canonical run states:
`CREATED, READY, IN_PROGRESS, WAITING_FOR_INPUT, COMPLETED, FAILED, BLOCKED, ARCHIVED`
(legacy `CONSENSUS_REACHED`→`COMPLETED`, `SPLIT`→`BLOCKED`, `WAITING_FOR_FINCH/TOP_HYPOTHESIS`→`WAITING_FOR_INPUT`).

Rules enforced:
- Dependencies first: out-of-order generate/save persist `BLOCKED` (409, recoverable), no fabrication.
- `POST /generate` → `WAITING_FOR_INPUT`, never `COMPLETED`; `POST /save|/submit` with valid response → `COMPLETED`.
- Invalid (422) → `FAILED`, output never persisted, step stays executable.
- Overwrite guard: saving over `COMPLETED` without `overwrite:true` → 409; explicit overwrite archives prior to `execution_history`.
- Retry archives target + downstream to `execution_history` before reset (previous execution preserved); Finch re-runs archive to `finch_history`.
- Dashboard reflects DB truth: `GET /api/runs/:id/status` derives canonical status + `compute_progress()` (completed/total/percentage/next/blocked/failed) on every read; frontend normalizes `BLOCKED/ARCHIVED` and shows derived status.
- Progress: `GET /api/runs/:id/status`, `GET /api/runs/:id`, `GET /api/runs` (per-mission progress).
- Multiple missions: independent `runs` rows; `GET /api/runs` lists each with own progress; completing one never touches another.
- Archive: `POST /api/runs/:id/archive` (`COMPLETED` → `ARCHIVED`, terminal).

Tests: `tests/test_workflow_state.py` (16 tests). Full suite (`pytest tests/`): 62 passed.

## PHASE 7 — Hypothesis Laboratory

Functional research workspace over `.agentflow/state.db` (SQLite only, no fabrication).

Model (`hypotheses` + migration for old DBs): `id, title, description, source_agent, evidence, weaknesses, survivability (0–10), elo_rating (0–4000, unrated=null), status (ACTIVE/SELECTED/ARCHIVED), iteration, ranking, score, notes`.

Operations (real API + dashboard cards):
- **Create manually** — `POST /api/runs/:id/hypotheses` (+ dashboard form; unknown fields stay blank).
- **Import from agent outputs** — `POST /hypotheses/import` scans completed Generation/Evolution/Ranking `H#/E#` lines; dedupes by label; parse failure returns `parse_ok:false` + manual-entry message, imports 0 (never fabricates scores/evidence).
- **Edit** — `PATCH /hypotheses/:id` (title/desc/evidence/weaknesses/surv/elo/status).
- **Compare** — `GET|POST /hypotheses/compare?a=&b=[&winner=]` side-by-side (strengths/evidence/weaknesses/surv/ELO/ranking/selection/iteration) + deltas; explicit winner applies ELO K=32 (unrated=1500 prior) and persists.
- **Select** — `POST /hypotheses/:id/select` (one SELECTED per run+iteration) writes `executions.top_hypothesis` so Crow's `{{TOP_HYPOTHESIS}}` embeds it; architected/Crow preview verified.
- **Archive/Restore** — `POST .../archive` → `ARCHIVED` (excluded from ranking, cannot select); `POST .../restore` → `ACTIVE`.

Validation: surv 0–10, ELO 0–4000, status enum, agent-or-`manual`; invalid → 400 (no storage).

Tests: `tests/test_hypothesis_lab.py` (8 tests). Full suite (`pytest tests/`): 70 passed.

## PHASE 8 — Ranking Dashboard

Ranking agent results from stored SQLite only (no demo values):

- **Leaderboard** — `GET /ranking` returns `leaderboard` sorted by stored `ranking` (+ELO/surv tiebreak); ranks are editable, archived excluded.
- **ELO visualisation** — `elo_visual` (id/label/elo/elo_pct/rank); bars scaled from actual stored `elo_rating` min-max; unrated shown as “Unrated” (pct=null), never fabricated.
- **Ranking changes** — `ranking_events` table records every rank/ELO/score/surv/status edit (`EDIT`), bulk reorder (`REORDER`), compare-win ELO updates; exposed as `ranking_changes` with prev→new + Δ in leaderboard.
- **Comparative results** — `ranking_matches` table parses Ranking “X beats Y: reason” tournament lines (persisted, idempotent); shown as tournament list.
- **Winning hypothesis** — `winning_hypothesis` (SELECTED row else stored top text) + `crow_available`; Crow preview verifies `{{TOP_HYPOTHESIS}}` replaced from SQLite.
- **Ranking history** — `GET /ranking/history` per-iteration snapshots (output/top/leaderboard) + events + matches.
- **Manual correction** — `PATCH /hypotheses/:id` {ranking|elo_rating|score|survivability} per row, `POST /ranking/reorder` {order:[ids]} for bulk order; both persisted + recorded.
- Parser fix: “beats” lines no longer consume H/E labels, so E1/E2 evolved candidates parse correctly.

Dashboard: winner hero, leaderboard with inline correction, ELO bars, tournament list, changes timeline, history snapshots — all polled from `/ranking`.

Tests: `tests/test_ranking_dashboard.py` (9 tests). Full suite (`pytest tests/`): 79 passed.

## PHASE 9 — Crow and Falcon Workspaces

Dedicated investigation interfaces (manual-paste only, no internet browsing):

- **Crow** fields: Selected hypothesis (from SQLite top), Literature research, Technical feasibility, Implementation strategy, Dependencies, Risks, Recommendations.
- **Falcon** fields: Component being investigated, Technical architecture, Detailed analysis, Implementation requirements, Testing strategy, Limitations.
- **Flow:** `POST /workspace/<crow|falcon>/generate` (prompt with real prior outputs) → external AI → `POST /save` (validated completion, auto-parses `##` sections into workspace notes) → paste also via PATCH `POST /workspace/<ws>` {sections} for manual edits; `POST /workspace/<ws>/parse` re-splits saved output. Raw output always preserved; parse failure keeps raw + manual guidance (no fabrication).
- **History:** each workspace returns execution history (iteration/status/timestamps/raw excerpt) + timestamps per section.
- **Templates** (`prompts/crow.txt`, `falcon.txt`) updated to request the exact section headings.

Tests: `tests/test_workspaces.py` (7 tests). Full suite: 86 passed.

## PHASE 10 — Finch Consensus Laboratory

Eight independent evaluations (manual paste, no auto-evaluation, `Key finding: YES|NO|INCONCLUSIVE` required):

- **Per-evaluation store:** `finch_runs` (run_number, raw_response, parsed_vote, validation_status VALID/INVALID, created_at/updated_at timestamps).
- **Eight individual submits:** `PUT /finch/run/<1..8>` {response} per card; **bulk multiblock** `POST /finch` {responses|[multiblock]} (atomic, strict 400 on invalid for CLI parity) — both write same tables.
- **Edit invalid:** `PUT /finch/run/<n>` with new text re-parses; INVALID rows stored first, never counted as votes.
- **Recalc:** `POST /finch/recalc` finalizes only at 8/8 VALID (outcome via `>=4` rule; else `SPLIT` pending banner), returns `INCOMPLETE x/8 valid` without saving consensus otherwise. Individual saves auto-recalc when 8/8 valid.
- **Visual:** eight editable cards (textarea + vote/validation badges + timestamp + per-card save), vote chart (YES/NO/INCONCLUSIVE totals + % of 8), consensus banner (outcome + percentages), totals line, `GET /finch/history` evaluation history (snapshots + consensus rows). All derived from stored runs — no decoration without data.

Tests: `tests/test_finch_lab.py` (8 tests). Full suite (`pytest tests/`): 94 passed.

## PHASE 11 — Iteration Engine

Multiple research iterations with full preservation:

- **Start next:** `POST /loop` only after Finch consensus; creates fresh PENDING executions for `iteration N+1` (old rows untouched, statuses not copied), inherits original goal + previous consensus + winning hypothesis + unresolved findings, and auto-generates a fresh Supervisor prompt (PREVIOUS_CONSENSUS includes prior outcome + top).
- **Independence:** each iteration owns 9 execution rows + hypotheses/finch/workspace notes keyed by iteration; history across iterations preserved (`GET /history`, `/agent/:agent/history`).
- **Selector:** top-nav `<select id=iteration-select>` + `POST /iterations/switch` {iteration} (updates `runs.current_iteration`, all panels re-render for that iteration).
- **Compare:** `GET /iterations/compare?a=&b=` — hypothesis added/removed, ranking changes (rank/ELO/status), consensus before/after, new findings (added titles), unresolved questions (Unknowns sections + “?” lines).
- **Snapshots:** `GET /iterations` lists per-iteration completed count, consensus, top/count; `POST /loop` returns inheritance payload + supervisor prompt.

Tests: `tests/test_iterations.py` (5 tests). Full suite (`pytest tests/`): 99 passed.

## PHASE 12 — Research History and Export

Complete mission history with full-fidelity Markdown (no truncation/fabrication):

- **All saved missions:** `GET /runs` list with derived progress; history panel shows Unfinished/Completed + Resume/Inspect (opens via existing switch; iterations via top-nav selector).
- **Research dossier:** `GET /runs/:id/research-history` (run, progress, per-iteration executions/hypotheses/finch/winner).
- **Compare missions:** `GET /diff/:other` (progress, iterations, hypothesis counts) + iterations compare still available.
- **Export:** `GET /export.md` + `agentflow export` now share `agentflow/export.py::build_markdown` — mission details, per-iteration agent prompts + raw responses (backtick-safe fences), hypotheses (all fields), rankings (leaderboard/matches/events), Finch raw evaluations + history, consensus per iteration, iteration history/unresolved, workspace sections, archived executions. Raw responses never omitted.

Tests: `tests/test_history_export.py` (5 tests). Full suite (`pytest tests/`): 104 passed.
