"""Validated environment configuration for the independent VPVR subsystem.

Defaults are research/paper-only. This configuration does not enable live order
execution; no live order adapter is included in this release.
"""
from __future__ import annotations
from dataclasses import dataclass
import os

@dataclass(frozen=True)
class VPVRConfig:
    profile_rows: int = 60
    value_area_pct: float = 0.70
    ema_length: int = 20
    risk_per_trade_pct: float = 0.005
    daily_loss_limit_pct: float = 0.015
    max_consecutive_losses: int = 3
    min_reward_risk: float = 2.0
    default_leverage: float = 10.0
    max_leverage: float = 20.0
    max_positions: int = 1
    stop_buffer_pct: float = 0.001
    min_volume_multiple: float = 1.0
    node_smoothing_window: int = 3
    node_threshold_pct: float = 0.15
    max_data_age_seconds: int = 90
    max_spread_pct: float = 0.20
    max_abs_funding_rate: float = 0.001
    initial_paper_balance: float = 1000.0
    fee_rate: float = 0.0005
    slippage_bps: float = 2.0
    funding_rate_per_8h: float = 0.0
    profile_lookback_15m: int = 96
    max_hold_minutes: int = 180
    paper_state_file: str = "data/vpvr_paper_state.json"

    @classmethod
    def from_env(cls) -> "VPVRConfig":
        return cls(
            profile_rows=int(os.getenv("VPVR_PROFILE_ROWS", "60")),
            value_area_pct=float(os.getenv("VPVR_VALUE_AREA_PCT", "0.70")),
            ema_length=int(os.getenv("VPVR_EMA_LENGTH", "20")),
            risk_per_trade_pct=float(os.getenv("VPVR_RISK_PER_TRADE_PCT", "0.005")),
            daily_loss_limit_pct=float(os.getenv("VPVR_DAILY_LOSS_LIMIT_PCT", "0.015")),
            max_consecutive_losses=int(os.getenv("VPVR_MAX_CONSECUTIVE_LOSSES", "3")),
            min_reward_risk=float(os.getenv("VPVR_MIN_REWARD_RISK", "2.0")),
            default_leverage=float(os.getenv("VPVR_DEFAULT_LEVERAGE", "10")),
            max_leverage=float(os.getenv("VPVR_MAX_LEVERAGE", "20")),
            max_positions=int(os.getenv("VPVR_MAX_POSITIONS", "1")),
            stop_buffer_pct=float(os.getenv("VPVR_STOP_BUFFER_PCT", "0.001")),
            min_volume_multiple=float(os.getenv("VPVR_MIN_VOLUME_MULTIPLE", "1.0")),
            node_smoothing_window=int(os.getenv("VPVR_NODE_SMOOTHING_WINDOW", "3")),
            node_threshold_pct=float(os.getenv("VPVR_NODE_THRESHOLD_PCT", "0.15")),
            max_data_age_seconds=int(os.getenv("VPVR_MAX_DATA_AGE_SECONDS", "90")),
            max_spread_pct=float(os.getenv("VPVR_MAX_SPREAD_PCT", "0.20")),
            max_abs_funding_rate=float(os.getenv("VPVR_MAX_ABS_FUNDING_RATE", "0.001")),
            initial_paper_balance=float(os.getenv("VPVR_PAPER_BALANCE", "1000")),
            fee_rate=float(os.getenv("VPVR_FEE_RATE", "0.0005")),
            slippage_bps=float(os.getenv("VPVR_SLIPPAGE_BPS", "2")),
            funding_rate_per_8h=float(os.getenv("VPVR_FUNDING_RATE_PER_8H", "0")),
            profile_lookback_15m=int(os.getenv("VPVR_PROFILE_LOOKBACK_15M", "96")),
            max_hold_minutes=int(os.getenv("VPVR_MAX_HOLD_MINUTES", "180")),
            paper_state_file=os.getenv("VPVR_PAPER_STATE_FILE", "data/vpvr_paper_state.json"),
        ).validate()

    def validate(self) -> "VPVRConfig":
        if not 50 <= self.profile_rows <= 70:
            raise ValueError("VPVR_PROFILE_ROWS must be between 50 and 70")
        if not 0.5 <= self.value_area_pct <= 0.9:
            raise ValueError("value_area_pct must be in [0.5, 0.9]")
        if not 0.0025 <= self.risk_per_trade_pct <= 0.005:
            raise ValueError("risk per trade must be 0.25%-0.5%")
        if not 0 < self.daily_loss_limit_pct <= 0.015:
            raise ValueError("daily loss limit must be in (0, 1.5%]")
        if self.max_consecutive_losses < 1 or self.max_positions < 1:
            raise ValueError("loss and position limits must be positive")
        if self.default_leverage < 1 or self.max_leverage > 20 or self.default_leverage > self.max_leverage:
            raise ValueError("leverage must be between 1 and max 20x")
        if self.min_reward_risk < 2:
            raise ValueError("minimum reward/risk cannot be below 2")
        if self.node_smoothing_window < 1 or not 0 < self.node_threshold_pct < 1:
            raise ValueError("invalid volume-node configuration")
        if self.max_data_age_seconds < 1 or self.profile_lookback_15m < 30 or self.max_hold_minutes < 1:
            raise ValueError("invalid freshness/profile lookback")
        if self.max_spread_pct <= 0 or self.max_abs_funding_rate < 0:
            raise ValueError("invalid spread or maximum absolute funding rate")
        if self.initial_paper_balance <= 0 or self.fee_rate < 0 or self.slippage_bps < 0:
            raise ValueError("invalid paper account or cost configuration")
        if not self.paper_state_file:
            raise ValueError("paper state file path cannot be empty")
        return self
