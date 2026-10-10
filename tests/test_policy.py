from datetime import datetime, timedelta, timezone

from cartpilot.marketplace.policy import decide_refund

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def _order(status="missing", customer="c1", qty=1):
    return {"id": "o1", "customer_id": customer, "items": {"i1": {"product_id": "p1", "qty": qty, "status": status}}}


def _decide(order=None, reason="missing_item", price=6.5, prior=None, customer="c1"):
    return decide_refund(order=order or _order(), item_id="i1", reason=reason, unit_price=price,
                         customer_id=customer, prior_refunds=prior or [], now=NOW,
                         auto_limit=25, max_auto_30d=3)


def test_small_valid_refund_auto_approved():
    d = _decide()
    assert d.status == "approved" and d.amount == 6.5


def test_over_limit_needs_human():
    d = _decide(order=_order("damaged"), reason="damaged", price=62.0)
    assert d.status == "needs_approval" and d.amount == 62.0


def test_quantity_counts_toward_amount():
    d = _decide(order=_order(qty=5), price=6.0)  # $30 > $25
    assert d.status == "needs_approval"


def test_claim_inconsistent_with_item_status_rejected():
    d = _decide(order=_order("delivered"), reason="missing_item")
    assert d.status == "rejected"


def test_other_customers_order_rejected():
    d = _decide(order=_order(customer="c2"))
    assert d.status == "rejected" and "does not belong" in d.reason


def test_duplicate_refund_rejected():
    prior = [{"order_id": "o1", "item_id": "i1", "customer_id": "c1", "status": "approved", "ts": NOW.isoformat()}]
    assert _decide(prior=prior).status == "rejected"


def test_frequent_refunder_goes_to_human():
    ts = (NOW - timedelta(days=5)).isoformat()
    prior = [{"order_id": f"x{n}", "item_id": "i1", "customer_id": "c1", "status": "approved", "ts": ts} for n in range(3)]
    d = _decide(prior=prior)
    assert d.status == "needs_approval" and "last 30 days" in d.reason


def test_old_refunds_dont_count():
    ts = (NOW - timedelta(days=45)).isoformat()
    prior = [{"order_id": f"x{n}", "item_id": "i1", "customer_id": "c1", "status": "approved", "ts": ts} for n in range(5)]
    assert _decide(prior=prior).status == "approved"


def test_unknown_reason_and_item_rejected():
    assert _decide(reason="because").status == "rejected"
    d = decide_refund(order=_order(), item_id="zz", reason="missing_item", unit_price=1, customer_id="c1",
                      prior_refunds=[], now=NOW)
    assert d.status == "rejected"
