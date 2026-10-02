"""
shopify_auth.py

Shopify App Proxy HMAC SHA-256 signature verification and shop domain validation.
"""

import hmac
import hashlib
import re

import os
from dataclasses import dataclass
from typing import Optional
from store_credentials_db import get_store_credentials

SHOP_DOMAIN_REGEX = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$", re.IGNORECASE)


@dataclass
class ShopContext:
    shop_domain: str
    storefront_token: Optional[str] = None
    admin_token: Optional[str] = None


class InvalidShopDomainError(Exception):
    pass


class InvalidProxySignatureError(Exception):
    pass


class StorefrontConfigurationMissing(Exception):
    pass


def validate_shop_domain(domain: str) -> bool:
    """Returns True if domain strictly matches standard myshopify.com handle format."""
    if not domain:
        return False
    return bool(SHOP_DOMAIN_REGEX.match(domain.strip().lower()))


def verify_app_proxy_signature(query_params: dict, client_secret: str) -> bool:
    """
    Verifies HMAC SHA-256 signature passed by Shopify App Proxy requests.
    Shopify builds signature by concatenating query parameters (excluding 'signature')
    sorted lexicographically by key.
    """
    signature = query_params.get("signature")
    if not signature or not client_secret:
        return False

    filtered_params = {k: v for k, v in query_params.items() if k != "signature"}

    formatted_params = []
    for k in sorted(filtered_params.keys()):
        val = filtered_params[k]
        if isinstance(val, list):
            val_str = ",".join(str(x) for x in val)
        else:
            val_str = str(val)
        formatted_params.append(f"{k}={val_str}")

    message = "".join(formatted_params)
    calculated_digest = hmac.new(
        client_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(calculated_digest, signature)


def resolve_shop_context(raw_domain: Optional[str], query_params: Optional[dict] = None) -> ShopContext:
    """
    Resolves a verified ShopContext object containing normalized shop domain and store tokens.
    Verifies domain format, App Proxy signature (if present), and loads credentials from DB.
    """
    params = query_params or {}
    domain_candidate = params.get("shop") or params.get("shop_domain") or raw_domain or os.getenv("SHOPIFY_STORE_DOMAIN", "")
    normalized_domain = (domain_candidate or "").strip().lower()

    if not validate_shop_domain(normalized_domain):
        raise InvalidShopDomainError(f"Invalid Shopify store domain format '{normalized_domain}'.")

    if "signature" in params:
        client_secret = os.getenv("SHOPIFY_CLIENT_SECRET", "")
        if not verify_app_proxy_signature(params, client_secret):
            raise InvalidProxySignatureError(f"App Proxy signature verification failed for shop '{normalized_domain}'.")

    creds = get_store_credentials(normalized_domain)
    allow_fallback = os.getenv("ALLOW_SINGLE_STORE_ENV_FALLBACK", "true").lower() in ("true", "1", "yes")
    default_domain = os.getenv("SHOPIFY_STORE_DOMAIN", "").strip().lower()

    sf_token = creds.get("storefront_token") if creds else None
    adm_token = creds.get("admin_token") if creds else None

    if not sf_token and not adm_token:
        # Only fallback if domain matches default domain (or no default domain set)
        if allow_fallback and (not default_domain or normalized_domain == default_domain):
            sf_token = os.getenv("SHOPIFY_STOREFRONT_TOKEN") or None
            adm_token = os.getenv("SHOPIFY_ADMIN_TOKEN") or None
        elif not os.getenv("SHOPIFY_CLIENT_ID") or not os.getenv("SHOPIFY_CLIENT_SECRET"):
            raise StorefrontConfigurationMissing(f"Store credentials missing for shop '{normalized_domain}'.")

    return ShopContext(
        shop_domain=normalized_domain,
        storefront_token=sf_token,
        admin_token=adm_token,
    )