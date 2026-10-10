"""
Deterministic substitution ranking. Hard constraints (stock, dietary needs)
are filters, never scores, so the model can't be talked into violating them.
The LLM's job is to explain the ranked options to the customer.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    product_id: str
    name: str
    price: float
    score: float
    why: list[str]


def rank_substitutes(
    *,
    original: dict,
    original_id: str,
    catalog: dict[str, dict],
    dietary: list[str],
    accepted_brands: list[str],
    top_n: int = 3,
) -> list[Candidate]:
    out: list[Candidate] = []
    for pid, p in catalog.items():
        if pid == original_id:
            continue
        if p["category"] != original["category"]:
            continue
        if p["stock"] <= 0:
            continue
        # Hard filter: every dietary requirement must be satisfied.
        if any(need not in p.get("tags", []) for need in dietary):
            continue

        score, why = 0.0, ["same category", "in stock"]
        if p["brand"] == original["brand"]:
            score += 2
            why.append("same brand")
        elif p["brand"] in accepted_brands:
            score += 1
            why.append("brand customer has accepted before")
        gap = abs(p["price"] - original["price"]) / original["price"]
        if gap <= 0.25:
            score += 2
            why.append("price within 25%")
        else:
            score -= 1
            why.append("price differs by more than 25%")
        if p["unit"] == original["unit"]:
            score += 1
            why.append("same size")
        out.append(Candidate(pid, p["name"], p["price"], score, why))

    out.sort(key=lambda c: (-c.score, c.price, c.product_id))
    return out[:top_n]
