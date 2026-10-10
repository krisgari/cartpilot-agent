from cartpilot.marketplace.onboarding import parse_catalog

GOOD = "SKU,Product Name,Unit Price,Qty,Dept\nA1,Basmati Rice 5kg,$12.50,40,Grains\nA2,Olive Oil 1L,9.99,12,Oils\n"


def test_aliases_and_money_parsing():
    r = parse_catalog(GOOD, "w1")
    assert set(r["products"]) == {"w1-A1", "w1-A2"}
    assert r["products"]["w1-A1"]["price"] == 12.5 and r["products"]["w1-A1"]["stock"] == 40
    assert r["issues"] == []


def test_bad_rows_reported_good_rows_kept():
    csv = GOOD + "A3,Bad Price,abc,5,Oils\nA4,Neg Stock,3.00,-2,Oils\nA1,Dup,1.00,1,Oils\n,No Sku,1,1,x\n"
    r = parse_catalog(csv, "w1")
    assert set(r["products"]) == {"w1-A1", "w1-A2"}
    assert r["rows_seen"] == 6
    text = " ".join(r["issues"])
    assert "non-numeric price" in text and "negative stock" in text and "duplicate sku" in text and "missing sku" in text


def test_missing_required_columns():
    r = parse_catalog("name,price\nx,1\n", "w1")
    assert r["products"] == {} and "missing required column" in r["issues"][0]


def test_empty_file():
    assert parse_catalog("", "w1")["issues"] == ["file is empty"]
