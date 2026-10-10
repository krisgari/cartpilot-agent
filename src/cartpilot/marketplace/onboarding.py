"""
Wholesaler catalog onboarding: normalise a messy CSV into validated product
records. Pure code; the agent explains the findings, it doesn't do the parsing.
"""
from __future__ import annotations

import csv
import io
import re

ALIASES = {
    "sku": {"sku", "item_code", "code", "id", "product_id"},
    "name": {"name", "product", "description", "item", "product_name"},
    "price": {"price", "unit_price", "cost", "wholesale_price"},
    "stock": {"stock", "qty", "quantity", "on_hand", "inventory"},
    "category": {"category", "dept", "department", "type"},
    "brand": {"brand", "manufacturer", "maker"},
    "unit": {"unit", "size", "uom"},
}


def _canonical(header: str) -> str | None:
    h = re.sub(r"[^a-z0-9_]+", "_", header.strip().lower()).strip("_")
    for canon, names in ALIASES.items():
        if h in names:
            return canon
    return None


def parse_catalog(csv_text: str, wholesaler_id: str) -> dict:
    """Return {"products": {pid: record}, "issues": [str], "rows_seen": int}."""
    reader = csv.reader(io.StringIO(csv_text.strip()))
    try:
        headers = next(reader)
    except StopIteration:
        return {"products": {}, "issues": ["file is empty"], "rows_seen": 0}

    cols = [_canonical(h) for h in headers]
    issues: list[str] = []
    missing = {"sku", "name", "price", "stock"} - {c for c in cols if c}
    if missing:
        return {
            "products": {},
            "issues": [f"missing required column(s): {', '.join(sorted(missing))}"],
            "rows_seen": 0,
        }

    products: dict[str, dict] = {}
    seen_skus: set[str] = set()
    rows_seen = 0
    for line_no, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        rows_seen += 1
        rec = {c: v.strip() for c, v in zip(cols, row) if c}
        sku = rec.get("sku", "")
        if not sku:
            issues.append(f"line {line_no}: missing sku")
            continue
        if sku in seen_skus:
            issues.append(f"line {line_no}: duplicate sku '{sku}' (first occurrence kept)")
            continue
        seen_skus.add(sku)
        if not rec.get("name"):
            issues.append(f"line {line_no}: sku '{sku}' has no name")
            continue
        try:
            price = float(rec["price"].replace("$", "").replace(",", ""))
        except (ValueError, KeyError):
            issues.append(f"line {line_no}: sku '{sku}' has non-numeric price '{rec.get('price')}'")
            continue
        if price <= 0:
            issues.append(f"line {line_no}: sku '{sku}' has non-positive price {price}")
            continue
        try:
            stock = int(float(rec["stock"]))
        except (ValueError, KeyError):
            issues.append(f"line {line_no}: sku '{sku}' has non-numeric stock '{rec.get('stock')}'")
            continue
        if stock < 0:
            issues.append(f"line {line_no}: sku '{sku}' has negative stock {stock}")
            continue

        pid = f"{wholesaler_id}-{sku}"
        products[pid] = {
            "name": rec["name"],
            "category": (rec.get("category") or "uncategorized").lower(),
            "brand": rec.get("brand") or "unknown",
            "price": round(price, 2),
            "unit": rec.get("unit") or "1ea",
            "tags": [],
            "stock": stock,
            "store_id": wholesaler_id,
        }
    return {"products": products, "issues": issues, "rows_seen": rows_seen}
