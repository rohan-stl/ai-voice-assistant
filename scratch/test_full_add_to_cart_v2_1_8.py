import requests
import json

BASE_URL = "http://127.0.0.1:8000"
STORE = "mustali-dev.myshopify.com"
SESSION_ID = "v218_full_add_to_cart_test"

requests.post(f"{BASE_URL}/session/reset", json={"session_id": SESSION_ID, "shop_domain": STORE})

# 1. Ask for Boys Casual Jacket
res1 = requests.post(f"{BASE_URL}/voice-text", json={
    "session_id": SESSION_ID,
    "shop_domain": STORE,
    "message": "Boys Casual Jacket"
}).json()
print("Step 1:", res1.get("answer"), "| Stage:", res1.get("stage"))

# 2. Select size xl
res2 = requests.post(f"{BASE_URL}/voice-text", json={
    "session_id": SESSION_ID,
    "shop_domain": STORE,
    "message": "xl"
}).json()
print("Step 2:", res2.get("answer"), "| Stage:", res2.get("stage"))

# 3. Confirm add to cart with "Yes" (triggers fast yes-confirm or confirm logic)
res3 = requests.post(f"{BASE_URL}/voice-text", json={
    "session_id": SESSION_ID,
    "shop_domain": STORE,
    "message": "Yes"
}).json()
print("Step 3:", res3.get("answer"), "| Stage:", res3.get("stage"))
print("Added Variant ID:", res3.get("added_variant_id"))
print("Cart:", json.dumps(res3.get("cart"), indent=2))

assert res3.get("added_variant_id") is not None or (res3.get("cart") and len(res3.get("cart", {}).get("lines", [])) > 0), "Variant MUST be added to cart!"
print("✔ ADD TO CART CONFIRMATION TEST PASSED CLEANLY!")
