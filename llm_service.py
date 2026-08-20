import os
import json
import time
from openai import OpenAI, RateLimitError
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1"
)

MODEL_NAME = "openai/gpt-oss-20b:free"

MAX_RETRIES = 3

# Cap how long any single retry wait can be, even if the provider
# reports a much larger retry_after_seconds value.
MAX_RETRY_WAIT_SECONDS = 30

FALLBACK_RESPONSE = {
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

RATE_LIMITED_RESPONSE = {
    "intent": "general_question",
    "filters": {
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


def _extract_retry_after_seconds(error: RateLimitError):
    """
    Pull the provider-suggested retry_after_seconds out of the error
    body if present (OpenRouter includes this in error.metadata).
    Returns None if it can't be found.
    """
    try:
        body = getattr(error, "body", None) or {}
        metadata = body.get("error", {}).get("metadata", {})
        retry_after = metadata.get("retry_after_seconds")

        if retry_after is not None:
            return float(retry_after)

    except Exception:
        pass

    return None


def _call_llm_with_retry(prompt: str):
    """
    Calls the LLM, retrying on RateLimitError with the provider's
    suggested wait time (falling back to simple exponential backoff
    if none is given). Re-raises the last error if all retries fail.
    """
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            return client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            )

        except RateLimitError as error:
            last_error = error

            wait_seconds = _extract_retry_after_seconds(error)

            if wait_seconds is None:
                # No hint from the provider — simple exponential backoff
                wait_seconds = 2 ** attempt

            wait_seconds = min(wait_seconds, MAX_RETRY_WAIT_SECONDS)

            is_last_attempt = attempt == MAX_RETRIES - 1

            print(
                f"LLM rate-limited (attempt {attempt + 1}/{MAX_RETRIES}). "
                f"{'Giving up.' if is_last_attempt else f'Retrying in {wait_seconds}s...'}"
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

5a. Always spell "product_type" for T-shirts exactly as "T-Shirt"
    (capital T, capital S), matching how it appears in the product
    catalog above. Use this exact casing consistently, never
    "T-shirt" or "t-shirt".

6. If the customer says:
   "Show me blue T-shirts"

   return approximately:

   {{
     "intent": "filter_products",
     "filters": {{
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

    try:
        response = _call_llm_with_retry(prompt)

    except RateLimitError:
        return RATE_LIMITED_RESPONSE

    if not response.choices:
        return FALLBACK_RESPONSE

    content = response.choices[0].message.content

    if not content:
        return FALLBACK_RESPONSE

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