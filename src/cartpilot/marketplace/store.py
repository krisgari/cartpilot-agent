"""
SQLite-backed marketplace store (stand-in for the real order / inventory / payments systems).

Guarantees enforced BY THE DATABASE, not by application code:
  * refunds are append-only (triggers reject UPDATE / DELETE) and UNIQUE(order_id, item_id),
    so a double refund is impossible even under a race or a retry;
  * audit_log is append-only;
  * stock can never go negative (CHECK constraint);
  * multi-step writes run in a BEGIN IMMEDIATE transaction (`with store.tx():`).

One short-lived connection per operation (SQLite handles this well and it is thread-safe for
FastAPI's threadpool); WAL mode + busy_timeout for concurrent readers/writers. For multi-node
production, swap this module for Postgres with the same method surface.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cartpilot.config import settings

SCHEMA = """
CREATE TABLE customers   (id TEXT PRIMARY KEY, name TEXT NOT NULL, dietary TEXT NOT NULL, accepted_brands TEXT NOT NULL);
CREATE TABLE products    (id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, brand TEXT NOT NULL,
                          price REAL NOT NULL CHECK (price > 0), unit TEXT NOT NULL, tags TEXT NOT NULL,
                          stock INTEGER NOT NULL CHECK (stock >= 0), store_id TEXT NOT NULL);
CREATE TABLE orders      (id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, store_id TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE order_items (order_id TEXT NOT NULL, item_id TEXT NOT NULL, product_id TEXT NOT NULL,
                          qty INTEGER NOT NULL CHECK (qty > 0), status TEXT NOT NULL, PRIMARY KEY (order_id, item_id));
CREATE TABLE refunds     (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, item_id TEXT NOT NULL,
                          customer_id TEXT NOT NULL, amount REAL NOT NULL, reason TEXT NOT NULL,
                          status TEXT NOT NULL, approved_by TEXT NOT NULL, ts TEXT NOT NULL,
                          UNIQUE (order_id, item_id));
CREATE TABLE escalations (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, order_id TEXT,
                          kind TEXT NOT NULL, summary TEXT NOT NULL, payload TEXT NOT NULL,
                          status TEXT NOT NULL DEFAULT 'open', resolved_by TEXT, resolved_at TEXT, ts TEXT NOT NULL);
CREATE TABLE substitutions (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, item_id TEXT NOT NULL,
                          from_product TEXT NOT NULL, to_product TEXT NOT NULL, ts TEXT NOT NULL);
CREATE TABLE audit_log   (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
                          action TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TRIGGER refunds_no_update  BEFORE UPDATE ON refunds   BEGIN SELECT RAISE(ABORT, 'refunds are append-only'); END;
CREATE TRIGGER refunds_no_delete  BEFORE DELETE ON refunds   BEGIN SELECT RAISE(ABORT, 'refunds are append-only'); END;
CREATE TRIGGER audit_no_update    BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_no_delete    BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


class DuplicateRefund(Exception):
    """A refund already exists for this order line (enforced by a UNIQUE constraint)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ref(n: int) -> str:
    return f"ESC-{n:04d}"


class Store:
    def __init__(self, state_path: Path | None = None, seed_path: Path | None = None):
        self.db_path = Path(state_path or settings.state_path)
        self.seed_path = Path(seed_path or settings.seed_path)
        self._local = threading.local()
        if not self.db_path.exists():
            self.reset()

    # --- connection / transaction plumbing --------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)  # autocommit; we BEGIN explicitly
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def _cx(self):
        tx_conn = getattr(self._local, "tx_conn", None)
        if tx_conn is not None:
            yield tx_conn
            return
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self):
        """Run several statements atomically; takes the write lock up front (no read-then-write races)."""
        if getattr(self._local, "tx_conn", None) is not None:  # already inside a tx: join it
            yield
            return
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        self._local.tx_conn = conn
        try:
            yield
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            self._local.tx_conn = None
            conn.close()

    # --- lifecycle ---------------------------------------------------------------------
    def reset(self) -> None:
        """Rebuild the database from the seed file (used by the eval harness and tests)."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)
        seed = json.loads(self.seed_path.read_text())
        with self._cx() as c:
            c.executescript(SCHEMA)
            for cid, v in seed["customers"].items():
                c.execute("INSERT INTO customers VALUES (?,?,?,?)",
                          (cid, v["name"], json.dumps(v["dietary"]), json.dumps(v["accepted_brands"])))
            for pid, p in seed["products"].items():
                c.execute("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?)",
                          (pid, p["name"], p["category"], p["brand"], p["price"], p["unit"],
                           json.dumps(p["tags"]), p["stock"], p["store_id"]))
            for oid, o in seed["orders"].items():
                c.execute("INSERT INTO orders VALUES (?,?,?,?)", (oid, o["customer_id"], o["store_id"], o["status"]))
                for iid, it in o["items"].items():
                    c.execute("INSERT INTO order_items VALUES (?,?,?,?,?)",
                              (oid, iid, it["product_id"], it["qty"], it["status"]))

    # --- reads ---------------------------------------------------------------------------
    @staticmethod
    def _product(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["tags"] = json.loads(d["tags"])
        return d

    def get_customer(self, customer_id: str) -> dict | None:
        with self._cx() as c:
            r = c.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if r is None:
            return None
        return {"name": r["name"], "dietary": json.loads(r["dietary"]), "accepted_brands": json.loads(r["accepted_brands"])}

    def get_product(self, product_id: str) -> dict | None:
        with self._cx() as c:
            r = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        return self._product(r) if r else None

    def get_order(self, order_id: str) -> dict | None:
        with self._cx() as c:
            o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if o is None:
                return None
            items = c.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY item_id", (order_id,)).fetchall()
        return {
            "customer_id": o["customer_id"], "store_id": o["store_id"], "status": o["status"],
            "items": {i["item_id"]: {"product_id": i["product_id"], "qty": i["qty"], "status": i["status"]} for i in items},
        }

    def products_in_store(self, store_id: str) -> dict[str, dict]:
        with self._cx() as c:
            rows = c.execute("SELECT * FROM products WHERE store_id=?", (store_id,)).fetchall()
        return {r["id"]: self._product(r) for r in rows}

    def refunds(self) -> list[dict]:
        with self._cx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM refunds ORDER BY id").fetchall()]

    def substitutions(self) -> list[dict]:
        with self._cx() as c:
            rows = c.execute("SELECT * FROM substitutions ORDER BY id").fetchall()
        return [{"order_id": r["order_id"], "item_id": r["item_id"], "from": r["from_product"],
                 "to": r["to_product"], "ts": r["ts"]} for r in rows]

    def escalations(self, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM escalations", ()
        if status:
            sql, args = sql + " WHERE status=?", (status,)
        with self._cx() as c:
            rows = c.execute(sql + " ORDER BY id", args).fetchall()
        return [self._esc(r) for r in rows]

    def get_escalation(self, esc_id: int) -> dict | None:
        with self._cx() as c:
            r = c.execute("SELECT * FROM escalations WHERE id=?", (esc_id,)).fetchone()
        return self._esc(r) if r else None

    @staticmethod
    def _esc(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["ref"] = _ref(d["id"])
        return d

    def audit_entries(self) -> list[dict]:
        with self._cx() as c:
            rows = c.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    # --- writes --------------------------------------------------------------------------
    def audit(self, actor: str, action: str, detail: dict) -> None:
        with self._cx() as c:
            c.execute("INSERT INTO audit_log (ts, actor, action, detail) VALUES (?,?,?,?)",
                      (_now(), actor, action, json.dumps(detail, default=str)))

    def add_refund(self, *, order_id: str, item_id: str, customer_id: str, amount: float, reason: str,
                   approved_by: str, status: str = "approved", ts: str | None = None) -> dict:
        try:
            with self._cx() as c:
                c.execute(
                    "INSERT INTO refunds (order_id,item_id,customer_id,amount,reason,status,approved_by,ts) VALUES (?,?,?,?,?,?,?,?)",
                    (order_id, item_id, customer_id, amount, reason, status, approved_by, ts or _now()))
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                raise DuplicateRefund(f"{order_id}/{item_id} already refunded") from exc
            raise
        self.audit(approved_by, "refund_issued", {"order_id": order_id, "item_id": item_id, "amount": amount, "reason": reason})
        return {"order_id": order_id, "item_id": item_id, "amount": amount}

    def add_escalation(self, customer_id: str, summary: str, order_id: str | None = None,
                       kind: str = "general", payload: dict | None = None) -> dict:
        """Refund-approval escalations are de-duplicated per order line while one is open."""
        with self.tx():
            with self._cx() as c:
                if kind == "refund_approval" and payload:
                    for r in c.execute("SELECT * FROM escalations WHERE kind='refund_approval' AND status='open' AND order_id=?",
                                       (order_id,)).fetchall():
                        if json.loads(r["payload"]).get("item_id") == payload.get("item_id"):
                            return self._esc(r)
                cur = c.execute(
                    "INSERT INTO escalations (customer_id, order_id, kind, summary, payload, ts) VALUES (?,?,?,?,?,?)",
                    (customer_id, order_id, kind, summary, json.dumps(payload or {}), _now()))
                new_id = cur.lastrowid
            self.audit("system", "escalation_created", {"id": _ref(new_id), "kind": kind, "order_id": order_id})
        return self.get_escalation(new_id)

    def resolve_escalation(self, esc_id: int, resolver: str, approve: bool) -> dict:
        """Human decision on an escalation. Approving a refund escalation issues the refund (once)."""
        with self.tx():
            esc = self.get_escalation(esc_id)
            if esc is None:
                return {"error": f"escalation {_ref(esc_id)} not found"}
            if esc["status"] != "open":
                return {"error": f"escalation {_ref(esc_id)} is already {esc['status']}"}
            result: dict = {}
            if approve and esc["kind"] == "refund_approval":
                p = esc["payload"]
                try:
                    result = self.add_refund(order_id=p["order_id"], item_id=p["item_id"], customer_id=esc["customer_id"],
                                             amount=p["amount"], reason=p["reason"], approved_by=resolver)
                except DuplicateRefund:
                    return {"error": "this item was already refunded"}
            new_status = "approved" if approve else "rejected"
            with self._cx() as c:
                c.execute("UPDATE escalations SET status=?, resolved_by=?, resolved_at=? WHERE id=?",
                          (new_status, resolver, _now(), esc_id))
            self.audit(resolver, f"escalation_{new_status}", {"id": _ref(esc_id)})
        return {"status": new_status, "ref": _ref(esc_id), **result}

    def apply_substitution(self, order_id: str, item_id: str, product_id: str) -> dict:
        with self.tx():
            with self._cx() as c:
                item = c.execute("SELECT * FROM order_items WHERE order_id=? AND item_id=?", (order_id, item_id)).fetchone()
                original = item["product_id"]
                c.execute("UPDATE products SET stock = stock - ? WHERE id=?", (item["qty"], product_id))  # CHECK blocks <0
                c.execute("UPDATE order_items SET product_id=?, status='substituted' WHERE order_id=? AND item_id=?",
                          (product_id, order_id, item_id))
                c.execute("INSERT INTO substitutions (order_id,item_id,from_product,to_product,ts) VALUES (?,?,?,?,?)",
                          (order_id, item_id, original, product_id, _now()))
            self.audit("system", "substitution_applied", {"order_id": order_id, "item_id": item_id, "from": original, "to": product_id})
        return {"order_id": order_id, "item_id": item_id, "from": original, "to": product_id}

    def upsert_products(self, products: dict[str, dict]) -> int:
        with self.tx():
            with self._cx() as c:
                for pid, p in products.items():
                    c.execute(
                        "INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                        "name=excluded.name, category=excluded.category, brand=excluded.brand, price=excluded.price, "
                        "unit=excluded.unit, tags=excluded.tags, stock=excluded.stock, store_id=excluded.store_id",
                        (pid, p["name"], p["category"], p["brand"], p["price"], p["unit"], json.dumps(p["tags"]),
                         p["stock"], p["store_id"]))
        return len(products)
