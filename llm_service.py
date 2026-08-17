import os
import json
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1"
)


def ask_llm(message: str, products: list) -> dict:

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
You are an AI shopping assistant for a Shopify store.

Customer request:
{message}

Available products:
{product_text}

Your job is to understand the customer's request and determine whether
they are asking to filter/find products or asking a general question.

Return ONLY valid JSON.

The JSON must have exactly this structure:

{{
  "intent": "filter_products" or "general_question",
  "filters": {{
    "product_type": null,
    "color": null,
    "size": null,
    "vendor": null,
    "max_price": null,
    "min_price": null
}},
  "answer": "short conversational response"
}}

Rules:

1. If the customer is looking for products, use:
   "intent": "filter_products"

2. Extract product filters from the customer's request.

3. Possible filters include:
   - product_type
   - color
   - size
   - vendor
   - max_price
   - min_price

4. If a filter was not specified, set it to null.

5. Do NOT invent filters.

6. If the customer says:
   "Show me blue T-shirts"

   return approximately:

   {{
     "intent": "filter_products",
     "filters": {{
       "product_type": "T-shirt",
       "color": "blue",
       "vendor": null,
       "max_price": null,
       "min_price": null
     }},
     "answer": "Here are the blue T-shirts I found."
   }}

7. If the customer says:
   "Show me Nike blue T-shirts under $40"

   return approximately:

   {{
     "intent": "filter_products",
     "filters": {{
       "product_type": "T-shirt",
       "color": "blue",
       "vendor": "Nike",
       "max_price": 40,
       "min_price": null
     }},
     "answer": "Here are the Nike blue T-shirts under $40."
   }}

If the customer says:
   "Show me blue T-shirts of size Medium"

   return approximately:

   {{
     "intent": "filter_products",
     "filters": {{
       "product_type": "T-Shirt",
       "color": "blue",
       "size": "M",
       "vendor": null,
       "max_price": null,
       "min_price": null
     }},
     "answer": "Here are the blue medium T-shirts I found."
   }}

8. If the customer asks:
   "What is the capital of India?"

   return approximately:

   {{
     "intent": "general_question",
     "filters": {{
    "product_type": None,
    "color": None,
    "size": None,
    "vendor": None,
    "max_price": None,
    "min_price": None
}},
     "answer": "The capital of India is New Delhi."
   }}

9. Do not invent product information.

10. Keep the answer concise and conversational.

11. Return ONLY JSON. Do not use markdown code fences.
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

    if not response.choices:
        return {
            "intent": "general_question",
            "filters": {
                "product_type": None,
                "color": None,
                "size": None,
                "vendor": None,
                "max_price": None,
                "min_price": None
            },
            "answer": "I didn't understand that. Please try again."
        }

    content = response.choices[0].message.content

    if not content:
        return {
            "intent": "general_question",
            "filters": {
                "product_type": None,
                "color": None,
                "size": None,
                "vendor": None,
                "max_price": None,
                "min_price": None
            },
            "answer": "I didn't understand that. Please try again."
        }

    content = content.strip()

    try:
        return json.loads(content)

    except json.JSONDecodeError:
        # Fallback if the model doesn't return valid JSON
        return {
            "intent": "general_question",
            "filters": {
                "product_type": None,
                "color": None,
                "vendor": None,
                "max_price": None,
                "min_price": None
            },
            "answer": content
        }