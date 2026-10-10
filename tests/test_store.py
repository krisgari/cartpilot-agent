import sqlite3
import threading
from pathlib import Path

from cartpilot.agents.tools import build_consumer_tools
from cartpilot.marketplace.store import DuplicateRefund, Store

SEED = Path(__file__).resolve().parents[1] / "data" / "seed.json"


def _store(tmp_path):
    return Store(state_path=tmp_path / "t.db", seed_path=SEED)


def _refund(s, order="o1002", item="i1", by="policy:auto"):
    return s.add_refund(order_id=order, item_id=item, customer_id="c1", amount=6.5, reason="missing_item", approved_by=by)


def test_refund_ledger_is_append_only(tmp_path):
    s = _store(tmp_path)
    _refund(s)
    with s._cx() as c:
        for sql in ("UPDATE refunds SET amount=0", "DELETE FROM refunds"):
            try:
                c.execute(sql)
                raise AssertionError("ledger was mutable")
            except sqlite3.DatabaseError as exc:
                assert "append-only" in str(exc)


def test_audit_log_is_append_only(tmp_path):
    s = _store(tmp_path)
    s.audit("x", "a", {})
    with s._cx() as c:
        try:
            c.execute("DELETE FROM audit_log")
            raise AssertionError("audit log was mutable")
        except sqlite3.DatabaseError as exc:
            assert "append-only" in str(exc)


def test_duplicate_refund_blocked_by_database(tmp_path):
    s = _store(tmp_path)
    _refund(s)
    try:
        _refund(s)
        raise AssertionError("duplicate refund accepted")
    except DuplicateRefund:
        pass
    assert len(s.refunds()) == 1


def test_concurrent_refund_requests_issue_exactly_one(tmp_path):
    s = _store(tmp_path)
    results = []

    def worker():
        results.append(build_consumer_tools(s, "c1")["request_refund"]("o1002", "i1", "missing_item")["status"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("approved") == 1 and results.count("rejected") == 7
    assert len(s.refunds()) == 1


def test_transaction_rolls_back_on_error(tmp_path):
    s = _store(tmp_path)
    try:
        with s.tx():
            _refund(s)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert s.refunds() == []


def test_stock_cannot_go_negative(tmp_path):
    s = _store(tmp_path)
    try:
        with s._cx() as c:
            c.execute("UPDATE products SET stock = stock - 1 WHERE id='p100'")  # p100 has 0
        raise AssertionError("negative stock accepted")
    except sqlite3.IntegrityError:
        pass


def test_refund_escalation_deduplicated_while_open(tmp_path):
    s = _store(tmp_path)
    t = build_consumer_tools(s, "c1")
    t["request_refund"]("o1003", "i1", "damaged")
    t["request_refund"]("o1003", "i1", "damaged")
    assert len(s.escalations()) == 1


def test_approving_escalation_issues_refund_once(tmp_path):
    s = _store(tmp_path)
    build_consumer_tools(s, "c1")["request_refund"]("o1003", "i1", "damaged")
    esc = s.escalations()[0]
    assert esc["kind"] == "refund_approval" and esc["status"] == "open"
    r = s.resolve_escalation(esc["id"], "ops:maria", approve=True)
    assert r["status"] == "approved" and r["amount"] == 62.0
    assert s.refunds()[0]["approved_by"] == "ops:maria"
    again = s.resolve_escalation(esc["id"], "ops:maria", approve=True)
    assert "error" in again and len(s.refunds()) == 1


def test_rejecting_escalation_issues_no_refund(tmp_path):
    s = _store(tmp_path)
    build_consumer_tools(s, "c1")["request_refund"]("o1003", "i1", "damaged")
    r = s.resolve_escalation(s.escalations()[0]["id"], "ops:maria", approve=False)
    assert r["status"] == "rejected" and s.refunds() == []


def test_every_decision_is_audited(tmp_path):
    s = _store(tmp_path)
    t = build_consumer_tools(s, "c1")
    t["request_refund"]("o1002", "i1", "missing_item")   # approved
    t["request_refund"]("o1002", "i2", "missing_item")   # rejected (item was delivered)
    decisions = [a for a in s.audit_entries() if a["action"] == "refund_decision"]
    assert [d["detail"]["status"] for d in decisions] == ["approved", "rejected"]
    assert all(d["actor"] == "consumer:c1" for d in decisions)
