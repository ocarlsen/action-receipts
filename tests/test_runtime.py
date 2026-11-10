"""The runtime over the fake core, driven by a scripted model (no API calls, no cost)."""
import json
import os
from types import SimpleNamespace as NS

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql:///fernhollow_test")

from fastapi.testclient import TestClient  # noqa: E402

from receipts import receipt  # noqa: E402
from receipts.approver import decide  # noqa: E402
from receipts.core import app  # noqa: E402
from receipts.db import connect  # noqa: E402
from receipts.runtime import Agent, cost_usd  # noqa: E402
from receipts.seed import reseed  # noqa: E402

USAGE = NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=3000, cache_creation_input_tokens=0)


def text(t):
    return NS(content=[{"type": "text", "text": t}], stop_reason="end_turn", usage=USAGE, model="claude-opus-5")


def call(*calls):
    blocks = [{"type": "tool_use", "id": f"toolu_{name}_{i}", "name": name, "input": inp}
              for i, (name, inp) in enumerate(calls)]
    return NS(content=blocks, stop_reason="tool_use", usage=USAGE, model="claude-opus-5")


class Scripted:
    """Returns canned responses in order and keeps what it was sent."""
    model = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.seen = []

    def create(self, *, system, tools, messages):
        self.seen.append(json.loads(json.dumps(messages)))
        return self.responses.pop(0)


@pytest.fixture
def conn():
    with connect() as c:
        reseed(c)
        yield c


@pytest.fixture
def m0001(conn):
    """M0001's sign-in details and the ids the user story needs."""
    r = conn.execute("SELECT h.membership_no, m.phone_last4 FROM core.member m JOIN core.membership_holder h"
                     " USING (member_id) JOIN core.membership ms USING (membership_no)"
                     " WHERE m.member_id = 'M0001' AND ms.label = 'Personal'").fetchone()
    no = r["membership_no"]
    loan = conn.execute("SELECT loan_id, payment_due_cents FROM core.loan WHERE membership_no = %s", (no,)).fetchone()
    card = conn.execute("SELECT card_id FROM core.card c JOIN core.account a USING (account_id)"
                        " WHERE a.membership_no = %s", (no,)).fetchone()
    return {"membership_no": no, "phone_last4": r["phone_last4"], "checking": f"A{no}-C",
            "card_id": card["card_id"], "loan_id": loan["loan_id"], "due": loan["payment_due_cents"]}


def approving(conn, action_id):
    """Stands in for the person at the approval queue: approves at once."""
    decide(conn, str(action_id), "approve", "test-approver")
    return conn.execute("SELECT decision, approver, payload_sha256, reason FROM app.approval"
                        " WHERE action_id = %s", (action_id,)).fetchone()


def agent(conn, responses, wait=approving):
    return Agent(conn, TestClient(app), Scripted(responses), wait=wait)


def core_state(conn, m):
    card = conn.execute("SELECT status FROM core.card WHERE card_id = %s", (m["card_id"],)).fetchone()["status"]
    bal = conn.execute("SELECT balance_cents FROM core.account WHERE account_id = %s", (m["checking"],)).fetchone()
    return card, bal["balance_cents"]


def test_user_story_end_to_end(conn, m0001):
    m = m0001
    _, before = core_state(conn, m)
    a = agent(conn, [
        call(("verify_member", {"membership_no": m["membership_no"], "phone_last4": m["phone_last4"]})),
        call(("list_memberships", {})),
        text("You have two memberships, Personal and Business. Which one is this about?"),
        call(("freeze_card", {"card_id": m["card_id"]})),
        text("Your card is frozen. Shall I pay this month's loan payment from checking?"),
        call(("pay_loan", {"loan_id": m["loan_id"], "from_account_id": m["checking"], "amount_cents": m["due"]})),
        text("Done: the payment has posted."),
    ])
    assert "two memberships" in a.send(f"Hi, membership {m['membership_no']}, phone ends {m['phone_last4']}")
    assert "frozen" in a.send("Personal. My card was stolen.")
    assert "posted" in a.send("Yes please.")

    # the model saw the core's answer, not just a success flag
    listing = a.model.seen[2][-1]["content"][0]
    assert not listing.get("is_error") and m["loan_id"] in listing["content"]

    card, after = core_state(conn, m)
    assert card == "frozen"
    assert after == before - m["due"]

    finals = conn.execute(
        "SELECT a.tool, (SELECT kind FROM app.action_event e WHERE e.action_id = a.action_id"
        "  ORDER BY event_id DESC LIMIT 1) AS final FROM app.action a WHERE conversation_id = %s"
        " ORDER BY created_at", (a.conversation_id,)).fetchall()
    assert [(r["tool"], r["final"]) for r in finals] == [
        ("verify_member", "executed"), ("list_memberships", "executed"),
        ("freeze_card", "executed"), ("pay_loan", "executed")]
    n_turns = conn.execute("SELECT count(*) FROM app.turn WHERE conversation_id = %s",
                           (a.conversation_id,)).fetchone()["count"]
    assert n_turns == 3 + 7 + 4 + 1  # member turns + model responses + tool-result turns + holding message
    n_ledger = conn.execute("SELECT count(*) FROM app.ledger WHERE conversation_id = %s",
                            (a.conversation_id,)).fetchone()["count"]
    assert n_ledger == 7


def test_idempotency_key_is_the_action_id(conn, m0001):
    m = m0001
    a = agent(conn, [
        call(("verify_member", {"membership_no": m["membership_no"], "phone_last4": m["phone_last4"]})),
        call(("freeze_card", {"card_id": m["card_id"]})),
        text("Frozen."),
    ])
    a.send("freeze my card")
    row = conn.execute("SELECT a.action_id, o.idempotency_key FROM app.action a JOIN core.operation o"
                       " ON o.idempotency_key = a.action_id::text WHERE a.tool = 'freeze_card'"
                       " AND a.conversation_id = %s", (a.conversation_id,)).fetchone()
    assert row is not None


def test_unverified_reads_are_refused(conn, m0001):
    m = m0001
    a = agent(conn, [call(("get_balance", {"account_id": m["checking"]})), text("I need to verify you first.")])
    a.send("what's my balance?")
    result = a.model.seen[1][-1]["content"][0]
    assert result["is_error"] and "not verified" in result["content"]


def test_another_members_account_is_refused(conn, m0001):
    m = m0001
    other = conn.execute("SELECT account_id FROM core.account a JOIN core.membership_holder h USING (membership_no)"
                         " WHERE h.member_id = 'M0010' AND a.kind = 'checking'").fetchone()["account_id"]
    _, before = core_state(conn, m)
    a = agent(conn, [
        call(("verify_member", {"membership_no": m["membership_no"], "phone_last4": m["phone_last4"]})),
        call(("transfer", {"from_account_id": m["checking"], "to_account_id": other, "amount_cents": 5000})),
        text("I can only move money between your own accounts."),
    ])
    a.send("send $50 to my friend")
    _, after = core_state(conn, m)
    assert after == before
    last = conn.execute("SELECT e.kind FROM app.action a JOIN app.action_event e USING (action_id)"
                        " WHERE a.tool = 'transfer' AND a.conversation_id = %s ORDER BY e.event_id DESC LIMIT 1",
                        (a.conversation_id,)).fetchone()
    assert last["kind"] == "refused"


def test_core_failure_is_reported_not_claimed(conn, m0001):
    m = m0001
    a = agent(conn, [
        call(("verify_member", {"membership_no": m["membership_no"], "phone_last4": "0000" if m["phone_last4"] != "0000" else "1111"})),
        text("That didn't match our records."),
    ])
    a.send("hi")
    result = a.model.seen[1][-1]["content"][0]
    assert result["is_error"] and "did not complete" in result["content"]
    assert a.session.member_id is None


def test_handoff_ends_the_conversation(conn):
    a = agent(conn, [call(("handoff", {"reason": "asked for a person", "summary": "wants a human"}))])
    reply = a.send("let me talk to a person")
    assert "representative" in reply
    assert a.session.handed_off["summary"] == "wants a human"
    assert "representative" in a.send("hello?")


def test_runtime_hands_off_when_the_model_loops(conn, m0001):
    m = m0001
    a = agent(conn, [call(("list_memberships", {}))] * 12)
    a.send("hi")
    assert a.session.handed_off["reason"].startswith("no answer after")


def test_receipt_renders(conn, m0001, tmp_path):
    m = m0001
    a = agent(conn, [
        call(("verify_member", {"membership_no": m["membership_no"], "phone_last4": m["phone_last4"]})),
        call(("freeze_card", {"card_id": m["card_id"]})),
        text("Your card ending is frozen."),
    ])
    a.send("freeze my card <script>")
    page = receipt.write(conn, str(a.conversation_id), str(tmp_path)).read_text()
    assert "freeze_card" in page and "executed" in page
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "$0.0" in page


def test_cost():
    # 1000 in at $5/M + 3000 cache read at $0.50/M + 100 out at $25/M
    assert str(cost_usd("claude-opus-5", USAGE)) == "0.009000"
