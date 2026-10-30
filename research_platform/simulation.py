from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from .market_data import Candle, Instrument
from .strategies import StrategySignal


@dataclass(frozen=True)
class PaperSimulationConfig:
    risk_per_trade_pct: float = 0.005
    leverage: float = 5.0
    fee_rate: float = 0.0005
    spread_bps: float = 5.0
    slippage_bps: float = 2.0
    max_holding_candles: int = 100

    def __post_init__(self) -> None:
        if not math.isfinite(self.risk_per_trade_pct) or not 0 < self.risk_per_trade_pct <= 0.02:
            raise ValueError("risk_per_trade_pct must be in (0, 0.02]")
        if not math.isfinite(self.leverage) or not 1 <= self.leverage <= 20:
            raise ValueError("leverage must be in [1, 20]")
        for name in ("fee_rate", "spread_bps", "slippage_bps"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.max_holding_candles < 1:
            raise ValueError("max_holding_candles must be positive")


@dataclass(frozen=True)
class PaperTradeResult:
    strategy_id: str
    product_id: str
    symbol: str
    direction: str
    status: str
    reason: str
    decision_time: str
    entry_time: str | None = None
    exit_time: str | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    quantity: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    equity_before: float = 0.0
    equity_after: float = 0.0
    exit_reason: str | None = None
    assumptions: tuple[str, ...] = ()


class PaperSimulator:
    """Single-trade, OHLCV-only simulator; never connects to exchange execution."""

    def __init__(self, config: PaperSimulationConfig | None = None):
        self.config = config or PaperSimulationConfig()

    def simulate(
        self,
        signal: StrategySignal,
        instrument: Instrument,
        candles: Iterable[Candle],
        equity: float,
    ) -> PaperTradeResult:
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("equity must be positive and finite")
        if signal.product_id != instrument.product_id or signal.symbol != instrument.symbol:
            raise ValueError("signal and instrument identity do not match")
        if signal.decision_time.tzinfo is None or signal.decision_time.utcoffset() is None:
            raise ValueError("signal decision time must be timezone-aware")
        specs = instrument.contract_specs
        if specs is None or not specs.verified:
            return self._rejected(signal, equity, "contract specifications are not verified")
        if any(value is None for value in (
            specs.contract_multiplier, specs.tick_size, specs.lot_increment, specs.minimum_quantity,
        )):
            return self._rejected(signal, equity, "verified contract specification is incomplete")
        if signal.stop_price is None:
            return self._rejected(signal, equity, "strategy stop cannot be normalized")
        if signal.entry_reference == "legacy trigger entry" and signal.entry_price is None:
            return self._rejected(signal, equity, "trigger entry requires a signal entry price")

        bars = list(candles)
        if any(not item.is_complete for item in bars):
            return self._rejected(signal, equity, "incomplete candle supplied to simulator")
        if any(a.start_time >= b.start_time for a, b in zip(bars, bars[1:])):
            return self._rejected(signal, equity, "candles must be strictly chronological and unique")
        future = [bar for bar in bars if bar.start_time >= signal.decision_time]
        if not future:
            return self._rejected(signal, equity, "no candle at or after the signal decision time")

        stop = self._round_price(
            signal.stop_price, specs.tick_size, signal.direction, away=True
        )
        entry_bar = None
        entry_price = None
        entry_start_index = 0
        trigger_entry = signal.entry_reference == "legacy trigger entry"
        if signal.entry_reference not in (
            "next_1h_open", "next_5m_open", "next_1m_open",
            "legacy signal candle close", "legacy trigger entry",
        ):
            return self._rejected(signal, equity, "entry reference is not supported by simulator")
        for index, bar in enumerate(future):
            if trigger_entry:
                triggered = (
                    bar.high >= signal.entry_price if signal.direction == "LONG"
                    else bar.low <= signal.entry_price
                )
                invalidated = (
                    bar.low <= stop if signal.direction == "LONG"
                    else bar.high >= stop
                )
                if not triggered:
                    if invalidated:
                        return self._rejected(
                            signal, equity, "stop level traded before trigger entry"
                        )
                    continue
                if invalidated:
                    return self._rejected(
                        signal, equity, "entry and stop touched in one bar; entry ordering ambiguous"
                    )
                reference = (
                    max(float(signal.entry_price), bar.open) if signal.direction == "LONG"
                    else min(float(signal.entry_price), bar.open)
                )
                entry_start_index = index
            else:
                reference = bar.open
                entry_start_index = index
            entry_price = self._adverse_fill(reference, signal.direction, entering=True)
            entry_price = self._round_price(
                entry_price, specs.tick_size, signal.direction, away=False
            )
            entry_bar = bar
            break
        if entry_bar is None or entry_price is None:
            return self._rejected(signal, equity, "entry was not triggered in supplied candles")

        distance = entry_price - stop if signal.direction == "LONG" else stop - entry_price
        if distance <= 0:
            return self._rejected(signal, equity, "stop is on the wrong side of simulated entry")

        multiplier = specs.contract_multiplier
        adverse_rate = (self.config.spread_bps / 2 + self.config.slippage_bps) / 10_000
        estimated_cost_per_contract = (
            entry_price * adverse_rate + stop * adverse_rate
            + (entry_price + stop) * self.config.fee_rate
        ) * multiplier
        risk_per_contract = distance * multiplier + estimated_cost_per_contract
        risk_budget = equity * self.config.risk_per_trade_pct
        quantity = math.floor((risk_budget / risk_per_contract) / specs.lot_increment) * specs.lot_increment
        quantity = min(quantity, (equity * self.config.leverage / (entry_price * multiplier)
                                  // specs.lot_increment) * specs.lot_increment)
        if specs.maximum_quantity is not None:
            quantity = min(
                quantity,
                (specs.maximum_quantity // specs.lot_increment) * specs.lot_increment,
            )
        if quantity < specs.minimum_quantity:
            return self._rejected(signal, equity, "risk-sized quantity is below verified minimum")

        target = signal.target_prices[0] if signal.target_prices else None
        if target is not None:
            target = self._round_price(
                target, specs.tick_size, signal.direction, away=False
            )
            if (signal.direction == "LONG" and target <= entry_price) or (
                signal.direction == "SHORT" and target >= entry_price
            ):
                return self._rejected(signal, equity, "target is not beyond simulated entry")
        close_bar = None
        exit_raw = None
        exit_reason = None
        held = 0
        for bar in future[entry_start_index:]:
            held += 1
            if signal.direction == "LONG":
                stop_hit = bar.low <= stop
                target_hit = target is not None and bar.high >= target
                if stop_hit:
                    exit_raw = min(stop, bar.open) if bar.open < stop else stop
                    close_bar, exit_reason = bar, "STOP_FIRST"
                    break
                if target_hit:
                    exit_raw = target
                    close_bar, exit_reason = bar, "TARGET"
                    break
            else:
                stop_hit = bar.high >= stop
                target_hit = target is not None and bar.low <= target
                if stop_hit:
                    exit_raw = max(stop, bar.open) if bar.open > stop else stop
                    close_bar, exit_reason = bar, "STOP_FIRST"
                    break
                if target_hit:
                    exit_raw = target
                    close_bar, exit_reason = bar, "TARGET"
                    break
            if held >= self.config.max_holding_candles:
                exit_raw = bar.close
                close_bar, exit_reason = bar, "MAX_HOLD"
                break

        if close_bar is None or exit_raw is None or exit_reason is None:
            return self._rejected(signal, equity, "no exit within supplied candles and holding limit")

        exit_price = self._adverse_fill(
            exit_raw, signal.direction, entering=False,
        )
        exit_price = self._round_price(exit_price, specs.tick_size, signal.direction, away=False)
        gross = (
            (exit_price - entry_price) if signal.direction == "LONG"
            else (entry_price - exit_price)
        ) * quantity * multiplier
        fee_notional = (entry_price + exit_price) * quantity * multiplier
        fees = fee_notional * self.config.fee_rate
        net = gross - fees
        assumptions = (
            "entry at first candle open at or after signal decision time unless signal supplies a price",
            "fixed spread and slippage applied adversely to entry and exit",
            "fees charged on entry and exit notional",
            "stop takes precedence when stop and target both touch in one OHLCV candle",
            "no queue priority, partial fills, market impact, funding cash flow, or portfolio netting modeled",
        )
        return PaperTradeResult(
            strategy_id=signal.strategy_id,
            product_id=signal.product_id,
            symbol=signal.symbol,
            direction=signal.direction,
            status="simulated",
            reason="paper simulation completed",
            decision_time=signal.decision_time.isoformat(),
            entry_time=entry_bar.start_time.isoformat(),
            exit_time=close_bar.start_time.isoformat(),
            entry_price=entry_price,
            exit_price=exit_price,
            stop_price=stop,
            target_price=target,
            quantity=quantity,
            gross_pnl=gross,
            fees=fees,
            net_pnl=net,
            equity_before=equity,
            equity_after=equity + net,
            exit_reason=exit_reason,
            assumptions=assumptions,
        )

    def _rejected(self, signal: StrategySignal, equity: float, reason: str) -> PaperTradeResult:
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

    def _adverse_fill(self, price: float, direction: str, entering: bool) -> float:
        adverse_bps = self.config.spread_bps / 2 + self.config.slippage_bps
        direction_sign = 1 if direction == "LONG" else -1
        trade_sign = direction_sign if entering else -direction_sign
        return price * (1 + trade_sign * adverse_bps / 10_000)

    @staticmethod
    def _round_price(price: float, tick: float, direction: str, away: bool) -> float:
        if tick <= 0:
            raise ValueError("verified tick size must be positive")
        quotient = price / tick
        if away:
            rounded = math.floor(quotient + 1e-12) if direction == "LONG" else math.ceil(quotient - 1e-12)
        else:
            rounded = math.ceil(quotient - 1e-12) if direction == "LONG" else math.floor(quotient + 1e-12)
        return rounded * tick
