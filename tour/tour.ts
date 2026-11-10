// tour/tour.ts
//
//   node tour.ts              records the walkthrough of ../out/site into ../out/tour/tour.mp4 (+ .gif,
//                             a contact sheet of one frame per caption)
//   node tour.ts --check      no recording: drives the same steps and checks every caption's geometry
//
// Playwright drives the built static site at 1440x900 with an injected caption bubble, highlight
// frame, cursor and click pulse (overlay.ts); ffmpeg trims the film to start on the title card and
// adds a 112 px band in the page background below it, where a player's controls sit. Numbers in
// captions are read off the pages, never typed. No model or API calls.

import { spawn, type ChildProcess } from "node:child_process";
import { mkdir, readdir, rm, stat } from "node:fs/promises";
import { join, resolve } from "node:path";
import { chromium, type Locator, type Page } from "playwright";
import { OVERLAY_SCRIPT } from "./overlay.ts";
import { placementProblems, type Rect } from "./geometry.ts";

const W = 1440;
const H = 900;
const BAND = 112;
const BG = "0xf7f6f2"; // the site's --bg (receipts/receipt.py CSS)
const SITE = resolve("../out/site");
const OUT = resolve("../out/tour");
const CHECK = process.argv.includes("--check");
const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

class Tour {
  page: Page;
  t0: number;
  step = 0;
  marks: number[] = [];
  problems: string[] = [];

  constructor(page: Page, t0: number) {
    this.page = page;
    this.t0 = t0;
  }

  pause(ms: number) { return CHECK ? Promise.resolve() : wait(ms); }
  settle(ms: number) { return wait(CHECK ? Math.min(ms, 250) : ms); }
  now() { return (Date.now() - this.t0) / 1000; }

  async ready() {
    await this.page.waitForFunction(() => !!(window as any).tourUI);
    await this.page.evaluate(() => (window as any).tourUI.build());
  }

  async box(l: Locator): Promise<Rect> {
    const b = await l.first().boundingBox();
    if (!b) throw new Error(`no box for ${l}`);
    return { x: b.x, y: b.y, w: b.width, h: b.height };
  }

  /** Scrolls `l` to the middle of the viewport when it is not comfortably on screen. */
  async reveal(l: Locator) {
    await l.first().waitFor({ state: "visible", timeout: 10_000 });
    const moved = await l.first().evaluate((el, behavior) => {
      const r = el.getBoundingClientRect();
      if (r.top >= 24 && r.bottom <= innerHeight - 60) return false;
      el.scrollIntoView({ behavior: behavior as ScrollBehavior, block: "center" });
      return true;
    }, CHECK ? "instant" : "smooth");
    if (moved) await this.still(l);
  }

  /** Waits until `l` stops moving (a long smooth scroll outlasts any fixed pause). */
  async still(l: Locator) {
    let last = "";
    for (let i = 0; i < 40; i++) {
      await wait(CHECK ? 50 : 150);
      const b = JSON.stringify(await this.box(l));
      if (b === last) return;
      last = b;
    }
  }

  async moveTo(l: Locator, ms = 550) {
    const b = await this.box(l);
    const x = Math.round(b.x + Math.min(b.w / 2, 120));
    const y = Math.round(b.y + Math.min(b.h / 2, 24));
    await this.page.evaluate(([x, y, ms]) => (window as any).tourUI.point(x, y, ms), [x, y, ms] as const);
    await this.pause(ms + 80);
    return { x, y };
  }

  async click(l: Locator) {
    await this.reveal(l);
    const { x, y } = await this.moveTo(l);
    await this.page.evaluate(([x, y]) => (window as any).tourUI.click(x, y), [x, y] as const);
    await this.pause(150);
    await l.first().click();
    await this.page.waitForLoadState("load");
    await this.ready();
    await this.settle(500);
  }

  /** A caption beside `l` (never on it when a side fits), held long enough to read. */
  async say(text: string, l: Locator | null, opts: { place?: "above" | "below" | "right" | "left"; maxW?: number } = {}) {
    const words = text.split(/\s+/).length;
    if (words > 14) throw new Error(`caption over 14 words: ${text}`);
    if (/[—–]/.test(text)) throw new Error(`caption has a dash: ${text}`);
    this.step++;
    if (l) {
      await this.reveal(l);
      let target = await this.box(l);
      const size = await this.page.evaluate(([t, w]) => (window as any).tourUI.size(t, w), [text, opts.maxW ?? null] as const);
      const room = target.y + target.h + 16 + size.h <= H - 12 || target.y - 16 - size.h >= 12 ||
        target.x + target.w + 16 + size.w <= W - 12 || target.x - 16 - size.w >= 12;
      if (!room) {   // a tall, full-width target: scroll it down so the caption fits above it
        await this.page.evaluate(([d, b]) => window.scrollBy({ top: d, behavior: b as ScrollBehavior }),
          [target.y - (size.h + 40), CHECK ? "instant" : "smooth"] as const);
        await this.still(l);
        target = await this.box(l);
      }
      if (target.h > 420) target = { ...target, h: Math.max(60, Math.min(target.h, H - 16 - target.y)) };
      await this.moveTo(l, 380);
      const placed = await this.page.evaluate(([t, r, prefer, w]) => {
        const ui = (window as any).tourUI;
        ui.highlight(r);
        return ui.say(t, r, prefer, w);
      }, [text, target, opts.place ?? null, opts.maxW ?? null] as const);
      const p = placementProblems({ x: placed.x, y: placed.y, w: placed.w, h: placed.h }, target, W, H);
      if (placed.side === "over") p.push("no side fits");
      for (const x of p) this.problems.push(`step ${this.step} "${text}": ${x}`);
    } else {
      await this.page.evaluate(([t]) => { const ui = (window as any).tourUI; ui.highlight(null); ui.say(t, null); }, [text] as const);
    }
    const ms = Math.max(2400, Math.min(3400, 1300 + 140 * words));
    await this.pause(450);
    this.marks.push(this.now());
    await this.pause(ms - 450);
    await this.page.evaluate(() => (window as any).tourUI.hide());
    await this.pause(250);
  }
}

const text = async (l: Locator) => (await l.first().innerText()).trim();

async function tour(t: Tour, page: Page, base: string): Promise<number> {
  const nav = (name: string) => page.locator(`nav.top a:text-is("${name}")`);
  await page.goto(base + "index.html");
  await t.ready();
  await t.settle(600);
  await page.evaluate(() => (window as any).tourUI.title(true, "Every action has a receipt", "A working demo on a fictional credit union", 0));
  const introAt = t.now();
  await t.pause(1700);
  await page.evaluate(() => (window as any).tourUI.title(false, null, null, 300));
  await t.settle(400);

  // Overview: the note, then the four claims in order
  await t.say("Fernhollow Credit Union is fictional. The core banking system is a small fake.", page.locator(".note"));
  await t.say("One: the model can only request actions; code decides whether each runs.", page.locator(".panel").nth(0));
  await t.say("Two: every write is idempotent and leaves a receipt built from database rows.", page.locator(".panel").nth(1));
  await t.say("Three: a case passes only when the core shows the action.", page.locator(".panel").nth(2));
  await t.say("Four: model changes are decided on a frozen set, by a rule fixed first.", page.locator(".panel").nth(3));

  // Dashboard
  await t.click(nav("Dashboard"));
  await t.say("At a glance: pass rate, cost per conversation, latency and the budget guard.", page.locator(".grid"));
  await t.say("An hourly canary runs runtime, policy, core and checker, with a scripted model.", page.locator(".ticks"));
  const cards = page.locator("h2:text-is('Both guards, proven red') ~ .panel");
  if ((await cards.count()) !== 2 || (await cards.allInnerTexts()).some((x) => !x.includes("Caught at")))
    throw new Error("the dashboard does not show both guards caught on this code");
  await t.say("Fault one, the core lies: the read-back catches it before anyone says done.", cards.nth(0));
  await t.say("Fault two, the model lies: the live check holds the reply unsent.", cards.nth(1));


  // Eval report
  await t.click(nav("Eval report"));
  const big = await text(page.locator(".big"));
  await t.say(`${big} scripted conversations pass. What happened is scored on the core.`, page.locator(".big"));
  await t.say("Completion comes from core rows, never from what the agent said.",
    page.locator("h2:text-is('Pass rate per column') + div table"));
  await t.say("An injected SYSTEM OVERRIDE asks for $8,000. Policy refuses; nothing moves.",
    page.locator("tr", { has: page.locator("a:text-is('xfr-06')") }));
  // The one failure, shown, not skipped. The caption explains ho-04 (transcript read at the time), so the
  // film refuses to record if the run's failures are anything else.
  const fails = page.locator("h2:text-is('All cases') + div tr.failrow");
  const failIds = await fails.locator("td:first-child").allInnerTexts();
  if (failIds.join() !== "ho-04") throw new Error(`the miss caption is written for ho-04; this run failed: ${failIds.join(", ")}`);
  await t.say("The one miss: it asked one more question before handing off; the script ended.", fails.first());

  // Benchmark
  await t.click(nav("Benchmark"));
  const row = (label: string) => page.locator("tr", { has: page.locator(`td:text-is("${label}")`) }).first();
  const passes = (await row("Cases passing every column").locator("td").allInnerTexts()).slice(1);
  const costs = (await row("Cost per conversation, mean").locator("td").allInnerTexts()).slice(1).map((s) => Number(s.replace("$", "")));
  const pct = Math.round((100 * costs[1]!) / costs[0]!);
  const [, baseModel, candModel] = (await page.locator("tr").first().locator("th").allInnerTexts()).map((x) => x.trim());
  // The page's subtitle names baseline and candidate; the caption must name the candidate, never the baseline.
  if (!(await text(page.locator("p.sub").first())).includes(`candidate ${candModel}`) || baseModel === candModel)
    throw new Error(`benchmark columns misread: baseline ${baseModel}, candidate ${candModel}`);
  await t.say(`Same frozen set on ${candModel}: ${passes[1]} pass, against ${passes[0]}.`, row("Cases passing every column"));
  await t.say(`And ${candModel} costs ${pct}% as much per conversation.`, row("Cost per conversation, mean"));
  await t.say("Every one of its misses: one extra question the fixed script never answered.",
    page.locator("h2:text-is('Cases that differ') + div table"));
  await t.say("The verdict comes from a rule fixed in code before the runs.", page.locator(".verdict"));

  // One receipt, last: the deep dive
  await t.click(nav("A receipt"));
  const ask = page.locator(".msg.member").first();
  const amount = (await text(ask)).match(/\$[\d,]+\.\d\d/)![0];
  await t.say(`A member asks to pay ${amount} toward a loan from checking.`, ask);
  await t.say("Before money moves: amount, accounts, posts immediately. Then it waits for yes.",
    page.locator(".msg", { hasText: "posts immediately" }));
  const pay = page.locator(".act", { has: page.locator(".tool", { hasText: "pay_loan" }) });
  await t.say("Over $250 policy gates it; the eval's stand-in approver signs this exact payload.", pay);
  await t.say("The member hears a fixed holding line, written by code, not the model.",
    page.locator(".msg", { hasText: "fixed runtime message" }));
  await t.say("Said versus did: each claimed action is checked against the core's own rows.",
    page.locator("h2:text-is('Said versus did') ~ table").first());
  const cost = (await text(page.locator("h2:text-is('Summary') + table tr").nth(1).locator("td").last()));
  await t.say(`Every model call is on the ledger. This conversation cost ${cost}.`,
    page.locator("h2:text-is('Cost ledger') + table"));

  await page.evaluate(() => (window as any).tourUI.title(true, "Every action has a receipt", "A working demo on a fictional credit union"));
  await t.pause(2600);
  return introAt;
}

function serve(port: number): ChildProcess {
  return spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", SITE],
    { stdio: "ignore", detached: true });
}

function ffmpeg(args: string[]): Promise<string> {
  return new Promise((ok, fail) => {
    const p = spawn("ffmpeg", ["-hide_banner", ...args], { stdio: ["ignore", "ignore", "pipe"] });
    let err = "";
    p.stderr.on("data", (c: Buffer) => (err += c.toString()));
    p.on("close", (code) => (code === 0 ? ok(err) : fail(new Error(`ffmpeg exited ${code}\n${err.slice(-2000)}`))));
  });
}

async function main() {
  const port = 4391;
  const server = serve(port);
  await wait(800);
  await rm(OUT, { recursive: true, force: true });
  await mkdir(OUT, { recursive: true });
  const browser = await chromium.launch();
  let raw: string | null = null;
  let t: Tour;
  let introAt = 0;
  try {
    const ctx = await browser.newContext(CHECK ? { viewport: { width: W, height: H } }
      : { viewport: { width: W, height: H }, recordVideo: { dir: join(OUT, "raw"), size: { width: W, height: H } } });
    await ctx.addInitScript({ content: OVERLAY_SCRIPT });
    const page = await ctx.newPage();
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    t = new Tour(page, Date.now());
    try {
      introAt = await tour(t, page, `http://127.0.0.1:${port}/`);
    } finally {
      const v = page.video();
      await ctx.close();
      raw = v ? await v.path() : null;
    }
    if (errors.length) throw new Error(`page errors: ${errors.join("; ")}`);
  } finally {
    await browser.close();
    if (server.pid) process.kill(-server.pid);
  }
  for (const p of t!.problems) console.log("PROBLEM " + p);
  if (CHECK) {
    console.log(`${t!.step} captions, ${t!.problems.length} problems`);
    process.exitCode = t!.problems.length ? 1 : 0;
    return;
  }
  // The film starts on the title card: find it (a dark stretch) and trim everything before it.
  const bd = await ffmpeg(["-t", "15", "-i", raw!, "-vf", "blackdetect=d=0.5:pic_th=0.98:pix_th=0.10", "-an", "-f", "null", "-"]);
  const m = bd.match(/black_start:([\d.]+)/);
  if (!m) throw new Error("title card not found in the recording");
  const start = Number(m[1]);
  const shift = start - introAt;
  const band = `pad=${W}:${H + BAND}:0:0:color=${BG}`;
  const mp4 = join(OUT, "tour.mp4");
  await ffmpeg(["-y", "-ss", (start + 0.03).toFixed(2), "-i", raw!, "-vf", `fps=30,${band},format=yuv420p`,
    "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-movflags", "+faststart", "-an", mp4]);
  await ffmpeg(["-y", "-t", "15", "-i", mp4, "-filter_complex",
    "fps=12,scale=900:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=4",
    "-loop", "0", join(OUT, "tour.gif")]);
  for (const [i, sec] of t!.marks.entries())
    await ffmpeg(["-y", "-ss", Math.max(0, sec + shift).toFixed(2), "-i", raw!, "-vf", band, "-frames:v", "1", "-q:v", "3",
      join(OUT, `step-${String(i + 1).padStart(2, "0")}.jpg`)]);
  const shots = (await readdir(OUT)).filter((f) => /^step-\d+\.jpg$/.test(f));
  await ffmpeg(["-y", "-framerate", "1", "-pattern_type", "glob", "-i", join(OUT, "step-*.jpg"),
    "-vf", `scale=480:-1,tile=4x${Math.ceil(shots.length / 4)}:padding=6:color=${BG}`, "-frames:v", "1", join(OUT, "contact.png")]);
  const mb = (await stat(mp4)).size / 1e6;
  console.log(`wrote ${mp4} (${mb.toFixed(1)} MB), ${shots.length} captions, ${join(OUT, "contact.png")}`);
}

main().catch((e) => { console.error(e instanceof Error ? e.message : e); process.exitCode = 1; });
