"""
Agent tools, built PER REQUEST and bound to the authenticated actor.

Identity is closed over, not passed by the model: a consumer's tools can only
ever touch that consumer's orders, no matter what the model (or a prompt
injection) tries to ask for. Every tool returns a plain dict and never raises
to the model; errors come back as {"error": "..."} so the model can react.

Side effects are guarded by deterministic code:
  - request_refund -> policy engine decides (approve / needs approval / reject)
  - apply_substitution -> only allowed for a candidate the ranker produced
"""
from __future__ import annotations

import sqlite3
from typing import Callable

from cartpilot.marketplace import onboarding, policy, substitution
from cartpilot.marketplace.store import DuplicateRefund, Store


def _owned_order(store: Store, customer_id: str, order_id: str) -> tuple[dict | None, dict | None]:
    order = store.get_order(order_id)
    # Same message whether it doesn't exist or belongs to someone else: no probing.
    if order is None or order["customer_id"] != customer_id:
        return None, {"error": f"order '{order_id}' not found for this customer"}
    return {**order, "id": order_id}, None


def build_consumer_tools(store: Store, customer_id: str) -> dict[str, Callable]:
    def get_my_order(order_id: str) -> dict:
        """Look up one of the signed-in customer's orders: items, statuses, prices."""
        order, err = _owned_order(store, customer_id, order_id)
        if err:
            return err
        items = {}
        for iid, it in order["items"].items():
            p = store.get_product(it["product_id"]) or {}
            items[iid] = {**it, "name": p.get("name"), "unit_price": p.get("price")}
        return {"order_id": order_id, "status": order["status"], "items": items}

    def suggest_substitutions(order_id: str, item_id: str) -> dict:
        """Ranked in-stock substitutes for an order item that is out of stock. Respects dietary needs."""
        order, err = _owned_order(store, customer_id, order_id)
        if err:
            return err
        item = order["items"].get(item_id)
        if item is None:
            return {"error": f"item '{item_id}' not on order"}
        if item["status"] != "out_of_stock":
            return {"error": f"item status is '{item['status']}', substitution only applies to out_of_stock items"}
        customer = store.get_customer(customer_id) or {}
        original = store.get_product(item["product_id"])
        cands = substitution.rank_substitutes(
            original=original,
            original_id=item["product_id"],
            catalog=store.products_in_store(order["store_id"]),
            dietary=customer.get("dietary", []),
            accepted_brands=customer.get("accepted_brands", []),
        )
        return {
            "original": original["name"],
            "candidates": [
                {"product_id": c.product_id, "name": c.name, "price": c.price, "score": c.score, "why": c.why}
                for c in cands
            ],
        }

    def apply_substitution(order_id: str, item_id: str, product_id: str) -> dict:
        """Apply a substitution. Only allowed for a product returned by suggest_substitutions."""
        suggestion = suggest_substitutions(order_id, item_id)
        if "error" in suggestion:
            return suggestion
        allowed = {c["product_id"] for c in suggestion["candidates"]}
        if product_id not in allowed:
            return {"error": f"'{product_id}' is not an approved substitute for this item", "allowed": sorted(allowed)}
        try:
            rec = store.apply_substitution(order_id, item_id, product_id)
        except sqlite3.IntegrityError:  # CHECK (stock >= 0): someone else took the last unit
            return {"error": "that product just went out of stock; ask for new suggestions"}
        return {"status": "applied", **rec}

    def request_refund(order_id: str, item_id: str, reason: str) -> dict:
        """Request a refund for one order item. reason: missing_item | damaged | wrong_item | late.
        The policy engine decides; the result says approved, needs_approval, or rejected."""
        order, err = _owned_order(store, customer_id, order_id)
        if err:
            return err
        item = order["items"].get(item_id)
        if item is None:
            return {"error": f"item '{item_id}' not on order"}
        product = store.get_product(item["product_id"]) or {}
        actor = f"consumer:{customer_id}"
        # Decide and write inside ONE transaction holding the write lock, so two concurrent requests
        # cannot both pass the policy check. The UNIQUE constraint is the final backstop.
        with store.tx():
            decision = policy.decide_refund(
                order=order,
                item_id=item_id,
                reason=reason,
                unit_price=product.get("price", 0.0),
                customer_id=customer_id,
                prior_refunds=store.refunds(),
            )
            status, why = decision.status, decision.reason
            if status == "approved":
                try:
                    store.add_refund(order_id=order_id, item_id=item_id, customer_id=customer_id,
                                     amount=decision.amount, reason=reason, approved_by="policy:auto")
                except DuplicateRefund:
                    status, why = "rejected", "this item was already refunded"
            elif status == "needs_approval":
                store.add_escalation(
                    customer_id,
                    f"Refund ${decision.amount:.2f} for {order_id}/{item_id} ({reason}) needs approval: {decision.reason}",
                    order_id, kind="refund_approval",
                    payload={"order_id": order_id, "item_id": item_id, "reason": reason, "amount": decision.amount},
                )
            store.audit(actor, "refund_decision",
                        {"order_id": order_id, "item_id": item_id, "reason": reason, "status": status, "why": why})
        return {"status": status, "reason": why, "amount": decision.amount}

    def escalate_to_human(summary: str, order_id: str | None = None) -> dict:
        """Hand the case to a human agent with a short summary."""
        rec = store.add_escalation(customer_id, summary, order_id, kind="general")
        return {"status": "escalated", "id": rec["ref"]}

    return {f.__name__: f for f in (
        get_my_order, suggest_substitutions, apply_substitution, request_refund, escalate_to_human,
    )}


def build_wholesaler_tools(store: Store, wholesaler_id: str) -> dict[str, Callable]:
    def import_catalog(csv_text: str) -> dict:
        """Validate and import a wholesaler product CSV. Valid rows are imported; invalid rows are reported."""
        parsed = onboarding.parse_catalog(csv_text, wholesaler_id)
        imported = store.upsert_products(parsed["products"]) if parsed["products"] else 0
        store.audit(f"wholesaler:{wholesaler_id}", "catalog_import",
                    {"rows_seen": parsed["rows_seen"], "imported": imported, "issues": len(parsed["issues"])})
        return {
            "status": "imported" if imported else "nothing_imported",
            "rows_seen": parsed["rows_seen"],
            "imported": imported,
            "rejected": parsed["rows_seen"] - imported,
            "issues": parsed["issues"],
        }

    return {"import_catalog": import_catalog}
