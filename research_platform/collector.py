from __future__ import annotations

from datetime import datetime, timezone

from .market_data import (
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
        if timeframe not in self.provider.capabilities.candle_timeframes:
            raise UnsupportedCapabilityError(
                f"{self.provider.capabilities.provider} does not provide {timeframe} candles"
            )
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

        candles = self.provider.fetch_candles(instrument, timeframe, start_ts, end_ts)
        self.store.save_candles(candles)
        self._quality(instrument, f"candles:{timeframe}", bool(candles), "no candles returned")

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
            "candles": len(candles),
            "orderbook_snapshot": book is not None,
            "trades": len(trades),
            "funding_observations": len(funding),
        }

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
