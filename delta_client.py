import os
from dotenv import load_dotenv
from delta_rest_client import DeltaRestClient


DEFAULT_BASE_URL = "https://api.india.delta.exchange"

load_dotenv()

API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")
BASE_URL = os.getenv("DELTA_BASE_URL", DEFAULT_BASE_URL)

if not API_KEY or not API_SECRET:
    print("⚠️ DELTA_API_KEY / DELTA_API_SECRET not found. Public endpoints will still work; private order routes will fail.")

client = DeltaRestClient(
    base_url=BASE_URL,
    api_key=API_KEY or "",
    api_secret=API_SECRET or "",
)
