from rich.panel import Panel
from rich.table import Table
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn
console = Console()

def banner():
    console.print(Panel("[bold cyan]AGENTFLOW[/bold cyan]\n[dim]MULTI-AGENT RESEARCH ENGINE[/dim]", style="cyan", expand=False))

def status_table(run, executions, consensus=None):
    banner()
    console.print(f"[bold]RUN:[/bold] {run['id']}  [bold]ITERATION:[/bold] {run['current_iteration']}  [bold]STATUS:[/bold] {run['status']}")
    console.print(f"[bold]OBJECTIVE:[/bold] {run['goal']}\n")
    t = Table(title="Workflow")
    t.add_column("#"); t.add_column("Agent"); t.add_column("Status")
    for i, e in enumerate(executions, 1):
        st = e["status"]
        color = "green" if st=="COMPLETED" else ("yellow" if st in ("PENDING","WAITING") else "red")
        t.add_column("")
        if i == 1:
            t.add_column("Agent")
            t.add_column("Status")
    # rebuild cleanly
    t2 = Table(title="Workflow")
    t2.add_column("#"); t2.add_column("Agent"); t2.add_column("Status")
    for i, e in enumerate(executions, 1):
        t2.add_row(str(i), e["agent"].capitalize(), f"[{color}]{e['status']}[/{color}]")
    console.print(t2)
    done = sum(1 for e in executions if e["status"]=="COMPLETED")
    console.print(f"PROGRESS: {done}/{len(executions)}")
    if consensus:
        console.print(f"[bold]FINCH:[/bold] {consensus.get('outcome', consensus)}")
    nxt = next((e for e in executions if e["status"]!="COMPLETED"), None)
    if nxt:
        console.print(f"\nNEXT ACTION: [bold]agentflow next[/bold] → {nxt['agent']}")
    else:
        console.print("\nAll agents completed. Use [bold]agentflow export[/bold] or [bold]agentflow loop[/bold].")
