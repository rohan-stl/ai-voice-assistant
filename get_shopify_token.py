#!/usr/bin/env python3
"""
get_shopify_token.py

Exchanges the app's client_id + client_secret for a store access token
using the Shopify OAuth token exchange (online token for dev store).

Run once: python get_shopify_token.py
Then copy the printed tokens into your .env file.
"""

import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.getenv("SHOPIFY_CLIENT_ID", "64250f16378df9cbc2ac2fe547c838f7")
CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "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")
STORE_DOMAIN  = os.getenv("SHOPIFY_STORE_DOMAIN", "bhavin-ai-shopping-assistant-demo.myshopify.com")

def get_admin_token_via_exchange():
    """
    For dev stores with a Partners app, use the client credentials
    to get an offline (permanent) admin access token.
    """
    url = f"https://{STORE_DOMAIN}/admin/oauth/access_token"
    
    # Try with client credentials
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
    }
    
    print(f"Requesting Admin token from: {url}")
    resp = requests.post(url, json=payload, timeout=15)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text[:500]}")
    
    if resp.ok:
        token = resp.json().get("access_token")
        print(f"\n✅ SHOPIFY_ADMIN_TOKEN={token}")
        return token
    else:
        print("\n❌ client_credentials flow not supported — trying another approach...")
        return None


def get_storefront_token_via_admin(admin_token: str):
    """
    Once we have an admin token, use it to create/list storefront tokens.
    """
    url = f"https://{STORE_DOMAIN}/admin/api/2026-07/storefront_access_tokens.json"
    
    # List existing tokens first
    resp = requests.get(
        url,
        headers={
            "X-Shopify-Access-Token": admin_token,
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    
    if resp.ok:
        tokens = resp.json().get("storefront_access_tokens", [])
        if tokens:
            token = tokens[0].get("access_token")
            print(f"\n✅ Existing SHOPIFY_STOREFRONT_TOKEN={token}")
            return token
    
    # Create a new one
    resp = requests.post(
        url,
        json={"storefront_access_token": {"title": "AI Shopping Assistant"}},
        headers={
            "X-Shopify-Access-Token": admin_token,
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    
    if resp.ok:
        token = resp.json().get("storefront_access_token", {}).get("access_token")
        print(f"\n✅ New SHOPIFY_STOREFRONT_TOKEN={token}")
        return token
    else:
        print(f"❌ Could not get storefront token: {resp.status_code} {resp.text[:200]}")
        return None


if __name__ == "__main__":
    print("=" * 60)
    print("Shopify Token Fetcher")
    print("=" * 60)
    
    admin_token = get_admin_token_via_exchange()
    
    if admin_token:
        storefront_token = get_storefront_token_via_admin(admin_token)
        
        print("\n" + "=" * 60)
        print("Copy these into your .env file:")
        print("=" * 60)
        print(f"SHOPIFY_ADMIN_TOKEN={admin_token}")
        if storefront_token:
            print(f"SHOPIFY_STOREFRONT_TOKEN={storefront_token}")
    else:
        print("\n" + "=" * 60)
        print("The client_credentials OAuth grant is NOT supported for Partners apps.")
        print("\nYou need to get the token via the Shopify store admin:")
        print(f"\n1. Go to: https://{STORE_DOMAIN}/admin/settings/apps/development")
        print("2. Allow legacy custom app development")
        print("3. Create an app → set scopes → Install → copy token")
        print("=" * 60)
