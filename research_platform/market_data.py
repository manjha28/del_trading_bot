from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

import pandas as pd


TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

AggressorSide = Literal["buy", "sell", "unknown"]
IntegrityState = Literal["not_provided", "synchronized", "gap_detected"]
QualityState = Literal["available", "partial", "unavailable", "stale", "unknown"]


class UnsupportedCapabilityError(RuntimeError):
    """Raised when a provider cannot supply a requested observation."""


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _positive_finite(value: float, field_name: str, allow_zero: bool = False) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        bound = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{field_name} must be finite and {bound}")
    return result


@dataclass(frozen=True)
class ContractSpecifications:
    contract_multiplier: float | None
    unit_currency: str | None
    tick_size: float | None
    lot_increment: float | None
    minimum_quantity: float | None
    maximum_quantity: float | None
    verified: bool
    verification_source: str | None
    verification_errors: tuple[str, ...] = ()

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any]) -> ContractSpecifications:
        verification = metadata.get("contract_spec_verification") or {}
        if not isinstance(verification, Mapping):
            verification = {}

        def number(value: Any) -> float | None:
            if value is None:
                return None
            try:
                result = float(value)
            except (TypeError, ValueError):
                return None
            return result if math.isfinite(result) and result > 0 else None

        verified = verification.get("spec_verified") is True
        errors = tuple(str(item) for item in verification.get("verification_errors", ()))
        if not verified and not errors:
            errors = ("contract sizing has no trusted verification",)
        return cls(
            contract_multiplier=number(metadata.get("contract_value")),
            unit_currency=(
                str(metadata["contract_unit_currency"])
                if metadata.get("contract_unit_currency") else None
            ),
            tick_size=number(metadata.get("tick_size")),
            lot_increment=number(
                verification.get("lot_size") if verified
                else metadata.get("size_increment")
            ),
            minimum_quantity=number(
                verification.get("minimum_order_contracts") if verified
                else metadata.get("minimum_order_contracts")
            ),
            maximum_quantity=number(metadata.get("position_size_limit")),
            verified=verified,
            verification_source=verification.get("verification_policy") if verified else None,
            verification_errors=errors,
        )


@dataclass(frozen=True)
class Instrument:
    product_id: str
    symbol: str
    contract_type: str
    state: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    contract_specs: ContractSpecifications | None = None

    def __post_init__(self) -> None:
        if not self.product_id.strip() or not self.symbol.strip():
            raise ValueError("instrument product_id and symbol are required")
        if not self.contract_type.strip() or not self.state.strip():
            raise ValueError("instrument contract_type and state are required")
        if self.contract_specs is None:
            object.__setattr__(
                self, "contract_specs", ContractSpecifications.from_metadata(self.metadata)
            )

    @classmethod
    def from_delta(cls, product: Mapping[str, Any]) -> Instrument:
        symbol = str(product.get("symbol") or "").strip()
        if not symbol:
            raise ValueError("Delta product is missing symbol")
        product_id = product.get("id")
        if product_id is None:
            product_id = symbol
        contract_type = str(product.get("contract_type") or "")
        state = str(product.get("state") or "unknown")
        return cls(
            product_id=str(product_id),
            symbol=symbol,
            contract_type=contract_type,
            state=state,
            metadata=dict(product),
        )


@dataclass(frozen=True)
class DataQuality:
    state: QualityState
    observed_at: datetime | None
    source: str
    completeness: float | None = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in ("available", "partial", "unavailable", "stale", "unknown"):
            raise ValueError(f"invalid data quality state: {self.state}")
        if not self.source.strip():
            raise ValueError("quality source is required")
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        if self.completeness is not None:
            value = float(self.completeness)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("completeness must be between 0 and 1")
            object.__setattr__(self, "completeness", value)


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    catalog_states: frozenset[str]
    candle_timeframes: frozenset[str]
    complete_catalog_snapshot: bool
    ticker_snapshots: bool
    orderbook_snapshots: bool
    orderbook_deltas: bool
    timestamped_trades: bool
    funding_history: bool
    open_interest_history: bool
    liquidation_history: bool

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider name is required")
        unsupported = self.candle_timeframes.difference(TIMEFRAME_SECONDS)
        if unsupported:
            raise ValueError(f"unknown candle timeframes: {sorted(unsupported)}")


@dataclass(frozen=True)
class TickerSnapshot:
    product_id: str
    symbol: str
    observed_at: datetime
    source: str
    last_price: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    volume_24h_notional: float | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.product_id or not self.symbol or not self.source:
            raise ValueError("ticker product_id, symbol, and source are required")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        for name in ("last_price", "mark_price", "index_price", "volume_24h_notional"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self, name, _positive_finite(value, name, allow_zero=name == "volume_24h_notional")
                )


@dataclass(frozen=True)
class Candle:
    product_id: str
    symbol: str
    timeframe: str
    start_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str
    is_complete: bool
    is_derived: bool = False
    volume_unit: str | None = None

    def __post_init__(self) -> None:
        if self.timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"unsupported canonical timeframe: {self.timeframe}")
        if not self.product_id or not self.symbol or not self.source:
            raise ValueError("candle product_id, symbol, and source are required")
        object.__setattr__(self, "start_time", _aware(self.start_time, "start_time"))
        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, _positive_finite(getattr(self, name), name))
        object.__setattr__(self, "volume", _positive_finite(self.volume, "volume", allow_zero=True))
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("candle high is inconsistent with OHLC values")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("candle low is inconsistent with OHLC values")

    @property
    def close_time(self) -> datetime:
        from datetime import timedelta

        return self.start_time + timedelta(seconds=TIMEFRAME_SECONDS[self.timeframe])


@dataclass(frozen=True)
class TradePrint:
    product_id: str
    symbol: str
    timestamp: datetime
    price: float
    quantity: float
    aggressor_side: AggressorSide
    source: str
    trade_id: str | None = None

    def __post_init__(self) -> None:
        if not self.product_id or not self.symbol or not self.source:
            raise ValueError("trade product_id, symbol, and source are required")
        object.__setattr__(self, "timestamp", _aware(self.timestamp, "timestamp"))
        object.__setattr__(self, "price", _positive_finite(self.price, "price"))
        object.__setattr__(self, "quantity", _positive_finite(self.quantity, "quantity"))
        if self.aggressor_side not in ("buy", "sell", "unknown"):
            raise ValueError("aggressor_side must be buy, sell, or unknown")


@dataclass(frozen=True)
class BookLevel:
    price: float
    quantity: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _positive_finite(self.price, "price"))
        object.__setattr__(self, "quantity", _positive_finite(self.quantity, "quantity", allow_zero=True))


@dataclass(frozen=True)
class OrderBookSnapshot:
    product_id: str
    symbol: str
    observed_at: datetime
    source: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    exchange_timestamp: datetime | None = None
    sequence: int | None = None
    integrity: IntegrityState = "not_provided"

    def __post_init__(self) -> None:
        if not self.product_id or not self.symbol or not self.source:
            raise ValueError("book product_id, symbol, and source are required")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        if self.exchange_timestamp is not None:
            object.__setattr__(
                self, "exchange_timestamp", _aware(self.exchange_timestamp, "exchange_timestamp")
            )
        if self.sequence is not None and self.sequence < 0:
            raise ValueError("book sequence must be non-negative")
        if self.integrity not in ("not_provided", "synchronized", "gap_detected"):
            raise ValueError("invalid order-book integrity state")


@dataclass(frozen=True)
class FundingObservation:
    product_id: str
    symbol: str
    timestamp: datetime
    rate: float
    source: str

    def __post_init__(self) -> None:
        if not self.product_id or not self.symbol or not self.source:
            raise ValueError("funding product_id, symbol, and source are required")
        object.__setattr__(self, "timestamp", _aware(self.timestamp, "timestamp"))
        rate = float(self.rate)
        if not math.isfinite(rate):
            raise ValueError("funding rate must be finite")
        object.__setattr__(self, "rate", rate)


@runtime_checkable
class MarketDataProvider(Protocol):
    capabilities: ProviderCapabilities

    def fetch_products(self) -> list[Instrument]: ...

    def fetch_tickers(self) -> list[TickerSnapshot]: ...

    def fetch_candles(
        self, instrument: Instrument, timeframe: str, start_ts: int, end_ts: int
    ) -> list[Candle]: ...

    def fetch_orderbook(self, instrument: Instrument, depth: int = 20) -> OrderBookSnapshot: ...

    def fetch_trades(self, instrument: Instrument) -> list[TradePrint]: ...

    def fetch_funding_history(
        self, instrument: Instrument, start_ts: int, end_ts: int, resolution: str = "1h"
    ) -> list[FundingObservation]: ...


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _aware(value, "timestamp")
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            pass
    if isinstance(value, (int, float)):
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("timestamp must be finite")
        unit = "us" if abs(numeric) > 1e14 else ("ms" if abs(numeric) > 1e11 else "s")
        return pd.to_datetime(numeric, unit=unit, utc=True).to_pydatetime()
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.to_pydatetime().astimezone(timezone.utc)


def _frame_to_candles(
    frame: pd.DataFrame,
    instrument: Instrument,
    timeframe: str,
    source: str,
    end_ts: int,
) -> list[Candle]:
    candles = []
    now = datetime.now(timezone.utc)
    requested_end = datetime.fromtimestamp(end_ts, timezone.utc)
    completeness_cutoff = min(now, requested_end)
    for row in frame.itertuples(index=False):
        start = _timestamp(row.time)
        close_time = start.timestamp() + TIMEFRAME_SECONDS[timeframe]
        candles.append(Candle(
            product_id=instrument.product_id,
            symbol=instrument.symbol,
            timeframe=timeframe,
            start_time=start,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
            source=source,
            is_complete=close_time <= completeness_cutoff.timestamp(),
        ))
    return candles


def _book_levels(raw_levels: list[Any]) -> tuple[BookLevel, ...]:
    levels = []
    for raw in raw_levels:
        if isinstance(raw, Mapping):
            price = raw.get("price")
            quantity = raw.get("size", raw.get("quantity", raw.get("volume")))
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            price, quantity = raw[0], raw[1]
        else:
            raise ValueError("unsupported order-book level shape")
        if price is None or quantity is None:
            raise ValueError("order-book level is missing price or quantity")
        levels.append(BookLevel(float(price), float(quantity)))
    return tuple(levels)


class _Adapter:
    source: str
    capabilities: ProviderCapabilities

    def __init__(self, client: Any):
        self.client = client
        self._products_by_symbol: dict[str, Instrument] = {}

    def fetch_products(self) -> list[Instrument]:
        products = [
            Instrument.from_delta(
                dict(row) | ({
                    "contract_spec_verification": self.client.contract_specs(row)
                } if callable(getattr(self.client, "contract_specs", None)) else {})
            )
            for row in self.client.fetch_products()
            if row.get("contract_type") == "perpetual_futures"
        ]
        self._products_by_symbol = {product.symbol: product for product in products}
        return products

    def fetch_tickers(self) -> list[TickerSnapshot]:
        if not self.capabilities.ticker_snapshots:
            raise UnsupportedCapabilityError(f"{self.source} has no ticker snapshots")
        rows = self._fetch_tickers()
        observed_at = datetime.now(timezone.utc)
        result = []
        for raw in rows:
            symbol = str(raw.get("symbol") or "")
            product = self._products_by_symbol.get(symbol)
            product_id = (
                product.product_id if product is not None
                else str(raw.get("product_id") or symbol)
            )
            if not symbol:
                continue
            result.append(TickerSnapshot(
                product_id=product_id,
                symbol=symbol,
                observed_at=observed_at,
                source=self.source,
                last_price=_optional_price(raw.get("close")),
                mark_price=_optional_price(raw.get("mark_price")),
                index_price=_optional_price(raw.get("index_price")),
                volume_24h_notional=_optional_number(next(
                    (raw.get(key) for key in ("quote_volume", "turnover_usd")
                     if raw.get(key) is not None),
                    None,
                )),
                raw=dict(raw),
            ))
        return result

    def _fetch_tickers(self) -> list[dict]:
        raise NotImplementedError

    def fetch_candles(
        self, instrument: Instrument, timeframe: str, start_ts: int, end_ts: int
    ) -> list[Candle]:
        if timeframe not in self.capabilities.candle_timeframes:
            raise UnsupportedCapabilityError(
                f"{self.source} does not provide {timeframe} candles"
            )
        if end_ts <= start_ts:
            raise ValueError("end_ts must be greater than start_ts")
        frame = self._fetch_candle_frame(instrument.symbol, timeframe, start_ts, end_ts)
        return _frame_to_candles(frame, instrument, timeframe, self.source, end_ts)

    def _fetch_candle_frame(
        self, symbol: str, timeframe: str, start_ts: int, end_ts: int
    ) -> pd.DataFrame:
        raise NotImplementedError

    def fetch_orderbook(self, instrument: Instrument, depth: int = 20) -> OrderBookSnapshot:
        if not self.capabilities.orderbook_snapshots:
            raise UnsupportedCapabilityError(f"{self.source} has no order-book snapshots")
        book = self._fetch_book(instrument.symbol, depth)
        timestamp = datetime.now(timezone.utc)
        exchange_ts = book.get("timestamp")
        if exchange_ts is not None:
            exchange_ts = _timestamp(exchange_ts)
        sequence = book.get("sequence")
        if sequence is not None:
            sequence = int(sequence)
        return OrderBookSnapshot(
            product_id=instrument.product_id,
            symbol=instrument.symbol,
            observed_at=timestamp,
            exchange_timestamp=exchange_ts,
            sequence=sequence,
            integrity="not_provided",
            source=self.source,
            bids=_book_levels(book.get("buy", book.get("bids", []))),
            asks=_book_levels(book.get("sell", book.get("asks", []))),
        )

    def _fetch_book(self, symbol: str, depth: int) -> dict:
        raise NotImplementedError

    def fetch_trades(self, instrument: Instrument) -> list[TradePrint]:
        if not self.capabilities.timestamped_trades:
            raise UnsupportedCapabilityError(f"{self.source} has no timestamped trade prints")
        frame = self._fetch_trade_frame(instrument.symbol)
        result = []
        for row in frame.itertuples(index=False):
            side = str(row.side).lower()
            if side not in ("buy", "sell"):
                raise ValueError(f"unsupported aggressor side from {self.source}: {side}")
            result.append(TradePrint(
                product_id=instrument.product_id,
                symbol=instrument.symbol,
                timestamp=_timestamp(row.time),
                price=float(row.price),
                quantity=float(row.volume),
                aggressor_side=side,
                source=self.source,
                trade_id=(str(row.trade_id) if hasattr(row, "trade_id") and pd.notna(row.trade_id) else None),
            ))
        return result

    def _fetch_trade_frame(self, symbol: str) -> pd.DataFrame:
        raise NotImplementedError

    def fetch_funding_history(
        self, instrument: Instrument, start_ts: int, end_ts: int, resolution: str = "1h"
    ) -> list[FundingObservation]:
        if not self.capabilities.funding_history:
            raise UnsupportedCapabilityError(f"{self.source} has no timestamped funding history")
        if end_ts <= start_ts:
            raise ValueError("end_ts must be greater than start_ts")
        frame = self.client.fetch_funding_history(
            instrument.symbol, start_ts, end_ts, resolution=resolution
        )
        return [
            FundingObservation(
                product_id=instrument.product_id,
                symbol=instrument.symbol,
                timestamp=_timestamp(row.time),
                rate=float(row.funding_rate),
                source=self.source,
            )
            for row in frame.itertuples(index=False)
        ]


class DeltaResearchAdapter(_Adapter):
    """Canonical adapter over the full-catalog coin research REST client."""

    source = "delta_research_rest"
    capabilities = ProviderCapabilities(
        provider=source,
        catalog_states=frozenset({"upcoming", "live", "expired", "settled"}),
        candle_timeframes=frozenset({"1m", "5m", "1h"}),
        complete_catalog_snapshot=True,
        ticker_snapshots=True,
        orderbook_snapshots=True,
        orderbook_deltas=False,
        timestamped_trades=True,
        funding_history=False,
        open_interest_history=False,
        liquidation_history=False,
    )

    def _fetch_candle_frame(self, symbol: str, timeframe: str, start_ts: int, end_ts: int) -> pd.DataFrame:
        return self.client.fetch_candles(symbol, timeframe, start_ts, end_ts)

    def _fetch_tickers(self) -> list[dict]:
        return list(self.client.fetch_tickers().values())

    def _fetch_book(self, symbol: str, depth: int) -> dict:
        return self.client.fetch_book(symbol, depth)

    def _fetch_trade_frame(self, symbol: str) -> pd.DataFrame:
        return self.client.fetch_trades(symbol)


class CoinResearchAdapter(DeltaResearchAdapter):
    """Compatibility name for the canonical adapter over coin_research."""


class VPVRDataAdapter(_Adapter):
    """Canonical adapter over VPVR's narrower public market-data client."""

    source = "vpvr_delta_rest"
    capabilities = ProviderCapabilities(
        provider=source,
        catalog_states=frozenset({"live"}),
        candle_timeframes=frozenset({"1m", "3m", "15m"}),
        complete_catalog_snapshot=False,
        ticker_snapshots=True,
        orderbook_snapshots=True,
        orderbook_deltas=False,
        timestamped_trades=True,
        funding_history=True,
        open_interest_history=False,
        liquidation_history=False,
    )

    def _fetch_candle_frame(self, symbol: str, timeframe: str, start_ts: int, end_ts: int) -> pd.DataFrame:
        return self.client.fetch_candles_range(symbol, timeframe, start_ts, end_ts)

    def _fetch_tickers(self) -> list[dict]:
        return self.client.fetch_tickers()

    def _fetch_book(self, symbol: str, depth: int) -> dict:
        return self.client.fetch_orderbook(symbol, depth)

    def _fetch_trade_frame(self, symbol: str) -> pd.DataFrame:
        return self.client.fetch_public_trades(symbol)


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _optional_price(value: Any) -> float | None:
    number = _optional_number(value)
    return number if number is not None and number > 0 else None
