from __future__ import annotations

from dataclasses import dataclass
import importlib
import math
from pathlib import Path
import sys
from typing import Any, Protocol

import pandas as pd

from .strategies import StrategySignal


@dataclass(frozen=True)
class ExitSimulationContext:
    signal: StrategySignal
    candles: pd.DataFrame
    entry_index: int
    entry_price: float
    stop_price: float
    quantity: float
    max_hold: int = 100
    candles_1m: pd.DataFrame | None = None

    def __post_init__(self) -> None:
        if self.candles.empty or "time" not in self.candles:
            raise ValueError("exit simulation requires timestamped candles")
        if not 0 <= self.entry_index < len(self.candles):
            raise ValueError("entry_index is outside the candle frame")
        for name in ("entry_price", "stop_price", "quantity"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.max_hold < 1:
            raise ValueError("max_hold must be positive")
        times = pd.to_datetime(self.candles["time"], errors="raise")
        if times.isna().any() or times.dt.tz is None:
            raise ValueError("exit candles must have valid timezone-aware timestamps")
        if not times.is_monotonic_increasing or times.duplicated().any():
            raise ValueError("exit candles must be strictly chronological and unique")


@dataclass(frozen=True)
class NormalizedExitOutcome:
    status: str
    strategy_id: str
    product_id: str
    symbol: str
    direction: str
    decision_time: str
    exit_reason: str
    exit_time: str
    exit_price: float | None
    quantity: float
    realized_pnl: float
    bars_held: int
    final_stop: float | None = None
    partial_pnl: float = 0.0
    remaining_pnl: float = 0.0
    partial_quantity: float = 0.0
    model: str = ""

    def as_record(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "product_id": self.product_id,
            "symbol": self.symbol,
            "decision_time": self.decision_time,
            "status": self.status,
            "direction": self.direction,
            "exit_reason": self.exit_reason,
            "exit_time": self.exit_time,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "gross_pnl": self.realized_pnl,
            "net_pnl": None,
            "pnl_basis": "legacy exit simulator PnL before fees and execution costs",
            "bars_held": self.bars_held,
            "final_stop": self.final_stop,
            "partial_pnl": self.partial_pnl,
            "remaining_pnl": self.remaining_pnl,
            "partial_quantity": self.partial_quantity,
            "model": self.model,
        }


class StrategyExitAdapter(Protocol):
    def simulate(self, context: ExitSimulationContext) -> NormalizedExitOutcome: ...


@dataclass(frozen=True)
class VPVRExitPlan:
    """Normalized description of VPVR's existing paper-engine exit rules."""

    strategy_id: str
    direction: str
    initial_stop: float
    targets: tuple[float, ...]
    target_fractions: tuple[float, ...]
    runner_fraction: float
    stop_updates: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    execution_owner: str = "vpvr.paper.PaperAccount"


def _legacy_mhr_api():
    package_root = Path(__file__).resolve().parents[1] / "strategy-short"
    package_path = str(package_root)
    if package_path not in sys.path:
        sys.path.insert(0, package_path)
    return importlib.import_module("mhr_pump_short")


class MHRExitAdapter:
    """Normalize MHR's native multi-leg trailing-exit result without changing it."""

    def __init__(self, config: Any = None):
        self.api = _legacy_mhr_api()
        self.config = config or self.api.MHRConfig()

    def simulate(self, context: ExitSimulationContext) -> NormalizedExitOutcome:
        if context.signal.direction != "SHORT":
            raise ValueError("MHR trailing exit supports SHORT positions only")
        if not float(context.quantity).is_integer():
            raise ValueError("MHR trailing exit requires an integer quantity")
        raw = self.api.simulate_trailing_exit(
            context.candles,
            context.entry_index,
            context.entry_price,
            context.stop_price,
            int(context.quantity),
            context.max_hold,
            bulk_1m=context.candles_1m,
        )
        partial_pnl, remaining_pnl, reason, timestamp, final_stop, bars, partial_qty = raw
        risk = context.stop_price - context.entry_price
        partial_price = context.entry_price - 2 * risk
        remaining_qty = context.quantity - partial_qty
        final_price = (
            context.entry_price - remaining_pnl / remaining_qty
            if remaining_qty > 0
            else (partial_price if partial_qty else None)
        )
        return NormalizedExitOutcome(
            status="simulated",
            strategy_id=context.signal.strategy_id,
            product_id=context.signal.product_id,
            symbol=context.signal.symbol,
            direction=context.signal.direction,
            decision_time=context.signal.decision_time.isoformat(),
            exit_reason=str(reason),
            exit_time=pd.Timestamp(timestamp).isoformat(),
            exit_price=final_price,
            quantity=float(context.quantity),
            realized_pnl=float(partial_pnl + remaining_pnl),
            bars_held=int(bars),
            final_stop=float(final_stop),
            partial_pnl=float(partial_pnl),
            remaining_pnl=float(remaining_pnl),
            partial_quantity=float(partial_qty),
            model="mhr_trailing",
        )


class ScalpPullbackExitAdapter:
    """Normalize the existing scalp simulator's aggregate exit result."""

    def __init__(self, config: Any = None):
        self.api = _legacy_mhr_api()
        self.config = config or self.api.ScalpConfig()

    def simulate(self, context: ExitSimulationContext) -> NormalizedExitOutcome:
        result = self.api.simulate_scalp_exit(
            context.candles,
            context.entry_index,
            context.entry_price,
            context.stop_price,
            context.quantity,
            context.signal.direction,
            self.config,
        )
        pnl, reason, timestamp, price, bars = result
        return NormalizedExitOutcome(
            status="simulated",
            strategy_id=context.signal.strategy_id,
            product_id=context.signal.product_id,
            symbol=context.signal.symbol,
            direction=context.signal.direction,
            decision_time=context.signal.decision_time.isoformat(),
            exit_reason=str(reason),
            exit_time=pd.Timestamp(timestamp).isoformat(),
            exit_price=float(price),
            quantity=float(context.quantity),
            realized_pnl=float(pnl),
            bars_held=int(bars),
            model="scalp_partial_trailing",
        )


class FastSlowMAExitAdapter:
    """Normalize Fast/Slow MA's stop/target/crossover exit simulator."""

    def __init__(self, config: Any = None):
        self.strategy = importlib.import_module("long_strategy")
        self.config = config or self.strategy.FAST_SLOW_DEFAULT_CONFIG

    def simulate(self, context: ExitSimulationContext) -> NormalizedExitOutcome:
        if not context.signal.target_prices:
            raise ValueError("Fast/Slow MA exit requires a target price")
        if not float(context.quantity).is_integer():
            raise ValueError("Fast/Slow MA exit requires an integer quantity")
        result = self.strategy.simulate_fast_slow_exit(
            context.candles,
            context.entry_index,
            context.entry_price,
            context.stop_price,
            context.signal.target_prices[0],
            int(context.quantity),
            context.signal.direction,
            self.config,
            max_hold=context.max_hold,
        )
        pnl, reason, timestamp, price, bars = result
        return NormalizedExitOutcome(
            status="simulated",
            strategy_id=context.signal.strategy_id,
            product_id=context.signal.product_id,
            symbol=context.signal.symbol,
            direction=context.signal.direction,
            decision_time=context.signal.decision_time.isoformat(),
            exit_reason=str(reason),
            exit_time=pd.Timestamp(timestamp).isoformat(),
            exit_price=float(price),
            quantity=float(context.quantity),
            realized_pnl=float(pnl),
            bars_held=int(bars),
            model="fast_slow_stop_target_crossover",
        )


def vpvr_exit_plan(signal: StrategySignal) -> VPVRExitPlan:
    if signal.strategy_id not in {"poc_reclaim", "value_area_rejection", "lvn_breakout"}:
        raise ValueError(f"unsupported VPVR strategy: {signal.strategy_id}")
    if signal.stop_price is None or not signal.target_prices:
        raise ValueError("VPVR exit plan requires a stop and at least one target")
    targets = signal.target_prices[:3]
    fractions = (1.0,) if len(targets) == 1 else (
        (0.30, 0.45) if len(targets) == 2 else (0.30, 0.45, 0.25)
    )
    return VPVRExitPlan(
        strategy_id=signal.strategy_id,
        direction=signal.direction,
        initial_stop=float(signal.stop_price),
        targets=tuple(float(target) for target in targets),
        target_fractions=fractions,
        runner_fraction=max(0.0, 1.0 - sum(fractions)),
        stop_updates=(
            "after target 1: move stop to entry",
            "after target 2: move stop to target 1",
            "after target 2: trail remaining quantity by the prior completed 1m candle extreme",
        ),
        invalidation_conditions=signal.invalidation_conditions,
    )
