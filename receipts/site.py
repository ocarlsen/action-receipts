"""Build the static site: an index, a dashboard, one eval report with its receipts, and the benchmark.

Everything is rendered from rows into out/site, then scanned for anything that must not leave
this machine (local paths, keys, connection strings, private addresses). A finding fails the
build and leaves no site. It pushes nothing; publishing is a separate, manual step.

    python -m receipts.site --eval-run <id> --benchmark <base_id> <cand_id> --showcase pay-02 [--domain x.y]
"""
import argparse
import html
import re
import shutil
from pathlib import Path

from receipts import receipt
from receipts.benchmark import load_run, verdict
from receipts.costs import rollup
from receipts.db import connect
from receipts.evalrun import frozen_sha

OUT = Path("out/site")
NOINDEX = '<meta name="robots" content="noindex,nofollow">'
FORBIDDEN = [r"/Users/", r"sk-ant", r"postgres(?:ql)?://", r"ANTHROPIC_API_KEY", r"\b100\.\d+\.\d+\.\d+\b",
             r"@gmail\.com", r"\.env\b"]
LEAK_TERMS_FILE = Path(__file__).resolve().parent.parent / ".leak-terms"


def _extra_leak_patterns(path: Path = LEAK_TERMS_FILE) -> list[str]:
    """Optional, gitignored file of extra private regex patterns, one per line.

    Blank lines and lines starting with # are ignored. A line that fails to compile is a loud
    failure, never a silent skip: it would mean the leak scan quietly stopped covering it.
    """
    if not path.exists():
        return []
    patterns = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            re.compile(line)
        except re.error as e:
            raise SystemExit(f"{path}:{lineno}: invalid regex {line!r}: {e}")
        patterns.append(line)
    return patterns

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{noindex}<title>{title}</title>
<style>{css}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;margin:12px 0}}
.panel{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}}
.panel h3{{margin:0 0 4px;font-size:15px}}.panel p{{margin:0;font-size:14px;color:var(--muted)}}
.big{{font-size:26px;font-weight:700;color:var(--accent)}}.ticks{{display:flex;flex-wrap:wrap;gap:4px;margin:8px 0}}
.tk{{width:18px;height:18px;border-radius:4px;background:var(--ok)}}.tk.red{{background:var(--bad)}}
a{{color:var(--accent)}}pre.alert{{border-left:4px solid var(--bad)}}
.watch a{{display:inline-block;background:var(--accent);color:var(--card);text-decoration:none;font-weight:600;padding:8px 14px;border-radius:6px}}nav.top a{{margin-right:12px}}
</style></head><body><div class="wrap">
{nav}
{body}</div></body></html>"""
NAV_CSS = "nav.top a{margin-right:12px}a{color:var(--accent)}"


def nav(showcase: str, prefix: str = "") -> str:
    """The top navigation, with links relative to a page `prefix` (e.g. "../") below the site root."""
    links = [("index.html", "Overview"), ("dashboard.html", "Dashboard"), ("evals/report.html", "Eval report"),
             ("benchmark.html", "Benchmark"), (showcase, "A receipt")]
    return '<nav class="top sub">' + "".join(f'<a href="{prefix}{h}">{t}</a>' for h, t in links) + "</nav>"


def page(title: str, body: str, showcase: str) -> str:
    return PAGE.format(noindex=NOINDEX, title=html.escape(title), css=receipt.CSS, body=body, nav=nav(showcase))


def index_body(ev: dict, bench_line: str, tour_secs: float | None = None) -> str:
    watch = ""
    if tour_secs:   # the tour is a doorway from here, not a nav item (the film itself walks the nav)
        m, s = divmod(round(tour_secs), 60)
        watch = f'<p class="watch"><a href="tour.html">&#9654; Watch the tour ({m}:{s:02d}, no sound)</a></p>'
    return f"""<h1>Every action has a receipt</h1>
<p class="sub">A member-service agent for a fictional credit union that completes core-banking actions
(freeze a card, pay a loan, move money, change an address) and proves each one.</p>
{watch}
<p class="note">Everything here is synthetic. Fernhollow Credit Union, its members and their accounts are
invented, and the "core" is a small fake written for this project; it does not model any vendor's core.
Nothing here is a compliance claim.</p>
<div class="grid">
<div class="panel"><h3>It acts, it does not narrate</h3><p>The model only asks for a tool. Code checks each call
against a policy first; no matching rule means refused. Larger payments and every address change wait for a
person, who approves the exact payload.</p></div>
<div class="panel"><h3>It proves what it did</h3><p>Every write carries an idempotency key and lands as a row,
and the runtime reads it back from the core before the member hears "done".
The <a href="{ev['showcase']}">receipt</a> shows what the agent said, what it called, which policy applied, who
approved and what it cost. A pattern checker reads every reply before it is sent and holds any "done" the core cannot back.</p></div>
<div class="panel"><h3>Completion, scored on the core</h3><p>{ev['passed']} of {ev['cases']} scripted
conversations pass. Whether each action happened is scored from the core's own rows, never from what the
agent said. A case passes when the core shows the action, never because the member was kept from a person. <a href="evals/report.html">Eval report</a>.</p></div>
<div class="panel"><h3>A model change is a measured decision</h3><p>{html.escape(bench_line)}
<a href="benchmark.html">Benchmark</a>.</p></div>
</div>
<h2>What is real and what is not</h2>
<table><tr><th>Real</th><th>Not real</th></tr>
<tr><td>The agent loop, policy engine, approval gate, idempotent writes, audit rows, the eval set and scorer,
the cost ledger, the canary</td><td>The credit union, its members, the core banking system, the approver
(one person, no sign-in)</td></tr>
<tr><td>Model calls to pinned Claude models, with their real cost and latency</td><td>Any production traffic
or real money</td></tr></table>
<p class="sub">Written from scratch by Owen Carlsen, with Claude Code. Static export; the numbers
are as of the export.</p>"""


# The deliberate faults the canary can inject, in plain words: what was broken, which guard must catch it,
# and the line in the tick's reasons that shows that guard fired.
FAULTS = {
    "drop_write": ("The core lies", "The fake core was told to answer every write with \"done\" and save nothing.",
                   "The read-back: before the member hears \"done\", the runtime looks the write up in the core.",
                   "read-back failed"),
    "false_done": ("The model lies", "The canary's scripted model was told to say the $50 moved without ever asking "
                   "for the transfer.", "The live said-versus-did check: every reply is checked against the core "
                   "before it is sent.", "held a reply"),
}


def dashboard_body(conn, ev: dict, version: str | None) -> str:
    """Canary ticks from the code being shown only: a tick proves something about the version that ran it."""
    from receipts.canary import alert_text
    ticks = conn.execute(
        "SELECT ok, failures, at AT TIME ZONE 'America/Los_Angeles' AS at FROM app.canary_tick"
        " WHERE code_version = %s ORDER BY tick_id DESC LIMIT 24", (version,)).fetchall()[::-1]
    dots = "".join(f"<span class='tk{'' if t['ok'] else ' red'}' title='{t['at']:%b %d %H:%M} PT'></span>" for t in ticks)
    latest = {}
    unexpected = []
    for t in ticks:
        inj = next((x["injected"] for x in t["failures"] if "injected" in x), None)
        if t["ok"]:
            continue
        if inj in FAULTS:
            latest[inj] = t
        else:
            unexpected.append(t)
    cards = []
    for key, (title, broke, guard, sign) in FAULTS.items():
        t = latest.get(key)
        if not t:
            continue
        why = [x["why"] for x in t["failures"] if "why" in x]
        cols = [x["column"] for x in t["failures"] if "column" in x]
        fired = any(sign in w for w in why)
        cards.append(f"""<div class="panel"><h3>{html.escape(title)}</h3>
<p>{html.escape(broke)}</p><p><b>Must be caught by:</b> {html.escape(guard)}</p>
<p><b>{'Caught' if fired else 'NOT caught'}</b> at {t['at']:%b %d %H:%M} PT; the member went to a person, and the tick
turned red with one alert, word for word:</p><pre class="alert">{html.escape(alert_text(cols, why))}</pre></div>""")
    proof = ("<h2>Both guards, proven red</h2><p class='sub'>Two deliberate faults, injected one at a time on this code. "
             "Each must be caught by its own guard before the member is told anything false.</p>"
             + "".join(cards)) if cards else ""
    odd = ("<h2>Unexpected red ticks</h2><ul>" + "".join(f"<li>{t['at']:%b %d %H:%M} PT: "
           + html.escape(', '.join(x['column'] for x in t['failures'] if 'column' in x)) + "</li>" for t in unexpected)
           + "</ul>") if unexpected else ""
    canary = f"""<h2>Canary: last {len(ticks)} ticks on this code ({html.escape(version or 'unknown')})</h2>
<p class="sub">One scripted conversation per tick through the real runtime, policy, core and checker, hourly. Silent
when clean; one alert on the first red tick. Ticks from earlier code are not shown: a tick only proves something
about the code that ran it.</p>
<div class="ticks">{dots}</div>
{odd}{proof}"""
    c = ev["costs"]
    return f"""<h1>Dashboard</h1>
<p class="sub">As of the export. Model {html.escape(ev['model'])}, frozen eval set.</p>
<div class="grid">
<div class="panel"><h3>Eval pass rate</h3><div class="big">{ev['passed']}/{ev['cases']}</div><p>cases pass every
column that applies</p></div>
<div class="panel"><h3>Cost per conversation</h3><div class="big">${c['p50_usd']:.3f}</div><p>p50; p95
${c['p95_usd']:.3f}; whole set ${c['total_usd']:.2f}</p></div>
<div class="panel"><h3>Model call latency</h3><div class="big">{(c['call_p50_ms'] or 0) / 1000:.1f} s</div><p>p50;
p95 {(c['call_p95_ms'] or 0) / 1000:.1f} s</p></div>
<div class="panel"><h3>Budget guard</h3><div class="big">$0.20</div><p>per conversation; at that point the
runtime hands the member to a person with the context</p></div>
</div>
{canary}"""


def tour_body(seconds: float) -> str:
    m, s = divmod(round(seconds), 60)
    return f"""<h1>A walkthrough</h1>
<p class="sub">{m}:{s:02d}, no sound. The same pages you can open from the top bar.</p>
<video src="tour.mp4" width="1440" height="1012" controls preload="metadata" playsinline
style="display:block;width:100%;height:auto;aspect-ratio:1440/1012;border:1px solid var(--line);border-radius:8px"></video>"""


def leak_scan(root: Path) -> list[str]:
    findings = []
    patterns = FORBIDDEN + _extra_leak_patterns()
    for f in root.rglob("*"):
        if f.suffix in (".html", ".txt", ".json", ".css", ".js", ".md") or f.name in ("CNAME", "robots.txt"):
            text = f.read_text(errors="replace")
            for pat in patterns:
                for m in re.finditer(pat, text):
                    findings.append(f"{f.relative_to(root)}: {pat} matched {text[max(0, m.start() - 20):m.end() + 20]!r}")
    return findings


def build(conn, eval_run: str, base: str, cand: str, showcase: str, domain: str | None,
          tour: Path | None = None, out: Path = OUT) -> Path:
    tmp = out.with_name(out.name + ".building")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    run = load_run(conn, eval_run)
    if run["set_sha256"] != frozen_sha():
        raise SystemExit(f"eval run {eval_run} is not on the frozen set")
    src = Path("out/evals") / eval_run
    shutil.copytree(src, tmp / "evals")
    case = next((r for r in run["results"] if r["id"] == showcase), None)
    if case is None:
        raise SystemExit(f"no case {showcase} in run {eval_run}")
    show = f"evals/receipts/{case['conversation_id']}.html"
    b, c = load_run(conn, base), load_run(conn, cand)
    shutil.copy(Path("out/benchmarks") / f"{base[:8]}_vs_{cand[:8]}.html", tmp / "benchmark.html")
    ev = {"model": run["model"], "passed": run["rates"]["passed"], "cases": run["rates"]["cases"],
          "costs": rollup(conn, [r["conversation_id"] for r in run["results"]]), "showcase": show}
    secs = None
    if tour and tour.exists():   # tour/tour.ts records it from the built site
        import subprocess
        secs = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(tour)], capture_output=True, text=True, check=True).stdout)
        shutil.copy(tour, tmp / "tour.mp4")
        (tmp / "tour.html").write_text(page("Tour", tour_body(secs), show))
    (tmp / "index.html").write_text(page("Every action has a receipt", index_body(ev, verdict(b, c)[1], secs), show))
    from receipts.canary import code_version
    version = (code_version() or "").removesuffix("-dirty")   # ticks on uncommitted code never count
    (tmp / "dashboard.html").write_text(page("Dashboard", dashboard_body(conn, ev, version), show))
    for f in tmp.rglob("*.html"):   # copied pages get the noindex tag, the top nav and the link colour too
        t = f.read_text()
        if NOINDEX not in t:
            t = t.replace("<head>", "<head>" + NOINDEX, 1)
        if '<nav class="top' not in t:
            prefix = "../" * (len(f.relative_to(tmp).parts) - 1)
            t = t.replace("</style>", NAV_CSS + "</style>", 1).replace('<div class="wrap">', '<div class="wrap">' + nav(show, prefix), 1)
        f.write_text(t)
    (tmp / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    (tmp / ".nojekyll").write_text("")
    if domain:   # a publish build: the deploy-only repo gets a CNAME and says what it is
        (tmp / "CNAME").write_text(domain + "\n")
        (tmp / "README.md").write_text(
            "# Every action has a receipt (static demo)\n\nDeploy-only repository: the static export of a demo "
            "member-service agent for a fictional credit union (Fernhollow Credit Union; members, accounts and the "
            "core banking system are all synthetic). The source is kept separately. Nothing here is a compliance "
            "claim.\n")
    findings = leak_scan(tmp)
    if findings:
        shutil.rmtree(tmp)
        raise SystemExit("leak scan failed, no site written:\n" + "\n".join(findings))
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--eval-run", required=True)
    p.add_argument("--benchmark", nargs=2, required=True, metavar=("BASE", "CAND"))
    p.add_argument("--showcase", default="pay-02")
    p.add_argument("--domain")
    p.add_argument("--tour", type=Path, default=Path("out/tour/tour.mp4"), help="the walkthrough film")
    args = p.parse_args()
    with connect() as conn:
        print(build(conn, args.eval_run, *args.benchmark, args.showcase, args.domain, args.tour))


if __name__ == "__main__":
    main()
