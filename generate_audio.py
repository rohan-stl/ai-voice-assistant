import os
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

load_dotenv()

client = ElevenLabs(
    api_key=os.getenv("ELEVENLABS_API_KEY")
)

text = (
    "Hi, I'm looking for a snowboard. "
    "I want something under seven hundred dollars, "
    "and it should be available in stock. "
    "Which one would you recommend?"
)

audio = client.text_to_speech.convert(
    text=text,
    voice_id="JBFqnCBsd6RMkjVDRZzb",
    model_id="eleven_multilingual_v2",
    output_format="mp3_44100_128",
)

with open("test_audio.mp3", "wb") as f:
    for chunk in audio:
        f.write(chunk)

print("Created test_audio.mp3")