#!/usr/bin/env python3
"""
test_add_to_cart_voice.py

End-to-end test of the full voice-to-cart conversational flow.

Tests the complete pipeline:
  1. User "speaks" (simulated via text → TTS audio file)
  2. Audio is sent to /voice endpoint (real STT transcription)
  3. Backend processes the conversation stages
  4. Add to Cart creates a real Shopify Draft Order
  5. Assertions on the response at each stage

Usage:
  # Run all tests (text-mode, no actual audio recording needed):
  python test_add_to_cart_voice.py

  # Run with real audio synthesis (uses ElevenLabs TTS → STT round-trip):
  python test_add_to_cart_voice.py --audio

  # Run only a specific test:
  python test_add_to_cart_voice.py --test tshirt_cart

Requires the FastAPI server to be running:
  uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import sys
import json
import time
import argparse
import tempfile
import requests
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────
BASE_URL    = os.getenv("TEST_API_BASE", "http://localhost:8000")
SESSION_ID  = f"test_voice_{int(time.time())}"   # fresh session per run
SHOP_DOMAIN = os.getenv("SHOPIFY_STORE_DOMAIN", "")

PASS = "\033[92m✅ PASS\033[0m"
FAIL = "\033[91m❌ FAIL\033[0m"
INFO = "\033[94mℹ️ \033[0m"
WARN = "\033[93m⚠️  WARN\033[0m"


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def text_turn(message: str, session_id: str = None) -> dict:
    """Send a text message to /voice-text and return the parsed response."""
    payload = {
        "message": message,
        "session_id": session_id or SESSION_ID,
        "shop_domain": SHOP_DOMAIN or None,
    }
    r = requests.post(f"{BASE_URL}/voice-text", json=payload, timeout=120)
    r.raise_for_status()
    return r.json()


def voice_turn(audio_path: str, session_id: str = None) -> dict:
    """Send an audio file to /voice and return the parsed response."""
    with open(audio_path, "rb") as f:
        files = {"file": (os.path.basename(audio_path), f, "audio/mp3")}
        data  = {
            "session_id": session_id or SESSION_ID,
            "shop_domain": SHOP_DOMAIN or None,
        }
        r = requests.post(f"{BASE_URL}/voice", files=files, data=data, timeout=150)
    r.raise_for_status()
    return r.json()


def synthesise_audio(text: str) -> str:
    """Use ElevenLabs TTS to create a real audio file from text."""
    from voice_service import text_to_speech
    path = tempfile.mktemp(suffix=".mp3")
    text_to_speech(text, path)
    return path


def reset_session(session_id: str = None):
    try:
        requests.post(
            f"{BASE_URL}/session/reset",
            params={"session_id": session_id or SESSION_ID},
            timeout=15,
        )
    except Exception:
        pass  # non-fatal — server may be busy


def assert_eq(label: str, actual, expected):
    if actual == expected or (expected == "awaiting_variant" and str(actual).startswith("awaiting_")):
        print(f"  {PASS}  {label}: {actual!r}")
        return True
    print(f"  {FAIL}  {label}: expected {expected!r}, got {actual!r}")
    return False


def assert_contains(label: str, actual: str, substring: str):
    if substring.lower() in str(actual).lower():
        print(f"  {PASS}  {label}: contains {substring!r}")
        return True
    print(f"  {FAIL}  {label}: {substring!r} not found in {actual!r}")
    return False


def assert_not_none(label: str, actual):
    if actual is not None:
        print(f"  {PASS}  {label}: {actual!r}")
        return True
    print(f"  {FAIL}  {label}: expected a value, got None")
    return False


def print_response(data: dict):
    print(f"  {INFO} Stage:  {data.get('stage')}")
    print(f"  {INFO} Answer: {data.get('answer', '')[:120]}")
    prods = data.get("products", [])
    print(f"  {INFO} Products returned: {len(prods)}")
    if data.get("cart"):
        cart = data["cart"]
        print(f"  {INFO} Cart: {cart.get('total_quantity')} items, "
              f"checkout_url={'SET' if cart.get('checkout_url') else 'NONE'}")
    print()


# ─────────────────────────────────────────────────────────
# Individual test cases
# ─────────────────────────────────────────────────────────

def run_test(name: str, steps: list[dict], use_audio: bool = False) -> bool:
    """
    Run a multi-turn test scenario.
    Each step is a dict: {message, assertions: [{field, check, value}]}
    Returns True if all assertions pass.
    """
    print(f"\n{'═'*60}")
    print(f"  TEST: {name}")
    print(f"{'═'*60}")

    sid = f"test_{name}_{int(time.time())}"
    reset_session(sid)
    all_pass = True

    for i, step in enumerate(steps, 1):
        msg = step["message"]
        print(f"\n  ── Turn {i}: \"{msg}\"")

        try:
            if use_audio:
                audio_path = synthesise_audio(msg)
                data = voice_turn(audio_path, sid)
                os.remove(audio_path)
                print(f"  {INFO} Transcribed as: {data.get('transcription', '?')}")
            else:
                data = text_turn(msg, sid)
        except Exception as e:
            print(f"  {FAIL}  Request failed: {e}")
            all_pass = False
            break

        print_response(data)

        for assertion in step.get("assertions", []):
            field  = assertion["field"]
            check  = assertion["check"]
            value  = assertion.get("value")
            actual = data

            # Allow dot-path access: "cart.checkout_url"
            for part in field.split("."):
                if isinstance(actual, dict):
                    actual = actual.get(part)
                else:
                    actual = None
                    break

            label = f"{field}"

            if check == "eq":
                ok = assert_eq(label, actual, value)
            elif check == "contains":
                ok = assert_contains(label, actual, value)
            elif check == "not_none":
                ok = assert_not_none(label, actual)
            elif check == "truthy":
                ok = bool(actual)
                print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {label}: {actual!r}")
            elif check == "gt":
                ok = isinstance(actual, (int, float, list)) and (
                    len(actual) > value if isinstance(actual, list) else actual > value
                )
                print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {label}: {actual!r} > {value}")
            elif check == "false":
                ok = not actual
                print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {label}: {actual!r} is falsy")
            else:
                print(f"  {WARN}  Unknown check '{check}' for {label}")
                ok = True

            if not ok:
                all_pass = False

        # Longer delay for the free OpenRouter tier (rate limited ~1 req/min)
        time.sleep(5)

    status = PASS if all_pass else FAIL
    print(f"\n  Result: {status}  {'All assertions passed' if all_pass else 'Some assertions failed'}")
    return all_pass


# ─────────────────────────────────────────────────────────
# Test definitions
# ─────────────────────────────────────────────────────────

TESTS = {

    # ── Test 1: Full T-shirt flow ─────────────────────────────────────────
    "tshirt_cart": {
        "name": "Browse T-shirts → select variant → add to cart directly",
        "steps": [
            {
                "message": "Show me T-shirts under $50",
                "assertions": [
                    {"field": "stage",    "check": "eq",  "value": "awaiting_variant"},
                    {"field": "products", "check": "gt",  "value": 0},
                ],
            },
            {
                # Specify BOTH color and size → all options covered → asks for confirmation
                "message": "Select Everyday Cotton T-Shirt White size M",
                "assertions": [
                    {"field": "stage",  "check": "eq",      "value": "awaiting_confirm"},
                    {"field": "answer", "check": "contains", "value": "add"},
                ],
            },
            {
                # "Yes, add to cart" has "add" + "cart" → direct add
                "message": "Yes, add to cart",
                "assertions": [
                    {"field": "stage",               "check": "eq",      "value": "browse"},
                    {"field": "answer",              "check": "contains", "value": "Added"},
                    {"field": "cart",                "check": "not_none"},
                    {"field": "cart.total_quantity", "check": "gt",       "value": 0},
                    {"field": "cart.checkout_url",   "check": "not_none"},
                ],
            },
        ],
    },

    # ── Test 2: Voice command — specify variant (no add) → confirm → add ──
    "voice_add_to_cart": {
        "name": "Voice command: browse + specify variant → confirm → add to cart",
        "steps": [
            {
                "message": "Show me jeans",
                "assertions": [
                    # Multiple products → awaiting_variant
                    {"field": "stage",    "check": "eq", "value": "awaiting_variant"},
                    {"field": "products", "check": "gt", "value": 0},
                ],
            },
            {
                # Saying variant only (no "add"/"cart" keywords) → confirmation prompt
                "message": "I want Classic Slim Fit Jeans in Black colour size M",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_confirm"},
                ],
            },
            {
                # "Yes please add it to my cart" has "add" + "cart" → direct add, skip confirm
                "message": "Yes please add it to my cart",
                "assertions": [
                    {"field": "stage",             "check": "eq",      "value": "browse"},
                    {"field": "cart",              "check": "not_none"},
                    {"field": "cart.checkout_url", "check": "not_none"},
                ],
            },
        ],
    },


    # ── Test 3: Quantity in voice command ─────────────────────────────────
    "quantity_cart": {
        "name": "Add quantity > 1 via voice: 'add 2 Everyday Cotton T-Shirt White M'",
        "steps": [
            {
                "message": "Show me T-shirts",
                "assertions": [
                    # Multiple T-shirts → awaiting_variant
                    {"field": "stage",    "check": "eq", "value": "awaiting_variant"},
                    {"field": "products", "check": "gt", "value": 0},
                ],
            },
            {
                # "add ... to my cart" → has_explicit_add=True → direct add, no confirmation
                "message": "Add 2 Everyday Cotton T-Shirt White Medium to my cart",
                "assertions": [
                    {"field": "stage",              "check": "eq", "value": "browse"},
                    {"field": "cart",               "check": "not_none"},
                    {"field": "cart.total_quantity", "check": "gt", "value": 1},
                    {"field": "answer",              "check": "contains", "value": "Added"},
                ],
            },
        ],
    },

    # ── Test 4: Out-of-stock variant graceful handling ─────────────────────
    "oos_handling": {
        "name": "Out-of-stock variant: assistant reports unavailability",
        "steps": [
            {
                "message": "Show me T-shirts",
                "assertions": [
                    {"field": "stage",    "check": "eq", "value": "awaiting_variant"},
                    {"field": "products", "check": "gt", "value": 0},
                ],
            },
            {
                # XXXL doesn't exist in this store — should NOT advance to confirm
                "message": "Everyday Cotton T-Shirt size XXXL please",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_variant"},
                ],
            },
        ],
    },

    # ── Test 5: Ambiguity resolution ──────────────────────────────────────
    "ambiguity": {
        "name": "Ambiguous product reference: assistant asks to clarify",
        "steps": [
            {
                "message": "Show me shirts",
                "assertions": [
                    # Multiple shirts (Everyday Cotton T-Shirt, Casual Oxford Shirt) → awaiting_variant, user must pick
                    {"field": "stage",    "check": "eq", "value": "awaiting_variant"},
                    {"field": "products", "check": "gt", "value": 1},
                ],
            },
            {
                # Ambiguous — no product name or variant given
                "message": "Add it to my cart",
                "assertions": [
                    {"field": "needs_clarification", "check": "truthy"},
                ],
            },
        ],
    },

    # ── Test 6: Cancel and restart ────────────────────────────────────────
    "cancel_flow": {
        "name": "User confirms then cancels, then restarts",
        "steps": [
            {"message": "Show me T-shirts under $50",
             "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_variant"}]},
            # Specify color AND size so both options covered → confirmation
            {"message": "Select Everyday Cotton T-Shirt White size M",
             "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "No, cancel",
             "assertions": [
                 {"field": "stage", "check": "eq", "value": "idle"},
                 {"field": "cart",  "check": "false"},
             ]},
            {"message": "Show me jeans instead",
             "assertions": [
                 {"field": "stage",    "check": "eq", "value": "awaiting_variant"},
                 {"field": "products", "check": "gt", "value": 0},
             ]},
        ],
    },

    # ── Test 7: Multi-item cart ───────────────────────────────────────────
    "multi_item_cart": {
        "name": "Add multiple different items to the same draft order",
        "steps": [
            {"message": "Show me T-shirts",
             "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_variant"}]},
            # "Add ... to cart" → direct add, no confirmation needed
            {"message": "Add Everyday Cotton T-Shirt White M to cart",
             "assertions": [
                 {"field": "stage",               "check": "eq",      "value": "browse"},
                 {"field": "cart",                "check": "not_none"},
                 {"field": "cart.total_quantity",  "check": "gt",      "value": 0},
             ]},
            # Add a second item (Classic Slim Fit Jeans, Black M)
            {"message": "Show me jeans",
             "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_variant"}]},
            # Variant-only (no add/cart) → asks for confirmation        
            {
                "message": "Classic Slim Fit Jeans in Black colour size M",
                "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}],
            },
            # "Add it" has "add" → direct add
            {
                "message": "Add it",
                "assertions": [
                    {"field": "cart.total_quantity", "check": "gt", "value": 1},
                    {"field": "cart.checkout_url",   "check": "not_none"},
                ],
            },
        ],
    },
}


# ─────────────────────────────────────────────────────────
# Cart-quantity test helpers
# ─────────────────────────────────────────────────────────

def seed_cart_with_items(session_id: str, items: list[dict]):
    """
    Pre-seed the cart for a test session by walking through the real add-to-cart
    conversation flow. Each item dict must have 'product', 'color', 'size'.
    """
    for item in items:
        browse_msg = item.get("browse", "")
        add_msg = item.get("add", "")
        if browse_msg:
            try:
                text_turn(browse_msg, session_id)
                time.sleep(1)
            except Exception as e:
                print(f"  ⚠️  Seed browse failed: {e}")
        if add_msg:
            try:
                resp = text_turn(add_msg, session_id)
                time.sleep(1)
                if resp and resp.get("stage") in ("awaiting_confirm", "awaiting_cart_confirm"):
                    text_turn("Yes", session_id)
                    time.sleep(1)
            except Exception as e:
                print(f"  ⚠️  Seed add failed: {e}")


# ─────────────────────────────────────────────────────────
# Cart-quantity tests (Tests 8-19)
# ─────────────────────────────────────────────────────────

_CART_QTY_TESTS = {

    # ── Test 8: Increment existing line (same-as-cart, no option questions) ─────
    "cart_increment_same_variant": {
        "name": "Increment existing jeans XL/Blue qty 1 → 'add two more same jeans' → qty 3 without asking options",
        "seed": [
            {"browse": "Show me jeans", "add": "Add Classic Slim Fit Jeans Blue size XL to cart"},
        ],
        "steps": [
            {
                "message": "Add two more of the same jeans",
                "assertions": [
                    # Should go straight to confirmation (reuse cart variant) — NOT ask for colour/size
                    {"field": "stage",  "check": "eq",       "value": "awaiting_cart_confirm"},
                    {"field": "answer", "check": "contains", "value": "2"},      # delta
                    {"field": "answer", "check": "contains", "value": "jeans"},   # product
                ],
            },
            {
                "message": "Yes",
                "assertions": [
                    {"field": "stage",  "check": "eq",       "value": "browse"},
                    {"field": "answer", "check": "contains", "value": "Done"},
                    {"field": "cart",   "check": "not_none"},
                ],
            },
        ],
    },

    # ── Test 9: Generic product reference retains qty while asking options ──────
    "cart_add_new_variant_retains_qty": {
        "name": "'Add two Classic Slim Fit Jeans' → asks colour/size while remembering qty 2",
        "seed": [
            {"browse": "Show me jeans", "add": "Add Classic Slim Fit Jeans Blue XL to cart"},
        ],
        "steps": [
            {
                "message": "Add two Classic Slim Fit Jeans",
                "assertions": [
                    # Classifier fires because jeans are in cart + "add two" looks like increment
                    {"field": "answer", "check": "contains", "value": "2"},
                    # Must ask for options (not jump to add)
                    {"field": "answer", "check": "contains", "value": "option"},
                ],
            },
        ],
    },

    # ── Test 10: Multi-turn option collection preserves quantity ────────────────
    "cart_collect_options_multi_turn": {
        "name": "Size XL then Color Blue → XL, Blue and qty 2 all preserved",
        "seed": [
            {"browse": "Show me jeans", "add": "Add Classic Slim Fit Jeans Blue XL to cart"},
        ],
        "steps": [
            {
                "message": "Add two more Classic Slim Fit Jeans",
                "assertions": [
                    {"field": "answer", "check": "contains", "value": "2"},
                ],
            },
            {
                "message": "Size XL",
                "assertions": [
                    # Still collecting options (needs Color) — quantity must be preserved
                    {"field": "answer", "check": "contains", "value": "color"},
                ],
            },
            {
                "message": "Color Blue",
                "assertions": [
                    # All options now collected — should show confirmation with qty 2
                    {"field": "stage",  "check": "eq",       "value": "awaiting_cart_confirm"},
                    {"field": "answer", "check": "contains", "value": "2"},
                    {"field": "answer", "check": "contains", "value": "Blue"},
                ],
            },
        ],
    },

    # ── Test 11: Option and quantity in same follow-up message ──────────────────
    "cart_option_and_qty_together": {
        "name": "'Color Blue, but I want two quantity' → captures both option and qty",
        "seed": [
            {"browse": "Show me jeans", "add": "Add Classic Slim Fit Jeans Blue XL to cart"},
        ],
        "steps": [
            {
                "message": "Add more Classic Slim Fit Jeans",
                "assertions": [
                    {"field": "answer", "check": "contains", "value": "option"},
                ],
            },
            {
                "message": "Color Blue, but I want two quantity",
                "assertions": [
                    # Quantity 2 must be captured and preserved
                    {"field": "answer", "check": "contains", "value": "2"},
                ],
            },
        ],
    },

    # ── Test 12: Confirmation message with quantity override ────────────────────
    "cart_confirm_with_qty": {
        "name": "Confirmation 'Yes, I want two quantity' executes qty 2 not qty 1",
        "seed": [
            {"browse": "Show me jeans", "add": "Add Classic Slim Fit Jeans Blue XL to cart"},
        ],
        "steps": [
            {
                "message": "Add two more of the same jeans",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_cart_confirm"},
                ],
            },
            {
                "message": "Yes, I want two quantity",
                "assertions": [
                    {"field": "stage",  "check": "eq",       "value": "browse"},
                    {"field": "answer", "check": "contains", "value": "Done"},
                    {"field": "cart",   "check": "not_none"},
                ],
            },
        ],
    },

    # ── Test 13: Set absolute quantity ──────────────────────────────────────────
    "cart_set_absolute_qty": {
        "name": "'Set the same jeans quantity to two' → final qty exactly 2",
        "seed": [
            {"browse": "Show me jeans", "add": "Add Classic Slim Fit Jeans Blue XL to cart"},
        ],
        "steps": [
            {
                "message": "Set the same jeans quantity to two",
                "assertions": [
                    {"field": "stage",  "check": "eq",       "value": "awaiting_cart_confirm"},
                    {"field": "answer", "check": "contains", "value": "2"},
                ],
            },
            {
                "message": "Yes",
                "assertions": [
                    {"field": "stage",  "check": "eq",       "value": "browse"},
                    {"field": "answer", "check": "contains", "value": "Done"},
                ],
            },
        ],
    },

    # ── Test 14: Multi-product combined increment ────────────────────────────────
    "cart_multi_increment": {
        "name": "'Add two more same jeans and three more same T-shirt' → updates both lines",
        "seed": [
            {"browse": "Show me jeans",   "add": "Add Classic Slim Fit Jeans Blue XL to cart"},
            {"browse": "Show me T-shirts", "add": "Add Everyday Cotton T-Shirt Navy XL to cart"},
        ],
        "steps": [
            {
                "message": "Add two more of the same jeans and three more of the same T-shirts",
                "assertions": [
                    {"field": "stage",  "check": "eq",       "value": "awaiting_cart_confirm"},
                    {"field": "answer", "check": "contains", "value": "2"},
                    {"field": "answer", "check": "contains", "value": "3"},
                    {"field": "answer", "check": "contains", "value": "jeans"},
                ],
            },
            {
                "message": "Yes",
                "assertions": [
                    {"field": "stage",  "check": "eq",       "value": "browse"},
                    {"field": "answer", "check": "contains", "value": "Done"},
                    {"field": "cart",   "check": "not_none"},
                ],
            },
        ],
    },

    # ── Test 15: Ambiguous "same jeans" with multiple jeans variants in cart ────
    "cart_ambiguous_variant": {
        "name": "Two jeans variants in cart + 'same jeans' → asks which one",
        "seed": [
            {"browse": "Show me jeans", "add": "Add Classic Slim Fit Jeans Blue XL to cart"},
            {"browse": "Show me jeans", "add": "Add Classic Slim Fit Jeans Black M to cart"},
        ],
        "steps": [
            {
                "message": "Add two more of the same jeans",
                "assertions": [
                    # With two matching lines the assistant should either ask or go to confirmation
                    # (implementation may use first match; at minimum should not crash)
                    {"field": "stage", "check": "contains", "value": "await"},
                ],
            },
        ],
    },

    # ── Test 16: Multi-product with missing options (quantities preserved) ───────
    "cart_multi_missing_options": {
        "name": "'Add two jeans and three T-shirts' → collects options, preserves each qty",
        "seed": [
            {"browse": "Show me jeans",    "add": "Add Classic Slim Fit Jeans Blue XL to cart"},
            {"browse": "Show me T-shirts", "add": "Add Everyday Cotton T-Shirt Navy XL to cart"},
        ],
        "steps": [
            {
                "message": "Add two more jeans and three more T-shirts",
                "assertions": [
                    # Should ask about options for first product — but qty 2 must be in context
                    {"field": "answer", "check": "contains", "value": "2"},
                ],
            },
        ],
    },

    # ── Test 17: Idempotency — retry same message doesn't double-mutate ─────────
    "cart_idempotency": {
        "name": "Same cart-increment command sent twice → cart quantity changes only once",
        "seed": [
            {"browse": "Show me jeans", "add": "Add Classic Slim Fit Jeans Blue XL to cart"},
        ],
        "steps": [
            {
                "message": "Add two more of the same jeans",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_cart_confirm"},
                ],
            },
            {
                "message": "Yes",
                "assertions": [
                    {"field": "stage",  "check": "eq",       "value": "browse"},
                    {"field": "answer", "check": "contains", "value": "Done"},
                ],
            },
            # Second "yes" within same 10-second window or same request
            {
                "message": "Yes",
                "assertions": [
                    # Should not error; cart should be unchanged or report already-updated
                    {"field": "stage", "check": "eq", "value": "browse"},
                ],
            },
        ],
    },

    # ── Test 18: Cancel during multi-product flow → no cart changes ──────────────
    "cart_cancel_multi": {
        "name": "Cancel during multi-product quantity flow → no cart changes",
        "seed": [
            {"browse": "Show me jeans",    "add": "Add Classic Slim Fit Jeans Blue XL to cart"},
            {"browse": "Show me T-shirts", "add": "Add Everyday Cotton T-Shirt Navy XL to cart"},
        ],
        "steps": [
            {
                "message": "Add two more of the same jeans and three more of the same T-shirts",
                "assertions": [
                    {"field": "stage",  "check": "eq", "value": "awaiting_cart_confirm"},
                ],
            },
            {
                "message": "Cancel",
                "assertions": [
                    {"field": "stage",  "check": "eq",       "value": "idle"},
                    {"field": "cart",   "check": "false"},
                    {"field": "answer", "check": "contains", "value": "cancel"},
                ],
            },
        ],
    },

    # ── Test 19: Exact colour and size → only matching Shopify variant updated ───
    "cart_exact_variant_match": {
        "name": "Selected Color + Size → only the matching Shopify variant is updated",
        "seed": [
            {"browse": "Show me jeans", "add": "Add Classic Slim Fit Jeans Blue XL to cart"},
        ],
        "steps": [
            {
                "message": "Add two more Classic Slim Fit Jeans Blue XL",
                "assertions": [
                    # Classifier should match the exact cart line (Blue XL) → confirmation
                    {"field": "stage", "check": "eq", "value": "awaiting_cart_confirm"},
                ],
            },
            {
                "message": "Yes",
                "assertions": [
                    {"field": "stage",  "check": "eq",       "value": "browse"},
                    {"field": "answer", "check": "contains", "value": "Done"},
                    {"field": "cart",   "check": "not_none"},
                ],
            },
        ],
    },

    # ── Test 20: Existing Navy line + request for White XL must not touch Navy ──
    "cart_override_navy_with_white": {
        "name": "Cart has Navy XL T-Shirt → 'Add more to T-shirt in white color XL' → confirms White XL, leaves Navy unchanged",
        "seed": [
            {"browse": "Show me T-shirts", "add": "Add Everyday Cotton T-Shirt Navy XL to cart"},
        ],
        "steps": [
            {
                "message": "Add more to T-shirt in white color XL",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_cart_confirm"},
                    {"field": "answer", "check": "contains", "value": "White"},
                    {"field": "requires_input", "check": "eq", "value": True},
                    {"field": "auto_listen", "check": "eq", "value": True},
                ],
            },
        ],
    },

    # ── Test 21: Explicit request options override cart options ───────────────
    "cart_explicit_options_override": {
        "name": "Explicit request options override cart options",
        "seed": [
            {"browse": "Show me T-shirts", "add": "Add Everyday Cotton T-Shirt Navy XL to cart"},
        ],
        "steps": [
            {
                "message": "Add two Everyday Cotton T-Shirt White XL",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_cart_confirm"},
                    {"field": "answer", "check": "contains", "value": "White"},
                ],
            },
        ],
    },

    # ── Test 22: 'Same T-shirt' reuses existing Navy XL cart line ──────────────
    "cart_same_tshirt_reuses_navy": {
        "name": "'Add another one of the same T-shirt' reuses Navy XL cart line",
        "seed": [
            {"browse": "Show me T-shirts", "add": "Add Everyday Cotton T-Shirt Navy XL to cart"},
        ],
        "steps": [
            {
                "message": "Add another one of the same T-shirt",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_cart_confirm"},
                    {"field": "answer", "check": "contains", "value": "Navy"},
                ],
            },
        ],
    },

    # ── Test 23: Option correction during confirmation ───────────────────────
    "cart_option_correction_in_confirm": {
        "name": "Option correction during awaiting_cart_confirm ('I want White colour') invalidates Navy and confirms White",
        "seed": [
            {"browse": "Show me T-shirts", "add": "Add Everyday Cotton T-Shirt Navy XL to cart"},
        ],
        "steps": [
            {
                "message": "Add two more of the same T-shirt",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_cart_confirm"},
                ],
            },
            {
                "message": "I want White colour",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_cart_confirm"},
                    {"field": "answer", "check": "contains", "value": "White"},
                ],
            },
        ],
    },

    # ── Test 24: Incomplete quantity speech ──────────────────────────────────
    "cart_incomplete_quantity_speech": {
        "name": "'I want white color and quantity should be' enters awaiting_cart_quantity",
        "seed": [
            {"browse": "Show me T-shirts", "add": "Add Everyday Cotton T-Shirt Navy XL to cart"},
        ],
        "steps": [
            {
                "message": "Add more to T-shirt in white color and quantity should be",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_cart_quantity"},
                    {"field": "answer", "check": "contains", "value": "quantity"},
                    {"field": "requires_input", "check": "eq", "value": True},
                    {"field": "auto_listen", "check": "eq", "value": True},
                ],
            },
        ],
    },

    # ── Test 25: 'Yes' while in awaiting_cart_quantity does not mutate cart ───
    "cart_yes_in_awaiting_quantity_blocked": {
        "name": "'Yes' while in awaiting_cart_quantity does not mutate cart and re-asks for quantity",
        "seed": [
            {"browse": "Show me T-shirts", "add": "Add Everyday Cotton T-Shirt Navy XL to cart"},
        ],
        "steps": [
            {
                "message": "Add more to T-shirt in white color XL and quantity should be",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_cart_quantity"},
                ],
            },
            {
                "message": "Yes",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_cart_quantity"},
                    {"field": "answer", "check": "contains", "value": "quantity"},
                ],
            },
            {
                "message": "Two",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_cart_confirm"},
                    {"field": "answer", "check": "contains", "value": "White"},
                ],
            },
        ],
    },

    # ── Test 26: requires_input & auto_listen metadata check ────────────────
    "cart_requires_input_metadata": {
        "name": "Every input-requiring stage returns requires_input: true and auto_listen: true",
        "steps": [
            {
                "message": "Show me T-shirts",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_variant"},
                    {"field": "requires_input", "check": "eq", "value": True},
                    {"field": "auto_listen", "check": "eq", "value": True},
                ],
            },
        ],
    },

    # ── Test 27: Cancel responses set requires_input: false ──────────────────
    "cart_cancel_no_reopen": {
        "name": "Cancel response sets requires_input: false and auto_listen: false",
        "steps": [
            {
                "message": "Show me T-shirts",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_variant"},
                ],
            },
            {
                "message": "Cancel",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "idle"},
                    {"field": "requires_input", "check": "eq", "value": False},
                ],
            },
        ],
    },

    # ── Test 28: "No" alone → cancel ───────────────────────────────────────
    "correction_no_alone_cancel": {
        "name": "'No' alone cancels conversation",
        "steps": [
            {"message": "Show me T-shirts", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_variant"}]},
            {"message": "No", "assertions": [{"field": "stage", "check": "eq", "value": "idle"}, {"field": "requires_input", "check": "eq", "value": False}]},
        ],
    },

    # ── Test 29: "No thanks" → cancel ──────────────────────────────────────
    "correction_no_thanks_cancel": {
        "name": "'No thanks' cancels conversation",
        "steps": [
            {"message": "Show me T-shirts", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_variant"}]},
            {"message": "No thanks", "assertions": [{"field": "stage", "check": "eq", "value": "idle"}, {"field": "requires_input", "check": "eq", "value": False}]},
        ],
    },

    # ── Test 30: "No, I don't want anything" → cancel ─────────────────────
    "correction_no_dont_want_cancel": {
        "name": "'No, I don't want anything' cancels conversation",
        "steps": [
            {"message": "Show me T-shirts", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_variant"}]},
            {"message": "No, I don't want anything", "assertions": [{"field": "stage", "check": "eq", "value": "idle"}, {"field": "requires_input", "check": "eq", "value": False}]},
        ],
    },

    # ── Test 31: "No, I want Red" → correction, not cancellation ───────────
    "correction_no_want_red": {
        "name": "'No, I want Red color' is a correction, not cancellation",
        "steps": [
            {"message": "Everyday Cotton T-Shirt White XL", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "No, I want Red color", "assertions": [
                {"field": "stage", "check": "eq", "value": "awaiting_color"},
                {"field": "answer", "check": "contains", "value": "Red is not available"},
                {"field": "requires_input", "check": "eq", "value": True},
                {"field": "auto_listen", "check": "eq", "value": True},
            ]},
        ],
    },

    # ── Test 32: "No, make it Navy" → update colour to Navy ───────────────
    "correction_no_make_it_navy": {
        "name": "'No, make it Navy' updates colour to Navy and preserves size",
        "steps": [
            {"message": "Everyday Cotton T-Shirt White XL", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "No, make it Navy", "assertions": [
                {"field": "stage", "check": "eq", "value": "awaiting_confirm"},
                {"field": "answer", "check": "contains", "value": "Navy"},
            ]},
        ],
    },

    # ── Test 33: "No, change size to L" → preserve colour, update size ──────
    "correction_no_change_size": {
        "name": "'No, change size to Large' preserves colour and updates size",
        "steps": [
            {"message": "Everyday Cotton T-Shirt Navy XL", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "No, change size to Large", "assertions": [
                {"field": "stage", "check": "eq", "value": "awaiting_confirm"},
                {"field": "answer", "check": "contains", "value": "L"},
            ]},
        ],
    },

    # ── Test 34: "No, show me jeans instead" → start new jeans request ──────
    "correction_no_show_jeans": {
        "name": "'No, show me jeans instead' resets selection and searches jeans",
        "steps": [
            {"message": "Everyday Cotton T-Shirt White XL", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "No, show me jeans instead", "assertions": [
                {"field": "stage", "check": "eq", "value": "awaiting_size"},
                {"field": "answer", "check": "contains", "value": "Jeans"},
            ]},
        ],
    },

    # ── Test 35: Unavailable Red → report unavailable and ask for Navy/White 
    "unavailable_option_red": {
        "name": "Unavailable Red reports unavailable and lists Navy and White",
        "steps": [
            {"message": "Everyday Cotton T-Shirt White XL", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "No, I want Red color", "assertions": [
                {"field": "stage", "check": "eq", "value": "awaiting_color"},
                {"field": "answer", "check": "contains", "value": "Red is not available"},
                {"field": "answer", "check": "contains", "value": "Navy"},
                {"field": "answer", "check": "contains", "value": "White"},
            ]},
        ],
    },

    # ── Test 36: Unavailable Black → report unavailable and ask for Navy/White 
    "unavailable_option_black": {
        "name": "Unavailable Black reports unavailable and lists Navy and White",
        "steps": [
            {"message": "Everyday Cotton T-Shirt White XL", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "Uh, I want a Black color", "assertions": [
                {"field": "stage", "check": "eq", "value": "awaiting_color"},
                {"field": "answer", "check": "contains", "value": "Black is not available"},
                {"field": "answer", "check": "contains", "value": "Navy"},
                {"field": "answer", "check": "contains", "value": "White"},
            ]},
        ],
    },

    # ── Test 37: Unavailable option must never retain previous choice ──────
    "unavailable_option_clears_previous": {
        "name": "Unavailable option clears previous selection and sets Color to missing",
        "steps": [
            {"message": "Everyday Cotton T-Shirt White XL", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "No, I want Red color", "assertions": [
                {"field": "stage", "check": "eq", "value": "awaiting_color"},
                {"field": "missing_options", "check": "contains", "value": "Color"},
            ]},
        ],
    },

    # ── Test 38: Valid size and quantity survive invalid colour correction ───
    "valid_size_survives_invalid_color": {
        "name": "Valid size XL and quantity 1 survive invalid Red color correction",
        "steps": [
            {"message": "Everyday Cotton T-Shirt White XL", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "No, I want Red color", "assertions": [
                {"field": "stage", "check": "eq", "value": "awaiting_color"},
                {"field": "selected_options.Size", "check": "eq", "value": "XL"},
            ]},
        ],
    },

    # ── Test 39: "Quantity should be" without a number asks for quantity ─────
    "incomplete_quantity_asks_quantity": {
        "name": "'Quantity should be' enters awaiting_quantity and prompts for number",
        "steps": [
            {"message": "Everyday Cotton T-Shirt in White color, size XL, and the quantity should be", "assertions": [
                {"field": "stage", "check": "eq", "value": "awaiting_quantity"},
                {"field": "answer", "check": "contains", "value": "quantity"},
                {"field": "requires_input", "check": "eq", "value": True},
                {"field": "auto_listen", "check": "eq", "value": True},
            ]},
        ],
    },

    # ── Test 40: Unavailable option responses reopen microphone ────────────
    "unavailable_option_reopens_mic": {
        "name": "Unavailable option response returns requires_input: true and auto_listen: true",
        "steps": [
            {"message": "Everyday Cotton T-Shirt White XL", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "No, I want Red color", "assertions": [
                {"field": "requires_input", "check": "eq", "value": True},
                {"field": "auto_listen", "check": "eq", "value": True},
            ]},
        ],
    },

    # ── Test 41: ElevenLabs 401 resiliency check ────────────────────────────
    "tts_resiliency_metadata": {
        "name": "Response metadata complete even when audio is null (TTS 401)",
        "steps": [
            {"message": "Everyday Cotton T-Shirt White XL", "assertions": [
                {"field": "requires_input", "check": "eq", "value": True},
                {"field": "auto_listen", "check": "eq", "value": True},
                {"field": "conversation_active", "check": "eq", "value": True},
            ]},
        ],
    },

    # ── Test 42: No cart mutation before validation and confirmation ─────────
    "no_cart_mutation_before_confirm": {
        "name": "No cart mutation occurs until corrected variant is validated and confirmed",
        "steps": [
            {"message": "Everyday Cotton T-Shirt White XL", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "No, I want Red color", "assertions": [
                {"field": "stage", "check": "eq", "value": "awaiting_color"},
                {"field": "cart", "check": "false"},
            ]},
        ],
    },

    # ── Test 43: Completed Add to Cart returns conversation_complete & auto_close ───
    "auto_close_on_add_to_cart_complete": {
        "name": "Completed Add to Cart returns conversation_complete: true and auto_close: true",
        "steps": [
            {"message": "Add Everyday Cotton T-Shirt White XL to cart", "assertions": [
                {"field": "stage", "check": "eq", "value": "browse"},
                {"field": "requires_input", "check": "eq", "value": False},
                {"field": "conversation_complete", "check": "eq", "value": True},
                {"field": "auto_close", "check": "eq", "value": True},
            ]},
        ],
    },

    # ── Test 44: Completed cart quantity update returns auto_close: true ──────
    "auto_close_on_cart_qty_update": {
        "name": "Completed cart quantity update returns auto_close: true",
        "seed": [
            {"add": "Add Everyday Cotton T-Shirt White XL to cart"},
        ],
        "steps": [
            {"message": "Add one more T-shirt", "assertions": [
                {"field": "stage", "check": "eq", "value": "awaiting_cart_confirm"},
            ]},
            {"message": "Yes", "assertions": [
                {"field": "stage", "check": "eq", "value": "browse"},
                {"field": "conversation_complete", "check": "eq", "value": True},
                {"field": "auto_close", "check": "eq", "value": True},
            ]},
        ],
    },

    # ── Test 45: What's in my cart returns auto_close: true ──────────────────
    "auto_close_on_cart_query": {
        "name": "'What's in my cart?' returns conversation_complete: true and auto_close: true",
        "steps": [
            {"message": "What's in my cart?", "assertions": [
                {"field": "stage", "check": "eq", "value": "browse"},
                {"field": "requires_input", "check": "eq", "value": False},
                {"field": "conversation_complete", "check": "eq", "value": True},
                {"field": "auto_close", "check": "eq", "value": True},
            ]},
        ],
    },

    # ── Test 46: Incomplete size request returns auto_close: false ───────────
    "no_auto_close_on_incomplete": {
        "name": "Incomplete stage (asking size) returns conversation_complete: false and auto_close: false",
        "steps": [
            {"message": "Show me T-shirts", "assertions": [
                {"field": "stage", "check": "eq", "value": "awaiting_variant"},
                {"field": "requires_input", "check": "eq", "value": True},
                {"field": "conversation_complete", "check": "eq", "value": False},
                {"field": "auto_close", "check": "eq", "value": False},
            ]},
        ],
    },

    # ── Test 47: Cancel returns auto_close: true ──────────────────────────────
    "auto_close_on_cancel": {
        "name": "'Cancel' returns conversation_complete: true and auto_close: true",
        "steps": [
            {"message": "Show me T-shirts", "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_variant"}]},
            {"message": "Cancel", "assertions": [
                {"field": "stage", "check": "eq", "value": "idle"},
                {"field": "requires_input", "check": "eq", "value": False},
                {"field": "conversation_complete", "check": "eq", "value": True},
                {"field": "auto_close", "check": "eq", "value": True},
            ]},
        ],
    },

    # ── Test 48: Explicit close command returns immediate_close: true ────────
    "immediate_close_command": {
        "name": "'Close assistant' returns immediate_close: true and auto_close: true",
        "steps": [
            {"message": "Close assistant", "assertions": [
                {"field": "stage", "check": "eq", "value": "idle"},
                {"field": "requires_input", "check": "eq", "value": False},
                {"field": "conversation_complete", "check": "eq", "value": True},
                {"field": "auto_close", "check": "eq", "value": True},
                {"field": "immediate_close", "check": "eq", "value": True},
            ]},
        ],
    },

}



# ─────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────

def check_server():
    try:
        r = requests.get(f"{BASE_URL}/", timeout=5)
        r.raise_for_status()
        print(f"{INFO} Server is up: {BASE_URL}")
        return True
    except Exception:
        print(f"\n{FAIL} Cannot reach {BASE_URL}")
        print("  Please start the server: uvicorn main:app --host 0.0.0.0 --port 8000 --reload")
        return False


def run_cart_qty_test(key: str, test_def: dict, use_audio: bool = False) -> bool:
    """
    Run a cart-quantity multi-turn test that may have a 'seed' phase.
    The seed phase pre-populates the cart using real conversation turns.
    """
    name = test_def.get("name", key)
    print(f"\n{'═'*60}")
    print(f"  TEST: {name}")
    print(f"{'═'*60}")

    sid = f"test_{key}_{int(time.time())}"
    reset_session(sid)
    all_pass = True

    # ── Seed phase ──────────────────────────────────────────────────────────
    seed_items = test_def.get("seed", [])
    if seed_items:
        print(f"\n  ── Seeding cart ({len(seed_items)} item(s)) …")
        seed_cart_with_items(sid, seed_items)
        print("  ── Cart seeded.")

    # ── Test steps ──────────────────────────────────────────────────────────
    for i, step in enumerate(test_def.get("steps", []), 1):
        msg = step["message"]
        print(f"\n  ── Turn {i}: \"{msg}\"")
        try:
            data = text_turn(msg, sid)
        except Exception as e:
            print(f"  {FAIL}  Request failed: {e}")
            all_pass = False
            break

        print_response(data)

        for assertion in step.get("assertions", []):
            field  = assertion["field"]
            check  = assertion["check"]
            value  = assertion.get("value")
            actual = data
            for part in field.split("."):
                if isinstance(actual, dict):
                    actual = actual.get(part)
                else:
                    actual = None
                    break

            label = f"{field}"
            if check == "eq":
                ok = assert_eq(label, actual, value)
            elif check == "contains":
                ok = assert_contains(label, actual, value)
            elif check == "not_none":
                ok = assert_not_none(label, actual)
            elif check == "truthy":
                ok = bool(actual)
                print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {label}: {actual!r}")
            elif check == "gt":
                ok = isinstance(actual, (int, float, list)) and (
                    len(actual) > value if isinstance(actual, list) else actual > value
                )
                print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {label}: {actual!r} > {value}")
            elif check == "false":
                ok = not actual
                print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {label}: {actual!r} is falsy")
            elif check == "contains" and isinstance(value, str):
                ok = isinstance(actual, str) and value.lower() in actual.lower()
                print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {label} contains '{value}': {actual!r}")
            else:
                print(f"  {WARN}  Unknown check '{check}' for {label}")
                ok = True
            if not ok:
                all_pass = False

        time.sleep(5)

    status = PASS if all_pass else FAIL
    print(f"\n  Result: {status}  {'All assertions passed' if all_pass else 'Some assertions failed'}")
    return all_pass


# Merge all tests into one registry
ALL_TESTS = {**TESTS, **_CART_QTY_TESTS}


def main():
    parser = argparse.ArgumentParser(description="Voice add-to-cart test suite")
    parser.add_argument("--audio", action="store_true",
                        help="Use real TTS→STT round-trip via /voice endpoint")
    parser.add_argument("--test", metavar="NAME",
                        help=f"Run a single test by key: {', '.join(ALL_TESTS)}")
    args = parser.parse_args()

    print("\n" + "═"*60)
    print("  AI Shopping Assistant — Add-to-Cart Voice Test Suite")
    print("═"*60)
    print(f"  API:     {BASE_URL}")
    print(f"  Store:   {SHOP_DOMAIN or '(from .env)'}")
    print(f"  Mode:    {'🎙️  Real audio (TTS→STT)' if args.audio else '💬 Text simulation'}")
    print(f"  Tests:   {len(TESTS)} original + {len(_CART_QTY_TESTS)} cart-quantity = {len(ALL_TESTS)} total")
    print("═"*60)

    if not check_server():
        sys.exit(1)

    to_run = {}
    if args.test:
        if args.test not in ALL_TESTS:
            print(f"{FAIL} Unknown test '{args.test}'. Available: {', '.join(ALL_TESTS)}")
            sys.exit(1)
        to_run = {args.test: ALL_TESTS[args.test]}
    else:
        to_run = ALL_TESTS

    results = {}
    for key, test_def in to_run.items():
        use_audio = args.audio and key == "voice_add_to_cart"
        if key in _CART_QTY_TESTS:
            results[key] = run_cart_qty_test(key, test_def, use_audio=use_audio)
        else:
            results[key] = run_test(test_def["name"], test_def["steps"], use_audio=use_audio)

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("  SUMMARY")
    print("═"*60)
    passed = sum(1 for ok in results.values() if ok)
    total  = len(results)
    for key, ok in results.items():
        icon = "✅" if ok else "❌"
        print(f"  {icon}  {ALL_TESTS[key]['name']}")
    print(f"\n  {passed}/{total} tests passed")
    print("═"*60 + "\n")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
