from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from research_platform.market_data import (
    BookLevel,
    Candle,
    DataQuality,
    FundingObservation,
    Instrument,
    OrderBookSnapshot,
    TickerSnapshot,
    TradePrint,
)
from research_platform.store import ResearchDataStore


UTC = timezone.utc


class ResearchDataStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ResearchDataStore(Path(self.directory.name) / "research.sqlite3")
        self.instrument = Instrument(
            "1001", "TESTUSD", "perpetual_futures", "live",
            metadata={"tick_size": "0.01"},
        )
        self.store.reconcile_instruments([self.instrument], datetime(2026, 1, 1, tzinfo=UTC))

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_incomplete_catalog_does_not_mark_missing_products_inactive(self):
        other = Instrument("1002", "OTHERUSD", "perpetual_futures", "live")
        self.store.reconcile_instruments([other], datetime(2026, 1, 2, tzinfo=UTC))
        states = {item.product_id: item.state for item in self.store.list_instruments()}
        self.assertEqual(states["1001"], "live")
        self.assertEqual(states["1002"], "live")

    def test_authoritative_catalog_retains_unreturned_product_record(self):
        self.store.reconcile_instruments(
            [], datetime(2026, 1, 3, tzinfo=UTC), complete_catalog=True,
        )
        stored = self.store.list_instruments()[0]
        self.assertEqual(stored.state, "not_returned")
        self.assertEqual(stored.metadata["tick_size"], "0.01")

    def test_candles_round_trip_and_upsert_per_provider(self):
        candle = Candle(
            "1001", "TESTUSD", "1m", datetime(2026, 1, 1, tzinfo=UTC),
            10, 12, 9, 11, 5, "adapter-a", True, volume_unit="contracts",
        )
        self.store.save_candles([candle])
        self.store.save_candles([Candle(
            **{**candle.__dict__, "close": 10.5, "source": "adapter-a"}
        )])
        self.store.save_candles([Candle(
            **{**candle.__dict__, "source": "adapter-b"}
        )])
        loaded = self.store.load_candles("1001", "1m")
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].close, 10.5)
        self.assertEqual(loaded[0].volume_unit, "contracts")

    def test_market_observations_preserve_sources_and_known_timestamps(self):
        timestamp = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
        self.store.save_tickers([TickerSnapshot(
            "1001", "TESTUSD", timestamp, "ticker-adapter",
            last_price=10.5, volume_24h_notional=1200, raw={"quote_volume": 1200},
        )])
        trade = TradePrint(
            "1001", "TESTUSD", timestamp, 10.5, 2, "buy", "trade-adapter", "trade-1",
        )
        self.store.save_trades([trade, trade])
        self.store.save_trades([
            TradePrint("1001", "TESTUSD", timestamp, 10.5, 2, "buy", "trade-adapter")
        ] * 2)
        self.store.save_orderbook(OrderBookSnapshot(
            "1001", "TESTUSD", timestamp, "book-adapter",
            (BookLevel(10, 2),), (BookLevel(11, 3),),
        ))
        self.store.save_funding([FundingObservation(
            "1001", "TESTUSD", timestamp, 0.0002, "funding-adapter",
        )])
        self.store.save_quality("1001", "trades", DataQuality(
            "partial", timestamp, "trade-adapter", 0.8, ("trade endpoint is bounded",),
        ))
        summary = self.store.coverage_summary()
        self.assertEqual(summary["ticker_count"], 1)
        self.assertEqual(summary["trade_count"], 3)
        self.assertEqual(summary["orderbook_snapshot_count"], 1)
        self.assertEqual(summary["funding_observation_count"], 1)
        self.assertEqual(summary["data_quality_observations"], {"trades": 1})


if __name__ == "__main__":
    unittest.main()
