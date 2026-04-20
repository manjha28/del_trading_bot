"""
Configuration dataclasses for all strategies.

MHRConfig  — mhr_pump_short (1h candles, short only)
ScalpConfig — scalp_pullback (5m candles, both directions)
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MHRConfig:
    """
    Immutable configuration for the mhr_pump_short strategy.

    Entry thresholds
    ----------------
    lookback_green_candles   Number of consecutive green candles required for the
                             MHR_EXHAUSTION entry path.
    min_pump_pct             Minimum 24-hour price gain (%) to classify as a pump.
    lookback_hours           Candle window used for both pump detection and reentry checks.
    min_volume_surge_pct     Minimum ratio of recent-to-prior average volume confirming a pump.
    max_reentries            Maximum reentries allowed per pump event.
    reentry_cooldown_candles Minimum candles between a stop-out and the next reentry attempt.

    Capital / leverage
    ------------------
    capital_deployed         Nominal starting capital used when no live equity is provided.
    leverage                 Exchange leverage multiplier.
    sl_capital_pct           Fraction of trade capital risked on the stop-loss distance.
    pt_capital_pct           Minimum profit target as a fraction of trade capital.
    min_rr                   Minimum reward-to-risk ratio enforced on the take-profit price.
    max_capital_usage_pct    Maximum fraction of available equity deployed per trade.
    max_pnl_impact_pct       PnL cap multiplier — prevents unrealistic simulation swings.

    ATR multipliers
    ---------------
    atr_sl_multiplier        ATR multiplier for MHR_EXHAUSTION stop-loss distance.
    pump_atr_sl_multiplier   ATR multiplier for PUMP_HESITATION and REENTRY stop-loss distance
                             (wider to accommodate pump-driven volatility).
    """
    # ── Entry thresholds ─────────────────────────────────────────────────
    lookback_green_candles: int = 4
    min_pump_pct: float = 25.0
    lookback_hours: int = 24
    min_volume_surge_pct: float = 2.0
    max_reentries: int = 3
    reentry_cooldown_candles: int = 1

    # ── Capital / leverage ───────────────────────────────────────────────
    capital_deployed: float = 100.0
    leverage: float = 20.0
    sl_capital_pct: float = 0.05
    pt_capital_pct: float = 1.0
    min_rr: float = 4.0
    max_capital_usage_pct: float = 0.50
    max_pnl_impact_pct: float = 2.0

    # ── ATR multipliers ──────────────────────────────────────────────────
    atr_sl_multiplier: float = 1.5
    pump_atr_sl_multiplier: float = 2.5


@dataclass(frozen=True)
class ScalpConfig:
    """
    Immutable configuration for the scalp_pullback strategy.

    Runs on 5-minute candles with 1-hour EMA50 as trend filter.
    Trades both long and short depending on trend direction.

    Entry
    -----
    ema_5m_length           EMA period on 5-minute execution timeframe.
    ema_1h_length           EMA period on 1-hour trend filter (resampled from 5m).
    min_pullback_candles    Minimum consecutive opposing candles before signal.
    max_pullback_candles    Maximum consecutive opposing candles to consider.
    vol_lookback            Bars for computing average volume (signal candle must exceed avg).
    sl_buffer_pct           Buffer added beyond pullback extreme for stop-loss placement.

    Exit
    ----
    tp1_rr                  R-multiple at which 50% of position is closed.
    tp1_qty_pct             Fraction of position taken off at TP1.
    trail_lock_rr           After TP1, trail locks profit at best_r minus this buffer.

    Capital / leverage
    ------------------
    Same semantics as MHRConfig — fraction of equity, leverage, risk caps.
    """
    # ── Entry ────────────────────────────────────────────────────────────
    ema_5m_length: int = 20
    ema_1h_length: int = 50
    min_pullback_candles: int = 3
    max_pullback_candles: int = 4
    sl_buffer_pct: float = 0.0015
    vol_lookback: int = 20

    # ── Exit ─────────────────────────────────────────────────────────────
    tp1_rr: float = 1.5
    tp1_qty_pct: float = 0.50
    trail_lock_rr: float = 1.0

    # ── Capital / leverage ───────────────────────────────────────────────
    capital_deployed: float = 100.0
    leverage: float = 20.0
    sl_capital_pct: float = 0.02
    max_capital_usage_pct: float = 0.50
    max_pnl_impact_pct: float = 2.0

    # ── Trade management ─────────────────────────────────────────────────
    min_volume: float = 0.0
    cooldown_candles: int = 3
    max_trades_per_session: int = 100
    max_holding_candles: int = 60