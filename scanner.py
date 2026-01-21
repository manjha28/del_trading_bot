from delta_client import client

def get_live_perpetual_futures():
    response = client.request(
        "GET",
        "/v2/products"
    )

    data = response.json()        # <-- THIS WAS MISSING
    products = data["result"]

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

    return futures


if __name__ == "__main__":
    futures = get_live_perpetual_futures()
    print("Perpetual Futures Found:", len(futures))

    for f in futures[:]:
        print(f)
