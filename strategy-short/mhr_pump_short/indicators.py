"""
Technical indicator functions used by mhr_pump_short.

All functions are pure — they take pandas Series / DataFrames and return
Series or scalar values without side effects.
"""

import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential moving average (EMA) using Wilder / ewm span convention."""
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (RSI) using Wilder's smoothed averages.

    Warm-up candles (first `period` rows) are set to the neutral value 50.0
    so that downstream filters are not triggered by uninitialised data.
    """
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()

    result = pd.Series(50.0, index=series.index, dtype=float)
    both = (gain > 0) & (loss > 0)
    result[both] = 100.0 - (100.0 / (1.0 + gain[both] / loss[both]))
    result[(gain > 0) & (loss == 0)] = 100.0
    result[(gain == 0) & (loss > 0)] = 0.0
    result.iloc[:period] = 50.0
    return result


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range (ATR) using a simple rolling mean of True Range.

    True Range = max(high-low, |high-prev_close|, |low-prev_close|).
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(window=period, min_periods=1).mean()


def candle_volume_usd(candles: pd.DataFrame, lookback_bars: int = 24) -> float:
    """
    Approximate USD volume over the last `lookback_bars` candles.

    Uses `volume × close` when a close column is present; otherwise sums
    raw volume.  Returns 0.0 if volume data is absent or the DataFrame is empty.
    """
    if candles.empty or "volume" not in candles.columns:
        return 0.0
    recent = candles.tail(lookback_bars)
    if "close" in candles.columns:
        return float((recent["volume"] * recent["close"]).sum())
    return float(recent["volume"].sum())
