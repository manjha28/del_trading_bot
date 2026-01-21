import os
from dotenv import load_dotenv
from delta_rest_client import DeltaRestClient


load_dotenv()  # ← THIS MUST RUN

API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")

print("DEBUG API KEY LOADED:", API_KEY[:6] if API_KEY else None)

client = DeltaRestClient(
    base_url="https://api.india.delta.exchange",
    api_key=API_KEY,
    api_secret=API_SECRET
)

API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")
BASE_URL = os.getenv("DELTA_BASE_URL")

if not API_KEY or not API_SECRET:
    raise Exception("API keys not found in .env")

client = DeltaRestClient(
    base_url=BASE_URL,
    api_key=API_KEY,
    api_secret=API_SECRET
)
