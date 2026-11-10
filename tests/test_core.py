"""Deterministic seed, idempotent core, append-only audit tables."""
import os
import uuid

import psycopg
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql:///fernhollow_test")

from fastapi.testclient import TestClient  # noqa: E402

from receipts.core import app  # noqa: E402
from receipts.db import connect  # noqa: E402
from receipts.seed import core_hash, reseed  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db():
    with connect() as conn:
        reseed(conn)
    yield


def db():
    return connect()


def balance(account_id):
    return client.get(f"/accounts/{account_id}/balance").json()["balance_cents"]


def m0001_personal():
    ms = client.get("/members/M0001/memberships").json()["memberships"]
    personal = next(m for m in ms if m["label"] == "Personal")
    checking = next(a["account_id"] for a in personal["accounts"] if a["kind"] == "checking")
    return ms, personal, checking


def key():
    return {"Idempotency-Key": str(uuid.uuid4())}


def test_seed_twice_same_hash():
    with db() as conn:
        first = reseed(conn)
        second = reseed(conn)
    assert first == second


def test_seed_edge_cases():
    ms, personal, _ = m0001_personal()
    assert {m["label"] for m in ms} == {"Personal", "Business"}
    assert personal["loans"], "M0001 needs a loan to pay"
    joint = client.get("/members/M0003/memberships").json()["memberships"]
    assert joint[0]["role"] == "joint"
    cards = client.get("/members/M0004/memberships").json()["memberships"][0]["cards"]
    assert cards[0]["status"] == "frozen"


def test_verify():
    with db() as conn:
        row = conn.execute(
            "SELECT h.membership_no, m.phone_last4 FROM core.member m"
            " JOIN core.membership_holder h USING (member_id) WHERE m.member_id = 'M0002'").fetchone()
    ok = client.post("/verify", json={"membership_no": row["membership_no"], "phone_last4": row["phone_last4"]})
    assert ok.status_code == 200 and ok.json()["member_id"] == "M0002"
    wrong = "0000" if row["phone_last4"] != "0000" else "1111"
    assert client.post("/verify", json={"membership_no": row["membership_no"],
                                        "phone_last4": wrong}).status_code == 401


def test_write_requires_key():
    assert client.post("/cards/C0001/freeze").status_code == 400


def test_replay_same_key_writes_once():
    _, personal, checking = m0001_personal()
    loan = personal["loans"][0]
    body = {"from_account_id": checking, "amount_cents": loan["payment_due_cents"]}
    before = balance(checking)
    h = key()
    first = client.post(f"/loans/{loan['loan_id']}/payments", json=body, headers=h)
    again = client.post(f"/loans/{loan['loan_id']}/payments", json=body, headers=h)
    assert first.status_code == again.status_code == 200
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert again.json() == first.json()
    assert balance(checking) == before - loan["payment_due_cents"]


def test_same_key_different_request_refused():
    _, personal, checking = m0001_personal()
    loan = personal["loans"][0]
    h = key()
    assert client.post(f"/loans/{loan['loan_id']}/payments",
                       json={"from_account_id": checking, "amount_cents": 100}, headers=h).status_code == 200
    r = client.post(f"/loans/{loan['loan_id']}/payments",
                    json={"from_account_id": checking, "amount_cents": 200}, headers=h)
    assert r.status_code == 409


def test_after_commit_fault_then_retry_applies_once():
    _, personal, checking = m0001_personal()
    savings = next(a["account_id"] for a in personal["accounts"] if a["kind"] == "savings")
    before = balance(checking)
    h = key()
    body = {"from_account_id": checking, "to_account_id": savings, "amount_cents": 1_000}
    lost = client.post("/transfers", json=body, headers={**h, "X-Fault": "after_commit"})
    assert lost.status_code == 503
    retry = client.post("/transfers", json=body, headers=h)
    assert retry.status_code == 200 and retry.headers.get("Idempotent-Replayed") == "true"
    assert balance(checking) == before - 1_000


def test_error_fault_changes_nothing():
    with db() as conn:
        before = core_hash(conn)
    r = client.post("/cards/C0001/freeze", headers={**key(), "X-Fault": "error"})
    assert r.status_code == 503
    with db() as conn:
        assert core_hash(conn) == before


def test_freeze_already_frozen_is_not_a_change():
    card = client.get("/members/M0004/memberships").json()["memberships"][0]["cards"][0]
    r = client.post(f"/cards/{card['card_id']}/freeze", headers=key())
    assert r.json() == {"card_id": card["card_id"], "status": "frozen", "changed": False}


def test_refused_write_leaves_no_trace():
    _, _, checking = m0001_personal()
    with db() as conn:
        before = core_hash(conn)
    r = client.post("/transfers", headers=key(), json={
        "from_account_id": checking, "to_account_id": checking, "amount_cents": 1})
    assert r.status_code == 422
    r = client.post("/transfers", headers=key(), json={
        "from_account_id": checking, "to_account_id": "A100002-S", "amount_cents": 10**12})
    assert r.status_code == 422
    with db() as conn:
        assert core_hash(conn) == before


@pytest.mark.parametrize("sql", [
    "UPDATE app.approval SET decision = 'approve'",
    "DELETE FROM app.approval",
    "UPDATE core.operation SET response = '{}'",
    "DELETE FROM app.action_event",
])
def test_append_only(sql):
    conv, act = uuid.uuid4(), uuid.uuid4()
    with db() as conn:
        conn.execute("INSERT INTO app.conversation (conversation_id) VALUES (%s)", (conv,))
        conn.execute("INSERT INTO app.action (action_id, conversation_id, tool, args, policy_class)"
                     " VALUES (%s, %s, 'pay_loan', '{}', 'gated')", (act, conv))
        conn.execute("INSERT INTO app.action_event (action_id, kind) VALUES (%s, 'requested')", (act,))
        conn.execute("INSERT INTO app.approval (action_id, decision, approver, payload_sha256)"
                     " VALUES (%s, 'reject', 'owen', 'x')", (act,))
        conn.commit()
    client.post("/cards/C0001/freeze", headers=key())
    with db() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(sql)


def test_second_decision_on_same_action_refused():
    conv, act = uuid.uuid4(), uuid.uuid4()
    with db() as conn:
        conn.execute("INSERT INTO app.conversation (conversation_id) VALUES (%s)", (conv,))
        conn.execute("INSERT INTO app.action (action_id, conversation_id, tool, args, policy_class)"
                     " VALUES (%s, %s, 'transfer', '{}', 'gated')", (act, conv))
        conn.execute("INSERT INTO app.approval (action_id, decision, approver, payload_sha256)"
                     " VALUES (%s, 'approve', 'owen', 'x')", (act,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO app.approval (action_id, decision, approver, payload_sha256)"
                         " VALUES (%s, 'approve', 'owen', 'x')", (act,))
