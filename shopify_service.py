import requests
from shopify_auth import get_access_token, STORE_DOMAIN

SHOPIFY_API_VERSION = "2026-07"


def get_products(first=20):
    token = get_access_token()

    url = f"https://{STORE_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"

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

                variants(first: 10) {
                    nodes {
                        id
                        title
                        price
                        availableForSale
                        inventoryQuantity
                    }
                }
            }
        }
    }
    """

    variables = {
        "first": first
    }

    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }

    response = requests.post(
        url,
        json={
            "query": query,
            "variables": variables
        },
        headers=headers,
    )

    response.raise_for_status()

    data = response.json()

    if "errors" in data:
        raise Exception(data["errors"])

    return data["data"]["products"]["nodes"]


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