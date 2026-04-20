"""
mhr_pump_short — unified short-only strategy package.

Public API re-exported here for clean imports:

    from strategy-short.mhr_pump_short import MHRConfig, entry_signal, ...

Modules
-------
config      MHRConfig, ScalpConfig dataclasses
indicators  ema, rsi, atr, candle_volume_usd
fees        calculate_fees, BTC_ETH_SYMBOLS
sizing      trade_capital, sl_tp_qty, structural_sl_tp_qty, cap_pnl
entry       entry_signal, detect_pump, pump_still_active, hesitation_detected,
            exhaustion_setup, rejection_candle
exit        simulate_trailing_exit
scalp       scalp_entry, detect_long, detect_short, simulate_scalp_exit,
            backtest_scalp, scalp_qty, scalp_trade_capital
"""

# Config
from .config import MHRConfig, ScalpConfig

# Indicators
from .indicators import ema, rsi, atr, candle_volume_usd

# Fees
from .fees import calculate_fees, BTC_ETH_SYMBOLS

# Sizing
from .sizing import trade_capital, sl_tp_qty, structural_sl_tp_qty, cap_pnl

# Entry (MHR)
from .entry import (
    entry_signal,
    detect_pump,
    pump_still_active,
    hesitation_detected,
    exhaustion_setup,
    rejection_candle,
)

# Exit (MHR)
from .exit import simulate_trailing_exit

# Scalp
from .scalp import (
    scalp_entry,
    detect_long,
    detect_short,
    simulate_scalp_exit,
    backtest_scalp,
    scalp_qty,
    scalp_trade_capital,
    cap_scalp_pnl,
)

__all__ = [
    # Config
    "MHRConfig",
    "ScalpConfig",
    # Indicators
    "ema",
    "rsi",
    "atr",
    "candle_volume_usd",
    # Fees
    "calculate_fees",
    "BTC_ETH_SYMBOLS",
    # Sizing
    "trade_capital",
    "sl_tp_qty",
    "structural_sl_tp_qty",
    "cap_pnl",
    # Entry
    "entry_signal",
    "detect_pump",
    "pump_still_active",
    "hesitation_detected",
    "exhaustion_setup",
    "rejection_candle",
    # Exit
    "simulate_trailing_exit",
    # Scalp
    "scalp_entry",
    "detect_long",
    "detect_short",
    "simulate_scalp_exit",
    "backtest_scalp",
    "scalp_qty",
    "scalp_trade_capital",
    "cap_scalp_pnl",
]