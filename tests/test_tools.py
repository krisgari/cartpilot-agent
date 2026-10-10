import shutil
from pathlib import Path

from cartpilot.agents.tools import build_consumer_tools, build_wholesaler_tools
from cartpilot.marketplace.store import Store

SEED = Path(__file__).resolve().parents[1] / "data" / "seed.json"


def _store(tmp_path):
    return Store(state_path=tmp_path / "state.json", seed_path=SEED)


def test_customer_cannot_read_anothers_order(tmp_path):
    t = build_consumer_tools(_store(tmp_path), "c1")
    assert "error" in t["get_my_order"]("o2001")
    # Someone else's order and a nonexistent order are indistinguishable (no probing for valid ids).
    other = t["get_my_order"]("o2001")["error"].replace("o2001", "X")
    missing = t["get_my_order"]("o9999")["error"].replace("o9999", "X")
    assert other == missing


def test_refund_end_to_end_and_idempotent(tmp_path):
    s = _store(tmp_path)
    t = build_consumer_tools(s, "c1")
    first = t["request_refund"]("o1002", "i1", "missing_item")
    assert first["status"] == "approved" and first["amount"] == 6.5
    assert len(s.refunds()) == 1
    second = t["request_refund"]("o1002", "i1", "missing_item")
    assert second["status"] == "rejected" and len(s.refunds()) == 1  # no double refund


def test_big_refund_escalates_not_refunds(tmp_path):
    s = _store(tmp_path)
    r = build_consumer_tools(s, "c1")["request_refund"]("o1003", "i1", "damaged")
    assert r["status"] == "needs_approval"
    assert s.refunds() == [] and len(s.escalations()) == 1


def test_substitution_only_from_ranked_candidates(tmp_path):
    s = _store(tmp_path)
    t = build_consumer_tools(s, "c1")  # c1 is vegan
    bad = t["apply_substitution"]("o1001", "i1", "p103")  # whole milk: not vegan
    assert "error" in bad and s.substitutions() == []
    good = t["apply_substitution"]("o1001", "i1", "p104")
    assert good["status"] == "applied" and len(s.substitutions()) == 1
    assert s.get_order("o1001")["items"]["i1"]["product_id"] == "p104"


def test_substitution_rejected_for_in_stock_item(tmp_path):
    t = build_consumer_tools(_store(tmp_path), "c1")
    assert "error" in t["suggest_substitutions"]("o1001", "i2")


def test_wholesaler_import(tmp_path):
    s = _store(tmp_path)
    csv = "sku,name,price,qty\nA1,Rice,12.5,40\nA2,Oil,bad,3\n"
    r = build_wholesaler_tools(s, "w1")["import_catalog"](csv)
    assert r["imported"] == 1 and r["rejected"] == 1 and s.get_product("w1-A1")["price"] == 12.5
    assert s.get_product("w1-A2") is None
