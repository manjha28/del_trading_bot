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
        self.assertIn(
            "validation_only_regime_aware_strategy_selection",
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
            store.save_experiment(
                "selector-1", "validated_strategy_selector", "v1", "dataset-1",
                {"fold": 0}, {"mode": "paper_only"},
                {"selection": {"no_trade": True, "selected_count": 0}},
                created_at=datetime(2026, 1, 3, tzinfo=UTC),
            )

        _, summary = self.get("/api/summary")
        self.assertEqual(summary["experiment_count"], 2)
        self.assertEqual(summary["paper_simulation_count"], 1)
        self.assertEqual(summary["variant_evaluation_count"], 1)

        _, experiments = self.get("/api/experiments")
        self.assertEqual(experiments["total"], 2)
        legacy = next(item for item in experiments["items"] if item["experiment_id"] == "exp-1")
        self.assertEqual(legacy["metrics"]["net_pnl"], 12.5)

        _, simulations = self.get("/api/simulations?symbol=TESTUSD")
        self.assertEqual(simulations["total"], 1)
        self.assertEqual(simulations["items"][0]["result"]["direction"], "SHORT")

        _, evaluations = self.get("/api/variant-evaluations?variant_id=failed_high_vwap_retest")
        self.assertEqual(evaluations["total"], 1)
        self.assertEqual(evaluations["items"][0]["status"], "no_signal")
        _, selector = self.get("/api/selector")
        self.assertEqual(selector["total"], 1)
        self.assertTrue(selector["items"][0]["metrics"]["selection"]["no_trade"])
        _, filtered = self.get("/api/experiments?strategy_id=validated_strategy_selector")
        self.assertEqual(filtered["total"], 1)

    def test_api_exposes_durable_stream_events_and_health(self):
        timestamp = datetime(2026, 1, 2, tzinfo=UTC)
        with ResearchDataStore(self.db_path) as store:
            store.save_market_event(
                "101", "TESTUSD", "ob_l2", timestamp,
                {"book": {"buy": [], "sell": []}},
                integrity="not_provided",
            )
            store.record_stream_health(
                "delta-public-000", timestamp, "connected", True, 0, 0, ["TESTUSD"],
            )

        _, summary = self.get("/api/summary")
        self.assertEqual(summary["market_event_count"], 1)
        _, events = self.get("/api/market-events?event_type=ob_l2")
        self.assertEqual(events["items"][0]["integrity"], "not_provided")
        _, health = self.get("/api/stream-health")
        self.assertTrue(health["items"][0]["connected"])
        self.assertEqual(health["items"][0]["symbols"], ["TESTUSD"])

    def test_leaderboard_aggregates_shared_oos_folds_and_compares_selector(self):
        config = {
            "initial_equity": 10_000,
            "max_open_positions": 3,
            "max_portfolio_risk_pct": 0.015,
            "max_gross_leverage": 2.0,
            "max_pairwise_correlation": 0.8,
            "correlation_lookback_minutes": 60,
            "minimum_correlation_observations": 30,
            "max_daily_realized_loss_pct": 0.02,
            "max_realized_drawdown_pct": 0.10,
            "trade_risk_pct": 0.005,
            "trade_leverage": 2.0,
            "fee_rate": 0.0005,
            "spread_bps": 5,
            "slippage_bps": 2,
        }
        split = {
            "fold": 0,
            "out_of_sample_start": "2026-01-08T00:00:00+00:00",
            "out_of_sample_end": "2026-01-15T00:00:00+00:00",
        }
        with ResearchDataStore(self.db_path) as store:
            for strategy_id, pnl in (
                ("strategy_a", 10.0),
                ("strategy_b", 5.0),
                ("validated_strategy_selector", 12.0),
            ):
                store.save_experiment(
                    f"wf-run99-{strategy_id}-000",
                    strategy_id, "v1", "dataset-sha",
                    split, config,
                    {
                        "summary": {
                            "net_pnl_total": pnl,
                            "costed_simulated_count": 2,
                        },
                        "portfolio": {"max_drawdown_pct": 0.02},
                    },
                    status="completed",
                )
        _, response = self.get("/api/leaderboard")
        self.assertIn("meaningful only where OOS periods and execution assumptions match",
                      response["ranking_basis"])
        self.assertTrue(response["comparisons"][0]["comparable_periods_and_assumptions"])
        entries = response["comparisons"][0]["strategies"]
        self.assertEqual(entries[0]["strategy_id"], "validated_strategy_selector")
        self.assertEqual(entries[0]["best_standalone_net_pnl"], 10.0)
        self.assertEqual(entries[0]["difference_to_best_standalone"], 2.0)
        self.assertEqual(entries[0]["costed_trade_count"], 2)

        with ResearchDataStore(self.db_path) as store:
            store.save_experiment(
                "wf-runbad-strategy_a-000", "strategy_a", "v1", "dataset-sha",
                split, config, {"summary": {"net_pnl_total": 10.0}}, status="completed",
            )
            mismatched = dict(config) | {"slippage_bps": 9}
            store.save_experiment(
                "wf-runbad-strategy_b-000", "strategy_b", "v1", "dataset-sha",
                split, mismatched, {"summary": {"net_pnl_total": 11.0}},
                status="completed",
            )
        _, response = self.get("/api/leaderboard")
        unmatched = next(
            item for item in response["comparisons"]
            if item["comparison_set_id"].endswith(":wf-runbad")
        )
        self.assertFalse(unmatched["comparable_periods_and_assumptions"])
        self.assertTrue(all(
            item["best_standalone_net_pnl"] is None
            for item in unmatched["strategies"]
        ))

    def test_leaderboard_reports_compounded_equity_when_fold_chain_is_continuous(self):
        config = {
            "initial_equity": 1_000,
            "max_open_positions": 3,
            "max_portfolio_risk_pct": 0.015,
            "max_gross_leverage": 2.0,
            "max_pairwise_correlation": 0.8,
            "correlation_lookback_minutes": 60,
            "minimum_correlation_observations": 30,
            "max_daily_realized_loss_pct": 0.02,
            "max_realized_drawdown_pct": 0.10,
            "trade_risk_pct": 0.005,
            "trade_leverage": 2.0,
            "fee_rate": 0.0005,
            "spread_bps": 5,
            "slippage_bps": 2,
        }
        with ResearchDataStore(self.db_path) as store:
            for fold, start, end, initial, final in (
                (0, "2026-01-08", "2026-01-15", 1_000, 1_010),
                (1, "2026-01-15", "2026-01-22", 1_010, 990),
            ):
                store.save_experiment(
                    f"wf-compound99-strategy_a-{fold:03d}",
                    "strategy_a",
                    "v1",
                    "dataset-compound",
                    {
                        "fold": fold,
                        "out_of_sample_start": f"{start}T00:00:00+00:00",
                        "out_of_sample_end": f"{end}T00:00:00+00:00",
                    },
                    config,
                    {
                        "summary": {
                            "net_pnl_total": final - initial,
                            "costed_simulated_count": 1,
                        },
                        "portfolio": {
                            "initial_equity": initial,
                            "final_equity": final,
                            "compounded_oos_equity": final,
                            "max_drawdown_pct": 0.02,
                        },
                    },
                    status="completed",
                )
        _, response = self.get("/api/leaderboard")
        comparison = next(
            item for item in response["comparisons"]
            if item["comparison_set_id"].endswith(":wf-compound99")
        )
        entry = comparison["strategies"][0]
        self.assertEqual(entry["initial_equity"], 1_000)
        self.assertEqual(entry["final_equity"], 990)
        self.assertEqual(entry["net_pnl_total"], -10)
        self.assertIn("sequentially compounded", entry["aggregate_basis"])

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
