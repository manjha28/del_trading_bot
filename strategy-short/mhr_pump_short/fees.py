"""
Dynamic fee model for mhr_pump_short.

Delta Exchange charges a single-side maker/taker fee.  For very short-duration
trades the exit leg may be waived or negligible, so the model distinguishes
between a fast-exit (entry fee only) and a normal round-trip.

Fast-exit thresholds
--------------------
BTC / ETH pairs : 30 minutes  (higher liquidity → quicker fills)
All other coins : 15 minutes
"""

from __future__ import annotations


BTC_ETH_SYMBOLS: frozenset[str] = frozenset({
    "BTCUSD", "ETHUSD", "BTCUSDT", "ETHUSDT",
    "BTCPERP", "ETHPERP", "XBTUSD",
})

_FAST_THRESHOLD_BTC_ETH: float = 30.0   # minutes
_FAST_THRESHOLD_OTHER: float = 15.0     # minutes
_SINGLE_SIDE_RATE: float = 0.0005       # 0.05 %


def calculate_fees(
    symbol: str,
    entry_price: float,
    exit_price: float,
    qty: float,
    charge_rate: float,
    entry_time: object,
    exit_time: object,
) -> float:
    """
    Return the total fee for a completed trade.

    Parameters
    ----------
    symbol       : Trading symbol (used to select the fast-exit threshold).
    entry_price  : Fill price at trade open.
    exit_price   : Fill price at trade close.
    qty          : Contract quantity.
    charge_rate  : Round-trip fee rate (e.g. 0.0005 for 0.05 %).
    entry_time   : Trade open timestamp — datetime, pd.Timestamp, or Unix float.
    exit_time    : Trade close timestamp — same type as entry_time.

    Returns
    -------
    Fee in the same currency as price × qty (i.e. USD for USD-margined contracts).
    """
    fast_threshold = (
        _FAST_THRESHOLD_BTC_ETH
        if symbol.upper() in BTC_ETH_SYMBOLS
        else _FAST_THRESHOLD_OTHER
    )
    duration = _duration_minutes(entry_time, exit_time)
    entry_notional = abs(entry_price * qty)
    exit_notional = abs(exit_price * qty)

    if duration <= fast_threshold:
        return entry_notional * _SINGLE_SIDE_RATE
    return (entry_notional + exit_notional) * charge_rate


# ── Internal helpers ──────────────────────────────────────────────────────

def _duration_minutes(entry_time: object, exit_time: object) -> float:
    """Convert two timestamps to a duration in minutes.  Returns inf on failure."""
    try:
        if hasattr(entry_time, "timestamp") and hasattr(exit_time, "timestamp"):
            return (exit_time.timestamp() - entry_time.timestamp()) / 60.0  # type: ignore[union-attr]
        if isinstance(entry_time, (int, float)) and isinstance(exit_time, (int, float)):
            return (float(exit_time) - float(entry_time)) / 60.0
    except Exception:
        pass
    return float("inf")
