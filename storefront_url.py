from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
import re

from product_filter import normalize_filters, align_filters_for_url, _as_list


SHOPIFY_STORE_URL = "https://bhavin-ai-shopping-assistant-demo.myshopify.com"


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _tokenize(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value).lower())


def _find_best_match(items: list, query: str, fields: tuple[str, ...]) -> dict | None:
    if not query:
        return None

    normalized_query = _normalize(query)

    if not normalized_query:
        return None

    for item in items:
        for field in fields:
            value = item.get(field)
            if value and _normalize(value) == normalized_query:
                return item

    for item in items:
        for field in fields:
            value = item.get(field)
            if value and normalized_query in _normalize(value):
                return item

    return None


def _score_text(text: str, query_tokens: list[str]) -> int:
    tokens = set(_tokenize(text))
    return sum(1 for token in query_tokens if token in tokens)


def _product_type_hint_score(query: str, product: dict) -> int:
    query_norm = _normalize(query)
    query_tokens = _tokenize(query)

    if not query_tokens:
        return 0

    product_text = " ".join([
        product.get("title", ""),
        product.get("handle", ""),
        product.get("productType", "")
    ])
    product_norm = _normalize(product_text)

    score = 0

    # Strong preference for explicit "T-shirt" queries matching T-shirt products
    if "tshirt" in query_norm or ("t" in query_tokens and "shirt" in query_tokens):
        if "tshirt" in product_norm:
            score += 80
        elif "shirt" in product_norm:
            score += 20

    # Generic shirt requests still prefer shirt-like products, but less strongly.
    elif "shirt" in query_tokens:
        if "shirt" in product_norm:
            score += 35

    if "tee" in query_tokens and ("tee" in product_norm or "tshirt" in product_norm):
        score += 50

    return score


def _product_search_score(product: dict, query: str) -> int:
    query_norm = _normalize(query)
    query_tokens = _tokenize(query)

    if not query_tokens:
        return 0

    score = 0

    title = product.get("title", "")
    handle = product.get("handle", "")
    product_type = product.get("productType", "")
    vendor = product.get("vendor", "")
    description = product.get("description", "")
    variants = product.get("variants", {}).get("nodes", [])

    if query_norm == _normalize(title):
        score += 100

    if query_norm == _normalize(handle):
        score += 95

    if query_norm in _normalize(title):
        score += 60

    if query_norm in _normalize(handle):
        score += 55

    score += _product_type_hint_score(query, product)
    score += _score_text(title, query_tokens) * 18
    score += _score_text(handle, query_tokens) * 12
    score += _score_text(product_type, query_tokens) * 20
    score += _score_text(vendor, query_tokens) * 2
    score += _score_text(description, query_tokens) * 1

    for variant in variants:
        variant_title = variant.get("title", "")
        variant_color, variant_size = _extract_variant_parts(variant_title)

        score += _score_text(variant_title, query_tokens) * 6

        if variant_color:
            score += _score_text(variant_color, query_tokens) * 8

        if variant_size:
            score += _score_text(variant_size, query_tokens) * 3

        if variant_color and variant_size:
            score += _score_text(f"{variant_color} {variant_size}", query_tokens) * 1

    return score


def _extract_variant_parts(variant_title: str) -> tuple[str | None, str | None]:
    parts = [
        part.strip()
        for part in str(variant_title or "").split("/")
    ]

    color = parts[0] if len(parts) >= 1 and parts[0] else None
    size = parts[1] if len(parts) >= 2 and parts[1] else None

    return color, size


def _build_product_variant_url(base_url: str, variant_id: str | None) -> str:
    if not base_url:
        return base_url

    normalized_variant_id = _normalize_variant_id(variant_id)

    if not normalized_variant_id:
        return base_url

    parts = urlsplit(base_url)
    query_params = dict(parse_qsl(parts.query, keep_blank_values=True))
    query_params["variant"] = normalized_variant_id

    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query_params),
        parts.fragment,
    ))


def _normalize_variant_id(variant_id: str | None) -> str | None:
    if not variant_id:
        return None

    variant_id = str(variant_id).strip()

    if variant_id.isdigit():
        return variant_id

    match = re.search(r"(\d+)$", variant_id)
    if match:
        return match.group(1)

    return None


def _variant_search_score(variant: dict, query: str) -> int:
    query_norm = _normalize(query)
    query_tokens = _tokenize(query)

    if not query_tokens:
        return 0

    score = 0
    title = variant.get("title", "")
    variant_color, variant_size = _extract_variant_parts(title)

    if query_norm == _normalize(title):
        score += 100

    score += _score_text(title, query_tokens) * 12

    if variant_color:
        variant_color_norm = _normalize(variant_color)
        if query_norm and query_norm.find(variant_color_norm) != -1:
            score += 90
        score += _score_text(variant_color, query_tokens) * 20

    if variant_size:
        score += _score_text(variant_size, query_tokens) * 5

    if "tshirt" in query_norm or ("t" in query_tokens and "shirt" in query_tokens):
        score += 5

    return score


def build_storefront_url(filters: dict) -> str | None:
    """Build Shopify collection/search URL from filters (mustali_dev semantics)."""
    params = {}

    product_names = _as_list(filters.get("product_name"))
    product_types = _as_list(filters.get("product_type"))
    colors = _as_list(filters.get("color"))
    sizes = _as_list(filters.get("size"))
    vendors = _as_list(filters.get("vendor"))
    max_price = filters.get("max_price")
    min_price = filters.get("min_price")

    use_search_endpoint = bool(product_names)

    if product_names:
        params["q"] = " ".join(product_names)
        params["type"] = "product"

    if product_types:
        params["filter.p.product_type"] = product_types

    if colors:
        params["filter.v.option.color"] = [c.title() for c in colors]

    if sizes:
        params["filter.v.option.size"] = [s.upper() for s in sizes]

    if vendors:
        params["filter.p.vendor"] = vendors

    if min_price is not None:
        params["filter.v.price.gte"] = min_price
    elif max_price is not None:
        params["filter.v.price.gte"] = 0

    if max_price is not None:
        params["filter.v.price.lte"] = max_price

    if not params:
        return None

    params["sort_by"] = "title-ascending"
    base_path = "/search" if use_search_endpoint else "/collections/all"

    return (
        f"{SHOPIFY_STORE_URL}{base_path}?"
        f"{urlencode(params, doseq=True)}"
    )


def resolve_product_url(products: list, query: str) -> str | None:
    match = find_product_match(products, query)

    if not match:
        return None

    variants = match.get("variants", {}).get("nodes", [])
    matched_variant = find_variant_match(variants, query)
    base_url = (
        match.get("onlineStoreUrl")
        or f"{SHOPIFY_STORE_URL}/products/{match.get('handle')}"
    )

    return _build_product_variant_url(base_url, matched_variant.get("id") if matched_variant else None)


def find_product_match(products: list, query: str) -> dict | None:
    if not query:
        return None

    exact_match = _find_best_match(products, query, ("handle", "title"))
    if exact_match:
        return exact_match

    best_product = None
    best_score = 0

    for product in products:
        score = _product_search_score(product, query)
        if score > best_score:
            best_score = score
            best_product = product

    return best_product if best_score > 0 else None


def find_variant_match(variants: list, query: str) -> dict | None:
    if not query or not variants:
        return None

    exact_match = _find_best_match(variants, query, ("title",))
    if exact_match:
        return exact_match

    best_variant = None
    best_score = 0

    for variant in variants:
        score = _variant_search_score(variant, query)
        if score > best_score:
            best_score = score
            best_variant = variant

    return best_variant if best_score > 0 else None


def resolve_collection_url(collections: list, query: str) -> str | None:
    match = _find_best_match(collections, query, ("handle", "title"))

    if not match:
        return None

    return (
        match.get("onlineStoreUrl")
        or f"{SHOPIFY_STORE_URL}/collections/{match.get('handle')}"
    )


def find_collection_match(collections: list, query: str) -> dict | None:
    return _find_best_match(collections, query, ("handle", "title"))


def build_destination_url(
    result: dict,
    products: list,
    collections: list,
    filtered_products: list | None = None
) -> str:
    navigation_target = result.get("navigation_target", {}) or {}
    target_type = navigation_target.get("type")
    target_query = (
        navigation_target.get("query")
        or navigation_target.get("name")
        or ""
    )

    filters = result.get("filters", {}) or {}

    if not target_type:
        if result.get("intent") == "product_request":
            target_type = "product"
        elif result.get("intent") == "collection_request":
            target_type = "collection"

    if target_type == "product":
        product_url = resolve_product_url(products, target_query)
        if product_url:
            return product_url
        return None

    if target_type == "collection":
        if not target_query:
            target_query = filters.get("product_type") or ""

        collection_url = resolve_collection_url(collections, target_query)
        if collection_url:
            return collection_url
        return None

    if result.get("intent") == "filter_products":
        matched = filtered_products or products
        url_filters = align_filters_for_url(filters, matched)
        normalized = normalize_filters(url_filters, products)
        return build_storefront_url(normalized)

    return None
