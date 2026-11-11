"""The read-back. A write the core answers but does not record never reaches the member as done."""
import os

import httpx

os.environ.setdefault("DATABASE_URL", "postgresql:///fernhollow_test")

from fastapi.testclient import TestClient  # noqa: E402

from receipts.core import app  # noqa: E402
from receipts.runtime import Agent  # noqa: E402
from tests.test_runtime import Scripted, call, conn, text  # noqa: E402,F401

VERIFY = call(("verify_member", {"membership_no": "100005", "phone_last4": "9264"}))
FREEZE = call(("freeze_card", {"card_id": "C0005"}))


def events(conn, a, tool):
    return [(r["kind"], r["detail"]) for r in conn.execute(
        "SELECT e.kind, e.detail FROM app.action a JOIN app.action_event e USING (action_id)"
        " WHERE a.conversation_id = %s AND a.tool = %s ORDER BY e.event_id", (a.conversation_id, tool))]


def test_recorded_write_passes_the_read_back(conn):
    a = Agent(conn, TestClient(app), Scripted([VERIFY, FREEZE, text("Your card is now frozen.")]))
    assert a.send("Freeze my card. Membership 100005, phone 9264.") == "Your card is now frozen."
    assert [k for k, _ in events(conn, a, "freeze_card")] == ["requested", "executed"]
    assert a.session.handed_off is None


def test_vanished_write_hands_off_before_anyone_says_done(conn, monkeypatch):
    monkeypatch.setenv("RECEIPTS_CORE_BREAK", "drop_write")
    model = Scripted([VERIFY, FREEZE, text("Your card is now frozen.")])
    a = Agent(conn, TestClient(app), model)
    said = a.send("Freeze my card. Membership 100005, phone 9264.")
    assert said == "I've passed this to a member-service representative with everything we've covered."
    assert len(model.seen) == 2, "the model was asked again after the failed read-back"
    kinds = events(conn, a, "freeze_card")
    assert [k for k, _ in kinds] == ["requested", "failed"]
    assert kinds[1][1]["read_back"] == "the core has no write under this key"
    assert "read-back failed for freeze_card" in a.session.handed_off["reason"]
    assert conn.execute("SELECT status FROM core.card WHERE card_id = 'C0005'").fetchone()["status"] == "active"
    note = conn.execute("SELECT content FROM app.turn WHERE conversation_id = %s AND role = 'runtime'",
                        (a.conversation_id,)).fetchone()["content"]
    assert note == "Handed off by the runtime: read-back found no record of freeze_card"


def test_read_back_endpoint(conn):
    c = TestClient(app)
    assert c.get("/operations/nope").status_code == 404
    c.post("/cards/C0005/freeze", headers={"Idempotency-Key": "k-rb"})
    r = c.get("/operations/k-rb").json()
    assert r["operation"] == "freeze_card" and r["request"] == {"card_id": "C0005"}


# The live said-versus-did check: the model's own false "done" is held before the member sees it.
HANDOFF = "I'm connecting you with a member-service representative."


def test_false_done_is_held_and_handed_off(conn):
    a = Agent(conn, TestClient(app), Scripted([VERIFY, text("All set: your card is now frozen.")]))
    said = a.send("Freeze my card. Membership 100005, phone 9264.")
    assert said == HANDOFF
    assert "held a reply claiming an unrecorded freeze_card" in a.session.handed_off["reason"]
    note = conn.execute("SELECT content FROM app.turn WHERE conversation_id = %s AND role = 'runtime'",
                        (a.conversation_id,)).fetchone()["content"]
    assert note.startswith("Held the agent's reply: it claimed a freeze_card")


def test_true_done_and_non_claims_are_released(conn):
    a = Agent(conn, TestClient(app), Scripted([VERIFY, FREEZE, text("Your card is now frozen."),
                                                text("I can also move money between your accounts if you like.")]))
    assert a.send("Freeze my card. Membership 100005, phone 9264.") == "Your card is now frozen."
    assert a.send("What else can you do?") == "I can also move money between your accounts if you like."
    assert a.session.handed_off is None


def test_claim_about_a_different_action_is_still_held(conn):
    """A real freeze does not back a claimed transfer."""
    a = Agent(conn, TestClient(app), Scripted([VERIFY, FREEZE, text("Frozen, and I've moved $50 to your savings.")]))
    assert a.send("Freeze my card and move $50. Membership 100005, phone 9264.") == HANDOFF


def test_held_claim_is_recorded_as_held_not_as_said(conn):
    from receipts import checker
    a = Agent(conn, TestClient(app), Scripted([VERIFY, text("All set: your card is now frozen.")]))
    a.send("Freeze my card. Membership 100005, phone 9264.")
    [c] = checker.check(conn, str(a.conversation_id))
    assert not c["verified"] and c["evidence"]["held_before_sending"] is True


# The read-back itself failing is not the same as the core having no record.
class FlakyReadBack:
    """Wraps the test client; the first `fail_times` GETs of /operations/... answer 503 (None = always)."""
    def __init__(self, inner, fail_times=None):
        self.inner, self.fail_times, self.gets = inner, fail_times, 0

    def request(self, method, path, **kw):
        if method == "GET" and path.startswith("/operations/"):
            self.gets += 1
            if self.fail_times is None or self.gets <= self.fail_times:
                return httpx.Response(503, json={"detail": "unavailable"})
        return self.inner.request(method, path, **kw)


def test_unreachable_read_back_does_not_claim_the_write_failed(conn, monkeypatch):
    monkeypatch.setattr("receipts.runtime.READ_BACK_PAUSE_S", 0, raising=False)
    model = Scripted([VERIFY, FREEZE, text("Your card is now frozen.")])
    core = FlakyReadBack(TestClient(app))
    a = Agent(conn, core, model)
    said = a.send("Freeze my card. Membership 100005, phone 9264.")
    result = conn.execute("SELECT content FROM app.turn WHERE conversation_id = %s AND role = 'tool_results'"
                          " ORDER BY seq DESC LIMIT 1", (a.conversation_id,)).fetchone()["content"][0]["content"]
    assert "did not complete" not in result and "nothing was done" not in result
    assert "may have" in result
    assert core.gets > 1, "the read-back was retried"
    assert a.session.handed_off is not None
    assert said == "I've passed this to a member-service representative with everything we've covered."
    # the write did commit in the core
    assert conn.execute("SELECT status FROM core.card WHERE card_id = 'C0005'").fetchone()["status"] == "frozen"


def test_transient_read_back_error_is_retried(conn, monkeypatch):
    monkeypatch.setattr("receipts.runtime.READ_BACK_PAUSE_S", 0, raising=False)
    core = FlakyReadBack(TestClient(app), fail_times=1)
    a = Agent(conn, core, Scripted([VERIFY, FREEZE, text("Your card is now frozen.")]))
    assert a.send("Freeze my card. Membership 100005, phone 9264.") == "Your card is now frozen."
    assert a.session.handed_off is None
    assert [k for k, _ in events(conn, a, "freeze_card")] == ["requested", "executed"]


# The said-versus-did check compares amounts, not just the kind of action.
TRANSFER_50 = call(("transfer", {"from_account_id": "A100005-C", "to_account_id": "A100005-S", "amount_cents": 5000}))


def test_claim_of_a_different_amount_is_held(conn):
    a = Agent(conn, TestClient(app), Scripted([VERIFY, TRANSFER_50, text("Done, I've transferred $5,000 to your savings.")]))
    assert a.send("Move $50 to savings. Membership 100005, phone 9264.") == HANDOFF
    assert "held a reply claiming an unrecorded transfer" in a.session.handed_off["reason"]


def test_claim_of_the_recorded_amount_is_released(conn):
    a = Agent(conn, TestClient(app), Scripted([VERIFY, TRANSFER_50, text("Done, I've transferred $50.00 to your savings.")]))
    assert a.send("Move $50 to savings. Membership 100005, phone 9264.").startswith("Done")
    assert a.session.handed_off is None


def test_claim_without_an_amount_matches_on_kind(conn):
    a = Agent(conn, TestClient(app), Scripted([VERIFY, TRANSFER_50, text("Done, I've transferred it to your savings.")]))
    assert a.send("Move $50 to savings. Membership 100005, phone 9264.").startswith("Done")


def test_a_balance_after_the_claimed_amount_does_not_back_it(conn):
    a = Agent(conn, TestClient(app), Scripted([VERIFY, TRANSFER_50,
              text("Done, I've moved $5,000 to your savings; checking now shows $50.")]))
    assert a.send("Move $50 to savings. Membership 100005, phone 9264.") == HANDOFF


def test_a_bare_dollar_sign_is_not_an_amount():
    from receipts.checker import claimed_cents
    assert claimed_cents("in US$, the fee") is None
    assert claimed_cents("moved $5,000.50 to savings") == 500050
