import os
import math
import time
import json
import logging
from datetime import date, datetime, timezone

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

# ── Runtime config ────────────────────────────────────────────────────────────
AUTO_EXECUTE = os.getenv("AUTO_EXECUTE", "false").lower() == "true"
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "6"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
MIN_BALANCE = float(os.getenv("MIN_BALANCE_TO_TRADE", "10.0"))

# Hard cap on total capital deployed from the live wallet.
# Exchange auto-liquidates at 50% wallet loss ($5); guard fires at 40% ($4).
MAX_LIVE_CAPITAL = 10.0
LIQ_GUARD_USD = MAX_LIVE_CAPITAL * 0.40  # $4.00 — emergency close threshold

# ── Telegram ──────────────────────────────────────────────────────────────────
# Set these in .env:
#   TELEGRAM_BOT_TOKEN=<your bot token from @BotFather>
#   TELEGRAM_CHAT_ID=<your personal chat id — send any message to your bot,
#                    then call https://api.telegram.org/bot<TOKEN>/getUpdates>
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Strategy config ───────────────────────────────────────────────────────────
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
SCAN_INTERVAL = 3600          # 1 hour
MIN_VOLUME_USD = 100_000
MAX_SPREAD_PCT = 0.5
STATE_FILE = "trade_state.json"
BASE_URL = "https://api.india.delta.exchange"


# ── Telegram helper ───────────────────────────────────────────────────────────

def send_telegram(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        with requests.Session() as s:
            s.trust_env = False
            s.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")


# ── State persistence ─────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {
            "trade_count": 0,
            "last_trade_day": "",
            "next_trade_allowed_ts": 0,
            "watching": {},
            "last_signal_candles": {},   # symbol → last candle timestamp that fired a signal
        }


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2, default=str)


# ── Exchange helpers ──────────────────────────────────────────────────────────

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


# ── Trade management ──────────────────────────────────────────────────────────

def manage_trailing(pid, symbol, bracket, entry_ts, liq_guard_usd=LIQ_GUARD_USD):
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
            logger.info(f"{symbol}: 2R partial filled!")
            send_telegram(
                f"<b>PARTIAL TP HIT</b> — {symbol}\n"
                f"50% closed at 2R\n"
                f"Trailing remainder with SL at breakeven"
            )
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

                        # Liquidation guard: close immediately if unrealized loss
                        # approaches the exchange's 50%-wallet auto-liquidation level.
                        unrealized_loss = max(0.0, (price - entry) * size)
                        if unrealized_loss >= liq_guard_usd:
                            logger.warning(
                                f"LIQUIDATION GUARD {symbol}: loss ${unrealized_loss:.2f} "
                                f">= guard ${liq_guard_usd:.2f} — emergency close"
                            )
                            send_telegram(
                                f"<b>LIQUIDATION GUARD TRIGGERED</b>\n"
                                f"Symbol: {symbol}\n"
                                f"Unrealized loss: ${unrealized_loss:.4f} >= guard ${liq_guard_usd:.2f}\n"
                                f"Emergency market close sent!"
                            )
                            if sl_id: cancel(pid, sl_id)
                            if tp_id: cancel(pid, tp_id)
                            market_close(pid, int(size))
                            return {"exit_reason": "LIQ_GUARD", "sl_hit": False}

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


def execute_trade(pid, symbol, sig):
    if not is_authenticated():
        return None
    logger.info(
        f"{symbol} {sig.get('entry_path', 'UNKNOWN')} SHORT @ {sig['entry']:.6f} | "
        f"SL={sig['stop_loss']:.6f} ({sig.get('sl_method','?')}) | qty={sig['quantity']}"
    )

    # Alert: signal about to be executed
    send_telegram(
        f"<b>SIGNAL — MHR_EXHAUSTION SHORT</b>\n"
        f"Symbol: <b>{symbol}</b>\n"
        f"Entry: {sig['entry']:.6f}\n"
        f"SL: {sig['stop_loss']:.6f} ({sig.get('sl_method', '?')})\n"
        f"TP: {sig['take_profit']:.6f}\n"
        f"Qty: {sig['quantity']} | Cap: ${sig['trade_capital']:.2f}\n"
        f"4 consecutive green candles — pump exhaustion short"
    )

    if not AUTO_EXECUTE:
        if input("Execute? (y/n): ").strip().lower() != "y":
            return None

    resp = place_entry(pid, sig["quantity"], sig["entry"])
    if not resp:
        send_telegram(f"<b>ENTRY FAILED</b> — {symbol}\nOrder placement returned no response.")
        return None
    oid = resp.get("result", {}).get("id")
    if not oid or not wait_fill(pid, oid):
        send_telegram(f"<b>ENTRY NOT FILLED</b> — {symbol}\nOrder cancelled after 120s timeout.")
        return None

    bracket = place_brackets(pid, sig["quantity"], sig["entry"], sig["stop_loss"])
    send_telegram(
        f"<b>TRADE OPENED</b> — {symbol}\n"
        f"Entry filled @ {sig['entry']:.6f}\n"
        f"SL @ {sig['stop_loss']:.6f} | TP @ {bracket['entry'] - 2*bracket['risk']:.6f}\n"
        f"Qty: {sig['quantity']} | Risk: ${bracket['risk'] * sig['quantity']:.4f}"
    )

    result = manage_trailing(pid, symbol, bracket, time.time())
    logger.info(f"{symbol} closed: {result}")

    exit_reason = result.get("exit_reason", "UNKNOWN")
    send_telegram(
        f"<b>TRADE CLOSED</b> — {symbol}\n"
        f"Exit: {exit_reason}\n"
        f"{'SL hit — watching for reentry' if result.get('sl_hit') else 'Position closed'}"
    )
    return result


# ── Main scan loop ────────────────────────────────────────────────────────────

def run():
    state = load_state()
    today = date.today().isoformat()
    if state.get("last_trade_day") != today:
        state["trade_count"] = 0
        state["last_trade_day"] = today
        state["last_signal_candles"] = {}

    cooldown_remaining = state.get("next_trade_allowed_ts", 0) - int(time.time())
    at_max_positions = open_positions() >= MAX_OPEN_TRADES
    at_daily_limit = state["trade_count"] >= MAX_TRADES_PER_DAY

    bal = get_balance("USDT")
    cap = min(calculate_trade_capital(bal, CONFIG), MAX_LIVE_CAPITAL)

    can_trade = (
        cooldown_remaining <= 0
        and not at_max_positions
        and not at_daily_limit
        and bal >= MIN_BALANCE
        and cap >= 1.0
    )

    logger.info(
        f"Scan start | wallet=${bal:.2f} cap=${cap:.2f} "
        f"trades={state['trade_count']}/{MAX_TRADES_PER_DAY} "
        f"{'[COOLDOWN]' if cooldown_remaining > 0 else ''}"
        f"{'[MAX_POS]' if at_max_positions else ''}"
    )

    futures = get_live_perpetual_futures()
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
    last_signal_candles = state.get("last_signal_candles", {})
    signals_found = []

    for f in filtered:
        symbol = f["symbol"]
        candles = fetch_candles(symbol, "1h", 100)
        if candles.empty:
            continue

        prepared = candles.copy().sort_values("time").reset_index(drop=True)
        prepared["ema20"] = calculate_ema(prepared["close"], 20)
        prepared["rsi14"] = calculate_rsi(prepared["close"], 14)
        prepared["atr14"] = calculate_atr(prepared, 14)

        # Duplicate-candle guard: don't re-fire on the same 1h candle we already acted on.
        last_candle_ts = str(prepared.iloc[-1]["time"])
        if last_signal_candles.get(symbol) == last_candle_ts:
            continue

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
        if filter_info.get("entry_path") != "MHR_EXHAUSTION":
            continue

        # Mark this candle as seen for this symbol regardless of whether we trade.
        last_signal_candles[symbol] = last_candle_ts

        entry_price = float(prepared.iloc[-1]["close"])
        atr = float(prepared.iloc[-1]["atr14"]) if "atr14" in prepared.columns else 0.0
        use_pump_atr = filter_info.get("use_pump_atr", False)

        # Structural SL: SL at prev candle high (no buffer), qty from max loss / sl_dist.
        # Falls back to ATR if prev_high <= entry_price (gap-up) or qty resolves to 0.
        prev_high = float(prepared.iloc[-2]["high"]) if len(prepared) >= 2 else 0.0
        sl = tp = 0.0
        qty = 0
        sl_method = "atr"

        if prev_high > entry_price:
            _max_loss = cap * CONFIG.sl_capital_pct
            _raw = math.floor(_max_loss / (prev_high - entry_price))
            if _raw >= 1:
                _cap_qty = math.floor(cap * CONFIG.leverage / entry_price)
                _qty = min(_raw, _cap_qty)
                if _qty >= 1:
                    sl = prev_high
                    qty = _qty
                    sl_method = "structural"
                    _tp_target = max(cap * CONFIG.pt_capital_pct, _max_loss * CONFIG.min_rr)
                    tp = entry_price - _tp_target / qty

        if sl_method == "atr":
            try:
                sl, tp, qty = calculate_sl_tp_prices(entry_price, CONFIG, cap, atr, use_pump_atr)
            except ValueError:
                continue

        signals_found.append(symbol)

        if not can_trade or state["trade_count"] >= MAX_TRADES_PER_DAY:
            logger.info(f"{symbol}: signal found but cannot trade now")
            continue

        sig = {
            "entry": entry_price, "stop_loss": sl, "take_profit": tp,
            "quantity": qty, "trade_capital": cap, "sl_method": sl_method,
            **filter_info,
        }
        logger.info(
            f"{symbol} | MHR_EXHAUSTION | SL={sl_method} | "
            f"entry={entry_price:.6f} sl={sl:.6f} qty={qty} cap=${cap:.2f}"
        )

        result = execute_trade(f["product_id"], symbol, sig)

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
            state["last_signal_candles"] = last_signal_candles
            save_state(state)
            bal = get_balance("USDT")
            cap = min(calculate_trade_capital(bal, CONFIG), MAX_LIVE_CAPITAL)
            can_trade = (
                state.get("next_trade_allowed_ts", 0) <= int(time.time())
                and open_positions() < MAX_OPEN_TRADES
                and state["trade_count"] < MAX_TRADES_PER_DAY
                and bal >= MIN_BALANCE
                and cap >= 1.0
            )

    # ── Scan complete: send Telegram summary ─────────────────────────────────
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    status_parts = []
    if cooldown_remaining > 0:
        status_parts.append(f"Cooldown {cooldown_remaining//60}m remaining")
    if at_max_positions:
        status_parts.append("Max positions open")
    if at_daily_limit:
        status_parts.append("Daily trade limit reached")
    status_str = " | ".join(status_parts) if status_parts else "Ready to trade"

    summary_msg = (
        f"<b>Scan complete</b> — {now_str}\n"
        f"Scanned: {len(filtered)} symbols\n"
        f"Signals found: {len(signals_found)}"
        + (f" ({', '.join(signals_found)})" if signals_found else "")
        + f"\nWallet: ${bal:.2f} | Trades today: {state['trade_count']}/{MAX_TRADES_PER_DAY}\n"
        f"Status: {status_str}"
    )
    send_telegram(summary_msg)
    logger.info(f"Scan complete — {len(filtered)} symbols, {len(signals_found)} signals: {signals_found}")

    # Clean expired watches
    for sym in list(watching.keys()):
        c = fetch_candles(sym, "1h", 30)
        if not c.empty:
            p = c.copy().sort_values("time").reset_index(drop=True)
            if not is_pump_still_active(p, CONFIG):
                del watching[sym]

    state["watching"] = watching
    state["last_signal_candles"] = last_signal_candles
    save_state(state)


if __name__ == "__main__":
    logger.info("Starting MHR pump short trader (30-min scan)...")
    send_telegram(
        "<b>Bot started</b>\n"
        f"Strategy: MHR_EXHAUSTION SHORT\n"
        f"Scan interval: every 30 minutes\n"
        f"Capital cap: ${MAX_LIVE_CAPITAL:.2f} | Liq guard: ${LIQ_GUARD_USD:.2f}"
    )
    while True:
        try:
            run()
        except KeyboardInterrupt:
            send_telegram("<b>Bot stopped</b> — KeyboardInterrupt")
            break
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            send_telegram(f"<b>Bot error</b>\n{type(e).__name__}: {e}")
        sleep_s = SCAN_INTERVAL - (time.time() % SCAN_INTERVAL) + 5
        logger.info(f"Next scan in {sleep_s:.0f}s")
        time.sleep(sleep_s)
