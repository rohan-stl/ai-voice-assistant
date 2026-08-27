import os
from dotenv import load_dotenv
from stt_provider import LocalWhisperProvider, GroqWhisperProvider, ElevenLabsSTTProvider
from tts_provider import EdgeTTSProvider, PyTTSx3Provider, ElevenLabsTTSProvider

load_dotenv()

STT_PROVIDER_NAME = os.getenv("STT_PROVIDER", "local_whisper")
TTS_PROVIDER_NAME = os.getenv("TTS_PROVIDER", "edge_tts")


def _create_stt_provider():
    if STT_PROVIDER_NAME == "local_whisper":
        model_size = os.getenv("WHISPER_MODEL_SIZE", "base")
        return LocalWhisperProvider(model_size=model_size, device="cpu", compute_type="int8")
    elif STT_PROVIDER_NAME == "groq_whisper":
        return GroqWhisperProvider(api_key=os.getenv("GROQ_API_KEY", ""))
    elif STT_PROVIDER_NAME == "elevenlabs":
        return ElevenLabsSTTProvider(api_key=os.getenv("ELEVENLABS_API_KEY", ""))
    else:
        raise ValueError(f"Unknown STT provider: {STT_PROVIDER_NAME}")


def _create_tts_provider():
    if TTS_PROVIDER_NAME == "edge_tts":
        voice = os.getenv("EDGE_TTS_VOICE", "en-US-GuyNeural")
        return EdgeTTSProvider(voice=voice)
    elif TTS_PROVIDER_NAME == "pyttsx3":
        return PyTTSx3Provider()
    elif TTS_PROVIDER_NAME == "elevenlabs":
        return ElevenLabsTTSProvider(
            api_key=os.getenv("ELEVENLABS_API_KEY", ""),
            voice_id=os.getenv("ELEVENLABS_VOICE_ID", ""),
        )
    else:
        raise ValueError(f"Unknown TTS provider: {TTS_PROVIDER_NAME}")


# Singletons initialized once at module load
_stt = _create_stt_provider()
_tts = _create_tts_provider()


def speech_to_text(audio_file_path: str) -> str:
    """Convert speech audio to text transcription."""
    return _stt.transcribe(audio_file_path)


def text_to_speech(text: str, output_path: str) -> str:
    """Convert text to speech, save at output_path, and return path."""
    return _tts.synthesize(text, output_path)
