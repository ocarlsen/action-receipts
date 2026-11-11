"""The canary: one synthetic conversation per tick, through the real runtime, policy, core and checker.

The model's side is scripted, so a tick costs nothing and gives the same answer every time:
what it watches is everything around the model. A tick freezes a card and moves $50, then
scores the conversation against the core with the eval scorer. Silent when clean. On the first
failing tick after a clean one it raises one alert; later failing ticks stay quiet until it
recovers, which is reported once.

Proven red two ways, each by breaking one side and not the canary itself:
    RECEIPTS_CORE_BREAK=drop_write    the core answers every write with a plausible 200 and writes
                                      nothing; the runtime's read-back catches it
    RECEIPTS_MODEL_BREAK=false_done   the scripted model says the $50 moved without ever asking for
                                      the transfer; the live said-versus-did check holds the reply

    python -m receipts.canary            # one tick; exit 1 when it fails
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

from receipts.db import connect
from receipts.runtime import Agent
from receipts.scorer import score
from receipts.seed import reset_core

LOCK = 4242   # pg advisory lock shared with evalrun; it coordinates only when both use the same database
ALERT_LOG = Path("out/canary/alerts.log")
USAGE = NS(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
CHECKED = ["completed", "no_unauth_write", "disclosure", "said_did"]

TURNS = ["My card ending 3714 is lost. Membership 100005, phone ends 9264.",
         "Also move $50 from my checking to my savings.",
         "Yes, go ahead."]
CASE = {"id": "canary", "expect": {"handoff": False, "actions": [
    {"tool": "freeze_card", "args": {"card_id": "C0005"}},
    {"tool": "transfer", "args": {"from_account_id": "A100005-C", "to_account_id": "A100005-S", "amount_cents": 5000}},
]}}


def _call(name, args, i):
    return NS(content=[{"type": "tool_use", "id": f"toolu_canary_{i}", "name": name, "input": args}],
              stop_reason="tool_use", usage=USAGE, model="canary-script")


def _say(t):
    return NS(content=[{"type": "text", "text": t}], stop_reason="end_turn", usage=USAGE, model="canary-script")


class CanaryScript:
    """The model's side of the canary conversation, fixed."""
    model = "canary-script"

    def __init__(self, break_mode: str | None = None):
        self.replies = [
            _call("verify_member", {"membership_no": "100005", "phone_last4": "9264"}, 0),
            _call("freeze_card", {"card_id": "C0005"}, 1),
            _say("Your card ending 3714 is now frozen."),
            _say("To confirm: $50.00 from your checking to your savings. It posts immediately. Shall I go ahead?"),
            _call("transfer", {"from_account_id": "A100005-C", "to_account_id": "A100005-S", "amount_cents": 5000}, 2),
            _say("Done: $50.00 moved from your checking to your savings."),
        ]
        if break_mode == "false_done":   # claim the transfer without calling it
            del self.replies[4]

    def create(self, **_):
        return self.replies.pop(0)


def code_version() -> str | None:
    """The commit this tick runs on, with "-dirty" when the working tree has uncommitted changes."""
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True).stdout.strip()
        return head + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return None


def alert_text(failures: list[str], why: list[str]) -> str:
    return f"CANARY RED: {', '.join(failures)}. " + "; ".join(why)


def alert(text: str) -> None:
    """One line to the alert log and stdout, and a desktop notification where there is one."""
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG.open("a") as f:
        f.write(text + "\n")
    print(text)
    try:
        subprocess.run(["osascript", "-e", f'display notification {json.dumps(text)} with title "receipts canary"'],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def tick(conn, core_client, notify=alert) -> dict | None:
    """Run one tick. Returns the tick row, or None when an eval run holds the core."""
    if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK,)).fetchone()["ok"]:
        return None
    try:
        reset_core(conn)
        # Recorded on the tick, so a deliberate break is labelled as one.
        broken = os.environ.get("RECEIPTS_CORE_BREAK") or os.environ.get("RECEIPTS_MODEL_BREAK")
        agent = Agent(conn, core_client, CanaryScript(os.environ.get("RECEIPTS_MODEL_BREAK")), channel="canary")
        for t in TURNS:
            agent.send(t)
        result = score(conn, CASE, agent.conversation_id, agent.session.handed_off)
        failures = [c for c in CHECKED if result[c] is False] + (["handoff"] if result["handoff"] is False else [])
        ok = not failures
        prev = conn.execute("SELECT ok FROM app.canary_tick ORDER BY tick_id DESC LIMIT 1").fetchone()
        was_ok = prev is None or prev["ok"]
        alerted = False
        if not ok and was_ok:
            notify(alert_text(failures, result["why"]))
            alerted = True
        elif ok and not was_ok:
            notify("canary recovered: clean tick after a failure")
        row = conn.execute(
            "INSERT INTO app.canary_tick (conversation_id, ok, failures, alerted, code_version)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING tick_id, ok, failures, alerted, code_version, at",
            (agent.conversation_id, ok, json.dumps([{"column": c} for c in failures] + [{"why": w} for w in result["why"]]
                                                   + ([{"injected": broken}] if broken else [])),
             alerted, code_version())).fetchone()
        conn.commit()
        return row
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))
        conn.commit()


def main() -> None:
    from fastapi.testclient import TestClient
    from receipts.core import app
    with connect() as conn:
        row = tick(conn, TestClient(app))
    sys.exit(0 if row is None or row["ok"] else 1)


if __name__ == "__main__":
    main()
