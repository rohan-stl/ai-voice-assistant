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

def add_to_draft_order(
    session_id: str,
    shop_domain: str,
    product: dict,
    variant: dict,
    quantity: int = 1,
    admin_token: str = None,
    client_id: str = None,
    client_secret: str = None,
) -> dict:
    """
    Add a variant to a session's cart.

    Strategy:
      1. Try Storefront API cart (requires SHOPIFY_STOREFRONT_TOKEN in .env).
      2. Fall back to Admin API Draft Orders (requires write_draft_orders scope).
    """
    domain = shop_domain or _DEFAULT_DOMAIN
    storefront_token = _DEFAULT_STOREFRONT_TOKEN
    resolved_admin_token = admin_token or _DEFAULT_ADMIN_TOKEN

    # ── Strategy 1: Storefront API ───────────────────────────────────────
    if storefront_token:
        try:
            variant_gid = variant["id"]  # already in gid://shopify/ProductVariant/XXX form

            existing = _carts.get(session_id)
            if existing and existing.get("cart_id") and str(existing["cart_id"]).startswith("gid://shopify/Cart/"):
                cart = _storefront_cart_add(domain, storefront_token, existing["cart_id"], variant_gid, quantity)
            else:
                cart = _storefront_cart_create(domain, storefront_token, variant_gid, quantity)

            serialised = _serialise_storefront_cart(cart)
            _carts[session_id] = serialised
            print(f"[cart] Storefront cart updated — {serialised['total_quantity']} item(s), checkout: {serialised['checkout_url']}")
            return serialised

        except Exception as exc:
            print(f"[cart] Storefront API failed: {exc} — trying Draft Orders fallback...")

    # ── Strategy 2: Admin Draft Orders fallback ──────────────────────────
    if not resolved_admin_token:
        raise RuntimeError(
            "Cannot add to cart: no SHOPIFY_STOREFRONT_TOKEN or SHOPIFY_ADMIN_TOKEN found in .env.\n"
            "Please add one of these to your .env file:\n"
            "  SHOPIFY_STOREFRONT_TOKEN=shpat_xxx  (recommended, Storefront API)\n"
            "  SHOPIFY_ADMIN_TOKEN=shpat_xxx        (Admin API, needs write_draft_orders scope)"
        )

    variant_numeric_id = _gid_to_id(variant["id"])
    existing = _draft_orders.get(session_id)

    if existing:
        draft = _add_line_to_draft(domain, resolved_admin_token, existing["draft_id"], variant_numeric_id, quantity)
    else:
        draft = _create_draft(domain, resolved_admin_token, variant_numeric_id, quantity)

    serialised = _serialise_draft(draft, product, variant)
    _draft_orders[session_id] = {"draft_id": draft["id"], **serialised}
    print(f"[cart] Draft order updated — {serialised['total_quantity']} item(s), checkout: {serialised['checkout_url']}")
    return serialised


def remove_from_cart_by_variant(
    session_id: str,
    shop_domain: str,
    variant_id: str,
    admin_token: str = None,
    new_quantity: Optional[int] = None,
) -> dict:
    """
    Remove or update a line item from a session's cart matching variant_id (or GID).
    """
    domain = shop_domain or _DEFAULT_DOMAIN
    storefront_token = _DEFAULT_STOREFRONT_TOKEN
    resolved_admin_token = admin_token or _DEFAULT_ADMIN_TOKEN

    variant_str = str(variant_id)
    numeric_id = _gid_to_id(variant_str) if "gid://" in variant_str else int(variant_str)

    # ── Storefront API removal/update ───────────────────────────────────
    existing = _carts.get(session_id)
    if existing and existing.get("cart_id"):
        target_line_id = None
        line_ids_to_remove = []
        for line in existing.get("lines", []):
            line_var_id = str(line.get("variant_id", ""))
            line_num_id = _gid_to_id(line_var_id) if "gid://" in line_var_id else (int(line_var_id) if line_var_id.isdigit() else None)
            if line_num_id == numeric_id or line_var_id == variant_str:
                target_line_id = line.get("line_id")
                line_ids_to_remove.append(target_line_id)

        if storefront_token and str(existing.get("cart_id", "")).startswith("gid://shopify/Cart/"):
            try:
                if new_quantity is not None and new_quantity > 0 and target_line_id:
                    cart = _storefront_cart_update(domain, storefront_token, existing["cart_id"], target_line_id, new_quantity)
                elif line_ids_to_remove:
                    cart = _storefront_cart_remove(domain, storefront_token, existing["cart_id"], line_ids_to_remove)
                else:
                    return existing

                serialised = _serialise_storefront_cart(cart)
                _carts[session_id] = serialised
                return serialised
            except Exception as exc:
                print(f"[cart] Storefront remove/update failed: {exc}")

        # Fallback local filter
        if new_quantity is not None and new_quantity > 0:
            for l in existing.get("lines", []):
                if _gid_to_id(str(l.get("variant_id","0"))) == numeric_id:
                    l["quantity"] = new_quantity
        else:
            existing["lines"] = [
                l for l in existing.get("lines", [])
                if _gid_to_id(str(l.get("variant_id","0"))) != numeric_id
            ]
        existing["total_quantity"] = sum(l.get("quantity", 1) for l in existing["lines"])
        return existing

    # ── Draft Orders removal ───────────────────────────────────────────
    draft_info = _draft_orders.get(session_id)
    if draft_info and draft_info.get("draft_id") and resolved_admin_token:
        draft_id = draft_info["draft_id"]
        get_resp = requests.get(
            _admin_url(domain, f"draft_orders/{draft_id}.json"),
            headers=_admin_headers(resolved_admin_token),
            timeout=15,
        )
        if get_resp.ok:
            existing_lines = get_resp.json()["draft_order"].get("line_items", [])
            clean_lines = [
                {"variant_id": l["variant_id"], "quantity": l["quantity"]}
                for l in existing_lines if l.get("variant_id") != numeric_id
            ]
            put_resp = requests.put(
                _admin_url(domain, f"draft_orders/{draft_id}.json"),
                json={"draft_order": {"line_items": clean_lines}},
                headers=_admin_headers(resolved_admin_token),
                timeout=20,
            )
            if put_resp.ok:
                draft = put_resp.json()["draft_order"]
                lines = draft_info.get("lines", [])
                dummy_prod = {"title": "Product"}
                dummy_var = {"price": "0.00"}
                serialised = _serialise_draft(draft, dummy_prod, dummy_var)
                _draft_orders[session_id] = {"draft_id": draft["id"], **serialised}
                return serialised

    return {"cart_id": "", "total_quantity": 0, "lines": []}


def get_draft_order(session_id: str) -> dict | None:
    """Return the current cart for a session (or None)."""
    if session_id in _carts:
        return _carts[session_id]
    d = _draft_orders.get(session_id)
    return {k: v for k, v in d.items() if k != "draft_id"} if d else None


def clear_draft_order(session_id: str):
    """Remove cart state for a session (e.g. on reset)."""
    _carts.pop(session_id, None)
    _draft_orders.pop(session_id, None)


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


