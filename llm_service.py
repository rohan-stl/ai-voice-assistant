"""
llm_service.py

Handles all LLM interaction via OpenRouter.

Two entry points:
  ask_llm(message, products)
      Used for the initial browse stage — extracts intent + product filters.

  ask_llm_cart(message, products, filtered_products, selected_product)
      Used after the user has seen results — extracts:
        intent: select_variant | add_to_cart | confirm_yes | confirm_no
                | filter_products | general_question
        product_ref: which product the user is talking about
        variant_ref: size / color / sku the user mentioned
        quantity:    how many units they want (default 1)
"""

import os
import json
import time
from openai import OpenAI, RateLimitError
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1",
)

MODEL_PRIORITY = [
    "openrouter/free",
    "google/gemma-3-27b-it:free",
    "meta-llama/llama-3.2-3b-instruct:free",
]
MODEL_NAME = MODEL_PRIORITY[0]
MAX_RETRIES = 3
MAX_RETRY_WAIT_SECONDS = 30


# -------------------------------------------------------
# Fallback responses
# -------------------------------------------------------

FALLBACK_RESPONSE = {
    "intent": "general_question",
    "filters": {
        "product_name": None,
        "product_type": None,
        "color": None,
        "size": None,
        "vendor": None,
        "max_price": None,
        "min_price": None,
    },
    "answer": "I didn't understand that. Please try again.",
}

RATE_LIMITED_RESPONSE = {
    **FALLBACK_RESPONSE,
    "answer": (
        "I'm getting a lot of requests right now — "
        "please try again in a few seconds."
    ),
}

FALLBACK_CART_RESPONSE = {
    "intent": "general_question",
    "product_ref": None,
    "variant_ref": None,
    "quantity": 1,
    "answer": "I didn't understand that. Could you please clarify?",
}


# -------------------------------------------------------
# Internal helpers
# -------------------------------------------------------

def _extract_retry_after_seconds(error: RateLimitError):
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
    """Try each model in MODEL_PRIORITY until one succeeds."""
    last_error = None
    for model in MODEL_PRIORITY:
        for attempt in range(MAX_RETRIES):
            try:
                print(f"[LLM] Calling {model} (attempt {attempt + 1})")
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,  # low temp → more deterministic JSON
                )
                if resp.choices and resp.choices[0].message.content:
                    return resp
            except RateLimitError as error:
                last_error = error
                wait_seconds = _extract_retry_after_seconds(error)
                if wait_seconds is None:
                    wait_seconds = 2 ** attempt
                wait_seconds = min(wait_seconds, MAX_RETRY_WAIT_SECONDS)
                is_last = attempt == MAX_RETRIES - 1
                print(
                    f"[LLM] Rate-limited on {model} (attempt {attempt + 1}/{MAX_RETRIES}). "
                    f"{'Trying next model.' if is_last else f'Retrying in {wait_seconds}s...'}"
                )
                if not is_last:
                    time.sleep(wait_seconds)
            except Exception as error:
                last_error = error
                print(f"[LLM] Error on {model}: {error}")
                break  # try next model immediately
    raise last_error


def _parse_json(content: str, fallback: dict) -> dict:
    content = content.strip()
    # Strip markdown code fences if present
    if content.startswith("```"):
        lines = content.splitlines()
        content = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {**fallback, "answer": content}


def _build_product_text(products: list) -> str:
    lines = []
    for product in products:
        variants = product.get("variants", {}).get("nodes", [])
        variant_text = []
        for v in variants:
            opts = v.get("selectedOptions", [])
            opt_str = ", ".join(f"{o['name']}: {o['value']}" for o in opts) if opts else v.get("title", "")
            variant_text.append(
                f"  - VariantID: {v.get('id', '')}, {opt_str}, "
                f"Price: ${v.get('price', '')}, "
                f"Available: {v.get('availableForSale', False)}, "
                f"Inventory: {v.get('inventoryQuantity', 0)}"
            )
        lines.append(
            f"Product: {product.get('title', '')}\n"
            f"ProductType: {product.get('productType', '')}\n"
            f"Vendor: {product.get('vendor', '')}\n"
            f"Variants:\n" + "\n".join(variant_text)
        )
    return "\n\n".join(lines)


# -------------------------------------------------------
# Public entry point 1: Browse / filter
# -------------------------------------------------------

def ask_llm(message: str, products: list) -> dict:
    """
    Extract intent and product filters from a user browse query.
    Returns:
      { intent, filters, answer }
    """
    product_text = _build_product_text(products)

    prompt = f"""
You are an AI shopping assistant for a Shopify store.

Customer request:
{message}

Available products:
{product_text}

Your job is to understand the customer's request and determine whether
they are asking to filter/find products or asking a general question.

Return ONLY valid JSON with exactly this structure:

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
1. Use "filter_products" when the customer is browsing for products.
2. Extract relevant filters; set unused ones to null.
3. "product_type" = general category (e.g. "T-Shirt", "Snowboard").
4. "product_name" = specific named product from the catalog.
5. Never put a specific product name into "product_type".
6. Keep the answer concise and conversational.
7. Return ONLY JSON. No markdown code fences.
"""
    try:
        response = _call_llm_with_retry(prompt)
    except Exception as exc:
        # Catch RateLimitError, NotFoundError, and any other API error
        # so the server never returns a 500 to the client.
        print(f"LLM ask_llm error (non-fatal): {exc}")
        if "rate" in str(exc).lower() or "429" in str(exc):
            return RATE_LIMITED_RESPONSE
        return FALLBACK_RESPONSE

    if not response.choices:
        return FALLBACK_RESPONSE

    content = response.choices[0].message.content
    if not content:
        return FALLBACK_RESPONSE

    return _parse_json(content, FALLBACK_RESPONSE)


# -------------------------------------------------------
# Public entry point 2: Cart / variant selection
# -------------------------------------------------------

def ask_llm_cart(
    message: str,
    filtered_products: list,
    selected_product: dict = None,
) -> dict:
    """
    Interpret a follow-up message when the user is selecting a variant
    or adding to cart.

    Returns:
      {
        "intent": one of:
          "select_variant"  – user specifying size/color/qty
          "add_to_cart"     – user explicitly saying add to cart
          "confirm_yes"     – user confirming a pre-add prompt
          "confirm_no"      – user cancelling
          "filter_products" – user is actually searching for new products
          "general_question"
        "product_ref": product title the user is referring to (or null)
        "variant_ref": size/color/sku mentioned (or null, e.g. "M", "blue", "XL")
        "quantity": integer (default 1),
        "answer": short conversational response
      }
    """
    product_text = _build_product_text(filtered_products)

    selected_context = ""
    if selected_product:
        selected_context = (
            f"\nThe customer has already shown interest in: "
            f"{selected_product.get('title', '')}\n"
        )

    prompt = f"""
You are an AI shopping assistant for a Shopify store helping a customer
complete their purchase.
{selected_context}
Currently shown products:
{product_text}

Customer says:
{message}

Determine what the customer wants. Return ONLY valid JSON:

{{
  "intent": "<intent>",
  "product_ref": "<product title or null>",
  "variant_ref": "<size/color/sku or null>",
  "quantity": <integer, default 1>,
  "answer": "<short conversational response>"
}}

Intent values:
- "select_variant"  – customer is specifying a size, color, quantity, or picking a product from a list
- "add_to_cart"     – customer explicitly says "add to cart", "buy it", "add this", "I want this"
- "confirm_yes"     – customer confirms a pending action ("yes", "that's right", "go ahead", "confirm")
- "confirm_no"      – customer cancels ("no", "cancel", "nevermind", "go back")
- "filter_products" – customer is searching for different/new products (start a new search)
- "general_question"– customer is asking a question not related to selecting/buying

Rules:
- Extract the variant the customer mentions (size like "M", "Large", "XL"; color like "blue", "red")
- Normalise sizes: "medium" → "M", "large" → "L", "small" → "S", "extra large" → "XL"
- If the customer says "2 of them" or "add 3", set quantity accordingly
- product_ref should be the exact product title from the shown products list, or null if unclear
- Return ONLY JSON. No markdown code fences.
"""
    try:
        response = _call_llm_with_retry(prompt)
    except Exception as exc:
        # Catch RateLimitError, NotFoundError, and any other API error
        # so the server never returns a 500 to the client.
        print(f"LLM ask_llm_cart error (non-fatal): {exc}")
        rate_msg = RATE_LIMITED_RESPONSE["answer"]
        return {**FALLBACK_CART_RESPONSE, "answer": rate_msg}

    if not response.choices:
        return FALLBACK_CART_RESPONSE

    content = response.choices[0].message.content
    if not content:
        return FALLBACK_CART_RESPONSE

    result = _parse_json(content, FALLBACK_CART_RESPONSE)

    # Ensure quantity is always a positive int
    try:
        result["quantity"] = max(1, int(result.get("quantity", 1)))
    except (TypeError, ValueError):
        result["quantity"] = 1

    return result