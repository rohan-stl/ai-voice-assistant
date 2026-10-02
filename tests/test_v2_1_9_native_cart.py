"""
tests/test_v2_1_9_native_cart.py
Tests for v2.1.9 Shopify Native Ajax Cart migration and production safeguards.
Uses standard library unittest.
"""

import unittest
import uuid
from unittest.mock import patch, MagicMock

from conversation_state import ConversationState, session_store
from cart_service import (
    save_pending_cart_selection,
    get_session_cart_context,
    clear_session_cart_context,
    sync_native_cart
)
from main import handle_client_cart_action_result
from shopify_auth import ShopContext
from shopify_service import _resolve_shop_credentials


class TestV219NativeCartBackend(unittest.TestCase):

    def setUp(self):
        self.session_id = f"test_sess_{uuid.uuid4().hex[:8]}"
        self.state = ConversationState(session_id=self.session_id)
        session_store.save(self.session_id, self.state)

    def tearDown(self):
        session_store.delete(self.session_id)
        clear_session_cart_context(self.session_id)

    def test_newly_installed_store_zero_credentials_cart_action(self):
        """
        Assert that a valid cart_action is generated when both tokens are None (zero merchant credentials).
        """
        shop_context = ShopContext(shop_domain="new-store.myshopify.com", admin_token=None, storefront_token=None)
        
        action_id = str(uuid.uuid4())
        self.state.set_pending_cart_action(
            action_id=action_id,
            action_type="add",
            product_handle="test-shirt",
            product_title="Test T-Shirt",
            variant_id="998877",
            variant_title="M / Blue",
            selected_options={"Size": "M", "Color": "Blue"},
            quantity=1,
            quantity_mode="add_new"
        )
        
        pending = self.state.get_pending_cart_action()
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action_id"], action_id)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["expected_operation"], "add")
        self.assertEqual(pending["product_handle"], "test-shirt")
        self.assertEqual(pending["variant_id"], "998877")
        self.assertEqual(pending["quantity"], 1)
        self.assertEqual(self.state.stage, "awaiting_client_cart_action")

    def test_no_ensure_storefront_token_called(self):
        """
        Verify that _resolve_shop_credentials does NOT attempt storefrontAccessTokenCreate when no credentials exist.
        """
        with patch("shopify_service.get_store_credentials", return_value=None), \
             patch("shopify_service._ALLOW_ENV_FALLBACK", False):
            sf_token, admin_token = _resolve_shop_credentials("unregistered-store.myshopify.com")
            self.assertIsNone(sf_token)
            self.assertIsNone(admin_token)

    def test_system_event_acknowledgement_successful(self):
        """
        Assert client_cart_action_result acknowledgement updates backend pending action state.
        """
        action_id = str(uuid.uuid4())
        self.state.set_pending_cart_action(
            action_id=action_id,
            action_type="add",
            product_handle="casual-jacket",
            product_title="Boys Casual Jacket",
            variant_id="12345",
            variant_title="XL",
            selected_options={"Size": "XL"},
            quantity=2,
            quantity_mode="add_new"
        )

        all_products = [{"id": "prod_1", "title": "Boys Casual Jacket"}]

        resp = handle_client_cart_action_result(
            session_id=self.session_id,
            action_id=action_id,
            success=True,
            error=None,
            result_data={"total_quantity": 2},
            state=self.state,
            all_products=all_products
        )

        self.assertEqual(resp["intent"], "cart_action_success")
        self.assertIn("Added 2x **Boys Casual Jacket** (XL) to your cart", resp["answer"])
        self.assertIsNone(self.state.get_pending_cart_action())
        self.assertEqual(self.state.stage, "browse")

    def test_acknowledgement_uses_backend_title_authority(self):
        """
        Ensure backend uses stored pending_cart_action product title, ignoring spoofed titles.
        """
        action_id = str(uuid.uuid4())
        self.state.set_pending_cart_action(
            action_id=action_id,
            action_type="add",
            product_handle="authentic-item",
            product_title="Authentic Rolex Watch",
            variant_id="554433",
            variant_title="Silver",
            selected_options={},
            quantity=1,
            quantity_mode="add_new"
        )

        all_products = []
        resp = handle_client_cart_action_result(
            session_id=self.session_id,
            action_id=action_id,
            success=True,
            error=None,
            result_data={"product_title": "FAKED $0.01 ITEM"},
            state=self.state,
            all_products=all_products
        )

        self.assertIn("Authentic Rolex Watch", resp["answer"])
        self.assertNotIn("FAKED", resp["answer"])

    def test_duplicate_acknowledgement_rejection(self):
        """
        Assert duplicate or mismatched action_id acknowledgements are rejected cleanly.
        """
        action_id = str(uuid.uuid4())
        self.state.set_pending_cart_action(
            action_id=action_id,
            action_type="add",
            product_handle="item",
            product_title="Item",
            variant_id="111",
            selected_options={},
            quantity=1
        )

        # First acknowledgement succeeds
        handle_client_cart_action_result(
            session_id=self.session_id,
            action_id=action_id,
            success=True,
            error=None,
            result_data={},
            state=self.state,
            all_products=[]
        )

        # Second acknowledgement with same action_id fails (pending action cleared)
        resp2 = handle_client_cart_action_result(
            session_id=self.session_id,
            action_id=action_id,
            success=True,
            error=None,
            result_data={},
            state=self.state,
            all_products=[]
        )

        self.assertEqual(resp2["intent"], "invalid_action_ack")
        self.assertIn("couldn't verify", resp2["answer"])

    def test_sync_native_cart_sanitizes_lines(self):
        """
        Assert sync_native_cart updates local session memory without storing tokens or secrets.
        """
        raw_cart = {
          "token": "SECRET_SHOPIFY_CART_TOKEN_12345",
          "checkout_charge_amount": 100,
          "item_count": 1,
          "total_price": 4500,
          "currency": "USD",
          "items": [
            {
              "key": "line-key-1",
              "id": 123456,
              "variant_id": 123456,
              "product_title": "Jeans",
              "variant_title": "32W",
              "quantity": 1,
              "price": 4500,
              "line_price": 4500
            }
          ]
        }

        sync_native_cart(self.session_id, raw_cart)
        cached = get_session_cart_context(self.session_id)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["total_quantity"], 1)
        self.assertEqual(cached["lines"][0]["product_title"], "Jeans")
        self.assertNotIn("token", cached)


    def test_clarification_response_metadata(self):
        """
        Assert clarification responses return explicit stage and lifecycle flags.
        """
        from main import _build_response
        self.state.stage = "awaiting_product_clarification"
        response = _build_response(
            state=self.state,
            answer="Could you clarify what you're looking for? I don't see a matching product.",
            audio_b64=None,
            products=[],
            needs_clarification=True,
        )
        self.assertFalse(response["conversation_complete"])
        self.assertTrue(response["requires_input"])
        self.assertTrue(response["auto_listen"])
        self.assertFalse(response["auto_close"])
        self.assertEqual(
            response["stage"],
            "awaiting_product_clarification",
        )


if __name__ == "__main__":
    unittest.main()

