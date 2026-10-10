"""
Refund policy engine. Money decisions live HERE, in plain deterministic code
with explicit limits, not in a prompt. The LLM can only *request* a refund;
this module decides whether it is approved, needs a human, or is rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cartpilot.config import settings

VALID_REASONS = {"missing_item", "damaged", "wrong_item", "late"}
# Item statuses for which a refund claim is plausible, per reason.
CONSISTENT_STATUS = {
    "missing_item": {"missing"},
    "damaged": {"damaged"},
    "wrong_item": {"wrong_item", "substituted"},
    "late": {"delivered", "late"},
}


@dataclass(frozen=True)
class Decision:
    status: str  # "approved" | "needs_approval" | "rejected"
    reason: str
    amount: float = 0.0


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def decide_refund(
    *,
    order: dict,
    item_id: str,
    reason: str,
    unit_price: float,
    customer_id: str,
    prior_refunds: list[dict],
    now: datetime | None = None,
    auto_limit: float | None = None,
    max_auto_30d: int | None = None,
) -> Decision:
    auto_limit = settings.auto_refund_limit if auto_limit is None else auto_limit
    max_auto_30d = settings.max_auto_refunds_30d if max_auto_30d is None else max_auto_30d
    now = now or datetime.now(timezone.utc)

    if reason not in VALID_REASONS:
        return Decision("rejected", f"unknown reason '{reason}'")
    if order.get("customer_id") != customer_id:
        return Decision("rejected", "order does not belong to this customer")
    item = order.get("items", {}).get(item_id)
    if item is None:
        return Decision("rejected", f"item '{item_id}' not found on order")
    if item["status"] not in CONSISTENT_STATUS[reason]:
        return Decision(
            "rejected",
            f"item status '{item['status']}' is not consistent with claim '{reason}'",
        )

    amount = round(unit_price * item["qty"], 2)

    # Idempotency: one refund per order line, ever.
    for r in prior_refunds:
        if r.get("order_id") == order.get("id") and r.get("item_id") == item_id:
            return Decision("rejected", "this item was already refunded", amount)

    recent = [
        r for r in prior_refunds
        if r.get("customer_id") == customer_id
        and r.get("status") == "approved"
        and _parse(r["ts"]) >= now - timedelta(days=30)
    ]
    if amount > auto_limit:
        return Decision("needs_approval", f"amount ${amount:.2f} exceeds auto-approve limit ${auto_limit:.2f}", amount)
    if len(recent) >= max_auto_30d:
        return Decision("needs_approval", f"{len(recent)} refunds in last 30 days (limit {max_auto_30d})", amount)
    return Decision("approved", "within policy", amount)
