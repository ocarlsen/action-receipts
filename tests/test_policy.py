"""Policy engine, approval gate, said-versus-did checker, handoff packet."""
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql:///fernhollow_test")

from fastapi.testclient import TestClient  # noqa: E402

from receipts import approver, checker, receipt  # noqa: E402
from receipts.approver import decide  # noqa: E402
from receipts.policy import classify  # noqa: E402
from tests.test_runtime import agent, call, conn, core_state, m0001, text  # noqa: E402,F401


def events(conn, a, tool):
    return [r["kind"] for r in conn.execute(
        "SELECT e.kind FROM app.action a JOIN app.action_event e USING (action_id)"
        " WHERE a.conversation_id = %s AND a.tool = %s ORDER BY e.event_id", (a.conversation_id, tool))]


def never(conn, action_id):
    raise AssertionError("asked a person to approve a call that needs no approval")


def deciding(decision, reason=None):
    def wait(conn, action_id):
        decide(conn, str(action_id), decision, "test-approver", reason)
        return conn.execute("SELECT decision, approver, payload_sha256, reason FROM app.approval"
                            " WHERE action_id = %s", (action_id,)).fetchone()
    return wait


def pay(m, cents):
    return call(("pay_loan", {"loan_id": m["loan_id"], "from_account_id": m["checking"], "amount_cents": cents}))


def verify(m):
    return call(("verify_member", {"membership_no": m["membership_no"], "phone_last4": m["phone_last4"]}))


@pytest.mark.parametrize("tool,args,expected", [
    ("get_balance", {"account_id": "x"}, ("read", "read.balance")),
    ("freeze_card", {"card_id": "x"}, ("low", "low.freeze")),
    ("pay_loan", {"amount_cents": 25000}, ("low", "low.loan")),
    ("pay_loan", {"amount_cents": 25001}, ("gated", "gated.loan")),
    ("transfer", {"amount_cents": 500000}, ("gated", "gated.transfer")),
    ("transfer", {"amount_cents": 500001}, ("denied", None)),
    ("update_address", {"address": "1 Main St"}, ("gated", "gated.address")),
    ("close_account", {}, ("denied", None)),
])
def test_classify(conn, tool, args, expected):
    assert classify(conn, tool, args) == expected


def test_large_payment_waits_for_approval(conn, m0001):
    m = m0001
    _, before = core_state(conn, m)
    said = []
    a = agent(conn, [verify(m), pay(m, m["due"]), text("Done: the payment has posted.")])
    a.on_runtime_message = said.append
    a.send("pay my loan")
    assert m["due"] > 25000
    assert events(conn, a, "pay_loan") == ["requested", "approved", "executed"]
    assert said == ["One moment while I confirm this with a colleague."]
    assert core_state(conn, m)[1] == before - m["due"]


def test_small_payment_needs_no_approval(conn, m0001):
    m = m0001
    a = agent(conn, [verify(m), pay(m, 10000), text("Paid.")], wait=never)
    a.send("pay $100 on my loan")
    assert events(conn, a, "pay_loan") == ["requested", "executed"]


def test_rejected_payment_changes_nothing(conn, m0001):
    m = m0001
    _, before = core_state(conn, m)
    a = agent(conn, [verify(m), pay(m, m["due"]), text("A colleague declined it, so nothing was paid.")],
              wait=deciding("reject", "unusual for this member"))
    a.send("pay my loan")
    assert events(conn, a, "pay_loan") == ["requested", "rejected", "refused"]
    assert core_state(conn, m)[1] == before
    result = a.model.seen[2][-1]["content"][0]
    assert result["is_error"] and "unusual for this member" in result["content"]


def test_no_decision_in_time_changes_nothing(conn, m0001):
    m = m0001
    _, before = core_state(conn, m)
    a = agent(conn, [verify(m), pay(m, m["due"]), text("I couldn't get that approved, so nothing was paid.")],
              wait=lambda conn, action_id: None)
    a.send("pay my loan")
    assert events(conn, a, "pay_loan") == ["requested", "expired", "refused"]
    assert core_state(conn, m)[1] == before


def test_approval_for_a_different_payload_is_refused(conn, m0001):
    m = m0001
    _, before = core_state(conn, m)

    def tampered(conn, action_id):
        row = deciding("approve")(conn, action_id)
        return {**row, "payload_sha256": "0" * 64}
    a = agent(conn, [verify(m), pay(m, m["due"]), text("Nothing was paid.")], wait=tampered)
    a.send("pay my loan")
    assert events(conn, a, "pay_loan")[-1] == "refused"
    assert core_state(conn, m)[1] == before


def test_approving_twice_adds_one_row(conn, m0001):
    m = m0001
    a = agent(conn, [verify(m), pay(m, m["due"]), text("Done.")])
    a.send("pay my loan")
    action_id = conn.execute("SELECT action_id FROM app.action WHERE conversation_id = %s AND tool = 'pay_loan'",
                             (a.conversation_id,)).fetchone()["action_id"]
    assert decide(conn, str(action_id), "approve", "someone-else") is False
    assert conn.execute("SELECT count(*) FROM app.approval WHERE action_id = %s",
                        (action_id,)).fetchone()["count"] == 1
    assert events(conn, a, "pay_loan").count("approved") == 1


def test_no_policy_row_is_refused_and_on_the_receipt(conn, m0001, tmp_path):
    m = m0001
    savings = m["checking"].replace("-C", "-S")
    _, before = core_state(conn, m)
    a = agent(conn, [verify(m),
                     call(("transfer", {"from_account_id": m["checking"], "to_account_id": savings,
                                        "amount_cents": 600000})),
                     text("I can't move that much in chat.")], wait=never)
    a.send("move $6,000 to savings")
    assert events(conn, a, "transfer") == ["requested", "refused"]
    assert core_state(conn, m)[1] == before
    page = receipt.write(conn, str(a.conversation_id), str(tmp_path)).read_text()
    assert "no policy rule allows transfer" in page and "policy: denied" in page


def test_address_change_is_always_gated(conn, m0001):
    m = m0001
    a = agent(conn, [verify(m), call(("update_address", {"address": "12 New Road, Fernhollow"})), text("Updated.")])
    a.send("I moved")
    assert events(conn, a, "update_address") == ["requested", "approved", "executed"]


def test_checker_flags_a_claim_with_no_core_change(conn, m0001):
    m = m0001
    a = agent(conn, [verify(m), text("Your card is now frozen.")])
    a.send("my card was stolen")
    results = checker.check(conn, str(a.conversation_id))
    assert [(r["tool"], r["verified"]) for r in results] == [("freeze_card", False)]
    assert core_state(conn, m)[0] == "active"


def test_checker_verifies_a_real_action_once(conn, m0001):
    m = m0001
    a = agent(conn, [verify(m), call(("freeze_card", {"card_id": m["card_id"]})),
                     text("Your card is now frozen. Shall I also pay your loan?")])
    a.send("my card was stolen")
    first = checker.check(conn, str(a.conversation_id))
    assert [(r["tool"], r["verified"]) for r in first] == [("freeze_card", True)]
    assert checker.check(conn, str(a.conversation_id)) == first
    assert conn.execute("SELECT count(*) FROM app.claim_check WHERE conversation_id = %s",
                        (a.conversation_id,)).fetchone()["count"] == 1


def test_checker_ignores_offers_and_negations():
    assert checker.find_claims("Shall I freeze your card? I could not pay the loan. Once approved, it will post.") == []


def test_handoff_carries_the_context_packet(conn, m0001):
    m = m0001
    a = agent(conn, [verify(m), call(("freeze_card", {"card_id": m["card_id"]})),
                     call(("handoff", {"reason": "dispute", "summary": "card stolen, wants to dispute charges"}))])
    a.send("my card was stolen and I want to dispute charges")
    packet = a.session.handed_off
    assert packet["verified"] and packet["member_id"] == "M0001"
    assert [(x["tool"], x["status"]) for x in packet["actions"]] == [
        ("verify_member", "executed"), ("freeze_card", "executed")]


def test_approver_console(conn, m0001):
    m = m0001
    a = agent(conn, [verify(m), pay(m, m["due"]), text("Nothing was paid.")], wait=lambda c, i: None)
    a.send("pay my loan")   # expires: the queue must not offer it
    ui = TestClient(approver.app)
    assert "Nothing waiting" in ui.get("/").text

    pending = []

    def via_console(conn, action_id):
        page = ui.get("/").text
        pending.append(str(action_id) in page)
        r = ui.post(f"/actions/{action_id}/decide", data={"decision": "approve", "approver": "owen", "reason": ""},
                    follow_redirects=False)
        assert r.status_code == 303
        return conn.execute("SELECT decision, approver, payload_sha256, reason FROM app.approval"
                            " WHERE action_id = %s", (action_id,)).fetchone()
    b = agent(conn, [verify(m), pay(m, m["due"]), text("Done.")], wait=via_console)
    b.send("pay my loan")
    assert pending == [True]
    assert events(conn, b, "pay_loan") == ["requested", "approved", "executed"]


def _decide_over_http(action_id, decision="approve"):
    from receipts import approver
    return TestClient(approver.app).post(f"/actions/{action_id}/decide", follow_redirects=False,
                                         data={"decision": decision, "approver": "late"})


def _gated_action_id(conn, a):
    return conn.execute("SELECT action_id FROM app.action WHERE conversation_id = %s AND tool = 'pay_loan'",
                        (a.conversation_id,)).fetchone()["action_id"]


def test_approval_after_expiry_is_refused(conn, m0001):
    m = m0001
    a = agent(conn, [verify(m), pay(m, m["due"]), text("Nothing was paid.")], wait=lambda conn, action_id: None)
    a.send("pay my loan")
    aid = _gated_action_id(conn, a)
    assert _decide_over_http(aid).status_code == 409
    assert conn.execute("SELECT count(*) FROM app.approval WHERE action_id = %s", (aid,)).fetchone()["count"] == 0


def test_approval_after_a_refusal_is_refused(conn, m0001):
    m = m0001

    def tampered(conn, action_id):
        return {**deciding("approve")(conn, action_id), "payload_sha256": "0" * 64}
    a = agent(conn, [verify(m), pay(m, m["due"]), text("Nothing was paid.")], wait=tampered)
    a.send("pay my loan")
    aid = _gated_action_id(conn, a)
    assert events(conn, a, "pay_loan")[-1] == "refused"
    assert _decide_over_http(aid, "reject").status_code == 409
