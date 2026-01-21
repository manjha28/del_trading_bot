import pandas as pd

EMA_LENGTH = 5
RR = 3


def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def short_signal(df):
    """
    SHORT-ONLY STRATEGY (STRICT, NON-REPAINT)

    Uses ONLY last two CLOSED candles.

    Alert candle  = candle[-2]
    Entry candle  = candle[-1]

    Rules:
    - Alert candle must be fully above EMA (low > EMA)
    - Entry candle must break alert candle low
    - Entry price = alert candle low
    - SL = alert candle high
    - TP = 1:3 RR
    """

    if df is None or len(df) < EMA_LENGTH + 2:
        return None

    df = df.copy().sort_values("time").reset_index(drop=True)
    df["ema"] = calculate_ema(df["close"], EMA_LENGTH)

    # Use ONLY last two CLOSED candles
    alert = df.iloc[-2]
    entry_candle = df.iloc[-1]

    # Alert candle condition
    if alert["low"] <= alert["ema"]:
        return None

    # Entry condition: NEXT candle breaks alert low
    if entry_candle["low"] >= alert["low"]:
        return None

    risk = alert["high"] - alert["low"]
    if risk <= 0:
        return None

    return {
        "signal": "SHORT",
        "entry": float(alert["low"]),
        "stop_loss": float(alert["high"]),
        "take_profit": float(alert["low"] - risk * RR),
        "alert_candle_time": alert["time"]
    }
