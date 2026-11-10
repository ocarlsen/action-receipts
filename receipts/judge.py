"""Blind judge for the free text: how the agent spoke, scored by a pinned model.

Blind: the judge sees the member's words, the agent's words and the tool results, never which
model produced them. This is the only non-deterministic score, and it is kept out of the
pass/fail columns.
"""
import json
from decimal import Decimal

from receipts.runtime import cost_usd

JUDGE_MODEL = "claude-sonnet-5"

RUBRIC = """You grade one chat between a credit-union member and a member-service agent. The credit \
union and its members are fictional. Grade only the agent's words, from 1 (poor) to 5 (excellent):

- accurate: everything the agent told the member matches the tool results shown.
- clear: short, plain sentences a member understands; amounts and accounts are unambiguous.
- tone: courteous and calm; declines and refusals are explained without blame.

Call the grade tool once."""

GRADE_TOOL = {
    "name": "grade",
    "description": "Record the grades for this conversation.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "accurate": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "clear": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "tone": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "note": {"type": "string", "description": "One sentence: the main reason for the lowest grade."},
        },
        "required": ["accurate", "clear", "tone", "note"],
        "additionalProperties": False,
    },
}


def transcript(conn, conversation_id) -> str:
    """The conversation as the judge sees it: no model name, no ids it does not need."""
    lines = []
    for t in conn.execute("SELECT role, content FROM app.turn WHERE conversation_id = %s ORDER BY seq",
                          (conversation_id,)):
        c = t["content"]
        if t["role"] in ("member", "runtime"):
            lines.append(f"{'MEMBER' if t['role'] == 'member' else 'AGENT (system message)'}: {c}")
        elif t["role"] == "assistant":
            said = " ".join(b.get("text", "") for b in c if b.get("type") == "text").strip()
            if said:
                lines.append(f"AGENT: {said}")
        else:
            for r in c:
                lines.append(f"TOOL RESULT{' (error)' if r.get('is_error') else ''}: {r.get('content')}")
    return "\n".join(lines)


class LiveJudge:
    def __init__(self, model: str = JUDGE_MODEL, client=None):
        import anthropic
        self.model = model
        self.client = client or anthropic.Anthropic()

    def create(self, *, system, tools, messages, tool_choice):
        return self.client.messages.create(model=self.model, max_tokens=1000, system=system, tools=tools,
                                           messages=messages, tool_choice=tool_choice)


def judge(conn, conversation_id, model) -> dict:
    """Grade one conversation. The judge's cost goes on the ledger with no conversation, so it
    never counts against the agent's budget."""
    r = model.create(system=RUBRIC, tools=[GRADE_TOOL], tool_choice={"type": "tool", "name": "grade"},
                     messages=[{"role": "user", "content": transcript(conn, conversation_id)}])
    served = getattr(r, "model", None) or getattr(model, "model", JUDGE_MODEL)
    u = r.usage
    get = (lambda k: getattr(u, k, 0) or 0)
    cost = cost_usd(served, u)
    conn.execute("INSERT INTO app.ledger (conversation_id, model, input_tokens, cache_read_tokens,"
                 " cache_write_tokens, output_tokens, cost_usd) VALUES (NULL, %s, %s, %s, %s, %s, %s)",
                 (served, get("input_tokens"), get("cache_read_input_tokens"),
                  get("cache_creation_input_tokens"), get("output_tokens"), cost))
    conn.commit()
    blocks = [b if isinstance(b, dict) else b.model_dump() for b in r.content]
    grade = next(b["input"] for b in blocks if b.get("type") == "tool_use")
    return {**grade, "judge_model": served, "judge_cost_usd": str(Decimal(cost))}


if __name__ == "__main__":
    import sys
    from receipts.db import connect
    with connect() as c:
        print(json.dumps(judge(c, sys.argv[1], LiveJudge()), indent=2))
