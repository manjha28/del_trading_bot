"""
Exit simulation for mhr_pump_short.

Implements the trailing exit model:
  - At 2R: take 50% of position (partial TP).
  - After partial fill: trail stop-loss to breakeven, then trail by (best_R - 1) × risk.
  - Max hold: exit at close of the final allowed candle.

This module is standalone — it has no dependency on config.py, entry.py, or
sizing.py.  All inputs are plain scalars and DataFrames.
"""

from __future__ import annotations

import pandas as pd


def simulate_trailing_exit(
    candles: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    initial_sl: float,
    qty: int,
    max_hold: int,
    bulk_1m: pd.DataFrame | None = None,
    use_1m_resolution: bool = True,
) -> tuple[float, float, str, object, float, int, int]:
    """
    Simulate the mhr_pump_short exit model for a SHORT position.

    Exit model
    ----------
    - Risk (R) = initial_sl − entry_price  (positive for a valid short setup)
    - Partial TP at 2R: close qty//2 (q1) when price drops 2 × risk below entry.
    - After partial fill: trail SL begins at entry (breakeven).
    - Trail rule: trail_sl = min(trail_sl, entry − (best_R − 1) × risk)
      i.e. the SL trails 1R behind the best price reached.
    - Max hold: if neither SL nor full TP is hit within max_hold candles,
      close remaining position at the last candle's close.

    When SL and TP hit on the same 1-hour candle, 1-minute data is used to
    determine which occurred first.  If 1m data is unavailable the candle
    open is used as a proximity proxy.

    Parameters
    ----------
    candles          : Prepared 1h OHLCV DataFrame (must include 'high', 'low', 'close').
    entry_idx        : Index of the entry candle in `candles`.
    entry_price      : Short entry fill price.
    initial_sl       : Initial stop-loss price (must be > entry_price for a short).
    qty              : Total contract quantity to trade.
    max_hold         : Maximum number of candles to hold the position.
    bulk_1m          : Optional 1-minute OHLCV DataFrame for intra-candle resolution.
    use_1m_resolution: Whether to use bulk_1m for SL/TP ordering; ignored if bulk_1m is None.

    Returns
    -------
    Tuple of:
      partial_pnl  : PnL from the partial TP leg (0.0 if no partial fill occurred).
      remainder_pnl: PnL from the remaining position leg.
      exit_reason  : One of the exit reason constants below.
      exit_time    : Timestamp of the closing candle.
      final_sl     : Stop-loss price at time of exit (initial_sl if never trailed).
      bars_held    : Number of candles held.
      partial_qty  : Quantity filled at partial TP (qty//2, or 0 if no partial fill).

    Exit reasons
    ------------
    SL_OR_TRAIL            — Stop hit with no prior partial fill.
    TRAIL_STOP_AFTER_PARTIAL — Stop hit after partial TP was already taken.
    MAX_HOLD_EXIT          — Max hold reached with no partial fill.
    MAX_HOLD_AFTER_PARTIAL — Max hold reached after partial fill.
    INVALID_RISK           — initial_sl ≤ entry_price; trade cannot be sized.
    """
    risk = initial_sl - entry_price
    if risk <= 0:
        return 0.0, 0.0, "INVALID_RISK", candles.iloc[entry_idx]["time"], initial_sl, 0, 0

    q1 = qty // 2       # partial TP quantity
    q2 = qty - q1       # trailing remainder quantity
    tp_price = entry_price - 2.0 * risk  # 2R target for partial fill

    partial_pnl = 0.0
    partial_done = False
    trail_sl = initial_sl
    best_r = 0.0
    bars = 0
    max_idx = min(len(candles) - 1, entry_idx + max_hold)

    for idx in range(entry_idx + 1, max_idx + 1):
        bars += 1
        c = candles.iloc[idx]
        ch, cl = float(c["high"]), float(c["low"])

        sl_hit = ch >= trail_sl
        fav_r = max(0.0, (entry_price - cl) / risk)
        tp_hit = (not partial_done) and fav_r >= 2.0

        # ── Both SL and TP hit on the same candle ────────────────────────
        if sl_hit and tp_hit:
            order = (
                _resolve_1m(c["time"], trail_sl, tp_price, bulk_1m)
                if use_1m_resolution and bulk_1m is not None
                else "UNKNOWN"
            )
            if order == "UNKNOWN":
                o = float(c["open"])
                order = "TP_FIRST" if abs(o - tp_price) < abs(o - trail_sl) else "SL_FIRST"

            if order == "TP_FIRST":
                partial_pnl = (entry_price - tp_price) * q1
                return (
                    partial_pnl,
                    (entry_price - trail_sl) * q2,
                    "TRAIL_STOP_AFTER_PARTIAL",
                    c["time"], trail_sl, bars, q1,
                )
            # SL_FIRST — no partial fill has occurred yet (tp_hit requires partial_done=False)
            return 0.0, (entry_price - trail_sl) * qty, "SL_OR_TRAIL", c["time"], trail_sl, bars, 0

        # ── SL only ──────────────────────────────────────────────────────
        if sl_hit:
            if partial_done:
                return (
                    partial_pnl,
                    (entry_price - trail_sl) * q2,
                    "TRAIL_STOP_AFTER_PARTIAL",
                    c["time"], trail_sl, bars, q1,
                )
            return 0.0, (entry_price - trail_sl) * qty, "SL_OR_TRAIL", c["time"], trail_sl, bars, 0

        # ── Update best R and trail ───────────────────────────────────────
        best_r = max(best_r, fav_r)

        if not partial_done and q1 > 0 and fav_r >= 2.0:
            partial_done = True
            partial_pnl = (entry_price - tp_price) * q1

        if best_r > 1.0:
            # Trail SL 1R behind the best price reached
            trail_sl = min(trail_sl, entry_price - (best_r - 1.0) * risk)

    # ── Max hold reached ─────────────────────────────────────────────────
    last = candles.iloc[max_idx]
    close_price = float(last["close"])
    remainder_pnl = (entry_price - close_price) * (q2 if partial_done else qty)

    if partial_done:
        return partial_pnl, remainder_pnl, "MAX_HOLD_AFTER_PARTIAL", last["time"], trail_sl, bars, q1
    return 0.0, remainder_pnl, "MAX_HOLD_EXIT", last["time"], trail_sl, bars, 0


# ── Internal helpers ──────────────────────────────────────────────────────

def _resolve_1m(
    candle_time: object,
    sl_price: float,
    tp_price: float,
    bulk_1m: pd.DataFrame,
) -> str:
    """
    Use 1-minute candle data to determine whether SL or TP was hit first
    within the hour identified by `candle_time`.

    Returns 'SL_FIRST', 'TP_FIRST', or 'UNKNOWN' (when no 1m data covers
    the period or no level was touched within it).

    Convention (short position):
      SL is hit when 1m high ≥ sl_price.
      TP is hit when 1m low  ≤ tp_price.
    """
    if bulk_1m is None or bulk_1m.empty:
        return "UNKNOWN"

    try:
        ts = candle_time.timestamp() if hasattr(candle_time, "timestamp") else float(candle_time)  # type: ignore[union-attr]
        hour_start = pd.Timestamp(ts, unit="s", tz="UTC")
    except Exception:
        return "UNKNOWN"

    hour_end = hour_start + pd.Timedelta(hours=1)
    mask = (bulk_1m["time"] >= hour_start) & (bulk_1m["time"] < hour_end)
    within = bulk_1m[mask]

    if within.empty:
        return "UNKNOWN"

    for _, mc in within.iterrows():
        h, l = float(mc["high"]), float(mc["low"])
        sl_hit = h >= sl_price
        tp_hit = l <= tp_price
        if sl_hit and tp_hit:
            o = float(mc["open"])
            return "TP_FIRST" if abs(o - tp_price) < abs(o - sl_price) else "SL_FIRST"
        if sl_hit:
            return "SL_FIRST"
        if tp_hit:
            return "TP_FIRST"

    return "UNKNOWN"
