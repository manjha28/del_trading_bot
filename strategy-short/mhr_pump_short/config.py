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
    capital_deployed: float = 3.33
    leverage: float = 20.0
    sl_capital_pct: float = 0.05
    pt_capital_pct: float = 1.0
    min_rr: float = 4.0
    max_capital_usage_pct: float = 0.50
    max_pnl_impact_pct: float = 2.0

    # ── ATR multipliers ──────────────────────────────────────────────────
    atr_sl_multiplier: float = 1.5
    pump_atr_sl_multiplier: float = 2.5
