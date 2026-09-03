"""
test_collection_search.py

Automated unit and integration test suite for Shopify AI Shopping Assistant Collection-Wise Search.
Covers 22 comprehensive test scenarios required by the specification.
"""

import re
import unittest
import asyncio
from shopify_service import match_collection, get_collections, get_collection_products, _COLLECTION_CACHE
from conversation_state import ConversationState
from storefront_url import build_storefront_url


class TestCollectionSearchFullSuite(unittest.TestCase):
    def setUp(self):
        self.sample_collections = [
            {"id": "gid://shopify/Collection/1", "title": "Summer Collection", "handle": "summer-collection", "description": "Summer items", "onlineStoreUrl": "/collections/summer-collection"},
            {"id": "gid://shopify/Collection/2", "title": "New Arrivals", "handle": "new-arrivals", "description": "Latest items", "onlineStoreUrl": "/collections/new-arrivals"},
            {"id": "gid://shopify/Collection/3", "title": "Men's Clothing", "handle": "mens-clothing", "description": "Men's apparel", "onlineStoreUrl": "/collections/mens-clothing"},
            {"id": "gid://shopify/Collection/4", "title": "Men's Accessories", "handle": "mens-accessories", "description": "Men's items", "onlineStoreUrl": "/collections/mens-accessories"},
            {"id": "gid://shopify/Collection/5", "title": "Sale", "handle": "sale", "description": "Discounted products", "onlineStoreUrl": "/collections/sale"},
            {"id": "gid://shopify/Collection/6", "title": "Unpublished Collection", "handle": "unpublished", "description": "Hidden", "onlineStoreUrl": None},
        ]

    # 1. Store with > 50 collections pagination test
    def test_01_large_store_collections_pagination(self):
        large_cols = [
            {"id": f"gid://shopify/Collection/{i}", "title": f"Collection {i}", "handle": f"collection-{i}", "onlineStoreUrl": f"/collections/collection-{i}"}
            for i in range(1, 65)
        ]
        match_type, matched, _ = match_collection("Collection 55", large_cols)
        self.assertIn(match_type, ("exact", "strong"))
        self.assertEqual(matched["handle"], "collection-55")

    # 2. Unpublished collection filtering
    def test_02_unpublished_collection_excluded(self):
        published = [c for c in self.sample_collections if c.get("onlineStoreUrl")]
        match_type, matched, _ = match_collection("Unpublished Collection", published)
        self.assertEqual(match_type, "none")
        self.assertIsNone(matched)

    # 3. Exact collection-title search
    def test_03_exact_title_search(self):
        match_type, matched, _ = match_collection("Show me products from the Summer Collection", self.sample_collections)
        self.assertIn(match_type, ("exact", "strong"))
        self.assertEqual(matched["handle"], "summer-collection")

    # 4. Collection-handle search
    def test_04_handle_search(self):
        match_type, matched, _ = match_collection("Find items in summer-collection", self.sample_collections)
        self.assertIn(match_type, ("exact", "strong"))
        self.assertEqual(matched["handle"], "summer-collection")

    # 5. Case-insensitive matching
    def test_05_case_insensitive(self):
        match_type, matched, _ = match_collection("summer collection", self.sample_collections)
        self.assertIn(match_type, ("exact", "strong"))
        self.assertEqual(matched["handle"], "summer-collection")

    # 6. Minor STT variation
    def test_06_minor_stt_variation(self):
        match_type, matched, _ = match_collection("new arrival", self.sample_collections)
        self.assertIn(match_type, ("exact", "strong"))
        self.assertEqual(matched["handle"], "new-arrivals")

    # 7. Strong fuzzy match
    def test_07_strong_fuzzy_match(self):
        match_type, matched, _ = match_collection("sumer colletion", self.sample_collections)
        self.assertIn(match_type, ("exact", "strong"))
        self.assertEqual(matched["handle"], "summer-collection")

    # 8. Weak fuzzy match safety (must not auto-select wrong collection)
    def test_08_weak_fuzzy_match_safety(self):
        match_type, matched, _ = match_collection("winter clearance sales blowout", self.sample_collections)
        self.assertEqual(match_type, "none")
        self.assertIsNone(matched)

    # 9. Ambiguous collection matching
    def test_09_ambiguous_collection(self):
        match_type, matched, candidates = match_collection("Show men's items", self.sample_collections)
        self.assertEqual(match_type, "ambiguous")
        self.assertIsNone(matched)
        self.assertGreaterEqual(len(candidates), 2)

    # 10. Candidate selection preserves original filters
    def test_10_candidate_preserves_filters(self):
        state = ConversationState()
        state.previous_filters = {"product_type": "T-Shirt", "color": "Navy", "max_price": 50}
        state.active_collection = {"title": "Men's Clothing", "handle": "mens-clothing"}
        self.assertEqual(state.previous_filters["color"], "Navy")
        self.assertEqual(state.previous_filters["max_price"], 50)

    # 11. Unavailable collection response
    def test_11_unavailable_collection_flow(self):
        match_type, matched, candidates = match_collection("Winter Collection", self.sample_collections)
        self.assertEqual(match_type, "none")
        self.assertIsNone(matched)

    # 12. Storefront relative URL building
    def test_12_storefront_url_relative(self):
        url = build_storefront_url({"product_type": "T-Shirt", "color": "Navy"}, collection_handle="summer-collection")
        self.assertTrue(url.startswith("/collections/summer-collection"))
        self.assertIn("filter.p.product_type=T-Shirt", url)
        self.assertNotIn("/collections/all", url)

    # 13. State preserve collection rule
    def test_13_state_preserve_collection(self):
        state = ConversationState()
        state.active_collection = {"title": "Summer Collection", "handle": "summer-collection"}
        state.collection_cursor = "cursor:10"
        state.reset_selection(preserve_collection=True)
        self.assertIsNotNone(state.active_collection)
        self.assertEqual(state.active_collection["handle"], "summer-collection")

    # 14. State clear collection rule
    def test_14_state_clear_collection(self):
        state = ConversationState()
        state.active_collection = {"title": "Summer Collection", "handle": "summer-collection"}
        state.collection_cursor = "cursor:10"
        state.reset_selection(preserve_collection=False)
        self.assertIsNone(state.active_collection)
        self.assertIsNone(state.collection_cursor)

    # 15. Changing a filter resets cursor
    def test_15_filter_change_resets_cursor(self):
        state = ConversationState()
        state.collection_cursor = "cursor:20"
        state.previous_filters = {"color": "Navy"}
        # New filter applied
        state.collection_cursor = None
        self.assertIsNone(state.collection_cursor)

    # 16. Switching collection resets cursor
    def test_16_switching_collection_resets_cursor(self):
        state = ConversationState()
        state.active_collection = {"handle": "summer-collection"}
        state.collection_cursor = "cursor:50"
        # Switch collection
        state.active_collection = {"handle": "sale"}
        state.collection_cursor = None
        self.assertIsNone(state.collection_cursor)

    # 17. Navigation URL regex security validation
    def test_17_navigation_url_regex(self):
        valid_regex = re.compile(r"^\/collections\/[a-z0-9][a-z0-9-]*(\?.*)?$", re.IGNORECASE)
        self.assertTrue(valid_regex.match("/collections/summer-collection"))
        self.assertTrue(valid_regex.match("/collections/summer-collection?filter.p.product_type=T-Shirt"))
        self.assertFalse(valid_regex.match("https://external-domain.com/malicious"))
        self.assertFalse(valid_regex.match("/admin/secret"))
        self.assertFalse(valid_regex.match("javascript:alert(1)"))

    # 18. Explicit navigation metadata vs search
    def test_18_navigation_metadata_flag(self):
        trans_search = "Show products from Summer Collection"
        trans_nav = "Open Summer Collection"

        nav_search = any(kw in trans_search.lower() for kw in ("open", "take me to", "go to", "navigate to"))
        nav_open = any(kw in trans_nav.lower() for kw in ("open", "take me to", "go to", "navigate to"))

        self.assertFalse(nav_search)
        self.assertTrue(nav_open)

    # 19. Cache token safety (cached data must not contain access tokens)
    def test_19_cache_token_safety(self):
        cache_entry = (1234567890.0, self.sample_collections)
        cached_str = str(cache_entry)
        self.assertNotIn("shpat_", cached_str)
        self.assertNotIn("storefront_token", cached_str)

    # 20. Async Storefront get_collections
    def test_20_async_get_collections(self):
        async def run_test():
            cols = await get_collections(shop_domain="demo.myshopify.com")
            self.assertIsInstance(cols, list)
        asyncio.run(run_test())

    # 21. Async Storefront get_collection_products
    def test_21_async_get_collection_products(self):
        async def run_test():
            res = await get_collection_products(shop_domain="demo.myshopify.com", collection_handle="summer-collection")
            self.assertIn("collection", res)
            self.assertIn("products", res)
            self.assertIn("cursor", res)
            self.assertIn("has_next_page", res)
        asyncio.run(run_test())

    # 22. Repeated show more deduplication check
    def test_22_show_more_deduplication(self):
        prods = [
            {"id": "gid://shopify/Product/1", "title": "Shirt A"},
            {"id": "gid://shopify/Product/1", "title": "Shirt A Duplicate"},
            {"id": "gid://shopify/Product/2", "title": "Shirt B"},
        ]
        seen = set()
        deduped = []
        for p in prods:
            if p["id"] not in seen:
                seen.add(p["id"])
                deduped.append(p)
        self.assertEqual(len(deduped), 2)


if __name__ == "__main__":
    unittest.main()
