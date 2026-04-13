import random
import time
from datetime import date

import pandas as pd
import requests

from delta_client import client
from scanner import get_live_perpetual_futures
from strategy_short import short_signal

# ================= CONFIG ================= #

ALLOWED_SYMBOLS = {"HANAUSD"}
MAX_TRADES_PER_DAY = 10
FIXED_QTY = 1
MIN_VOLUME_USD = 5_000_000
MAX_SPREAD_PCT = 0.5
COOLDOWN_CANDLES_MIN = 3
COOLDOWN_CANDLES_MAX = 5
MAX_OPEN_TRADES = 5

trade_count = 0
last_trade_day = date.today()
next_trade_allowed_ts = 0


# ================= DATA ================= #

def get_5m_candles(symbol):
    end_ts = int(time.time())
    start_ts = end_ts - (5 * 60 * 200)

    url = "https://api.india.delta.exchange/v2/history/candles"
    params = {"symbol": symbol, "resolution": "5m", "start": start_ts, "end": end_ts}

    r = requests.get(url, params=params, timeout=10)
    if r.status_code != 200:
        return None

    data = r.json()
    if not data.get("success"):
        return None

    df = pd.DataFrame(data["result"])
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


def get_market_stats(symbol):
    try:
        r = requests.get("https://api.india.delta.exchange/v2/tickers", timeout=10)
        r.raise_for_status()
        payload = r.json()
        for t in payload.get("result", []):
            if t.get("symbol") != symbol:
                continue
            vol = t.get("quote_volume") or t.get("turnover") or t.get("volume_usd")
            bid = t.get("best_bid") or t.get("bid")
            ask = t.get("best_ask") or t.get("ask")
            spread_pct = None
            if bid and ask and float(bid) > 0:
                spread_pct = ((float(ask) - float(bid)) / float(bid)) * 100
            return {"volume_usd": float(vol) if vol is not None else None, "spread_pct": spread_pct}
    except Exception:
        return {"volume_usd": None, "spread_pct": None}

    return {"volume_usd": None, "spread_pct": None}


# ================= ORDERS ================= #

def place_short_limit_entry(product_id, qty, price):
    return client.request(
        "POST",
        "/v2/orders",
        payload={
            "product_id": product_id,
            "size": qty,
            "side": "sell",
            "order_type": "limit_order",
            "limit_price": str(price),
            "time_in_force": "gtc",
        },
        auth=True,
    )


def place_stop_loss(product_id, qty, sl_price):
    try:
        return client.place_stop_order(
            product_id=product_id,
            size=qty,
            side="buy",
            order_type="market",
            stop_price=str(sl_price),
        )
    except Exception as e:
        print("❌ SL order failed:", e)
        return None


def place_take_profit_limit(product_id, qty, tp_price):
    return client.request(
        "POST",
        "/v2/orders",
        payload={
            "product_id": product_id,
            "size": qty,
            "side": "buy",
            "order_type": "limit_order",
            "limit_price": str(tp_price),
            "reduce_only": "true",
            "time_in_force": "gtc",
        },
        auth=True,
    )


def place_mhr_style_brackets(product_id, total_qty, entry_price, sl_price):
    """
    MHR style:
    - Full SL
    - 50% TP at 1:2
    - remaining quantity intended for 1:1 trailing management.
    """
    risk = sl_price - entry_price
    tp_2r = entry_price - (2 * risk)

    partial_qty = total_qty // 2
    remainder_qty = total_qty - partial_qty

    place_stop_loss(product_id, total_qty, sl_price)
    if partial_qty > 0:
        place_take_profit_limit(product_id, partial_qty, tp_2r)

    print(f"🧩 Brackets placed | SL(full)={sl_price} TP(50%@2R)={tp_2r} remainder_for_trailing={remainder_qty}")
    print("ℹ️ Trailing for remainder follows 1:1 logic and should be managed by the trailing loop/controller.")


# ================= WAIT LOGIC ================= #

def wait_for_entry_fill(product_id, order_id, timeout=120):
    print("⏳ Waiting for entry fill...")
    start = time.time()

    while time.time() - start < timeout:
        orders = client.get_live_orders()
        open_ids = [o["id"] for o in orders]

        if order_id not in open_ids:
            print("✅ Entry filled")
            return True

        time.sleep(1)

    print("⏰ Entry not filled, cancelling")
    client.request("DELETE", f"/v2/orders/{order_id}", payload={"product_id": product_id}, auth=True)
    return False


def wait_for_position(product_id, timeout=10):
    for _ in range(timeout):
        pos = client.get_position(product_id)
        if pos and float(pos.get("size", 0)) != 0:
            return True
        time.sleep(1)
    return False


def get_open_positions_count():
    try:
        positions = client.get_positions()
        return len([p for p in positions if abs(float(p.get("size", 0))) > 0])
    except Exception:
        return 0


def calculate_short_sl(entry_price):
    return round(entry_price * 1.015, 6)


# ================= MAIN ================= #

def run():
    global trade_count, last_trade_day, next_trade_allowed_ts

    now = int(time.time())
    if now < next_trade_allowed_ts:
        wait_secs = next_trade_allowed_ts - now
        print(f"⏳ Cooldown active, wait {wait_secs}s")
        return

    today = date.today()
    if today != last_trade_day:
        trade_count = 0
        last_trade_day = today

    if get_open_positions_count() >= MAX_OPEN_TRADES:
        print(f"🛑 Max open trades reached ({MAX_OPEN_TRADES})")
        return

    futures = get_live_perpetual_futures()
    if not futures:
        print("❌ No live perpetual futures found. Check API connectivity/base URL.")
        return

    allowed_futures = [f for f in futures if f["symbol"] in ALLOWED_SYMBOLS]
    if not allowed_futures:
        print(f"⚠️ No futures from ALLOWED_SYMBOLS found. Allowed={sorted(ALLOWED_SYMBOLS)}")
        return

    for f in allowed_futures:
        symbol = f["symbol"]
        product_id = f["product_id"]

        if trade_count >= MAX_TRADES_PER_DAY:
            print("🛑 Daily trade limit reached")
            return

        m = get_market_stats(symbol)
        if m["volume_usd"] is not None and m["volume_usd"] < MIN_VOLUME_USD:
            print(f"⏭️ Skipping {symbol}: low volume {m['volume_usd']}")
            continue
        if m["spread_pct"] is not None and m["spread_pct"] > MAX_SPREAD_PCT:
            print(f"⏭️ Skipping {symbol}: spread {m['spread_pct']:.4f}% > {MAX_SPREAD_PCT}%")
            continue

        df = get_5m_candles(symbol)
        if df is None or df.empty:
            continue

        signal = short_signal(df)
        if not signal:
            continue

        print(f"\n🚨 SHORT SIGNAL | {symbol}")
        entry_price = float(signal["entry"])
        sl_price = calculate_short_sl(entry_price)

        print(f"Entry: {entry_price}")
        print(f"SL   : {sl_price}")

        confirm = input("Execute trade? (y/n): ").lower()
        if confirm != "y":
            print("❌ Trade skipped")
            return

        entry = place_short_limit_entry(product_id, FIXED_QTY, signal["entry"])
        entry_json = entry.json()

        if not entry_json.get("success"):
            print("❌ Entry rejected:", entry_json)
            return

        order_id = entry_json["result"]["id"]
        print(f"📌 LIMIT SHORT PLACED (id={order_id})")

        if not wait_for_entry_fill(product_id, order_id):
            return

        if not wait_for_position(product_id):
            print("❌ No position detected, aborting brackets")
            return

        place_mhr_style_brackets(product_id, FIXED_QTY, entry_price, sl_price)
        trade_count += 1

        cooldown = random.randint(COOLDOWN_CANDLES_MIN, COOLDOWN_CANDLES_MAX)
        next_trade_allowed_ts = int(time.time()) + (cooldown * 5 * 60)
        print(f"🧊 Cooldown set for {cooldown} candles")
        return


if __name__ == "__main__":
    run()
