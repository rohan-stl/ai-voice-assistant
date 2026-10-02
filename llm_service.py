"""
llm_service.py

Handles all LLM interaction via OpenRouter.
Extracts intent, product filters, and collection requests from customer speech.
"""

import os
import json
import re
import time
from openai import OpenAI, RateLimitError
from dotenv import load_dotenv
import logger as log
from models import CartIntentModel, BrowseIntentModel

load_dotenv()


def _coerce_llm_filters(raw: dict) -> dict:
    """
    Coerce and validate the raw LLM filters dict before use.

    Handles:
    - Price as string ("$50", "50 USD", "fifty") → float or None
    - Null-like strings ("null", "none", "") → None
    - Whitespace stripping on all string fields
    """
    if not isinstance(raw, dict):
        return {}

    out = {}
    _NULL_STRINGS = {"", "null", "none", "n/a", "na", "nil", "undefined"}

    for key in ("product_name", "product_type", "color", "size", "vendor", "collection_name"):
        val = raw.get(key)
        if val is None or (isinstance(val, str) and val.strip().lower() in _NULL_STRINGS):
            out[key] = None
        else:
            out[key] = str(val).strip()

    for key in ("min_price", "max_price"):
        val = raw.get(key)
        if val is None or (isinstance(val, str) and val.strip().lower() in _NULL_STRINGS):
            out[key] = None
        else:
            try:
                # Strip currency symbols, spaces, and commas before parsing
                clean = re.sub(r"[^\d.]", "", str(val))
                out[key] = float(clean) if clean else None
            except (ValueError, TypeError):
                out[key] = None

    return out


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


FALLBACK_RESPONSE = {
    "intent": "general_question",
    "collection_name": None,
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
    last_error = None
    for model in MODEL_PRIORITY:
        for attempt in range(MAX_RETRIES):
            try:
                log.log_llm_call(model, attempt + 1)
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
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
                log.log_warn(
                    f"LLM rate-limited on {model} ({attempt+1}/{MAX_RETRIES})",
                    "Trying next model." if is_last else f"Retrying in {wait_seconds}s…"
                )
                if not is_last:
                    time.sleep(wait_seconds)
            except Exception as error:
                last_error = error
                log.log_error(f"LLM error on {model}", error)
                break
    raise last_error


def _parse_json(content: str, fallback: dict) -> dict:
    content = content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        content = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return _sanitize_answer(data, fallback)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return _sanitize_answer(data, fallback)
        except json.JSONDecodeError:
            pass

    clean_content = content
    if "user safety:" in clean_content.lower() or "safety:" in clean_content.lower():
        clean_content = fallback.get("answer", "I found matching products for you.")

    return {**fallback, "answer": clean_content}


def _sanitize_answer(data: dict, fallback: dict) -> dict:
    answer = str(data.get("answer", "") or "").strip()
    if "user safety:" in answer.lower() or "safety:" in answer.lower():
        data["answer"] = fallback.get("answer", "I found matching products for you.")
    return data


def _build_product_text(products: list, query: str = "") -> str:
    """
    Build a compact product catalogue string for the LLM prompt.

    When a query string is supplied, products are scored by token overlap
    against the query and the top 10 most relevant are sent, not just the
    first 15 in catalogue order. This prevents the target product from being
    invisible to the LLM when the store has many products.

    Each product line includes the handle (stable Shopify ID) so the LLM
    can return it as product_ref for a lossless round-trip match.
    """
    if not isinstance(products, list):
        products = []

    if query:
        q_words = set(re.split(r'\W+', query.lower())) - {
            "a", "an", "the", "i", "me", "my", "want", "need", "show",
            "find", "get", "some", "any", "please", "can", "you", "give",
            "looking", "for", "have", "do", "is", "are", "in", "of", "and"
        }
        q_words.discard("")
        scored = []
        for p in products:
            title_words = set(re.split(r'\W+', p.get("title", "").lower()))
            type_words  = set(re.split(r'\W+', p.get("productType", "").lower()))
            desc_words  = set(re.split(r'\W+', (p.get("description") or "").lower()))
            score = len(q_words & (title_words | type_words | desc_words))
            scored.append((score, p))
        scored.sort(key=lambda x: x[0], reverse=True)
        candidates = [p for _, p in scored[:10]]
    else:
        candidates = products[:10]

    lines = []
    for product in candidates:
        variants = product.get("variants", {}).get("nodes", [])
        v_summaries = []
        for v in variants[:5]:
            opts = v.get("selectedOptions", [])
            opt_str = "/".join(o['value'] for o in opts) if opts else v.get("title", "")
            v_summaries.append(f"{opt_str} (${v.get('price', '0')})")
        lines.append(
            f"• [{product.get('handle', '')}] {product.get('title', '')} "
            f"[{product.get('productType', '')}] - Vars: {', '.join(v_summaries)}"
        )
    return "\n".join(lines)


def ask_llm(message: str, products: list) -> dict:
    """Extract intent, collection references, and product filters from a browse query."""
    product_text = _build_product_text(products, query=message)

    prompt = f"""
You are an AI shopping assistant for a Shopify store.

Customer request:
{message}

Available products:
{product_text}

Determine what the customer wants. Return ONLY valid JSON with structure:

{{
  "intent": "search_products_in_collection" or "list_collections" or "clear_collection_filter" or "filter_products" or "general_question",
  "collection_name": "<collection title/name mentioned or null>",
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
1. "list_collections" when user asks what collections exist (e.g. "What collections do you have?", "Do you have collections?").
2. "clear_collection_filter" when user asks to search all products/store (e.g. "Show all products", "Search the whole store", "Remove collection filter").
3. "search_products_in_collection" when user mentions a specific collection (e.g. "Summer Collection", "Men's Collection", "Sale Collection").
4. "collection_name" should be the exact collection phrase mentioned (e.g. "Summer Collection", "Sale", "Men's").
5. Keep "answer" extremely short (1 sentence, max 10-12 words).
6. CRITICAL: If the user asks for a specific material, style, or type of item (e.g., "linen", "leather", "snowboard"), YOU MUST place that descriptive keyword in "product_name". Do not leave "product_name" null if they gave a descriptive word.
7. Return ONLY JSON. No markdown code fences.
"""
    try:
        response = _call_llm_with_retry(prompt)
    except Exception as exc:
        log.log_warn("ask_llm", str(exc))
        return {"intent": "general_question", "collection_name": None, "filters": {}, "answer": "Sorry, I had trouble understanding that."}

    if not response.choices:
        return FALLBACK_RESPONSE

    content = response.choices[0].message.content
    if not content:
        return FALLBACK_RESPONSE

    result = _parse_json(content, FALLBACK_RESPONSE)
    try:
        validated = BrowseIntentModel(**result)
        result = validated.model_dump()
    except Exception as e:
        log.log_warn("ask_llm_validation", f"Pydantic validation failed: {str(e)}")
        allowed_intents = ["search_products_in_collection", "list_collections", "clear_collection_filter", "filter_products", "general_question"]
        if result.get("intent") not in allowed_intents:
            result["intent"] = "general_question"
        if not isinstance(result.get("filters"), dict):
            result["filters"] = {}
            
    return result


def ask_llm_cart(
    message: str,
    filtered_products: list,
    selected_product: dict = None,
) -> dict:
    product_text = _build_product_text(filtered_products, query=message)

    selected_context = ""
    if selected_product:
        selected_context = (
            f"\nThe customer has already shown interest in: "
            f"{selected_product.get('title', '')}\n"
        )

    prompt = f"""
You are an AI shopping assistant for a Shopify store.
{selected_context}
Currently shown products:
{product_text}

Customer says:
{message}

Determine what the customer wants. Return ONLY valid JSON:

{{
  "intent": "<intent>",
  "product_ref": "<product handle (the [bracketed] identifier) or null>",
  "variant_ref": "<size/color/sku or null>",
  "quantity": <integer, default 1>,
  "go_to_checkout": <true if customer wants to go to checkout, false otherwise>,
  "answer": "<short conversational response>"
}}

Intent values:
- "select_variant"  – customer is specifying size, color, quantity
- "add_to_cart"     – customer says "add to cart", "buy it"
- "remove_from_cart"– customer says "remove from cart"
- "checkout"        – customer says "go to checkout"
- "confirm_yes"     – customer confirms
- "confirm_no"      – customer cancels
- "filter_products" – customer starts a new search
- "general_question"

Rules:
- "product_ref" MUST be the product's handle (the bracketed identifier shown before the title, e.g. "classic-tee"). Do NOT use the product title.
- Keep "answer" extremely short (1 sentence, max 10-12 words).
- Return ONLY JSON. No markdown code fences.
"""
    try:
        response = _call_llm_with_retry(prompt)
    except Exception as exc:
        log.log_warn("ask_llm_cart", str(exc))
        return {"intent": "general_question", "answer": "Sorry, I had trouble understanding that."}

    if not response.choices:
        return FALLBACK_CART_RESPONSE

    content = response.choices[0].message.content
    if not content:
        return FALLBACK_CART_RESPONSE

    result = _parse_json(content, FALLBACK_CART_RESPONSE)
    
    try:
        validated = CartIntentModel(**result)
        result = validated.model_dump()
    except Exception as e:
        log.log_warn("ask_llm_cart_validation", f"Pydantic validation failed: {str(e)}")
        try:
            result["quantity"] = max(1, int(result.get("quantity", 1)))
        except (TypeError, ValueError):
            result["quantity"] = 1
        allowed_intents = ["select_variant", "add_to_cart", "remove_from_cart", "checkout", "confirm_yes", "confirm_no", "filter_products", "general_question"]
        if result.get("intent") not in allowed_intents:
            result["intent"] = "general_question"
        result["go_to_checkout"] = bool(result.get("go_to_checkout"))

    # Normalise string refs — convert null-like values to None
    _null = {"", "null", "none", "n/a"}
    result["product_ref"] = str(result.get("product_ref") or "").strip().lower() or None
    if result["product_ref"] in _null:
        result["product_ref"] = None
    result["variant_ref"] = str(result.get("variant_ref") or "").strip() or None
    if result["variant_ref"] and result["variant_ref"].lower() in _null:
        result["variant_ref"] = None

    return result