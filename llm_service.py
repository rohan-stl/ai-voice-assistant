import os
import json
import time
import re
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


def _compact_product_catalog(products: list, limit: int = 40) -> str:
    """Build a token-efficient product summary for the LLM prompt."""
    lines = []
    for product in products[:limit]:
        variants = product.get("variants", {}).get("nodes", [])
        prices = [float(v.get("price", 0) or 0) for v in variants if v.get("price")]
        price_range = f"${min(prices):.0f}-${max(prices):.0f}" if prices else "N/A"
        colors_sizes = ", ".join(
            v.get("title", "") for v in variants[:4]
        )
        lines.append(
            f"- {product.get('title', '')} | type={product.get('productType', '')} "
            f"| vendor={product.get('vendor', '')} | {price_range} | variants: {colors_sizes}"
        )
    return "\n".join(lines)


def ask_llm(message: str, products: list) -> dict:
    product_text = _compact_product_catalog(products)

    prompt = f"""You are an AI shopping assistant for a Shopify store.

Customer request: {message}

Product catalog ({min(len(products), 40)} items):
{product_text}

Return ONLY valid JSON with this exact structure:
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
  "answer": "short conversational response (max 2 sentences)"
}}

Rules:
1. Shopping requests → "intent": "filter_products"
2. Off-topic questions → "intent": "general_question"
3. Use exact product titles from the catalog for "product_name"
4. Use exact product_type casing from catalog (e.g. "T-Shirt", "Snowboard")
5. "product_name" = specific product; "product_type" = category
6. Unspecified filters = null. Do NOT invent filters or products.
7. Set "vendor" only if the customer explicitly mentions a brand or vendor name.
8. Keep "answer" concise — it will be spoken aloud.
9. Return ONLY JSON, no markdown fences."""

    try:
        content = _call_llm_with_retry(prompt)
    except Exception:
        return FALLBACK_RESPONSE

    if not content:
        return FALLBACK_RESPONSE

    content = content.strip()

    # 1. Strip markdown code fences if present (e.g. ```json ... ```)
    cleaned_content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.MULTILINE)
    cleaned_content = re.sub(r"\s*```$", "", cleaned_content, flags=re.MULTILINE).strip()

    # 2. Extract JSON object using regex if the model surrounded it with text/thinking
    json_match = re.search(r"(\{.*\})", cleaned_content, re.DOTALL)
    if json_match:
        json_str = json_match.group(1).strip()
        try:
            parsed = json.loads(json_str)
            if isinstance(parsed, dict) and "answer" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

    # 3. Direct JSON load attempt
    try:
        parsed = json.loads(cleaned_content)
        if isinstance(parsed, dict) and "answer" in parsed:
            return parsed
    except json.JSONDecodeError:
        pass

    # 4. Clean fallback if parsing failed: strip all markdown/tags so TTS gets only clean text
    clean_text = re.sub(r"<(think|thought)>.*?</\1>", "", content, flags=re.DOTALL)
    clean_text = re.sub(r"<(think|thought)>.*$", "", clean_text, flags=re.DOTALL)
    clean_text = re.sub(r"```.*?```", "", clean_text, flags=re.DOTALL)
    clean_text = re.sub(r"[\{\}\[\]\"]", "", clean_text).strip()
    
    if not clean_text or len(clean_text) > 200:
        clean_text = "Here are the products matching your request."

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
        "answer": clean_text
    }
