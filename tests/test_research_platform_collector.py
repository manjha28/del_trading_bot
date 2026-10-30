from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from research_platform.collector import ResearchCollector
from research_platform.market_data import (
    BookLevel,
    Candle,
    FundingObservation,
    Instrument,
    OrderBookSnapshot,
    ProviderCapabilities,
    TickerSnapshot,
    TradePrint,
    UnsupportedCapabilityError,
)
from research_platform.store import ResearchDataStore


UTC = timezone.utc


class FixtureProvider:
    capabilities = ProviderCapabilities(
        provider="fixture",
        catalog_states=frozenset({"live", "expired"}),
        candle_timeframes=frozenset({"1m"}),
        complete_catalog_snapshot=True,
        ticker_snapshots=True,
        orderbook_snapshots=True,
        orderbook_deltas=False,
        timestamped_trades=True,
        funding_history=False,
        open_interest_history=False,
        liquidation_history=False,
    )

    def __init__(self):
        self.instrument = Instrument("1", "TESTUSD", "perpetual_futures", "live")
        self.instruments = [self.instrument]
        self.ticker_calls = 0

    def fetch_products(self):
        return self.instruments

    def fetch_tickers(self):
        self.ticker_calls += 1
        return [
            TickerSnapshot(
                item.product_id, item.symbol, datetime(2026, 1, 1, tzinfo=UTC), "fixture",
                last_price=100, volume_24h_notional=500,
            )
            for item in self.instruments
        ]

    def fetch_candles(self, instrument, timeframe, start_ts, end_ts):
        return [Candle(
            instrument.product_id, instrument.symbol, timeframe,
            datetime(2026, 1, 1, tzinfo=UTC), 100, 102, 99, 101, 10,
            "fixture", True,
        )]

    def fetch_orderbook(self, instrument, depth=20):
        return OrderBookSnapshot(
            instrument.product_id, instrument.symbol, datetime(2026, 1, 1, tzinfo=UTC),
            "fixture", (BookLevel(100, 5),), (BookLevel(101, 4),),
        )

    def fetch_trades(self, instrument):
        return [TradePrint(
            instrument.product_id, instrument.symbol, datetime(2026, 1, 1, tzinfo=UTC),
            100, 1, "buy", "fixture", "trade-1",
        )]

    def fetch_funding_history(self, instrument, start_ts, end_ts, resolution="1h"):
        raise AssertionError("unsupported funding endpoint must not be called")


class OneMinuteHistoryProvider(FixtureProvider):
    def __init__(self, missing_middle=False):
        super().__init__()
        self.missing_middle = missing_middle

    def fetch_candles(self, instrument, timeframe, start_ts, end_ts):
        starts = [0, 120] if self.missing_middle else [0, 60, 120]
        return [
            Candle(
                instrument.product_id, instrument.symbol, "1m",
                datetime.fromtimestamp(start_ts + offset, UTC),
                100 + offset / 60, 102 + offset / 60, 99 + offset / 60,
                101 + offset / 60, 2, "fixture", True,
            )
            for offset in starts
        ]


class ResearchCollectorTests(unittest.TestCase):
    def test_collection_persists_supported_data_and_explicit_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchDataStore(Path(directory) / "research.sqlite3")
            provider = FixtureProvider()
            collector = ResearchCollector(provider, store)
            instruments = collector.reconcile_universe()
            result = collector.capture(instruments[0], "1m", 1767225600, 1767225660)
            summary = store.coverage_summary()
            self.assertEqual(result["candles"], 1)
            self.assertEqual(result["trades"], 1)
            self.assertEqual(summary["instrument_count"], 1)
            self.assertEqual(summary["candle_count"], 1)
            self.assertEqual(summary["ticker_count"], 1)
            self.assertEqual(summary["trade_count"], 1)
            self.assertEqual(summary["orderbook_snapshot_count"], 1)
            self.assertEqual(summary["funding_observation_count"], 0)
            self.assertEqual(
                summary["data_quality_observations"],
                {
                    "candles:1m": 1,
                    "funding": 1,
                    "liquidations": 1,
                    "open_interest": 1,
                    "orderbook_deltas": 1,
                    "orderbook_snapshot": 1,
                    "ticker": 1,
                    "trades": 1,
                },
            )
            store.close()

    def test_derives_aligned_candles_only_from_complete_source_intervals(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchDataStore(Path(directory) / "research.sqlite3")
            provider = OneMinuteHistoryProvider()
            collector = ResearchCollector(provider, store)
            collector.reconcile_universe()
            start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
            result = collector.capture(
                provider.instrument, "3m", start, start + 180
            )
            self.assertEqual(result["candles"], 1)
            self.assertEqual(result["source_candles"], 3)
            self.assertEqual(result["derived_candles"], 1)
            candle = store.load_candles("1", "3m")[0]
            self.assertTrue(candle.is_derived)
            self.assertEqual(candle.source, "derived:1m:fixture")
            self.assertEqual(candle.volume, 6)
            self.assertEqual((candle.high, candle.low, candle.close), (104, 99, 103))
            store.close()

    def test_missing_source_minute_does_not_create_aggregate_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchDataStore(Path(directory) / "research.sqlite3")
            provider = OneMinuteHistoryProvider(missing_middle=True)
            collector = ResearchCollector(provider, store)
            collector.reconcile_universe()
            start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
            result = collector.capture(
                provider.instrument, "3m", start, start + 180
            )
            self.assertEqual(result["candles"], 0)
            self.assertEqual(store.load_candles("1", "3m"), [])
            quality = store.instrument_detail("1")["data_quality"]
            target = next(item for item in quality if item["data_type"] == "candles:3m")
            self.assertEqual(target["state"], "unavailable")
            store.close()

    def test_collection_rejects_unsupported_timeframe_without_network_calls(self):
        provider = FixtureProvider()
        provider.capabilities = replace(
            provider.capabilities, candle_timeframes=frozenset()
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchDataStore(Path(directory) / "research.sqlite3")
            collector = ResearchCollector(provider, store)
            collector.reconcile_universe()
            with self.assertRaisesRegex(UnsupportedCapabilityError, "cannot provide or derive 5m"):
                collector.capture(provider.instrument, "5m", 1, 301)
            store.close()

    def test_batch_collection_fetches_shared_tickers_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchDataStore(Path(directory) / "research.sqlite3")
            provider = FixtureProvider()
            provider.instruments.append(
                Instrument("2", "OTHERUSD", "perpetual_futures", "expired")
            )
            collector = ResearchCollector(provider, store)
            instruments = collector.reconcile_universe()
            results = collector.capture_many(instruments, "1m", 1767225600, 1767225660)
            self.assertEqual(len(results), 2)
            self.assertEqual(provider.ticker_calls, 1)
            self.assertEqual(store.coverage_summary()["ticker_count"], 2)
            self.assertEqual(store.coverage_summary()["orderbook_snapshot_count"], 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
