import os
import requests
from dotenv import load_dotenv

load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")


def speech_to_text(audio_file_path: str) -> str:
    """
    Transcribe audio using ElevenLabs Scribe via direct REST call.
    """
    url = "https://api.elevenlabs.io/v1/speech-to-text"

    file_size = os.path.getsize(audio_file_path)
    print(f"[STT] Audio file size: {file_size} bytes, path: {audio_file_path}")

    if file_size < 1000:
        print(f"[STT] Audio too short ({file_size} bytes) — skipping transcription")
        return ""

    with open(audio_file_path, "rb") as audio_file:
        ext = os.path.splitext(audio_file_path)[1] or ".webm"
        mime = "audio/mpeg" if ext == ".mp3" else "audio/webm"

        response = requests.post(
            url,
            headers={"xi-api-key": ELEVENLABS_API_KEY},
            files={"file": (f"audio{ext}", audio_file, mime)},
            data={"model_id": "scribe_v1"},
            timeout=60,
        )

    if not response.ok:
        print(f"[STT] ElevenLabs error {response.status_code}: {response.text[:500]}")
        response.raise_for_status()

    result = response.json()
    text = result.get("text", "")
    print(f"[STT] Transcription: {text!r}")
    return text


def text_to_speech(text: str, output_path: str):

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"

    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    data = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75
        }
    }

    response = requests.post(
        url,
        headers=headers,
        json=data,
        stream=True,
        timeout=60
    )

    response.raise_for_status()

    with open(output_path, "wb") as audio_file:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                audio_file.write(chunk)

    return output_path