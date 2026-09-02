import os
import subprocess
import tempfile
from abc import ABC, abstractmethod


# ---------------------------------------------------------------------------
# Shared audio normalisation utility
# ---------------------------------------------------------------------------

def _preprocess_audio(input_path: str) -> str:
    """
    Convert any browser audio format (WebM/Opus, OGG, MP4/AAC, WAV, …) to a
    clean 16 kHz mono WAV file using ffmpeg.

    This eliminates transcription errors caused by:
      - Unsupported codecs in Whisper's internal decoder
      - Stereo input confusing the VAD filter
      - Non-standard sample rates

    Returns the path to the temporary WAV file (caller must delete it).
    Returns the original path unchanged if ffmpeg is not available so the
    system degrades gracefully.
    """
    wav_path = tempfile.mktemp(suffix=".wav")
    cmd = [
        "ffmpeg",
        "-y",                   # overwrite without asking
        "-i", input_path,       # input file (any format)
        "-ac", "1",             # mono
        "-ar", "16000",         # 16 kHz — Whisper's native sample rate
        "-sample_fmt", "s16",   # 16-bit signed PCM
        "-vn",                  # drop any video stream
        "-loglevel", "error",   # suppress noisy ffmpeg stdout
        wav_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode == 0 and os.path.exists(wav_path):
            print(f"[STT] Audio preprocessed → {wav_path}")
            return wav_path
        else:
            print(f"[STT] ffmpeg preprocessing failed (rc={result.returncode}), using original file")
            if os.path.exists(wav_path):
                os.remove(wav_path)
            return input_path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("[STT] ffmpeg not available, using original file")
        return input_path


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class STTProvider(ABC):
    """Base interface for all Speech-to-Text providers."""

    @abstractmethod
    def transcribe(self, audio_file_path: str) -> str:
        """Transcribe an audio file to text."""
        pass


# ---------------------------------------------------------------------------
# Local faster-whisper (CPU, no API key required)
# ---------------------------------------------------------------------------

class LocalWhisperProvider(STTProvider):
    """
    faster-whisper running locally on CPU.

    Accuracy improvements over the original implementation:
    - Audio preprocessed to clean 16 kHz mono WAV via ffmpeg before transcription.
    - Language pinned to English to prevent misdetection.
    - VAD filter enabled — strips silence/noise segments before transcription.
    - beam_size=10 (vs default 5) — more hypothesis candidates, better accuracy.
    - best_of=5 — selects best result from 5 sample sequences.
    - initial_prompt — domain context primes the model with shopping vocabulary
      so product names like "snowboard", "T-shirt", "hoodie" are less likely to
      be misheared.
    - condition_on_previous_text=False — prevents error propagation across
      long utterances (not relevant for short queries but safer).
    - temperature=0 — greedy decoding, more deterministic output.
    - no_speech_threshold lowered to 0.4 — stricter silence detection.
    - hallucination_silence_threshold — suppresses hallucinated words on silence.
    """

    # Domain vocabulary hint: primes Whisper with words it is likely to encounter.
    # Whisper uses this as a prior, dramatically reducing mishearing of product terms.
    _INITIAL_PROMPT = (
        "Shopping assistant query. "
        "The user is searching for products like snowboards, T-shirts, hoodies, "
        "jackets, pants, accessories, shoes, hats, shorts, swimwear, bags. "
        "Prices are in dollars. Colors include red, blue, green, black, white, "
        "yellow, purple, pink, orange, grey. Sizes are XS, S, M, L, XL, XXL. "
        "Brands mentioned may include Burton, Volcom, Nike, Adidas, Oakley."
    )

    def __init__(
        self,
        model_size: str = "medium",
        device: str = "cpu",
        compute_type: str = "int8",
    ):
        from faster_whisper import WhisperModel
        print(
            f"[STT] Loading local Whisper model: "
            f"size={model_size}, device={device}, compute_type={compute_type}"
        )
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._model_size = model_size

    def transcribe(self, audio_file_path: str) -> str:
        # Check if file exists and has content
        if not audio_file_path or not os.path.exists(audio_file_path):
            print("[STT] Audio file does not exist")
            return ""

        file_size = os.path.getsize(audio_file_path)
        if file_size < 200:
            print(f"[STT] Audio file too small ({file_size} bytes), skipping transcription")
            return ""

        wav_path = _preprocess_audio(audio_file_path)
        created_wav = wav_path != audio_file_path

        try:
            segments, info = self.model.transcribe(
                wav_path,
                language="en",
                beam_size=5,                         # good accuracy/speed balance on CPU
                temperature=0.0,
                vad_filter=True,
                vad_parameters={
                    "min_silence_duration_ms": 300,
                    "speech_pad_ms": 200,
                },
                initial_prompt=self._INITIAL_PROMPT,
                condition_on_previous_text=False,
                no_speech_threshold=0.4,
                compression_ratio_threshold=2.4,
                hallucination_silence_threshold=0.5,
            )

            text = " ".join(segment.text for segment in segments).strip()

            print(
                f"[STT] Transcribed ({self._model_size}): \"{text}\" | "
                f"lang={info.language} ({info.language_probability:.0%})"
            )
            return text

        except Exception as e:
            print(f"[STT] Local Whisper transcription failed: {e}")
            return ""

        finally:
            if created_wav and os.path.exists(wav_path):
                os.remove(wav_path)


# ---------------------------------------------------------------------------
# Groq Whisper (cloud API, free tier: 28,800 audio sec / day)
# ---------------------------------------------------------------------------

class GroqWhisperProvider(STTProvider):
    """
    Groq-hosted Whisper API (whisper-large-v3-turbo).

    This is the most accurate option for this hardware because it runs
    Whisper large-v3 (full quality) on Groq's fast inference servers.
    No GPU required locally.

    Accuracy improvements:
    - Uses whisper-large-v3-turbo (much larger than local base/small).
    - language="en" pinned to English.
    - prompt parameter injects domain vocabulary for product term accuracy.
    - Audio preprocessed to 16 kHz WAV before upload to avoid codec issues.
    """

    _PROMPT = (
        "Shopping assistant. Products: snowboards, T-shirts, hoodies, jackets, "
        "pants, shoes, accessories, bags. Prices in dollars. "
        "Colors: red, blue, green, black, white, yellow, purple, pink, orange, grey. "
        "Sizes: XS, S, M, L, XL, XXL."
    )

    def __init__(self, api_key: str, model: str = "whisper-large-v3-turbo"):
        from groq import Groq
        self.client = Groq(api_key=api_key)
        self.model = model

    def transcribe(self, audio_file_path: str) -> str:
        if not audio_file_path or not os.path.exists(audio_file_path):
            print("[STT] Audio file does not exist")
            return ""

        file_size = os.path.getsize(audio_file_path)
        if file_size < 200:
            print(f"[STT] Audio file too small ({file_size} bytes), skipping transcription")
            return ""

        wav_path = _preprocess_audio(audio_file_path)
        created_wav = wav_path != audio_file_path

        try:
            print(f"[STT] Groq API transcribing with model {self.model}...")
            with open(wav_path, "rb") as audio_file:
                transcription = self.client.audio.transcriptions.create(
                    file=audio_file,
                    model=self.model,
                    language="en",
                    prompt=self._PROMPT,
                    response_format="text",
                )
            text = transcription.strip() if isinstance(transcription, str) else transcription.text.strip()
            print(f"[STT] Groq transcribed: \"{text}\"")
            return text

        except Exception as e:
            print(f"[STT] Groq Whisper transcription failed: {e}")
            return ""

        finally:
            if created_wav and os.path.exists(wav_path):
                os.remove(wav_path)


# ---------------------------------------------------------------------------
# ElevenLabs (legacy fallback, keep for backward compatibility)
# ---------------------------------------------------------------------------

class ElevenLabsSTTProvider(STTProvider):
    """ElevenLabs Scribe v2 (legacy fallback)."""

    def __init__(self, api_key: str):
        from elevenlabs.client import ElevenLabs
        self.client = ElevenLabs(api_key=api_key)

    def transcribe(self, audio_file_path: str) -> str:
        print(f"[STT] ElevenLabs Scribe transcribing {audio_file_path}")
        with open(audio_file_path, "rb") as audio_file:
            transcription = self.client.speech_to_text.convert(
                file=audio_file,
                model_id="scribe_v2",
            )
        return transcription.text
