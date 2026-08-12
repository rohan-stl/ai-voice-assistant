import os
import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("ELEVENLABS_API_KEY")

url = "https://api.elevenlabs.io/v1/text-to-speech/JBFqnCBsd6RMkjVDRZzb"

headers = {
    "xi-api-key": api_key,
    "Content-Type": "application/json",
}

payload = {
    "text": "The Collection Snowboard Hydrogen costs six hundred dollars and is currently in stock.",
    "model_id": "eleven_multilingual_v2"
}

response = requests.post(
    url,
    headers=headers,
    json=payload
)

print("Status:", response.status_code)
print("Content-Type:", response.headers.get("content-type"))

if response.ok:
    with open("test_response.mp3", "wb") as f:
        f.write(response.content)

    print("Created test_response.mp3")
else:
    print("Response:", response.text)