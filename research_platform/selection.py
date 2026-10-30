from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Mapping, Sequence

import pandas as pd

from .simulation import PaperTradeResult
from .strategies import StrategySignal


SignalKey = tuple[str, str, str]
EvidenceKey = tuple[str, str]


def signal_key(signal: StrategySignal) -> SignalKey:
    return (
        signal.strategy_id,
        signal.product_id,
        signal.decision_time.astimezone(timezone.utc).isoformat(),
    )


@dataclass(frozen=True)
class ValidationEvidence:
    strategy_id: str
    regime: str
    simulated_trades: int
    net_expectancy: float
    net_pnl: float
    win_rate: float
    validation_end: str


@dataclass(frozen=True)
class StrategySelectionPolicy:
    minimum_validation_trades: int = 3
    minimum_net_expectancy: float = 0.0
    overlap_window: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.minimum_validation_trades < 1:
            raise ValueError("minimum_validation_trades must be positive")
        if not math.isfinite(self.minimum_net_expectancy):
            raise ValueError("minimum_net_expectancy must be finite")
        if self.overlap_window < timedelta(0):
            raise ValueError("overlap_window cannot be negative")


@dataclass(frozen=True)
class SelectionDecision:
    signal: StrategySignal
    regime: str | None
    selected: bool
    reason: str


@dataclass(frozen=True)
class StrategySelectionResult:
    selected: tuple[StrategySignal, ...]
    decisions: tuple[SelectionDecision, ...]
    evidence: Mapping[EvidenceKey, ValidationEvidence]

    def as_record(self) -> dict:
        counts: dict[str, int] = defaultdict(int)
        for item in self.decisions:
            counts["selected" if item.selected else item.reason] += 1
        return {
            "candidate_count": len(self.decisions),
            "selected_count": len(self.selected),
            "no_trade": not self.selected,
            "decision_counts": dict(sorted(counts.items())),
            "evidence": [
                {
                    "strategy_id": item.strategy_id,
                    "regime": item.regime,
                    "simulated_trades": item.simulated_trades,
                    "net_expectancy": item.net_expectancy,
                    "net_pnl": item.net_pnl,
                    "win_rate": item.win_rate,
                    "validation_end": item.validation_end,
                }
                for _, item in sorted(self.evidence.items())
            ],
            "decisions": [
                {
                    "strategy_id": item.signal.strategy_id,
                    "product_id": item.signal.product_id,
                    "symbol": item.signal.symbol,
                    "decision_time": item.signal.decision_time.isoformat(),
                    "regime": item.regime,
                    "selected": item.selected,
                    "reason": item.reason,
                }
                for item in self.decisions
            ],
        }


def classify_market_regime(frame: pd.DataFrame, as_of: datetime) -> str | None:
    """Classify from completed 1h candles using the instrument's own recent history."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("regime classification time must be timezone-aware")
    required = {"time", "high", "low", "close"}
    if not required.issubset(frame.columns):
        raise ValueError("regime frame is missing required OHLC columns")
    if frame.empty:
        return None
    times = pd.to_datetime(frame.time, errors="raise", utc=True)
    if not times.is_monotonic_increasing or times.duplicated().any():
        raise ValueError("regime candles must be chronologically ordered and unique")
    completed = frame.loc[
        times + pd.Timedelta(hours=1) <= pd.Timestamp(as_of).tz_convert("UTC")
    ]
    if len(completed) < 48:
        return None

    high = completed.high.astype(float).reset_index(drop=True)
    low = completed.low.astype(float).reset_index(drop=True)
    close = completed.close.astype(float).reset_index(drop=True)
    rolling_range = (
        high.rolling(24).max() - low.rolling(24).min()
    ) / close
    current_range = float(rolling_range.iloc[-1])
    range_baseline = rolling_range.iloc[-49:-1].dropna()
    if range_baseline.empty or not math.isfinite(current_range):
        return None
    median_range = float(range_baseline.median())
    range_mad = float((range_baseline - median_range).abs().median())
    if current_range > median_range + 3 * range_mad:
        return "high_volatility"

    returns_24h = close.pct_change(24).dropna()
    prior_returns = returns_24h.iloc[-49:-1]
    if prior_returns.empty:
        return None
    current_return = float(returns_24h.iloc[-1])
    robust_move = float(prior_returns.abs().median())
    if current_return > 2 * robust_move and current_return > 0:
        return "uptrend"
    if current_return < -2 * robust_move and current_return < 0:
        return "downtrend"
    return "range_bound"


def build_validation_evidence(
    outcomes: Sequence[PaperTradeResult],
    regimes_by_signal: Mapping[SignalKey, str | None],
    validation_end: datetime,
) -> dict[EvidenceKey, ValidationEvidence]:
    if validation_end.tzinfo is None or validation_end.utcoffset() is None:
        raise ValueError("validation_end must be timezone-aware")
    grouped: dict[EvidenceKey, list[float]] = defaultdict(list)
    for outcome in outcomes:
        if outcome.status != "simulated":
            continue
        identity = (
            outcome.strategy_id,
            outcome.product_id,
            datetime.fromisoformat(outcome.decision_time).astimezone(timezone.utc).isoformat(),
        )
        regime = regimes_by_signal.get(identity)
        if regime is None:
            continue
        if not math.isfinite(outcome.net_pnl):
            raise ValueError("validation PnL must be finite")
        grouped[(outcome.strategy_id, regime)].append(outcome.net_pnl)

    validation_stamp = validation_end.astimezone(timezone.utc).isoformat()
    result = {}
    for (strategy_id, regime), pnls in grouped.items():
        result[(strategy_id, regime)] = ValidationEvidence(
            strategy_id=strategy_id,
            regime=regime,
            simulated_trades=len(pnls),
            net_expectancy=sum(pnls) / len(pnls),
            net_pnl=sum(pnls),
            win_rate=sum(value > 0 for value in pnls) / len(pnls),
            validation_end=validation_stamp,
        )
    return result


class ValidatedStrategySelector:
    """Select only strategies with positive, sufficiently sampled validation evidence."""

    def __init__(self, policy: StrategySelectionPolicy | None = None):
        self.policy = policy or StrategySelectionPolicy()

    def select(
        self,
        signals: Sequence[StrategySignal],
        evidence: Mapping[EvidenceKey, ValidationEvidence],
        regimes_by_signal: Mapping[SignalKey, str | None],
    ) -> StrategySelectionResult:
        def rank(signal: StrategySignal):
            regime = regimes_by_signal.get(signal_key(signal))
            validation = evidence.get((signal.strategy_id, regime)) if regime else None
            if validation is None:
                return (signal.decision_time, signal.product_id, 1, 0.0, signal.strategy_id)
            validation_end = datetime.fromisoformat(validation.validation_end)
            if validation_end.tzinfo is None or validation_end.utcoffset() is None:
                raise ValueError("validation evidence timestamp must be timezone-aware")
            eligible = (
                validation_end < signal.decision_time
                and validation.simulated_trades >= self.policy.minimum_validation_trades
                and validation.net_expectancy > self.policy.minimum_net_expectancy
            )
            return (
                signal.decision_time,
                signal.product_id,
                0 if eligible else 1,
                -validation.net_expectancy if eligible else 0.0,
                signal.strategy_id,
            )

        ordered = sorted(
            signals,
            key=rank,
        )
        decisions: list[SelectionDecision] = []
        selected: list[StrategySignal] = []
        selected_by_product: dict[str, list[datetime]] = defaultdict(list)
        for signal in ordered:
            regime = regimes_by_signal.get(signal_key(signal))
            if regime is None:
                decisions.append(SelectionDecision(signal, None, False, "regime unavailable"))
                continue
            validation = evidence.get((signal.strategy_id, regime))
            if validation is None:
                decisions.append(
                    SelectionDecision(signal, regime, False, "no validation evidence for regime")
                )
                continue
            validation_end = datetime.fromisoformat(validation.validation_end)
            if validation_end.tzinfo is None or validation_end.utcoffset() is None:
                raise ValueError("validation evidence timestamp must be timezone-aware")
            if validation_end >= signal.decision_time:
                decisions.append(
                    SelectionDecision(
                        signal, regime, False, "validation evidence is not prior to signal"
                    )
                )
                continue
            if validation.simulated_trades < self.policy.minimum_validation_trades:
                decisions.append(
                    SelectionDecision(signal, regime, False, "insufficient validation trades")
                )
                continue
            if validation.net_expectancy <= self.policy.minimum_net_expectancy:
                decisions.append(
                    SelectionDecision(signal, regime, False, "validation expectancy not positive")
                )
                continue
            prior_times = selected_by_product[signal.product_id]
            if any(
                abs(signal.decision_time - prior) < self.policy.overlap_window
                for prior in prior_times
            ):
                decisions.append(
                    SelectionDecision(signal, regime, False, "overlapping same-product signal")
                )
                continue
            selected.append(signal)
            prior_times.append(signal.decision_time)
            decisions.append(SelectionDecision(signal, regime, True, "validated evidence"))
        return StrategySelectionResult(tuple(selected), tuple(decisions), dict(evidence))
