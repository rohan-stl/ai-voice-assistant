import unittest
import sys
import os

sys.path.insert(0, os.path.abspath("."))
from variant_resolver import (
    is_shopify_default_title,
    get_remaining_option_values,
    _get_product_options_summary,
    _validate_and_apply_options,
)
from conversation_state import ConversationState
from main import _build_response
from shopify_service import ShopifyAuthenticationError


class TestVariantOptions(unittest.TestCase):

    def test_01_is_shopify_default_title(self):
        self.assertTrue(is_shopify_default_title("Title", "Default Title"))
        self.assertTrue(is_shopify_default_title("title", "default title"))
        self.assertFalse(is_shopify_default_title("Title", "Available"))
        self.assertFalse(is_shopify_default_title("Size", "Default Title"))
        self.assertFalse(is_shopify_default_title("Color", "Blue"))

    def test_02_size_only_product(self):
        product = {
            "title": "Casual Shorts",
            "options": [{"name": "Size", "values": ["S", "M", "L"]}],
            "variants": {"nodes": [
                {"id": "v1", "title": "S", "availableForSale": True, "selectedOptions": [{"name": "Size", "value": "S"}]},
                {"id": "v2", "title": "M", "availableForSale": True, "selectedOptions": [{"name": "Size", "value": "M"}]},
                {"id": "v3", "title": "L", "availableForSale": True, "selectedOptions": [{"name": "Size", "value": "L"}]},
            ]}
        }
        res = get_remaining_option_values(product, {})
        self.assertIn("Size", res)
        vals = [item["value"] for item in res["Size"] if item["available"]]
        self.assertEqual(vals, ["S", "M", "L"])

    def test_03_color_only_product(self):
        product = {
            "title": "Leather Wallet",
            "options": [{"name": "Color", "values": ["Black", "Brown"]}],
            "variants": {"nodes": [
                {"id": "v1", "title": "Black", "availableForSale": True, "selectedOptions": [{"name": "Color", "value": "Black"}]},
                {"id": "v2", "title": "Brown", "availableForSale": True, "selectedOptions": [{"name": "Color", "value": "Brown"}]},
            ]}
        }
        res = get_remaining_option_values(product, {})
        self.assertIn("Color", res)
        vals = [item["value"] for item in res["Color"] if item["available"]]
        self.assertEqual(vals, ["Black", "Brown"])

    def test_04_custom_options_material_style(self):
        product = {
            "title": "Designer Sofa",
            "options": [{"name": "Material", "values": ["Leather", "Velvet"]}, {"name": "Style", "values": ["Modern", "Classic"]}],
            "variants": {"nodes": [
                {"id": "v1", "title": "Leather / Modern", "availableForSale": True, "selectedOptions": [{"name": "Material", "value": "Leather"}, {"name": "Style", "value": "Modern"}]},
                {"id": "v2", "title": "Velvet / Classic", "availableForSale": True, "selectedOptions": [{"name": "Material", "value": "Velvet"}, {"name": "Style", "value": "Classic"}]},
            ]}
        }
        res = get_remaining_option_values(product, {})
        self.assertIn("Material", res)
        self.assertIn("Style", res)

    def test_05_default_title_product(self):
        product = {
            "title": "Single Item Pack",
            "options": [{"name": "Title", "values": ["Default Title"]}],
            "variants": {"nodes": [
                {"id": "v1", "title": "Default Title", "availableForSale": True, "selectedOptions": [{"name": "Title", "value": "Default Title"}]}
            ]}
        }
        res = get_remaining_option_values(product, {})
        self.assertEqual(res, {})
        summary = _get_product_options_summary(product, {})
        self.assertIn("does not require option selection", summary)

    def test_06_sold_out_variants_filtered(self):
        product = {
            "title": "Boys Casual Jacket",
            "options": [{"name": "Size", "values": ["S", "M", "L"]}],
            "variants": {"nodes": [
                {"id": "v1", "title": "S", "availableForSale": True, "selectedOptions": [{"name": "Size", "value": "S"}]},
                {"id": "v2", "title": "M", "availableForSale": False, "selectedOptions": [{"name": "Size", "value": "M"}]},
                {"id": "v3", "title": "L", "availableForSale": False, "selectedOptions": [{"name": "Size", "value": "L"}]},
            ]}
        }
        res = get_remaining_option_values(product, {})
        avail_vals = [item["value"] for item in res["Size"] if item["available"]]
        self.assertEqual(avail_vals, ["S"])
        unavail_vals = [item["value"] for item in res["Size"] if not item["available"]]
        self.assertEqual(sorted(unavail_vals), ["L", "M"])

    def test_07_partially_available_combinations(self):
        # White/L is unavailable; Navy/M & Navy/L & White/M are available
        product = {
            "title": "Casual Shirt",
            "options": [{"name": "Color", "values": ["Navy", "White"]}, {"name": "Size", "values": ["M", "L"]}],
            "variants": {"nodes": [
                {"id": "v1", "title": "Navy / M", "availableForSale": True, "selectedOptions": [{"name": "Color", "value": "Navy"}, {"name": "Size", "value": "M"}]},
                {"id": "v2", "title": "Navy / L", "availableForSale": True, "selectedOptions": [{"name": "Color", "value": "Navy"}, {"name": "Size", "value": "L"}]},
                {"id": "v3", "title": "White / M", "availableForSale": True, "selectedOptions": [{"name": "Color", "value": "White"}, {"name": "Size", "value": "M"}]},
                {"id": "v4", "title": "White / L", "availableForSale": False, "selectedOptions": [{"name": "Color", "value": "White"}, {"name": "Size", "value": "L"}]},
            ]}
        }
        # If White selected -> remaining size available should be only M
        res_white = get_remaining_option_values(product, {"Color": "White"})
        white_sizes = [item["value"] for item in res_white["Size"] if item["available"]]
        self.assertEqual(white_sizes, ["M"])

    def test_08_duplicate_option_values_deduplicated(self):
        product = {
            "title": "Hat",
            "variants": {"nodes": [
                {"id": "v1", "availableForSale": True, "selectedOptions": [{"name": "Color", "value": "Red"}]},
                {"id": "v2", "availableForSale": True, "selectedOptions": [{"name": "Color", "value": "red"}]},
            ]}
        }
        res = get_remaining_option_values(product, {})
        color_vals = [item["value"] for item in res["Color"]]
        self.assertEqual(color_vals, ["Red"])

    def test_09_specific_size_request(self):
        product = {
            "title": "Boys Casual Jacket",
            "options": [{"name": "Size", "values": ["4-5 Years", "6-7 Years", "8-9 Years"]}],
            "variants": {"nodes": [
                {"id": "v1", "availableForSale": True, "selectedOptions": [{"name": "Size", "value": "4-5 Years"}]},
                {"id": "v2", "availableForSale": True, "selectedOptions": [{"name": "Size", "value": "6-7 Years"}]},
                {"id": "v3", "availableForSale": True, "selectedOptions": [{"name": "Size", "value": "8-9 Years"}]},
            ]}
        }
        summary = _get_product_options_summary(product, {}, requested_option_name="Size")
        self.assertIn("Available sizes are 4-5 Years, 6-7 Years and 8-9 Years", summary)
        self.assertIn("Which size would you like?", summary)

    def test_10_invalid_requested_option(self):
        product = {
            "title": "Boys Casual Jacket",
            "options": [{"name": "Color", "values": ["Navy", "Black"]}],
            "variants": {"nodes": [
                {"id": "v1", "availableForSale": True, "selectedOptions": [{"name": "Color", "value": "Navy"}]},
                {"id": "v2", "availableForSale": True, "selectedOptions": [{"name": "Color", "value": "Black"}]},
            ]}
        }
        # Provide a second catalog product with Color:Red so the dynamic scanner
        # recognizes "Red" as a Color attempt — which is invalid for this product.
        catalog_with_red = [
            product,
            {"title": "Other Shirt", "options": [{"name": "Color", "values": ["Red"]}],
             "variants": {"nodes": [{"id": "vr", "availableForSale": True,
                                     "selectedOptions": [{"name": "Color", "value": "Red"}]}]}}
        ]
        state = ConversationState()
        valid, invalid, missing = _validate_and_apply_options(product, "Red", state, catalog_with_red)
        self.assertIn("Color", invalid)
        self.assertEqual(invalid["Color"], "Red")

    def test_11_raw_title_available_product_configuration(self):
        # Product literally configured with Title: Available in Shopify
        product = {
            "title": "Mystery Box",
            "options": [{"name": "Title", "values": ["Available"]}],
            "variants": {"nodes": [
                {"id": "v1", "availableForSale": True, "selectedOptions": [{"name": "Title", "value": "Available"}]}
            ]}
        }
        summary = _get_product_options_summary(product, {})
        # Must report no option selection required, not invent sizes
        self.assertIn("does not require option selection", summary)

    def test_12_regression_never_contains_options_available_metadata(self):
        product = {
            "title": "Boys Casual Jacket",
            "options": [{"name": "Size", "values": ["M", "L"]}],
            "variants": {"nodes": [
                {"id": "v1", "availableForSale": True, "selectedOptions": [{"name": "Size", "value": "M"}]},
                {"id": "v2", "availableForSale": True, "selectedOptions": [{"name": "Size", "value": "L"}]},
            ]}
        }
        summary = _get_product_options_summary(product, {})
        self.assertNotIn("Options: Available", summary)
        self.assertNotIn("Available: Available", summary)

    def test_13_response_contract_canonical_stage(self):
        state = ConversationState()
        state.stage = "awaiting_variant"
        state.selected_product = {
            "title": "Jacket",
            "options": [{"name": "Size", "values": ["S"]}],
            "variants": {"nodes": [{"id": "v1", "availableForSale": True, "selectedOptions": [{"name": "Size", "value": "S"}]}]}
        }
        resp = _build_response(
            transcription="Show sizes",
            answer="Available sizes are S.",
            state=state,
            products=[],
            audio_b64=None
        )
        self.assertEqual(resp["stage"], "awaiting_variant_option")
        self.assertTrue(resp["requires_input"])
        self.assertTrue(resp["auto_listen"])
        self.assertFalse(resp["auto_close"])
        self.assertFalse(resp["conversation_complete"])
        self.assertIn("variant_options", resp)


if __name__ == "__main__":
    unittest.main()
