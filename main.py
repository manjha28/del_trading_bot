import os
import math
import time
import json
import logging
from datetime import date

import requests
import pandas as pd

from delta_client import client, is_authenticated
from scanner import get_live_perpetual_futures, get_market_stats
from strategy_short import (
    UnifiedShortConfig,
    calculate_ema, calculate_rsi, calculate_atr,
    calculate_sl_tp_prices, calculate_trade_capital,
    unified_entry_filter, detect_pump, is_pump_still_active,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("trading.log")],
)
logger = logging.getLogger(__name__)

AUTO_EXECUTE = os.getenv("AUTO_EXECUTE", "false").lower() == "true"
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "6"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
MIN_BALANCE = float(os.getenv("MIN_BALANCE_TO_TRADE", "10.0"))

CONFIG = UnifiedShortConfig(
    lookback_green_candles=4,
    min_pump_pct=50.0,
    capital_deployed=100.0,
    leverage=20.0,
    sl_capital_pct=0.05,
    max_capital_usage_pct=0.50,
    max_pnl_impact_pct=2.0,
    atr_sl_multiplier=1.5,
    pump_atr_sl_multiplier=2.5,
    max_reentries=3,
)

MAX_HOLD = 24
COOLDOWN_SECONDS = 1 * 3600
MIN_VOLUME_USD = 5_000_000
MAX_SPREAD_PCT = 0.5
ALLOWED_SYMBOLS: set[str] = set()
STATE_FILE = "trade_state.json"
BASE_URL = "https://api.india.delta.exchange"


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"trade_count": 0, "last_trade_day": "", "next_trade_allowed_ts": 0, "watching": {}}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2, default=str)


def get_balance(asset="USDT"):
    if not is_authenticated():
        return 0.0
    try:
        d = client.request("GET", "/v2/wallet/balances", auth=True).json()
        if not d.get("success"):
            return 0.0
        for w in d.get("result", []):
            a = w.get("asset_symbol") or w.get("currency") or w.get("asset") or ""
            if a.upper() == asset.upper():
                return float(w.get("available_balance") or w.get("balance") or 0)
        return 0.0
    except Exception as e:
        logger.error(f"Balance error: {e}")
        return 0.0


def fetch_candles(symbol, resolution="1h", hours=100):
    end_ts = int(time.time())
    start_ts = end_ts - (hours * 3600)
    try:
        with requests.Session() as s:
            s.trust_env = False
            r = s.get(f"{BASE_URL}/v2/history/candles",
                      params={"symbol": symbol, "resolution": resolution,
                              "start": start_ts, "end": end_ts}, timeout=20)
            r.raise_for_status()
            p = r.json()
    except Exception as e:
        logger.error(f"Candles error {symbol}: {e}")
        return pd.DataFrame()
    if not p.get("success"):
        return pd.DataFrame()
    df = pd.DataFrame(p["result"])
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.sort_values("time").reset_index(drop=True)
    for c in ["open", "high", "low", "close", "volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def place_entry(pid, qty, price):
    try:
        d = client.request("POST", "/v2/orders", payload={
            "product_id": pid, "size": qty, "side": "sell",
            "order_type": "limit_order", "limit_price": str(price),
            "time_in_force": "gtc"}, auth=True).json()
        return d if d.get("success") else None
    except Exception as e:
        logger.error(f"Entry: {e}")
        return None


def place_sl(pid, qty, sl):
    try:
        return client.place_stop_order(product_id=pid, size=qty, side="buy",
                                       order_type="market", stop_price=str(sl))
    except Exception as e:
        logger.error(f"SL: {e}")
        return None


def place_tp(pid, qty, tp):
    try:
        d = client.request("POST", "/v2/orders", payload={
            "product_id": pid, "size": qty, "side": "buy",
            "order_type": "limit_order", "limit_price": str(tp),
            "reduce_only": "true", "time_in_force": "gtc"}, auth=True).json()
        return d if d.get("success") else None
    except Exception as e:
        logger.error(f"TP: {e}")
        return None


def cancel(pid, oid):
    try:
        client.request("DELETE", f"/v2/orders/{oid}", payload={"product_id": pid}, auth=True)
        return True
    except Exception:
        return False


def market_close(pid, qty):
    try:
        return client.request("POST", "/v2/orders", payload={
            "product_id": pid, "size": qty, "side": "buy",
            "order_type": "market_order", "reduce_only": "true"}, auth=True).json()
    except Exception:
        return None


def wait_fill(pid, oid, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if oid not in [o["id"] for o in client.get_live_orders()]:
                return True
        except Exception:
            pass
        time.sleep(2)
    cancel(pid, oid)
    return False


def open_positions():
    try:
        return len([p for p in client.get_positions() if abs(float(p.get("size", 0))) > 0])
    except Exception:
        return 0


def place_brackets(pid, qty, entry, sl):
    q1 = qty // 2
    risk = sl - entry
    tp_2r = entry - 2.0 * risk
    result = {"total_qty": qty, "entry": entry, "sl": sl, "risk": risk,
              "q1": q1, "sl_id": None, "tp_id": None}
    resp = place_sl(pid, qty, sl)
    if resp:
        try:
            result["sl_id"] = resp.get("result", {}).get("id")
        except Exception:
            pass
    if q1 > 0:
        resp = place_tp(pid, q1, tp_2r)
        if resp:
            try:
                result["tp_id"] = resp.get("result", {}).get("id")
            except Exception:
                pass
    return result


def manage_trailing(pid, bracket, entry_ts):
    entry = bracket["entry"]
    risk = bracket["risk"]
    sl_id = bracket["sl_id"]
    tp_id = bracket["tp_id"]
    q1 = bracket["q1"]
    q2 = bracket["total_qty"] - q1
    partial_done = False
    best_r = 0.0
    trail_sl = bracket["sl"]
    deadline = entry_ts + MAX_HOLD * 3600

    while True:
        time.sleep(60)
        try:
            pos = client.get_position(pid)
            size = abs(float(pos.get("size", 0))) if pos else 0
        except Exception:
            size = 0
        if size == 0:
            return {"exit_reason": "CLOSED", "sl_hit": False}
        if time.time() >= deadline:
            if sl_id: cancel(pid, sl_id)
            if tp_id: cancel(pid, tp_id)
            market_close(pid, int(size))
            return {"exit_reason": "MAX_HOLD", "sl_hit": False}
        try:
            live = [o["id"] for o in client.get_live_orders()]
        except Exception:
            continue
        if not partial_done and tp_id and tp_id not in live:
            partial_done = True
            logger.info("2R partial filled!")
            if sl_id: cancel(pid, sl_id)
            if q2 > 0:
                resp = place_sl(pid, q2, trail_sl)
                if resp:
                    try: sl_id = resp.get("result", {}).get("id")
                    except: pass
        if sl_id and sl_id not in live:
            if tp_id and tp_id in live: cancel(pid, tp_id)
            return {"exit_reason": "TRAIL" if partial_done else "SL_HIT",
                    "sl_hit": not partial_done}
        try:
            with requests.Session() as sess:
                sess.trust_env = False
                r = sess.get(f"{BASE_URL}/v2/tickers", timeout=10)
                for t in r.json().get("result", []):
                    if t.get("product_id") == pid:
                        price = float(t.get("mark_price") or t.get("close"))
                        if risk > 0:
                            cur_r = (entry - price) / risk
                            best_r = max(best_r, cur_r)
                            if best_r > 1.0:
                                new_sl = entry - (best_r - 1.0) * risk
                                if new_sl < trail_sl:
                                    trail_sl = new_sl
                                    if sl_id: cancel(pid, sl_id)
                                    remain = q2 if partial_done else bracket["total_qty"]
                                    resp = place_sl(pid, remain, trail_sl)
                                    if resp:
                                        try: sl_id = resp.get("result", {}).get("id")
                                        except: pass
                        break
        except Exception:
            pass


def execute_trade(pid, sig):
    if not is_authenticated():
        return None
    logger.info(
        f"{sig.get('entry_path', 'UNKNOWN')} SHORT @ {sig['entry']:.6f} | "
        f"SL={sig['stop_loss']:.6f} | qty={sig['quantity']} | "
        f"pump={sig.get('pump_pct', 0):.1f}%"
    )
    if not AUTO_EXECUTE:
        if input("Execute? (y/n): ").strip().lower() != "y":
            return None
    resp = place_entry(pid, sig["quantity"], sig["entry"])
    if not resp:
        return None
    oid = resp.get("result", {}).get("id")
    if not oid or not wait_fill(pid, oid):
        return None
    bracket = place_brackets(pid, sig["quantity"], sig["entry"], sig["stop_loss"])
    result = manage_trailing(pid, bracket, time.time())
    logger.info(f"Closed: {result}")
    return result


def run():
    state = load_state()
    today = date.today().isoformat()
    if state.get("last_trade_day") != today:
        state["trade_count"] = 0
        state["last_trade_day"] = today

    if int(time.time()) < state.get("next_trade_allowed_ts", 0):
        return
    if open_positions() >= MAX_OPEN_TRADES:
        return
    if state["trade_count"] >= MAX_TRADES_PER_DAY:
        return

    bal = get_balance("USDT")
    if bal < MIN_BALANCE:
        return
    cap = calculate_trade_capital(bal, CONFIG)
    if cap < 5.0:
        return

    logger.info(f"Wallet: ${bal:.2f} | Trade: ${cap:.2f}")

    futures = get_live_perpetual_futures()
    if ALLOWED_SYMBOLS:
        futures = [f for f in futures if f["symbol"] in ALLOWED_SYMBOLS]

    stats = get_market_stats()
    filtered = []
    for f in futures:
        st = stats.get(f["symbol"], {})
        vol = st.get("volume_usd")
        spread = st.get("spread_pct")
        if vol is not None and vol < MIN_VOLUME_USD:
            continue
        if spread is not None and spread > MAX_SPREAD_PCT:
            continue
        filtered.append(f)

    logger.info(f"Scanning {len(filtered)} symbols...")

    watching = state.get("watching", {})

    for f in filtered:
        if state["trade_count"] >= MAX_TRADES_PER_DAY:
            break

        symbol = f["symbol"]
        candles = fetch_candles(symbol, "1h", 100)
        if candles.empty:
            continue

        prepared = candles.copy().sort_values("time").reset_index(drop=True)
        prepared["ema20"] = calculate_ema(prepared["close"], 20)
        prepared["rsi14"] = calculate_rsi(prepared["close"], 14)
        prepared["atr14"] = calculate_atr(prepared, 14)

        watch = watching.get(symbol)
        is_reentry = watch is not None and watch.get("reentries", 0) < CONFIG.max_reentries
        prev_entry = watch.get("last_entry", 0) if watch else 0

        idx = len(prepared) - 1
        filter_info = unified_entry_filter(
            prepared, idx, CONFIG,
            is_reentry=is_reentry, previous_entry_price=prev_entry
        )

        if not filter_info:
            continue

        entry_price = float(prepared.iloc[-1]["open"])
        atr = float(prepared.iloc[-1]["atr14"])
        use_pump_atr = filter_info.get("use_pump_atr", False)

        try:
            sl, tp, qty = calculate_sl_tp_prices(entry_price, CONFIG, cap, atr, use_pump_atr)
        except ValueError:
            continue

        sig = {
            "entry": entry_price, "stop_loss": sl, "take_profit": tp,
            "quantity": qty, "trade_capital": cap,
            **filter_info,
        }

        logger.info(f"{symbol} | {filter_info.get('entry_path')}: SIGNAL")
        result = execute_trade(f["product_id"], sig)

        if result:
            state["trade_count"] += 1
            state["next_trade_allowed_ts"] = int(time.time()) + COOLDOWN_SECONDS

            if result.get("sl_hit"):
                if symbol not in watching:
                    watching[symbol] = {"reentries": 0}
                watching[symbol]["reentries"] += 1
                watching[symbol]["last_entry"] = entry_price
                logger.info(f"{symbol}: SL hit — watching ({watching[symbol]['reentries']}/{CONFIG.max_reentries})")
            else:
                if symbol in watching:
                    del watching[symbol]

            state["watching"] = watching
            save_state(state)
            bal = get_balance("USDT")
            cap = calculate_trade_capital(bal, CONFIG)
            break

    # Clean expired watches
    for sym in list(watching.keys()):
        c = fetch_candles(sym, "1h", 30)
        if not c.empty:
            p = c.copy().sort_values("time").reset_index(drop=True)
            if not is_pump_still_active(p, CONFIG):
                del watching[sym]

    state["watching"] = watching
    save_state(state)


if __name__ == "__main__":
    logger.info("Starting unified MHR + Pump short trader...")
    while True:
        try:
            run()
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
        sleep_s = 3600 - (time.time() % 3600) + 5
        logger.info(f"Sleep {sleep_s:.0f}s")
        time.sleep(sleep_s)