"""AgentFlow CLI — manual copy-paste orchestration. No LLM calls, no network."""
import sys, os, json, click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from agentflow import db
from agentflow.agents import load_template, render_prompt, build_context, AGENT_IDS, validate_sequence
from agentflow.consensus import parse_finch_vote, evaluate_consensus, validate_finch_responses
from agentflow.utils.formatting import console, status_table
from agentflow.utils.clipboard import copy_text

PROJECT = "."

def _run_or_fail(rid=None):
    if rid:
        run = db.get_run(rid, PROJECT)
        if not run:
            console.print(f"[red]Run {rid} not found.[/red]"); sys.exit(1)
        return run
    runs = db.list_runs(PROJECT)
    if not runs:
        console.print("[red]No runs. Create one: agentflow new --goal \"...\"[/red]"); sys.exit(1)
    if len(runs) == 1:
        return runs[0]
    console.print("[yellow]Multiple runs — specify RUN_ID.[/yellow]")
    for r in runs:
        console.print(f"  {r['id']}  {r['status']}  {r['goal'][:60]}")
    sys.exit(1)

def _current_run_id(ctx):
    # store current run id in env file
    p = os.path.join(PROJECT, ".agentflow", "current")
    try:
        return open(p).read().strip()
    except Exception:
        return None

def _set_current(rid):
    os.makedirs(os.path.join(PROJECT, ".agentflow"), exist_ok=True)
    open(os.path.join(PROJECT, ".agentflow", "current"), "w").write(rid)

@click.group()
@click.option("--project", default=".", help="Project dir (default .)")
@click.pass_context
def main(ctx, project):
    global PROJECT
    PROJECT = project
    ctx.ensure_object(dict)

@main.command()
@click.option("--goal", required=True, help="Research/engineering objective")
def new(goal):
    """Create a new run and generate Supervisor prompt."""
    rid = db.create_run(goal, PROJECT)
    _set_current(rid)
    console.print(f"[green]Created run {rid}[/green]")
    # generate supervisor prompt immediately
    run = db.get_run(rid, PROJECT)
    tmpl = load_template("supervisor")
    ctx_map = build_context(run, [], iteration=1)
    rendered, unresolved = render_prompt(tmpl, ctx_map)
    db.save_prompt(rid, "supervisor", rendered, PROJECT)
    console.print(Panel(rendered, title="Supervisor prompt — copy into your AI agent", style="cyan"))
    if unresolved:
        console.print(f"[yellow]Unresolved placeholders: {', '.join(unresolved)}[/yellow]")
    console.print(f"\nNext: agentflow paste --run {rid}")

@main.command()
@click.option("--run", "run_id", default=None, help="Run ID (default: current)")
@click.option("--editor", is_flag=True, help="Use $EDITOR for input")
@click.option("--yes", "yes", is_flag=True, help="Skip confirmation (for pipes/scripts)")
def paste(run_id, editor, yes):
    """Paste an agent response (interactive, stdin, or $EDITOR)."""
    run = _run_or_fail(run_id or _current_run_id(None))
    pending = db.next_pending(run["id"], PROJECT)
    if not pending:
        console.print("[yellow]No pending agent. Use 'agentflow loop' for next iteration.[/yellow]"); return
    agent = pending["agent"]
    console.print(f"[bold]Pasting for {agent} (Run {run['id']} Iter {run['current_iteration']})[/bold]")
    if editor:
        import tempfile, subprocess
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".md", delete=False) as tf:
            tf.write("# Paste agent output here, save and exit\n")
            fname = tf.name
        subprocess.run([os.environ.get("EDITOR", "notepad" if os.name=="nt" else "vi"), fname])
        text = open(fname).read().strip()
        os.unlink(fname)
    elif sys.stdin.isatty():
        console.print("Type response. End with empty line + Ctrl+D (Linux) or Ctrl+Z+Enter (Windows).")
        lines = []
        try:
            while True:
                line = input()
                lines.append(line)
        except EOFError:
            pass
        text = "\n".join(lines).strip()
    else:
        text = sys.stdin.read().strip()
    if not text:
        console.print("[red]Empty input — not saved.[/red]"); return
    if not yes and not click.confirm("Save this output?"):
        console.print("[yellow]Cancelled — nothing saved.[/yellow]"); return
    db.save_output(run["id"], agent, text, PROJECT)
    console.print(f"[green]Saved {agent} output.[/green] Run [bold]agentflow next[/bold] for the following agent.")

@main.command()
@click.option("--run", "run_id", default=None)
@click.option("--raw-data", default="", help="Raw data for Finch")
def next(run_id, raw_data):
    """Generate the next agent prompt (fills placeholders)."""
    run = _run_or_fail(run_id or _current_run_id(None))
    pending = db.next_pending(run["id"], PROJECT)
    if not pending:
        console.print("[bold]All agents complete.[/bold] Use export / loop."); return
    agent = pending["agent"]
    # order validation: ensure prior agents completed
    exes = db.get_executions(run["id"], iteration=run["current_iteration"], project=PROJECT)
    done = [e["agent"] for e in exes if e["status"]=="COMPLETED"]
    validate_sequence(done)  # raises if skipped
    idx = AGENT_IDS.index(agent)
    if done != AGENT_IDS[:idx]:
        console.print(f"[red]Cannot run {agent} — missing prior outputs: expected {AGENT_IDS[:idx]}, got {done}[/red]"); sys.exit(1)
    # top hypothesis from ranking
    top = ""
    r_ex = db.get_execution(run["id"], "ranking", iteration=run["current_iteration"], project=PROJECT)
    if r_ex and r_ex.get("top_hypothesis"): top = r_ex["top_hypothesis"]
    elif r_ex and r_ex.get("output"):
        # extract 'Top hypothesis: ...' if present
        import re
        m = re.search(r"Top hypothesis:\s*(.+)", r_ex["output"], re.I)
        top = m.group(1).strip() if m else r_ex["output"][:500]
    cons = db.get_consensus(run["id"], project=PROJECT)
    ctx_map = build_context(run, exes, consensus=cons, top_hypothesis=top, raw_data=raw_data, iteration=run["current_iteration"])
    tmpl = load_template(agent)
    rendered, unresolved = render_prompt(tmpl, ctx_map)
    db.save_prompt(run["id"], agent, rendered, PROJECT)
    console.print(Panel(rendered, title=f"{agent} — copy into your AI agent"))
    if unresolved:
        console.print(f"[yellow]Unresolved: {', '.join(unresolved)}[/yellow]")
    if copy_text(rendered):
        console.print("[green]Copied to clipboard.[/green]")

@main.command()
@click.option("--run", "run_id", default=None)
def status(run_id):
    """Show workflow status."""
    run = _run_or_fail(run_id or _current_run_id(None))
    exes = db.get_executions(run["id"], iteration=run["current_iteration"], project=PROJECT)
    cons = db.get_consensus(run["id"], project=PROJECT)
    status_table(run, exes, cons)

@main.command()
@click.argument("run_id")
def resume(run_id):
    """Resume a run (restore workflow)."""
    run = _run_or_fail(run_id)
    _set_current(run_id)
    exes = db.get_executions(run_id, iteration=run["current_iteration"], project=PROJECT)
    cons = db.get_consensus(run_id, project=PROJECT)
    status_table(run, exes, cons)
    console.print(f"\nResumed {run_id} — next pending: {[e['agent'] for e in exes if e['status']!='COMPLETED'][:1]}")

@main.command()
@click.option("--run", "run_id", default=None)
@click.option("--format", "fmt", default="md", show_default=True)
def export(run_id, fmt):
    """Export run to Markdown."""
    run = _run_or_fail(run_id or _current_run_id(None))
    all_exes = db.get_executions(run["id"], project=PROJECT)
    finch = db.get_finch_runs(run["id"], project=PROJECT)
    cons = db.get_consensus(run["id"], project=PROJECT)
    lines = [f"# AgentFlow Run {run['id']}", f"Goal: {run['goal']}", f"Status: {run['status']} Iteration: {run['current_iteration']}", ""]
    for e in all_exes:
        lines += [f"## Iteration {e['iteration']} — {e['agent']}", f"Status: {e['status']}", f"### Prompt", "```", e["prompt"] or "(none)", "```", f"### Output", "```", e["output"] or "(none)", "```", ""]
    lines += ["## Finch (individual)", ""]
    for f in finch:
        lines += [f"### Run {f['run_index']}", f"Vote: {f['parsed_vote']}", "```", f["raw_response"], "```"]
    if cons:
        lines += ["## Consensus", f"Outcome: {cons['outcome']}", f"YES:{cons['yes_count']} NO:{cons['no_count']} INCONCLUSIVE:{cons['inconclusive_count']} INVALID:{cons['invalid_count']}", cons["detail"] or ""]
    out = "\n".join(lines)
    if fmt == "md":
        path = os.path.join(PROJECT, ".agentflow", f"export-{run['id']}.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write(out)
        console.print(f"[green]Exported {path}[/green]")
    else:
        console.print(out)

@main.command()
@click.option("--run", "run_id", default=None)
@click.option("--text", default=None, help="Winning hypothesis text (else paste interactively)")
def set_top(run_id, text):
    """Set the winning hypothesis from Ranking."""
    run = _run_or_fail(run_id or _current_run_id(None))
    if text:
        hyp = text
    else:
        console.print("Paste winning hypothesis (Ctrl+D to finish):")
        try:
            lines = []
            while True: lines.append(input())
        except EOFError: pass
        hyp = "\n".join(lines).strip()
    if not hyp: console.print("[red]Empty — not saved.[/red]"); return
    db.save_top_hypothesis(run["id"], hyp, PROJECT)
    console.print("[green]Top hypothesis saved.[/green]")

@main.command()
@click.argument("step")
@click.option("--run", "run_id", default=None)
def copy(step, run_id):
    """Copy a previously generated prompt to clipboard."""
    run = _run_or_fail(run_id or _current_run_id(None))
    e = db.get_execution(run["id"], step, iteration=run["current_iteration"], project=PROJECT)
    if not e or not e.get("prompt"):
        console.print(f"[red]No prompt saved for {step}. Run 'agentflow next' first.[/red]"); return
    if copy_text(e["prompt"]):
        console.print("[green]Copied to clipboard.[/green]")
    else:
        console.print(Panel(e["prompt"], title=f"{step} prompt"))

@main.command()
@click.argument("run_a")
@click.argument("run_b")
def diff(run_a, run_b):
    """Compare two runs."""
    ra, rb = _run_or_fail(run_a), _run_or_fail(run_b)
    console.print(f"=== {run_a} vs {run_b} ===")
    console.print(f"Goal A: {ra['goal']}\nGoal B: {rb['goal']}")
    for label, r in (("A", ra), ("B", rb)):
        exes = db.get_executions(r["id"], project=PROJECT)
        cons = db.get_consensus(r["id"], project=PROJECT)
        console.print(f"[bold]{label} {r['id']}[/bold] iters:{r['current_iteration']} status:{r['status']} done:{sum(1 for e in exes if e['status']=='COMPLETED')}/{len(exes)} consensus:{cons['outcome'] if cons else '(none)'}")
        for e in exes:
            print(f"  {e['agent']}: {(e['output'] or '')[:80].replace(chr(10),' ')}")

@main.command()
@click.option("--run", "run_id", default=None)
def loop(run_id):
    """Start a new iteration after Finch consensus."""
    run = _run_or_fail(run_id or _current_run_id(None))
    cons = db.get_consensus(run["id"], project=PROJECT)
    if not cons:
        console.print("[red]Cannot loop — Finch consensus not reached.[/red]"); sys.exit(1)
    new_it = db.new_iteration(run["id"], PROJECT)
    console.print(f"[green]Started iteration {new_it} (previous archived).[/green] Run [bold]agentflow next[/bold].")

@main.command()
@click.option("--run", "run_id", default=None)
@click.option("--multiblock", is_flag=True, help="Paste 8 responses separated by ---")
def finch(run_id, multiblock):
    """Collect eight Finch responses (interactive or multiblock)."""
    run = _run_or_fail(run_id or _current_run_id(None))
    responses = []
    if multiblock:
        console.print("Paste 8 responses separated by lines containing only ---. End with Ctrl+D:")
        try:
            raw = sys.stdin.read()
        except EOFError:
            raw = ""
        parts = [p.strip() for p in raw.split("\n---")]
        # handle --- lines
        if len(parts) == 1 and "---" in raw:
            parts = [p.strip() for p in raw.split("---") if p.strip()]
        responses = parts
    else:
        for i in range(8):
            console.print(f"[bold]Finch Run {i+1}/8[/bold] — paste response, Enter to finish:")
            try:
                lines = []
                while True:
                    line = input()
                    if line == "": break
                    lines.append(line)
            except EOFError:
                pass
            responses.append("\n".join(lines).strip())
    try:
        validate_finch_responses(responses)
    except ValueError as e:
        console.print(f"[red]{e}[/red]"); sys.exit(1)
    votes = [parse_finch_vote(r) for r in responses]
    if "INVALID" in votes:
        console.print(f"[red]Invalid Finch responses at indices {[i+1 for i,v in enumerate(votes) if v=='INVALID']} — each must contain 'Key finding: YES|NO|INCONCLUSIVE'. Nothing saved.[/red]"); sys.exit(1)
    db.save_finch_runs(run["id"], responses, PROJECT)
    result = evaluate_consensus(votes)
    result["yes"] = result["yes"]; result["no"] = result["no"]; result["inconclusive"] = result["inconclusive"]; result["invalid"] = result["invalid"]
    db.save_consensus(run["id"], {"yes": result["yes"], "no": result["no"], "inconclusive": result["inconclusive"], "invalid": result["invalid"], "outcome": result["outcome"], "detail": json.dumps(result)}, PROJECT)
    console.print(Panel(f"YES {result['yes']} NO {result['no']} INCONCLUSIVE {result['inconclusive']} INVALID {result['invalid']}\n{result['outcome']}\nPercentages: {result['percentages']}", title="Finch consensus"))
    if result["outcome"].startswith("CONSENSUS"):
        db.set_run_status(run["id"], "CONSENSUS_REACHED", PROJECT)
    else:
        db.set_run_status(run["id"], "SPLIT", PROJECT)

if __name__ == "__main__":
    main()
