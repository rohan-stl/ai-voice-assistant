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
    Always returns relative path (e.g. /collections/summer-collection?filter.p.product_type=T-Shirt).
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
        base_path = f"/collections/{collection_handle}"
        use_search = False
    else:
        use_search = bool(product_names)
        base_path = "/search" if use_search else "/collections/all"

    if use_search and product_names:
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
        return base_path

    params["sort_by"] = "title-ascending"
    query_str = urlencode(params, doseq=True)
    return f"{base_path}?{query_str}"