"""The fake core: a deliberately small stand-in for a credit union's core banking system.

It is fake and named as fake. It does not model any vendor's core. Nine operations:

    reads   POST /verify, GET /members/{id}/memberships,
            GET /accounts/{id}/balance, GET /accounts/{id}/transactions,
            GET /operations/{idempotency_key} (what the core recorded under a key: the read-back)
    writes  POST /cards/{id}/freeze, POST /loans/{id}/payments,
            POST /transfers, POST /members/{id}/address

Every write requires an Idempotency-Key header. A replay with the same key and
request returns the stored response (Idempotent-Replayed: true) and changes
nothing; the same key with a different request gets 409. Only successful writes
are recorded under a key, so a refused write can be retried after it is fixed.

Fault injection for tests and the canary, via the X-Fault header:
    error         503 before doing anything
    after_commit  the write commits, then the caller still gets a 503
                  (the case idempotency keys exist for: the retry must not repeat it)
    drop_write    a broken core: it answers 200 with a plausible result and writes nothing.
                  Also set core-wide with RECEIPTS_CORE_BREAK=drop_write, which is how the
                  canary is proven red without changing the canary.

    uvicorn receipts.core:app --port 8100
"""
import json
import os
from typing import Callable

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from receipts.db import connect

app = FastAPI(title="Fernhollow fake core (synthetic, not a real core)")


def get_conn():
    # Autocommit, so each write's conn.transaction() block commits on its own
    # and an error raised after it (the after_commit fault) cannot undo it.
    with connect() as conn:
        conn.autocommit = True
        yield conn


class VerifyIn(BaseModel):
    membership_no: str
    phone_last4: str


class LoanPaymentIn(BaseModel):
    from_account_id: str
    amount_cents: int = Field(gt=0)


class TransferIn(BaseModel):
    from_account_id: str
    to_account_id: str
    amount_cents: int = Field(gt=0)


class AddressIn(BaseModel):
    address: str = Field(min_length=5)


def _fault(x_fault: str | None) -> None:
    if x_fault == "error":
        raise HTTPException(503, "core unavailable (injected fault)")


class _Dropped(Exception):
    """Rolls back a write the broken core pretends to have made."""


def _one(conn, sql: str, *params) -> dict:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise HTTPException(404, "not found")
    return row


def _idempotent_write(conn: psycopg.Connection, key: str | None, operation: str, request: dict,
                      do_write: Callable[[], dict], response: Response, x_fault: str | None) -> dict:
    if not key:
        raise HTTPException(400, "Idempotency-Key header is required for writes")
    x_fault = x_fault or os.environ.get("RECEIPTS_CORE_BREAK")
    _fault(x_fault)
    if x_fault == "drop_write":
        result = {}
        try:
            with conn.transaction():
                result = do_write()
                raise _Dropped()
        except _Dropped:
            pass
        return json.loads(json.dumps(result, default=str))
    stored = {"operation": operation, "request": request}
    prior = conn.execute(
        "SELECT operation, request, response FROM core.operation WHERE idempotency_key = %s", (key,)
    ).fetchone()
    if prior is not None:
        if {"operation": prior["operation"], "request": prior["request"]} != stored:
            raise HTTPException(409, "Idempotency-Key already used for a different request")
        response.headers["Idempotent-Replayed"] = "true"
        return prior["response"]
    try:
        with conn.transaction():
            result = do_write()
            conn.execute(
                "INSERT INTO core.operation (idempotency_key, operation, request, response)"
                " VALUES (%s, %s, %s, %s)",
                (key, operation, json.dumps(request), json.dumps(result, default=str)),
            )
    except psycopg.errors.UniqueViolation:
        # A concurrent request with the same key won the race; answer as a replay.
        conn.rollback()
        return _idempotent_write(conn, key, operation, request, do_write, response, None)
    if x_fault == "after_commit":
        raise HTTPException(503, "core timed out after committing (injected fault)")
    return json.loads(json.dumps(result, default=str))


# ---- reads -----------------------------------------------------------------

@app.post("/verify")
def verify(body: VerifyIn, conn=Depends(get_conn), x_fault: str | None = Header(None)):
    _fault(x_fault)
    row = conn.execute(
        "SELECT m.member_id, m.full_name FROM core.member m"
        " JOIN core.membership_holder h USING (member_id)"
        " WHERE h.membership_no = %s AND m.phone_last4 = %s",
        (body.membership_no, body.phone_last4),
    ).fetchone()
    if row is None:
        raise HTTPException(401, "verification failed")
    return {"verified": True, **row}


@app.get("/members/{member_id}/memberships")
def memberships(member_id: str, conn=Depends(get_conn), x_fault: str | None = Header(None)):
    _fault(x_fault)
    _one(conn, "SELECT 1 FROM core.member WHERE member_id = %s", member_id)
    rows = conn.execute(
        "SELECT ms.membership_no, ms.label, h.role,"
        "  (SELECT json_agg(json_build_object('account_id', a.account_id, 'kind', a.kind)"
        "          ORDER BY a.account_id)"
        "     FROM core.account a WHERE a.membership_no = ms.membership_no) AS accounts,"
        "  (SELECT json_agg(json_build_object('card_id', c.card_id, 'last4', c.last4, 'status', c.status))"
        "     FROM core.card c JOIN core.account a USING (account_id)"
        "    WHERE a.membership_no = ms.membership_no) AS cards,"
        "  (SELECT json_agg(json_build_object('loan_id', l.loan_id, 'kind', l.kind,"
        "          'payment_due_cents', l.payment_due_cents, 'due_date', l.due_date))"
        "     FROM core.loan l WHERE l.membership_no = ms.membership_no) AS loans"
        " FROM core.membership_holder h JOIN core.membership ms USING (membership_no)"
        " WHERE h.member_id = %s ORDER BY ms.membership_no",
        (member_id,),
    ).fetchall()
    return {"member_id": member_id, "memberships": rows}


@app.get("/operations/{key}")
def operation(key: str, conn=Depends(get_conn), x_fault: str | None = Header(None)):
    """The write the core recorded under this idempotency key; 404 when it recorded none."""
    _fault(x_fault)
    return _one(conn, "SELECT idempotency_key, operation, request, response, created_at FROM core.operation"
                " WHERE idempotency_key = %s", key)


@app.get("/accounts/{account_id}/balance")
def balance(account_id: str, conn=Depends(get_conn), x_fault: str | None = Header(None)):
    _fault(x_fault)
    return _one(conn, "SELECT account_id, kind, balance_cents FROM core.account WHERE account_id = %s",
                account_id)


@app.get("/accounts/{account_id}/transactions")
def transactions(account_id: str, limit: int = Query(10, ge=1, le=50), conn=Depends(get_conn),
                 x_fault: str | None = Header(None)):
    _fault(x_fault)
    _one(conn, "SELECT 1 FROM core.account WHERE account_id = %s", account_id)
    rows = conn.execute(
        "SELECT txn_id, posted_on, amount_cents, description FROM core.txn"
        " WHERE account_id = %s ORDER BY posted_on DESC, txn_id DESC LIMIT %s",
        (account_id, min(limit, 50)),
    ).fetchall()
    return {"account_id": account_id, "transactions": rows}


# ---- writes ----------------------------------------------------------------

@app.post("/cards/{card_id}/freeze")
def freeze_card(card_id: str, response: Response, conn=Depends(get_conn),
                idempotency_key: str | None = Header(None), x_fault: str | None = Header(None)):
    def write():
        card = _one(conn, "SELECT status FROM core.card WHERE card_id = %s FOR UPDATE", card_id)
        if card["status"] == "frozen":
            return {"card_id": card_id, "status": "frozen", "changed": False}
        conn.execute("UPDATE core.card SET status = 'frozen' WHERE card_id = %s", (card_id,))
        return {"card_id": card_id, "status": "frozen", "changed": True}
    return _idempotent_write(conn, idempotency_key, "freeze_card", {"card_id": card_id},
                             write, response, x_fault)


def _debit(conn, account_id: str, amount_cents: int) -> int:
    acct = _one(conn, "SELECT balance_cents FROM core.account WHERE account_id = %s FOR UPDATE",
                account_id)
    if acct["balance_cents"] < amount_cents:
        raise HTTPException(422, "insufficient funds")
    return conn.execute(
        "UPDATE core.account SET balance_cents = balance_cents - %s WHERE account_id = %s"
        " RETURNING balance_cents", (amount_cents, account_id)).fetchone()["balance_cents"]


@app.post("/loans/{loan_id}/payments")
def pay_loan(loan_id: str, body: LoanPaymentIn, response: Response, conn=Depends(get_conn),
             idempotency_key: str | None = Header(None), x_fault: str | None = Header(None)):
    def write():
        loan = _one(conn, "SELECT principal_cents FROM core.loan WHERE loan_id = %s FOR UPDATE", loan_id)
        if body.amount_cents > loan["principal_cents"]:
            raise HTTPException(422, "payment exceeds the loan balance")
        new_balance = _debit(conn, body.from_account_id, body.amount_cents)
        principal = conn.execute(
            "UPDATE core.loan SET principal_cents = principal_cents - %s WHERE loan_id = %s"
            " RETURNING principal_cents", (body.amount_cents, loan_id)).fetchone()["principal_cents"]
        conn.execute("INSERT INTO core.txn (account_id, posted_on, amount_cents, description)"
                     " VALUES (%s, current_date, %s, %s)",
                     (body.from_account_id, -body.amount_cents, f"Loan payment {loan_id}"))
        return {"loan_id": loan_id, "paid_cents": body.amount_cents,
                "principal_cents": principal, "from_balance_cents": new_balance}
    return _idempotent_write(conn, idempotency_key, "pay_loan",
                             {"loan_id": loan_id, **body.model_dump()}, write, response, x_fault)


@app.post("/transfers")
def transfer(body: TransferIn, response: Response, conn=Depends(get_conn),
             idempotency_key: str | None = Header(None), x_fault: str | None = Header(None)):
    def write():
        if body.from_account_id == body.to_account_id:
            raise HTTPException(422, "cannot transfer to the same account")
        _one(conn, "SELECT 1 FROM core.account WHERE account_id = %s", body.to_account_id)
        new_balance = _debit(conn, body.from_account_id, body.amount_cents)
        conn.execute("UPDATE core.account SET balance_cents = balance_cents + %s WHERE account_id = %s",
                     (body.amount_cents, body.to_account_id))
        conn.execute("INSERT INTO core.txn (account_id, posted_on, amount_cents, description)"
                     " VALUES (%s, current_date, %s, %s), (%s, current_date, %s, %s)",
                     (body.from_account_id, -body.amount_cents, f"Transfer to {body.to_account_id}",
                      body.to_account_id, body.amount_cents, f"Transfer from {body.from_account_id}"))
        return {"from_account_id": body.from_account_id, "to_account_id": body.to_account_id,
                "amount_cents": body.amount_cents, "from_balance_cents": new_balance}
    return _idempotent_write(conn, idempotency_key, "transfer", body.model_dump(), write, response, x_fault)


@app.post("/members/{member_id}/address")
def update_address(member_id: str, body: AddressIn, response: Response, conn=Depends(get_conn),
                   idempotency_key: str | None = Header(None), x_fault: str | None = Header(None)):
    def write():
        _one(conn, "SELECT 1 FROM core.member WHERE member_id = %s FOR UPDATE", member_id)
        conn.execute("UPDATE core.member SET address = %s WHERE member_id = %s", (body.address, member_id))
        return {"member_id": member_id, "address": body.address}
    return _idempotent_write(conn, idempotency_key, "update_address",
                             {"member_id": member_id, **body.model_dump()}, write, response, x_fault)
