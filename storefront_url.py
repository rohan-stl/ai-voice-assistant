from urllib.parse import urlencode


SHOPIFY_STORE_URL = "https://bhavin-ai-shopping-assistant-demo.myshopify.com"


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