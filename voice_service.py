import os
import requests
from dotenv import load_dotenv
import logger as log

load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")


def speech_to_text(audio_file_path: str) -> str:
    """
    Transcribe audio using ElevenLabs Scribe via direct REST call.
    Returns empty string (never raises) so callers can gracefully fall back.
    """
    url = "https://api.elevenlabs.io/v1/speech-to-text"

    if not ELEVENLABS_API_KEY:
        log.log_warn("STT", "No ELEVENLABS_API_KEY — skipping server-side STT")
        return ""

    file_size = os.path.getsize(audio_file_path)

    # Allow very short clips (single words can be < 200 bytes in webm)
    if file_size < 200:
        log.log_warn("STT", f"Audio too short ({file_size} bytes) — skipping")
        return ""

    try:
        with open(audio_file_path, "rb") as audio_file:
            ext = os.path.splitext(audio_file_path)[1] or ".webm"
            mime = "audio/mpeg" if ext == ".mp3" else "audio/webm"

            response = requests.post(
                url,
                headers={"xi-api-key": ELEVENLABS_API_KEY},
                files={"file": (f"audio{ext}", audio_file, mime)},
                data={
                    "model_id": "scribe_v1",
                    "language_code": "en",       # Hint language → better short-word accuracy
                    "tag_audio_events": "false",  # Keep transcript clean
                },
                timeout=20,
            )

        if response.status_code in (401, 403):
            # Quota exhausted or invalid key — warn but don't crash
            try:
                detail = response.json().get("detail", {})
                code = detail.get("code", "unauthorized") if isinstance(detail, dict) else "unauthorized"
            except Exception:
                code = "unauthorized"
            log.log_warn("STT", f"ElevenLabs {response.status_code} ({code}) — quota likely exhausted, signalling client to use browser STT")
            return ""

        if not response.ok:
            log.log_warn("STT", f"ElevenLabs {response.status_code}: {response.text[:200]}")
            return ""

        result = response.json()
        text = result.get("text", "").strip()
        return text

    except requests.exceptions.Timeout:
        log.log_warn("STT", "ElevenLabs STT timed out")
        return ""
    except Exception as exc:
        log.log_warn("STT", f"ElevenLabs STT error: {exc}")
        return ""


def text_to_speech(text: str, output_path: str):
    """
    Convert text to speech using ElevenLabs TTS.
    Raises on error (callers already catch and skip audio gracefully).
    """
    if not ELEVENLABS_API_KEY or not VOICE_ID:
        raise ValueError("ElevenLabs credentials not configured")

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
        timeout=15,
    )

    response.raise_for_status()

    with open(output_path, "wb") as audio_file:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                audio_file.write(chunk)

    return output_path