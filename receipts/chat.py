"""Talk to the agent in the terminal, as a member of the fictional Fernhollow Credit Union.

Needs the fake core running (uvicorn receipts.core:app --port 8100) and an Anthropic API key.
Every model call is billed; the receipt's cost ledger shows what this conversation cost.

    python -m receipts.chat                     # type member turns; blank line ends
    python -m receipts.chat --script story.txt  # one member turn per line
    python -m receipts.chat --whoami M0001      # print M0001's sign-in details first
"""
import argparse
import time

import httpx

from receipts import checker, receipt
from receipts.db import connect
from receipts.runtime import Agent, AnthropicModel


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--core", default="http://127.0.0.1:8100")
    p.add_argument("--script", help="file of member turns, one per line")
    p.add_argument("--whoami", help="print this member's membership number and phone last four")
    p.add_argument("--model", default=None)
    p.add_argument("--approver-url", default="http://127.0.0.1:8101/")
    args = p.parse_args()

    with connect() as conn, httpx.Client(base_url=args.core, timeout=30) as core:
        if args.whoami:
            for r in conn.execute(
                    "SELECT m.full_name, h.membership_no, ms.label, m.phone_last4 FROM core.member m"
                    " JOIN core.membership_holder h USING (member_id) JOIN core.membership ms USING (membership_no)"
                    " WHERE m.member_id = %s", (args.whoami,)):
                print(f"{r['full_name']}: membership {r['membership_no']} ({r['label']}), phone ends {r['phone_last4']}")
        model = AnthropicModel(args.model) if args.model else AnthropicModel()
        def holding(text):
            print(f"agent> {text}\n       (waiting for a person: approve or reject at {args.approver_url})")
        agent = Agent(conn, core, model, on_runtime_message=holding)
        turns = open(args.script).read().splitlines() if args.script else None
        while True:
            if turns is not None:
                if not turns:
                    break
                text = turns.pop(0)
                print(f"\nmember> {text}")
            else:
                text = input("\nmember> ").strip()
                if not text:
                    break
            print("agent> (thinking…)", end="\r", flush=True)
            started = time.monotonic()
            reply = agent.send(text)
            print(f"agent> {reply}  [{time.monotonic() - started:.0f}s]")
            if agent.session.handed_off:
                break
        if agent.session.handed_off:
            print(f"\nhandoff packet: {agent.session.handed_off}")
        checks = checker.check(conn, str(agent.conversation_id))
        print(f"\nsaid versus did: {sum(c['verified'] for c in checks)} of {len(checks)} claims backed by the core")
        for c in checks:
            if not c["verified"]:
                print(f"  NOT IN CORE: {c['claim']}")
        print(f"receipt: {receipt.write(conn, str(agent.conversation_id))}")


if __name__ == "__main__":
    main()
