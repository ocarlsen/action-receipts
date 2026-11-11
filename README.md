# Every action has a receipt

A member-service agent for a fictional credit union that completes core-banking actions
(freeze a card, pay a loan, move money, change an address) and proves every one of them:
each write is policy-gated, idempotent and audited. The idea is to score the agent on what
changed in the system of record, not on what it said.

[![The tour: the overview, the dashboard with both canary faults caught, the eval report, the model benchmark and one conversation's receipt](docs/media/receipts-tour.gif)](docs/media/receipts-tour.mp4)

The whole walkthrough, 1:44 with captions and no sound:
[docs/media/receipts-tour.mp4](docs/media/receipts-tour.mp4). Recorded by `tour/tour.ts` from the static export.

## Provenance

Written from scratch by Owen Carlsen, with Claude Code.

**Everything here is synthetic:** Fernhollow Credit Union, its members, accounts
and numbers are invented, and the "core" is a small fake written for this project. It does
not model any vendor's core banking system, and nothing here is a compliance claim.

## What it shows

- An idempotent fake core with append-only audit tables: a write needs an idempotency key, a
  replay returns the stored response, and UPDATE and DELETE on those tables are rejected by
  triggers. (TRUNCATE is not blocked: the eval runner and the canary reset the fake core with it.)
- A policy engine that defaults to deny: every tool call is classed read, low or gated by a
  rule, and loan payments and transfers over $250, and every address change, wait for a person
  who approves the exact payload.
- Said-versus-did held live: the runtime reads a write back from the core before the member
  hears "done", and a checker holds any claim of a completed action the core has no record of (for
  a transfer or loan payment that states a dollar amount, the recorded amount must match).
- A 60-case frozen eval set, scored from the core's own rows and the action log, never from
  what the agent said (the disclosure and said-versus-did columns read the agent's words only
  to check them against those rows).
- A two-model benchmark with a verdict rule fixed in code before the runs. In two runs of the
  frozen set (reports not included here), Opus 5 passed 59 of 60 cases and Sonnet 5 passed 54
  of 60 at 37% of the cost per conversation; a five-case gap is outside the two-case rerun
  noise, so the rule keeps Opus 5.
- An hourly canary, proven red with two faults: `RECEIPTS_CORE_BREAK=drop_write` (the core
  answers 200 and writes nothing) and `RECEIPTS_MODEL_BREAK=false_done` (the model claims a
  transfer it never asked for).
- A static export with a leak scan that fails the build on local paths, keys, connection
  strings or private addresses.

## Run it

Prerequisites:

- Python 3.12.
- A running local PostgreSQL. The commands below use `createdb` and the default
  `postgresql:///fernhollow`; set `DATABASE_URL` to use another database. Tests default to
  `postgresql:///fernhollow_test` and also honor `DATABASE_URL`. The hourly canary uses its
  own database, `fernhollow_canary` (see `receipts/canary.py` below).
- An Anthropic API key in `ANTHROPIC_API_KEY`, only for the billed commands (`chat`, `evalrun`
  without `--freeze`).
- Node and ffmpeg, only for the tour film.

    python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
    createdb fernhollow && createdb fernhollow_test
    .venv/bin/python -m receipts.seed          # DESTRUCTIVE: drops all data, saved eval runs included; reseeds
    .venv/bin/uvicorn receipts.core:app --port 8100
    .venv/bin/uvicorn receipts.approver:app --host 127.0.0.1 --port 8101   # approval queue; no sign-in, keep it on localhost
    .venv/bin/python -m pytest -q              # uses fernhollow_test; no API calls
    .venv/bin/python -m receipts.chat --whoami M0001   # live: needs ANTHROPIC_API_KEY, billed; needs the core running on :8100
    .venv/bin/python -m receipts.evalrun [--only id,...] [--judge]   # live eval run, billed (~$0.09/case); runs the core in-process
    .venv/bin/python -m receipts.evalrun --freeze      # record the set's hash in evals/FROZEN
    .venv/bin/python -m receipts.evalrun --model claude-sonnet-5 --judge   # same set, another pinned model
    .venv/bin/python -m receipts.benchmark <baseline_run_id> <candidate_run_id>   # comparison page + verdict

The two `uvicorn` commands each run in the foreground; use separate terminals.

## Layout

- `schema.sql`: `core.*` is the fake core (system of record); `app.*` is the agent side
  (conversations, actions, approvals, cost ledger, eval runs). Audit tables reject
  UPDATE and DELETE (triggers); `core.*` is reset by TRUNCATE between eval cases and canary ticks.
- `receipts/seed.py`: deterministic generator; same seed, same hash. Edge cases: one member
  with two memberships, a joint membership, a card that is already frozen.
- `receipts/core.py`: nine operations (five reads, four writes), including a read-back
  endpoint that returns what the core recorded under an idempotency key. Writes need an
  `Idempotency-Key`; a replay returns the stored response, a reused key with a different
  request gets 409. `X-Fault: after_commit` commits and then fails, to prove a retry does
  not repeat the write.
- `receipts/runtime.py`: the agent loop (Claude tool use, no framework). The model only asks
  for tools; the runtime checks each call in code (verified member, the member's own accounts),
  calls the core with the action id as the idempotency key, and records every request, result,
  transcript turn and model call. The member hears "done" only after the core confirms it.
- `receipts/policy.py` + `app.policy_rule`: every tool call is classed read, low or gated by a
  rule; no matching rule means refused (default deny). Loan payments and transfers over $250,
  and every address change, wait for a person.
- `receipts/approver.py`: the approval queue page (single user, no sign-in). A person approves
  the exact payload; a second approval records nothing. The member hears a fixed holding line,
  written by code, while the action waits.
- `receipts/checker.py`: said versus did, live and after the fact. The runtime runs it on every reply
  before it is sent and holds a "done" the core has no record of (then hands off); the module also records
  every claim on the receipt. A claim is backed by a recorded action of the same kind; when it states a
  dollar amount, the action must be for that amount.
- `receipts/receipt.py`: the per-conversation receipt page (the audit view), from rows only.
- `evals/cases.json` + `evals/CASES.md`: 60 scripted conversations (8 intents, plus traps:
  injection, wrong member, over limit, ambiguous account, core timeout, core down, must refuse).
  Member turns are fixed text; the set's hash covers its canonical JSON.
- `receipts/evalrun.py`: resets the core per case, plays the turns, stands in for the approver,
  injects the case's core fault, saves `app.eval_run`, renders the report and every receipt.
- `receipts/scorer.py`: six deterministic columns from the core and the action log: completed in
  core, no unauthorized write, disclosure before money moves (amount, from, to, posts immediately,
  then the member's yes), handoff with context, under budget, said = did.
- `receipts/judge.py`: a pinned model grades the free text blind (accurate, clear, tone); its cost
  is on the ledger but not on the conversation's budget, and it never decides pass or fail.
- `receipts/costs.py`: cost and latency rollups from the ledger (p50/p95 per conversation and per
  model call). The runtime's budget guard hands a conversation to a person at $0.20 spent.
- `receipts/benchmark.py`: two eval runs of the frozen set side by side, with a verdict from a
  rule fixed in code before the runs: no more safety-column failures, pass rate within the
  observed rerun noise (2 cases), mean cost at most 60% of the baseline's.
- `receipts/canary.py`: one scripted conversation per tick through the real runtime, policy, core
  and checker ($0). Silent when clean, one alert on the first red tick. Proven red with
  `RECEIPTS_CORE_BREAK=drop_write` (the core answers 200 and writes nothing) and
  `RECEIPTS_MODEL_BREAK=false_done` (the model claims the transfer without ever asking for
  it). Hourly via `ops/install_canary.sh` (launchd). Each tick resets the fake core, so the job runs
  against its own database (`fernhollow_canary`, created by the install script) and never touches
  the one chat uses. The dashboard export reads the ticks from there: build the site with
  `CANARY_DATABASE_URL=postgresql:///fernhollow_canary` (unset, it reads the default database), and
  run the manual fault injections with `DATABASE_URL=postgresql:///fernhollow_canary` too.
- `receipts/site.py`: static export to out/site (overview, dashboard, eval report with receipts,
  benchmark), noindex everywhere, and a leak scan that fails the build on local paths, keys,
  connection strings or private addresses. The built-in pattern list can be extended with an
  optional, gitignored `.leak-terms` file at the repo root (one regex per line) for any extra
  private terms. `ops/publish_site.sh` pushes the export to a public deploy-only repo named by
  `SITE_REPO` and domain `SITE_DOMAIN` (both required env vars); run it by hand.
- `tour/tour.ts`: the walkthrough film of the built site (Playwright plus ffmpeg; the caption
  overlay is `tour/overlay.ts`, its placement rules `tour/geometry.ts`). `cd tour && npm install && node tour.ts`; `--check` tests
  every caption's placement without recording. Caption numbers are read off the pages.
