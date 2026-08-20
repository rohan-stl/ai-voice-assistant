import os
import tempfile
import base64
from fastapi import FastAPI, UploadFile, File
from pydantic import BaseModel

from shopify_service import get_products
from llm_service import ask_llm
from voice_service import speech_to_text, text_to_speech
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from product_filter import filter_products, normalize_filters
from storefront_url import build_storefront_url

from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.responses import FileResponse

class AskRequest(BaseModel):
    message: str

app = FastAPI(title="AI Shopping Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "https://bhavin-ai-shopping-assistant-demo.myshopify.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "AI Shopping Assistant"
    }

@app.get("/products")
def products():
    return {
        "products": get_products()
    }

@app.post("/ask")
def ask(request: AskRequest):
    products = get_products()

    result = ask_llm(
        request.message,
        products
    )

    filters = result.get("filters", {})

    filtered_products = products

    if result.get("intent") == "filter_products":
        filtered_products = filter_products(
            products,
            filters
        )

    return {
        "answer": result.get("answer", ""),
        "intent": result.get("intent", "general_question"),
        "filters": filters,
        "products": filtered_products
    }

@app.post("/voice")
def voice(
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = None
):
    suffix = os.path.splitext(file.filename or "")[1] or ".webm"

    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=suffix
    ) as temp_file:

        temp_file.write(file.file.read())
        temp_file_path = temp_file.name

    output_file_path = tempfile.mktemp(suffix=".mp3")

    try:
        # 1. Speech → Text
        transcription = speech_to_text(temp_file_path)
        print("Transcription:", transcription)

        if not transcription or not transcription.strip():
            os.remove(temp_file_path)

            return {
                "answer": "I didn't hear anything. Please try again."
            }

        # 2. Get Shopify products
        products = get_products()
        for product in products:
            print(
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

        print("LLM result:", result)

        answer = result.get("answer", "")
        print("TTS started...")
        # 4. Text → Speech
        text_to_speech(
            answer,
            output_file_path
        )
        print("TTS completed")
        print("Audio file:", output_file_path)
        print("Audio exists:", os.path.exists(output_file_path))
        print("Audio size:", os.path.getsize(output_file_path))
        # 5. Delete files after response is complete
        background_tasks.add_task(
            os.remove,
            temp_file_path
        )

        background_tasks.add_task(
            os.remove,
            output_file_path
        )
        print("Returning audio response...")
        with open(output_file_path, "rb") as audio_file:
            audio_data = audio_file.read()

        audio_base64 = base64.b64encode(audio_data).decode("utf-8")

        filters = result.get("filters", {})

        storefront_url = build_storefront_url(
            normalize_filters(filters, products)
        )

        filtered_products = products

        if result.get("intent") == "filter_products":
            filtered_products = filter_products(
                products,
                filters
            )
        print("Filters:", filters)
        print("Filtered product count:", len(filtered_products))
        print("Filtered products:", filtered_products)
        return {
            "transcription": transcription,
            "answer": answer,
            "intent": result.get("intent", "general_question"),
            "filters": result.get("filters", {}),
            "storefront_url": storefront_url,
            "products": filtered_products,
            "audio": audio_base64
        }

    except Exception:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)

        if os.path.exists(output_file_path):
            os.remove(output_file_path)

        raise

@app.post("/voice-text")
def voice_text(request: AskRequest):
    print("Text input:", request.message)

    # 1. Get Shopify products
    print("Text input:", request.message)

    print("Fetching Shopify products...")
    products = get_products()

    print("Shopify products fetched:", len(products))

    print("Calling LLM...")

    # 2. LLM
    result = ask_llm(
        request.message,
        products
    )

    print("LLM result:", result)

    # 3. Get answer
    answer = result.get("answer", "")

    # 4. Filter products
    filters = result.get("filters", {})
    print("LLM result:", result)

    print("Building storefront URL...")
    storefront_url = build_storefront_url(
        normalize_filters(filters, products)
    )
    print("Storefront URL:", storefront_url)

    filtered_products = products

    if result.get("intent") == "filter_products":
        filtered_products = filter_products(
            products,
            filters
        )

    # 5. Generate TTS
    output_file_path = tempfile.mktemp(suffix=".mp3")

    try:
        print("TTS started...")

        text_to_speech(
            answer,
            output_file_path
        )

        print("TTS completed")
        print("Audio file:", output_file_path)
        print("Audio exists:", os.path.exists(output_file_path))
        print("Audio size:", os.path.getsize(output_file_path))

        # 6. Read audio
        with open(output_file_path, "rb") as audio_file:
            audio_data = audio_file.read()

        # 7. Convert audio to Base64
        audio_base64 = base64.b64encode(
            audio_data
        ).decode("utf-8")

        # 8. Return response
        return {
            "transcription": request.message,
            "answer": answer,
            "intent": result.get(
                "intent",
                "general_question"
            ),
            "filters": filters,
            "storefront_url": storefront_url,
            "products": filtered_products,
            "audio": audio_base64
        }

    finally:
        if os.path.exists(output_file_path):
            os.remove(output_file_path)