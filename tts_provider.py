import os
import asyncio
from abc import ABC, abstractmethod

class TTSProvider(ABC):
    """Base interface for all Text-to-Speech providers."""
    
    @abstractmethod
    def synthesize(self, text: str, output_path: str) -> str:
        """Convert text to speech, write audio to output_path, and return the path."""
        pass


class EdgeTTSProvider(TTSProvider):
    """Microsoft Edge TTS (free, no API key, high quality online voices)."""
    
    def __init__(self, voice="en-US-GuyNeural"):
        self.voice = voice
    
    def synthesize(self, text: str, output_path: str) -> str:
        import edge_tts
        print(f"Edge TTS generating audio for: '{text}' using voice {self.voice}")
        
        async def _generate():
            communicate = edge_tts.Communicate(text, self.voice)
            await communicate.save(output_path)
        
        asyncio.run(_generate())
        return output_path


class PyTTSx3Provider(TTSProvider):
    """pyttsx3 Offline TTS engine (fully offline fallback)."""
    
    def __init__(self):
        # We import pyttsx3 here to avoid engine initialization overhead if not used
        import pyttsx3
        self.engine = pyttsx3.init()
        # Set male voice if available or adjust properties
        voices = self.engine.getProperty('voices')
        if len(voices) > 0:
            self.engine.setProperty('voice', voices[0].id)
    
    def synthesize(self, text: str, output_path: str) -> str:
        # pyttsx3 synthesizes audio to WAV/MP3 files depending on OS capabilities
        print(f"pyttsx3 offline TTS saving audio to {output_path}")
        self.engine.save_to_file(text, output_path)
        self.engine.runAndWait()
        return output_path


class ElevenLabsTTSProvider(TTSProvider):
    """ElevenLabs TTS (legacy fallback)."""
    
    def __init__(self, api_key: str, voice_id: str):
        self.api_key = api_key
        self.voice_id = voice_id
    
    def synthesize(self, text: str, output_path: str) -> str:
        import requests
        print(f"ElevenLabs TTS generating audio for: '{text}'")
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
        headers = {
            "xi-api-key": self.api_key,
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
        response = requests.post(url, headers=headers, json=data, stream=True, timeout=60)
        response.raise_for_status()
        with open(output_path, "wb") as audio_file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    audio_file.write(chunk)
        return output_path
