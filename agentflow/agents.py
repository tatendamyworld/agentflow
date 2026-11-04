"""Agent definitions + workflow state machine + prompt engine."""
import pathlib, re

AGENTS = [
    {"id": "supervisor", "name": "Supervisor", "order": 1},
    {"id": "generation", "name": "Generation", "order": 2},
    {"id": "proximity", "name": "Proximity", "order": 3},
    {"id": "reflection", "name": "Reflection", "order": 4},
    {"id": "evolution", "name": "Evolution", "order": 5},
    {"id": "ranking", "name": "Ranking", "order": 6},
    {"id": "crow", "name": "Crow", "order": 7},
    {"id": "falcon", "name": "Falcon", "order": 8},
    {"id": "finch", "name": "Finch", "order": 9},
]
AGENT_IDS = [a["id"] for a in AGENTS]
PLACEHOLDERS = ["GOAL","SUPERVISOR_OUTPUT","GENERATION_OUTPUT","PROXIMITY_OUTPUT","REFLECTION_OUTPUT",
                "EVOLUTION_OUTPUT","RANKING_OUTPUT","CROW_OUTPUT","FALCON_OUTPUT","RAW_DATA","TOP_HYPOTHESIS",
                "PREVIOUS_CONSENSUS","ITERATION"]
PROMPT_DIR = pathlib.Path(__file__).parent / "prompts"

def load_template(agent_id: str) -> str:
    p = PROMPT_DIR / f"{agent_id}.txt"
    if not p.exists():
        raise FileNotFoundError(f"Prompt template missing: {p}")
    return p.read_text(encoding="utf-8")

def validate_sequence(completed: list):
    """Raise ValueError if completed skips agents or out of order."""
    seen = []
    for c in completed:
        if c not in AGENT_IDS:
            raise ValueError(f"Unknown agent {c}")
        seen.append(c)
    # No skipping: completed must be prefix of AGENT_IDS
    expect = AGENT_IDS[:len(seen)]
    if seen != expect:
        raise ValueError(f"Invalid sequence {seen} — expected prefix {expect}")
    return True

def render_prompt(template: str, context: dict):
    """Replace {{KEY}} from context; warn on unresolved. Returns (rendered, unresolved[])."""
    unresolved = []
    def repl(m):
        key = m.group(1).strip()
        if key in context and context[key] is not None:
            return str(context[key])
        unresolved.append(key)
        return m.group(0)  # preserve
    rendered = re.sub(r"\{\{(\w+)\}\}", repl, template)
    return rendered, sorted(set(unresolved))

def build_context(run, executions, consensus=None, top_hypothesis="", raw_data="", iteration=1):
    """Build placeholder dict from run + completed executions."""
    ctx = {"GOAL": run["goal"] if isinstance(run, dict) else run.goal, "ITERATION": str(iteration),
           "RAW_DATA": raw_data or "", "TOP_HYPOTHESIS": top_hypothesis or "", "PREVIOUS_CONSENSUS": str(consensus) if consensus else ""}
    keymap = {"supervisor":"SUPERVISOR_OUTPUT","generation":"GENERATION_OUTPUT","proximity":"PROXIMITY_OUTPUT",
              "reflection":"REFLECTION_OUTPUT","evolution":"EVOLUTION_OUTPUT","ranking":"RANKING_OUTPUT",
              "crow":"CROW_OUTPUT","falcon":"FALCON_OUTPUT"}
    for e in executions:
        if e.get("status") == "COMPLETED" and e.get("output"):
            ctx[keymap.get(e["agent"], "")] = e["output"]
            if e.get("top_hypothesis"):
                ctx["TOP_HYPOTHESIS"] = e["top_hypothesis"]
    if consensus:
        import json
        ctx["PREVIOUS_CONSENSUS"] = json.dumps(consensus, indent=2) if isinstance(consensus, (dict, list)) else str(consensus)
    return ctx
