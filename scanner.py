from delta_client import client
import requests

def get_live_perpetual_futures():
    products = []
    # First try through authenticated/unified client
    try:
        response = client.request("GET", "/v2/products")
        data = response.json()
        if data.get("success"):
            products = data.get("result", [])
    except Exception as e:
        print(f"⚠️ Client /v2/products request failed: {e}")

    # Fallback to direct public endpoint
    if not products:
        try:
            with requests.Session() as session:
                session.trust_env = False
                r = session.get("https://api.india.delta.exchange/v2/products", timeout=20)
            r.raise_for_status()
            data = r.json()
            if data.get("success"):
                products = data.get("result", [])
        except Exception as e:
            print(f"❌ Public /v2/products fallback failed: {e}")
            return []

    futures = []
    for p in products:
        if (
            p.get("contract_type") == "perpetual_futures"
            and p.get("state") == "live"
        ):
            futures.append({
                "symbol": p["symbol"],
                "product_id": p["id"]
            })

    print(f"ℹ️ Live perpetual futures fetched: {len(futures)}")
    return futures


if __name__ == "__main__":
    futures = get_live_perpetual_futures()
    print("Perpetual Futures Found:", len(futures))

    for f in futures[:]:
        print(f)
