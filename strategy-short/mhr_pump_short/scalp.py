"""
scalp_pullback strategy — 5-minute trend-following micro pullback.

Execution timeframe : 5-minute candles (resampled from 1m if needed)
Trend filter        : 1-hour EMA50 (resampled from 5m)

LONG  : price > EMA20(5m), 3+ red candles, strong green signal candle
SHORT : price < EMA20(5m), 3+ green candles, strong red signal candle

Exit  : 50% at TP1 (1.5R), trail remaining at best_r - 1R
"""

from __future__ import annotations

import math
import logging
from bisect import bisect_right

import pandas as pd

from .config import ScalpConfig
from .indicators import ema, atr
from .fees import calculate_fees

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# RESAMPLING
# ═══════════════════════════════════════════════════════════════════════════════

def resample_to_5m(df_1m: pd.DataFrame) -> pd.DataFrame:
    """Resample 1-minute OHLCV to 5-minute candles."""
    if df_1m.empty:
        return pd.DataFrame()
    df = df_1m.copy().set_index("time")
    r = df.resample("5min").agg({
        "open": "first", "high": "max",
        "low": "min", "close": "last", "volume": "sum",
    }).dropna().reset_index()
    return r


def resample_5m_to_1h(df_5m: pd.DataFrame) -> pd.DataFrame:
    """Resample 5-minute candles to 1-hour for trend filter."""
    if df_5m.empty:
        return pd.DataFrame()
    df = df_5m.copy().set_index("time")
    r = df.resample("1h").agg({
        "open": "first", "high": "max",
        "low": "min", "close": "last", "volume": "sum",
    }).dropna().reset_index()
    return r


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def _has_above_avg_volume(prepared: pd.DataFrame, idx: int, lookback: int) -> bool:
    """Signal candle must have above-average volume."""
    if "volume" not in prepared.columns or idx < lookback:
        return True
    current = float(prepared.iloc[idx]["volume"])
    recent = prepared["volume"].iloc[max(0, idx - lookback):idx].astype(float)
    if recent.empty or recent.mean() <= 0:
        return True
    return current >= recent.mean()


def detect_long(prepared: pd.DataFrame, idx: int, config: ScalpConfig) -> dict | None:
    """
    LONG setup on 5-minute candles.

    Requirements:
    - Signal candle is green
    - Price above EMA20(5m)
    - 3+ consecutive red candles before (pullback)
    - Signal candle has above-average volume
    - SL below pullback low with buffer
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

    atr_val = float(sig["atr14"]) if "atr14" in prepared.columns else 0

    # Volume confirmation
    if not _has_above_avg_volume(prepared, idx, config.vol_lookback):
        return None

    # Count consecutive red candles before signal
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

    # SL below pullback low with buffer
    sl = low * (1.0 - config.sl_buffer_pct)

    return {
        "direction": "LONG",
        "sl_price": sl,
        "pullback_candles": count,
        "atr": atr_val,
        "signal_candle_time": sig["time"],
    }


def detect_short(prepared: pd.DataFrame, idx: int, config: ScalpConfig) -> dict | None:
    """
    SHORT setup on 5-minute candles.

    Requirements:
    - Signal candle is red
    - Price below EMA20(5m)
    - 3+ consecutive green candles before (pullback)
    - Signal candle has above-average volume
    - SL above pullback high with buffer
    """
    if idx < config.max_pullback_candles + 1:
        return None

    sig = prepared.iloc[idx]

    if float(sig["close"]) >= float(sig["open"]):
        return None

    if "ema20" in prepared.columns and float(sig["close"]) >= float(sig["ema20"]):
        return None

    atr_val = float(sig["atr14"]) if "atr14" in prepared.columns else 0

    if not _has_above_avg_volume(prepared, idx, config.vol_lookback):
        return None

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

    sl = high * (1.0 + config.sl_buffer_pct)

    return {
        "direction": "SHORT",
        "sl_price": sl,
        "pullback_candles": count,
        "atr": atr_val,
        "signal_candle_time": sig["time"],
    }


def scalp_entry(prepared: pd.DataFrame, idx: int, trend: str,
                config: ScalpConfig) -> dict | None:
    """Dispatch to long or short detection based on 1h trend."""
    if idx < max(config.ema_5m_length, config.max_pullback_candles + 2):
        return None

    if config.min_volume > 0 and "volume" in prepared.columns:
        if float(prepared.iloc[idx]["volume"]) < config.min_volume:
            return None

    if trend == "BULLISH":
        return detect_long(prepared, idx, config)
    if trend == "BEARISH":
        return detect_short(prepared, idx, config)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# POSITION SIZING
# ═══════════════════════════════════════════════════════════════════════════════

def scalp_qty(entry_price: float, risk: float, config: ScalpConfig,
              override_capital: float | None = None) -> float:
    """
    Compute position quantity for a scalp trade.

    Returns fractional qty (rounded to 3 decimals) for high-price tokens like BTC.
    """
    capital = override_capital or config.capital_deployed
    if capital <= 0 or entry_price <= 0 or risk <= 0:
        return 0
    max_notional = capital * config.leverage
    sl_loss = capital * config.sl_capital_pct
    qty = min(sl_loss / risk, max_notional / entry_price)
    qty = round(qty, 3)
    return min(qty, 10_000_000) if qty > 0 else 0


def scalp_trade_capital(available: float, config: ScalpConfig) -> float:
    """Allocate capital for the next scalp trade (same logic as MHR)."""
    tc = available * config.max_capital_usage_pct
    return tc if tc >= 1.0 else 0.0


def cap_scalp_pnl(net_pnl: float, trade_cap: float, config: ScalpConfig) -> float:
    """Clamp PnL to prevent unrealistic equity swings."""
    return max(min(net_pnl, trade_cap * config.max_pnl_impact_pct), -trade_cap)


# ═══════════════════════════════════════════════════════════════════════════════
# EXIT SIMULATION
# ═══════════════════════════════════════════════════════════════════════════════

def simulate_scalp_exit(
    candles: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    sl_price: float,
    qty: float,
    direction: str,
    config: ScalpConfig,
) -> tuple[float, str, object, float, int]:
    """
    Simulate scalp exit: 50% at TP1, trail remaining at best_r - 1R.

    Returns (pnl, exit_reason, exit_time, exit_price, bars_held).
    """
    risk = abs(entry_price - sl_price) if direction == "LONG" else abs(sl_price - entry_price)
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


# ═══════════════════════════════════════════════════════════════════════════════
# BACKTEST RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

def backtest_scalp(
    symbol: str,
    candles_raw: pd.DataFrame,
    config: ScalpConfig,
    running_equity: float | None = None,
    charge_rate: float = 0.0005,
) -> tuple[list[dict], float]:
    """
    Run scalp backtest on 5-minute candles (auto-resampled from 1m if needed).

    Returns (trades_list, final_equity).
    """
    trades: list[dict] = []
    equity = running_equity or config.capital_deployed

    if candles_raw.empty or len(candles_raw) < 300:
        logger.info(f"  {symbol} | scalp: too few candles ({len(candles_raw)})")
        return trades, equity

    # Auto-detect 1m vs 5m input and resample if needed
    sorted_raw = candles_raw.copy().sort_values("time").reset_index(drop=True)
    time_diff = 60
    if len(sorted_raw) >= 2:
        time_diff = (sorted_raw["time"].iloc[1] - sorted_raw["time"].iloc[0]).total_seconds()

    if time_diff < 120:
        df_5m = resample_to_5m(sorted_raw)
    else:
        df_5m = sorted_raw

    if df_5m.empty or len(df_5m) < 300:
        logger.info(f"  {symbol} | scalp: not enough 5m candles ({len(df_5m)})")
        return trades, equity

    # Prepare 5m indicators
    prepared = df_5m.copy().sort_values("time").reset_index(drop=True)
    prepared["ema20"] = ema(prepared["close"].astype(float), config.ema_5m_length)
    prepared["atr14"] = atr(prepared, 14)

    # Build 1h candles for trend filter
    df_1h = resample_5m_to_1h(prepared)
    if df_1h.empty or len(df_1h) < config.ema_1h_length + 1:
        logger.info(f"  {symbol} | scalp: not enough 1h ({len(df_1h)})")
        return trades, equity

    df_1h["ema50"] = ema(df_1h["close"].astype(float), config.ema_1h_length)

    # Build sorted trend lookup for O(log n) bisect search
    sorted_1h_times = df_1h["time"].tolist()
    sorted_1h_trends = []
    for _, row in df_1h.iterrows():
        p = float(row["close"])
        e = float(row["ema50"])
        sorted_1h_trends.append("BULLISH" if e > 0 and p > e else ("BEARISH" if e > 0 else None))

    def get_trend(ts):
        pos = bisect_right(sorted_1h_times, ts) - 1
        return sorted_1h_trends[pos] if 0 <= pos < len(sorted_1h_trends) else None

    # ── Scan loop ────────────────────────────────────────────────────────
    idx = max(config.max_pullback_candles + 2, config.ema_5m_length + 5)
    session_trades = 0
    last_trade_idx = -config.cooldown_candles - 1
    stats = {"trend_none": 0, "trend_bull": 0, "trend_bear": 0,
             "filter_rej": 0, "cap_rej": 0, "qty_rej": 0, "risk_rej": 0,
             "longs": 0, "shorts": 0}

    while idx < len(prepared) - 1:
        if idx - last_trade_idx < config.cooldown_candles:
            idx += 1; continue
        if session_trades >= config.max_trades_per_session:
            break

        trend = get_trend(prepared.iloc[idx]["time"])
        if not trend:
            stats["trend_none"] += 1; idx += 1; continue

        stats["trend_bull" if trend == "BULLISH" else "trend_bear"] += 1

        setup = scalp_entry(prepared, idx, trend, config)
        if not setup:
            stats["filter_rej"] += 1; idx += 1; continue

        direction = setup["direction"]
        sl_price = setup["sl_price"]

        # Direct entry at next candle open
        nxt = idx + 1
        if nxt >= len(prepared):
            break
        entry_price = float(prepared.iloc[nxt]["open"])
        risk = abs(entry_price - sl_price) if direction == "LONG" else abs(sl_price - entry_price)

        if risk <= 0:
            stats["risk_rej"] += 1; idx += 1; continue

        trade_cap = scalp_trade_capital(equity, config)
        if trade_cap < 1.0:
            stats["cap_rej"] += 1; idx += 1; continue

        qty = scalp_qty(entry_price, risk, config, trade_cap)
        if qty <= 0:
            stats["qty_rej"] += 1; idx += 1; continue

        pnl, reason, exit_time, exit_price, bars = simulate_scalp_exit(
            prepared, nxt, entry_price, sl_price, qty, direction, config,
        )

        fees = calculate_fees(symbol, entry_price, exit_price, qty,
                              charge_rate, prepared.iloc[idx]["time"], exit_time)
        net = pnl - fees
        net = cap_scalp_pnl(net, trade_cap, config)

        eq_before = equity
        equity += net
        if equity < 0:
            equity = 0.0

        stats["longs" if direction == "LONG" else "shorts"] += 1
        session_trades += 1

        trades.append({
            "symbol": symbol, "strategy": "scalp_pullback",
            "direction": direction,
            "entry_time": prepared.iloc[idx]["time"], "exit_time": exit_time,
            "entry_price": entry_price, "sl_price": sl_price,
            "exit_price": exit_price,
            "qty": qty, "trade_capital": trade_cap,
            "equity_before_trade": eq_before, "equity_after_trade": equity,
            "pullback_candles": setup["pullback_candles"],
            "atr14": setup["atr"], "trend_5m": trend,
            "bars_held": bars, "gross_pnl": pnl, "fees": fees,
            "net_pnl": net, "rr_achieved": pnl / (risk * qty) if risk * qty > 0 else 0,
            "roi_on_capital": net / trade_cap if trade_cap > 0 else 0,
            "exit_reason": reason,
            "entry_path": f"SCALP_{direction}",
            "is_reentry": False, "pump_pct": 0, "volume_surge": 0,
            "ema_distance_pct": None, "rsi14": None,
            "wick_body_ratio": None, "target_price": 0,
            "initial_sl_price": sl_price, "final_sl_price": sl_price,
            "atr_pct": 0, "partial_qty_filled": 0,
            "lower_high_price": None, "exhaustion_5m_signals": None,
            "rsi_5m": None, "sl_method": "pullback",
        })

        last_trade_idx = nxt + bars
        idx = last_trade_idx + config.cooldown_candles
        if equity <= 0:
            break

    logger.info(
        f"  {symbol} | scalp_5m: "
        f"5m={len(prepared)}, 1h={len(df_1h)}, "
        f"bull={stats['trend_bull']}, bear={stats['trend_bear']}, "
        f"rej={stats['filter_rej']}, "
        f"L={stats['longs']}, S={stats['shorts']}, "
        f"trades={len(trades)}, eq={equity:.2f}"
    )
    return trades, equity