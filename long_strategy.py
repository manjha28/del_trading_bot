import math
import numpy as np
from dataclasses import dataclass
from typing import Optional

import pandas as pd


# ═══════════════════════════════════════════════════════════════════════
# MOVING AVERAGE IMPLEMENTATIONS
# All 12 MA types from the PineScript f_ma() function
# ═══════════════════════════════════════════════════════════════════════

def ma_sma(series: pd.Series, length: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=length, min_periods=1).mean()


def ma_ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=length, adjust=False).mean()


def ma_rma(series: pd.Series, length: int) -> pd.Series:
    """
    Running Moving Average (Wilder's smoothing).
    Equivalent to ta.rma() in PineScript.
    alpha = 1/length
    """
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def ma_wma(series: pd.Series, length: int) -> pd.Series:
    """
    Weighted Moving Average.
    Equivalent to ta.wma() in PineScript.
    """
    weights = np.arange(1, length + 1, dtype=float)

    def _wma(window):
        w = weights[-len(window):]
        return np.dot(window, w) / w.sum()

    return series.rolling(window=length, min_periods=length).apply(_wma, raw=True)


def ma_vwma(df: pd.DataFrame, length: int) -> pd.Series:
    """
    Volume Weighted Moving Average.
    Equivalent to ta.vwma() in PineScript.
    Falls back to SMA if no volume column.
    """
    if "volume" not in df.columns:
        return ma_sma(df["close"].astype(float), length)
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    return (close * vol).rolling(window=length, min_periods=1).sum() / \
           vol.rolling(window=length, min_periods=1).sum().replace(0, float("nan"))


def ma_tema(series: pd.Series, length: int) -> pd.Series:
    """
    Triple Exponential Moving Average.
    PineScript: 3 * ema1 - 3 * ema2 + ema3
    """
    ema1 = ma_ema(series, length)
    ema2 = ma_ema(ema1, length)
    ema3 = ma_ema(ema2, length)
    return 3.0 * ema1 - 3.0 * ema2 + ema3


def ma_dema(series: pd.Series, length: int) -> pd.Series:
    """
    Double Exponential Moving Average.
    PineScript: 2 * ema(src, len) - ema(ema(src, len), len)
    """
    ema1 = ma_ema(series, length)
    ema2 = ma_ema(ema1, length)
    return 2.0 * ema1 - ema2


def ma_tma(series: pd.Series, length: int) -> pd.Series:
    """
    Triangular Moving Average.
    PineScript: sma(sma(src, len), len)
    """
    sma1 = ma_sma(series, length)
    return ma_sma(sma1, length)


def ma_alma(series: pd.Series, length: int, offset: float = 0.85,
            sigma: float = 6.0) -> pd.Series:
    """
    Arnaud Legoux Moving Average.
    Equivalent to ta.alma(src, len, 0.85, 6) in PineScript.
    """
    m = math.floor(offset * (length - 1))
    s = length / sigma
    weights = np.array([
        math.exp(-((i - m) ** 2) / (2 * s * s))
        for i in range(length)
    ])
    weights = weights / weights.sum()

    def _alma(window):
        if len(window) < length:
            return float("nan")
        return np.dot(window, weights)

    return series.rolling(window=length, min_periods=length).apply(_alma, raw=True)


def ma_hma(series: pd.Series, length: int) -> pd.Series:
    """
    Hull Moving Average.
    Equivalent to ta.hma() in PineScript.
    HMA = WMA(2 * WMA(src, len/2) - WMA(src, len), sqrt(len))
    """
    half_len = max(1, length // 2)
    sqrt_len = max(1, int(math.sqrt(length)))
    wma_half = ma_wma(series, half_len)
    wma_full = ma_wma(series, length)
    diff = 2.0 * wma_half - wma_full
    # Fill NaN before computing final WMA
    diff = diff.fillna(method="bfill")
    return ma_wma(diff, sqrt_len)


def ma_lsma(series: pd.Series, length: int) -> pd.Series:
    """
    Least Squares Moving Average (Linear Regression).
    Equivalent to ta.linreg(src, len, 0) in PineScript.
    """
    def _linreg(window):
        if len(window) < length:
            return float("nan")
        x = np.arange(length, dtype=float)
        y = np.array(window, dtype=float)
        # Linear regression: y = mx + b, return value at x = length-1
        n = len(x)
        sum_x = x.sum()
        sum_y = y.sum()
        sum_xy = (x * y).sum()
        sum_x2 = (x * x).sum()
        denom = n * sum_x2 - sum_x * sum_x
        if denom == 0:
            return y[-1]
        m = (n * sum_xy - sum_x * sum_y) / denom
        b = (sum_y - m * sum_x) / n
        return m * (n - 1) + b  # value at offset 0

    return series.rolling(window=length, min_periods=length).apply(_linreg, raw=True)


def ma_zlema(series: pd.Series, length: int) -> pd.Series:
    """
    Zero Lag Exponential Moving Average.
    PineScript:
        lag = floor((len - 1) / 2)
        priceAdjusted = src + (src - src[lag])
        result = ema(priceAdjusted, len)
    """
    lag = (length - 1) // 2
    adjusted = series + (series - series.shift(lag))
    adjusted = adjusted.fillna(series)
    return ma_ema(adjusted, length)


def get_ma(df: pd.DataFrame, ma_type: str, length: int) -> pd.Series:
    """
    Master MA selector — matches the PineScript f_ma() function exactly.
    Supports all 12 MA types from the original script.
    """
    series = df["close"].astype(float)
    ma_type = ma_type.upper()

    ma_map = {
        "EMA": lambda: ma_ema(series, length),
        "SMA": lambda: ma_sma(series, length),
        "RMA": lambda: ma_rma(series, length),
        "WMA": lambda: ma_wma(series, length),
        "VWMA": lambda: ma_vwma(df, length),
        "TEMA": lambda: ma_tema(series, length),
        "DEMA": lambda: ma_dema(series, length),
        "TMA": lambda: ma_tma(series, length),
        "ALMA": lambda: ma_alma(series, length),
        "HMA": lambda: ma_hma(series, length),
        "LSMA": lambda: ma_lsma(series, length),
        "ZLEMA": lambda: ma_zlema(series, length),
    }

    if ma_type not in ma_map:
        raise ValueError(
            f"Unknown MA type: {ma_type}. "
            f"Supported: {', '.join(sorted(ma_map.keys()))}"
        )

    return ma_map[ma_type]()


# ═══════════════════════════════════════════════════════════════════════
# ATR FOR STOP LOSS SIZING
# ═══════════════════════════════════════════════════════════════════════

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


# ═══════════════════════════════════════════════════════════════════════
# STRATEGY CONFIGURATION
#
# Matches PineScript defaults:
#   fastLength = 24, slowLength = 65, maType = "EMA"
#   useShorts = false, commission = 0.1%
#   initial_capital = 100, default_qty = 100% of equity
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FastSlowMAConfig:
    # MA settings (from PineScript inputs)
    fast_length: int = 24
    slow_length: int = 65
    ma_type: str = "EMA"

    # Trade direction
    allow_long: bool = True
    allow_short: bool = False       # useShorts = false in PineScript

    # Capital / risk management
    capital_deployed: float = 100.0
    leverage: float = 20.0
    max_capital_usage_pct: float = 0.50   # 50% of equity per trade
    sl_capital_pct: float = 0.05          # SL = 5% of capital
    min_entry_price: float = 0.10
    max_pnl_impact_pct: float = 2.0

    # ATR-based SL/TP
    atr_sl_multiplier: float = 1.5
    tp_rr_ratio: float = 3.0             # TP at 3× risk distance

    # Position size cap (prevents explosion on low-price tokens) [7]
    max_quantity: int = 10_000_000


FAST_SLOW_DEFAULT_CONFIG = FastSlowMAConfig()


# ═══════════════════════════════════════════════════════════════════════
# CROSSOVER / CROSSUNDER DETECTION
#
# Matches PineScript:
#   ta.crossover(maMomentum, 0)  → fast crosses above slow
#   ta.crossunder(maMomentum, 0) → fast crosses below slow
# ═══════════════════════════════════════════════════════════════════════

def detect_crossover(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """Bullish crossover: fast crosses ABOVE slow."""
    return ((fast > slow) & (fast.shift(1) <= slow.shift(1))).fillna(False)


def detect_crossunder(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """Bearish crossunder: fast crosses BELOW slow."""
    return ((fast < slow) & (fast.shift(1) >= slow.shift(1))).fillna(False)


# ═══════════════════════════════════════════════════════════════════════
# POSITION SIZING
# ═══════════════════════════════════════════════════════════════════════

def calculate_trade_capital(available_capital: float, config: FastSlowMAConfig) -> float:
    """Strictly 50% of available capital."""
    tc = available_capital * config.max_capital_usage_pct
    return tc if tc >= 1.0 else 0.0


def calculate_long_sl_tp(
    entry_price: float,
    config: FastSlowMAConfig,
    override_capital: float | None = None,
    atr_value: float | None = None,
) -> tuple[float, float, int]:
    """
    Compute SL, TP and quantity for a LONG position.
    SL is BELOW entry, TP is ABOVE entry.
    """
    if entry_price <= 0:
        raise ValueError("entry_price must be > 0")
    if entry_price < config.min_entry_price:
        raise ValueError(f"price {entry_price:.6f} below {config.min_entry_price}")

    capital = override_capital if override_capital is not None else config.capital_deployed
    if capital <= 0:
        raise ValueError("capital must be > 0")

    max_notional = capital * config.leverage
    sl_loss = capital * config.sl_capital_pct

    if atr_value and atr_value > 0:
        sl_dist = atr_value * config.atr_sl_multiplier
        raw_qty = min(sl_loss / sl_dist, max_notional / entry_price)
    else:
        raw_qty = max_notional / entry_price
        sl_dist = None

    qty = min(math.floor(raw_qty), config.max_quantity)
    if qty < 1:
        raise ValueError("quantity < 1")
    if qty * entry_price > max_notional * 1.01:
        qty = math.floor(max_notional / entry_price)
        if qty < 1:
            raise ValueError("quantity < 1 after cap")

    # LONG: SL below entry, TP above entry
    if sl_dist is not None:
        sl_price = entry_price - sl_dist
        tp_price = entry_price + sl_dist * config.tp_rr_ratio
    else:
        sl_price = entry_price - (sl_loss / qty)
        tp_price = entry_price + (sl_loss / qty) * config.tp_rr_ratio

    return float(max(sl_price, 0.0)), float(tp_price), int(qty)


def calculate_short_sl_tp(
    entry_price: float,
    config: FastSlowMAConfig,
    override_capital: float | None = None,
    atr_value: float | None = None,
) -> tuple[float, float, int]:
    """
    Compute SL, TP and quantity for a SHORT position.
    SL is ABOVE entry, TP is BELOW entry.
    """
    if entry_price <= 0:
        raise ValueError("entry_price must be > 0")
    if entry_price < config.min_entry_price:
        raise ValueError(f"price {entry_price:.6f} below {config.min_entry_price}")

    capital = override_capital if override_capital is not None else config.capital_deployed
    if capital <= 0:
        raise ValueError("capital must be > 0")

    max_notional = capital * config.leverage
    sl_loss = capital * config.sl_capital_pct

    if atr_value and atr_value > 0:
        sl_dist = atr_value * config.atr_sl_multiplier
        raw_qty = min(sl_loss / sl_dist, max_notional / entry_price)
    else:
        raw_qty = max_notional / entry_price
        sl_dist = None

    qty = min(math.floor(raw_qty), config.max_quantity)
    if qty < 1:
        raise ValueError("quantity < 1")
    if qty * entry_price > max_notional * 1.01:
        qty = math.floor(max_notional / entry_price)
        if qty < 1:
            raise ValueError("quantity < 1 after cap")

    # SHORT: SL above entry, TP below entry
    if sl_dist is not None:
        sl_price = entry_price + sl_dist
        tp_price = entry_price - sl_dist * config.tp_rr_ratio
    else:
        sl_price = entry_price + (sl_loss / qty)
        tp_price = entry_price - (sl_loss / qty) * config.tp_rr_ratio

    return float(sl_price), float(max(tp_price, 0.0)), int(qty)


def cap_pnl(net_pnl: float, trade_capital: float, config: FastSlowMAConfig) -> float:
    """Cap PnL to prevent exponential equity explosion [7]."""
    return max(min(net_pnl, trade_capital * config.max_pnl_impact_pct), -trade_capital)


# ═══════════════════════════════════════════════════════════════════════
# SIGNAL GENERATION
#
# Matches PineScript logic:
#   if ta.crossover(maMomentum, 0)  → close short, entry long
#   if ta.crossunder(maMomentum, 0) → close long, entry short (if enabled)
#
# The PineScript uses position flipping (close + reverse on crossover).
# In our implementation, each signal is independent — the backtest
# handles position management.
# ═══════════════════════════════════════════════════════════════════════

def prepare_indicators(df: pd.DataFrame, config: FastSlowMAConfig) -> pd.DataFrame:
    """Compute fast MA, slow MA, ATR, and crossover signals."""
    if df is None or df.empty:
        return pd.DataFrame()

    min_bars = max(config.fast_length, config.slow_length) + 5
    if len(df) < min_bars:
        return pd.DataFrame()

    prepared = df.copy().sort_values("time").reset_index(drop=True)

    # Compute MAs using the selected type
    prepared["fast_ma"] = get_ma(prepared, config.ma_type, config.fast_length)
    prepared["slow_ma"] = get_ma(prepared, config.ma_type, config.slow_length)

    # ATR for SL sizing
    prepared["atr14"] = calculate_atr(prepared, 14)

    # MA momentum (matches PineScript: fastMA >= slowMA ? 1 : -1)
    prepared["ma_momentum"] = 0
    prepared.loc[prepared["fast_ma"] >= prepared["slow_ma"], "ma_momentum"] = 1
    prepared.loc[prepared["fast_ma"] < prepared["slow_ma"], "ma_momentum"] = -1

    # MA separation as percentage of slow MA
    slow_safe = prepared["slow_ma"].replace(0, float("nan"))
    prepared["ma_separation_pct"] = (
        (prepared["fast_ma"] - prepared["slow_ma"]).abs() / slow_safe * 100
    )

    # Crossovers (matches PineScript ta.crossover/ta.crossunder)
    prepared["long_signal"] = detect_crossover(prepared["fast_ma"], prepared["slow_ma"])
    prepared["short_signal"] = detect_crossunder(prepared["fast_ma"], prepared["slow_ma"])

    # Trend state for logging
    prepared["trend"] = "neutral"
    prepared.loc[prepared["fast_ma"] > prepared["slow_ma"], "trend"] = "bullish"
    prepared.loc[prepared["fast_ma"] < prepared["slow_ma"], "trend"] = "bearish"

    return prepared


def get_long_signal(
    df: pd.DataFrame,
    config: FastSlowMAConfig = FAST_SLOW_DEFAULT_CONFIG,
    override_capital: float | None = None,
) -> Optional[dict]:
    """
    Generate a LONG signal when fast MA crosses above slow MA.
    Used for live trading — checks the most recent candle.
    """
    if not config.allow_long:
        return None

    prepared = prepare_indicators(df, config)
    if prepared.empty:
        return None

    last_idx = len(prepared) - 1
    last = prepared.iloc[last_idx]

    if not last["long_signal"]:
        return None

    entry_price = float(last["close"])
    atr_value = float(last["atr14"])
    fast_ma = float(last["fast_ma"])
    slow_ma = float(last["slow_ma"])

    try:
        sl, tp, qty = calculate_long_sl_tp(
            entry_price, config, override_capital, atr_value
        )
    except ValueError:
        return None

    risk = entry_price - sl
    rr = (tp - entry_price) / risk if risk > 0 else None

    return {
        "signal": "LONG",
        "strategy": "fast_slow_ma_long",
        "entry": entry_price,
        "stop_loss": sl,
        "take_profit": tp,
        "quantity": qty,
        "capital_deployed": override_capital or config.capital_deployed,
        "leverage": config.leverage,
        "risk_reward": float(rr) if rr else None,
        "atr14": atr_value,
        "fast_ma": fast_ma,
        "slow_ma": slow_ma,
        "ma_type": config.ma_type,
        "fast_length": config.fast_length,
        "slow_length": config.slow_length,
        "ma_separation_pct": float(last["ma_separation_pct"]),
        "trend": last["trend"],
        "entry_candle_time": last["time"],
    }


def get_short_signal(
    df: pd.DataFrame,
    config: FastSlowMAConfig = FAST_SLOW_DEFAULT_CONFIG,
    override_capital: float | None = None,
) -> Optional[dict]:
    """
    Generate a SHORT signal when fast MA crosses below slow MA.
    Used for live trading — checks the most recent candle.
    Only fires if config.allow_short is True.
    """
    if not config.allow_short:
        return None

    prepared = prepare_indicators(df, config)
    if prepared.empty:
        return None

    last_idx = len(prepared) - 1
    last = prepared.iloc[last_idx]

    if not last["short_signal"]:
        return None

    entry_price = float(last["close"])
    atr_value = float(last["atr14"])
    fast_ma = float(last["fast_ma"])
    slow_ma = float(last["slow_ma"])

    try:
        sl, tp, qty = calculate_short_sl_tp(
            entry_price, config, override_capital, atr_value
        )
    except ValueError:
        return None

    risk = sl - entry_price
    rr = (entry_price - tp) / risk if risk > 0 else None

    return {
        "signal": "SHORT",
        "strategy": "fast_slow_ma_short",
        "entry": entry_price,
        "stop_loss": sl,
        "take_profit": tp,
        "quantity": qty,
        "capital_deployed": override_capital or config.capital_deployed,
        "leverage": config.leverage,
        "risk_reward": float(rr) if rr else None,
        "atr14": atr_value,
        "fast_ma": fast_ma,
        "slow_ma": slow_ma,
        "ma_type": config.ma_type,
        "fast_length": config.fast_length,
        "slow_length": config.slow_length,
        "ma_separation_pct": float(last["ma_separation_pct"]),
        "trend": last["trend"],
        "entry_candle_time": last["time"],
    }


def get_exit_signal(
    df: pd.DataFrame,
    current_position: str,
    config: FastSlowMAConfig = FAST_SLOW_DEFAULT_CONFIG,
) -> Optional[str]:
    """
    Check if current position should be closed.

    PineScript logic:
    - If long and bearish crossunder → close long
    - If short and bullish crossover → close short

    Returns "CLOSE_LONG", "CLOSE_SHORT", or None.
    """
    prepared = prepare_indicators(df, config)
    if prepared.empty:
        return None

    last = prepared.iloc[-1]

    if current_position == "LONG" and last["short_signal"]:
        return "CLOSE_LONG"
    elif current_position == "SHORT" and last["long_signal"]:
        return "CLOSE_SHORT"

    return None


# ═══════════════════════════════════════════════════════════════════════
# BACKTEST-COMPATIBLE ENTRY FILTER
#
# This function is designed to be called from backtest.py [5]
# in the same pattern as mhr_basic_entry_filters in strategy_short.py [4]
# ═══════════════════════════════════════════════════════════════════════

def fast_slow_ma_entry_filter(
    prepared: pd.DataFrame,
    entry_idx: int,
    config: FastSlowMAConfig = FAST_SLOW_DEFAULT_CONFIG,
    direction: str = "LONG",
) -> Optional[dict]:
    """
    Entry filter compatible with backtest_symbol() loop.

    direction: "LONG" or "SHORT"
    Returns filter_info dict or None if no signal.
    """
    if entry_idx < max(config.fast_length, config.slow_length) + 2:
        return None

    entry = prepared.iloc[entry_idx]

    if direction == "LONG" and not entry.get("long_signal", False):
        return None
    elif direction == "SHORT" and not entry.get("short_signal", False):
        return None

    if direction == "LONG" and not config.allow_long:
        return None
    if direction == "SHORT" and not config.allow_short:
        return None

    fast_ma = float(entry["fast_ma"]) if "fast_ma" in prepared.columns else None
    slow_ma = float(entry["slow_ma"]) if "slow_ma" in prepared.columns else None
    ma_sep = float(entry["ma_separation_pct"]) if "ma_separation_pct" in prepared.columns else None

    return {
        "entry_candle_time": entry["time"],
        "fast_ma": fast_ma,
        "slow_ma": slow_ma,
        "ma_type": config.ma_type,
        "fast_length": config.fast_length,
        "slow_length": config.slow_length,
        "ma_separation_pct": ma_sep,
        "trend": entry.get("trend", "unknown"),
        "ema_distance_pct": ma_sep,    # compatibility with backtest logging
        "rsi14": None,
        "wick_body_ratio": 0.0,
    }


# ═══════════════════════════════════════════════════════════════════════
# BACKTEST EXIT SIMULATOR
#
# The PineScript uses position flipping — close + reverse on crossover.
# This simulator handles:
# 1. Hold until opposite crossover (close position)
# 2. OR SL/TP hit
# 3. OR max hold reached
# ═══════════════════════════════════════════════════════════════════════

def simulate_fast_slow_exit(
    candles: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    qty: int,
    direction: str,
    config: FastSlowMAConfig,
    max_hold: int = 100,
) -> tuple[float, str, pd.Timestamp, float, int]:
    """
    Simulate exit for fast/slow MA strategy.

    The PineScript exits on the OPPOSITE crossover signal.
    We also check SL/TP levels per candle.

    Returns: (pnl, exit_reason, exit_time, exit_price, bars_held)
    """
    bars_held = 0
    max_exit_idx = min(len(candles) - 1, entry_idx + max_hold)

    for idx in range(entry_idx + 1, max_exit_idx + 1):
        bars_held += 1
        candle = candles.iloc[idx]
        c_high = float(candle["high"])
        c_low = float(candle["low"])
        c_close = float(candle["close"])

        if direction == "LONG":
            # Check SL (price drops below SL)
            if c_low <= sl_price:
                pnl = (sl_price - entry_price) * qty
                return pnl, "SL_HIT", candle["time"], sl_price, bars_held

            # Check TP (price rises above TP)
            if c_high >= tp_price:
                pnl = (tp_price - entry_price) * qty
                return pnl, "TP_HIT", candle["time"], tp_price, bars_held

            # Check for exit signal (bearish crossover)
            if "short_signal" in candles.columns and candle.get("short_signal", False):
                pnl = (c_close - entry_price) * qty
                return pnl, "MA_CROSS_EXIT", candle["time"], c_close, bars_held

        else:  # SHORT
            # Check SL (price rises above SL)
            if c_high >= sl_price:
                pnl = (entry_price - sl_price) * qty
                return pnl, "SL_HIT", candle["time"], sl_price, bars_held

            # Check TP (price drops below TP)
            if c_low <= tp_price:
                pnl = (entry_price - tp_price) * qty
                return pnl, "TP_HIT", candle["time"], tp_price, bars_held

            # Check for exit signal (bullish crossover)
            if "long_signal" in candles.columns and candle.get("long_signal", False):
                pnl = (entry_price - c_close) * qty
                return pnl, "MA_CROSS_EXIT", candle["time"], c_close, bars_held

    # Max hold exit
    last = candles.iloc[max_exit_idx]
    exit_price = float(last["close"])
    if direction == "LONG":
        pnl = (exit_price - entry_price) * qty
    else:
        pnl = (entry_price - exit_price) * qty

    return pnl, "MAX_HOLD_EXIT", last["time"], exit_price, bars_held


# ═══════════════════════════════════════════════════════════════════════
# VOLUME HELPER (kept for backtest compatibility)
# ═══════════════════════════════════════════════════════════════════════

def estimate_candle_volume_usd(candles: pd.DataFrame, lookback_bars: int = 24) -> float:
    if candles.empty or "volume" not in candles.columns:
        return 0.0
    recent = candles.tail(lookback_bars)
    if "close" in candles.columns:
        return float((recent["volume"] * recent["close"]).sum())
    return float(recent["volume"].sum())