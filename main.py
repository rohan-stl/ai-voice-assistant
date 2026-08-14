import os
import tempfile

from fastapi import FastAPI, UploadFile, File
from pydantic import BaseModel
import os
import tempfile

from fastapi import FastAPI, UploadFile, File
from pydantic import BaseModel

from shopify_service import get_products
from llm_service import ask_llm
from voice_service import speech_to_text, text_to_speech
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

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

    answer = ask_llm(
        request.message,
        products
    )

    return {
        "answer": answer
    }


@app.post("/voice")
def voice(file: UploadFile = File(...)):
    suffix = os.path.splitext(file.filename or "")[1] or ".mp3"

    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=suffix
    ) as temp_file:

        temp_file.write(file.file.read())
        temp_file_path = temp_file.name

    output_file_path = tempfile.mktemp(suffix=".mp3")

    try:
        transcription = speech_to_text(temp_file_path)

        products = get_products()

        answer = ask_llm(
            transcription,
            products
        )

        # 4. Answer → Speech
        text_to_speech(
            answer,
            output_file_path
        )

        return FileResponse(
            output_file_path,
            media_type="audio/mpeg",
            filename="shopping_assistant_response.mp3"
        )

    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)