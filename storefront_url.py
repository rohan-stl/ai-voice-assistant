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

    product_types = _as_list(filters.get("product_type"))
    colors = _as_list(filters.get("color"))
    sizes = _as_list(filters.get("size"))
    vendors = _as_list(filters.get("vendor"))
    max_price = filters.get("max_price")
    min_price = filters.get("min_price")

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

    # Keep the collection sorted consistently
    params["sort_by"] = "title-ascending"

    return (
        f"{SHOPIFY_STORE_URL}/collections/all?"
        f"{urlencode(params, doseq=True)}"
    )