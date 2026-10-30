from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from math import ceil

from .market_data import (
    Candle,
    DataQuality,
    Instrument,
    MarketDataProvider,
    TIMEFRAME_SECONDS,
    TickerSnapshot,
    UnsupportedCapabilityError,
)
from .store import ResearchDataStore


class ResearchCollector:
    """Orchestrates public provider reads into the canonical research store."""

    def __init__(self, provider: MarketDataProvider, store: ResearchDataStore):
        self.provider = provider
        self.store = store

    def reconcile_universe(self) -> list[Instrument]:
        instruments = self.provider.fetch_products()
        self.store.reconcile_instruments(
            instruments,
            complete_catalog=self.provider.capabilities.complete_catalog_snapshot,
        )
        return instruments

    def capture(
        self,
        instrument: Instrument,
        timeframe: str,
        start_ts: int,
        end_ts: int,
        book_depth: int = 20,
        ticker_snapshots: list[TickerSnapshot] | None = None,
    ) -> dict:
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        source_timeframe = self._source_timeframe(timeframe)
        if end_ts <= start_ts:
            raise ValueError("end_ts must be greater than start_ts")

        tickers = ticker_snapshots or []
        if ticker_snapshots is None and self.provider.capabilities.ticker_snapshots:
            tickers = self.provider.fetch_tickers()
            self.store.save_tickers(tickers)
        if self.provider.capabilities.ticker_snapshots:
            ticker_match = any(item.product_id == instrument.product_id for item in tickers)
            self._quality(instrument, "ticker", ticker_match, "ticker snapshot not returned for product")
        else:
            self._unsupported(instrument, "ticker")

        candles, derived, source_timeframe = self._fetch_candles(
            instrument, timeframe, start_ts, end_ts, source_timeframe
        )
        self.store.save_candles(candles)
        self.store.save_candles(derived)
        target_available = (
            any(item.is_complete for item in candles)
            if source_timeframe == timeframe else bool(derived)
        )
        if source_timeframe != timeframe:
            self._quality(
                instrument, f"candles:{source_timeframe}",
                any(item.is_complete for item in candles),
                "no complete source candles returned",
            )
        self._quality(
            instrument, f"candles:{timeframe}", target_available,
            "no complete or derived candles returned",
        )

        book = None
        if instrument.state != "live":
            self._quality(instrument, "orderbook_snapshot", False, "instrument is not live")
            book = None
        elif self.provider.capabilities.orderbook_snapshots:
            book = self.provider.fetch_orderbook(instrument, book_depth)
            self.store.save_orderbook(book)
            self._quality(instrument, "orderbook_snapshot", True, "")
        else:
            self._unsupported(instrument, "orderbook_snapshot")

        trades = []
        if instrument.state != "live":
            self._quality(instrument, "trades", False, "instrument is not live")
        elif self.provider.capabilities.timestamped_trades:
            trades = self.provider.fetch_trades(instrument)
            self.store.save_trades(trades)
            self._quality(instrument, "trades", bool(trades), "provider returned no trade prints")
        else:
            self._unsupported(instrument, "trades")

        funding = []
        if self.provider.capabilities.funding_history:
            funding = self.provider.fetch_funding_history(instrument, start_ts, end_ts)
            self.store.save_funding(funding)
            self._quality(
                instrument, "funding", bool(funding),
                "provider returned no funding observations",
            )
        else:
            self._unsupported(instrument, "funding")

        for data_type, available in (
            ("orderbook_deltas", self.provider.capabilities.orderbook_deltas),
            ("open_interest", self.provider.capabilities.open_interest_history),
            ("liquidations", self.provider.capabilities.liquidation_history),
        ):
            if not available:
                self._unsupported(instrument, data_type)

        return {
            "instrument": instrument,
            "timeframe": timeframe,
            "ticker_count": len(tickers),
            "candles": len(candles) if source_timeframe == timeframe else len(derived),
            "source_candles": len(candles),
            "derived_candles": len(derived),
            "orderbook_snapshot": book is not None,
            "trades": len(trades),
            "funding_observations": len(funding),
        }

    def _fetch_candles(
        self,
        instrument: Instrument,
        timeframe: str,
        start_ts: int,
        end_ts: int,
        source_timeframe: str,
    ) -> tuple[list[Candle], list[Candle], str]:
        if source_timeframe == timeframe:
            return self.provider.fetch_candles(
                instrument, timeframe, start_ts, end_ts
            ), [], timeframe
        from .market_data import TIMEFRAME_SECONDS

        target_seconds = TIMEFRAME_SECONDS[timeframe]
        base_timeframe = source_timeframe
        base_seconds = TIMEFRAME_SECONDS[base_timeframe]
        aligned_start = start_ts - start_ts % base_seconds
        aligned_end = ceil(end_ts / base_seconds) * base_seconds
        base_candles = self.provider.fetch_candles(
            instrument, base_timeframe, aligned_start, aligned_end
        )
        start = datetime.fromtimestamp(start_ts, timezone.utc)
        end = datetime.fromtimestamp(end_ts, timezone.utc)
        grouped = defaultdict(list)
        for candle in base_candles:
            bucket = int(candle.start_time.timestamp()) // target_seconds
            grouped[bucket].append(candle)
        derived = []
        expected_count = target_seconds // base_seconds
        for bucket, group in grouped.items():
            bucket_start_ts = bucket * target_seconds
            bucket_start = datetime.fromtimestamp(bucket_start_ts, timezone.utc)
            bucket_end = datetime.fromtimestamp(
                bucket_start_ts + target_seconds, timezone.utc
            )
            if bucket_start < start or bucket_end > end:
                continue
            group.sort(key=lambda candle: candle.start_time)
            expected_times = [
                datetime.fromtimestamp(
                    bucket_start_ts + offset * base_seconds, timezone.utc
                )
                for offset in range(expected_count)
            ]
            if (
                len(group) != expected_count
                or [item.start_time for item in group] != expected_times
                or any(not item.is_complete for item in group)
                or len({item.source for item in group}) != 1
            ):
                continue
            volume_units = {item.volume_unit for item in group}
            derived.append(Candle(
                product_id=instrument.product_id,
                symbol=instrument.symbol,
                timeframe=timeframe,
                start_time=bucket_start,
                open=group[0].open,
                high=max(item.high for item in group),
                low=min(item.low for item in group),
                close=group[-1].close,
                volume=sum(item.volume for item in group),
                source=f"derived:{base_timeframe}:{group[0].source}",
                is_complete=True,
                is_derived=True,
                volume_unit=next(iter(volume_units)) if len(volume_units) == 1 else None,
            ))
        requested_start = datetime.fromtimestamp(start_ts, timezone.utc)
        requested_end = datetime.fromtimestamp(end_ts, timezone.utc)
        stored_base = [
            item for item in base_candles
            if requested_start <= item.start_time < requested_end
        ]
        return stored_base, derived, base_timeframe

    def _source_timeframe(self, timeframe: str) -> str:
        capabilities = self.provider.capabilities.candle_timeframes
        if timeframe in capabilities:
            return timeframe
        from .market_data import TIMEFRAME_SECONDS

        target_seconds = TIMEFRAME_SECONDS[timeframe]
        bases = sorted(
            (
                base for base in capabilities
                if TIMEFRAME_SECONDS[base] < target_seconds
                and target_seconds % TIMEFRAME_SECONDS[base] == 0
            ),
            key=TIMEFRAME_SECONDS.__getitem__,
            reverse=True,
        )
        if not bases:
            raise UnsupportedCapabilityError(
                f"{self.provider.capabilities.provider} cannot provide or derive "
                f"{timeframe} candles"
            )
        return bases[0]

    def capture_many(
        self,
        instruments: list[Instrument],
        timeframe: str,
        start_ts: int,
        end_ts: int,
        book_depth: int = 20,
    ) -> list[dict]:
        tickers: list[TickerSnapshot] | None = None
        if self.provider.capabilities.ticker_snapshots:
            tickers = self.provider.fetch_tickers()
            self.store.save_tickers(tickers)
        return [
            self.capture(
                instrument, timeframe, start_ts, end_ts, book_depth,
                ticker_snapshots=tickers,
            )
            for instrument in instruments
        ]

    def _quality(self, instrument: Instrument, data_type: str, available: bool, reason: str) -> None:
        self.store.save_quality(instrument.product_id, data_type, DataQuality(
            state="available" if available else "unavailable",
            observed_at=datetime.now(timezone.utc),
            source=self.provider.capabilities.provider,
            reasons=() if available else (reason,),
        ))

    def _unsupported(self, instrument: Instrument, data_type: str) -> None:
        self.store.save_quality(instrument.product_id, data_type, DataQuality(
            state="unavailable",
            observed_at=datetime.now(timezone.utc),
            source=self.provider.capabilities.provider,
            reasons=("provider capability is not available",),
        ))

    def record_failure(self, instrument: Instrument, data_type: str, error: Exception) -> None:
        self.store.save_quality(instrument.product_id, data_type, DataQuality(
            state="partial",
            observed_at=datetime.now(timezone.utc),
            source=self.provider.capabilities.provider,
            reasons=(f"{type(error).__name__}: {error}",),
        ))
