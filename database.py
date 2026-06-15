from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(__file__).resolve().parent / "data" / "cashback_agent.db"
CARDS_SEED_PATH = DATA_DIR / "cards.json"
TRANSACTIONS_SEED_PATH = DATA_DIR / "transactions.json"


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_database() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cards (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                statement_type TEXT NOT NULL DEFAULT 'calendar_month',
                statement_close_day INTEGER,
                cashback_close_day INTEGER,
                credit_limit INTEGER NOT NULL DEFAULT 0,
                min_total_spend INTEGER NOT NULL DEFAULT 0,
                period_cap INTEGER,
                cashback_round_down_to INTEGER NOT NULL DEFAULT 0,
                cashback_rules_json TEXT NOT NULL DEFAULT '[]',
                aliases_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY,
                card_id TEXT NOT NULL,
                transaction_date TEXT NOT NULL,
                amount INTEGER NOT NULL,
                merchant_name TEXT,
                mcc TEXT,
                payment_method TEXT NOT NULL CHECK (payment_method IN ('online', 'pos')),
                category TEXT,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (card_id) REFERENCES cards(id)
            );

            CREATE INDEX IF NOT EXISTS idx_transactions_card_date
            ON transactions(card_id, transaction_date);

            CREATE INDEX IF NOT EXISTS idx_transactions_mcc
            ON transactions(mcc);

            CREATE TABLE IF NOT EXISTS merchant_mcc (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant_full_name TEXT,
                merchant_name TEXT NOT NULL,
                merchant_key TEXT NOT NULL,
                address TEXT,
                amex TEXT,
                payment_method TEXT NOT NULL DEFAULT 'any',
                mcc TEXT NOT NULL,
                category TEXT,
                note TEXT,
                source TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_merchant_mcc_key
            ON merchant_mcc(merchant_key);

            CREATE INDEX IF NOT EXISTS idx_merchant_mcc_mcc
            ON merchant_mcc(mcc);
            """
        )
        ensure_columns(conn)
        card_count = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        if card_count == 0:
            seed_cards(conn)
        # Transactions are user data. Do not auto-seed examples into the live dashboard.


def ensure_columns(conn: sqlite3.Connection) -> None:
    card_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(cards)").fetchall()
    }
    if "cashback_round_down_to" not in card_columns:
        conn.execute("ALTER TABLE cards ADD COLUMN cashback_round_down_to INTEGER NOT NULL DEFAULT 0")


def seed_cards(conn: sqlite3.Connection) -> None:
    if not CARDS_SEED_PATH.exists():
        return
    cards = json.loads(CARDS_SEED_PATH.read_text(encoding="utf-8")).get("cards", [])
    for card in cards:
        statement = card.get("statement", {})
        conn.execute(
            """
            INSERT OR IGNORE INTO cards (
                id, name, statement_type, statement_close_day, cashback_close_day,
                credit_limit, min_total_spend, period_cap, cashback_round_down_to,
                cashback_rules_json, aliases_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card["id"],
                card["name"],
                statement.get("type", "calendar_month"),
                statement.get("close_day"),
                card.get("cashback_close_day"),
                int(card.get("credit_limit", 0)),
                int(card.get("min_total_spend", 0)),
                card.get("period_cap"),
                int(card.get("cashback_round_down_to", 0)),
                json.dumps(card.get("cashback_rules", []), ensure_ascii=False),
                json.dumps(card.get("aliases", []), ensure_ascii=False),
            ),
        )


def seed_transactions(conn: sqlite3.Connection) -> None:
    if not TRANSACTIONS_SEED_PATH.exists():
        return
    txns = json.loads(TRANSACTIONS_SEED_PATH.read_text(encoding="utf-8")).get("transactions", [])
    for txn in txns:
        exists = conn.execute("SELECT 1 FROM cards WHERE id = ?", (txn["card_id"],)).fetchone()
        if not exists:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO transactions (
                id, card_id, transaction_date, amount, merchant_name,
                mcc, payment_method, category, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                txn["id"],
                txn["card_id"],
                txn.get("date"),
                int(txn["amount"]),
                txn.get("merchant"),
                txn.get("mcc"),
                txn.get("channel") or "online",
                txn.get("category"),
                txn.get("note"),
            ),
        )


def list_cards() -> list[dict[str, Any]]:
    init_database()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM cards ORDER BY name").fetchall()
    return [_card_row_to_dict(row) for row in rows]


def list_transactions() -> list[dict[str, Any]]:
    init_database()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM transactions ORDER BY transaction_date, created_at").fetchall()
    return [_transaction_row_to_dict(row) for row in rows]


def get_latest_transaction() -> dict[str, Any] | None:
    init_database()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM transactions ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return _transaction_row_to_dict(row) if row else None


def insert_transaction(payload: dict[str, Any]) -> dict[str, Any]:
    init_database()
    txn_id = payload.get("id") or f"txn-{dt.datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    payment_method = (payload.get("channel") or payload.get("payment_method") or "online").lower()
    if payment_method not in {"online", "pos"}:
        raise ValueError("payment_method/channel must be 'online' or 'pos'")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO transactions (
                id, card_id, transaction_date, amount, merchant_name,
                mcc, payment_method, category, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                txn_id,
                payload["card_id"],
                payload.get("date") or payload.get("transaction_date") or dt.date.today().isoformat(),
                int(payload["amount"]),
                payload.get("merchant") or payload.get("merchant_name"),
                payload.get("mcc"),
                payment_method,
                payload.get("category"),
                payload.get("note"),
            ),
        )
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    return _transaction_row_to_dict(row)


def delete_transaction(transaction_id: str | None = None) -> dict[str, Any]:
    init_database()
    if not transaction_id or transaction_id in {"cuối", "cuoi", "last", "latest", "gần nhất", "gan nhat"}:
        target = get_latest_transaction()
    else:
        with connect() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
        target = _transaction_row_to_dict(row) if row else None
    if not target:
        raise ValueError("transaction not found")
    with connect() as conn:
        conn.execute("DELETE FROM transactions WHERE id = ?", (target["id"],))
    return target


def update_transaction_mcc(transaction_id: str, mcc: str, category: str | None = None) -> dict[str, Any]:
    init_database()
    with connect() as conn:
        conn.execute(
            "UPDATE transactions SET mcc = ?, category = COALESCE(?, category) WHERE id = ?",
            (str(mcc), category, transaction_id),
        )
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if not row:
        raise ValueError("transaction not found")
    return _transaction_row_to_dict(row)


def replace_merchant_mcc(rows: list[dict[str, Any]], source: str = "import") -> int:
    init_database()
    with connect() as conn:
        conn.execute("DELETE FROM merchant_mcc WHERE source = ?", (source,))
        for row in rows:
            merchant_name = str(row.get("merchant_name") or "").strip()
            mcc = str(row.get("mcc") or "").strip()
            if not merchant_name or not mcc:
                continue
            payment_method = normalize_payment_method(row.get("payment_method"))
            conn.execute(
                """
                INSERT INTO merchant_mcc (
                    merchant_full_name, merchant_name, merchant_key, address,
                    amex, payment_method, mcc, category, note, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("merchant_full_name"),
                    merchant_name,
                    normalize_key(merchant_name),
                    row.get("address"),
                    row.get("amex"),
                    payment_method,
                    mcc,
                    row.get("category"),
                    row.get("note"),
                    source,
                ),
            )
        count = conn.execute("SELECT COUNT(*) FROM merchant_mcc WHERE source = ?", (source,)).fetchone()[0]
    return int(count)


def insert_merchant_mcc(payload: dict[str, Any]) -> dict[str, Any]:
    init_database()
    merchant_name = str(payload.get("merchant_name") or payload.get("merchant") or "").strip()
    mcc = str(payload.get("mcc") or "").strip()
    if not merchant_name or not mcc:
        raise ValueError("merchant_name and mcc are required")
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO merchant_mcc (
                merchant_full_name, merchant_name, merchant_key, address,
                amex, payment_method, mcc, category, note, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.get("merchant_full_name") or merchant_name,
                merchant_name,
                normalize_key(merchant_name),
                payload.get("address"),
                payload.get("amex"),
                normalize_payment_method(payload.get("payment_method")),
                mcc,
                payload.get("category"),
                payload.get("note"),
                payload.get("source") or "manual",
            ),
        )
        row = conn.execute("SELECT * FROM merchant_mcc WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _merchant_mcc_row_to_dict(row)


def upsert_merchant_mcc(payload: dict[str, Any]) -> dict[str, Any]:
    init_database()
    merchant_name = str(payload.get("merchant_name") or payload.get("merchant") or "").strip()
    mcc = str(payload.get("mcc") or "").strip()
    method = normalize_payment_method(payload.get("payment_method"))
    if not merchant_name or not mcc:
        raise ValueError("merchant_name and mcc are required")
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM merchant_mcc
            WHERE merchant_key = ? AND payment_method = ?
            ORDER BY id DESC LIMIT 1
            """,
            (normalize_key(merchant_name), method),
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE merchant_mcc
                SET mcc = ?, category = COALESCE(?, category), note = COALESCE(?, note),
                    address = COALESCE(?, address), source = ?
                WHERE id = ?
                """,
                (
                    mcc,
                    payload.get("category"),
                    payload.get("note"),
                    payload.get("address"),
                    payload.get("source") or "manual",
                    row["id"],
                ),
            )
            row = conn.execute("SELECT * FROM merchant_mcc WHERE id = ?", (row["id"],)).fetchone()
        else:
            return insert_merchant_mcc(payload)
    return _merchant_mcc_row_to_dict(row)


def lookup_merchant_mcc(merchant: str | None, payment_method: str | None = None) -> dict[str, Any] | None:
    init_database()
    key = normalize_key(merchant)
    if not key:
        return None
    method = normalize_payment_method(payment_method)
    with connect() as conn:
        if method == "any":
            rows = conn.execute(
                "SELECT * FROM merchant_mcc ORDER BY LENGTH(merchant_key) DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM merchant_mcc
                WHERE payment_method IN (?, 'any')
                ORDER BY LENGTH(merchant_key) DESC
                """,
                (method,),
            ).fetchall()
    best = None
    best_score = 0
    for row in rows:
        merchant_key = row["merchant_key"]
        score = 0
        if key == merchant_key:
            score = 1000 + len(merchant_key)
        elif merchant_key and merchant_key in key:
            score = 500 + len(merchant_key)
        elif key and key in merchant_key:
            score = 250 + len(key)
        if score > best_score:
            best = row
            best_score = score
    return _merchant_mcc_row_to_dict(best) if best is not None else None


def search_merchant_mcc(query: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
    init_database()
    key = normalize_key(query)
    with connect() as conn:
        if key:
            rows = conn.execute(
                """
                SELECT * FROM merchant_mcc
                WHERE merchant_key LIKE ? OR merchant_full_name LIKE ? OR mcc LIKE ?
                ORDER BY merchant_name
                LIMIT ?
                """,
                (f"%{key}%", f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM merchant_mcc ORDER BY merchant_name LIMIT ?",
                (limit,),
            ).fetchall()
    return [_merchant_mcc_row_to_dict(row) for row in rows]


def search_merchant_mcc_by_address(
    address_query: str | None = None,
    mcc: str | None = None,
    payment_method: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    init_database()
    address_key = str(address_query or "").strip()
    method = normalize_payment_method(payment_method)
    clauses = []
    params: list[Any] = []
    if address_key:
        clauses.append("(address LIKE ? OR merchant_full_name LIKE ?)")
        params.extend([f"%{address_key}%", f"%{address_key}%"])
    if mcc:
        clauses.append("mcc = ?")
        params.append(str(mcc))
    if method != "any":
        clauses.append("payment_method IN (?, 'any')")
        params.append(method)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM merchant_mcc {where} ORDER BY merchant_name LIMIT ?",
            params,
        ).fetchall()
    return [_merchant_mcc_row_to_dict(row) for row in rows]


def upsert_card(payload: dict[str, Any]) -> dict[str, Any]:
    init_database()
    statement = payload.get("statement", {})
    card_id = payload.get("id")
    if not card_id:
        raise ValueError("card id is required")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO cards (
                id, name, statement_type, statement_close_day, cashback_close_day,
                credit_limit, min_total_spend, period_cap, cashback_round_down_to,
                cashback_rules_json, aliases_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                statement_type = excluded.statement_type,
                statement_close_day = excluded.statement_close_day,
                cashback_close_day = excluded.cashback_close_day,
                credit_limit = excluded.credit_limit,
                min_total_spend = excluded.min_total_spend,
                period_cap = excluded.period_cap,
                cashback_round_down_to = excluded.cashback_round_down_to,
                cashback_rules_json = excluded.cashback_rules_json,
                aliases_json = excluded.aliases_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                card_id,
                payload["name"],
                payload.get("statement_type") or statement.get("type", "calendar_month"),
                payload.get("statement_close_day") or statement.get("close_day"),
                payload.get("cashback_close_day"),
                int(payload.get("credit_limit", 0)),
                int(payload.get("min_total_spend", 0)),
                payload.get("period_cap"),
                int(payload.get("cashback_round_down_to", 0)),
                json.dumps(payload.get("cashback_rules", []), ensure_ascii=False),
                json.dumps(payload.get("aliases", []), ensure_ascii=False),
            ),
        )
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    return _card_row_to_dict(row)


def schema_summary() -> dict[str, Any]:
    return {
        "database": str(DB_PATH),
        "tables": {
            "cards": [
                "id",
                "name",
                "statement_type",
                "statement_close_day",
                "cashback_close_day",
                "credit_limit",
                "min_total_spend",
                "period_cap",
                "cashback_round_down_to",
                "cashback_rules_json",
                "aliases_json",
            ],
            "transactions": [
                "id",
                "card_id",
                "transaction_date",
                "amount",
                "merchant_name",
                "mcc",
                "payment_method",
                "category",
                "note",
            ],
            "merchant_mcc": [
                "id",
                "merchant_full_name",
                "merchant_name",
                "merchant_key",
                "address",
                "amex",
                "payment_method",
                "mcc",
                "category",
                "note",
                "source",
            ],
        },
    }


def normalize_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def normalize_payment_method(value: Any) -> str:
    method = str(value or "any").strip().lower()
    if method in {"online", "pos"}:
        return method
    return "any"


def _card_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "aliases": json.loads(row["aliases_json"] or "[]"),
        "statement": {
            "type": row["statement_type"],
            "close_day": row["statement_close_day"],
        },
        "cashback_close_day": row["cashback_close_day"],
        "credit_limit": row["credit_limit"],
        "min_total_spend": row["min_total_spend"],
        "period_cap": row["period_cap"],
        "cashback_round_down_to": row["cashback_round_down_to"],
        "cashback_rules": json.loads(row["cashback_rules_json"] or "[]"),
    }


def _transaction_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "card_id": row["card_id"],
        "date": row["transaction_date"],
        "transaction_date": row["transaction_date"],
        "amount": row["amount"],
        "merchant": row["merchant_name"],
        "merchant_name": row["merchant_name"],
        "mcc": row["mcc"],
        "channel": row["payment_method"],
        "payment_method": row["payment_method"],
        "category": row["category"],
        "note": row["note"],
    }


def _merchant_mcc_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "merchant_full_name": row["merchant_full_name"],
        "merchant_name": row["merchant_name"],
        "merchant_key": row["merchant_key"],
        "address": row["address"],
        "amex": row["amex"],
        "payment_method": row["payment_method"],
        "mcc": row["mcc"],
        "category": row["category"],
        "note": row["note"],
        "source": row["source"],
    }
