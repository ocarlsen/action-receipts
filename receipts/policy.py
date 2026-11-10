"""The policy engine: which tool calls may run, and which wait for a person.

Rules live in app.policy_rule. Default deny: a call that no rule covers is refused.
"""
import hashlib
import json


def classify(conn, tool: str, args: dict) -> tuple[str, str | None]:
    """Return (policy_class, rule_id); ('denied', None) when no rule covers the call."""
    amount = args.get("amount_cents")
    rules = conn.execute(
        "SELECT rule_id, max_amount_cents, policy_class FROM app.policy_rule"
        " WHERE tool = %s ORDER BY max_amount_cents NULLS LAST", (tool,)).fetchall()
    for r in rules:
        if r["max_amount_cents"] is None or (amount is not None and amount <= r["max_amount_cents"]):
            return r["policy_class"], r["rule_id"]
    return "denied", None


def payload_sha256(tool: str, args: dict) -> str:
    """The exact bytes an approver signs off on."""
    canonical = json.dumps({"tool": tool, "args": args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
