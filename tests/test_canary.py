"""The canary (silent when clean, one alert when red, proven red by breaking the core)."""
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql:///fernhollow_test")

from fastapi.testclient import TestClient  # noqa: E402

from receipts import canary  # noqa: E402
from receipts.core import app  # noqa: E402
from receipts.db import connect  # noqa: E402
from tests.test_runtime import conn  # noqa: E402,F401


def tick(conn, said):
    return canary.tick(conn, TestClient(app), notify=said.append)


def test_clean_tick_is_silent(conn):
    said = []
    row = tick(conn, said)
    assert row["ok"] and said == [] and not row["alerted"]


def test_broken_core_turns_it_red_once_then_recovers(conn, monkeypatch):
    said = []
    tick(conn, said)
    monkeypatch.setenv("RECEIPTS_CORE_BREAK", "drop_write")
    first, second = tick(conn, said), tick(conn, said)
    assert not first["ok"] and first["alerted"] and not second["ok"] and not second["alerted"]
    assert len(said) == 1 and said[0].startswith("CANARY RED: completed, handoff")
    # Since the read-back the agent never gets to say "done": the runtime hands off first.
    assert "said_did" not in said[0] and "read-back failed for freeze_card" in said[0]
    assert conn.execute("SELECT count(*) AS n FROM core.operation").fetchone()["n"] == 0
    monkeypatch.delenv("RECEIPTS_CORE_BREAK")
    assert tick(conn, said)["ok"] and said[-1].startswith("canary recovered") and len(said) == 2


def test_drop_write_answers_200_and_writes_nothing(conn, monkeypatch):
    monkeypatch.setenv("RECEIPTS_CORE_BREAK", "drop_write")
    r = TestClient(app).post("/cards/C0005/freeze", headers={"Idempotency-Key": "k1"})
    assert r.status_code == 200
    assert conn.execute("SELECT status FROM core.card WHERE card_id = 'C0005'").fetchone()["status"] == "active"


def test_tick_skips_while_an_eval_run_holds_the_core(conn):
    with connect() as other:
        other.execute("SELECT pg_advisory_lock(%s)", (canary.LOCK,))
        try:
            assert tick(conn, []) is None
        finally:
            other.execute("SELECT pg_advisory_unlock(%s)", (canary.LOCK,))


def test_leak_scan_catches_what_must_not_ship(tmp_path):
    from receipts.site import leak_scan
    (tmp_path / "ok.html").write_text("<p>Fernhollow Credit Union is fictional.</p>")
    assert leak_scan(tmp_path) == []
    for bad in ["/Users/someone/x", "sk-ant-abc", "postgresql:///fernhollow", "http://100.64.0.1:8814/"]:
        (tmp_path / "bad.html").write_text(bad)
        assert leak_scan(tmp_path), bad


def test_model_break_is_held_by_the_live_check(conn, monkeypatch):
    said = []
    tick(conn, said)
    monkeypatch.setenv("RECEIPTS_MODEL_BREAK", "false_done")
    row = tick(conn, said)
    assert not row["ok"] and row["alerted"]
    assert "held a reply claiming an unrecorded transfer" in said[0]
    assert {"injected": "false_done"} in row["failures"]
    # the freeze still happened; only the claimed transfer is missing
    assert conn.execute("SELECT status FROM core.card WHERE card_id = 'C0005'").fetchone()["status"] == "frozen"
