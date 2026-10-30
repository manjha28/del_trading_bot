from __future__ import annotations

from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import json
import tempfile
from pathlib import Path
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from research_platform.market_data import Candle, Instrument, TickerSnapshot
from research_platform.server import create_handler, serve
from research_platform.store import ResearchDataStore
from coin_research.store import ResearchStore


UTC = timezone.utc


class ResearchPlatformServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "platform.sqlite3"
        with ResearchDataStore(self.db_path) as store:
            instrument = Instrument(
                "101", "TESTUSD", "perpetual_futures", "live",
                metadata={"tick_size": "0.5"},
            )
            store.reconcile_instruments([instrument], datetime(2026, 1, 1, tzinfo=UTC))
            store.save_tickers([TickerSnapshot(
                "101", "TESTUSD", datetime(2026, 1, 1, tzinfo=UTC),
                "fixture", last_price=100, volume_24h_notional=500,
            )])
            store.save_candles([Candle(
                "101", "TESTUSD", "1m", datetime(2026, 1, 1, tzinfo=UTC),
                100, 102, 99, 101, 10, "fixture", True,
            )])
        coin_path = Path(self.directory.name) / "coin.sqlite3"
        with ResearchStore(coin_path):
            pass
        self.vpvr_path = Path(self.directory.name) / "vpvr.json"
        self.vpvr_path.write_text(json.dumps({
            "mode": "PAPER_ONLY", "balance": 1200, "positions": {},
            "pending": {}, "trades": [{"symbol": "TESTUSD", "net_pnl": 5}],
        }), encoding="utf-8")
        reports = Path(self.directory.name) / "mhr_reports"
        reports.mkdir()
        (reports / "fixture_backtest_summary.csv").write_text("symbol,pnl\nTESTUSD,5\n", encoding="utf-8")
        source_paths = {
            "coin_research_db": coin_path,
            "vpvr_paper_state": self.vpvr_path,
            "mhr_results_dir": reports,
        }
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), create_handler(self.db_path, source_paths)
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.directory.cleanup()

    def get(self, path):
        with urlopen(self.base + path, timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_api_exposes_summary_catalog_details_and_capabilities_read_only(self):
        status, summary = self.get("/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(summary["instrument_count"], 1)
        self.assertEqual(summary["candle_count"], 1)

        _, products = self.get("/api/products?search=test")
        self.assertEqual(products["total"], 1)
        self.assertEqual(products["items"][0]["last_price"], 100)

        _, detail = self.get("/api/products/101")
        self.assertEqual(detail["symbol"], "TESTUSD")
        self.assertEqual(detail["metadata"]["tick_size"], "0.5")

        _, candles = self.get("/api/products/101/candles?timeframe=1m")
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0]["close"], 101)

        _, capabilities = self.get("/api/capabilities")
        self.assertIn("open_interest_history", capabilities["not_implemented"])
        self.assertIn(
            "mhr_scalp_fast_slow_normalized_exit_adapters",
            capabilities["available_research_tools"],
        )

        with ResearchDataStore(self.db_path, read_only=True) as store:
            self.assertEqual(store.coverage_summary()["candle_count"], 1)

    def test_api_rejects_bad_query_and_unknown_product(self):
        with self.assertRaises(HTTPError) as invalid:
            urlopen(self.base + "/api/products?limit=invalid", timeout=3)
        self.assertEqual(invalid.exception.code, 400)
        with self.assertRaises(HTTPError) as missing:
            urlopen(self.base + "/api/products/unknown", timeout=3)
        self.assertEqual(missing.exception.code, 404)

    def test_api_lists_persisted_experiments_and_paper_simulations(self):
        with ResearchDataStore(self.db_path) as store:
            store.save_experiment(
                "exp-1", "fixture", "v1", "dataset-1",
                {"train": "2025", "oos": "2026"}, {"risk": 0.005},
                {"net_pnl": 12.5}, created_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
            store.save_paper_simulation({
                "strategy_id": "fixture",
                "product_id": "101",
                "symbol": "TESTUSD",
                "decision_time": "2026-01-02T00:00:00+00:00",
                "status": "simulated",
                "direction": "SHORT",
                "net_pnl": 12.5,
                "reason": "target hit",
            }, "exp-1")
            store.save_variant_evaluation(
                "failed_high_vwap_retest", "101", "TESTUSD",
                datetime(2026, 1, 2, tzinfo=UTC), "no_signal",
                "fixture evaluation",
            )

        _, summary = self.get("/api/summary")
        self.assertEqual(summary["experiment_count"], 1)
        self.assertEqual(summary["paper_simulation_count"], 1)
        self.assertEqual(summary["variant_evaluation_count"], 1)

        _, experiments = self.get("/api/experiments")
        self.assertEqual(experiments["total"], 1)
        self.assertEqual(experiments["items"][0]["metrics"]["net_pnl"], 12.5)

        _, simulations = self.get("/api/simulations?symbol=TESTUSD")
        self.assertEqual(simulations["total"], 1)
        self.assertEqual(simulations["items"][0]["result"]["direction"], "SHORT")

        _, evaluations = self.get("/api/variant-evaluations?variant_id=failed_high_vwap_retest")
        self.assertEqual(evaluations["total"], 1)
        self.assertEqual(evaluations["items"][0]["status"], "no_signal")

    def test_api_exposes_variant_gates_and_aggregates_existing_sources_read_only(self):
        _, variants = self.get("/api/variants")
        catalog = {item["strategy_id"]: item for item in variants["items"]}
        self.assertEqual(catalog["liquidation_cascade"]["status"], "unavailable")
        self.assertIn("OHLCV cannot establish absorption",
                      " ".join(catalog["absorption_price_rejection"]["limitations"]))

        before = self.vpvr_path.read_bytes()
        _, sources = self.get("/api/sources")
        self.assertEqual(sources["coin_research"]["status"], "available")
        self.assertEqual(sources["vpvr_paper"]["mode"], "PAPER_ONLY")
        self.assertEqual(sources["vpvr_paper"]["trade_count"], 1)
        self.assertEqual(sources["mhr_reports"]["report_count"], 1)
        self.assertEqual(self.vpvr_path.read_bytes(), before)

    def test_server_rejects_non_loopback_bind(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            serve(self.db_path, host="0.0.0.0", port=8766)


if __name__ == "__main__":
    unittest.main()
