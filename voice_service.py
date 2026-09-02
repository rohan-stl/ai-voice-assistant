import os
from dotenv import load_dotenv
from stt_provider import LocalWhisperProvider, GroqWhisperProvider, ElevenLabsSTTProvider
from tts_provider import EdgeTTSProvider, PyTTSx3Provider, ElevenLabsTTSProvider

load_dotenv()

STT_PROVIDER_NAME = os.getenv("STT_PROVIDER", "auto")
TTS_PROVIDER_NAME = os.getenv("TTS_PROVIDER", "edge_tts")

# ---------------------------------------------------------------------------
# STT Initialization (Primary + Fallback)
# ---------------------------------------------------------------------------

_primary_stt = None
_fallback_stt = None

groq_key = os.getenv("GROQ_API_KEY", "").strip()

if STT_PROVIDER_NAME in ("auto", "groq_whisper") and groq_key:
    print("[STT] Primary STT: Groq Whisper (whisper-large-v3-turbo, ~1.5s, high accuracy)")
    _primary_stt = GroqWhisperProvider(api_key=groq_key)
    model_size = os.getenv("WHISPER_MODEL_SIZE", "small")
    print(f"[STT] Fallback STT: Local Whisper (size={model_size})")
    _fallback_stt = LocalWhisperProvider(model_size=model_size, device="cpu", compute_type="int8")
else:
    model_size = os.getenv("WHISPER_MODEL_SIZE", "small")
    print(f"[STT] Primary STT: Local Whisper (size={model_size})")
    _primary_stt = LocalWhisperProvider(model_size=model_size, device="cpu", compute_type="int8")

# ---------------------------------------------------------------------------
# TTS Initialization (Primary + Fallback)
# ---------------------------------------------------------------------------

_primary_tts = None
_fallback_tts = None

if TTS_PROVIDER_NAME == "edge_tts":
    voice = os.getenv("EDGE_TTS_VOICE", "en-US-GuyNeural")
    print(f"[TTS] Primary TTS: Edge TTS (voice={voice})")
    _primary_tts = EdgeTTSProvider(voice=voice)
    try:
        _fallback_tts = PyTTSx3Provider()
        print("[TTS] Fallback TTS: pyttsx3 (offline)")
    except Exception:
        _fallback_tts = None
elif TTS_PROVIDER_NAME == "pyttsx3":
    _primary_tts = PyTTSx3Provider()
elif TTS_PROVIDER_NAME == "elevenlabs":
    _primary_tts = ElevenLabsTTSProvider(
        api_key=os.getenv("ELEVENLABS_API_KEY", ""),
        voice_id=os.getenv("ELEVENLABS_VOICE_ID", ""),
    )
else:
    _primary_tts = EdgeTTSProvider(voice="en-US-GuyNeural")


# ---------------------------------------------------------------------------
# Public Functions
# ---------------------------------------------------------------------------

def speech_to_text(audio_file_path: str) -> str:
    """
    Convert speech audio to text.
    First tries Primary STT (Groq Whisper). If it fails or returns empty text,
    automatically falls back to Secondary STT (Local Whisper).
    """
    text = ""
    try:
        text = _primary_stt.transcribe(audio_file_path)
    except Exception as err:
        print(f"[STT] Primary provider failed: {err}")

    if not text and _fallback_stt:
        print("[STT] Falling back to Secondary STT...")
        try:
            text = _fallback_stt.transcribe(audio_file_path)
        except Exception as err:
            print(f"[STT] Fallback provider failed: {err}")

    return text.strip() if text else ""


def text_to_speech(text: str, output_path: str) -> str:
    """
    Convert text to speech audio file.
    Tries Primary TTS (Edge TTS). If it fails, falls back to pyttsx3.
    """
    text = (text or "").strip()
    if not text:
        text = "Here are the results."

    try:
        return _primary_tts.synthesize(text, output_path)
    except Exception as err:
        print(f"[TTS] Primary TTS failed: {err}")
        if _fallback_tts:
            try:
                print("[TTS] Falling back to offline pyttsx3 TTS...")
                return _fallback_tts.synthesize(text, output_path)
            except Exception as err2:
                print(f"[TTS] Fallback TTS failed: {err2}")

    return output_path
