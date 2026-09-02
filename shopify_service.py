"""
shopify_service.py

Fetches product catalog from the Shopify Admin GraphQL API.
Accepts shop_domain and admin_token per call so the same backend
can serve multiple Shopify stores without code changes.

Falls back to .env values when called without explicit credentials
(useful for local development / single-store deployments).
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

SHOPIFY_API_VERSION = "2026-07"

_DEFAULT_DOMAIN = os.getenv("SHOPIFY_STORE_DOMAIN", "")
_DEFAULT_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
_DEFAULT_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
_DEFAULT_ADMIN_TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "")


def _get_admin_token(shop_domain: str, client_id: str, client_secret: str) -> str:
    """
    Exchange client credentials for a short-lived Admin API access token.
    This is the existing OAuth client_credentials flow.
    """
    url = f"https://{shop_domain}/admin/oauth/access_token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    return response.json()["access_token"]


import time

_PRODUCT_CACHE = {}  # cache_key -> (timestamp, products_list)
CACHE_TTL_SECONDS = 60


def get_products(
    first: int = 50,
    shop_domain: str = None,
    admin_token: str = None,
    client_id: str = None,
    client_secret: str = None,
    force_refresh: bool = False,
) -> list:
    """
    Fetch the product catalog from the Shopify Admin GraphQL API with 60s caching.
    """
    domain = shop_domain or _DEFAULT_DOMAIN
    if not domain:
        raise ValueError("shop_domain is required")

    cache_key = f"{domain}:{first}"
    now = time.time()

    if not force_refresh and cache_key in _PRODUCT_CACHE:
        ts, cached_products = _PRODUCT_CACHE[cache_key]
        if now - ts < CACHE_TTL_SECONDS:
            return cached_products

    # Resolve the admin token
    if admin_token:
        token = admin_token
    elif client_id and client_secret:
        token = _get_admin_token(domain, client_id, client_secret)
    elif _DEFAULT_ADMIN_TOKEN:
        token = _DEFAULT_ADMIN_TOKEN
    else:
        # Fall back to env-based OAuth flow (existing behaviour)
        token = _get_admin_token(domain, _DEFAULT_CLIENT_ID, _DEFAULT_CLIENT_SECRET)

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

    response = requests.post(
        url,
        json={"query": query, "variables": {"first": first}},
        headers=headers,
        timeout=20,
    )
    data = response.json()

    if "errors" in data:
        raise Exception(data["errors"])

    raw_products = data["data"]["products"]["nodes"]
    # Filter out DRAFT, ARCHIVED, and hidden products so catalog matches Shopify Storefront channel
    products = [
        p for p in raw_products
        if p.get("status") == "ACTIVE" and "hidden" not in p.get("title", "").lower()
    ]
    _PRODUCT_CACHE[cache_key] = (now, products)
    return products


if __name__ == "__main__":
    products = get_products()

    print(f"\nFound {len(products)} products:\n")

    for product in products:
        print(f"- {product['title']}")
        print(f"  Type: {product['productType']}")
        print(f"  Vendor: {product['vendor']}")
        print(f"  Status: {product['status']}")
        print(f"  Inventory: {product['totalInventory']}")
        print(f"  URL: {product.get('onlineStoreUrl')}")

        for variant in product["variants"]["nodes"]:
            print(f"  Variant: {variant['title']}")
            print(f"    Price: ${variant['price']}")
            print(f"    Available: {variant['availableForSale']}")
            print(f"    Inventory: {variant['inventoryQuantity']}")

        print()