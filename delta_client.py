import os
import socket
import logging
from dotenv import load_dotenv
from delta_rest_client import DeltaRestClient

# Force IPv4 for all outbound connections so the IP whitelist on Delta Exchange
# never changes (dynamic IPv6 rotates every session; IPv4 is stable).
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.india.delta.exchange"

load_dotenv()

API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")
BASE_URL = os.getenv("DELTA_BASE_URL", DEFAULT_BASE_URL)

_AUTHENTICATED = bool(API_KEY and API_SECRET)

if not _AUTHENTICATED:
    logger.warning(
        "DELTA_API_KEY / DELTA_API_SECRET not found. "
        "Public endpoints will work; private order routes will fail."
    )

client = DeltaRestClient(
    base_url=BASE_URL,
    api_key=API_KEY or "",
    api_secret=API_SECRET or "",
)


def is_authenticated() -> bool:
    """Check if the client has valid API credentials loaded."""
    return _AUTHENTICATED