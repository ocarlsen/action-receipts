"""Deterministic synthetic data for the fictional Fernhollow Credit Union.

Same seed, same rows, same hash. Every name, number and address is invented.

    python -m receipts.seed            # reset the schema, seed, print the hash
"""
import argparse
import hashlib
import json
import random
from datetime import date, timedelta

import psycopg

from receipts.db import connect, reset_schema

FIRST = ["Ada", "Bram", "Celia", "Dev", "Esme", "Farid", "Greta", "Hugo", "Ines", "Jonah",
         "Kira", "Luca", "Mira", "Niko", "Opal", "Pavel", "Quinn", "Rosa", "Silas", "Tess"]
LAST = ["Abernathy", "Bellweather", "Castellan", "Dunmore", "Elsworth", "Fairbrook", "Galloway",
        "Hartwell", "Ironside", "Juniper", "Kestrel", "Lindqvist", "Marchetti", "Northcott"]
STREETS = ["Alder Way", "Birch Lane", "Cedar Court", "Driftwood Road", "Elm Row", "Fern Hollow Drive"]
MERCHANTS = ["Grocer", "Fuel stop", "Pharmacy", "Coffee", "Hardware", "Utility bill", "Restaurant"]

AS_OF = date(2026, 10, 1)   # fixed "today" for the seed, so dates never drift
SEED_TABLES = ["member", "membership", "membership_holder", "account", "card", "loan", "txn"]


def generate(seed: int = 7, n_members: int = 24) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    rows: dict[str, list[dict]] = {t: [] for t in SEED_TABLES}
    used_names = set()

    def new_member(i: int) -> str:
        while True:
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            if name not in used_names:
                used_names.add(name)
                break
        member_id = f"M{i:04d}"
        rows["member"].append({
            "member_id": member_id,
            "full_name": name,
            "phone_last4": f"{rng.randrange(10000):04d}",
            "address": f"{rng.randrange(10, 9999)} {rng.choice(STREETS)}, Fernhollow",
        })
        return member_id

    membership_seq = iter(range(100001, 199999))
    card_seq = iter(range(1, 10_000))

    def new_membership(label: str, holders: list[tuple[str, str]]) -> str:
        loan_kinds = ["equipment", "business line"] if label == "Business" else ["auto", "personal", "home equity"]
        no = str(next(membership_seq))
        rows["membership"].append({"membership_no": no, "label": label})
        for member_id, role in holders:
            rows["membership_holder"].append({"membership_no": no, "member_id": member_id, "role": role})
        for kind in ("checking", "savings"):
            account_id = f"A{no}-{kind[0].upper()}"
            balance = rng.randrange(5_000, 2_500_000) if kind == "checking" else rng.randrange(0, 5_000_000)
            rows["account"].append({"account_id": account_id, "membership_no": no,
                                    "kind": kind, "balance_cents": balance})
            if kind == "checking":
                rows["card"].append({"card_id": f"C{next(card_seq):04d}", "account_id": account_id,
                                     "last4": f"{rng.randrange(10000):04d}", "status": "active"})
                for _ in range(10):
                    rows["txn"].append({
                        "account_id": account_id,
                        "posted_on": AS_OF - timedelta(days=rng.randrange(1, 31)),
                        "amount_cents": -rng.randrange(300, 25_000),
                        "description": rng.choice(MERCHANTS),
                    })
        if rng.random() < 0.5:
            payment = rng.randrange(15_000, 90_000)
            rows["loan"].append({
                "loan_id": f"L{no}", "membership_no": no,
                "kind": rng.choice(loan_kinds),
                "principal_cents": payment * rng.randrange(12, 60),
                "payment_due_cents": payment,
                "due_date": AS_OF + timedelta(days=rng.randrange(3, 28)),
            })
        return no

    members = [new_member(i) for i in range(1, n_members + 1)]

    # Deliberate edge cases, always the first members:
    # M0001 holds two memberships (personal and business) and has a loan on the personal one.
    first = new_membership("Personal", [(members[0], "primary")])
    if not any(l["membership_no"] == first for l in rows["loan"]):
        rows["loan"].append({"loan_id": f"L{first}", "membership_no": first, "kind": "auto",
                             "principal_cents": 1_240_000, "payment_due_cents": 41_250,
                             "due_date": AS_OF + timedelta(days=10)})
    new_membership("Business", [(members[0], "primary")])
    # M0002 and M0003 share one joint membership.
    new_membership("Personal", [(members[1], "primary"), (members[2], "joint")])
    # M0004's card is already frozen.
    new_membership("Personal", [(members[3], "primary")])
    rows["card"][-1]["status"] = "frozen"
    # Everyone else: one personal membership.
    for m in members[4:]:
        new_membership("Personal", [(m, "primary")])

    rows["txn"].sort(key=lambda t: (t["account_id"], t["posted_on"], t["description"], t["amount_cents"]))
    return rows


def load(conn: psycopg.Connection, rows: dict[str, list[dict]]) -> None:
    with conn.cursor() as cur:
        for table in SEED_TABLES:
            for r in rows[table]:
                cols = ", ".join(r)
                marks = ", ".join(["%s"] * len(r))
                cur.execute(f"INSERT INTO core.{table} ({cols}) VALUES ({marks})", list(r.values()))
    conn.commit()


def core_hash(conn: psycopg.Connection) -> str:
    """sha256 over every core table's rows, in primary-key order."""
    h = hashlib.sha256()
    for table in SEED_TABLES + ["operation"]:
        cols = "*" if table != "operation" else "idempotency_key, operation, request, response"
        for row in conn.execute(f"SELECT {cols} FROM core.{table} ORDER BY 1, 2"):
            h.update(json.dumps(row, sort_keys=True, default=str).encode())
    return h.hexdigest()


def reseed(conn: psycopg.Connection, seed: int = 7) -> str:
    reset_schema(conn)
    load(conn, generate(seed))
    return core_hash(conn)


def reset_core(conn: psycopg.Connection, seed: int = 7) -> str:
    """Put the core back to the seed and leave app.* (transcripts, actions, ledger) alone."""
    tables = ", ".join(f"core.{t}" for t in SEED_TABLES + ["operation"])
    conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    load(conn, generate(seed))
    return core_hash(conn)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    with connect() as conn:
        print(reseed(conn, args.seed))


if __name__ == "__main__":
    main()
