import asyncio
from shopify_service import get_products
async def main():
    products = await get_products("bhavin-ai-shopping-assistant-demo.myshopify.com", None)
    types = {p.get("productType") for p in products if p.get("productType")}
    print(types)
asyncio.run(main())
