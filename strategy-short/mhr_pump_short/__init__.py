"""
mhr_pump_short — Multi-Hour Runner Pump Short strategy package.

Strategy overview
-----------------
Short perpetual futures on Delta Exchange India using one of three entry paths:

  MHR_EXHAUSTION   — N consecutive green candles signal an exhausted uptrend.
  PUMP_HESITATION  — A ≥50% pump with volume surge is followed by a hesitation
                     candle, indicating the move may be reversing.
  REENTRY          — After a stop-out, re-enter when the pump is still active
                     and price has pulled back below the prior entry.

Exit model: 50% partial TP at 2R, trailing stop-loss on the remainder.

Usage
-----
Add the parent ``strategy-short`` directory to sys.path, then import directly:

    import sys
    sys.path.insert(0, "strategy-short")

    from mhr_pump_short import MHRConfig, entry_signal, simulate_trailing_exit

Module layout
-------------
config.py      MHRConfig — strategy configuration dataclass
indicators.py  ema, rsi, atr, candle_volume_usd — technical indicators
fees.py        calculate_fees, BTC_ETH_SYMBOLS — dynamic fee model
sizing.py      trade_capital, sl_tp_qty, cap_pnl — position sizing
entry.py       entry_signal — unified entry filter + all helper conditions
exit.py        simulate_trailing_exit — trailing exit simulator
"""

from .config import MHRConfig
from .indicators import ema, rsi, atr, candle_volume_usd
from .fees import calculate_fees, BTC_ETH_SYMBOLS
from .sizing import trade_capital, sl_tp_qty, structural_sl_tp_qty, cap_pnl
from .entry import (
    entry_signal,
    detect_pump,
    pump_still_active,
    hesitation_detected,
    exhaustion_setup,
    rejection_candle,
)
from .exit import simulate_trailing_exit

__all__ = [
    # Config
    "MHRConfig",
    # Entry
    "entry_signal",
    "detect_pump",
    "pump_still_active",
    "hesitation_detected",
    "exhaustion_setup",
    "rejection_candle",
    # Exit
    "simulate_trailing_exit",
    # Sizing
    "sl_tp_qty",
    "structural_sl_tp_qty",
    "trade_capital",
    "cap_pnl",
    # Fees
    "calculate_fees",
    "BTC_ETH_SYMBOLS",
    # Indicators
    "ema",
    "rsi",
    "atr",
    "candle_volume_usd",
]
