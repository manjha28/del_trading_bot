import time
import requests
import pandas as pd
from scanner import get_live_perpetual_futures
from strategy_short import short_signal
from datetime import date
from delta_client import client

# ================= CONFIG ================= #

ALLOWED_SYMBOLS = {"HANAUSD"}
MAX_TRADES_PER_DAY = 10
FIXED_QTY = 1

trade_count = 0
last_trade_day = date.today()

# ================= DATA ================= #

def get_5m_candles(symbol):
    end_ts = int(time.time())
    start_ts = end_ts - (5 * 60 * 200)

    url = "https://api.india.delta.exchange/v2/history/candles"
    params = {
        "symbol": symbol,
        "resolution": "5m",
        "start": start_ts,
        "end": end_ts
    }

    r = requests.get(url, params=params, timeout=10)
    if r.status_code != 200:
        return None

    data = r.json()
    if not data.get("success"):
        return None

    df = pd.DataFrame(data["result"])
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df

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
            "time_in_force": "gtc"
        },
        auth=True
    )

def place_stop_loss(product_id, qty, sl_price):
    """
    Proper Delta stop-loss for SHORT position.
    Uses Delta's native place_stop_order.
    """
    try:
        return client.place_stop_order(
            product_id=product_id,
            size=qty,
            side="buy",                 # BUY closes SHORT
            order_type="market",        # MUST be market
            stop_price=str(sl_price),   # trigger price
        )
    except Exception as e:
        print("❌ SL order failed:", e)
        return None


def place_take_profit(product_id, qty, tp_price):
    return client.request(
        "POST",
        "/v2/orders",
        payload={
            "product_id": product_id,
            "size": qty,
            "side": "buy",                 # required by API
            "order_type": "market_order", # IMPORTANT
            "stop_price": str(tp_price),
            "reduce_only": "true"
        },
        auth=True
    )

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
    client.request(
        "DELETE",
        f"/v2/orders/{order_id}",
        payload={"product_id": product_id},
        auth=True
    )
    return False

def wait_for_position(product_id, timeout=10):
    for _ in range(timeout):
        pos = client.get_position(product_id)
        if pos and float(pos.get("size", 0)) != 0:
            return True
        time.sleep(1)
    return False

def calculate_short_sl(entry_price):
    return round(entry_price * 1.015, 6)



# ================= MAIN ================= #

def run():
    global trade_count, last_trade_day

    today = date.today()
    if today != last_trade_day:
        trade_count = 0
        last_trade_day = today

    futures = get_live_perpetual_futures()

    for f in futures:
        symbol = f["symbol"]
        product_id = f["product_id"]

        if symbol not in ALLOWED_SYMBOLS:
            continue

        if trade_count >= MAX_TRADES_PER_DAY:
            print("🛑 Daily trade limit reached")
            return

        df = get_5m_candles(symbol)
        if df is None or df.empty:
            continue

        signal = short_signal(df)
        if not signal:
            continue

        print(f"\n🚨 SHORT SIGNAL | {symbol}")
        entry_price = float(signal["entry"])
        sl_price = calculate_short_sl(entry_price)
        tp_price = float(signal["take_profit"])

        print(f"Entry: {entry_price}")
        print(f"SL   : {sl_price}")
        print(f"TP   : {tp_price}")

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
            print("❌ No position detected, aborting SL/TP")
            return

        place_stop_loss(product_id, FIXED_QTY, sl_price)
        place_take_profit(product_id, FIXED_QTY, signal["take_profit"])

        print("🛡 SL & TP attached (Delta-native)")

        trade_count += 1
        return  # ONE TRADE PER RUN

# ================= ENTRY ================= #

if __name__ == "__main__":
    run()
