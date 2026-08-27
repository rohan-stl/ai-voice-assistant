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
    if actual == expected:
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
        "name": "Browse T-shirts → select variant → confirm → add to cart",
        "steps": [
            {
                "message": "Show me T-shirts under $50",
                "assertions": [
                    # After filter result with matches, stage is awaiting_variant
                    {"field": "stage",    "check": "eq",  "value": "awaiting_variant"},
                    {"field": "products", "check": "gt",  "value": 0},
                ],
            },
            {
                "message": "Select Classic T-Shirt size M",
                "assertions": [
                    {"field": "stage",  "check": "eq",      "value": "awaiting_confirm"},
                    {"field": "answer", "check": "contains", "value": "confirm"},
                ],
            },
            {
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

    # ── Test 2: Voice command (TTS audio → /voice endpoint → STT) ──────────
    "voice_add_to_cart": {
        "name": "Voice command: browse + variant + confirm via /voice endpoint",
        "steps": [
            {
                "message": "Show me snowboards",
                "assertions": [
                    # Multiple snowboards → awaiting_variant
                    {"field": "stage",    "check": "eq", "value": "awaiting_variant"},
                    {"field": "products", "check": "gt", "value": 0},
                ],
            },
            {
                # Be specific — name the product and variant so no ambiguity
                "message": "I want The Complete Snowboard in Powder colour",
                "assertions": [
                    {"field": "stage", "check": "eq", "value": "awaiting_confirm"},
                ],
            },
            {
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
        "name": "Add quantity > 1 via voice: 'add 2 Classic T-Shirt Blue M'",
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
                "message": "Add 2 Classic T-Shirt Blue Medium to my cart",
                "assertions": [
                    {"field": "stage",    "check": "eq",      "value": "awaiting_confirm"},
                    {"field": "quantity", "check": "eq",       "value": 2},
                    {"field": "answer",   "check": "contains", "value": "2x"},
                ],
            },
            {
                "message": "Yes, confirm",
                "assertions": [
                    {"field": "stage",              "check": "eq", "value": "browse"},
                    {"field": "cart.total_quantity", "check": "gt", "value": 1},
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
                "message": "Classic T-Shirt size XXXL please",
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
                "message": "Show me snowboards",
                "assertions": [
                    # Multiple snowboards → awaiting_variant, user must pick
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
            {"message": "Select Classic T-Shirt size M",
             "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "No, cancel",
             "assertions": [
                 {"field": "stage", "check": "eq", "value": "awaiting_variant"},
                 {"field": "cart",  "check": "false"},
             ]},
            {"message": "Show me snowboards instead",
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
            {"message": "Add Classic T-Shirt Blue M to cart",
             "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "Yes",
             "assertions": [
                 {"field": "cart",               "check": "not_none"},
                 {"field": "cart.total_quantity", "check": "gt", "value": 0},
             ]},
            # Add a second item (The Complete Snowboard, Powder)
            {"message": "Show me snowboards",
             "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_variant"}]},
            {"message": "The Complete Snowboard in Powder colour",
             "assertions": [{"field": "stage", "check": "eq", "value": "awaiting_confirm"}]},
            {"message": "Add it",
             "assertions": [
                 {"field": "cart.total_quantity", "check": "gt", "value": 1},
                 {"field": "cart.checkout_url",   "check": "not_none"},
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


def main():
    parser = argparse.ArgumentParser(description="Voice add-to-cart test suite")
    parser.add_argument("--audio", action="store_true",
                        help="Use real TTS→STT round-trip via /voice endpoint")
    parser.add_argument("--test", metavar="NAME",
                        help=f"Run a single test by key: {', '.join(TESTS)}")
    args = parser.parse_args()

    print("\n" + "═"*60)
    print("  AI Shopping Assistant — Add-to-Cart Voice Test Suite")
    print("═"*60)
    print(f"  API:     {BASE_URL}")
    print(f"  Store:   {SHOP_DOMAIN or '(from .env)'}")
    print(f"  Mode:    {'🎙️  Real audio (TTS→STT)' if args.audio else '💬 Text simulation'}")
    print("═"*60)

    if not check_server():
        sys.exit(1)

    to_run = {}
    if args.test:
        if args.test not in TESTS:
            print(f"{FAIL} Unknown test '{args.test}'. Available: {', '.join(TESTS)}")
            sys.exit(1)
        to_run = {args.test: TESTS[args.test]}
    else:
        to_run = TESTS

    results = {}
    for key, test_def in to_run.items():
        # voice_add_to_cart test always uses real audio when --audio flag is set
        use_audio = args.audio and key == "voice_add_to_cart"
        results[key] = run_test(test_def["name"], test_def["steps"], use_audio=use_audio)

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("  SUMMARY")
    print("═"*60)
    passed = sum(1 for ok in results.values() if ok)
    total  = len(results)
    for key, ok in results.items():
        icon = "✅" if ok else "❌"
        print(f"  {icon}  {TESTS[key]['name']}")
    print(f"\n  {passed}/{total} tests passed")
    print("═"*60 + "\n")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
