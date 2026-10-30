"""
Backward-compatibility shim.

All strategy logic now lives in the strategy-short/mhr_pump_short/ package.
This module re-exports the public API so that existing imports in backtest.py
and main.py continue to work without modification.

    # These two are equivalent:
    from strategy_short import MHRConfig, entry_signal
    from mhr_pump_short import MHRConfig, entry_signal
"""

import sys
import os

# Add the package parent to sys.path so "mhr_pump_short" is importable
_pkg_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy-short")
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

# ── Re-export everything from the package ─────────────────────────────────
from mhr_pump_short import (  # noqa: E402
    # Config
    MHRConfig,
    ScalpConfig,
    # Indicators
    ema as calculate_ema,
    rsi as calculate_rsi,
    atr as calculate_atr,
    candle_volume_usd as estimate_candle_volume_usd,
    # Fees
    calculate_fees as calculate_dynamic_fees,
    # Sizing
    trade_capital as calculate_trade_capital,
    sl_tp_qty as calculate_sl_tp_prices,
    structural_sl_tp_qty,
    cap_pnl,
    # Entry (MHR)
    entry_signal as unified_entry_filter,
    detect_pump,
    pump_still_active as is_pump_still_active,
    hesitation_detected as pump_hesitation_detected,
    exhaustion_setup as exhaustion_setup_ok,
    rejection_candle as entry_candle_rejection_ok,
    # Exit (MHR)
    simulate_trailing_exit,
    # Scalp
    scalp_entry as scalp_entry_filter,
    simulate_scalp_exit,
    backtest_scalp as backtest_scalp_on_1m,
    scalp_qty as calculate_scalp_qty,
    scalp_trade_capital,
    cap_scalp_pnl,
)

# Legacy aliases used by main.py CONFIG
UnifiedShortConfig = MHRConfig
DEFAULT_CONFIG = MHRConfig()
SCALP_DEFAULT_CONFIG = ScalpConfig()