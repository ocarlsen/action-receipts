"""Render one conversation's receipt: the audit view.

What the agent said, every tool call with its inputs and the core's answer, the policy class
and event history of each action, and what the conversation cost. Built from database rows only.

    python -m receipts.receipt <conversation_id> [--out out/receipts]
"""
import argparse
import html
import json
from decimal import Decimal
from pathlib import Path

from receipts.db import connect

CSS = """
:root{--bg:#f7f6f2;--fg:#1d2321;--muted:#5d6763;--card:#fff;--line:#d9ddd8;--accent:#2f5d50;
--ok:#1f6b45;--okbg:#e3f1e8;--bad:#8a2b22;--badbg:#f7e3e0;--warn:#7a5a12;--warnbg:#f6edd5;--code:#f0f2ee}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151918;--fg:#e4e8e5;--muted:#9aa5a0;
--card:#1e2422;--line:#33403b;--accent:#8cc3b0;--ok:#8fd4aa;--okbg:#1d3328;--bad:#f0a49a;--badbg:#3a2120;
--warn:#e8c97a;--warnbg:#3a311a;--code:#252c29}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
.wrap{max-width:920px;margin:0 auto}h1{font-size:21px;margin:0 0 2px}h2{font-size:16px;margin:26px 0 8px;
color:var(--accent)}.sub{color:var(--muted);font-size:13px;margin:0 0 14px}
.note{background:var(--warnbg);color:var(--warn);border-radius:6px;padding:8px 12px;font-size:13px}
.msg{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 12px;margin:8px 0}
.msg .who{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.member{border-left:4px solid var(--accent)}
.act{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 12px;margin:8px 0}
.act .head{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.tool{font-weight:600;font-family:ui-monospace,Menlo,monospace}
.tag{font-size:12px;font-weight:600;border-radius:4px;padding:1px 7px}
.executed{background:var(--okbg);color:var(--ok)}.refused,.failed{background:var(--badbg);color:var(--bad)}
.requested,.approved,.expired{background:var(--warnbg);color:var(--warn)}.rejected{background:var(--badbg);color:var(--bad)}.cls{background:var(--code);color:var(--muted)}
details summary{cursor:pointer;color:var(--muted);font-size:13px;margin-top:6px}
pre{background:var(--code);border-radius:6px;padding:8px;overflow-x:auto;font-size:12.5px;margin:6px 0 0;white-space:pre-wrap;word-break:break-word}
table{border-collapse:collapse;width:100%;background:var(--card);font-size:14px}
td,th{border:1px solid var(--line);padding:6px 8px;text-align:left}th{background:var(--code)}
td.n{text-align:right;font-variant-numeric:tabular-nums}
"""


def load(conn, conversation_id: str) -> dict:
    conv = conn.execute("SELECT * FROM app.conversation WHERE conversation_id = %s", (conversation_id,)).fetchone()
    if conv is None:
        raise SystemExit(f"no conversation {conversation_id}")
    turns = conn.execute("SELECT seq, role, content, at FROM app.turn WHERE conversation_id = %s ORDER BY seq",
                         (conversation_id,)).fetchall()
    actions = conn.execute(
        "SELECT a.action_id, a.tool, a.args, a.policy_class, a.policy_rule_id, a.created_at,"
        " (SELECT json_agg(json_build_object('kind', e.kind, 'detail', e.detail, 'at', e.at) ORDER BY e.event_id)"
        "    FROM app.action_event e WHERE e.action_id = a.action_id) AS events"
        " FROM app.action a WHERE a.conversation_id = %s ORDER BY a.created_at, a.action_id",
        (conversation_id,)).fetchall()
    ledger = conn.execute("SELECT * FROM app.ledger WHERE conversation_id = %s ORDER BY ledger_id",
                          (conversation_id,)).fetchall()
    checks = conn.execute("SELECT seq, tool, claim, verified, evidence FROM app.claim_check"
                          " WHERE conversation_id = %s ORDER BY check_id", (conversation_id,)).fetchall()
    return {"conversation": conv, "turns": turns, "actions": actions, "ledger": ledger, "checks": checks}


def _e(s) -> str:
    return html.escape(str(s))


def render(data: dict) -> str:
    conv, turns, actions, ledger = data["conversation"], data["turns"], data["actions"], data["ledger"]
    by_tool_use = {}
    for a in actions:
        first = (a["events"] or [{}])[0]
        by_tool_use[(first.get("detail") or {}).get("tool_use_id")] = a

    parts = []
    for t in turns:
        if t["role"] == "member":
            parts.append(f'<div class="msg member"><div class="who">Member</div>{_e(t["content"])}</div>')
        elif t["role"] == "runtime":
            parts.append(f'<div class="msg"><div class="who">Agent (fixed runtime message)</div>{_e(t["content"])}</div>')
        elif t["role"] == "assistant":
            for b in t["content"]:
                if b.get("type") == "text" and b["text"].strip():
                    parts.append(f'<div class="msg"><div class="who">Agent</div>{_e(b["text"])}</div>')
                elif b.get("type") == "tool_use":
                    a = by_tool_use.get(b["id"])
                    parts.append(_action(a) if a else f'<div class="act">tool call {_e(b["name"])} (no action row)</div>')

    spent = sum((Decimal(str(r["cost_usd"])) for r in ledger), Decimal(0))
    rows = "".join(
        f'<tr><td>{_e(r["model"])}</td><td class="n">{r["input_tokens"]:,}</td><td class="n">{r["cache_read_tokens"]:,}</td>'
        f'<td class="n">{r["cache_write_tokens"]:,}</td><td class="n">{r["output_tokens"]:,}</td>'
        f'<td class="n">${Decimal(str(r["cost_usd"])):.4f}</td></tr>' for r in ledger)
    checks = data["checks"]
    if checks:
        check_rows = "".join(
            f'<tr><td><span class="tag {"executed" if c["verified"] else "refused"}">'
            f'{"verified" if c["verified"] else "HELD, not sent" if c["evidence"].get("held_before_sending") else "NOT IN CORE"}'
            f'</span></td><td>{_e(c["tool"])}</td>'
            f'<td>{_e(c["claim"])}</td></tr>' for c in checks)
        said = (f'<table><tr><th>Core</th><th>Action</th><th>What the agent said</th></tr>{check_rows}</table>')
    else:
        said = '<p class="sub">No completed actions claimed, or not checked yet.</p>'
    writes = [a for a in actions if a["policy_class"] != "read"]
    done = sum(1 for a in writes if _final(a) == "executed")

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Conversation receipt</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>Conversation receipt</h1>
<p class="sub">Fernhollow Credit Union (fictional) · conversation {_e(conv["conversation_id"])} ·
started {_e(conv["started_at"]):.19} · model {_e(conv["model"])}</p>
<p class="note">Synthetic demo. The credit union, the member and the core are all invented.</p>
<h2>Summary</h2>
<table><tr><th>Tool calls</th><th>Writes executed</th><th>Writes refused or failed</th><th>Model calls</th><th>Cost</th></tr>
<tr><td class="n">{len(actions)}</td><td class="n">{done}</td><td class="n">{len(writes) - done}</td>
<td class="n">{len(ledger)}</td><td class="n">${spent:.4f}</td></tr></table>
<h2>Said versus did</h2>
<p class="sub">Each completed action the agent claimed, checked against what the core recorded.</p>
{said}
<h2>Conversation and actions</h2>
{''.join(parts)}
<h2>Cost ledger</h2>
<table><tr><th>Model</th><th>Input</th><th>Cache read</th><th>Cache write</th><th>Output</th><th>Cost</th></tr>{rows}</table>
</div></body></html>"""


def _final(a) -> str:
    return (a["events"] or [{"kind": "requested"}])[-1]["kind"]


def _action(a) -> str:
    final = _final(a)
    result = ""
    for e in a["events"] or []:
        if e["kind"] in ("executed", "refused", "failed", "expired"):
            result = json.dumps(e["detail"], indent=2, default=str)
    history = " → ".join(
        e["kind"] + (f' by {e["detail"].get("approver")}' if e["kind"] in ("approved", "rejected") else "")
        for e in a["events"] or [])
    return (f'<div class="act"><div class="head"><span class="tool">{_e(a["tool"])}</span>'
            f'<span class="tag {final}">{final}</span><span class="tag cls">policy: {_e(a["policy_class"])}'
            f'{" · " + _e(a["policy_rule_id"]) if a["policy_rule_id"] else ""}</span>'
            f'<span class="sub" style="margin:0">{_e(history)}</span></div>'
            f'<pre>{_e(json.dumps(a["args"], indent=2))}</pre>'
            + (f'<details><summary>Core response</summary><pre>{_e(result)}</pre></details>'
               if a["policy_class"] == "read" else f'<pre>{_e(result)}</pre>')
            + '</div>')


def write(conn, conversation_id: str, out_dir: str = "out/receipts") -> Path:
    path = Path(out_dir) / f"{conversation_id}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(load(conn, conversation_id)))
    return path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("conversation_id")
    p.add_argument("--out", default="out/receipts")
    args = p.parse_args()
    with connect() as conn:
        print(write(conn, args.conversation_id, args.out))


if __name__ == "__main__":
    main()
