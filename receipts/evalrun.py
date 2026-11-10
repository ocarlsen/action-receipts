"""Run the eval set: every case is a scripted conversation, scored against the core.

Before each case the core goes back to the seed. The member's turns are fixed text; a stand-in
for the person at the approval queue approves or rejects as the case says; an optional fault
makes the core fail one write. Then scorer.py reads the rows and the core, and the run is saved
to app.eval_run and rendered as a report page.

    python -m receipts.evalrun [--only pay-01,xfer-02] [--judge] [--model claude-opus-5]

Live: calls the model for every case (billed to ANTHROPIC_API_KEY). Tests drive it with a
scripted model instead.
"""
import argparse
import hashlib
import html
import json
import uuid
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from receipts import receipt
from receipts.approver import decide
from receipts.costs import rollup
from receipts.db import connect
from receipts.runtime import MODEL, Agent
from receipts.scorer import COLUMNS, score
from receipts.seed import reset_core

CASES = Path(__file__).resolve().parent.parent / "evals" / "cases.json"
FROZEN = CASES.with_name("FROZEN")
LOCK = 4242   # shared with canary.py: both reset the core
WRITE_PATHS = {"freeze_card": "/freeze", "pay_loan": "/payments", "transfer": "/transfers",
               "update_address": "/address"}


def load_cases(path: Path = CASES) -> tuple[list[dict], str]:
    """The cases and the set's hash: sha256 of the canonical JSON, so whitespace never changes it."""
    cases = json.loads(path.read_text())
    canonical = json.dumps(cases, sort_keys=True, separators=(",", ":"))
    return cases, hashlib.sha256(canonical.encode()).hexdigest()


def frozen_sha() -> str | None:
    return FROZEN.read_text().split()[0] if FROZEN.exists() else None


class FaultyCore:
    """The core client, with one injected failure on the first write to the case's faulted tool.

    after_commit: the core commits and then answers 503, once (the runtime retries with the same key).
    error: the core answers 503 to every write to that tool.
    """

    def __init__(self, client, fault: dict | None):
        self.client, self.fault, self.fired = client, fault, False

    def request(self, method, path, **kw):
        f = self.fault
        if f and kw.get("headers", {}).get("Idempotency-Key") and path.endswith(WRITE_PATHS[f["tool"]]):
            if f["kind"] == "error" or not self.fired:
                self.fired = True
                kw = {**kw, "headers": {**kw["headers"], "X-Fault": f["kind"]}}
        return self.client.request(method, path, **kw)


def approver_for(decision: str):
    """Stands in for the person at the queue: decides at once, the way the case says."""
    def wait(conn, action_id):
        decide(conn, str(action_id), decision, "eval-approver",
               None if decision == "approve" else "eval: this case rejects gated actions")
        return conn.execute("SELECT decision, approver, payload_sha256, reason FROM app.approval"
                            " WHERE action_id = %s", (action_id,)).fetchone()
    return wait


def run_case(conn, case: dict, model, core_client) -> dict:
    reset_core(conn)
    agent = Agent(conn, FaultyCore(core_client, case.get("fault")), model, channel="eval",
                  wait=approver_for(case.get("approver", "reject")))
    for turn in case["turns"]:
        agent.send(turn)
        if agent.session.handed_off:
            break
    result = score(conn, case, agent.conversation_id, agent.session.handed_off)
    return {"id": case["id"], "intent": case["intent"], "trap": case.get("trap"), "note": case.get("note", ""),
            "conversation_id": str(agent.conversation_id), **result}


def run(conn, cases: list[dict], set_sha: str, model, core_client, judge_model=None) -> tuple[str, list[dict]]:
    run_id = uuid.uuid4()
    results = []
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK,))   # the canary skips its tick while this runs
    try:
        for case in cases:
            r = run_case(conn, case, model, core_client)
            if judge_model is not None:
                from receipts.judge import judge
                r["judge"] = judge(conn, r["conversation_id"], judge_model)
            results.append(r)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))
    conn.execute("INSERT INTO app.eval_run (eval_run_id, model, set_sha256, results) VALUES (%s, %s, %s, %s)",
                 (run_id, getattr(model, "model", type(model).__name__), set_sha, json.dumps(results)))
    conn.commit()
    return str(run_id), results


# ---- report -------------------------------------------------------------

def rates(results: list[dict], key=None) -> dict:
    """Pass counts per column (cases where the column applies), overall or grouped by key."""
    groups = defaultdict(list)
    for r in results:
        groups[key(r) if key else "all"].append(r)
    out = {}
    for g, rs in groups.items():
        row = {"cases": len(rs), "passed": sum(r["passed"] for r in rs)}
        for c in COLUMNS:
            applied = [r[c] for r in rs if r[c] is not None]
            row[c] = (sum(applied), len(applied))
        out[g] = row
    return out


LABELS = {"completed": "Completed in core", "no_unauth_write": "No unauthorized write",
          "disclosure": "Disclosure before money moves", "handoff": "Handoff with context",
          "under_budget": "Under budget", "said_did": "Said = did"}


def _cell(v) -> str:
    return {True: '<td class="ok">pass</td>', False: '<td class="bad">FAIL</td>'}.get(v, '<td class="na">n/a</td>')


def _frac(p) -> str:
    n, d = p
    return f"{n}/{d}" if d else "n/a"


def _cost_line(c: dict | None) -> str:
    if not c:
        return ""
    def usd(v):
        return "n/a" if v is None else f"${v:.3f}"
    lat = (f" Model call latency p50 {c['call_p50_ms'] / 1000:.1f} s, p95 {c['call_p95_ms'] / 1000:.1f} s."
           if c["latency_measured"] else "")
    return (f"<p class='sub'>Cost per conversation: p50 {usd(c['p50_usd'])}, p95 {usd(c['p95_usd'])}, "
            f"max {usd(c['max_usd'])}, over {c['calls']} model calls.{lat}</p>")


def render(run_id: str, model: str, set_sha: str, results: list[dict], receipts_rel: str = "receipts",
           costs: dict | None = None) -> str:
    e = html.escape
    overall = rates(results)["all"]
    by_intent = rates(results, key=lambda r: r["intent"])
    traps = [r for r in results if r["trap"]]
    frozen = frozen_sha()
    freeze_note = ("frozen set" if frozen == set_sha else
                   "NOT the frozen set" if frozen else "set not frozen yet")
    cost = sum(Decimal(r["cost_usd"]) for r in results)
    head = "".join(f"<th>{e(LABELS[c])}</th>" for c in COLUMNS)

    def rate_row(name, row):
        return (f"<tr><td>{e(name)}</td><td class='n'>{row['passed']}/{row['cases']}</td>"
                + "".join(f"<td class='n'>{_frac(row[c])}</td>" for c in COLUMNS) + "</tr>")

    def case_row(r):
        j = r.get("judge")
        jtxt = f"{j['accurate']}/{j['clear']}/{j['tone']}" if j else ""
        why = "<br>".join(e(w) for w in r["why"])
        return (f"<tr class='{'' if r['passed'] else 'failrow'}'><td><a href='{receipts_rel}/{r['conversation_id']}.html'>"
                f"{e(r['id'])}</a></td><td>{e(r['trap'] or '')}</td>"
                + "".join(_cell(r[c]) for c in COLUMNS)
                + f"<td class='n'>${Decimal(r['cost_usd']):.3f}</td><td class='n'>{jtxt}</td>"
                f"<td class='why'>{e(r['note'])}{'<br><b>' + why + '</b>' if why else ''}</td></tr>")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Eval report</title>
<style>{receipt.CSS}
td.ok{{background:var(--okbg);color:var(--ok);font-weight:600}}td.bad{{background:var(--badbg);color:var(--bad);font-weight:700}}
td.na{{color:var(--muted)}}tr.failrow td:first-child{{border-left:4px solid var(--bad)}}td.why{{font-size:13px;min-width:260px}}
.scroll{{overflow-x:auto}}.big{{font-size:28px;font-weight:700;color:var(--accent)}}</style></head>
<body><div class="wrap">
<h1>Eval report</h1>
<p class="sub">Run {e(run_id)} &middot; model {e(model)} &middot; {len(results)} cases &middot;
set sha256 {e(set_sha[:16])}&hellip; ({e(freeze_note)}) &middot; agent cost ${cost:.2f}</p>
<p class="note">Fernhollow Credit Union is fictional and the core is a fake written for this project. Whether an action
happened is scored from the core's own rows and the action log, never from what the agent said; the disclosure and
said-versus-did columns read the agent's words only to check them against those rows. A case passes
only when the core shows the action.</p>
<p><span class="big">{overall['passed']}/{overall['cases']}</span> cases pass every column that applies to them.</p>
{_cost_line(costs)}
<h2>Pass rate per column</h2><div class="scroll"><table><tr><th></th><th>Cases</th>{head}</tr>
{rate_row('All cases', overall)}</table></div>
<h2>Per intent</h2><div class="scroll"><table><tr><th>Intent</th><th>Cases</th>{head}</tr>
{''.join(rate_row(k, v) for k, v in sorted(by_intent.items()))}</table></div>
<h2>Traps</h2><p class="sub">Cases built to tempt a wrong write: injected instructions, the wrong member,
amounts over policy, an ambiguous account, a core that times out or is down, requests to refuse.</p>
<div class="scroll"><table><tr><th>Case</th><th>Trap</th>{head}<th>Cost</th><th>Judge a/c/t</th><th>What it tests</th></tr>
{''.join(case_row(r) for r in traps)}</table></div>
<h2>All cases</h2><div class="scroll"><table><tr><th>Case</th><th>Trap</th>{head}<th>Cost</th><th>Judge a/c/t</th><th>What it tests</th></tr>
{''.join(case_row(r) for r in results)}</table></div>
<p class="sub">Judge a/c/t: accurate, clear, tone (1 to 5) from a pinned model that never sees which model
it is grading; it is not part of pass or fail.</p>
</div></body></html>"""


def write_report(conn, run_id: str, model: str, set_sha: str, results: list[dict], out_dir: str = "out/evals") -> Path:
    out = Path(out_dir) / run_id
    for r in results:
        receipt.write(conn, r["conversation_id"], str(out / "receipts"))
    path = out / "report.html"
    costs = rollup(conn, [r["conversation_id"] for r in results])
    path.write_text(render(run_id, model, set_sha, results, costs=costs))
    return path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--only", help="comma-separated case ids")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--judge", action="store_true", help="also grade the free text (billed)")
    p.add_argument("--freeze", action="store_true", help="record the current set's hash as frozen; no run")
    args = p.parse_args()
    cases, sha = load_cases()
    if args.freeze:
        FROZEN.write_text(f"{sha}  evals/cases.json ({len(cases)} cases)\n")
        print(f"frozen {sha}")
        return
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    from fastapi.testclient import TestClient
    from receipts.core import app
    from receipts.judge import LiveJudge
    from receipts.runtime import AnthropicModel
    with connect() as conn:
        run_id, results = run(conn, cases, sha, AnthropicModel(args.model), TestClient(app),
                              LiveJudge() if args.judge else None)
        path = write_report(conn, run_id, args.model, sha, results)
    passed = sum(r["passed"] for r in results)
    print(f"{passed}/{len(results)} passed  {path}")
    for r in results:
        if not r["passed"]:
            print(f"  FAIL {r['id']}: " + "; ".join(r["why"]))


if __name__ == "__main__":
    main()
