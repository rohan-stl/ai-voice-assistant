import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shopify_service import (
    extract_variant_nodes,
    normalize_shopify_variant,
    determine_product_availability,
)
from variant_resolver import _is_variant_in_stock, _get_product_options_summary


class TestVariantAvailabilityV218(unittest.TestCase):

    def test_extract_variant_nodes_graphql_formats(self):
        nodes_input = {"nodes": [{"id": "gid://1", "title": "Node 1"}]}
        self.assertEqual(extract_variant_nodes(nodes_input), [{"id": "gid://1", "title": "Node 1"}])

        edges_input = {"edges": [{"node": {"id": "gid://2", "title": "Node 2"}}]}
        self.assertEqual(extract_variant_nodes(edges_input), [{"id": "gid://2", "title": "Node 2"}])

        empty_input = {}
        self.assertEqual(extract_variant_nodes(empty_input), [])

    def test_normalize_shopify_variant_availability_preservation(self):
        # 1. True + 0 inventory -> available_for_sale is True
        node1 = {
            "id": "gid://shopify/ProductVariant/101",
            "title": "4-5 Years",
            "availableForSale": True,
            "inventoryQuantity": 0,
            "selectedOptions": [{"name": "Size", "value": "4-5 Years"}],
        }
        norm1 = normalize_shopify_variant(node1)
        self.assertTrue(norm1["available_for_sale"])
        self.assertEqual(norm1["quantity_available"], 0)

        # 2. False -> available_for_sale is False
        node2 = {
            "id": "gid://shopify/ProductVariant/102",
            "title": "6-7 Years",
            "availableForSale": False,
            "inventoryQuantity": 5,
        }
        norm2 = normalize_shopify_variant(node2)
        self.assertFalse(norm2["available_for_sale"])

        # 3. None / missing -> available_for_sale is None (unknown)
        node3 = {
            "id": "gid://shopify/ProductVariant/103",
            "title": "8-9 Years",
        }
        norm3 = normalize_shopify_variant(node3)
        self.assertIsNone(norm3["available_for_sale"])

    def test_determine_product_availability(self):
        v_avail = [{"available_for_sale": True}, {"available_for_sale": False}]
        self.assertEqual(determine_product_availability(v_avail, True), "available")

        v_sold_out = [{"available_for_sale": False}, {"available_for_sale": False}]
        self.assertEqual(determine_product_availability(v_sold_out, True), "sold_out")

        v_unknown = [{"available_for_sale": None}]
        self.assertEqual(determine_product_availability(v_unknown, True), "unknown")

        self.assertEqual(determine_product_availability(v_avail, False), "unknown")

    def test_is_variant_in_stock_zero_inventory(self):
        variant = {
            "availableForSale": True,
            "inventoryQuantity": 0,
        }
        self.assertTrue(_is_variant_in_stock(variant))

    def test_schema_consistency_never_sold_out_with_options(self):
        from conversation_state import ConversationState
        from main import _build_response

        product = {
            "id": "gid://shopify/Product/1",
            "title": "Boys Casual Jacket",
            "options": [{"id": "opt1", "name": "Size", "values": ["4-5 Years", "6-7 Years"]}],
            "variants": {
                "nodes": [
                    {
                        "id": "v1",
                        "title": "4-5 Years",
                        "availableForSale": True,
                        "inventoryQuantity": 0,
                        "selectedOptions": [{"name": "Size", "value": "4-5 Years"}],
                    },
                    {
                        "id": "v2",
                        "title": "6-7 Years",
                        "availableForSale": True,
                        "inventoryQuantity": 0,
                        "selectedOptions": [{"name": "Size", "value": "6-7 Years"}],
                    },
                ]
            },
        }

        state = ConversationState(session_id="test_sess")
        state.selected_product = product
        payload = _build_response(state, answer="Available options below:", audio_b64=None, products=[product])

        self.assertFalse(payload["sold_out"])
        self.assertEqual(payload["availability_status"], "available")
        self.assertTrue(len(payload["variant_options"]) > 0)


if __name__ == "__main__":
    unittest.main()
