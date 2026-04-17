import logging
import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.india.delta.exchange"
HTTP_TIMEOUT = 20


def get_live_perpetual_futures(base_url: str = DEFAULT_BASE_URL) -> list[dict]:
    """
    Fetch live perpetual futures from PUBLIC endpoint.
    No authentication needed.
    """
    try:
        with requests.Session() as session:
            session.trust_env = False
            r = session.get(f"{base_url}/v2/products", timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()

            if not data.get("success"):
                logger.error("Products API returned success=false")
                return []

            products = data.get("result", [])

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch products: {e}")
        return []
    except ValueError:
        logger.error("Non-JSON response from products endpoint")
        return []

    futures = [
        {"symbol": p["symbol"], "product_id": p["id"]}
        for p in products
        if p.get("contract_type") == "perpetual_futures"
        and p.get("state") == "live"
    ]

    logger.info(f"Live perpetual futures fetched: {len(futures)}")
    return futures


def get_market_stats(
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, dict[str, float | None]]:
    """
    Fetch volume and spread data from public tickers endpoint.
    Uses explicit None checks to avoid falsy-zero bugs.
    """
    stats: dict[str, dict[str, float | None]] = {}
    try:
        with requests.Session() as session:
            session.trust_env = False
            r = session.get(f"{base_url}/v2/tickers", timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            tickers = data.get("result", [])

    except Exception as e:
        logger.error(f"Failed to fetch tickers: {e}")
        return {}

    for t in tickers:
        symbol = t.get("symbol")
        if not symbol:
            continue

        # Explicit None checks — 0 is valid, not falsy skip
        volume = t.get("quote_volume")
        if volume is None:
            volume = t.get("turnover")
        if volume is None:
            volume = t.get("volume_usd")
        if volume is None:
            volume = t.get("volume")

        try:
            volume_val = float(volume) if volume is not None else None
        except (TypeError, ValueError):
            volume_val = None

        bid = t.get("best_bid") or t.get("bid")
        ask = t.get("best_ask") or t.get("ask")
        spread_pct = None
        try:
            if bid is not None and ask is not None and float(bid) > 0:
                spread_pct = ((float(ask) - float(bid)) / float(bid)) * 100.0
        except (TypeError, ValueError):
            spread_pct = None

        stats[symbol] = {"volume_usd": volume_val, "spread_pct": spread_pct}

    return stats


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    futures = get_live_perpetual_futures()
    print(f"Perpetual Futures Found: {len(futures)}")
    for f in futures:
        print(f)