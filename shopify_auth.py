import os
import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET")
STORE_DOMAIN = os.getenv("SHOPIFY_STORE_DOMAIN")

def get_access_token():
    url = f"https://{STORE_DOMAIN}/admin/oauth/access_token"

    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }

    response = requests.post(url, json=payload)

    print("Status:", response.status_code)
    print("Response:", response.text)

    response.raise_for_status()

    return response.json()["access_token"]

if __name__ == "__main__":
    token = get_access_token()

    print("\nAccess token received successfully.")
    print("Token length:", len(token))