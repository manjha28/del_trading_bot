from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib
import math
from pathlib import Path
import sys
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

import pandas as pd

from .market_data import Instrument, TIMEFRAME_SECONDS


Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class StrategySpecification:
    strategy_id: str
    version: str
    timeframes: tuple[str, ...]
    required_fields: tuple[str, ...]
    regimes: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.version:
            raise ValueError("strategy_id and version are required")
        unsupported = set(self.timeframes).difference(TIMEFRAME_SECONDS)
        if unsupported:
            raise ValueError(f"unknown strategy timeframes: {sorted(unsupported)}")


@dataclass(frozen=True)
class StrategyContext:
    instrument: Instrument
    as_of: datetime
    frames: Mapping[str, pd.DataFrame]
    regime: Any = None
    profile: Any = None
    provenance: Mapping[str, str] = field(default_factory=dict)
    state: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("strategy context as_of must be timezone-aware")
        object.__setattr__(self, "as_of", self.as_of.astimezone(timezone.utc))

    def completed_frame(self, timeframe: str) -> pd.DataFrame:
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        frame = self.frames.get(timeframe)
        if frame is None:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        if frame.empty:
            return frame.copy().reset_index(drop=True)
        required = {"time", "open", "high", "low", "close", "volume"}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"{timeframe} frame missing fields: {sorted(missing)}")
        result = frame.copy()
        parsed_times = pd.to_datetime(result["time"], errors="raise")
        if parsed_times.isna().any() or parsed_times.dt.tz is None:
            raise ValueError(f"{timeframe} timestamps must be valid and timezone-aware")
        result["time"] = parsed_times.dt.tz_convert("UTC")
        if result["time"].duplicated().any():
            raise ValueError(f"{timeframe} frame contains duplicate candle timestamps")
        result = result.sort_values("time")
        close_times = result["time"] + pd.to_timedelta(TIMEFRAME_SECONDS[timeframe], unit="s")
        result = result.loc[close_times <= pd.Timestamp(self.as_of)].reset_index(drop=True)
        return result


@dataclass(frozen=True)
class StrategySignal:
    strategy_id: str
    strategy_version: str
    product_id: str
    symbol: str
    direction: Direction
    decision_time: datetime
    entry_price: float | None
    entry_reference: str
    stop_price: float | None
    stop_logic: str
    target_prices: tuple[float, ...]
    target_logic: str
    invalidation_conditions: tuple[str, ...]
    position_sizing_inputs: Mapping[str, Any]
    provenance: Mapping[str, str]
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.direction not in ("LONG", "SHORT"):
            raise ValueError("direction must be LONG or SHORT")
        if self.decision_time.tzinfo is None or self.decision_time.utcoffset() is None:
            raise ValueError("signal decision_time must be timezone-aware")
        object.__setattr__(self, "decision_time", self.decision_time.astimezone(timezone.utc))
        if not self.strategy_id or not self.product_id or not self.symbol:
            raise ValueError("signal strategy and instrument identity are required")
        for name in ("entry_price", "stop_price"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(float(value)) or float(value) <= 0):
                raise ValueError(f"{name} must be a positive finite price")
        targets = tuple(float(value) for value in self.target_prices)
        if any(not math.isfinite(value) or value <= 0 for value in targets):
            raise ValueError("target prices must be positive and finite")
        object.__setattr__(self, "target_prices", targets)


@runtime_checkable
class StrategyAdapter(Protocol):
    specification: StrategySpecification

    def evaluate(self, context: StrategyContext) -> list[StrategySignal]: ...


def _decision_time(value: Any, timeframe: str) -> datetime:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return (stamp + pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe])).to_pydatetime()


def _mhr_api():
    package_root = Path(__file__).resolve().parents[1] / "strategy-short"
    package_path = str(package_root)
    if package_path not in sys.path:
        sys.path.insert(0, package_path)
    return importlib.import_module("mhr_pump_short")


class MHRExhaustionAdapter:
    specification = StrategySpecification(
        "mhr_exhaustion", "legacy-wrapper-v1", ("1h",),
        ("time", "open", "high", "low", "close", "volume"),
        description="Compatibility wrapper around the existing MHR entry detector.",
    )

    def __init__(self, config: Any = None, entry_paths: set[str] | None = None):
        api = _mhr_api()
        self.api = api
        self.config = config or api.MHRConfig()
        self.entry_paths = frozenset(entry_paths) if entry_paths is not None else None

    def evaluate(self, context: StrategyContext) -> list[StrategySignal]:
        frame = context.completed_frame("1h")
        if frame.empty:
            return []
        prior_price = context.state.get("previous_entry_price")
        is_reentry = bool(context.state.get("is_reentry", False))
        result = self.api.entry_signal(
            frame, len(frame) - 1, self.config,
            is_reentry=is_reentry, previous_entry_price=prior_price,
        )
        if result is None or (
            self.entry_paths is not None and result["entry_path"] not in self.entry_paths
        ):
            return []
        candle = frame.iloc[-1]
        previous_high = float(frame.iloc[-2].high) if len(frame) > 1 else None
        return [StrategySignal(
            strategy_id=self.specification.strategy_id,
            strategy_version=self.specification.version,
            product_id=context.instrument.product_id,
            symbol=context.instrument.symbol,
            direction="SHORT",
            decision_time=_decision_time(candle.time, "1h"),
            entry_price=None,
            entry_reference="next_1h_open",
            stop_price=None,
            stop_logic=(
                "previous completed candle high; legacy ATR fallback when structural "
                "stop cannot produce a valid quantity"
            ),
            target_prices=(),
            target_logic=(
                "legacy MHR minimum reward:risk target, calculated after entry and "
                "independently of this signal"
            ),
            invalidation_conditions=("entry setup ceases to be valid before next-bar entry",),
            position_sizing_inputs={
                "leverage": self.config.leverage,
                "risk_capital_pct": self.config.sl_capital_pct,
                "max_capital_usage_pct": self.config.max_capital_usage_pct,
                "quantity": "deferred to independent risk sizing",
            },
            provenance={"1h": context.provenance.get("1h", "unspecified")},
            diagnostics={**result, "previous_candle_high": previous_high},
        )]


class ScalpPullbackAdapter:
    specification = StrategySpecification(
        "scalp_pullback", "legacy-wrapper-v1", ("5m", "1h"),
        ("time", "open", "high", "low", "close", "volume"),
        regimes=("bullish", "bearish"),
        description="Compatibility wrapper around the existing scalp entry detector.",
    )

    def __init__(self, config: Any = None):
        api = _mhr_api()
        self.api = api
        self.config = config or api.ScalpConfig()

    def evaluate(self, context: StrategyContext) -> list[StrategySignal]:
        frame = context.completed_frame("5m")
        hourly = context.completed_frame("1h")
        minimum = max(self.config.ema_5m_length, self.config.max_pullback_candles + 2)
        if len(frame) <= minimum or len(hourly) < self.config.ema_1h_length:
            return []
        ema = self.api.ema(hourly["close"].astype(float), self.config.ema_1h_length)
        if pd.isna(ema.iloc[-1]):
            return []
        trend = "BULLISH" if float(hourly.close.iloc[-1]) > float(ema.iloc[-1]) else "BEARISH"
        frame["ema20"] = self.api.ema(frame["close"].astype(float), self.config.ema_5m_length)
        frame["atr14"] = self.api.atr(frame, 14)
        result = self.api.scalp_entry(frame, len(frame) - 1, trend, self.config)
        if result is None:
            return []
        candle = frame.iloc[-1]
        direction: Direction = result["direction"]
        return [StrategySignal(
            strategy_id=self.specification.strategy_id,
            strategy_version=self.specification.version,
            product_id=context.instrument.product_id,
            symbol=context.instrument.symbol,
            direction=direction,
            decision_time=_decision_time(candle.time, "5m"),
            entry_price=None,
            entry_reference="next_5m_open",
            stop_price=float(result["sl_price"]),
            stop_logic="legacy pullback extreme plus configured buffer",
            target_prices=(),
            target_logic=(
                f"partial take profit at {self.config.tp1_rr}R then trail remaining "
                f"position with {self.config.trail_lock_rr}R lock"
            ),
            invalidation_conditions=("stop loss hit", "trend filter reverses"),
            position_sizing_inputs={
                "leverage": self.config.leverage,
                "risk_capital_pct": self.config.sl_capital_pct,
                "max_capital_usage_pct": self.config.max_capital_usage_pct,
                "quantity": "deferred to independent risk sizing",
            },
            provenance={
                "5m": context.provenance.get("5m", "unspecified"),
                "1h": context.provenance.get("1h", "unspecified"),
            },
            diagnostics={**result, "trend": trend},
        )]


class FastSlowMAAdapter:
    def __init__(self, config: Any = None, timeframe: str = "1h"):
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        self.timeframe = timeframe
        self.long_strategy = importlib.import_module("long_strategy")
        self.config = config or self.long_strategy.FAST_SLOW_DEFAULT_CONFIG
        self.specification = StrategySpecification(
            "fast_slow_ma", "legacy-wrapper-v1", (timeframe,),
            ("time", "open", "high", "low", "close", "volume"),
            description="Compatibility wrapper around Fast/Slow MA signals.",
        )

    def evaluate(self, context: StrategyContext) -> list[StrategySignal]:
        frame = context.completed_frame(self.timeframe)
        if frame.empty:
            return []
        results = []
        for direction, function in (
            ("LONG", self.long_strategy.get_long_signal),
            ("SHORT", self.long_strategy.get_short_signal),
        ):
            raw = function(frame, self.config)
            if raw is None:
                continue
            results.append(StrategySignal(
                strategy_id=self.specification.strategy_id,
                strategy_version=self.specification.version,
                product_id=context.instrument.product_id,
                symbol=context.instrument.symbol,
                direction=direction,
                decision_time=_decision_time(raw["entry_candle_time"], self.timeframe),
                entry_price=float(raw["entry"]),
                entry_reference="legacy signal candle close",
                stop_price=float(raw["stop_loss"]),
                stop_logic="legacy ATR-based stop",
                target_prices=(float(raw["take_profit"]),),
                target_logic="legacy configured risk:reward target",
                invalidation_conditions=("opposite fast/slow moving-average crossover",),
                position_sizing_inputs={
                    "legacy_quantity_reference": raw["quantity"],
                    "leverage": raw["leverage"],
                    "quantity": "recomputed by independent risk sizing",
                },
                provenance={self.timeframe: context.provenance.get(self.timeframe, "unspecified")},
                diagnostics=raw,
            ))
        return results


class VPVRStrategyAdapter:
    def __init__(self, strategy_id: str):
        from vpvr import strategies

        detectors = {
            "poc_reclaim": strategies.poc_reclaim,
            "value_area_rejection": strategies.value_area_rejection,
            "lvn_breakout": strategies.lvn_breakout,
        }
        try:
            self.detector = detectors[strategy_id]
        except KeyError as exc:
            raise ValueError(f"unknown VPVR strategy: {strategy_id}") from exc
        self.specification = StrategySpecification(
            strategy_id, "legacy-wrapper-v1", ("1m", "3m", "15m"),
            ("time", "open", "high", "low", "close", "volume"),
            description="Compatibility wrapper around one VPVR strategy detector.",
        )

    def evaluate(self, context: StrategyContext) -> list[StrategySignal]:
        if context.regime is None or context.profile is None:
            return []
        frames = {tf: context.completed_frame(tf) for tf in self.specification.timeframes}
        result = self.detector(
            context.instrument.symbol, frames["1m"], frames["3m"], frames["15m"],
            context.profile, context.regime,
        )
        if result is None:
            return []
        return [StrategySignal(
            strategy_id=result.strategy,
            strategy_version=self.specification.version,
            product_id=context.instrument.product_id,
            symbol=result.symbol,
            direction=result.direction,
            decision_time=_decision_time(result.timestamp, "1m"),
            entry_price=float(result.entry),
            entry_reference="legacy trigger entry",
            stop_price=float(result.stop_loss),
            stop_logic="legacy strategy stop level",
            target_prices=tuple(float(price) for price in result.targets),
            target_logic="legacy ordered volume-profile targets",
            invalidation_conditions=tuple(result.invalidation),
            position_sizing_inputs={"quantity": "deferred to independent risk sizing"},
            provenance={
                timeframe: context.provenance.get(timeframe, "unspecified")
                for timeframe in self.specification.timeframes
            },
            diagnostics=result.as_dict(),
        )]
