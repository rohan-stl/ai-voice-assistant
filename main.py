import os
import tempfile
import base64
from fastapi import FastAPI, UploadFile, File
from pydantic import BaseModel

from shopify_service import get_products, get_collections
from llm_service import ask_llm
from voice_service import speech_to_text, text_to_speech
from fastapi.middleware.cors import CORSMiddleware
from product_filter import filter_products
from storefront_url import (
    build_destination_url,
    find_product_match,
)

class AskRequest(BaseModel):
    message: str

app = FastAPI(title="AI Shopping Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    print("[API] GET / -> ok")
    return {
        "status": "ok",
        "service": "AI Shopping Assistant"
    }

@app.get("/products")
def products():
    print("[API] GET /products")
    products = get_products()
    print("[API] /products count:", len(products))
    return {
        "products": products
    }

@app.post("/ask")
def ask(request: AskRequest):
    print("[API] POST /ask")
    print("[API] /ask message:", request.message)
    products = get_products()
    collections = get_collections()
    print("[API] Products fetched:", len(products))
    print("[API] Collections fetched:", len(collections))

    result = ask_llm(
        request.message,
        products
    )
    print("[API] LLM result:", result)

    filters = result.get("filters", {})

    filtered_products = products

    if result.get("intent") in ("filter_products", "collection_request"):
        filtered_products = filter_products(
            products,
            filters
        )
    elif result.get("intent") == "product_request":
        target_query = (
            result.get("navigation_target", {}) or {}
        ).get("query", "")
        matched_product = find_product_match(products, target_query)

        if matched_product:
            filtered_products = [matched_product]
    print("[API] Filtered products count:", len(filtered_products))

    destination_url = build_destination_url(
        result,
        products,
        collections,
        filtered_products=filtered_products
    )
    print("[API] Destination URL:", destination_url)
    should_redirect = bool(destination_url) and result.get("intent") in (
        "filter_products",
        "product_request",
        "collection_request"
    )
    print("[API] Should redirect:", should_redirect)

    response_payload = {
        "answer": result.get("answer", ""),
        "intent": result.get("intent", "general_question"),
        "navigation_target": result.get("navigation_target", {}),
        "filters": filters,
        "destination_url": destination_url,
        "should_redirect": should_redirect,
        "products": filtered_products
    }
    print("[API] /ask response:", response_payload)
    return response_payload

@app.post("/voice")
def voice(
    file: UploadFile = File(...)
):
    print("[API] POST /voice")
    print("[API] Incoming file:", file.filename)
    suffix = os.path.splitext(file.filename or "")[1] or ".webm"
    temp_file_path = None
    output_file_path = None

    try:
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=suffix
        ) as temp_file:
            temp_file.write(file.file.read())
            temp_file_path = temp_file.name
            print("[API] Temporary upload saved:", temp_file_path)

        output_file_path = tempfile.mktemp(suffix=".mp3")
        print("[API] Temporary output path:", output_file_path)

        # 1. Speech → Text
        transcription = speech_to_text(temp_file_path)
        print("[API] STT transcription:", transcription)

        if not transcription or not transcription.strip():
            print("[API] Empty transcription")
            os.remove(temp_file_path)

            return {
                "answer": "I didn't hear anything. Please try again."
            }

        # 2. Get Shopify products
        products = get_products()
        collections = get_collections()
        print("[API] Products fetched:", len(products))
        print("[API] Collections fetched:", len(collections))
        for product in products:
            print(
                "[API] Product:",
                product["title"],
                [
                    v.get("title")
                    for v in product.get("variants", {}).get("nodes", [])
                ]
            )

        # 3. LLM
        result = ask_llm(
            transcription,
            products
        )

        print("[API] LLM result:", result)

        answer = result.get("answer", "")
        filters = result.get("filters", {})

        filtered_products = products

        if result.get("intent") in ("filter_products", "collection_request"):
            filtered_products = filter_products(
                products,
                filters
            )
        elif result.get("intent") == "product_request":
            target_query = (
                result.get("navigation_target", {}) or {}
            ).get("query", "")
            matched_product = find_product_match(products, target_query)

            if matched_product:
                filtered_products = [matched_product]
        print("[API] Filters:", filters)
        print("[API] Filtered product count:", len(filtered_products))
        print("[API] Filtered products:", filtered_products)

        destination_url = build_destination_url(
            result,
            products,
            collections,
            filtered_products=filtered_products
        )
        print("[API] Destination URL:", destination_url)
        should_redirect = bool(destination_url) and result.get("intent") in (
            "filter_products",
            "product_request",
            "collection_request"
        )
        print("[API] Should redirect:", should_redirect)
        print("[API] TTS started...")
        # 4. Text → Speech
        text_to_speech(
            answer,
            output_file_path
        )
        print("[API] TTS completed")
        print("[API] Audio file:", output_file_path)
        print("[API] Audio exists:", os.path.exists(output_file_path))
        print("[API] Audio size:", os.path.getsize(output_file_path))
        print("[API] Returning audio response")
        with open(output_file_path, "rb") as audio_file:
            audio_data = audio_file.read()

        audio_base64 = base64.b64encode(audio_data).decode("utf-8")

        response_payload = {
            "transcription": transcription,
            "answer": answer,
            "intent": result.get("intent", "general_question"),
            "navigation_target": result.get("navigation_target", {}),
            "filters": result.get("filters", {}),
            "storefront_url": destination_url,
            "destination_url": destination_url,
            "should_redirect": should_redirect,
            "products": filtered_products,
            "audio": audio_base64
        }
        print("[API] /voice response keys:", list(response_payload.keys()))
        return response_payload

    except Exception:
        print("[API] /voice failed with exception")
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)

        if output_file_path and os.path.exists(output_file_path):
            os.remove(output_file_path)

        raise
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)

        if output_file_path and os.path.exists(output_file_path):
            os.remove(output_file_path)

@app.post("/voice-text")
def voice_text(request: AskRequest):
    print("[API] POST /voice-text")
    print("[API] Text input:", request.message)

    # 1. Get Shopify products
    print("[API] Fetching Shopify products...")
    products = get_products()
    collections = get_collections()

    print("[API] Shopify products fetched:", len(products))
    print("[API] Shopify collections fetched:", len(collections))

    print("[API] Calling LLM...")

    # 2. LLM
    result = ask_llm(
        request.message,
        products
    )

    print("[API] LLM result:", result)

    # 3. Get answer
    answer = result.get("answer", "")

    # 4. Filter products
    filters = result.get("filters", {})

    filtered_products = products

    if result.get("intent") in ("filter_products", "collection_request"):
        filtered_products = filter_products(
            products,
            filters
        )
    elif result.get("intent") == "product_request":
        target_query = (
            result.get("navigation_target", {}) or {}
        ).get("query", "")
        matched_product = find_product_match(products, target_query)

        if matched_product:
            filtered_products = [matched_product]
    print("[API] Filtered product count:", len(filtered_products))

    print("[API] Building storefront URL...")
    storefront_url = build_destination_url(
        result,
        products,
        collections,
        filtered_products=filtered_products
    )
    print("[API] Storefront URL:", storefront_url)
    should_redirect = bool(storefront_url) and result.get("intent") in (
        "filter_products",
        "product_request",
        "collection_request"
    )
    print("[API] Should redirect:", should_redirect)

    # 5. Generate TTS
    output_file_path = tempfile.mktemp(suffix=".mp3")
    print("[API] TTS output path:", output_file_path)

    try:
        print("[API] TTS started...")

        text_to_speech(
            answer,
            output_file_path
        )

        print("[API] TTS completed")
        print("[API] Audio file:", output_file_path)
        print("[API] Audio exists:", os.path.exists(output_file_path))
        print("[API] Audio size:", os.path.getsize(output_file_path))

        # 6. Read audio
        with open(output_file_path, "rb") as audio_file:
            audio_data = audio_file.read()

        # 7. Convert audio to Base64
        audio_base64 = base64.b64encode(
            audio_data
        ).decode("utf-8")

        # 8. Return response
        response_payload = {
            "transcription": request.message,
            "answer": answer,
            "intent": result.get(
                "intent",
                "general_question"
            ),
            "navigation_target": result.get("navigation_target", {}),
            "filters": filters,
            "storefront_url": storefront_url,
            "destination_url": storefront_url,
            "should_redirect": should_redirect,
            "products": filtered_products,
            "audio": audio_base64
        }
        print("[API] /voice-text response keys:", list(response_payload.keys()))
        return response_payload

    finally:
        if os.path.exists(output_file_path):
            os.remove(output_file_path)
