import os
import requests
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

client = ElevenLabs(
    api_key=ELEVENLABS_API_KEY
)

def speech_to_text(audio_file_path: str) -> str:
    with open(audio_file_path, "rb") as audio_file:
        transcription = client.speech_to_text.convert(
            file=audio_file,
            model_id="scribe_v2",
        )

    return transcription.text


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