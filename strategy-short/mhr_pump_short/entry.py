"""
Entry signal logic for mhr_pump_short.

Three distinct entry paths are evaluated in priority order by entry_signal():

  REENTRY        — Pump is still active; price has pulled back below the previous
                   entry; hesitation signal present.  Uses wider ATR stop.

  MHR_EXHAUSTION — N consecutive green candles form an uptrend exhaustion pattern
                   (multi-hour runner).  Normal ATR stop.

  PUMP_HESITATION — A fresh pump is detected and the current candle shows
                    momentum loss.  Uses wider ATR stop.

The returned signal dict is consumed directly by the backtest loop and by
main.py to drive live order placement.
"""

from __future__ import annotations

import pandas as pd

from .config import MHRConfig


# ═══════════════════════════════════════════════════════════════════════════
# MHR_EXHAUSTION helpers
# ═══════════════════════════════════════════════════════════════════════════

def _all_green(window: pd.DataFrame) -> bool:
    """True when every candle in the window closed above its open."""
    return bool((window["close"] > window["open"]).all())


def exhaustion_setup(window: pd.DataFrame) -> bool:
    """
    Validate an N-candle multi-hour runner exhaustion pattern.

    Conditions:
    - All candles in `window` are green (_all_green).
    - The final candle's body does not sit in the 1.3×–2.0× mean-body range.
      Rationale: a body between 1.3× and 2.0× is ambiguous — larger than a
      normal runner candle but not clearly a blow-off top.  Both extremes are
      accepted (≤ 1.3× = steady runner, ≥ 2.0× = blow-off, both are valid
      exhaustion signals).

    Returns False on an empty window or a failed green check.
    """
    if window.empty or not _all_green(window):
        return False
    bodies = (window["close"] - window["open"]).abs().astype(float)
    if len(bodies) < 2:
        return True
    mean_body = bodies.mean()
    if mean_body <= 0:
        return True
    ratio = bodies.iloc[-1] / mean_body
    if 1.3 < ratio < 2.0:
        return False
    return True


def rejection_candle(candle: pd.Series) -> tuple[bool, float]:
    """
    Determine whether a candle is a rejection candle (sellers stepping in).

    A rejection candle has an upper wick that exceeds its body, indicating
    that buyers pushed price higher but sellers pushed it back down before close.

    Returns
    -------
    (is_rejection: bool, wick_body_ratio: float)
    wick_body_ratio is inf when the body is zero (doji with any upper wick).
    """
    body = abs(float(candle["close"]) - float(candle["open"]))
    upper_wick = float(candle["high"]) - max(float(candle["open"]), float(candle["close"]))
    if body <= 0:
        ratio = float("inf") if upper_wick > 0 else 0.0
        return upper_wick > 0, ratio
    return upper_wick > body, upper_wick / body


# ═══════════════════════════════════════════════════════════════════════════
# Pump detection
# ═══════════════════════════════════════════════════════════════════════════

def detect_pump(candles: pd.DataFrame, config: MHRConfig) -> dict | None:
    """
    Identify a pump event in `candles`.

    A pump requires both a price surge and a volume surge over the lookback window:
    - Price gain ≥ config.min_pump_pct over config.lookback_hours candles.
    - Volume in the recent window ≥ config.min_volume_surge_pct × prior-window average.
      (Volume check is skipped when insufficient history is available; vol_surge = ∞.)

    Returns a metadata dict on detection, None otherwise:
      {pump_pct, volume_surge, price_24h_ago, highest_24h}
    """
    if candles.empty or len(candles) < config.lookback_hours:
        return None

    recent = candles.tail(config.lookback_hours)
    base_price = float(recent.iloc[0]["open"])
    current_price = float(recent.iloc[-1]["close"])

    if base_price <= 0:
        return None

    pump_pct = ((current_price - base_price) / base_price) * 100.0
    if pump_pct < config.min_pump_pct:
        return None

    vol_surge = float("inf")
    if "volume" in candles.columns:
        recent_vol = recent["volume"].astype(float).mean()
        if len(candles) >= config.lookback_hours * 2:
            prior = candles.iloc[-(config.lookback_hours * 2) : -config.lookback_hours]
            prior_vol = prior["volume"].astype(float).mean()
            if prior_vol > 0:
                vol_surge = recent_vol / prior_vol

    if vol_surge < config.min_volume_surge_pct:
        return None

    return {
        "pump_pct": pump_pct,
        "volume_surge": vol_surge,
        "price_24h_ago": base_price,
        "highest_24h": float(recent["high"].max()),
    }


def pump_still_active(candles: pd.DataFrame, config: MHRConfig) -> bool:
    """
    Return True when price is still ≥ 20 % above its level config.lookback_hours ago.

    Used to gate reentry attempts — we only re-enter if the pump thesis is still valid.
    """
    if candles.empty or len(candles) < config.lookback_hours:
        return False
    recent = candles.tail(config.lookback_hours)
    base = float(recent.iloc[0]["open"])
    current = float(recent.iloc[-1]["close"])
    if base <= 0:
        return False
    return ((current - base) / base) * 100.0 >= 20.0


# ═══════════════════════════════════════════════════════════════════════════
# Hesitation detection
# ═══════════════════════════════════════════════════════════════════════════

def hesitation_detected(df: pd.DataFrame, idx: int) -> bool:
    """
    Detect momentum loss on the candle at `idx` relative to the previous candle.

    Triggers on any one of the following conditions (weakest acceptable evidence):
    1. Candle is red (close < open) — directional reversal.
    2. Upper wick is present (high > max(open, close)) — sellers appeared intra-bar.
    3. Candle body is < 50 % of the prior candle's body — momentum fading.
    4. High did not exceed the prior candle's high — upward momentum stalling.

    Returns False when idx < 1 (no prior candle to compare against).
    """
    if idx < 1:
        return False

    cur = df.iloc[idx]
    prev = df.iloc[idx - 1]

    c_open = float(cur["open"])
    c_close = float(cur["close"])
    c_high = float(cur["high"])
    p_high = float(prev["high"])
    p_body = abs(float(prev["close"]) - float(prev["open"]))

    if c_close < c_open:                            # red candle
        return True
    if c_high > max(c_open, c_close):              # upper wick present
        return True
    c_body = abs(c_close - c_open)
    if p_body > 0 and c_body < p_body * 0.5:       # body shrinking
        return True
    if c_high <= p_high:                            # no new high
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# Unified entry filter
# ═══════════════════════════════════════════════════════════════════════════

def entry_signal(
    df: pd.DataFrame,
    idx: int,
    config: MHRConfig,
    is_reentry: bool = False,
    previous_entry_price: float = 0.0,
) -> dict | None:
    """
    Evaluate all entry paths for mhr_pump_short at candle index `idx`.

    The function requires pre-computed indicator columns in `df`:
      - ``ema20``  (optional) — used for diagnostic ema_distance_pct
      - ``rsi14``  (optional) — logged as a diagnostic field
    These are not hard gates; they are captured as metadata for post-analysis.

    Entry paths evaluated in priority order
    ----------------------------------------
    REENTRY (only when is_reentry=True):
        - pump_still_active: price ≥ 20 % above 24h-ago level
        - current open < previous_entry_price (lower entry)
        - hesitation_detected on current candle
        → uses pump_atr_sl_multiplier

    MHR_EXHAUSTION (is_reentry=False, checked first):
        - exhaustion_setup: N green candles with body-ratio validation
        → uses atr_sl_multiplier

    PUMP_HESITATION (is_reentry=False, fallback):
        - detect_pump: ≥ min_pump_pct gain + volume surge
        - hesitation_detected on current candle
        → uses pump_atr_sl_multiplier

    Parameters
    ----------
    df                    : Prepared OHLCV DataFrame with indicator columns.
    idx                   : Index of the candidate entry candle.
    config                : MHRConfig instance.
    is_reentry            : True when evaluating a reentry after a stop-out.
    previous_entry_price  : Price of the last stop-out entry (used for reentry gate).

    Returns
    -------
    Signal dict on a valid setup, None otherwise.

    Signal dict keys
    ----------------
    entry_candle_time, entry_path, is_reentry, pump_pct, volume_surge,
    ema_distance_pct, rsi14, use_pump_atr
    """
    lookback = config.lookback_green_candles
    if idx < max(lookback, config.lookback_hours):
        return None

    candle = df.iloc[idx]
    close = float(candle["close"])

    # Diagnostic fields — not hard gates
    ema_dist: float | None = None
    if "ema20" in df.columns:
        e20 = float(candle["ema20"])
        if e20 > 0:
            ema_dist = ((close - e20) / e20) * 100.0

    rsi_val: float | None = float(candle["rsi14"]) if "rsi14" in df.columns else None

    pump_window = df.iloc[max(0, idx - config.lookback_hours) : idx + 1]
    pump_info = detect_pump(pump_window, config)
    pump_pct = pump_info["pump_pct"] if pump_info else 0.0
    vol_surge = pump_info["volume_surge"] if pump_info else 0.0

    meta: dict = {
        "entry_candle_time": candle["time"],
        "pump_pct": pump_pct,
        "volume_surge": vol_surge,
        "ema_distance_pct": ema_dist,
        "rsi14": rsi_val,
    }

    # ── REENTRY ──────────────────────────────────────────────────────────
    if is_reentry:
        if not pump_still_active(pump_window, config):
            return None
        if float(candle["open"]) >= previous_entry_price:
            return None
        if not hesitation_detected(df, idx):
            return None
        return {**meta, "entry_path": "REENTRY", "is_reentry": True, "use_pump_atr": True}

    # ── MHR_EXHAUSTION ───────────────────────────────────────────────────
    setup_window = df.iloc[idx - lookback : idx]
    if exhaustion_setup(setup_window):
        return {**meta, "entry_path": "MHR_EXHAUSTION", "is_reentry": False, "use_pump_atr": False}

    # ── PUMP_HESITATION ──────────────────────────────────────────────────
    if pump_info and hesitation_detected(df, idx):
        return {**meta, "entry_path": "PUMP_HESITATION", "is_reentry": False, "use_pump_atr": True}

    return None
