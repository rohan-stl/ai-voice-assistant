from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
import re


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


def canonicalize_filters(filters: dict, products: list) -> dict:
    canonical = dict(filters or {})

    if not products:
        return canonical

    product_type = canonical.get("product_type")
    vendor = canonical.get("vendor")
    color = canonical.get("color")
    size = canonical.get("size")

    matched_product = None

    for product in products:
        actual_product_type = product.get("productType", "")
        actual_vendor = product.get("vendor", "")

        if product_type and not _normalize(product_type) in _normalize(actual_product_type):
            continue

        if vendor and not _normalize(vendor) in _normalize(actual_vendor):
            continue

        matched_product = product
        break

    if matched_product:
        if matched_product.get("productType"):
            canonical["product_type"] = matched_product.get("productType")

        if matched_product.get("vendor"):
            canonical["vendor"] = matched_product.get("vendor")

        variants = matched_product.get("variants", {}).get("nodes", [])
    else:
        variants = []
        for product in products:
            variants.extend(product.get("variants", {}).get("nodes", []))

    matched_variant = None

    for variant in variants:
        variant_color, variant_size = _extract_variant_parts(variant.get("title", ""))

        if color and (
            not variant_color
            or _normalize(color) not in _normalize(variant_color)
        ):
            continue

        if size and (
            not variant_size
            or _normalize(size) not in _normalize(variant_size)
        ):
            continue

        matched_variant = variant
        break

    if matched_variant:
        variant_color, variant_size = _extract_variant_parts(
            matched_variant.get("title", "")
        )

        if color and variant_color:
            canonical["color"] = variant_color

        if size and variant_size:
            canonical["size"] = variant_size

    return canonical


def build_storefront_url(filters: dict) -> str:
    params = {}

    product_type = filters.get("product_type")
    color = filters.get("color")
    size = filters.get("size")
    vendor = filters.get("vendor")
    max_price = filters.get("max_price")
    min_price = filters.get("min_price")

    # Product type
    if product_type:
        params["filter.p.product_type"] = product_type

    # Color
    if color:
        params["filter.v.option.color"] = color.title()

    # Size
    if size:
        params["filter.v.option.size"] = size.upper()

    # Vendor
    if vendor:
        params["filter.p.vendor"] = vendor

    # Price
    if min_price is not None:
        params["filter.v.price.gte"] = min_price
    elif max_price is not None:
        # Shopify's UI uses 0 as the lower bound
        params["filter.v.price.gte"] = 0

    if max_price is not None:
        params["filter.v.price.lte"] = max_price

    if not params:
        return f"{SHOPIFY_STORE_URL}/collections/all"

    # Keep the collection sorted consistently
    params["sort_by"] = "title-ascending"

    return (
        f"{SHOPIFY_STORE_URL}/collections/all?"
        f"{urlencode(params)}"
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
        canonical_filters = canonicalize_filters(
            filters,
            filtered_products or products
        )

        return build_storefront_url(canonical_filters)

    return None
