"""The agent runtime: one conversation between a member and the model, over the fake core.

The model never touches the core. It asks for a tool; the runtime records the request,
checks it in code (verified member, the member's own accounts), calls the core with the
action id as the idempotency key, and records what the core answered. After every write it
reads the core back by that key: a write the core answered but did not record is a failure,
and the conversation goes to a person. Before any reply reaches the member, a claim in it that
an action is done must have that action recorded by the core (the live said-versus-did check);
otherwise the reply is held and the conversation goes to a person.
"""
import json
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

import httpx

from receipts.checker import backing, find_claims
from receipts.policy import classify, payload_sha256
from receipts.tools import READS, TOOLS, WRITES

MODEL = "claude-opus-5"
MAX_STEPS = 12   # model calls per member turn before the runtime hands off
BUDGET_USD = Decimal("0.20")   # per conversation; at or past it, the runtime hands off before the next call
APPROVAL_TIMEOUT_S = 300
READ_BACK_ATTEMPTS = 3   # tries at the read-back when the core answers it with an error
READ_BACK_PAUSE_S = 0.3
HOLDING_MESSAGE = "One moment while I confirm this with a colleague."

# USD per million tokens. Cache writes are 1.25x input, cache reads 0.1x (5-minute cache).
PRICES = {
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-opus-4-8": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
}

SYSTEM = """You are the member-service agent for Fernhollow Credit Union, a fictional credit union \
used in a demo. You help members by chat, using the tools provided.

- Verify the member with verify_member before sharing any account information or taking any action.
- If the member holds more than one membership, ask which one before acting on an account.
- Say an action is done only after its tool result confirms it. If a tool returns an error, \
tell the member plainly what did not happen.
- Before any loan payment or transfer, tell the member in one message: the amount in dollars, \
the account it comes from, where it goes, and that it posts immediately. Then wait for their yes.
- Amounts in tool calls are in cents; speak to the member in dollars.
- Refer to accounts, cards and loans the way a member would ("your checking", "the card ending 3714", "your auto loan"), never by internal id.
- Use handoff when the member asks for a person or when the tools cannot do what they need.
- Some actions need a colleague's approval. You will get the tool result once they decide; \
if it was declined or timed out, tell the member plainly that nothing was done.
- Reply in plain sentences for a chat window: no markdown, no bold, no headings. Keep replies short."""


class Model(Protocol):
    def create(self, *, system: list, tools: list, messages: list) -> object: ...


# Models that take the server-side refusal fallback: the models this project uses that support it.
FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}


class AnthropicModel:
    """The live model, pinned by id, with adaptive thinking; refusal fallbacks where supported."""

    def __init__(self, model: str = MODEL, client=None):
        import anthropic
        self.model = model
        self.client = client or anthropic.Anthropic()

    def create(self, *, system, tools, messages):
        params = dict(model=self.model, max_tokens=16000, system=system, tools=tools, messages=messages,
                      thinking={"type": "adaptive"})
        if self.model in FALLBACK_MODELS:
            return self.client.beta.messages.create(**params, betas=["server-side-fallback-2026-07-01"],
                                                    fallbacks="default")
        return self.client.messages.create(**params)


def cost_usd(model: str, usage) -> Decimal:
    inp, out = PRICES.get(model, PRICES[MODEL])
    get = (lambda k: getattr(usage, k, 0) or 0) if not isinstance(usage, dict) else (lambda k: usage.get(k) or 0)
    total = (get("input_tokens") * inp
             + get("cache_creation_input_tokens") * inp * Decimal("1.25")
             + get("cache_read_input_tokens") * inp * Decimal("0.1")
             + get("output_tokens") * out)
    return (total / Decimal(1_000_000)).quantize(Decimal("0.000001"))


def _plain(block) -> dict:
    """A content block as a plain dict, safe to store and to send back unchanged."""
    if isinstance(block, dict):
        return block
    return block.model_dump(mode="json", exclude_none=True)


def wait_for_decision(conn, action_id, timeout_s: float = APPROVAL_TIMEOUT_S, poll_s: float = 1.0):
    """Block until a person decides on the action, or the timeout passes (returns None)."""
    deadline = time.monotonic() + timeout_s
    while True:
        row = conn.execute("SELECT decision, approver, payload_sha256, reason FROM app.approval"
                           " WHERE action_id = %s", (action_id,)).fetchone()
        conn.commit()   # end the read so the next poll sees new commits
        if row is not None or time.monotonic() >= deadline:
            return row
        time.sleep(poll_s)


class Refused(Exception):
    """A tool call the runtime will not send to the core."""


@dataclass
class Session:
    member_id: str | None = None
    accounts: set = field(default_factory=set)
    cards: set = field(default_factory=set)
    loans: set = field(default_factory=set)
    handed_off: dict | None = None


class Agent:
    def __init__(self, conn, core: httpx.Client, model: Model, channel: str = "chat",
                 wait=wait_for_decision, on_runtime_message=None, budget_usd: Decimal = BUDGET_USD):
        self.conn = conn
        self.budget_usd = budget_usd
        self.core = core
        self.model = model
        self.wait = wait
        self.on_runtime_message = on_runtime_message
        self.session = Session()
        self.messages: list[dict] = []
        self.seq = 0
        self.conversation_id = uuid.uuid4()
        conn.execute("INSERT INTO app.conversation (conversation_id, channel, model) VALUES (%s, %s, %s)",
                     (self.conversation_id, channel, getattr(model, "model", type(model).__name__)))
        conn.commit()

    # ---- transcript -------------------------------------------------------

    def _record(self, role: str, content) -> None:
        self.seq += 1
        self.conn.execute("INSERT INTO app.turn (conversation_id, seq, role, content) VALUES (%s, %s, %s, %s)",
                          (self.conversation_id, self.seq, role, json.dumps(content)))
        self.conn.commit()

    def _ledger(self, response, latency_ms: int) -> None:
        served = getattr(response, "model", None) or getattr(self.model, "model", MODEL)
        u = response.usage
        get = (lambda k: getattr(u, k, 0) or 0)
        self.conn.execute(
            "INSERT INTO app.ledger (conversation_id, model, input_tokens, cache_read_tokens,"
            " cache_write_tokens, output_tokens, cost_usd, latency_ms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (self.conversation_id, served, get("input_tokens"), get("cache_read_input_tokens"),
             get("cache_creation_input_tokens"), get("output_tokens"), cost_usd(served, u), latency_ms))
        self.conn.commit()

    def spent_usd(self) -> Decimal:
        return self.conn.execute("SELECT coalesce(sum(cost_usd), 0) AS c FROM app.ledger WHERE conversation_id = %s",
                                 (self.conversation_id,)).fetchone()["c"]

    # ---- the loop ---------------------------------------------------------

    def send(self, member_text: str) -> str:
        """One member turn. Returns what the agent says back."""
        if self.session.handed_off:
            return "You're connected to a member-service representative."
        self.messages.append({"role": "user", "content": member_text})
        self._record("member", member_text)
        system = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]
        for _ in range(MAX_STEPS):
            spent = self.spent_usd()
            if spent >= self.budget_usd:
                return self._handoff_now(f"conversation budget reached: ${spent} of ${self.budget_usd}")
            started = time.monotonic()
            response = self.model.create(system=system, tools=TOOLS, messages=self.messages)
            self._ledger(response, round((time.monotonic() - started) * 1000))
            content = [_plain(b) for b in response.content]
            self.messages.append({"role": "assistant", "content": content})
            self._record("assistant", content)
            if response.stop_reason in ("refusal", "max_tokens"):
                return self._handoff_now(f"model stopped: {response.stop_reason}")
            calls = [b for b in content if b.get("type") == "tool_use"]
            if not calls:
                reply = "\n".join(b["text"] for b in content if b.get("type") == "text").strip()
                return self._release(reply)
            results = [self._call_tool(c) for c in calls]
            self.messages.append({"role": "user", "content": results})
            self._record("tool_results", results)
            if self.session.handed_off:
                return "I've passed this to a member-service representative with everything we've covered."
        return self._handoff_now(f"no answer after {MAX_STEPS} model calls")

    def _release(self, reply: str) -> str:
        """The live said-versus-did check: a reply that claims a completed action the core has no record of
        is held, never shown to the member, and the conversation goes to a person."""
        unbacked = [(tool, claim) for tool, claim in find_claims(reply)
                    if backing(self.conn, self.conversation_id, tool, claim=claim) is None]
        if not unbacked:
            return reply
        tool, claim = unbacked[0]
        self._record("runtime", f"Held the agent's reply: it claimed a {tool} the core has no record of: {claim!r}")
        return self._handoff_now(f"held a reply claiming an unrecorded {tool}")

    def _handoff_now(self, reason: str) -> str:
        """The runtime hands off on its own (refusal, step limit, budget); the reason goes in the transcript."""
        self.session.handed_off = self._packet(reason, "The agent could not finish; see the actions below.")
        self._record("runtime", f"Handed off by the runtime: {reason}")
        return "I'm connecting you with a member-service representative."

    def _packet(self, reason: str, summary: str) -> dict:
        """What travels with the member to a person, so they never start over."""
        actions = self.conn.execute(
            "SELECT a.tool, a.args, (SELECT kind FROM app.action_event e WHERE e.action_id = a.action_id"
            "  ORDER BY event_id DESC LIMIT 1) AS status FROM app.action a"
            " WHERE a.conversation_id = %s AND a.tool <> 'handoff' ORDER BY a.created_at",
            (self.conversation_id,)).fetchall()
        return {"reason": reason, "summary": summary, "member_id": self.session.member_id,
                "verified": self.session.member_id is not None, "actions": actions}

    def _say(self, text: str) -> None:
        """A message to the member written by the runtime, not the model. It goes in the transcript."""
        self._record("runtime", text)
        if self.on_runtime_message:
            self.on_runtime_message(text)

    # ---- tools ------------------------------------------------------------

    def _call_tool(self, call: dict) -> dict:
        name, args = call["name"], call["input"]
        action_id = uuid.uuid4()
        policy, rule_id = classify(self.conn, name, args)
        self.conn.execute(
            "INSERT INTO app.action (action_id, conversation_id, tool, args, policy_class, policy_rule_id)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (action_id, self.conversation_id, name, json.dumps(args), policy, rule_id))
        self._event(action_id, "requested", {"tool_use_id": call["id"]})
        try:
            if policy == "denied":
                raise Refused(f"no policy rule allows {name} with these arguments")
            self._check(name, args)
            if policy == "gated":
                self._await_approval(action_id, name, args)
            result = self._execute(name, args, str(action_id))
            if name in WRITES:
                self._read_back(name, str(action_id), result)
        except Refused as e:
            self._event(action_id, "refused", {"reason": str(e)})
            return {"type": "tool_result", "tool_use_id": call["id"], "is_error": True,
                    "content": f"Refused: {e}"}
        except CoreError as e:
            self._event(action_id, "failed", {"status": e.status, "detail": e.detail})
            return {"type": "tool_result", "tool_use_id": call["id"], "is_error": True,
                    "content": f"The core did not complete this: {e.detail}"}
        except Unconfirmed as e:
            self._event(action_id, "failed", {"read_back_unavailable": str(e), "core_answered": e.answer})
            self.session.handed_off = self._packet(f"could not confirm {name} with the core: {e}",
                                                   "The core accepted a write but the confirmation check did not answer; it may have completed. See the actions below.")
            self._record("runtime", f"Handed off by the runtime: could not read back {name}; it may have completed")
            return {"type": "tool_result", "tool_use_id": call["id"], "is_error": True,
                    "content": "The core accepted this, but its record could not be checked just now, so it may have "
                               "completed. Do not say it was done or not done. A colleague is confirming."}
        except Unrecorded as e:
            self._event(action_id, "failed", {"read_back": str(e), "core_answered": e.answer})
            self.session.handed_off = self._packet(f"read-back failed for {name}: {e}",
                                                   "The core reported a write it did not record; see the actions below.")
            self._record("runtime", f"Handed off by the runtime: read-back found no record of {name}")
            return {"type": "tool_result", "tool_use_id": call["id"], "is_error": True,
                    "content": "The core answered but has no record of this; nothing was done. A colleague is taking over."}
        self._event(action_id, "executed", {"result": result})
        return {"type": "tool_result", "tool_use_id": call["id"], "content": json.dumps(result)}

    def _await_approval(self, action_id, name: str, args: dict) -> None:
        """Hold a gated call until a person approves this exact payload. Raises Refused otherwise."""
        self._say(HOLDING_MESSAGE)
        decision = self.wait(self.conn, action_id)
        if decision is None:
            self._event(action_id, "expired", {"timeout_s": APPROVAL_TIMEOUT_S})
            raise Refused("approval did not arrive in time; nothing was done")
        if decision["decision"] != "approve":
            raise Refused(f"a member-service officer declined this: {decision['reason'] or 'no reason given'}")
        if decision["payload_sha256"] != payload_sha256(name, args):
            raise Refused("the approval does not match this action's payload")

    def _event(self, action_id, kind: str, detail: dict) -> None:
        self.conn.execute("INSERT INTO app.action_event (action_id, kind, detail) VALUES (%s, %s, %s)",
                          (action_id, kind, json.dumps(detail, default=str)))
        self.conn.commit()

    def _check(self, name: str, args: dict) -> None:
        s = self.session
        if name in ("verify_member", "handoff"):
            return
        if s.member_id is None:
            raise Refused("the member is not verified yet")
        owned = {"account_id": s.accounts, "from_account_id": s.accounts, "to_account_id": s.accounts,
                 "card_id": s.cards, "loan_id": s.loans}
        for key, allowed in owned.items():
            if key in args and args[key] not in allowed:
                raise Refused(f"{args[key]} does not belong to the verified member")

    def _execute(self, name: str, args: dict, key: str) -> dict:
        s = self.session
        if name == "handoff":
            s.handed_off = self._packet(args["reason"], args["summary"])
            return {"handed_off": True, "packet": s.handed_off}
        if name == "verify_member":
            r = self._core("POST", "/verify", json=args)
            s.member_id = r["member_id"]
            self._load_memberships()
            return {"verified": True, "member_name": r["full_name"]}
        if name == "list_memberships":
            return self._core("GET", f"/members/{s.member_id}/memberships")
        if name == "get_balance":
            return self._core("GET", f"/accounts/{args['account_id']}/balance")
        if name == "get_transactions":
            limit = max(1, min(int(args["limit"]), 20))
            return self._core("GET", f"/accounts/{args['account_id']}/transactions", params={"limit": limit})
        headers = {"Idempotency-Key": key}
        if name == "freeze_card":
            return self._core("POST", f"/cards/{args['card_id']}/freeze", headers=headers)
        if name == "pay_loan":
            return self._core("POST", f"/loans/{args['loan_id']}/payments", headers=headers,
                              json={"from_account_id": args["from_account_id"],
                                    "amount_cents": args["amount_cents"]})
        if name == "transfer":
            return self._core("POST", "/transfers", headers=headers, json=args)
        if name == "update_address":
            return self._core("POST", f"/members/{s.member_id}/address", headers=headers, json=args)
        raise Refused(f"unknown tool {name}")

    def _read_back(self, name: str, key: str, answer: dict) -> None:
        """Ask the core what it recorded under this write's key. Raises Unrecorded when nothing is there, and
        Unconfirmed when the core cannot answer the lookup (the write may still have committed)."""
        for attempt in range(READ_BACK_ATTEMPTS):
            try:
                op = self._core("GET", f"/operations/{key}")
                break
            except CoreError as e:
                if e.status == 404:
                    raise Unrecorded("the core has no write under this key", answer) from None
                if attempt == READ_BACK_ATTEMPTS - 1:
                    raise Unconfirmed(f"the read-back answered {e.status}", answer) from None
                time.sleep(READ_BACK_PAUSE_S)
        if op["operation"] != name:
            raise Unrecorded(f"the key holds a {op['operation']}, not a {name}", answer)

    def _load_memberships(self) -> None:
        s = self.session
        for m in self._core("GET", f"/members/{s.member_id}/memberships")["memberships"]:
            s.accounts |= {a["account_id"] for a in m["accounts"] or []}
            s.cards |= {c["card_id"] for c in m["cards"] or []}
            s.loans |= {l["loan_id"] for l in m["loans"] or []}

    def _core(self, method: str, path: str, **kw) -> dict:
        """Call the core. A 5xx on a write is retried once with the same idempotency key."""
        attempts = 2 if "headers" in kw else 1
        for attempt in range(attempts):
            r = self.core.request(method, path, **kw)
            if r.status_code < 500 or attempt == attempts - 1:
                break
        if r.status_code >= 400:
            raise CoreError(r.status_code, r.json().get("detail", r.text))
        return r.json()


class Unrecorded(Exception):
    """The core answered a write but its own record has no such write (found by the read-back)."""
    def __init__(self, reason: str, answer: dict):
        super().__init__(reason)
        self.answer = answer


class Unconfirmed(Exception):
    """The core answered a write but the read-back could not be completed: the write may have committed."""
    def __init__(self, reason: str, answer: dict):
        super().__init__(reason)
        self.answer = answer


class CoreError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


assert WRITES | READS | {"handoff"} == {t["name"] for t in TOOLS}
