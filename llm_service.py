import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1"
)

def ask_llm(message: str, products: list) -> str:

    product_text = []

    for product in products:
        variants = product.get("variants", {}).get("nodes", [])

        variant_text = []

        for variant in variants:
            variant_text.append(
                f"Variant: {variant.get('title', '')}, "
                f"Price: ${variant.get('price', '')}, "
                f"Available: {variant.get('availableForSale', False)}, "
                f"Inventory: {variant.get('inventoryQuantity', 0)}"
            )

        product_text.append(
            f"""
Product: {product.get('title', '')}
Product Type: {product.get('productType', '')}
Description: {product.get('description', '')}
Vendor: {product.get('vendor', '')}
Status: {product.get('status', '')}
Total Inventory: {product.get('totalInventory', 0)}
Product URL: {product.get('onlineStoreUrl', '')}
Variants:
{" | ".join(variant_text)}
"""
        )

    product_text = "\n".join(product_text)

    prompt = f"""
You are an AI shopping assistant.

Customer request:
{message}

Available products:
{product_text}

Your job is to recommend the most relevant products based ONLY on the
information provided above.

Consider:
- Customer's requirements
- Product type
- Product description
- Price
- Availability
- Variants
- Inventory

Important rules:
- Do not invent product information.
- Do not claim a product is available if availableForSale is false.
- If the customer specifies a maximum price, do not recommend products
  above that price unless you clearly explain that no matching product
  was found.
- If no suitable product exists, clearly tell the customer.
- Keep the response concise and conversational.
"""

    response = client.chat.completions.create(
        model="openai/gpt-oss-20b:free",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    return response.choices[0].message.content