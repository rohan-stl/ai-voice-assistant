from urllib.parse import urlencode


SHOPIFY_STORE_URL = "https://bhavin-ai-shopping-assistant-demo.myshopify.com"


def _as_list(value):
    """
    Normalize a filter value into a list of trimmed strings.

    Accepts None, a single string (possibly comma-separated, e.g.
    "white, blue"), or an already-iterable value like a list.
    """
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]

    return [
        part.strip()
        for part in str(value).split(",")
        if part.strip()
    ]


def build_storefront_url(filters: dict) -> str:
    params = {}

    product_names = _as_list(filters.get("product_name"))
    product_types = _as_list(filters.get("product_type"))
    colors = _as_list(filters.get("color"))
    sizes = _as_list(filters.get("size"))
    vendors = _as_list(filters.get("vendor"))
    max_price = filters.get("max_price")
    min_price = filters.get("min_price")

    # Shopify's native collection filters (filter.p.*) are facet-based —
    # there is no "product name" facet, so a title can't be expressed as
    # a filter.p.* param the way product_type/vendor can. Free-text
    # product-name matching only works through the storefront search
    # endpoint's `q` param, so route there instead of /collections/all
    # whenever a product_name is present.
    use_search_endpoint = bool(product_names)

    if product_names:
        params["q"] = " ".join(product_names)
        params["type"] = "product"

    # Product type (Shopify supports repeated params for OR-filtering)
    if product_types:
        params["filter.p.product_type"] = product_types

    # Color
    if colors:
        params["filter.v.option.color"] = [c.title() for c in colors]

    # Size
    if sizes:
        params["filter.v.option.size"] = [s.upper() for s in sizes]

    # Vendor
    if vendors:
        params["filter.p.vendor"] = vendors

    # Price
    if min_price is not None:
        params["filter.v.price.gte"] = min_price
    elif max_price is not None:
        # Shopify's UI uses 0 as the lower bound
        params["filter.v.price.gte"] = 0

    if max_price is not None:
        params["filter.v.price.lte"] = max_price

    if not params:
        return None

    # Keep results sorted consistently
    params["sort_by"] = "title-ascending"

    base_path = "/search" if use_search_endpoint else "/collections/all"

    return (
        f"{SHOPIFY_STORE_URL}{base_path}?"
        f"{urlencode(params, doseq=True)}"
    )