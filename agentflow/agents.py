"""Agent definitions + workflow state machine + prompt engine."""
import pathlib, re

AGENTS = [
    {"id": "supervisor", "name": "Supervisor", "order": 1,
     "description": "Understands the objective and decomposes it into concrete sub-tasks, constraints, success criteria and unknowns. Decomposition only; does not solve the task.",
     "dependencies": ["GOAL"], "inputs": ["GOAL"]},
    {"id": "generation", "name": "Generation", "order": 2,
     "description": "Generates 8-10 distinct approaches/hypotheses from the Supervisor decomposition. Each states its core mechanism and risk; no ranking yet.",
     "dependencies": ["SUPERVISOR_OUTPUT", "GOAL"], "inputs": ["SUPERVISOR_OUTPUT"]},
    {"id": "proximity", "name": "Proximity", "order": 3,
     "description": "Identifies similarities among generated hypotheses, clusters them, and removes duplicates, keeping only distinct survivors.",
     "dependencies": ["GENERATION_OUTPUT"], "inputs": ["GENERATION_OUTPUT"]},
    {"id": "reflection", "name": "Reflection", "order": 4,
     "description": "Critically evaluates survivors and scores survivability 1-10 on feasibility, impact and risk. Keeps the top 4-6.",
     "dependencies": ["PROXIMITY_OUTPUT"], "inputs": ["PROXIMITY_OUTPUT"]},
    {"id": "evolution", "name": "Evolution", "order": 5,
     "description": "Combines promising hypotheses into 3-5 stronger evolved candidates (E1...), stating gains and risks of each combination.",
     "dependencies": ["REFLECTION_OUTPUT"], "inputs": ["REFLECTION_OUTPUT"]},
    {"id": "ranking", "name": "Ranking", "order": 6,
     "description": "Comparative ELO-style tournament over evolved candidates; crowns a single winner saved as TOP_HYPOTHESIS for Crow.",
     "dependencies": ["EVOLUTION_OUTPUT"], "inputs": ["EVOLUTION_OUTPUT"]},
    {"id": "crow", "name": "Crow", "order": 7,
     "description": "Researches the selected top hypothesis and produces an implementation plan (research, plan steps, risks).",
     "dependencies": ["TOP_HYPOTHESIS", "RANKING_OUTPUT"], "inputs": ["TOP_HYPOTHESIS", "RANKING_OUTPUT"]},
    {"id": "falcon", "name": "Falcon", "order": 8,
     "description": "Detailed investigation of a selected component from Crow's plan: data flow, edge cases, tests, findings and recommendations.",
     "dependencies": ["CROW_OUTPUT", "GOAL"], "inputs": ["CROW_OUTPUT", "GOAL"]},
    {"id": "finch", "name": "Finch", "order": 9,
     "description": "Benchmark analysis: one of eight independent evaluations of benchmark data. Each run must state Key finding: YES|NO|INCONCLUSIVE.",
     "dependencies": ["RAW_DATA"], "inputs": ["RAW_DATA"]},
]
AGENT_IDS = [a["id"] for a in AGENTS]
AGENT_BY_ID = {a["id"]: a for a in AGENTS}
PLACEHOLDERS = ["GOAL","SUPERVISOR_OUTPUT","GENERATION_OUTPUT","PROXIMITY_OUTPUT","REFLECTION_OUTPUT",
                "EVOLUTION_OUTPUT","RANKING_OUTPUT","CROW_OUTPUT","FALCON_OUTPUT","RAW_DATA","TOP_HYPOTHESIS",
                "PREVIOUS_CONSENSUS","ITERATION"]
PROMPT_DIR = pathlib.Path(__file__).parent / "prompts"

def get_agent(agent_id: str) -> dict:
    return AGENT_BY_ID.get(agent_id)

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
    expect = AGENT_IDS[:len(seen)]
    if seen != expect:
        raise ValueError(f"Invalid sequence {seen} — expected prefix {expect}")
    return True

def render_prompt(template: str, context: dict):
    """Replace {{KEY}} from context; warn on unresolved. Returns (rendered, unresolved[])."""
    unresolved = []
    def repl(m):
        key = m.group(1).strip()
        if key in context and context[key] is not None and str(context[key]) != "":
            return str(context[key])
        # Empty-string context values for optional keys are rendered empty
        # only when explicitly present for optional placeholders.
        if key in context and context[key] is not None and str(context[key]) == "" and key in ("PREVIOUS_CONSENSUS", "ITERATION"):
            return ""
        if key in context and context[key] is not None and str(context[key]) == "":
            # Empty required value -> treat as unresolved so missing info warns
            unresolved.append(key)
            return m.group(0)
        unresolved.append(key)
        return m.group(0)  # preserve
    rendered = re.sub(r"\{\{(\w+)\}\}", repl, template)
    return rendered, sorted(set(unresolved))

def build_context(run, executions, consensus=None, top_hypothesis="", raw_data="", iteration=1):
    """Build placeholder dict from run + completed executions.

    Only real, non-empty preceding outputs are injected — no fabricated
    placeholders. Missing required inputs are omitted so render_prompt
    reports them as unresolved (clear warning instead of fake data).
    GOAL / ITERATION / PREVIOUS_CONSENSUS are always available.
    """
    goal = run["goal"] if isinstance(run, dict) else run.goal
    ctx = {"GOAL": goal, "ITERATION": str(iteration)}
    # PREVIOUS_CONSENSUS: empty string on first iteration is legitimate
    ctx["PREVIOUS_CONSENSUS"] = ""
    keymap = {"supervisor":"SUPERVISOR_OUTPUT","generation":"GENERATION_OUTPUT","proximity":"PROXIMITY_OUTPUT",
              "reflection":"REFLECTION_OUTPUT","evolution":"EVOLUTION_OUTPUT","ranking":"RANKING_OUTPUT",
              "crow":"CROW_OUTPUT","falcon":"FALCON_OUTPUT"}
    # Only copy real outputs; missing preceding outputs stay absent
    for e in executions:
        if e.get("status") == "COMPLETED" and e.get("output"):
            ctx[keymap.get(e["agent"], "")] = e["output"]
            if e.get("top_hypothesis"):
                ctx["TOP_HYPOTHESIS"] = e["top_hypothesis"]
    if top_hypothesis:
        ctx["TOP_HYPOTHESIS"] = top_hypothesis
    if raw_data:
        ctx["RAW_DATA"] = raw_data
    # Note: RAW_DATA / TOP_HYPOTHESIS intentionally omitted when absent
    if consensus:
        import json
        ctx["PREVIOUS_CONSENSUS"] = json.dumps(consensus, indent=2) if isinstance(consensus, (dict, list)) else str(consensus)
    return ctx

def dependencies_for(agent_id: str):
    a = AGENT_BY_ID.get(agent_id)
    return list(a.get("dependencies", [])) if a else []

def missing_dependencies(agent_id: str, context: dict):
    """Return required placeholders absent/empty in context."""
    missing = []
    for dep in dependencies_for(agent_id):
        val = context.get(dep)
        if val is None or str(val).strip() == "":
            missing.append(dep)
    return missing
