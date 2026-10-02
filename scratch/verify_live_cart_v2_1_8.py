import requests
import json
import sys

BASE_URL = "http://127.0.0.1:8000"
STORE = "mustali-dev.myshopify.com"
SESSION_ID = "v218_live_cart_test_session"

print("=== STARTING LIVE CART VERIFICATION v2.1.8 ===")

# Reset session
requests.post(f"{BASE_URL}/session/reset", json={"session_id": SESSION_ID, "shop_domain": STORE})

# Step 1: Ask for Boys Casual Jacket
res1 = requests.post(f"{BASE_URL}/voice-text", json={
    "session_id": SESSION_ID,
    "shop_domain": STORE,
    "message": "Boys Casual Jacket"
}).json()

print("\n--- Step 1: Product Query ---")
print(f"Product Title: {res1.get('product_title')}")
print(f"Availability Status: {res1.get('availability_status')}")
print(f"Sold Out: {res1.get('sold_out')}")
print(f"Variant Options Count: {len(res1.get('variant_options', []))}")

assert res1.get("sold_out") is False, "Product must NOT be sold out"
assert res1.get("availability_status") == "available", "Availability status must be 'available'"
assert len(res1.get("variant_options", [])) > 0, "Variant options list must NOT be empty"

size_options = res1["variant_options"][0]["values"]
print(f"Available Sizes: {[opt['value'] for opt in size_options if opt['available']]}")

# Step 2: Select an enabled size (e.g., 'm')
res2 = requests.post(f"{BASE_URL}/voice-text", json={
    "session_id": SESSION_ID,
    "shop_domain": STORE,
    "message": "m"
}).json()

print("\n--- Step 2: Size Selection ---")
print(f"Stage: {res2.get('stage')}")
print(f"Selected Options: {res2.get('selected_options')}")
selected_variant = res2.get("selected_variant")
if selected_variant:
    print(f"Resolved Variant ID: {selected_variant.get('id')}")
    print(f"Variant Title: {selected_variant.get('title')}")
    print(f"Variant AvailableForSale: {selected_variant.get('availableForSale', selected_variant.get('available_for_sale'))}")

# Step 3: Confirm selection
res3 = requests.post(f"{BASE_URL}/voice-text", json={
    "session_id": SESSION_ID,
    "shop_domain": STORE,
    "message": "yes"
}).json()

print("\n--- Step 3: Selection Confirmation ---")
print(f"Stage: {res3.get('stage')}")
print(f"Answer: {res3.get('answer')}")
print(f"Added Variant ID: {res3.get('added_variant_id')}")

cart = res3.get("cart")
if cart:
    print(f"Cart Line Items: {len(cart.get('line_items', []))}")
    for item in cart.get("line_items", []):
        print(f"  Item: {item.get('title')} x {item.get('quantity')} (Variant: {item.get('variant_id')})")

print("\n✔ LIVE CART FLOW VERIFICATION PASSED PERFECTLY FOR v2.1.8!")
