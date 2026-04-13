import math
from dataclasses import dataclass
from typing import Optional

import pandas as pd


# ================= LEGACY EMA SHORT STRATEGY (kept as primary short_signal) ================= #

@dataclass(frozen=True)
class EMAShortConfig:
    ema_length: int = 5
    risk_reward: float = 3.0
    min_candles_buffer: int = 2


EMA_DEFAULT_CONFIG = EMAShortConfig()


def calculate_ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    safe_loss = loss.replace(0, float("nan"))
    rs = gain / safe_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0).astype(float)


def _validate_ema_dataframe(df: pd.DataFrame, config: EMAShortConfig) -> bool:
    if df is None or df.empty:
        return False

    required_columns = {"time", "open", "high", "low", "close"}
    if not required_columns.issubset(df.columns):
        return False

    return len(df) >= config.ema_length + config.min_candles_buffer


def short_signal(df: pd.DataFrame, config: EMAShortConfig = EMA_DEFAULT_CONFIG) -> Optional[dict]:
    if not _validate_ema_dataframe(df, config):
        return None

    prepared = df.copy().sort_values("time").reset_index(drop=True)
    prepared["ema"] = calculate_ema(prepared["close"], config.ema_length)

    alert = prepared.iloc[-2]
    entry_candle = prepared.iloc[-1]

    if alert["low"] <= alert["ema"]:
        return None

    if entry_candle["low"] >= alert["low"]:
        return None

    risk = float(alert["high"] - alert["low"])
    if risk <= 0:
        return None

    return {
        "signal": "SHORT",
        "entry": float(alert["low"]),
        "stop_loss": float(alert["high"]),
        "take_profit": float(float(alert["low"]) - risk * config.risk_reward),
        "alert_candle_time": alert["time"],
        "entry_candle_time": entry_candle["time"],
        "strategy": "ema_short",
    }


# ================= MULTI-HOUR RUNNER STRATEGY ================= #

@dataclass(frozen=True)
class MultiHourRunnerConfig:
    lookback_green_candles: int = 4
    capital_deployed: float = 100.0
    leverage: float = 20.0
    sl_capital_pct: float = 0.05
    pt_capital_pct: float = 1.0
    min_rr: float = 4.0


MULTI_HOUR_DEFAULT_CONFIG = MultiHourRunnerConfig()


def calculate_sl_tp_prices(entry_price: float, config: MultiHourRunnerConfig) -> tuple[float, float, int]:
    if entry_price <= 0:
        raise ValueError("entry_price must be > 0")
    if config.capital_deployed <= 0 or config.leverage <= 0:
        raise ValueError("capital_deployed and leverage must be > 0")

    raw_qty = (config.capital_deployed * config.leverage) / entry_price
    qty = math.floor(raw_qty)
    if qty < 1:
        raise ValueError("effective quantity < 1 lot; increase capital/leverage or choose lower price instrument")

    sl_loss_amount = config.capital_deployed * config.sl_capital_pct
    target_profit_amount = max(config.capital_deployed * config.pt_capital_pct, sl_loss_amount * config.min_rr)

    sl_price = entry_price + (sl_loss_amount / qty)
    tp_price = entry_price - (target_profit_amount / qty)
    return float(sl_price), float(tp_price), int(qty)


def _validate_multihour_dataframe(df: pd.DataFrame, lookback: int) -> bool:
    if df is None or df.empty:
        return False

    required_columns = {"time", "open", "high", "low", "close"}
    if not required_columns.issubset(df.columns):
        return False

    return len(df) >= max(lookback + 1, 60)


def is_multihour_runner_trigger(window: pd.DataFrame) -> bool:
    return bool((window["close"] > window["open"]).all())


def exhaustion_setup_ok(window: pd.DataFrame) -> bool:
    """Simple anti-overfit exhaustion check: green run + slowing body expansion."""
    if window.empty:
        return False
    if not is_multihour_runner_trigger(window):
        return False

    bodies = (window["close"] - window["open"]).abs().astype(float)
    if len(bodies) >= 2 and bodies.iloc[-1] > bodies.mean() * 1.5:
        return False
    return True


def entry_candle_rejection_ok(candle: pd.Series) -> tuple[bool, float]:
    body = abs(float(candle["close"]) - float(candle["open"]))
    upper_wick = float(candle["high"]) - max(float(candle["open"]), float(candle["close"]))
    ratio = (upper_wick / body) if body > 0 else float("inf")
    return upper_wick > body, ratio




def mhr_basic_entry_filters(prepared: pd.DataFrame, entry_idx: int, lookback: int) -> Optional[dict]:
    if entry_idx < lookback:
        return None
    setup = prepared.iloc[entry_idx - lookback:entry_idx]
    if not exhaustion_setup_ok(setup):
        return None
    entry = prepared.iloc[entry_idx]
    return {
        "setup_start_time": setup.iloc[0]["time"],
        "setup_end_time": setup.iloc[-1]["time"],
        "entry_candle_time": entry["time"],
        "ema_distance_pct": None,
        "rsi14": None,
        "wick_body_ratio": None,
    }

def mhr_rsi_entry_filters(
    prepared: pd.DataFrame,
    entry_idx: int,
    lookback: int,
    min_ema_overextension_pct: float = 2.0,
    min_rsi: float = 65.0,
) -> Optional[dict]:
    if entry_idx < lookback:
        return None

    setup = prepared.iloc[entry_idx - lookback:entry_idx]
    entry = prepared.iloc[entry_idx]

    if not exhaustion_setup_ok(setup):
        return None

    ema20 = float(entry["ema20"])
    ema50 = float(entry["ema50"])
    close_price = float(entry["close"])

    if ema20 <= 0:
        return None

    ema_distance_pct = ((close_price - ema20) / ema20) * 100.0
    if ema_distance_pct < min_ema_overextension_pct:
        return None

    rsi = float(entry["rsi14"])
    if rsi <= min_rsi:
        return None

    # trend filter: avoid shorting strong uptrends
    if close_price >= ema50:
        return None

    wick_ok, wick_body_ratio = entry_candle_rejection_ok(entry)
    if not wick_ok:
        return None

    return {
        "setup_start_time": setup.iloc[0]["time"],
        "setup_end_time": setup.iloc[-1]["time"],
        "entry_candle_time": entry["time"],
        "ema_distance_pct": float(ema_distance_pct),
        "rsi14": float(rsi),
        "wick_body_ratio": float(wick_body_ratio),
    }


def multihour_runner_short_signal(
    df: pd.DataFrame,
    config: MultiHourRunnerConfig = MULTI_HOUR_DEFAULT_CONFIG,
) -> Optional[dict]:
    lookback = config.lookback_green_candles
    if not _validate_multihour_dataframe(df, lookback):
        return None

    prepared = df.copy().sort_values("time").reset_index(drop=True)
    prepared["ema20"] = calculate_ema(prepared["close"], 20)
    prepared["ema50"] = calculate_ema(prepared["close"], 50)
    prepared["rsi14"] = calculate_rsi(prepared["close"], 14)

    entry_idx = len(prepared) - 1
    filter_info = mhr_rsi_entry_filters(prepared, entry_idx, lookback)
    if not filter_info:
        return None

    entry_candle = prepared.iloc[-1]
    entry_price = float(entry_candle["open"])
    try:
        sl_price, tp_price, qty = calculate_sl_tp_prices(entry_price, config)
    except ValueError:
        return None

    risk_per_unit = sl_price - entry_price
    rr = (entry_price - tp_price) / risk_per_unit if risk_per_unit > 0 else None

    return {
        "signal": "SHORT",
        "entry": entry_price,
        "stop_loss": float(sl_price),
        "take_profit": float(tp_price),
        "quantity": int(qty),
        "capital_deployed": float(config.capital_deployed),
        "leverage": float(config.leverage),
        "risk_reward": float(rr) if rr is not None else None,
        "strategy": "multihour_runner_short",
        **filter_info,
    }


# Backward compatibility alias
def mhr_entry_filters(prepared: pd.DataFrame, entry_idx: int, lookback: int, min_ema_overextension_pct: float = 2.0, min_rsi: float = 65.0):
    return mhr_rsi_entry_filters(prepared, entry_idx, lookback, min_ema_overextension_pct, min_rsi)
