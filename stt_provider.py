import os
from abc import ABC, abstractmethod

class STTProvider(ABC):
    """Base interface for all Speech-to-Text providers."""
    
    @abstractmethod
    def transcribe(self, audio_file_path: str) -> str:
        """Transcribe an audio file to text."""
        pass


class LocalWhisperProvider(STTProvider):
    """faster-whisper running locally on CPU/GPU."""
    
    def __init__(self, model_size="base", device="cpu", compute_type="int8"):
        from faster_whisper import WhisperModel
        print(f"Loading local Whisper model: size={model_size}, device={device}, compute_type={compute_type}")
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
    
    def transcribe(self, audio_file_path: str) -> str:
        segments, info = self.model.transcribe(audio_file_path)
        text = " ".join(segment.text for segment in segments).strip()
        print(f"Local Whisper transcribing {audio_file_path}. Language: {info.language} ({info.language_probability:.2f})")
        return text


class GroqWhisperProvider(STTProvider):
    """Groq-hosted Whisper API (free tier: 28,800 audio sec/day)."""
    
    def __init__(self, api_key: str, model="whisper-large-v3-turbo"):
        from groq import Groq
        self.client = Groq(api_key=api_key)
        self.model = model
    
    def transcribe(self, audio_file_path: str) -> str:
        print(f"Groq API transcribing {audio_file_path} using model {self.model}")
        with open(audio_file_path, "rb") as audio_file:
            transcription = self.client.audio.transcriptions.create(
                file=audio_file,
                model=self.model,
            )
        return transcription.text


class ElevenLabsSTTProvider(STTProvider):
    """ElevenLabs Scribe v2 (legacy fallback)."""
    
    def __init__(self, api_key: str):
        from elevenlabs.client import ElevenLabs
        self.client = ElevenLabs(api_key=api_key)
    
    def transcribe(self, audio_file_path: str) -> str:
        print(f"ElevenLabs Scribe transcribing {audio_file_path}")
        with open(audio_file_path, "rb") as audio_file:
            transcription = self.client.speech_to_text.convert(
                file=audio_file,
                model_id="scribe_v2",
            )
        return transcription.text
