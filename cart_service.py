"""
cart_service.py

Add-to-cart via Shopify Storefront API (primary) with Draft Orders fallback.

Why Storefront API?
  • Works with a public Storefront token — no Admin write_draft_orders scope needed.
  • Returns a real checkout URL customers can use immediately.
  • No 403 Forbidden errors from missing Admin API permissions.

Draft Orders fallback:
  • Used only if SHOPIFY_STOREFRONT_TOKEN is absent AND SHOPIFY_ADMIN_TOKEN
    has the write_draft_orders scope.

Required .env variables:
  SHOPIFY_STORE_DOMAIN        = yourstore.myshopify.com
  SHOPIFY_STOREFRONT_TOKEN    = shpat_xxx  (Storefront API public token)
  SHOPIFY_ADMIN_TOKEN         = shpat_xxx  (Admin API token, optional fallback)
"""

import os
import requests
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

STOREFRONT_API_VERSION = "2024-01"
ADMIN_API_VERSION = "2026-07"

_DEFAULT_DOMAIN           = os.getenv("SHOPIFY_STORE_DOMAIN", "")
_DEFAULT_ADMIN_TOKEN      = os.getenv("SHOPIFY_ADMIN_TOKEN", "")
_DEFAULT_STOREFRONT_TOKEN = os.getenv("SHOPIFY_STOREFRONT_TOKEN", "")

# ────────────────────────────────────────────────────────────
# In-memory cart state keyed by session_id
# ────────────────────────────────────────────────────────────
_carts: dict[str, dict] = {}          # session_id → { cart_id, checkout_url, lines, ... }
_draft_orders: dict[str, dict] = {}   # session_id → draft order (fallback)


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _storefront_url(domain: str) -> str:
    d = domain.rstrip("/")
    if not d.startswith("http"):
        d = f"https://{d}"
    return f"{d}/api/{STOREFRONT_API_VERSION}/graphql.json"


def _admin_url(domain: str, path: str) -> str:
    d = domain.rstrip("/")
    if not d.startswith("http"):
        d = f"https://{d}"
    return f"{d}/admin/api/{ADMIN_API_VERSION}/{path}"


def _gid_to_id(gid: str) -> int:
    return int(gid.split("/")[-1])


def _storefront_headers(storefront_token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "X-Shopify-Storefront-Access-Token": storefront_token,
    }


def _admin_headers(admin_token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": admin_token,
    }


# ────────────────────────────────────────────────────────────
# Storefront API — Cart (primary path)
# ────────────────────────────────────────────────────────────

_GQL_CART_CREATE = """
mutation cartCreate($lines: [CartLineInput!]!) {
  cartCreate(input: { lines: $lines }) {
    cart {
      id
      checkoutUrl
      totalQuantity
      cost {
        totalAmount { amount currencyCode }
        subtotalAmount { amount currencyCode }
      }
      lines(first: 50) {
        nodes {
          id
          quantity
          merchandise {
            ... on ProductVariant {
              id
              title
              price { amount currencyCode }
              product { title featuredImage { url } onlineStoreUrl }
            }
          }
        }
      }
    }
    userErrors { field message }
  }
}
"""

_GQL_CART_ADD = """
mutation cartLinesAdd($cartId: ID!, $lines: [CartLineInput!]!) {
  cartLinesAdd(cartId: $cartId, lines: $lines) {
    cart {
      id
      checkoutUrl
      totalQuantity
      cost {
        totalAmount { amount currencyCode }
        subtotalAmount { amount currencyCode }
      }
      lines(first: 50) {
        nodes {
          id
          quantity
          merchandise {
            ... on ProductVariant {
              id
              title
              price { amount currencyCode }
              product { title featuredImage { url } onlineStoreUrl }
            }
          }
        }
      }
    }
    userErrors { field message }
  }
}
"""

_GQL_CART_REMOVE = """
mutation cartLinesRemove($cartId: ID!, $lineIds: [ID!]!) {
  cartLinesRemove(cartId: $cartId, lineIds: $lineIds) {
    cart {
      id
      checkoutUrl
      totalQuantity
      cost {
        totalAmount { amount currencyCode }
        subtotalAmount { amount currencyCode }
      }
      lines(first: 50) {
        nodes {
          id
          quantity
          merchandise {
            ... on ProductVariant {
              id
              title
              price { amount currencyCode }
              product { title featuredImage { url } onlineStoreUrl }
            }
          }
        }
      }
    }
    userErrors { field message }
  }
}
"""

_GQL_CART_UPDATE = """
mutation cartLinesUpdate($cartId: ID!, $lines: [CartLineUpdateInput!]!) {
  cartLinesUpdate(cartId: $cartId, lines: $lines) {
    cart {
      id
      checkoutUrl
      totalQuantity
      cost {
        totalAmount { amount currencyCode }
        subtotalAmount { amount currencyCode }
      }
      lines(first: 50) {
        nodes {
          id
          quantity
          merchandise {
            ... on ProductVariant {
              id
              title
              price { amount currencyCode }
              product { title featuredImage { url } onlineStoreUrl }
            }
          }
        }
      }
    }
    userErrors { field message }
  }
}
"""


def _storefront_cart_create(domain: str, storefront_token: str, variant_gid: str, quantity: int) -> dict:
    resp = requests.post(
        _storefront_url(domain),
        json={
            "query": _GQL_CART_CREATE,
            "variables": {"lines": [{"merchandiseId": variant_gid, "quantity": quantity}]},
        },
        headers=_storefront_headers(storefront_token),
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, dict) or "data" not in data or not data.get("data"):
        raise Exception(f"Storefront cart create GraphQL error: {data.get('errors') if isinstance(data, dict) else data}")

    cart_create = data["data"].get("cartCreate") or {}
    errors = cart_create.get("userErrors", [])
    if errors:
        raise Exception(f"Storefront cart create userErrors: {errors}")

    return cart_create.get("cart", {})


def _storefront_cart_add(domain: str, storefront_token: str, cart_id: str, variant_gid: str, quantity: int) -> dict:
    resp = requests.post(
        _storefront_url(domain),
        json={
            "query": _GQL_CART_ADD,
            "variables": {"cartId": cart_id, "lines": [{"merchandiseId": variant_gid, "quantity": quantity}]},
        },
        headers=_storefront_headers(storefront_token),
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, dict) or "data" not in data or not data.get("data"):
        raise Exception(f"Storefront cart add GraphQL error: {data.get('errors') if isinstance(data, dict) else data}")

    cart_add = data["data"].get("cartLinesAdd") or {}
    errors = cart_add.get("userErrors", [])
    if errors:
        raise Exception(f"Storefront cart add userErrors: {errors}")

    return cart_add.get("cart", {})


def _storefront_cart_remove(domain: str, storefront_token: str, cart_id: str, line_ids: list[str]) -> dict:
    resp = requests.post(
        _storefront_url(domain),
        json={
            "query": _GQL_CART_REMOVE,
            "variables": {"cartId": cart_id, "lineIds": line_ids},
        },
        headers=_storefront_headers(storefront_token),
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, dict) or "data" not in data or not data.get("data"):
        raise Exception(f"Storefront cart remove GraphQL error: {data.get('errors') if isinstance(data, dict) else data}")

    errors = data["data"].get("cartLinesRemove", {}).get("userErrors", [])
    if errors:
        raise Exception(f"Storefront cart remove error: {errors}")

    return data["data"]["cartLinesRemove"]["cart"]


def _storefront_cart_update(domain: str, storefront_token: str, cart_id: str, line_id: str, new_quantity: int) -> dict:
    resp = requests.post(
        _storefront_url(domain),
        json={
            "query": _GQL_CART_UPDATE,
            "variables": {"cartId": cart_id, "lines": [{"id": line_id, "quantity": new_quantity}]},
        },
        headers=_storefront_headers(storefront_token),
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, dict) or "data" not in data or not data.get("data"):
        raise Exception(f"Storefront cart update GraphQL error: {data.get('errors') if isinstance(data, dict) else data}")

    errors = data["data"].get("cartLinesUpdate", {}).get("userErrors", [])
    if errors:
        raise Exception(f"Storefront cart update error: {errors}")

    return data["data"]["cartLinesUpdate"]["cart"]




def _serialise_storefront_cart(cart: dict) -> dict:
    lines = []
    for node in cart.get("lines", {}).get("nodes", []):
        merch = node.get("merchandise", {})
        product = merch.get("product", {})
        lines.append({
            "line_id":       node.get("id", ""),
            "quantity":      node.get("quantity", 1),
            "variant_id":    merch.get("id", ""),
            "variant_title": merch.get("title", ""),
            "product_title": product.get("title", ""),
            "product_image": (product.get("featuredImage") or {}).get("url"),
            "product_url":   product.get("onlineStoreUrl"),
            "price":         merch.get("price", {}).get("amount", "0.00"),
            "currency":      merch.get("price", {}).get("currencyCode", "USD"),
            "line_total":    str(
                float(merch.get("price", {}).get("amount", 0)) * node.get("quantity", 1)
            ),
        })
    cost = cart.get("cost", {})
    total = cost.get("totalAmount", {}).get("amount", "0.00")
    subtotal = cost.get("subtotalAmount", {}).get("amount", "0.00")
    return {
        "cart_id":        cart.get("id", ""),
        "checkout_url":   cart.get("checkoutUrl"),
        "total_quantity": cart.get("totalQuantity", len(lines)),
        "subtotal":       subtotal,
        "total":          total,
        "currency":       cost.get("totalAmount", {}).get("currencyCode", "USD"),
        "lines":          lines,
    }


# ────────────────────────────────────────────────────────────
# Draft Orders fallback (Admin API)
# ────────────────────────────────────────────────────────────

def _create_draft(domain: str, token: str, variant_numeric_id: int, quantity: int) -> dict:
    payload = {
        "draft_order": {
            "line_items": [{"variant_id": variant_numeric_id, "quantity": quantity}],
            "note": "Added via AI Shopping Assistant",
        }
    }
    resp = requests.post(
        _admin_url(domain, "draft_orders.json"),
        json=payload,
        headers=_admin_headers(token),
        timeout=20,
    )
    if resp.status_code == 403:
        raise PermissionError(
            "Admin token lacks 'write_draft_orders' scope. "
            "Add SHOPIFY_STOREFRONT_TOKEN to .env to use the Storefront API instead, "
            "or enable write_draft_orders in your Shopify Custom App settings."
        )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise Exception(f"Draft order create error: {data['errors']}")
    return data["draft_order"]


def _add_line_to_draft(domain: str, token: str, draft_id: int, variant_numeric_id: int, quantity: int) -> dict:
    get_resp = requests.get(
        _admin_url(domain, f"draft_orders/{draft_id}.json"),
        headers=_admin_headers(token),
        timeout=15,
    )
    get_resp.raise_for_status()
    existing_lines = get_resp.json()["draft_order"].get("line_items", [])

    found = False
    for line in existing_lines:
        if line.get("variant_id") == variant_numeric_id:
            line["quantity"] = line.get("quantity", 0) + quantity
            found = True
            break
    if not found:
        existing_lines.append({"variant_id": variant_numeric_id, "quantity": quantity})

    clean_lines = [
        {"variant_id": l["variant_id"], "quantity": l["quantity"]}
        for l in existing_lines if l.get("variant_id")
    ]

    put_resp = requests.put(
        _admin_url(domain, f"draft_orders/{draft_id}.json"),
        json={"draft_order": {"line_items": clean_lines}},
        headers=_admin_headers(token),
        timeout=20,
    )
    put_resp.raise_for_status()
    return put_resp.json()["draft_order"]


def _serialise_draft(draft: dict, product: dict, variant: dict) -> dict:
    lines = []
    for item in draft.get("line_items", []):
        lines.append({
            "line_id":       str(item.get("id", "")),
            "quantity":      item.get("quantity", 1),
            "variant_id":    str(item.get("variant_id", "")),
            "variant_title": item.get("variant_title") or item.get("title", ""),
            "product_title": item.get("title", product.get("title", "")),
            "product_image": (product.get("featuredImage") or {}).get("url"),
            "product_url":   product.get("onlineStoreUrl"),
            "price":         str(item.get("price", variant.get("price", "0.00"))),
            "currency":      draft.get("currency", "USD"),
            "line_total":    str(float(item.get("price", 0)) * item.get("quantity", 1)),
        })
    subtotal = draft.get("subtotal_price", "0.00")
    total = draft.get("total_price", subtotal)
    return {
        "cart_id":        f"draft_{draft['id']}",
        "draft_id":       draft["id"],
        "checkout_url":   draft.get("invoice_url"),
        "total_quantity": sum(l["quantity"] for l in lines),
        "subtotal":       subtotal,
        "total":          total,
        "currency":       draft.get("currency", "USD"),
        "lines":          lines,
    }


# ────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────

from shopify_auth import ShopContext, get_store_credentials
from shopify_service import _resolve_shop_credentials

def save_pending_cart_selection(
    session_id: str,
    product: dict = None,
    variant: dict = None,
    quantity: int = 1,
) -> dict:
    """
    Saves a pending product selection into internal session memory.
    Performs ZERO Shopify API requests (no Storefront or Draft Orders calls).
    The actual cart mutation is performed natively in the browser via /cart/add.js.
    """
    if not session_id:
        return {"cart_id": "", "total_quantity": 0, "lines": []}

    existing = _carts.get(session_id, {"cart_id": session_id, "lines": [], "total_quantity": 0, "subtotal": "0.00", "total": "0.00", "currency": "USD"})
    lines = existing.get("lines", [])

    v_id = variant.get("id") if isinstance(variant, dict) else None
    v_title = variant.get("title") if isinstance(variant, dict) else ""
    p_title = product.get("title") if isinstance(product, dict) else ""
    p_img = (product.get("featuredImage") or {}).get("url") if isinstance(product, dict) else None
    p_url = product.get("onlineStoreUrl") if isinstance(product, dict) else None
    price = str(variant.get("price", "0.00")) if isinstance(variant, dict) else "0.00"

    found = False
    for line in lines:
        if line.get("variant_id") == str(v_id):
            line["quantity"] = line.get("quantity", 0) + quantity
            line["line_total"] = f"{float(price) * line['quantity']:.2f}"
            found = True
            break

    if not found and v_id:
        lines.append({
            "line_id": str(v_id),
            "quantity": quantity,
            "variant_id": str(v_id),
            "variant_title": v_title,
            "product_title": p_title,
            "product_image": p_img,
            "product_url": p_url,
            "price": price,
            "currency": "USD",
            "line_total": f"{float(price) * quantity:.2f}",
        })

    total_qty = sum(l.get("quantity", 1) for l in lines)
    total_val = sum(float(l.get("price", 0)) * l.get("quantity", 1) for l in lines)

    updated = {
        "cart_id": existing.get("cart_id", session_id),
        "checkout_url": "/cart",
        "total_quantity": total_qty,
        "subtotal": f"{total_val:.2f}",
        "total": f"{total_val:.2f}",
        "currency": "USD",
        "lines": lines,
    }
    _carts[session_id] = updated
    return updated


def remove_from_cart_by_variant(
    session_id: str,
    shop_domain: str = None,
    variant_id: str = None,
    shop_context: Optional[ShopContext] = None,
    admin_token: str = None,
    new_quantity: Optional[int] = None,
) -> dict:
    """
    Remove or update a line item from a session's in-memory cart context matching variant_id.
    """
    variant_str = str(variant_id) if variant_id else ""
    numeric_id = _gid_to_id(variant_str) if "gid://" in variant_str else (int(variant_str) if variant_str.isdigit() else None)

    existing = _carts.get(session_id)
    if existing:
        if new_quantity is not None and new_quantity > 0:
            for l in existing.get("lines", []):
                l_vid = str(l.get("variant_id", ""))
                l_nid = _gid_to_id(l_vid) if "gid://" in l_vid else (int(l_vid) if l_vid.isdigit() else None)
                if l_nid == numeric_id or l_vid == variant_str:
                    l["quantity"] = new_quantity
        else:
            existing["lines"] = [
                l for l in existing.get("lines", [])
                if (_gid_to_id(str(l.get("variant_id", ""))) if "gid://" in str(l.get("variant_id","")) else str(l.get("variant_id",""))) != (numeric_id or variant_str)
            ]
        existing["total_quantity"] = sum(l.get("quantity", 1) for l in existing["lines"])
        return existing

    return {"cart_id": "", "total_quantity": 0, "lines": []}


def get_session_cart_context(session_id: str) -> dict | None:
    """Return the cached cart context for a session (or None)."""
    return _carts.get(session_id)


def clear_session_cart_context(session_id: str):
    """Remove session cart state (e.g. on reset)."""
    _carts.pop(session_id, None)
    _draft_orders.pop(session_id, None)


# Aliases for backward compatibility
get_draft_order = get_session_cart_context
clear_draft_order = clear_session_cart_context
add_to_draft_order = save_pending_cart_selection


def sync_native_cart(session_id: str, cart_context: dict | None):
    """
    Sync backend session cart with native Shopify store cart (/cart.js).
    Enables instant matching if customer added/removed items via Shopify theme buttons.
    """
    if not cart_context or not isinstance(cart_context, dict):
        return
    items = cart_context.get("items", [])
    if not isinstance(items, list):
        return

    lines = []
    for item in items:
        price_val = item.get("price", 0)
        price_str = f"{price_val / 100.0:.2f}" if isinstance(price_val, (int, float)) and price_val > 50 else str(price_val)
        
        line_price_val = item.get("final_line_price", item.get("line_price", 0))
        line_total_str = f"{line_price_val / 100.0:.2f}" if isinstance(line_price_val, (int, float)) and line_price_val > 50 else str(line_price_val)

        lines.append({
            "line_id": str(item.get("key", item.get("id", ""))),
            "quantity": item.get("quantity", 1),
            "variant_id": str(item.get("variant_id", item.get("id", ""))),
            "variant_title": item.get("variant_title") or item.get("title", ""),
            "product_title": item.get("product_title") or item.get("title", ""),
            "product_image": item.get("image"),
            "product_url": item.get("url"),
            "price": price_str,
            "currency": cart_context.get("currency", "USD"),
            "line_total": line_total_str,
        })

    existing = _carts.get(session_id, {})
    total_qty = cart_context.get("item_count", sum(l["quantity"] for l in lines))
    total_price = cart_context.get("total_price", 0)
    total_str = f"{total_price / 100.0:.2f}" if isinstance(total_price, (int, float)) and total_price > 50 else str(total_price)

    _carts[session_id] = {
        "cart_id": existing.get("cart_id", session_id),
        "checkout_url": existing.get("checkout_url", "/cart"),
        "total_quantity": total_qty,
        "subtotal": total_str,
        "total": total_str,
        "currency": cart_context.get("currency", "USD"),
        "lines": lines,
    }


def fetch_live_cart_line(
    session_id: str,
    line_id: str = None,
    variant_id: str = None,
) -> Optional[dict]:
    """
    Return the current cart line matching line_id or variant_id.
    Uses the backend-managed cache as the authoritative source
    (refreshed from Shopify whenever sync_native_cart is called).
    Returns None if the session has no cart or the line does not exist.
    """
    cart = get_draft_order(session_id)
    if not cart:
        return None
    for line in cart.get("lines", []):
        if line_id and line.get("line_id") == line_id:
            return line
        if variant_id:
            lv = str(line.get("variant_id", ""))
            # Accept both GID and numeric forms
            if lv == str(variant_id):
                return line
            try:
                if _gid_to_id(lv) == _gid_to_id(str(variant_id)):
                    return line
            except (ValueError, IndexError):
                pass
    return None


def update_cart_line_quantity(
    session_id: str,
    shop_domain: str,
    line_id: str,
    variant_id: str,
    new_quantity: int,
    admin_token: str = None,
) -> dict:
    """
    Set a specific cart line to a new absolute quantity.

    Strategy:
      1. Validate the line still exists in the backend cache.
      2. If new_quantity <= 0, remove the line entirely.
      3. Use Storefront API cartLinesUpdate (preferred).
      4. Fall back to remove_from_cart_by_variant with new_quantity (Draft Orders).

    The backend cache is updated so subsequent reads reflect the change without
    needing a round-trip to Shopify.
    """
    domain = shop_domain or _DEFAULT_DOMAIN
    storefront_token = _DEFAULT_STOREFRONT_TOKEN
    resolved_admin_token = admin_token or _DEFAULT_ADMIN_TOKEN

    existing = _carts.get(session_id)

    # ── Storefront API path ──────────────────────────────────────────────────
    if existing and storefront_token:
        cart_gid = existing.get("cart_id", "")
        if str(cart_gid).startswith("gid://shopify/Cart/"):
            # Resolve actual line_id (may come from cache or caller)
            actual_line_id = line_id
            for l in existing.get("lines", []):
                if l.get("line_id") == line_id:
                    actual_line_id = l.get("line_id", line_id)
                    break
                # Fallback: match by variant_id
                lv = str(l.get("variant_id", ""))
                if lv == str(variant_id):
                    actual_line_id = l.get("line_id", line_id)
                    break
            try:
                if new_quantity <= 0:
                    cart = _storefront_cart_remove(
                        domain, storefront_token, cart_gid, [actual_line_id]
                    )
                else:
                    cart = _storefront_cart_update(
                        domain, storefront_token, cart_gid, actual_line_id, new_quantity
                    )
                serialised = _serialise_storefront_cart(cart)
                _carts[session_id] = serialised
                print(
                    f"[cart] Line quantity updated to {new_quantity} — "
                    f"total {serialised['total_quantity']} item(s)"
                )
                return serialised
            except Exception as exc:
                print(f"[cart] Storefront line update failed: {exc} — falling back")

    # ── Draft Orders / local fallback ────────────────────────────────────────
    return remove_from_cart_by_variant(
        session_id=session_id,
        shop_domain=shop_domain,
        variant_id=variant_id,
        admin_token=admin_token,
        new_quantity=new_quantity if new_quantity > 0 else None,
    )

