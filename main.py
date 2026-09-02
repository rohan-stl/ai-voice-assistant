import os
import tempfile
from fastapi import FastAPI, UploadFile, File
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

from shopify_service import get_products
from pipeline_service import process_voice_request, process_text_request, _apply_filters, _fetch_shopify_data
from llm_service import ask_llm
from storefront_url import build_destination_url


class AskRequest(BaseModel):
    message: str

app = FastAPI(title="AI Shopping Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://bhavin-ai-shopping-assistant-demo.myshopify.com",
        "http://localhost:3000",
        "http://localhost:3001",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"status": "ok", "service": "AI Shopping Assistant"}


@app.get("/products")
def products():
    products_list = get_products(first=100)
    return {"products": products_list}


@app.post("/ask")
def ask(request: AskRequest):
    print("[API] POST /ask:", request.message)
    products_list, collections = _fetch_shopify_data()
    result = ask_llm(request.message, products_list)
    filtered_products = _apply_filters(result, products_list)
    destination_url = build_destination_url(
        result, products_list, collections, filtered_products=filtered_products
    )
    should_redirect = bool(destination_url) and result.get("intent") in (
        "filter_products", "product_request", "collection_request"
    )
    return {
        "answer": result.get("answer", ""),
        "intent": result.get("intent", "general_question"),
        "navigation_target": result.get("navigation_target", {}),
        "filters": result.get("filters", {}),
        "destination_url": destination_url,
        "should_redirect": should_redirect,
        "products": filtered_products,
    }


@app.post("/voice")
def voice(file: UploadFile = File(...)):
    print("[API] POST /voice file:", file.filename)
    suffix = os.path.splitext(file.filename or "")[1] or ".webm"
    temp_file_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(file.file.read())
            temp_file_path = temp_file.name

        return process_voice_request(temp_file_path)

    except Exception as exc:
        import traceback
        print(f"[API] /voice error: {exc}")
        traceback.print_exc()
        from pipeline_service import _error_response
        return _error_response("Sorry, I couldn't process your voice input. Please try again.")

    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)


@app.post("/voice-text")
def voice_text(request: AskRequest):
    print("[API] POST /voice-text:", request.message)
    return process_text_request(request.message)
