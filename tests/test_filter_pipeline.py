import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from product_filter import filter_products, StructuredQuery
from llm_service import _build_product_text, _coerce_llm_filters
from variant_resolver import _find_product_by_ref

# Mock data
MOCK_PRODUCTS = [
    {
        "id": "1",
        "title": "Pullover Sweatshirt",
        "handle": "pullover-sweatshirt",
        "productType": "Sweatshirt",
        "price": "50.00",
        "collections": [{"handle": "winter", "title": "Winter"}]
    },
    {
        "id": "2",
        "title": "Classic T-Shirt",
        "handle": "classic-tee",
        "productType": "Shirt",
        "price": "25.00",
        "collections": [{"handle": "summer", "title": "Summer"}]
    },
    {
        "id": "3",
        "title": "Linen Shirt",
        "handle": "linen-shirt",
        "productType": "Shirt",
        "price": "45.00",
        "collections": [{"handle": "summer", "title": "Summer"}]
    },
    {
        "id": "4",
        "title": "Leather Jacket",
        "handle": "leather-jacket",
        "productType": "Jacket",
        "price": "150.00",
        "collections": [{"handle": "winter", "title": "Winter"}, {"handle": "sale", "title": "Sale"}]
    },
    {
        "id": "5",
        "title": "Running Trainers",
        "handle": "running-trainers",
        "productType": "Shoes",
        "price": "80.00",
        "collections": [{"handle": "activewear", "title": "Activewear"}]
    },
]

def test_collection_exact_match():
    # Phase 1b: test collection filter matches handle
    filters = {"collection_handle": "summer"}
    results = filter_products(MOCK_PRODUCTS, filters)
    assert len(results) == 2
    assert {p["handle"] for p in results} == {"classic-tee", "linen-shirt"}

def test_collection_fallback_empty():
    # Phase 1b: test non-existent collection returns empty, not full catalog
    filters = {"collection_handle": "non-existent"}
    results = filter_products(MOCK_PRODUCTS, filters)
    assert len(results) == 0

def test_build_product_text_scoring():
    # Phase 2a: _build_product_text scores query and returns most relevant
    # The MOCK_PRODUCTS list has 5 items. If we limit to 1 (by modifying the test function momentarily for the test or just observing order)
    # Actually _build_product_text just returns a string. Let's verify the string contains the right item first.
    # To test ordering, we can observe the lines.
    
    query = "running trainers shoes"
    text = _build_product_text(MOCK_PRODUCTS, query=query)
    lines = text.split("\n")
    # "Running Trainers" should be the first item due to scoring
    assert "running-trainers" in lines[0]

def test_find_product_by_ref_handle():
    # Phase 2b: resolves handle exactly
    # "classic-tee" should resolve to Classic T-Shirt, not something else.
    matched = _find_product_by_ref(MOCK_PRODUCTS, "classic-tee")
    assert matched is not None
    assert matched["id"] == "2"

def test_synonym_expansion():
    # Phase 3b: "hoodie" matches "Pullover Sweatshirt"
    filters = {"product_name": "hoodie"}
    results = filter_products(MOCK_PRODUCTS, filters)
    assert len(results) == 1
    assert results[0]["handle"] == "pullover-sweatshirt"
    
    # "sneakers" matches "Running Trainers"
    filters = {"product_name": "sneakers"}
    results = filter_products(MOCK_PRODUCTS, filters)
    assert len(results) == 1
    assert results[0]["handle"] == "running-trainers"

def test_coerce_llm_filters():
    # Phase 4b: strips currency symbols and converts null strings
    raw = {
        "max_price": "$50.00",
        "min_price": "10 USD",
        "color": "null",
        "size": "",
        "product_type": "None"
    }
    coerced = _coerce_llm_filters(raw)
    assert coerced["max_price"] == 50.0
    assert coerced["min_price"] == 10.0
    assert coerced["color"] is None
    assert coerced["size"] is None
    assert coerced["product_type"] is None

def test_structured_query_round_trip():
    # Phase 4a
    sq = StructuredQuery(
        intent="filter_products",
        product_keywords=["blue", "shirt"],
        color="blue",
        price_max=50.0,
        collection_handle="summer"
    )
    d = sq.to_filter_dict()
    assert d["product_name"] == "blue, shirt"
    assert d["color"] == "blue"
    assert d["max_price"] == 50.0
    assert d["collection_handle"] == "summer"
    assert d["size"] is None
