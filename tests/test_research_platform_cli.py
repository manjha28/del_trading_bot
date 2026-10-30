from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_platform.cli import _run_scan, main
from research_platform.collector import ResearchCollector
from research_platform.market_data import (
    Candle,
    Instrument,
    ProviderCapabilities,
)
from research_platform.store import ResearchDataStore


UTC = timezone.utc


class CLIProvider:
    capabilities = ProviderCapabilities(
        provider="cli-fixture",
        catalog_states=frozenset({"live"}),
        candle_timeframes=frozenset({"1m"}),
        complete_catalog_snapshot=True,
        ticker_snapshots=False,
        orderbook_snapshots=False,
        orderbook_deltas=False,
        timestamped_trades=False,
        funding_history=False,
        open_interest_history=False,
        liquidation_history=False,
    )

    def __init__(self):
        self.instruments = [
            Instrument("1", "GOODUSD", "perpetual_futures", "live"),
            Instrument("2", "BADUSD", "perpetual_futures", "live"),
        ]

    def fetch_products(self):
        return self.instruments

    def fetch_candles(self, instrument, timeframe, start_ts, end_ts):
        if instrument.symbol == "BADUSD":
            raise RuntimeError("fixture candle outage")
        return [Candle(
            instrument.product_id, instrument.symbol, timeframe,
            datetime(2026, 1, 1, tzinfo=UTC), 100, 101, 99, 100, 10,
            "cli-fixture", True,
        )]


class ResearchPlatformCLITests(unittest.TestCase):
    def test_scan_reports_per_product_failure_and_keeps_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchDataStore(Path(directory) / "platform.sqlite3")
            provider = CLIProvider()
            collector = ResearchCollector(provider, store)
            args = Namespace(timeframe="1m", lookback_hours=24)
            result = _run_scan(collector, args)
            self.assertEqual(result["catalog_count"], 2)
            self.assertEqual(result["failures"], 1)
            self.assertEqual(result["coverage"]["instrument_count"], 2)
            detail = store.instrument_detail("2")
            quality = next(
                item for item in detail["data_quality"]
                if item["data_type"] == "collection:1m"
            )
            self.assertEqual(quality["state"], "partial")
            self.assertIn("fixture candle outage", quality["reasons"][0])
            store.close()

    def test_cli_validates_collection_parameters_without_network_access(self):
        with patch("research_platform.cli.logging.basicConfig"):
            with self.assertRaises(SystemExit) as result:
                main(["collect", "--lookback-hours", "0"])
        self.assertEqual(result.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
