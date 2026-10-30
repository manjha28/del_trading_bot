"""
Position sizing for mhr_pump_short.

Three public functions cover the full sizing lifecycle:
  trade_capital  — how much equity to deploy on the next trade
  sl_tp_qty      — stop-loss price, take-profit price, and contract quantity
  cap_pnl        — post-simulation PnL clamp to prevent unrealistic equity swings
"""

from __future__ import annotations

import math

from .config import MHRConfig


def trade_capital(available: float, config: MHRConfig) -> float:
    """
    Return the capital to deploy on the next trade.

    Allocates `max_capital_usage_pct` of available equity.  Returns 0.0 when
    the resulting amount is below 1.0 (too small to size a position).
    """
    allocated = available * config.max_capital_usage_pct
    return allocated if allocated >= 1.0 else 0.0


def sl_tp_qty(
    entry_price: float,
    config: MHRConfig,
    capital: float | None = None,
    atr_value: float | None = None,
    use_pump_atr: bool = False,
) -> tuple[float, float, int]:
    """
    Compute stop-loss price, take-profit price, and contract quantity for a short entry.

    Sizing logic
    ------------
    1. Stop-loss distance is ATR-based when atr_value is provided:
           sl_dist = atr_value × (pump_atr_sl_multiplier if use_pump_atr else atr_sl_multiplier)
       Quantity is the smaller of:
           - max loss budget / sl_dist   (keeps max loss ≤ sl_capital_pct × capital)
           - max notional / entry_price  (keeps position within leverage limit)
       Falls back to filling the full notional when no ATR is available.

    2. Take-profit distance satisfies both:
           tp_target = max(pt_capital_pct × capital, min_rr × sl_loss)
           tp_price  = entry_price − tp_target / qty

    3. Quantity is floored to the nearest integer and hard-capped at 10,000,000.

    Parameters
    ----------
    entry_price  : Short entry price (must be > 0).
    config       : MHRConfig instance.
    capital      : Override for trade capital; falls back to config.capital_deployed.
    atr_value    : Current ATR value; None triggers fallback to full-notional sizing.
    use_pump_atr : When True, use the wider pump ATR multiplier.

    Returns
    -------
    (sl_price, tp_price, qty)

    Raises
    ------
    ValueError if entry_price ≤ 0, capital ≤ 0, or the resulting quantity < 1.
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive, got {entry_price}")
    cap = capital if capital is not None else config.capital_deployed
    if cap <= 0:
        raise ValueError(f"capital must be positive, got {cap}")

    max_notional = cap * config.leverage
    sl_loss = cap * config.sl_capital_pct
    multiplier = config.pump_atr_sl_multiplier if use_pump_atr else config.atr_sl_multiplier

    if atr_value and atr_value > 0:
        sl_dist = atr_value * multiplier
        raw_qty = min(sl_loss / sl_dist, max_notional / entry_price)
    else:
        raw_qty = max_notional / entry_price
        sl_dist = None

    qty = min(math.floor(raw_qty), 10_000_000)
    if qty < 1:
        raise ValueError("computed quantity < 1 — trade capital or ATR too small")

    # Guard against floating-point overshoot of notional limit
    if qty * entry_price > max_notional * 1.01:
        qty = math.floor(max_notional / entry_price)
        if qty < 1:
            raise ValueError("quantity < 1 after notional cap")

    sl_price = entry_price + (sl_dist if sl_dist is not None else sl_loss / qty)
    tp_target = max(cap * config.pt_capital_pct, sl_loss * config.min_rr)
    tp_price = entry_price - (tp_target / qty)

    return float(sl_price), float(tp_price), int(qty)


def structural_sl_tp_qty(
    entry_price: float,
    prev_high: float,
    config: MHRConfig,
    capital: float | None = None,
    atr_value: float | None = None,
    use_pump_atr: bool = False,
) -> tuple[float, float, int]:
    """
    Compute SL, TP, and qty using the previous candle high as the stop level.

    SL is placed at prev_high (no buffer).
    qty = floor(max_loss / (prev_high − entry_price)).

    Falls back to sl_tp_qty() (ATR-based) when:
      - prev_high ≤ entry_price (gap-up; structure is below entry)
      - resulting qty < 1 (candle wick too wide for the given capital)

    Parameters
    ----------
    entry_price  : Short entry price.
    prev_high    : High of the candle immediately before the entry candle.
    config       : MHRConfig instance.
    capital      : Trade capital override; defaults to config.capital_deployed.
    atr_value    : Current ATR value passed through to fallback sl_tp_qty().
    use_pump_atr : Passed through to fallback sl_tp_qty().

    Returns
    -------
    (sl_price, tp_price, qty)

    Raises
    ------
    ValueError if both structural and ATR sizing fail to produce qty ≥ 1.
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive, got {entry_price}")
    cap = capital if capital is not None else config.capital_deployed
    if cap <= 0:
        raise ValueError(f"capital must be positive, got {cap}")

    if prev_high > entry_price:
        sl_dist = prev_high - entry_price
        max_loss = cap * config.sl_capital_pct
        raw_qty = math.floor(max_loss / sl_dist)
        if raw_qty >= 1:
            qty = min(raw_qty, math.floor(cap * config.leverage / entry_price))
            if qty >= 1:
                tp_target = max(cap * config.pt_capital_pct, max_loss * config.min_rr)
                tp_price = entry_price - tp_target / qty
                return float(prev_high), float(tp_price), int(qty)

    return sl_tp_qty(entry_price, config, capital, atr_value, use_pump_atr)


def cap_pnl(net_pnl: float, trade_cap: float, config: MHRConfig) -> float:
    """
    Clamp net PnL to [-trade_cap, trade_cap × max_pnl_impact_pct].

    Prevents a single simulated trade from swinging equity beyond what the
    leverage and capital limits would allow in live conditions.
    """
    return max(min(net_pnl, trade_cap * config.max_pnl_impact_pct), -trade_cap)
