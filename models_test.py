import requests

response = requests.get("https://openrouter.ai/api/v1/models")
response.raise_for_status()

models = response.json()["data"]

for model in models:
    pricing = model.get("pricing", {})

    if pricing.get("prompt") == "0" and pricing.get("completion") == "0":
        print(model["id"])