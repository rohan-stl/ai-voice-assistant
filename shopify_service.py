"""
shopify_service.py

Fetches product catalog and published collection metadata using the Shopify APIs.
Prefers Shopify Storefront Access Tokens for shopper-facing collection retrieval and falls back safely to Admin API when Storefront tokens are not supplied.
"""

import os
import re
import time
import asyncio
import requests
import difflib
from typing import Optional, Tuple
from dotenv import load_dotenv

load_dotenv()

from shopify_auth import ShopContext, get_store_credentials

SHOPIFY_API_VERSION = "2026-07"

def _get_default_domain() -> str:
    return os.getenv("SHOPIFY_STORE_DOMAIN", "")

def _get_default_admin_token() -> str:
    return os.getenv("SHOPIFY_ADMIN_TOKEN", "")

def _get_default_storefront_token() -> str:
    return os.getenv("SHOPIFY_STOREFRONT_TOKEN", "")
_ALLOW_ENV_FALLBACK = os.getenv("ALLOW_SINGLE_STORE_ENV_FALLBACK", "true").lower() in ("true", "1", "yes")

_PRODUCT_CACHE = {}      # cache_key -> (timestamp, products_list)
_COLLECTION_CACHE = {}   # cache_key -> (timestamp, collections_list)
_COLLECTION_LOCKS = {}   # cache_key -> asyncio.Lock
CACHE_TTL_SECONDS = 300


class ShopifyAuthenticationError(Exception):
    pass


class ShopifyUpstreamError(Exception):
    pass


def extract_variant_nodes(connection: dict) -> list:
    """Extract raw variant objects from GraphQL connection object supporting both 'nodes' and 'edges'."""
    if not isinstance(connection, dict):
        return []
    if isinstance(connection.get("nodes"), list):
        return [node for node in connection["nodes"] if isinstance(node, dict)]

    nodes = []
    for edge in connection.get("edges", []):
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
            nodes.append(edge["node"])
    return nodes


def normalize_shopify_variant(node: dict) -> dict:
    """
    Normalizes a single raw Shopify variant node.
    Preserves None as unknown availability (True = available, False = unavailable, None = unknown).
    """
    if not isinstance(node, dict):
        return {
            "id": None,
            "title": None,
            "price": None,
            "sku": None,
            "available_for_sale": None,
            "quantity_available": None,
            "selected_options": []
        }

    raw_available = node.get("availableForSale")
    if raw_available is None:
        raw_available = node.get("available_for_sale")

    available_for_sale = (
        raw_available
        if isinstance(raw_available, bool)
        else None
    )

    qty = node.get("quantityAvailable")
    if qty is None:
        qty = node.get("inventoryQuantity")

    return {
        "id": node.get("id"),
        "title": node.get("title"),
        "price": node.get("price"),
        "sku": node.get("sku"),
        "available_for_sale": available_for_sale,
        "quantity_available": qty,
        "selected_options": [
            {
                "name": option.get("name"),
                "value": option.get("value")
            }
            for option in node.get("selectedOptions", [])
            if isinstance(option, dict) and option.get("name") and option.get("value")
        ]
    }


def determine_product_availability(
    variants: list,
    variants_complete: bool = True
) -> str:
    """
    Determines product availability status from normalized variants list.
    Returns: 'available' | 'sold_out' | 'unknown'
    """
    if not variants_complete:
        return "unknown"
    if not variants:
        return "unknown"

    if any(v.get("available_for_sale") is True for v in variants):
        return "available"

    if all(v.get("available_for_sale") is False for v in variants):
        return "sold_out"

    return "unknown"



def _resolve_shop_credentials(domain: str) -> tuple[Optional[str], Optional[str]]:
    """Resolves (storefront_token, admin_token) for a specific shop domain from DB."""
    creds = get_store_credentials(domain)
    if creds:
        return creds.get("storefront_token"), creds.get("admin_token")
    if _ALLOW_ENV_FALLBACK:
        return _get_default_storefront_token() or None, _get_default_admin_token() or None
    return None, None


def _get_admin_token(shop_domain: str, client_id: str, client_secret: str) -> str:
    """Exchange client credentials for a short-lived Admin API access token."""
    url = f"https://{shop_domain}/admin/oauth/access_token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    return response.json()["access_token"]


def get_products(
    first: int = 50,
    shop_domain: str = None,
    shop_context: Optional[ShopContext] = None,
    admin_token: str = None,
    client_id: str = None,
    client_secret: str = None,
    force_refresh: bool = False,
) -> list:
    """Fetch global product catalog from Shopify Admin GraphQL API with caching."""
    if shop_context:
        domain = shop_context.shop_domain
        token = shop_context.admin_token
    else:
        domain = shop_domain or _DEFAULT_DOMAIN
        if not domain:
            return []
        db_sf, db_adm = _resolve_shop_credentials(domain)
        token = admin_token or db_adm

    cid = client_id or os.getenv("SHOPIFY_CLIENT_ID", "64250f16378df9cbc2ac2fe547c838f7")
    csec = client_secret or os.getenv("SHOPIFY_CLIENT_SECRET", "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")

    if not token and cid and csec:
        try:
            token = _get_admin_token(domain, cid, csec)
            if token:
                from store_credentials_db import set_store_credentials
                set_store_credentials(domain, admin_token=token)
                print(f"[shopify] Successfully exchanged OAuth client credentials for store '{domain}'")
        except Exception as exc:
            print(f"[shopify] OAuth token exchange failed for store '{domain}': {exc}")
            token = None

    if not token:
        raise ShopifyAuthenticationError(f"No Admin API access token available for store '{domain}'.")

    cache_key = f"{domain}:{first}"
    now = time.time()

    if not force_refresh and cache_key in _PRODUCT_CACHE:
        ts, cached_products = _PRODUCT_CACHE[cache_key]
        if now - ts < CACHE_TTL_SECONDS:
            return cached_products

    url = f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }
    query = """
    query GetProducts($first: Int!) {
        products(first: $first, query: "status:ACTIVE") {
            nodes {
                id
                title
                handle
                description
                productType
                vendor
                status
                totalInventory
                onlineStoreUrl
                featuredImage {
                    url
                    altText
                }
                options {
                    id
                    name
                    values
                }
                collections(first: 10) {
                    nodes {
                        id
                        handle
                        title
                    }
                }
                variants(first: 100) {
                    pageInfo {
                        hasNextPage
                        endCursor
                    }
                    nodes {
                        id
                        title
                        price
                        sku
                        availableForSale
                        inventoryQuantity
                        selectedOptions {
                            name
                            value
                        }
                    }
                }
            }
        }
    }
    """
    try:
        res = requests.post(url, json={"query": query, "variables": {"first": first}}, headers=headers, timeout=15)
    except Exception as exc:
        raise ShopifyUpstreamError(f"Network error querying Shopify Admin API for shop '{domain}': {exc}")

    if res.status_code in (401, 403):
        # Attempt automatic re-exchange of client credentials for this shop
        if cid and csec:
            try:
                new_token = _get_admin_token(domain, cid, csec)
                if new_token and new_token != token:
                    from store_credentials_db import set_store_credentials
                    set_store_credentials(domain, admin_token=new_token)
                    print(f"[shopify] Re-exchanged OAuth client credentials for '{domain}' after {res.status_code}")
                    return get_products(
                        first=first,
                        shop_domain=domain,
                        admin_token=new_token,
                        force_refresh=True,
                    )
            except Exception:
                pass
        raise ShopifyAuthenticationError(f"Shopify Admin API returned HTTP {res.status_code} for shop '{domain}'.")

    if not res.ok:
        raise ShopifyUpstreamError(f"Shopify Admin API returned HTTP {res.status_code} for shop '{domain}'.")

    payload = res.json()
    if "errors" in payload:
        err_msg = str(payload["errors"])
        if "access token" in err_msg.lower() or "unauthorized" in err_msg.lower():
            raise ShopifyAuthenticationError(f"Shopify Admin API auth error for '{domain}': {err_msg}")
        raise ShopifyUpstreamError(f"Shopify Admin API GraphQL error for '{domain}': {err_msg}")

    nodes = payload.get("data", {}).get("products", {}).get("nodes", [])
    products = []
    for node in nodes:
        variants_nodes = node.get("variants", {}).get("nodes", [])
        # Embed collection membership so filter_products can resolve collections
        # without additional API calls at query time.
        collection_nodes = node.get("collections", {}).get("nodes", []) if isinstance(node.get("collections"), dict) else []
        product_collections = [
            {"id": c.get("id"), "handle": c.get("handle", ""), "title": c.get("title", "")}
            for c in collection_nodes
            if isinstance(c, dict) and c.get("handle")
        ]
        products.append({
            "id": node.get("id"),
            "title": node.get("title"),
            "handle": node.get("handle"),
            "description": node.get("description"),
            "productType": node.get("productType"),
            "vendor": node.get("vendor"),
            "status": node.get("status"),
            "totalInventory": node.get("totalInventory"),
            "onlineStoreUrl": node.get("onlineStoreUrl"),
            "featuredImage": node.get("featuredImage"),
            "options": node.get("options", []),
            "collections": product_collections,
            "variants": {"nodes": variants_nodes},
        })

    _PRODUCT_CACHE[cache_key] = (now, products)
    return products


async def get_collections(
    shop_domain: str = None,
    storefront_token: str = None,
    admin_token: str = None,
    force_refresh: bool = False,
) -> list:
    domain = shop_domain or _DEFAULT_DOMAIN
    if not domain:
        return []

    cache_key = f"{domain}:{SHOPIFY_API_VERSION}"
    now = time.time()

    if not force_refresh and cache_key in _COLLECTION_CACHE:
        ts, cached = _COLLECTION_CACHE[cache_key]
        if now - ts < CACHE_TTL_SECONDS and cached:
            return cached

    if cache_key not in _COLLECTION_LOCKS:
        _COLLECTION_LOCKS[cache_key] = asyncio.Lock()

    lock = _COLLECTION_LOCKS[cache_key]
    async with lock:
        if not force_refresh and cache_key in _COLLECTION_CACHE:
            ts, cached = _COLLECTION_CACHE[cache_key]
            if now - ts < CACHE_TTL_SECONDS and cached:
                return cached

        collections = []
        has_next_page = True
        cursor = None

        db_sf, db_adm = _resolve_shop_credentials(domain)
        sf_token = storefront_token or db_sf
        adm_token = admin_token or db_adm

        while has_next_page:
            data = None
            if sf_token:
                url = f"https://{domain}/api/{SHOPIFY_API_VERSION}/graphql.json"
                headers = {
                    "Content-Type": "application/json",
                    "X-Shopify-Storefront-Access-Token": sf_token,
                }
                query = """
                query GetStorefrontCollections($first: Int!, $after: String) {
                    collections(first: $first, after: $after) {
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                        nodes {
                            id
                            title
                            handle
                            description
                            onlineStoreUrl
                        }
                    }
                }
                """
                try:
                    res = requests.post(url, json={"query": query, "variables": {"first": 50, "after": cursor}}, headers=headers, timeout=15)
                    if res.ok:
                        data = res.json().get("data", {}).get("collections")
                except Exception:
                    data = None

            if not data and adm_token:
                url = f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
                headers = {
                    "Content-Type": "application/json",
                    "X-Shopify-Access-Token": adm_token,
                }
                query = """
                query GetAdminCollections($first: Int!, $after: String) {
                    collections(first: $first, after: $after) {
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                        nodes {
                            id
                            title
                            handle
                            description
                        }
                    }
                }
                """
                try:
                    res = requests.post(url, json={"query": query, "variables": {"first": 50, "after": cursor}}, headers=headers, timeout=15)
                    if res.ok:
                        data = res.json().get("data", {}).get("collections")
                except Exception:
                    data = None

            if not data or "nodes" not in data:
                break

            nodes = data.get("nodes", [])
            page_info = data.get("pageInfo", {})
            has_next_page = page_info.get("hasNextPage", False)
            cursor = page_info.get("endCursor")

            for c in nodes:
                handle = c.get("handle", "")
                url_val = c.get("onlineStoreUrl") or (f"/collections/{handle}" if handle else None)
                if not url_val:
                    continue

                collections.append({
                    "id": c.get("id"),
                    "title": c.get("title"),
                    "handle": handle,
                    "description": c.get("description", ""),
                    "onlineStoreUrl": url_val,
                })

        if collections:
            _COLLECTION_CACHE[cache_key] = (now, collections)
        return collections


async def get_collection_products(
    shop_domain: str = None,
    storefront_token: str = None,
    admin_token: str = None,
    collection_handle: str = None,
    filters: dict = None,
    cursor: Optional[str] = None,
    first: int = 50,
) -> dict:
    domain = shop_domain or _DEFAULT_DOMAIN
    if not domain or not collection_handle:
        return {"collection": None, "products": [], "cursor": None, "has_next_page": False, "total_matches": 0}

    all_cols = await get_collections(domain, storefront_token, admin_token)
    matched_col = next((c for c in all_cols if c.get("handle") == collection_handle), None)

    try:
        global_prods = get_products(first=100, shop_domain=domain, admin_token=admin_token)
    except Exception:
        global_prods = []

    col_handle_clean = collection_handle.lower().strip()
    norm_col_title = (matched_col.get("title") if matched_col else col_handle_clean).lower().strip()

    # ── Strategy 1: Membership index lookup (preferred) ──────────────────
    # Products fetched by get_products() embed a `collections` list since
    # the GraphQL query was updated to include it. Use an exact handle match.
    col_products_by_membership = [
        p for p in global_prods
        if any(
            c.get("handle", "").lower() == col_handle_clean
            for c in p.get("collections", [])
        )
    ]

    # ── Strategy 2: Heuristic fallback (backward compat / old cache entries) ─
    # Only used when no products have the `collections` field populated
    # (e.g. cached before this change was deployed).
    has_membership_data = any("collections" in p for p in global_prods)
    if col_products_by_membership:
        col_products = col_products_by_membership
    elif not has_membership_data:
        # Old-format cache: fall back to title/type/handle substring heuristic
        col_products = []
        for p in global_prods:
            p_type = p.get("productType", "").lower().strip()
            p_title = p.get("title", "").lower().strip()
            p_handle = p.get("handle", "").lower().strip()
            if (col_handle_clean in p_handle or
                col_handle_clean.replace("-", " ") in p_title or
                norm_col_title in p_type or
                norm_col_title in p_title or
                p_type in norm_col_title):
                col_products.append(p)
    else:
        # Membership data is present but collection has zero products — return empty.
        # Do NOT fall back to all products; that silently defeats the filter.
        col_products = []

    seen_ids = set()
    deduped = []
    for p in col_products:
        pid = p.get("id")
        if pid not in seen_ids:
            seen_ids.add(pid)
            deduped.append(p)

    col_products = deduped

    start_idx = 0
    if cursor and cursor.startswith("cursor:"):
        try:
            start_idx = int(cursor.replace("cursor:", ""))
        except ValueError:
            start_idx = 0

    page_products = col_products[start_idx : start_idx + first]
    next_idx = start_idx + len(page_products)
    has_next = next_idx < len(col_products)
    next_cursor = f"cursor:{next_idx}" if has_next else None

    col_info = {
        "id": matched_col.get("id") if matched_col else f"gid://shopify/Collection/{collection_handle}",
        "title": matched_col.get("title") if matched_col else collection_handle.replace("-", " ").title(),
        "handle": collection_handle,
        "url": f"/collections/{collection_handle}",
    }

    return {
        "collection": col_info,
        "products": page_products,
        "cursor": next_cursor,
        "has_next_page": has_next,
        "total_matches": len(col_products),
    }


def match_collection(query_text: str, collections: list) -> Tuple[str, Optional[dict], list]:
    """
    Matches user query text against store collections with multi-level confidence scoring.
    Returns:
      (match_type, matched_collection, candidate_list)
      match_type: "exact" | "strong" | "ambiguous" | "none"
    """
    if not query_text or not collections:
        return "none", None, []

    # Clean and normalize query text
    clean_q = query_text.lower().replace("'", "").replace('"', "").replace(",", " ").replace(".", " ").replace("-", " ")
    clean_q = re.sub(r"\b(show|find|products|items|from|in|the|collection|category|view|open|take me to|go to|see)\b", "", clean_q)
    clean_q = re.sub(r"\s+", " ", clean_q).strip()

    if not clean_q:
        return "none", None, []

    # 1. Exact Match on title or handle
    for c in collections:
        title_clean = c.get("title", "").lower().replace("-", " ").strip()
        handle_clean = c.get("handle", "").lower().replace("-", " ").strip()
        if clean_q == title_clean or clean_q == handle_clean:
            return "exact", c, []

    # 2. Singular/Plural Exact Adjustments
    for c in collections:
        title_clean = c.get("title", "").lower().replace("-", " ").strip()
        handle_clean = c.get("handle", "").lower().replace("-", " ").strip()
        if clean_q + "s" == title_clean or clean_q + "s" == handle_clean:
            return "exact", c, []
        if title_clean.rstrip("s") == clean_q or handle_clean.rstrip("s") == clean_q:
            return "exact", c, []

    # 3. Fuzzy & Word-Overlap Confidence Scoring
    candidate_scores = []
    q_words = set(clean_q.split())

    for c in collections:
        title_clean = c.get("title", "").lower().replace("'", "").replace("-", " ").strip()
        handle_clean = c.get("handle", "").lower().replace("-", " ").strip()

        t_words = set(title_clean.split()).union(set(handle_clean.split()))
        overlap = q_words.intersection(t_words)

        # Character similarity using SequenceMatcher
        sim_title = difflib.SequenceMatcher(None, clean_q, title_clean).ratio()
        sim_handle = difflib.SequenceMatcher(None, clean_q, handle_clean).ratio()
        max_sim = max(sim_title, sim_handle)

        score = len(overlap) * 2.0 + max_sim
        if len(overlap) > 0 or max_sim > 0.75:
            candidate_scores.append((score, c, max_sim, len(overlap)))

    if not candidate_scores:
        return "none", None, []

    candidate_scores.sort(key=lambda x: x[0], reverse=True)

    top_score, top_col, top_sim, top_overlap = candidate_scores[0]

    # Strong match condition
    if top_sim >= 0.82 or (top_overlap >= 1 and len(candidate_scores) == 1):
        return "strong", top_col, []

    # Check for ambiguity among close candidates
    close_candidates = [item[1] for item in candidate_scores if item[0] >= top_score - 0.5]
    if len(close_candidates) > 1:
        return "ambiguous", None, close_candidates[:4]
    elif top_score >= 1.5:
        return "strong", top_col, []

    return "none", None, []