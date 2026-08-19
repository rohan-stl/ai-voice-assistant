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
    print("[LLM] Request received")
    print("[LLM] Customer message:", message)
    print("[LLM] Product count:", len(products))

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

Your job is to determine whether the customer is asking to:
- filter/find products,
- open a specific product,
- open a specific collection/category, or
- ask for clarification about a shopping request.

You are not a general-purpose assistant.
Do not answer unrelated factual questions.
If the utterance is off-topic, unclear, or likely a speech-to-text mistake,
ask the user to clarify what product or collection they want.

Return ONLY valid JSON.

The JSON must have exactly this structure:

{{
  "intent": "filter_products" or "product_request" or "collection_request" or "clarification_needed",
  "navigation_target": {{
    "type": "product" or "collection" or null,
    "query": "the product or collection name from the user" or null
  }},
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

1. If the customer is looking for products by attributes, use:
   "intent": "filter_products"

2. If the customer wants a specific product, use:
   "intent": "product_request"
   and set navigation_target.type to "product"

3. If the customer wants a collection/category, use:
   "intent": "collection_request"
   and set navigation_target.type to "collection"

4. If a filter was not specified, set it to null.

5. Do NOT invent filters or navigation targets.

6. If the customer says:
   "Show me blue T-shirts"

   return approximately:

   {{
     "intent": "filter_products",
     "navigation_target": {{
       "type": null,
       "query": null
     }},
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
     "navigation_target": {{
       "type": null,
       "query": null
     }},
     "filters": {{
       "product_type": "T-shirt",
       "color": "blue",
       "vendor": "Nike",
       "max_price": 40,
       "min_price": null
     }},
     "answer": "Here are the Nike blue T-shirts under $40."
   }}

8. If the customer says:
   "Show me the available t-shirts"

   return approximately:

   {{
     "intent": "collection_request",
     "navigation_target": {{
       "type": "collection",
       "query": "t-shirts"
     }},
     "filters": {{
       "product_type": "T-shirt",
       "color": null,
       "size": null,
       "vendor": null,
       "max_price": null,
       "min_price": null
     }},
     "answer": "Here are the T-shirts I found."
   }}

9. If the customer says something unrelated to shopping,
   such as an academic, news, or general knowledge question,
   return:

   {{
     "intent": "clarification_needed",
     "navigation_target": {{
       "type": null,
       "query": null
     }},
     "filters": {{
       "product_type": null,
       "color": null,
       "size": null,
       "vendor": null,
       "max_price": null,
       "min_price": null
     }},
     "answer": "I can help with products in this store. If you meant a product, collection, color, size, or price, please say that again."
   }}

10. Do not invent product information.

11. Keep the answer concise and conversational.

12. Return ONLY JSON. Do not use markdown code fences.
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

    print("[LLM] Response object received")

    if not response.choices:
        print("[LLM] No choices returned")
        return {
            "intent": "clarification_needed",
            "navigation_target": {
                "type": None,
                "query": None
            },
            "filters": {
                "product_type": None,
                "color": None,
                "size": None,
                "vendor": None,
                "max_price": None,
                "min_price": None
            },
            "answer": "I can help with products in this store. Please tell me what item, collection, color, size, or price you want."
        }

    content = response.choices[0].message.content

    print("[LLM] Raw content:", content)

    if not content:
        print("[LLM] Empty content returned")
        return {
            "intent": "clarification_needed",
            "navigation_target": {
                "type": None,
                "query": None
            },
            "filters": {
                "product_type": None,
                "color": None,
                "size": None,
                "vendor": None,
                "max_price": None,
                "min_price": None
            },
            "answer": "I can help with products in this store. Please tell me what item, collection, color, size, or price you want."
        }

    content = content.strip()

    try:
        parsed = json.loads(content)
        print("[LLM] Parsed JSON:", parsed)

        if "navigation_target" not in parsed:
            parsed["navigation_target"] = {
                "type": None,
                "query": None
            }

        if parsed.get("intent") == "general_question":
            parsed["intent"] = "clarification_needed"
            parsed["answer"] = (
                "I can help with products in this store. "
                "If you meant a product, collection, color, size, or price, "
                "please say that again."
            )

        return parsed

    except json.JSONDecodeError:
        print("[LLM] Failed to parse JSON, using fallback")
        # Fallback if the model doesn't return valid JSON
        return {
            "intent": "clarification_needed",
            "navigation_target": {
                "type": None,
                "query": None
            },
            "filters": {
                "product_type": None,
                "color": None,
                "size": None,
                "vendor": None,
                "max_price": None,
                "min_price": None
            },
            "answer": (
                "I can help with products in this store. "
                "If you meant a product, collection, color, size, or price, "
                "please say that again."
            )
        }
