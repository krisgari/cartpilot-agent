import json
from pathlib import Path

from cartpilot.marketplace.substitution import rank_substitutes

SEED = json.loads((Path(__file__).resolve().parents[1] / "data" / "seed.json").read_text())
CATALOG = SEED["products"]


def _rank(dietary, accepted=()):
    return rank_substitutes(original=CATALOG["p100"], original_id="p100", catalog=CATALOG,
                            dietary=list(dietary), accepted_brands=list(accepted))


def test_dietary_is_a_hard_filter_not_a_score():
    ids = {c.product_id for c in _rank(["vegan"])}
    assert "p103" not in ids  # whole milk is not vegan
    assert {"p101", "p102", "p104"} <= ids


def test_nut_free_excludes_almond():
    ids = {c.product_id for c in _rank(["nut-free"])}
    assert "p102" not in ids


def test_both_restrictions_applied_together():
    ids = {c.product_id for c in _rank(["vegan", "nut-free"])}
    assert ids == {"p101", "p104"}


def test_out_of_stock_and_original_excluded():
    ids = {c.product_id for c in _rank([])}
    assert "p100" not in ids


def test_same_brand_ranks_first():
    assert _rank(["vegan"])[0].product_id == "p104"  # OatCo Barista: same brand, price within 25%


def test_result_is_deterministic_and_capped():
    a, b = _rank(["vegan"]), _rank(["vegan"])
    assert [c.product_id for c in a] == [c.product_id for c in b]
    assert len(a) <= 3
