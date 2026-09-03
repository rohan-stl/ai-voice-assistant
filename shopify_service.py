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

SHOPIFY_API_VERSION = "2026-07"

_DEFAULT_DOMAIN = os.getenv("SHOPIFY_STORE_DOMAIN", "")
_DEFAULT_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
_DEFAULT_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
_DEFAULT_ADMIN_TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "")
_DEFAULT_STOREFRONT_TOKEN = os.getenv("SHOPIFY_STOREFRONT_TOKEN", "")

_PRODUCT_CACHE = {}      # cache_key -> (timestamp, products_list)
_COLLECTION_CACHE = {}   # cache_key -> (timestamp, collections_list)
_COLLECTION_LOCKS = {}   # cache_key -> asyncio.Lock
CACHE_TTL_SECONDS = 300


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
    admin_token: str = None,
    client_id: str = None,
    client_secret: str = None,
    force_refresh: bool = False,
) -> list:
    """Fetch global product catalog from Shopify Admin GraphQL API with caching."""
    domain = shop_domain or _DEFAULT_DOMAIN
    if not domain:
        return []

    cache_key = f"{domain}:{first}"
    now = time.time()

    if not force_refresh and cache_key in _PRODUCT_CACHE:
        ts, cached_products = _PRODUCT_CACHE[cache_key]
        if now - ts < CACHE_TTL_SECONDS:
            return cached_products

    if admin_token:
        token = admin_token
    elif client_id and client_secret:
        try:
            token = _get_admin_token(domain, client_id, client_secret)
        except Exception:
            token = None
    elif _DEFAULT_ADMIN_TOKEN:
        token = _DEFAULT_ADMIN_TOKEN
    else:
        try:
            token = _get_admin_token(domain, _DEFAULT_CLIENT_ID, _DEFAULT_CLIENT_SECRET)
        except Exception:
            token = None

    if not token:
        return []

    url = f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    query = """
    query GetProducts($first: Int!) {
        products(first: $first) {
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

                variants(first: 20) {
                    nodes {
                        id
                        title
                        price
                        availableForSale
                        inventoryQuantity

                        image {
                            url
                            altText
                        }

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

    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }

    try:
        response = requests.post(
            url,
            json={"query": query, "variables": {"first": first}},
            headers=headers,
            timeout=15,
        )
        if not response.ok:
            return []
        data = response.json()
        if "errors" in data or "data" not in data or not data["data"]:
            return []
        raw_products = data["data"]["products"]["nodes"]
        products = [
            p for p in raw_products
            if p.get("status") == "ACTIVE" and "hidden" not in p.get("title", "").lower()
        ]
        _PRODUCT_CACHE[cache_key] = (now, products)
        return products
    except Exception:
        return []


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

        sf_token = storefront_token or _DEFAULT_STOREFRONT_TOKEN
        adm_token = admin_token or _DEFAULT_ADMIN_TOKEN

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

    if not col_products:
        col_products = global_prods

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