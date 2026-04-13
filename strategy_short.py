import math
from dataclasses import dataclass
from typing import Optional

import pandas as pd


# ================= LEGACY EMA SHORT STRATEGY ================= #
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

    rsi = pd.Series(50.0, index=series.index, dtype=float)

    both_valid = (gain > 0) & (loss > 0)
    rsi[both_valid] = 100.0 - (100.0 / (1.0 + gain[both_valid] / loss[both_valid]))

    # All gains, no losses → RSI = 100
    rsi[(gain > 0) & (loss == 0)] = 100.0
    # All losses, no gains → RSI = 0
    rsi[(gain == 0) & (loss > 0)] = 0.0
    # No movement → RSI = 50 (already default)

    # Warmup period is unreliable
    rsi.iloc[:period] = 50.0

    return rsi.astype(float)


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


def calculate_sl_tp_prices(
    entry_price: float, config: MultiHourRunnerConfig
) -> tuple[float, float, int]:
    if entry_price <= 0:
        raise ValueError("entry_price must be > 0")
    if config.capital_deployed <= 0 or config.leverage <= 0:
        raise ValueError("capital_deployed and leverage must be > 0")

    raw_qty = (config.capital_deployed * config.leverage) / entry_price
    qty = math.floor(raw_qty)
    if qty < 1:
        raise ValueError(
            "effective quantity < 1 lot; increase capital/leverage or choose lower price instrument"
        )

    sl_loss_amount = config.capital_deployed * config.sl_capital_pct
    target_profit_amount = max(
        config.capital_deployed * config.pt_capital_pct,
        sl_loss_amount * config.min_rr,
    )

    sl_price = entry_price + (sl_loss_amount / qty)
    tp_price = entry_price - (target_profit_amount / qty)

    return float(sl_price), float(tp_price), int(qty)


def _validate_multihour_dataframe(df: pd.DataFrame, lookback: int) -> bool:
    if df is None or df.empty:
        return False
    required_columns = {"time", "open", "high", "low", "close"}
    if not required_columns.issubset(df.columns):
        return False
    # Need 55+ bars for EMA50 warmup + lookback window
    min_bars = max(lookback + 1, 55)
    return len(df) >= min_bars


def is_multihour_runner_trigger(window: pd.DataFrame) -> bool:
    return bool((window["close"] > window["open"]).all())


def exhaustion_setup_ok(window: pd.DataFrame) -> bool:
    """
    Green run required.  Accept two exhaustion patterns:
      1. Deceleration — last candle body is shrinking relative to the run.
      2. Blow-off     — last candle body is extremely large (climax candle).
    Reject "steady acceleration" which suggests trend continuation, not exhaustion.
    """
    if window.empty:
        return False
    if not is_multihour_runner_trigger(window):
        return False

    bodies = (window["close"] - window["open"]).abs().astype(float)
    if len(bodies) < 2:
        return True

    mean_body = bodies.mean()
    last_body = bodies.iloc[-1]

    if mean_body <= 0:
        return True

    ratio = last_body / mean_body

    # Blow-off climax candle (very large) → valid exhaustion
    if ratio >= 2.5:
        return True

    # Steady / moderate acceleration → likely continuation, not exhaustion
    if ratio > 1.5:
        return False

    # Deceleration or normal → valid exhaustion
    return True


def entry_candle_rejection_ok(candle: pd.Series) -> tuple[bool, float]:
    body = abs(float(candle["close"]) - float(candle["open"]))
    upper_wick = float(candle["high"]) - max(float(candle["open"]), float(candle["close"]))

    if body <= 0:
        # Doji — treat upper wick as rejection if present
        return upper_wick > 0, float("inf") if upper_wick > 0 else 0.0

    ratio = upper_wick / body
    return upper_wick > body, ratio


# ─── Entry filters ────────────────────────────────────────────────────

def mhr_basic_entry_filters(
    prepared: pd.DataFrame, entry_idx: int, lookback: int
) -> Optional[dict]:
    """
    Minimal filter: just needs a valid green-candle exhaustion setup.
    Still computes indicator values for backtest logging.
    """
    if entry_idx < lookback:
        return None

    setup = prepared.iloc[entry_idx - lookback:entry_idx]
    if not exhaustion_setup_ok(setup):
        return None

    entry = prepared.iloc[entry_idx]
    close_price = float(entry["close"])

    # Compute indicators for logging (not filtering)
    ema_distance_pct = None
    if "ema20" in prepared.columns:
        ema20 = float(entry["ema20"])
        if ema20 > 0:
            ema_distance_pct = ((close_price - ema20) / ema20) * 100.0

    rsi14 = None
    if "rsi14" in prepared.columns:
        rsi14 = float(entry["rsi14"])

    _, wick_body_ratio = entry_candle_rejection_ok(entry)

    return {
        "setup_start_time": setup.iloc[0]["time"],
        "setup_end_time": setup.iloc[-1]["time"],
        "entry_candle_time": entry["time"],
        "ema_distance_pct": ema_distance_pct,
        "rsi14": rsi14,
        "wick_body_ratio": float(wick_body_ratio),
    }


def mhr_rsi_entry_filters(
    prepared: pd.DataFrame,
    entry_idx: int,
    lookback: int,
    min_ema_overextension_pct: float = 2.0,
    min_rsi: float = 65.0,
    max_rsi: float = 85.0,
) -> Optional[dict]:
    """
    Stricter filter for mean-reversion shorts.

    Logic: price is overextended above EMA20 with overbought RSI — ripe for
    a pullback.  We do NOT require price to be below EMA50 because an
    overextension setup inherently means price is above its moving averages.
    """
    if entry_idx < lookback:
        return None

    setup = prepared.iloc[entry_idx - lookback:entry_idx]
    entry = prepared.iloc[entry_idx]

    if not exhaustion_setup_ok(setup):
        return None

    # ── Validate required columns exist ──
    for col in ("ema20", "ema50", "rsi14"):
        if col not in prepared.columns:
            return None

    ema20 = float(entry["ema20"])
    ema50 = float(entry["ema50"])
    close_price = float(entry["close"])

    if ema20 <= 0 or ema50 <= 0:
        return None

    # Price must be overextended ABOVE ema20 (mean-reversion short setup)
    ema_distance_pct = ((close_price - ema20) / ema20) * 100.0
    if ema_distance_pct < min_ema_overextension_pct:
        return None

    # RSI must be in overbought zone but not extreme (avoid catching falling knives)
    rsi = float(entry["rsi14"])
    if rsi < min_rsi or rsi > max_rsi:
        return None

    # Optional trend context: EMA20 should be above EMA50 (confirmed uptrend
    # that we're fading the overextension of — not a downtrend bounce)
    # Remove this block if you want to trade both contexts
    if ema20 < ema50:
        return None

    # Entry candle must show upper wick rejection
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


# ─── Live signal generator (uses RSI filter) ─────────────────────────

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


# ─── Backward compatibility alias ────────────────────────────────────
def mhr_entry_filters(
    prepared: pd.DataFrame,
    entry_idx: int,
    lookback: int,
    min_ema_overextension_pct: float = 2.0,
    min_rsi: float = 65.0,
):
    return mhr_rsi_entry_filters(
        prepared, entry_idx, lookback, min_ema_overextension_pct, min_rsi
    )