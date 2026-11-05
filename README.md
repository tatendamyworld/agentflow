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
