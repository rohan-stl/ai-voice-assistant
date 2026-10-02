from urllib.parse import urlencode


def _as_list(value):
    """Normalize a filter value into a list of trimmed strings."""
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]

    return [
        part.strip()
        for part in str(value).split(",")
        if part.strip()
    ]


def _clean_product_names(product_names: list, product_types: list) -> list:
    if not product_names or not product_types:
        return product_names

    cleaned = []
    type_roots = {pt.lower().replace("s", "") for pt in product_types}

    for name in product_names:
        name_root = name.lower().replace("s", "")
        if any(tr[:4] in name_root or name_root[:4] in tr for tr in type_roots if len(tr) >= 4):
            continue
        cleaned.append(name)

    return cleaned


def build_storefront_url(filters: dict = None, collection_handle: str = None) -> str:
    """
    Build relative storefront URL for a collection or search page.
    Always returns relative path.

    Strategy:
    - collection_handle supplied → /collections/<handle>?filter.*  (native collection page)
    - product_name only          → /search?q=<name>&type=product
    - product_type only          → /search?q=<type>&type=product
      (avoids broken filter.p.product_type= URLs when Shopify's native storefront
       filtering is not enabled or the productType field is blank/mismatched in
       Shopify Admin — the search endpoint works by title/description regardless)
    - product_type + color/size  → /search?q=<type>&type=product + filter params
    - Both product_name AND type → /search with combined query terms
    """
    filters = filters or {}
    params = {}

    product_types = _as_list(filters.get("product_type"))
    raw_names = _as_list(filters.get("product_name"))
    product_names = _clean_product_names(raw_names, product_types)
    colors = _as_list(filters.get("color"))
    sizes = _as_list(filters.get("size"))
    vendors = _as_list(filters.get("vendor"))
    max_price = filters.get("max_price")
    min_price = filters.get("min_price")

    if collection_handle:
        # ── Collection page: keep native filter params (theme must support them)
        base_path = f"/collections/{collection_handle}"

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

    else:
        # ── No specific collection: always use /search so results show regardless
        # of whether Shopify native filtering is configured on the theme.
        base_path = "/search"

        # Build query string: combine type keywords + product names
        query_terms = []
        if product_types:
            query_terms.extend(product_types)
        if product_names:
            query_terms.extend(product_names)

        if query_terms:
            params["q"] = " ".join(query_terms)
            params["type"] = "product"
        elif vendors:
            # vendor-only: search by vendor name
            params["q"] = " ".join(vendors)
            params["type"] = "product"

        # Add option filters where theme search supports them
        if colors:
            params["filter.v.option.color"] = [c.title() for c in colors]

        if sizes:
            params["filter.v.option.size"] = [s.upper() for s in sizes]

        if min_price is not None:
            params["filter.v.price.gte"] = min_price
        elif max_price is not None:
            params["filter.v.price.gte"] = 0

        if max_price is not None:
            params["filter.v.price.lte"] = max_price

    if not params:
        return base_path

    params["sort_by"] = "title-ascending"
    query_str = urlencode(params, doseq=True)
    return f"{base_path}?{query_str}"