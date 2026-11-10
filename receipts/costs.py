"""Cost and latency rollups from the ledger: per conversation, and across a set of conversations.

    python -m receipts.costs <eval_run_id>
"""
import argparse
import math
from decimal import Decimal

from receipts.db import connect


def percentile(values: list, p: float):
    """Nearest-rank percentile (p in 0..100); None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def per_conversation(conn, conversation_ids: list[str]) -> list[dict]:
    """One row per conversation: model calls, cost, and model time (sum of call latencies)."""
    rows = conn.execute(
        "SELECT conversation_id::text AS cid, count(*) AS calls, sum(cost_usd) AS cost_usd,"
        " sum(latency_ms) AS model_ms, array_agg(latency_ms) AS call_ms"
        " FROM app.ledger WHERE conversation_id = ANY(%s::uuid[]) GROUP BY conversation_id",
        (conversation_ids,)).fetchall()
    by = {r["cid"]: r for r in rows}
    return [by.get(c, {"cid": c, "calls": 0, "cost_usd": Decimal(0), "model_ms": None, "call_ms": []})
            for c in conversation_ids]


def rollup(conn, conversation_ids: list[str], budget_usd: Decimal | None = None) -> dict:
    convs = per_conversation(conn, conversation_ids)
    costs = [c["cost_usd"] for c in convs]
    call_ms = [ms for c in convs for ms in (c["call_ms"] or []) if ms is not None]
    conv_ms = [c["model_ms"] for c in convs if c["model_ms"] is not None]
    total = sum(costs, Decimal(0))
    return {
        "conversations": len(convs),
        "total_usd": total,
        "mean_usd": total / len(convs) if convs else None,
        "p50_usd": percentile(costs, 50), "p95_usd": percentile(costs, 95), "max_usd": max(costs, default=None),
        "over_budget": None if budget_usd is None else sum(c > budget_usd for c in costs),
        "calls": sum(c["calls"] for c in convs),
        "call_p50_ms": percentile(call_ms, 50), "call_p95_ms": percentile(call_ms, 95),
        "conv_p50_ms": percentile(conv_ms, 50), "conv_p95_ms": percentile(conv_ms, 95),
        "latency_measured": len(call_ms),
    }


def for_run(conn, eval_run_id: str) -> dict:
    run = conn.execute("SELECT results FROM app.eval_run WHERE eval_run_id = %s", (eval_run_id,)).fetchone()
    return rollup(conn, [r["conversation_id"] for r in run["results"]])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("eval_run_id")
    args = p.parse_args()
    with connect() as conn:
        for k, v in for_run(conn, args.eval_run_id).items():
            print(f"{k:18} {v}")


if __name__ == "__main__":
    main()
