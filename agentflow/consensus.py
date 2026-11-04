"""Finch 8-run consensus — deterministic majority voting."""
import re

VOTE_PATTERN = re.compile(r"key\s*finding\s*:\s*(YES|NO|INCONCLUSIVE)", re.IGNORECASE)

def parse_finch_vote(text: str):
    """Return YES/NO/INCONCLUSIVE/INVALID. Case-insensitive, no inference from unrelated text."""
    if not text or not text.strip():
        return "INVALID"
    # Only accept explicit 'Key finding: ...' lines
    m = VOTE_PATTERN.search(text.strip())
    if not m:
        return "INVALID"
    return m.group(1).upper()

def evaluate_consensus(votes: list):
    """votes: list of 8 strings (raw responses or parsed votes). Returns result dict."""
    # Support raw responses: parse each
    parsed = [parse_finch_vote(v) if v not in ("YES","NO","INCONCLUSIVE","INVALID") else v for v in votes] if votes and isinstance(votes[0], str) and len(votes[0]) > 12 else list(votes)
    # If someone passed raw text with 'Key finding', parse; else if short token, keep. Re-parse long ones safely:
    parsed = []
    for v in votes:
        if not isinstance(v, str):
            parsed.append("INVALID"); continue
        t = v.strip()
        if t.upper() in ("YES","NO","INCONCLUSIVE","INVALID"):
            parsed.append(t.upper())
        else:
            parsed.append(parse_finch_vote(v))
    assert len(parsed) == 8, f"Finch requires exactly 8 responses, got {len(parsed)}"
    yes = parsed.count("YES"); no = parsed.count("NO"); inc = parsed.count("INCONCLUSIVE"); inv = parsed.count("INVALID")
    counts = {"YES": yes, "NO": no, "INCONCLUSIVE": inc}
    # winner needs >=4
    winner, cnt = max(counts.items(), key=lambda x: x[1])
    if cnt >= 4:
        outcome = f"CONSENSUS: {winner}"
    else:
        outcome = "SPLIT — NEEDS MORE DATA"
    # percentages
    pct = {k: round(v/8*100) for k, v in counts.items()}
    return {"yes": yes, "no": no, "inconclusive": inc, "invalid": inv, "votes": parsed,
            "outcome": outcome, "percentages": pct}

def validate_finch_responses(responses: list):
    """Validate exactly 8 non-empty responses. Raises ValueError."""
    if len(responses) != 8:
        raise ValueError(f"Exactly 8 Finch responses required, got {len(responses)}")
    for i, r in enumerate(responses, 1):
        if not r or not str(r).strip():
            raise ValueError(f"Finch Run {i}/8 is empty")
    return True
