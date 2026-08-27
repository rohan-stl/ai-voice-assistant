import os
import json
import time
from dotenv import load_dotenv
from llm_provider import GroqLLMProvider, OpenRouterLLMProvider

load_dotenv()

LLM_PROVIDER_NAME = os.getenv("LLM_PROVIDER", "groq")


def _create_llm_provider():
    if LLM_PROVIDER_NAME == "groq":
        return GroqLLMProvider(api_key=os.getenv("GROQ_API_KEY", ""))
    elif LLM_PROVIDER_NAME == "openrouter":
        return OpenRouterLLMProvider(api_key=os.getenv("OPENROUTER_API_KEY", ""))
    else:
        raise ValueError(f"Unknown LLM provider: {LLM_PROVIDER_NAME}")


# Singleton initialized once at module load
_llm = _create_llm_provider()

MAX_RETRIES = 3
MAX_RETRY_WAIT_SECONDS = 30

FALLBACK_RESPONSE = {
    "intent": "general_question",
    "filters": {
        "product_name": None,
        "product_type": None,
        "color": None,
        "size": None,
        "vendor": None,
        "max_price": None,
        "min_price": None
    },
    "answer": "I didn't understand that. Please try again."
}

RATE_LIMITED_RESPONSE = {
    "intent": "general_question",
    "filters": {
        "product_name": None,
        "product_type": None,
        "color": None,
        "size": None,
        "vendor": None,
        "max_price": None,
        "min_price": None
    },
    "answer": (
        "I'm getting a lot of requests right now — "
        "please try again in a few seconds."
    )
}


def _call_llm_with_retry(prompt: str) -> str:
    """
    Calls the LLM provider, retrying on generic Exceptions with simple backoff.
    """
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            return _llm.chat(prompt)
        except Exception as error:
            last_error = error
            # Simple exponential backoff
            wait_seconds = min(2 ** attempt, MAX_RETRY_WAIT_SECONDS)
            is_last_attempt = attempt == MAX_RETRIES - 1

            print(
                f"LLM request failed (attempt {attempt + 1}/{MAX_RETRIES}). "
                f"{'Giving up.' if is_last_attempt else f'Retrying in {wait_seconds}s...'}. "
                f"Error: {error}"
            )

            if not is_last_attempt:
                time.sleep(wait_seconds)

    raise last_error


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
    "product_name": null,
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
   - product_name
   - product_type
   - color
   - size
   - vendor
   - max_price
   - min_price

4. If a filter was not specified, set it to null.

5. Do NOT invent filters.

5a. Always spell "product_type" for T-shirts exactly as "T-Shirt"
    (capital T, capital S), matching how it appears in the product
    catalog above. Use this exact casing consistently, never
    "T-shirt" or "t-shirt".

5b. "product_name" vs "product_type" — these are different things:
    - "product_type" is a general category (e.g. "Snowboard", "T-Shirt")
      and should be used when the customer describes what KIND of thing
      they want.
    - "product_name" is one specific, named product taken from the
      catalog above (e.g. "The Complete Snowboard", "Classic T-Shirt")
      and should be used when the customer refers to a particular
      product by name, even loosely (e.g. "the complete snowboard",
      "the archived one", "that classic tee").
    When the customer names a specific product, set "product_name" to
    that product's exact title as it appears in the catalog above, and
    leave "product_type" null unless the customer ALSO separately
    mentions a general category. Never put a specific product's name
    into "product_type".

6. If the customer says:
   "Show me blue T-shirts"

   return approximately:

   {{
     "intent": "filter_products",
     "filters": {{
       "product_name": null,
       "product_type": "T-Shirt",
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
       "product_name": null,
       "product_type": "T-Shirt",
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
       "product_name": null,
       "product_type": "T-Shirt",
       "color": "blue",
       "size": "M",
       "vendor": null,
       "max_price": null,
       "min_price": null
     }},
     "answer": "Here are the blue medium T-shirts I found."
   }}

7a. If the customer says:
   "Show me the complete snowboard"

   and the catalog above contains a product titled "The Complete
   Snowboard", return approximately:

   {{
     "intent": "filter_products",
     "filters": {{
       "product_name": "The Complete Snowboard",
       "product_type": null,
       "color": null,
       "size": null,
       "vendor": null,
       "max_price": null,
       "min_price": null
     }},
     "answer": "Here's the Complete Snowboard."
   }}

7b. If the customer says:
   "Show me the complete snowboard under $700"

   return approximately:

   {{
     "intent": "filter_products",
     "filters": {{
       "product_name": "The Complete Snowboard",
       "product_type": null,
       "color": null,
       "size": null,
       "vendor": null,
       "max_price": 700,
       "min_price": null
     }},
     "answer": "Here's the Complete Snowboard, if it's under $700."
   }}

8. If the customer asks:
   "What is the capital of India?"

   return approximately:

   {{
     "intent": "general_question",
     "filters": {{
        "product_name": null,
        "product_type": null,
        "color": null,
        "size": null,
        "vendor": null,
        "max_price": null,
        "min_price": null
     }},
     "answer": "The capital of India is New Delhi."
   }}

9. Do not invent product information.

10. Keep the answer concise and conversational.

11. Return ONLY JSON. Do not use markdown code fences.
"""

    try:
        content = _call_llm_with_retry(prompt)
    except Exception:
        # Fallback response if retry attempts are exhausted or rate-limited
        return FALLBACK_RESPONSE

    if not content:
        return FALLBACK_RESPONSE

    content = content.strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Fallback if the model response isn't formatted as valid JSON
        return {
            "intent": "general_question",
            "filters": {
                "product_name": None,
                "product_type": None,
                "color": None,
                "size": None,
                "vendor": None,
                "max_price": None,
                "min_price": None
            },
            "answer": content
        }
