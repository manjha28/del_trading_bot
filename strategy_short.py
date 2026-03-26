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


def _validate_ema_dataframe(df: pd.DataFrame, config: EMAShortConfig) -> bool:
    if df is None or df.empty:
        return False

    required_columns = {"time", "open", "high", "low", "close"}
    if not required_columns.issubset(df.columns):
        return False

    return len(df) >= config.ema_length + config.min_candles_buffer


def short_signal(df: pd.DataFrame, config: EMAShortConfig = EMA_DEFAULT_CONFIG) -> Optional[dict]:
    """
    Original EMA-based short strategy (kept intact):
    - Alert candle = candle[-2], Entry candle = candle[-1]
    - Alert low must be above EMA
    - Entry candle must break alert low
    - Entry = alert low, SL = alert high, TP = 1:R
    """
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


# ================= MULTI-HOUR RUNNER STRATEGY (new strategy, separate function name) ================= #

@dataclass(frozen=True)
class MultiHourRunnerConfig:
    lookback_green_candles: int = 5
    capital_deployed: float = 100.0
    leverage: float = 20.0
    sl_capital_pct: float = 0.05
    pt_capital_pct: float = 1.0


MULTI_HOUR_DEFAULT_CONFIG = MultiHourRunnerConfig()


def calculate_sl_tp_prices(entry_price: float, config: MultiHourRunnerConfig) -> tuple[float, float, float]:
    """
    Convert capital-based SL/PT targets into short-futures price levels.
    qty = (capital_deployed * leverage) / entry_price
    pnl = (entry_price - exit_price) * qty
    """
    if entry_price <= 0:
        raise ValueError("entry_price must be > 0")
    if config.capital_deployed <= 0 or config.leverage <= 0:
        raise ValueError("capital_deployed and leverage must be > 0")

    qty = (config.capital_deployed * config.leverage) / entry_price
    sl_loss_amount = config.capital_deployed * config.sl_capital_pct
    pt_profit_amount = config.capital_deployed * config.pt_capital_pct

    sl_price = entry_price + (sl_loss_amount / qty)
    tp_price = entry_price - (pt_profit_amount / qty)
    return sl_price, tp_price, qty


def _validate_multihour_dataframe(df: pd.DataFrame, lookback: int) -> bool:
    if df is None or df.empty:
        return False

    required_columns = {"time", "open", "high", "low", "close"}
    if not required_columns.issubset(df.columns):
        return False

    return len(df) >= lookback + 1


def is_multihour_runner_trigger(window: pd.DataFrame) -> bool:
    return bool((window["close"] > window["open"]).all())


def multihour_runner_short_signal(
    df: pd.DataFrame,
    config: MultiHourRunnerConfig = MULTI_HOUR_DEFAULT_CONFIG,
) -> Optional[dict]:
    """
    New strategy:
    - If past 5 hourly candles are bullish (close > open),
      short at open of next candle.
    - SL = 5% of capital deployed.
    - PT = 100% of capital deployed.
    """
    lookback = config.lookback_green_candles
    if not _validate_multihour_dataframe(df, lookback):
        return None

    prepared = df.copy().sort_values("time").reset_index(drop=True)
    setup = prepared.iloc[-(lookback + 1):-1]
    entry_candle = prepared.iloc[-1]

    if not is_multihour_runner_trigger(setup):
        return None

    entry_price = float(entry_candle["open"])
    sl_price, tp_price, qty = calculate_sl_tp_prices(entry_price, config)

    return {
        "signal": "SHORT",
        "entry": entry_price,
        "stop_loss": float(sl_price),
        "take_profit": float(tp_price),
        "quantity": float(qty),
        "capital_deployed": float(config.capital_deployed),
        "leverage": float(config.leverage),
        "setup_start_time": setup.iloc[0]["time"],
        "setup_end_time": setup.iloc[-1]["time"],
        "entry_candle_time": entry_candle["time"],
        "strategy": "multihour_runner_short",
    }
