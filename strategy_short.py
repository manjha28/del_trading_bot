import math
import logging
from bisect import bisect_right
from dataclasses import dataclass
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# COMMON INDICATORS
# ═══════════════════════════════════════════════════════════════════════

def calculate_ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rsi = pd.Series(50.0, index=series.index, dtype=float)
    both_valid = (gain > 0) & (loss > 0)
    rsi[both_valid] = 100.0 - (100.0 / (1.0 + gain[both_valid] / loss[both_valid]))
    rsi[(gain > 0) & (loss == 0)] = 100.0
    rsi[(gain == 0) & (loss > 0)] = 0.0
    rsi.iloc[:period] = 50.0
    return rsi.astype(float)


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


def estimate_candle_volume_usd(candles: pd.DataFrame, lookback_bars: int = 24) -> float:
    if candles.empty or "volume" not in candles.columns:
        return 0.0
    recent = candles.tail(lookback_bars)
    if "close" in candles.columns:
        return float((recent["volume"] * recent["close"]).sum())
    return float(recent["volume"].sum())


# ═══════════════════════════════════════════════════════════════════════
# DYNAMIC FEE CALCULATION
# ═══════════════════════════════════════════════════════════════════════

BTC_ETH_SYMBOLS = {
    "BTCUSD", "ETHUSD", "BTCUSDT", "ETHUSDT",
    "BTCPERP", "ETHPERP", "XBTUSD",
}


def calculate_dynamic_fees(
    symbol: str,
    entry_price: float,
    exit_price: float,
    qty: float,
    charge_rate: float,
    entry_time,
    exit_time,
) -> float:
    """
    Dynamic fee calculation:
    - BTC/ETH: if closed within 30 minutes → only entry fee (0.05%)
    - Other coins: if closed within 15 minutes → only entry fee (0.05%)
    - Otherwise: normal round-trip fees (entry + exit) × charge_rate
    """
    is_btc_eth = symbol.upper() in BTC_ETH_SYMBOLS
    fast_exit_minutes = 30.0 if is_btc_eth else 15.0
    single_side_rate = 0.0005

    duration_minutes = float("inf")
    try:
        if hasattr(entry_time, 'timestamp') and hasattr(exit_time, 'timestamp'):
            duration_seconds = exit_time.timestamp() - entry_time.timestamp()
            duration_minutes = duration_seconds / 60.0
        elif isinstance(entry_time, (int, float)) and isinstance(exit_time, (int, float)):
            duration_minutes = (exit_time - entry_time) / 60.0
    except Exception:
        duration_minutes = float("inf")

    entry_notional = abs(entry_price * qty)
    exit_notional = abs(exit_price * qty)

    if duration_minutes <= fast_exit_minutes:
        return entry_notional * single_side_rate
    else:
        return (entry_notional + exit_notional) * charge_rate


# ═══════════════════════════════════════════════════════════════════════
# UNIFIED SHORT STRATEGY: mhr_pump_short
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class UnifiedShortConfig:
    lookback_green_candles: int = 4
    min_pump_pct: float = 50.0
    lookback_hours: int = 24
    min_volume_surge_pct: float = 2.0
    max_reentries: int = 3
    reentry_cooldown_candles: int = 1
    capital_deployed: float = 100.0
    leverage: float = 20.0
    sl_capital_pct: float = 0.05
    pt_capital_pct: float = 1.0
    min_rr: float = 4.0
    max_capital_usage_pct: float = 0.50
    max_pnl_impact_pct: float = 2.0
    atr_sl_multiplier: float = 1.5
    pump_atr_sl_multiplier: float = 2.5


DEFAULT_CONFIG = UnifiedShortConfig()


def calculate_trade_capital(available_capital: float, config) -> float:
    tc = available_capital * config.max_capital_usage_pct
    return tc if tc >= 1.0 else 0.0


def calculate_sl_tp_prices(entry_price, config, override_capital=None,
                           atr_value=None, use_pump_atr=False):
    if entry_price <= 0:
        raise ValueError("entry_price must be > 0")
    capital = override_capital if override_capital is not None else config.capital_deployed
    if capital <= 0:
        raise ValueError("capital must be > 0")
    max_notional = capital * config.leverage
    sl_loss = capital * config.sl_capital_pct
    atr_mult = config.pump_atr_sl_multiplier if use_pump_atr else config.atr_sl_multiplier
    if atr_value and atr_value > 0:
        sl_dist = atr_value * atr_mult
        raw_qty = min(sl_loss / sl_dist, max_notional / entry_price)
    else:
        raw_qty = max_notional / entry_price
        sl_dist = None
    qty = min(math.floor(raw_qty), 10_000_000)
    if qty < 1:
        raise ValueError("quantity < 1")
    if qty * entry_price > max_notional * 1.01:
        qty = math.floor(max_notional / entry_price)
        if qty < 1:
            raise ValueError("quantity < 1 after cap")
    sl_price = entry_price + (sl_dist if sl_dist else sl_loss / qty)
    tp_target = max(capital * config.pt_capital_pct, sl_loss * config.min_rr)
    tp_price = entry_price - (tp_target / qty)
    return float(sl_price), float(tp_price), int(qty)


def cap_pnl(net_pnl, trade_capital, config):
    return max(min(net_pnl, trade_capital * config.max_pnl_impact_pct), -trade_capital)


# ─── MHR Exhaustion [4] ──────────────────────────────────────────────

def is_multihour_runner_trigger(window):
    return bool((window["close"] > window["open"]).all())


def exhaustion_setup_ok(window: pd.DataFrame) -> bool:
    if window.empty or not is_multihour_runner_trigger(window):
        return False
    bodies = (window["close"] - window["open"]).abs().astype(float)
    if len(bodies) < 2:
        return True
    mean_body = bodies.mean()
    if mean_body <= 0:
        return True
    ratio = bodies.iloc[-1] / mean_body
    if ratio >= 2.0:
        return True
    if ratio > 1.3:
        return False
    return True


def entry_candle_rejection_ok(candle):
    body = abs(float(candle["close"]) - float(candle["open"]))
    upper_wick = float(candle["high"]) - max(float(candle["open"]), float(candle["close"]))
    if body <= 0:
        return upper_wick > 0, float("inf") if upper_wick > 0 else 0.0
    return upper_wick > body, upper_wick / body


# ─── Pump Detection ──────────────────────────────────────────────────

def detect_pump(candles, config):
    if candles.empty or len(candles) < config.lookback_hours:
        return None
    recent = candles.tail(config.lookback_hours)
    price_24h_ago = float(recent.iloc[0]["open"])
    current_price = float(recent.iloc[-1]["close"])
    if price_24h_ago <= 0:
        return None
    pump_pct = ((current_price - price_24h_ago) / price_24h_ago) * 100.0
    if pump_pct < config.min_pump_pct:
        return None
    vol_surge = float("inf")
    if "volume" in candles.columns:
        recent_vol = recent["volume"].astype(float).mean()
        if len(candles) >= config.lookback_hours * 2:
            prior = candles.iloc[-(config.lookback_hours * 2):-config.lookback_hours]
            prior_vol = prior["volume"].astype(float).mean()
            if prior_vol > 0:
                vol_surge = recent_vol / prior_vol
    if vol_surge < config.min_volume_surge_pct:
        return None
    return {"pump_pct": pump_pct, "volume_surge": vol_surge,
            "price_24h_ago": price_24h_ago, "highest_24h": float(recent["high"].max())}


def is_pump_still_active(candles, config):
    if candles.empty or len(candles) < config.lookback_hours:
        return False
    recent = candles.tail(config.lookback_hours)
    p0 = float(recent.iloc[0]["open"])
    pc = float(recent.iloc[-1]["close"])
    if p0 <= 0:
        return False
    return ((pc - p0) / p0) * 100.0 >= 20.0


def pump_hesitation_detected(prepared, entry_idx):
    if entry_idx < 1:
        return False
    current = prepared.iloc[entry_idx]
    previous = prepared.iloc[entry_idx - 1]
    c_open, c_close = float(current["open"]), float(current["close"])
    c_high = float(current["high"])
    p_high = float(previous["high"])
    p_body = abs(float(previous["close"]) - float(previous["open"]))
    if c_close < c_open:
        return True
    if c_high - max(c_open, c_close) > 0:
        return True
    c_body = abs(c_close - c_open)
    if p_body > 0 and c_body < p_body * 0.5:
        return True
    if c_high <= p_high:
        return True
    return False


# ─── Unified Entry Filter ────────────────────────────────────────────

def unified_entry_filter(prepared, entry_idx, config, is_reentry=False,
                         previous_entry_price=0.0):
    lookback = config.lookback_green_candles
    if entry_idx < max(lookback, config.lookback_hours):
        return None
    entry = prepared.iloc[entry_idx]
    close = float(entry["close"])
    ema_dist = None
    if "ema20" in prepared.columns:
        e20 = float(entry["ema20"])
        if e20 > 0:
            ema_dist = ((close - e20) / e20) * 100.0
    rsi14 = float(entry["rsi14"]) if "rsi14" in prepared.columns else None
    pump_window = prepared.iloc[max(0, entry_idx - config.lookback_hours):entry_idx + 1]
    pump_info = detect_pump(pump_window, config)
    has_pump = pump_info is not None
    pump_pct = pump_info["pump_pct"] if pump_info else 0.0
    vol_surge = pump_info["volume_surge"] if pump_info else 0.0

    if is_reentry:
        if not is_pump_still_active(pump_window, config):
            return None
        entry_price = float(entry["open"])
        if entry_price >= previous_entry_price:
            return None
        if not pump_hesitation_detected(prepared, entry_idx):
            return None
        return {"entry_candle_time": entry["time"], "entry_path": "REENTRY",
                "is_reentry": True, "pump_pct": pump_pct, "volume_surge": vol_surge,
                "ema_distance_pct": ema_dist, "rsi14": rsi14, "use_pump_atr": True}

    setup = prepared.iloc[entry_idx - lookback:entry_idx]
    if exhaustion_setup_ok(setup):
        return {"entry_candle_time": entry["time"], "entry_path": "MHR_EXHAUSTION",
                "is_reentry": False, "pump_pct": pump_pct, "volume_surge": vol_surge,
                "ema_distance_pct": ema_dist, "rsi14": rsi14, "use_pump_atr": False}

    if has_pump and pump_hesitation_detected(prepared, entry_idx):
        return {"entry_candle_time": entry["time"], "entry_path": "PUMP_HESITATION",
                "is_reentry": False, "pump_pct": pump_pct, "volume_surge": vol_surge,
                "ema_distance_pct": ema_dist, "rsi14": rsi14, "use_pump_atr": True}

    return None


# ═══════════════════════════════════════════════════════════════════════
# SCALP STRATEGY: scalp_pullback — 5-Minute Micro Pullback
#
# TWEAKS APPLIED:
# Tweak 3: min_pullback_candles = 3 (was 2) — deeper pullbacks only
# Tweak 4: Volume confirmation — signal candle must have above-average volume
# Tweak 5: SL buffer = 0.15% — wider stops to reduce false stop-outs
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ScalpConfig:
    ema_5m_length: int = 20
    ema_1h_length: int = 50
    min_pullback_candles: int = 4       # TWEAK 3: was 2, now requires 3 candle pullback
    max_pullback_candles: int = 6       # allow up to 4 for flexibility
    sl_buffer_pct: float = 0.0025      # TWEAK 5: 0.15% buffer on SL
    vol_lookback: int = 20             # TWEAK 4: lookback for average volume
    capital_deployed: float = 100.0
    leverage: float = 20.0
    sl_capital_pct: float = 0.02
    max_capital_usage_pct: float = 0.50
    max_pnl_impact_pct: float = 2.0
    tp1_rr: float = 1.4
    tp1_qty_pct: float = 0.50
    trail_lock_rr: float = 1.0
    min_volume: float = 0.0
    cooldown_candles: int = 3
    max_trades_per_session: int = 100
    max_holding_candles: int = 30


SCALP_DEFAULT_CONFIG = ScalpConfig()


def resample_5m_to_1h(df_5m: pd.DataFrame) -> pd.DataFrame:
    if df_5m.empty:
        return pd.DataFrame()
    df = df_5m.copy().set_index("time")
    r = df.resample("1h").agg({
        "open": "first", "high": "max",
        "low": "min", "close": "last", "volume": "sum",
    }).dropna().reset_index()
    return r


def has_above_average_volume(prepared: pd.DataFrame, idx: int, lookback: int = 20) -> bool:
    """
    TWEAK 4: Check if the signal candle has above-average volume.
    Strong reversals should come with volume confirmation.
    """
    if "volume" not in prepared.columns or idx < lookback:
        return True  # if no volume data, skip this filter

    current_vol = float(prepared.iloc[idx]["volume"])
    recent_vols = prepared["volume"].iloc[max(0, idx - lookback):idx].astype(float)

    if recent_vols.empty or recent_vols.mean() <= 0:
        return True

    avg_vol = recent_vols.mean()
    return current_vol >= avg_vol


def detect_scalp_long(prepared, idx, config):
    """
    LONG setup on 5m:
    - Signal candle is green
    - Price above EMA20
    - 3+ consecutive red candles before (TWEAK 3: was 2)
    - Signal candle has above-average volume (TWEAK 4)
    - SL below pullback low with 0.15% buffer (TWEAK 5)
    """
    if idx < config.max_pullback_candles + 1:
        return None
    sig = prepared.iloc[idx]
    # Signal candle must be green
    if float(sig["close"]) <= float(sig["open"]):
        return None
    # Price must be above EMA20
    if "ema20" in prepared.columns and float(sig["close"]) <= float(sig["ema20"]):
        return None

    atr = float(sig["atr14"]) if "atr14" in prepared.columns else 0

    # TWEAK 4: Volume confirmation
    if not has_above_average_volume(prepared, idx, config.vol_lookback):
        return None

    # Check for consecutive red candles before signal (TWEAK 3: min 3)
    count = 0
    low = float("inf")
    for lb in range(1, config.max_pullback_candles + 1):
        if idx - lb < 0:
            break
        c = prepared.iloc[idx - lb]
        if float(c["close"]) < float(c["open"]):
            count += 1
            low = min(low, float(c["low"]))
        else:
            break
    if count < config.min_pullback_candles:
        return None

    # TWEAK 5: SL below pullback low with buffer
    sl = low * (1.0 - config.sl_buffer_pct)

    return {"direction": "LONG", "sl_price": sl,
            "pullback_candles": count, "atr": atr,
            "signal_candle_time": sig["time"]}


def detect_scalp_short(prepared, idx, config):
    """
    SHORT setup on 5m:
    - Signal candle is red
    - Price below EMA20
    - 3+ consecutive green candles before (TWEAK 3: was 2)
    - Signal candle has above-average volume (TWEAK 4)
    - SL above pullback high with 0.15% buffer (TWEAK 5)
    """
    if idx < config.max_pullback_candles + 1:
        return None
    sig = prepared.iloc[idx]
    # Signal candle must be red
    if float(sig["close"]) >= float(sig["open"]):
        return None
    # Price must be below EMA20
    if "ema20" in prepared.columns and float(sig["close"]) >= float(sig["ema20"]):
        return None

    atr = float(sig["atr14"]) if "atr14" in prepared.columns else 0

    # TWEAK 4: Volume confirmation
    if not has_above_average_volume(prepared, idx, config.vol_lookback):
        return None

    # Check for consecutive green candles before signal (TWEAK 3: min 3)
    count = 0
    high = 0.0
    for lb in range(1, config.max_pullback_candles + 1):
        if idx - lb < 0:
            break
        c = prepared.iloc[idx - lb]
        if float(c["close"]) > float(c["open"]):
            count += 1
            high = max(high, float(c["high"]))
        else:
            break
    if count < config.min_pullback_candles:
        return None

    # TWEAK 5: SL above pullback high with buffer
    sl = high * (1.0 + config.sl_buffer_pct)

    return {"direction": "SHORT", "sl_price": sl,
            "pullback_candles": count, "atr": atr,
            "signal_candle_time": sig["time"]}


def scalp_entry_filter(prepared, idx, trend_1h, config):
    if idx < max(config.ema_5m_length, config.max_pullback_candles + 2):
        return None
    if config.min_volume > 0 and "volume" in prepared.columns:
        c = prepared.iloc[idx]
        if float(c["volume"]) < config.min_volume:
            return None
    if trend_1h == "BULLISH":
        return detect_scalp_long(prepared, idx, config)
    if trend_1h == "BEARISH":
        return detect_scalp_short(prepared, idx, config)
    return None


def simulate_scalp_exit(candles, entry_idx, entry_price, sl_price, qty,
                        direction, config):
    if direction == "LONG":
        risk = entry_price - sl_price
    else:
        risk = sl_price - entry_price
    if risk <= 0:
        return 0.0, "INVALID_RISK", candles.iloc[entry_idx]["time"], entry_price, 0

    q1 = round(qty * config.tp1_qty_pct, 3)
    q2 = round(qty - q1, 3)
    partial = 0.0
    partial_done = False
    trail_sl = sl_price
    best_r = 0.0
    bars = 0
    max_idx = min(len(candles) - 1, entry_idx + config.max_holding_candles)

    for idx in range(entry_idx + 1, max_idx + 1):
        bars += 1
        c = candles.iloc[idx]
        ch, cl = float(c["high"]), float(c["low"])

        if direction == "LONG":
            if cl <= trail_sl:
                pnl = partial + (trail_sl - entry_price) * (q2 if partial_done else qty)
                return pnl, "TRAIL_SL" if partial_done else "SL_HIT", c["time"], trail_sl, bars
            fav_r = (ch - entry_price) / risk if risk > 0 else 0
        else:
            if ch >= trail_sl:
                pnl = partial + (entry_price - trail_sl) * (q2 if partial_done else qty)
                return pnl, "TRAIL_SL" if partial_done else "SL_HIT", c["time"], trail_sl, bars
            fav_r = (entry_price - cl) / risk if risk > 0 else 0

        best_r = max(best_r, fav_r)

        if not partial_done and q1 > 0 and fav_r >= config.tp1_rr:
            partial_done = True
            partial = risk * config.tp1_rr * q1
            trail_sl = entry_price

        if partial_done and best_r > config.trail_lock_rr:
            lock = best_r - config.trail_lock_rr
            if direction == "LONG":
                trail_sl = max(trail_sl, entry_price + lock * risk)
            else:
                trail_sl = min(trail_sl, entry_price - lock * risk)

    last = candles.iloc[max_idx]
    ep = float(last["close"])
    if direction == "LONG":
        pnl = partial + (ep - entry_price) * (q2 if partial_done else qty)
    else:
        pnl = partial + (entry_price - ep) * (q2 if partial_done else qty)
    return pnl, "MAX_HOLD_PARTIAL" if partial_done else "MAX_HOLD", last["time"], ep, bars


def calculate_scalp_qty(entry_price, risk, config, override_capital=None):
    capital = override_capital or config.capital_deployed
    if capital <= 0 or entry_price <= 0 or risk <= 0:
        return 0
    max_notional = capital * config.leverage
    sl_loss = capital * config.sl_capital_pct
    qty = min(sl_loss / risk, max_notional / entry_price)
    qty = round(qty, 3)
    qty = min(qty, 10_000_000)
    return qty if qty > 0 else 0


def backtest_scalp_on_1m(symbol, candles_raw, config, running_equity=None, charge_rate=0.0005):
    """
    Run scalp backtest on 5-minute candles.
    All three tweaks applied:
    - Tweak 3: min 3 pullback candles
    - Tweak 4: volume confirmation on signal candle
    - Tweak 5: 0.15% SL buffer
    """
    trades = []
    equity = running_equity or config.capital_deployed
    if candles_raw.empty or len(candles_raw) < 300:
        logger.info(f"  {symbol} | scalp: too few candles ({len(candles_raw)})")
        return trades, equity

    sorted_raw = candles_raw.copy().sort_values("time").reset_index(drop=True)
    if len(sorted_raw) >= 2:
        time_diff = (sorted_raw["time"].iloc[1] - sorted_raw["time"].iloc[0]).total_seconds()
    else:
        time_diff = 60

    if time_diff < 120:
        df_5m_raw = sorted_raw.set_index("time").resample("5min").agg({
            "open": "first", "high": "max",
            "low": "min", "close": "last", "volume": "sum",
        }).dropna().reset_index()
    else:
        df_5m_raw = sorted_raw

    if df_5m_raw.empty or len(df_5m_raw) < 300:
        logger.info(f"  {symbol} | scalp: not enough 5m candles ({len(df_5m_raw)})")
        return trades, equity

    prepared = df_5m_raw.copy().sort_values("time").reset_index(drop=True)
    prepared["ema20"] = calculate_ema(prepared["close"].astype(float), config.ema_5m_length)
    prepared["atr14"] = calculate_atr(prepared, 14)

    df_1h = resample_5m_to_1h(prepared)
    if df_1h.empty or len(df_1h) < config.ema_1h_length + 1:
        logger.info(f"  {symbol} | scalp: not enough 1h ({len(df_1h)} need {config.ema_1h_length + 1})")
        return trades, equity

    df_1h["ema50"] = calculate_ema(df_1h["close"].astype(float), config.ema_1h_length)

    sorted_1h_times = df_1h["time"].tolist()
    sorted_1h_trends = []
    for _, r1h in df_1h.iterrows():
        p = float(r1h["close"])
        e = float(r1h["ema50"])
        if e > 0:
            sorted_1h_trends.append("BULLISH" if p > e else "BEARISH")
        else:
            sorted_1h_trends.append(None)

    def get_trend_at(ts):
        pos = bisect_right(sorted_1h_times, ts) - 1
        if 0 <= pos < len(sorted_1h_trends):
            return sorted_1h_trends[pos]
        return None

    idx = max(config.max_pullback_candles + 2, config.ema_5m_length + 5)
    session_trades = 0
    last_trade_idx = -config.cooldown_candles - 1

    trend_none = 0
    trend_checked = 0
    trend_bullish = 0
    trend_bearish = 0
    filter_rej = 0
    capital_rej = 0
    qty_rej = 0
    risk_rej = 0
    vol_rej = 0
    long_count = 0
    short_count = 0

    while idx < len(prepared) - 1:
        if idx - last_trade_idx < config.cooldown_candles:
            idx += 1
            continue
        if session_trades >= config.max_trades_per_session:
            break

        ct = prepared.iloc[idx]["time"]
        trend = get_trend_at(ct)
        trend_checked += 1

        if not trend:
            trend_none += 1
            idx += 1
            continue

        if trend == "BULLISH":
            trend_bullish += 1
        else:
            trend_bearish += 1

        setup = scalp_entry_filter(prepared, idx, trend, config)
        if not setup:
            filter_rej += 1
            idx += 1
            continue

        direction = setup["direction"]
        sl_price = setup["sl_price"]

        nxt = idx + 1
        if nxt >= len(prepared):
            break

        entry_price = float(prepared.iloc[nxt]["open"])

        if direction == "LONG":
            risk = entry_price - sl_price
        else:
            risk = sl_price - entry_price

        if risk <= 0:
            risk_rej += 1
            idx += 1
            continue

        trade_cap = calculate_trade_capital(equity, config)
        if trade_cap < 1.0:
            capital_rej += 1
            idx += 1
            continue

        qty = calculate_scalp_qty(entry_price, risk, config, trade_cap)
        if qty <= 0:
            qty_rej += 1
            idx += 1
            continue

        pnl, reason, exit_time, exit_price, bars = simulate_scalp_exit(
            prepared, nxt, entry_price, sl_price, qty, direction, config
        )

        fees = calculate_dynamic_fees(
            symbol, entry_price, exit_price, qty,
            charge_rate, ct, exit_time
        )
        net = pnl - fees
        net = cap_pnl(net, trade_cap, config)

        eq_before = equity
        equity += net
        if equity < 0:
            equity = 0.0

        rr = pnl / (risk * qty) if risk * qty > 0 else 0
        if direction == "LONG":
            long_count += 1
        else:
            short_count += 1
        session_trades += 1

        trades.append({
            "symbol": symbol, "strategy": "scalp_pullback",
            "direction": direction,
            "entry_time": ct, "exit_time": exit_time,
            "entry_price": entry_price, "sl_price": sl_price,
            "exit_price": exit_price,
            "qty": qty, "trade_capital": trade_cap,
            "equity_before_trade": eq_before, "equity_after_trade": equity,
            "pullback_candles": setup["pullback_candles"],
            "atr14": setup["atr"], "trend_5m": trend,
            "bars_held": bars, "gross_pnl": pnl, "fees": fees,
            "net_pnl": net, "rr_achieved": rr,
            "roi_on_capital": net / trade_cap if trade_cap > 0 else 0,
            "exit_reason": reason,
            "entry_path": f"SCALP_{direction}",
            "is_reentry": False, "pump_pct": 0, "volume_surge": 0,
            "ema_distance_pct": None, "rsi14": None,
            "wick_body_ratio": None, "target_price": 0,
            "initial_sl_price": sl_price, "final_sl_price": sl_price,
            "atr_pct": 0, "partial_qty_filled": 0,
            "lower_high_price": None, "exhaustion_5m_signals": None,
            "rsi_5m": None,
        })

        last_trade_idx = nxt + bars
        idx = last_trade_idx + config.cooldown_candles
        if equity <= 0:
            break

    logger.info(
        f"  {symbol} | scalp_5m: "
        f"candles_5m={len(prepared)}, 1h={len(df_1h)}, "
        f"trend_checked={trend_checked}, trend_none={trend_none}, "
        f"bullish={trend_bullish}, bearish={trend_bearish}, "
        f"filter_rej={filter_rej}, cap_rej={capital_rej}, "
        f"qty_rej={qty_rej}, risk_rej={risk_rej}, "
        f"longs={long_count}, shorts={short_count}, "
        f"trades={len(trades)}, eq={equity:.2f}"
    )
    return trades, equity