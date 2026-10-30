from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import math
from typing import Mapping, Sequence

from .market_data import Candle, Instrument
from .simulation import PaperSimulationConfig, PaperSimulator, PaperTradeResult
from .strategies import StrategySignal


@dataclass(frozen=True)
class PortfolioSimulationConfig:
    max_open_positions: int = 3
    max_portfolio_risk_pct: float = 0.015
    max_gross_leverage: float = 2.0
    trade: PaperSimulationConfig = PaperSimulationConfig()

    def __post_init__(self) -> None:
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be positive")
        for name in ("max_portfolio_risk_pct", "max_gross_leverage"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.max_portfolio_risk_pct > 0.02:
            raise ValueError("max_portfolio_risk_pct cannot exceed 2%")
        if self.max_gross_leverage > 20:
            raise ValueError("max_gross_leverage cannot exceed 20x")


@dataclass(frozen=True)
class PortfolioSimulationResult:
    status: str
    initial_equity: float
    final_equity: float
    outcomes: tuple[PaperTradeResult, ...]
    realized_max_drawdown_pct: float
    max_concurrent_positions: int
    constraints: Mapping[str, float | int]
    limitations: tuple[str, ...]

    @property
    def accepted_count(self) -> int:
        return sum(item.status == "simulated" for item in self.outcomes)

    @property
    def rejected_count(self) -> int:
        return sum(item.status != "simulated" for item in self.outcomes)

    @property
    def net_pnl(self) -> float:
        return sum(item.net_pnl for item in self.outcomes if item.status == "simulated")


class PortfolioPaperSimulator:
    """Chronological portfolio paper simulation with conservative exposure gates.

    Each trade uses the single-trade simulator's fee/spread/slippage model.
    Overlapping positions are admitted only when verified contract notional,
    open-slot, and worst-case stop-risk constraints all remain within limits.
    """

    def __init__(self, config: PortfolioSimulationConfig | None = None):
        self.config = config or PortfolioSimulationConfig()
        per_trade_risk = min(
            self.config.trade.risk_per_trade_pct,
            self.config.max_portfolio_risk_pct / self.config.max_open_positions,
        )
        self.trade_config = replace(self.config.trade, risk_per_trade_pct=per_trade_risk)
        self.simulator = PaperSimulator(self.trade_config)

    def simulate(
        self,
        signals: Sequence[StrategySignal],
        instruments: Mapping[str, Instrument],
        candles_by_product: Mapping[str, Sequence[Candle]],
        initial_equity: float,
    ) -> PortfolioSimulationResult:
        if not math.isfinite(initial_equity) or initial_equity <= 0:
            raise ValueError("initial_equity must be positive and finite")
        ordered = sorted(
            enumerate(signals),
            key=lambda item: (item[1].decision_time, item[0]),
        )
        seen: set[tuple[str, str, str]] = set()
        outcomes: list[PaperTradeResult] = []
        active: list[tuple[PaperTradeResult, float, float]] = []
        equity = float(initial_equity)
        equity_marks = [equity]
        max_concurrent = 0

        def settle(cutoff: datetime) -> None:
            nonlocal active, equity
            due = sorted(
                (item for item in active
                 if item[0].exit_time and datetime.fromisoformat(item[0].exit_time) <= cutoff),
                key=lambda item: item[0].exit_time or "",
            )
            for item in due:
                result, _, _ = item
                before = equity
                equity += result.net_pnl
                settled = replace(result, equity_before=before, equity_after=equity)
                for index, existing in enumerate(outcomes):
                    if (
                        existing.strategy_id == result.strategy_id
                        and existing.product_id == result.product_id
                        and existing.decision_time == result.decision_time
                        and existing.status == "simulated"
                    ):
                        outcomes[index] = settled
                        break
                equity_marks.append(equity)
                active.remove(item)

        for _, signal in ordered:
            decision = signal.decision_time
            settle(decision)

            identity = (signal.strategy_id, signal.product_id, decision.isoformat())
            if identity in seen:
                outcomes.append(self._reject(signal, equity, "duplicate signal identity"))
                continue
            seen.add(identity)
            instrument = instruments.get(signal.product_id)
            bars = candles_by_product.get(signal.product_id)
            if instrument is None or bars is None:
                outcomes.append(self._reject(signal, equity, "instrument or candles unavailable"))
                continue

            candidate = self.simulator.simulate(signal, instrument, bars, equity)
            if candidate.status != "simulated":
                outcomes.append(candidate)
                continue
            entry_at = datetime.fromisoformat(candidate.entry_time)
            settle(entry_at)

            specs = instrument.contract_specs
            if specs is None or not specs.verified or specs.contract_multiplier is None:
                outcomes.append(self._reject(signal, equity, "verified contract multiplier unavailable"))
                continue
            notional = candidate.entry_price * candidate.quantity * specs.contract_multiplier
            risk = abs(candidate.entry_price - candidate.stop_price) * candidate.quantity * specs.contract_multiplier
            open_notional = sum(item[1] for item in active)
            open_risk = sum(item[2] for item in active)
            rejection = None
            if any(item[0].product_id == signal.product_id for item in active):
                rejection = "same-product position already open"
            elif len(active) >= self.config.max_open_positions:
                rejection = "maximum open positions reached"
            elif open_notional + notional > equity * self.config.max_gross_leverage:
                rejection = "maximum gross portfolio exposure exceeded"
            elif open_risk + risk > equity * self.config.max_portfolio_risk_pct:
                rejection = "maximum portfolio stop-risk exceeded"
            if rejection:
                outcomes.append(self._reject(signal, equity, rejection))
                continue

            active.append((candidate, notional, risk))
            outcomes.append(candidate)
            max_concurrent = max(max_concurrent, len(active))

        # Settle the remaining accepted trades in exit-time order.
        final_exit = max(
            (datetime.fromisoformat(item[0].exit_time) for item in active if item[0].exit_time),
            default=datetime.max.replace(tzinfo=decision.tzinfo) if ordered else datetime.max,
        )
        settle(final_exit)
        peak = initial_equity
        max_drawdown = 0.0
        for mark in equity_marks:
            peak = max(peak, mark)
            if peak:
                max_drawdown = max(max_drawdown, (peak - mark) / peak)

        return PortfolioSimulationResult(
            status="simulated" if any(o.status == "simulated" for o in outcomes) else "no_trades",
            initial_equity=float(initial_equity),
            final_equity=float(equity),
            outcomes=tuple(outcomes),
            realized_max_drawdown_pct=max_drawdown,
            max_concurrent_positions=max_concurrent,
            constraints={
                "max_open_positions": self.config.max_open_positions,
                "max_portfolio_risk_pct": self.config.max_portfolio_risk_pct,
                "max_gross_leverage": self.config.max_gross_leverage,
                "per_trade_risk_pct": self.trade_config.risk_per_trade_pct,
            },
            limitations=(
                "Positions are filled with the single-trade OHLCV simulator; partial fills and market impact are absent.",
                "Funding cash flows, maintenance margin, liquidation, and correlation stress are not modeled.",
                "Signals are processed chronologically and overlapping same-product positions are rejected.",
            ),
        )

    @staticmethod
    def _reject(signal: StrategySignal, equity: float, reason: str) -> PaperTradeResult:
        return PaperTradeResult(
            strategy_id=signal.strategy_id,
            product_id=signal.product_id,
            symbol=signal.symbol,
            direction=signal.direction,
            status="rejected",
            reason=reason,
            decision_time=signal.decision_time.isoformat(),
            equity_before=equity,
            equity_after=equity,
        )
