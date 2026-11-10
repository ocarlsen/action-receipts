"""Budget guard, per-call latency, cost rollups, and the two-model benchmark verdict."""
import os
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql:///fernhollow_test")

from fastapi.testclient import TestClient  # noqa: E402

from receipts import benchmark, evalrun  # noqa: E402
from receipts.core import app  # noqa: E402
from receipts.costs import percentile, rollup  # noqa: E402
from receipts.runtime import Agent, AnthropicModel  # noqa: E402
from tests.test_runtime import Scripted, call, conn, text  # noqa: E402,F401
from tests.test_evals import DISCLOSE, PAY, VERIFY, case  # noqa: E402

BALANCE = call(("get_balance", {"account_id": "A100005-C"}))


def test_budget_guard_hands_off_a_long_conversation(conn):
    """Each scripted call costs $0.009; a $0.03 budget stops it after four calls, mid-loop."""
    model = Scripted([VERIFY] + [BALANCE] * 10 + [text("unreachable")])
    a = Agent(conn, TestClient(app), model, budget_usd=Decimal("0.03"))
    said = a.send("Membership 100005, phone 9264. Keep checking my balance.")
    assert a.session.handed_off and "budget" in a.session.handed_off["reason"]
    assert said == "I'm connecting you with a member-service representative."
    assert len(model.seen) == 4 and a.spent_usd() >= Decimal("0.03")
    note = conn.execute("SELECT content FROM app.turn WHERE conversation_id = %s AND role = 'runtime'",
                        (a.conversation_id,)).fetchone()["content"]
    assert note.startswith("Handed off by the runtime: conversation budget reached")


def test_every_call_records_latency(conn):
    a = Agent(conn, TestClient(app), Scripted([VERIFY, text("Verified.")]))
    a.send("Membership 100005, phone 9264.")
    ms = [r["latency_ms"] for r in conn.execute("SELECT latency_ms FROM app.ledger WHERE conversation_id = %s",
                                                  (a.conversation_id,))]
    assert len(ms) == 2 and all(m is not None and m >= 0 for m in ms)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.messages = NS(create=lambda **kw: self.calls.append(("plain", kw)))
        self.beta = NS(messages=NS(create=lambda **kw: self.calls.append(("beta", kw))))


@pytest.mark.parametrize("model,path,fallbacks", [
    ("claude-opus-5", "beta", "default"),
    ("claude-sonnet-5", "plain", None),
])
def test_fallbacks_only_where_documented(model, path, fallbacks):
    c = FakeClient()
    AnthropicModel(model, client=c).create(system=[], tools=[], messages=[])
    kind, kw = c.calls[0]
    assert kind == path and kw.get("fallbacks") == fallbacks and kw["model"] == model
    assert kw["thinking"] == {"type": "adaptive"}


def test_percentile_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([3, 1, 2], 50) == 2
    assert percentile(list(range(1, 101)), 95) == 95
    assert percentile([5], 95) == 5


def run(r_id, model, passed, cost, safety=0, cases=60):
    results = [{"id": f"c{i}", "trap": None, "passed": i < passed, "why": [] if i < passed else ["x"],
                **{c: True for c in benchmark.COLUMNS}} for i in range(cases)]
    return {"id": r_id, "model": model, "set_sha256": "s", "results": results,
            "rates": {"passed": passed, "cases": cases}, "safety_fails": safety,
            "costs": {"mean_usd": Decimal(cost)}, "judge": None}


@pytest.mark.parametrize("cand,switch,word", [
    (run("b", "cheap", 58, "0.010"), True, "Switch"),
    (run("b", "cheap", 56, "0.010"), True, "Switch"),         # two fewer: inside rerun noise
    (run("b", "cheap", 55, "0.010"), False, "rerun noise"),   # three fewer: a real gap
    (run("b", "cheap", 58, "0.010", safety=1), False, "safety"),
    (run("b", "cheap", 58, "0.020"), False, "bar"),           # 80% of the cost: not worth it
])
def test_verdict_rule(cand, switch, word):
    base = run("a", "big", 58, "0.025")
    got, line = benchmark.verdict(base, cand)
    assert got is switch and word in line


def test_benchmark_page_from_two_runs(conn, tmp_path):
    good = [VERIFY, DISCLOSE, call(("pay_loan", PAY)), text("Your payment has posted.")]
    planted = [VERIFY, DISCLOSE, text("Your payment has posted.")]
    a_id, _ = evalrun.run(conn, [case()], "f" * 64, Scripted(good), TestClient(app))
    b_id, _ = evalrun.run(conn, [case()], "f" * 64, Scripted(planted), TestClient(app))
    page = benchmark.write(conn, a_id, b_id, str(tmp_path)).read_text()
    assert "Keep" in page and "Cases that differ" in page and "t-01" in page and "p95" in page
    costs = rollup(conn, [r["conversation_id"] for r in
                          conn.execute("SELECT results FROM app.eval_run WHERE eval_run_id = %s",
                                       (a_id,)).fetchone()["results"]])
    assert costs["conversations"] == 1 and costs["calls"] == 4 and costs["p95_usd"] == Decimal("0.036")
