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
