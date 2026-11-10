"""Said versus did: find each completed action the agent claimed, and check the core shows it.

A claim is verified only if, before the agent said it, an action of that kind executed in this
conversation AND the core recorded the write under that action's idempotency key. A confident
"I've frozen your card" with nothing in the core is the failure this exists to catch. The runtime
runs the same test live on every reply before it is sent (runtime.Agent._release); this module is
the after-the-fact record of what was said.

Claims are found with patterns, sentence by sentence; questions, offers and negated sentences
are skipped. That is deliberately narrow: it can miss an unusual phrasing (a false negative),
and every pattern is listed below.

    python -m receipts.checker <conversation_id>
"""
import argparse
import json
import re

from receipts.db import connect

CLAIMS = {
    "freeze_card": [r"\bcard\b.{0,40}\b(?:is|has been|was|now)\b.{0,15}\bfrozen\b",
                    r"\bI(?:'ve| have)?\s+frozen\b", r"\bfroze (?:your|the) card\b"],
    "pay_loan": [r"\bpaid (?:toward|towards|to|on)\b", r"\bpayment\b.{0,40}\b(?:has posted|posted|went through|"
                 r"has gone through|is complete|was made|has been made)\b", r"\bI(?:'ve| have) (?:paid|made the payment)\b"],
    "transfer": [r"\b(?:transferred|moved)\b.{0,60}\b(?:to|into)\b", r"\btransfer\b.{0,30}\b(?:is complete|went through|"
                 r"has gone through|is done)\b"],
    "update_address": [r"\baddress\b.{0,30}\b(?:is|has been|was)\b.{0,10}\b(?:updated|changed)\b",
                       r"\bI(?:'ve| have) (?:updated|changed) your address\b"],
}
# Questions, offers and conditionals ("Shall I...", "Once approved, ...") and negations are not claims,
# nor are statements of existing state ("is currently frozen", "is already frozen"), nor a
# confirmation read back before acting ("Just to confirm: $390 from checking, paid toward ...").
SKIP = re.compile(r"\?\s*$|^(?:I can|I could|I'll|I will|Shall I|Should I|Would you|Do you|Want me|If|Once|When|After"
                  r"|(?:Just )?to confirm)\b"
                  r"|\b(?:not|n't|never|unable|nothing|no (?:money|funds|payment|transfer|change)s?)\b"
                  r"|\b(?:currently|already|still)\b", re.I)


def sentences(text: str) -> list[str]:
    text = re.sub(r"[*_`#]", "", text)
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def find_claims(text: str) -> list[tuple[str, str]]:
    found = []
    for s in sentences(text):
        if SKIP.search(s):
            continue
        for tool, patterns in CLAIMS.items():
            if any(re.search(p, s, re.I) for p in patterns):
                found.append((tool, s))
    return found


def backing(conn, conversation_id, tool: str, at=None):
    """The latest executed action of this kind in the conversation that the core recorded under its key
    (executed no later than `at`, when given); None when there is none."""
    return conn.execute(
        "SELECT a.action_id, a.args, o.response FROM app.action a"
        " JOIN app.action_event e ON e.action_id = a.action_id AND e.kind = 'executed'"
        "  AND (%s::timestamptz IS NULL OR e.at <= %s::timestamptz)"
        " JOIN core.operation o ON o.idempotency_key = a.action_id::text"
        " WHERE a.conversation_id = %s AND a.tool = %s ORDER BY e.at DESC LIMIT 1",
        (at, at, conversation_id, tool)).fetchone()


def check(conn, conversation_id: str) -> list[dict]:
    """Check a conversation once; a second call returns the stored result."""
    done = conn.execute("SELECT seq, tool, claim, verified, evidence FROM app.claim_check"
                        " WHERE conversation_id = %s ORDER BY check_id", (conversation_id,)).fetchall()
    if done:
        return done
    turns = conn.execute("SELECT seq, content, at FROM app.turn WHERE conversation_id = %s AND role = 'assistant'"
                         " ORDER BY seq", (conversation_id,)).fetchall()
    results = []
    held = {r["seq"] - 1 for r in conn.execute(
        "SELECT seq FROM app.turn WHERE conversation_id = %s AND role = 'runtime'"
        " AND content::text LIKE '\"Held the agent''s reply%%'", (conversation_id,))}
    for t in turns:
        text = " ".join(b.get("text", "") for b in t["content"] if b.get("type") == "text")
        for tool, claim in find_claims(text):
            found = backing(conn, conversation_id, tool, t["at"])
            evidence = ({"action_id": str(found["action_id"]), "args": found["args"],
                         "core_response": found["response"]} if found
                        else {"reason": f"no executed {tool} recorded by the core before this was said"})
            if not found and t["seq"] in held:   # the live check stopped it: the member never saw it
                evidence["held_before_sending"] = True
            row = {"seq": t["seq"], "tool": tool, "claim": claim, "verified": found is not None,
                   "evidence": evidence}
            conn.execute("INSERT INTO app.claim_check (conversation_id, seq, tool, claim, verified, evidence)"
                         " VALUES (%s, %s, %s, %s, %s, %s)",
                         (conversation_id, t["seq"], tool, claim, row["verified"], json.dumps(evidence, default=str)))
            results.append(row)
    conn.commit()
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("conversation_id")
    args = p.parse_args()
    with connect() as conn:
        for r in check(conn, args.conversation_id):
            print(f"{'OK ' if r['verified'] else 'RED'}  {r['tool']:15} {r['claim']}")


if __name__ == "__main__":
    main()
