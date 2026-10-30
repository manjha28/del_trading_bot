from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import math
import statistics
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
    max_pairwise_correlation: float = 0.8
    correlation_lookback_minutes: int = 60
    minimum_correlation_observations: int = 30
    max_daily_realized_loss_pct: float = 0.02
    max_realized_drawdown_pct: float = 0.10

    def __post_init__(self) -> None:
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be positive")
        for name in (
            "max_portfolio_risk_pct", "max_gross_leverage",
            "max_pairwise_correlation", "max_daily_realized_loss_pct",
            "max_realized_drawdown_pct",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.max_pairwise_correlation >= 1:
            raise ValueError("max_pairwise_correlation must be less than 1")
        if self.correlation_lookback_minutes < 2:
            raise ValueError("correlation_lookback_minutes must be at least 2")
        if self.minimum_correlation_observations < 3:
            raise ValueError("minimum_correlation_observations must be at least 3")
        if self.max_portfolio_risk_pct > 0.02:
            raise ValueError("max_portfolio_risk_pct cannot exceed 2%")
        if self.max_gross_leverage > 20:
            raise ValueError("max_gross_leverage cannot exceed 20x")
        if self.max_daily_realized_loss_pct > 0.2:
            raise ValueError("max_daily_realized_loss_pct cannot exceed 20%")
        if self.max_realized_drawdown_pct > 0.5:
            raise ValueError("max_realized_drawdown_pct cannot exceed 50%")


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
        *,
        max_exit_time: datetime | None = None,
    ) -> PortfolioSimulationResult:
        if not math.isfinite(initial_equity) or initial_equity <= 0:
            raise ValueError("initial_equity must be positive and finite")
        if max_exit_time is not None and (
            max_exit_time.tzinfo is None or max_exit_time.utcoffset() is None
        ):
            raise ValueError("max_exit_time must be timezone-aware")
        ordered = sorted(
            enumerate(signals),
            key=lambda item: (item[1].decision_time, item[0]),
        )
        seen: set[tuple[str, str, str]] = set()
        outcomes: list[PaperTradeResult] = []
        active: list[tuple[PaperTradeResult, float, float]] = []
        equity = float(initial_equity)
        equity_marks = [equity]
        daily_start_equity = {}
        daily_realized_pnl = {}
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
                exit_day = datetime.fromisoformat(
                    result.exit_time
                ).astimezone(timezone.utc).date()
                daily_start_equity.setdefault(exit_day, equity)
                daily_realized_pnl[exit_day] = (
                    daily_realized_pnl.get(exit_day, 0.0) + result.net_pnl
                )
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
            decision_day = decision.astimezone(timezone.utc).date()
            daily_start_equity.setdefault(decision_day, equity)

            identity = (signal.strategy_id, signal.product_id, decision.isoformat())
            if identity in seen:
                outcomes.append(self._reject(signal, equity, "duplicate signal identity"))
                continue
            seen.add(identity)
            daily_limit = (
                daily_start_equity[decision_day]
                * self.config.max_daily_realized_loss_pct
            )
            if daily_realized_pnl.get(decision_day, 0.0) <= -daily_limit:
                outcomes.append(self._reject(
                    signal, equity, "maximum daily realized loss exceeded"
                ))
                continue
            peak_equity = max(equity_marks)
            if peak_equity > 0 and (
                peak_equity - equity
            ) / peak_equity >= self.config.max_realized_drawdown_pct:
                outcomes.append(self._reject(
                    signal, equity, "maximum realized portfolio drawdown exceeded"
                ))
                continue
            instrument = instruments.get(signal.product_id)
            bars = candles_by_product.get(signal.product_id)
            if instrument is None or bars is None:
                outcomes.append(self._reject(signal, equity, "instrument or candles unavailable"))
                continue

            candidate = self.simulator.simulate(
                signal, instrument, bars, equity, max_exit_time=max_exit_time
            )
            if candidate.status != "simulated":
                outcomes.append(candidate)
                continue
            entry_at = datetime.fromisoformat(candidate.entry_time)
            settle(entry_at)
            if candidate.equity_before != equity:
                candidate = self.simulator.simulate(
                    signal, instrument, bars, equity,
                    max_exit_time=max_exit_time,
                )
                if candidate.status != "simulated":
                    outcomes.append(candidate)
                    continue

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
            else:
                rejection = self._correlation_rejection(
                    signal, decision, active, candles_by_product
                )
            if rejection is None and open_notional + notional > equity * self.config.max_gross_leverage:
                rejection = "maximum gross portfolio exposure exceeded"
            elif rejection is None and open_risk + risk > equity * self.config.max_portfolio_risk_pct:
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
                "max_pairwise_correlation": self.config.max_pairwise_correlation,
                "correlation_lookback_minutes": self.config.correlation_lookback_minutes,
                "max_daily_realized_loss_pct": self.config.max_daily_realized_loss_pct,
                "max_realized_drawdown_pct": self.config.max_realized_drawdown_pct,
                "per_trade_risk_pct": self.trade_config.risk_per_trade_pct,
            },
            limitations=(
                "Positions use candle-level latency plus the single-trade OHLCV simulator; sub-candle latency, partial fills, and market impact are absent.",
                "Funding cash flows, maintenance margin, liquidation, and correlation stress outside the recent aligned-return gate are not modeled.",
                "Daily loss and drawdown gates use realized equity only; intratrade mark-to-market risk is not modeled.",
                "Signals are processed chronologically and overlapping same-product positions are rejected.",
            ),
        )

    def _correlation_rejection(
        self,
        signal: StrategySignal,
        decision: datetime,
        active: list[tuple[PaperTradeResult, float, float]],
        candles_by_product: Mapping[str, Sequence[Candle]],
    ) -> str | None:
        for open_trade, _, _ in active:
            if open_trade.direction != signal.direction:
                continue
            returns = self._aligned_returns(
                candles_by_product.get(signal.product_id, ()),
                candles_by_product.get(open_trade.product_id, ()),
                decision,
            )
            if len(returns) < self.config.minimum_correlation_observations:
                return "insufficient aligned history to estimate exposure correlation"
            new_returns, open_returns = zip(*returns)
            if statistics.pstdev(new_returns) == 0 or statistics.pstdev(open_returns) == 0:
                return "undefined return correlation; correlated exposure rejected"
            correlation = statistics.correlation(new_returns, open_returns)
            if not math.isfinite(correlation):
                return "non-finite return correlation; correlated exposure rejected"
            if correlation >= self.config.max_pairwise_correlation:
                return (
                    "same-direction correlated exposure exceeded "
                    f"{self.config.max_pairwise_correlation:.2f}"
                )
        return None

    def _aligned_returns(
        self,
        left: Sequence[Candle],
        right: Sequence[Candle],
        decision: datetime,
    ) -> list[tuple[float, float]]:
        cutoff = decision.astimezone(timezone.utc)
        lookback = cutoff - timedelta(minutes=self.config.correlation_lookback_minutes)

        def series(candles: Sequence[Candle]) -> dict[datetime, float]:
            ordered = sorted(
                (
                    candle for candle in candles
                    if candle.is_complete
                    and lookback <= candle.start_time < cutoff
                ),
                key=lambda candle: candle.start_time,
            )
            return {
                current.start_time: current.close / prior.close - 1
                for prior, current in zip(ordered, ordered[1:])
                if prior.close > 0
            }

        left_returns = series(left)
        right_returns = series(right)
        return [
            (left_returns[stamp], right_returns[stamp])
            for stamp in sorted(left_returns.keys() & right_returns.keys())
        ]

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
