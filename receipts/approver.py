"""The approval queue: a person approves or rejects the exact payload of a gated action.

Single-user demo console, no sign-in. A decision is one row per action (UNIQUE), so
approving twice records nothing the second time.

    uvicorn receipts.approver:app --host 0.0.0.0 --port 8101
"""
import html
import json

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from receipts.db import connect
from receipts.policy import payload_sha256

PENDING_SQL = """
SELECT a.action_id, a.conversation_id, a.tool, a.args, a.policy_rule_id, a.created_at
  FROM app.action a
 WHERE a.policy_class = 'gated'
   AND NOT EXISTS (SELECT 1 FROM app.approval p WHERE p.action_id = a.action_id)
   AND NOT EXISTS (SELECT 1 FROM app.action_event e WHERE e.action_id = a.action_id
                    AND e.kind IN ('expired', 'refused'))
 ORDER BY a.created_at"""


def decide(conn, action_id: str, decision: str, approver: str, reason: str | None = None) -> bool:
    """Record a decision. Returns False if the action was already decided (nothing written)."""
    if decision not in ("approve", "reject"):
        raise ValueError(decision)
    with conn.transaction():
        action = conn.execute("SELECT tool, args, policy_class FROM app.action WHERE action_id = %s",
                              (action_id,)).fetchone()
        if action is None or action["policy_class"] != "gated":
            raise LookupError(f"no gated action {action_id}")
        row = conn.execute(
            "INSERT INTO app.approval (action_id, decision, approver, payload_sha256, reason)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (action_id) DO NOTHING RETURNING approval_id",
            (action_id, decision, approver, payload_sha256(action["tool"], action["args"]), reason)).fetchone()
        if row is None:
            return False
        conn.execute("INSERT INTO app.action_event (action_id, kind, detail) VALUES (%s, %s, %s)",
                     (action_id, "approved" if decision == "approve" else "rejected",
                      json.dumps({"approver": approver, "reason": reason})))
    conn.commit()   # the waiting runtime reads this from its own connection
    return True


app = FastAPI(title="Fernhollow approval queue (demo)")

CSS = """:root{--bg:#f7f6f2;--fg:#1d2321;--muted:#5d6763;--card:#fff;--line:#d9ddd8;--accent:#2f5d50;--code:#f0f2ee;
--ok:#1f6b45;--bad:#8a2b22}@media (prefers-color-scheme:dark){:root{--bg:#151918;--fg:#e4e8e5;--muted:#9aa5a0;
--card:#1e2422;--line:#33403b;--accent:#8cc3b0;--code:#252c29;--ok:#8fd4aa;--bad:#f0a49a}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,Helvetica,Arial,sans-serif}
.wrap{max-width:820px;margin:0 auto}h1{font-size:21px;margin:0}.sub{color:var(--muted);font-size:13px}
.item{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:12px 0}
pre{background:var(--code);border-radius:6px;padding:8px;white-space:pre-wrap;word-break:break-all;font-size:13px}
button{font-size:15px;border:0;border-radius:6px;padding:7px 16px;color:#fff;cursor:pointer;margin-right:8px}
.ok{background:var(--ok)}.bad{background:var(--bad)}input{font-size:14px;padding:5px;margin:0 8px 8px 0}
td,th{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;font-size:14px}table{border-collapse:collapse;width:100%}"""


@app.get("/", response_class=HTMLResponse)
def queue():
    with connect() as conn:
        pending = conn.execute(PENDING_SQL).fetchall()
        recent = conn.execute(
            "SELECT p.decided_at, p.decision, p.approver, a.tool, a.args FROM app.approval p"
            " JOIN app.action a USING (action_id) ORDER BY p.decided_at DESC LIMIT 10").fetchall()
    e = lambda s: html.escape(str(s))  # noqa: E731
    items = "".join(f"""<div class="item"><b>{e(a['tool'])}</b> <span class="sub">rule {e(a['policy_rule_id'])} ·
conversation {e(str(a['conversation_id'])[:8])} · requested {e(a['created_at']):.19}</span>
<pre>{e(json.dumps(a['args'], indent=2))}</pre>
<div class="sub">payload sha256 {e(payload_sha256(a['tool'], a['args']))}</div>
<form method="post" action="/actions/{a['action_id']}/decide" style="margin-top:8px">
<input name="approver" value="owen" size="10" aria-label="approver"><input name="reason" placeholder="reason (optional)" size="28">
<br><button class="ok" name="decision" value="approve">Approve</button><button class="bad" name="decision" value="reject">Reject</button>
</form></div>""" for a in pending) or '<p class="sub">Nothing waiting.</p>'
    rows = "".join(f"<tr><td>{e(r['decided_at']):.19}</td><td>{e(r['decision'])}</td><td>{e(r['approver'])}</td>"
                   f"<td>{e(r['tool'])}</td><td>{e(json.dumps(r['args']))}</td></tr>" for r in recent)
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{'' if pending else '<meta http-equiv="refresh" content="3">'}<title>Approval queue</title><style>{CSS}</style></head><body><div class="wrap">
<h1>Approval queue</h1><p class="sub">Fernhollow Credit Union (fictional) · single-user demo console, no sign-in · {'reload to check for new requests' if pending else 'checks for new requests every 3 seconds'}</p>
{items}<h2 style="font-size:16px">Recent decisions</h2><table><tr><th>When</th><th>Decision</th><th>By</th><th>Tool</th><th>Payload</th></tr>{rows}</table>
</div></body></html>"""


@app.post("/actions/{action_id}/decide")
def decide_form(action_id: str, decision: str = Form(...), approver: str = Form(...), reason: str = Form("")):
    with connect() as conn:
        try:
            decide(conn, action_id, decision, approver.strip() or "unknown", reason.strip() or None)
        except (LookupError, ValueError) as err:
            raise HTTPException(404, str(err))
    return RedirectResponse("/", status_code=303)
