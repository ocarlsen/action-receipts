"""The read-back. A write the core answers but does not record never reaches the member as done."""
import os

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
