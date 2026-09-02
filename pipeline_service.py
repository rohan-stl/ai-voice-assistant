"""
Unified voice/text shopping pipeline.

Flow: STT (voice only) → Shopify fetch ∥ STT → LLM → filter → TTS
"""
import base64
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from shopify_service import get_products, get_collections
from llm_service import ask_llm
from voice_service import speech_to_text, text_to_speech
from product_filter import filter_products
from storefront_url import build_destination_url, find_product_match

_DEBUG_LOG = os.path.join(
    os.path.dirname(__file__), ".cursor", "debug-1b7523.log"
)

_EMPTY_FILTERS = {
    "product_name": None,
    "product_type": None,
    "color": None,
    "size": None,
    "vendor": None,
    "max_price": None,
    "min_price": None,
}


def _log(stage: str, data: dict, hypothesis_id: str = "pipeline"):
    entry = {
        "sessionId": "1b7523",
        "runId": "pipeline",
        "hypothesisId": hypothesis_id,
        "location": f"pipeline_service.py:{stage}",
        "message": stage,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        os.makedirs(os.path.dirname(_DEBUG_LOG), exist_ok=True)
        with open(_DEBUG_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _build_dynamic_query(filters: dict) -> str | None:
    parts = []
    if filters.get("vendor"):
        parts.append(f'vendor:"{filters["vendor"]}"')
    if filters.get("product_type"):
        parts.append(f'product_type:"{filters["product_type"]}"')
    if filters.get("product_name"):
        parts.append(f'title:"{filters["product_name"]}"')
    return " OR ".join(parts) if parts else None


def _apply_filters(result: dict, products: list) -> list:
    intent = result.get("intent", "general_question")
    filters = result.get("filters") or {}
    filtered = products

    if intent in ("filter_products", "collection_request"):
        filtered = filter_products(products, filters)
        if not filtered:
            dynamic_query = _build_dynamic_query(filters)
            if dynamic_query:
                dynamic_products = get_products(first=50, query_str=dynamic_query)
                if dynamic_products:
                    filtered = filter_products(dynamic_products, filters) or dynamic_products

    elif intent == "product_request":
        target_query = (result.get("navigation_target") or {}).get("query", "")
        matched = find_product_match(products, target_query)
        if matched:
            filtered = [matched]
        elif target_query:
            dynamic_products = get_products(first=20, query_str=f'title:"{target_query}"')
            if dynamic_products:
                filtered = dynamic_products

    return filtered


def _synthesize_audio(answer: str) -> tuple[str, str]:
    """Return (audio_base64, output_path). output_path is temp — caller must delete."""
    text = (answer or "").strip()
    if not text:
        return "", ""

    # Keep TTS fast — long answers slow synthesis noticeably
    if len(text) > 400:
        text = text[:397] + "..."

    output_path = tempfile.mktemp(suffix=".mp3")
    text_to_speech(text, output_path)

    if not os.path.exists(output_path) or os.path.getsize(output_path) < 100:
        if os.path.exists(output_path):
            os.remove(output_path)
        return "", ""

    with open(output_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")
    return audio_b64, output_path


def _fetch_shopify_data():
    with ThreadPoolExecutor(max_workers=2) as pool:
        products_future = pool.submit(get_products, 50)
        collections_future = pool.submit(get_collections)
        return products_future.result(), collections_future.result()


def process_text_request(message: str) -> dict:
    t0 = time.perf_counter()
    message = (message or "").strip()
    if not message:
        return _error_response("Please type or speak a product request.")

    products, collections = _fetch_shopify_data()
    _log("shopify_fetched", {"ms": round((time.perf_counter() - t0) * 1000), "count": len(products)})

    t1 = time.perf_counter()
    result = ask_llm(message, products)
    _log("llm_done", {"ms": round((time.perf_counter() - t1) * 1000), "intent": result.get("intent")})

    answer = result.get("answer", "")
    filtered_products = _apply_filters(result, products)

    destination_url = build_destination_url(
        result, products, collections, filtered_products=filtered_products
    )
    _log("url_built", {
        "filtered_count": len(filtered_products),
        "titles": [p.get("title") for p in filtered_products[:10]],
        "storefront_url": destination_url,
        "filters": result.get("filters"),
    }, hypothesis_id="url-align")
    should_redirect = bool(destination_url) and result.get("intent") in (
        "filter_products", "product_request", "collection_request"
    )

    audio_b64, audio_path = _synthesize_audio(answer)
    if audio_path and os.path.exists(audio_path):
        os.remove(audio_path)

    _log("pipeline_complete", {
        "total_ms": round((time.perf_counter() - t0) * 1000),
        "products": len(filtered_products),
        "has_audio": bool(audio_b64),
    })

    return {
        "transcription": message,
        "answer": answer,
        "intent": result.get("intent", "general_question"),
        "navigation_target": result.get("navigation_target", {}),
        "filters": result.get("filters", _EMPTY_FILTERS),
        "storefront_url": destination_url,
        "destination_url": destination_url,
        "should_redirect": should_redirect,
        "products": filtered_products,
        "audio": audio_b64,
    }


def process_voice_request(audio_file_path: str) -> dict:
    t0 = time.perf_counter()

    # Run STT and Shopify fetch in parallel — saves 1-3 s per request
    with ThreadPoolExecutor(max_workers=2) as pool:
        stt_future = pool.submit(speech_to_text, audio_file_path)
        shopify_future = pool.submit(_fetch_shopify_data)
        transcription = stt_future.result()
        products, collections = shopify_future.result()

    stt_ms = round((time.perf_counter() - t0) * 1000)
    _log("stt_done", {"ms": stt_ms, "text_len": len(transcription or ""), "products": len(products)})

    if not transcription or not transcription.strip():
        return _error_response("I didn't hear anything. Please try again.")

    t1 = time.perf_counter()
    result = ask_llm(transcription, products)
    _log("llm_done", {"ms": round((time.perf_counter() - t1) * 1000), "intent": result.get("intent")})

    answer = result.get("answer", "")
    filtered_products = _apply_filters(result, products)

    destination_url = build_destination_url(
        result, products, collections, filtered_products=filtered_products
    )
    _log("url_built", {
        "filtered_count": len(filtered_products),
        "titles": [p.get("title") for p in filtered_products[:10]],
        "storefront_url": destination_url,
        "filters": result.get("filters"),
    }, hypothesis_id="url-align")
    should_redirect = bool(destination_url) and result.get("intent") in (
        "filter_products", "product_request", "collection_request"
    )

    audio_b64, audio_path = _synthesize_audio(answer)
    if audio_path and os.path.exists(audio_path):
        os.remove(audio_path)

    _log("pipeline_complete", {
        "total_ms": round((time.perf_counter() - t0) * 1000),
        "stt_ms": stt_ms,
        "products": len(filtered_products),
        "has_audio": bool(audio_b64),
    })

    return {
        "transcription": transcription,
        "answer": answer,
        "intent": result.get("intent", "general_question"),
        "navigation_target": result.get("navigation_target", {}),
        "filters": result.get("filters", _EMPTY_FILTERS),
        "storefront_url": destination_url,
        "destination_url": destination_url,
        "should_redirect": should_redirect,
        "products": filtered_products,
        "audio": audio_b64,
    }


def _error_response(message: str) -> dict:
    audio_b64, audio_path = _synthesize_audio(message)
    if audio_path and os.path.exists(audio_path):
        os.remove(audio_path)
    return {
        "transcription": "",
        "answer": message,
        "intent": "general_question",
        "navigation_target": {},
        "filters": dict(_EMPTY_FILTERS),
        "storefront_url": None,
        "destination_url": None,
        "should_redirect": False,
        "products": [],
        "audio": audio_b64,
    }
