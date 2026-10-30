from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timedelta, timezone
import io
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
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

    def test_evaluate_command_persists_no_signal_and_unavailable_states(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "research.sqlite3"
            with ResearchDataStore(db_path) as store:
                store.reconcile_instruments([
                    Instrument("9", "QUIETUSD", "perpetual_futures", "live")
                ])
                store.save_candles([Candle(
                    "9", "QUIETUSD", "1m",
                    datetime.now(UTC).replace(second=0, microsecond=0),
                    100, 101, 99, 100, 10, "fixture", True,
                )])
            output = io.StringIO()
            with redirect_stdout(output):
                result = main([
                    "evaluate", "--db", str(db_path),
                    "--variants", "liquidity_sweep_reclaim", "liquidation_cascade",
                ])
            self.assertEqual(result, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["status_counts"]["insufficient_data"], 1)
            self.assertEqual(report["status_counts"]["unavailable"], 1)
            with ResearchDataStore(db_path, read_only=True) as store:
                events = store.list_variant_evaluations(limit=10)
            self.assertEqual(events["total"], 2)
            self.assertEqual(
                {item["status"] for item in events["items"]},
                {"insufficient_data", "unavailable"},
            )

    def test_backtest_persists_experiment_and_trade_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "research.sqlite3"
            instrument = Instrument.from_delta({
                "id": "1",
                "symbol": "TESTUSD",
                "contract_type": "perpetual_futures",
                "state": "live",
                "contract_value": "1",
                "tick_size": "0.5",
                "contract_unit_currency": "USD",
                "position_size_limit": "1000",
                "contract_spec_verification": {
                    "spec_verified": True,
                    "lot_size": 1,
                    "minimum_order_contracts": 1,
                    "verification_policy": "fixture",
                    "verification_errors": [],
                },
            })
            start = datetime(2026, 1, 1, tzinfo=UTC)
            candles = [
                Candle(
                    "1", "TESTUSD", "1m", start + timedelta(minutes=index),
                    100, 101, 99, 100, 10, "fixture", True,
                )
                for index in range(4321)
            ]
            with ResearchDataStore(db_path) as store:
                store.reconcile_instruments([instrument])
                store.save_candles(candles)

            decision_time = start + timedelta(days=2)
            outcome = {
                "strategy_id": "fixture_variant",
                "product_id": "1",
                "symbol": "TESTUSD",
                "direction": "SHORT",
                "status": "simulated",
                "reason": "fixture",
                "decision_time": decision_time.isoformat(),
                "entry_time": decision_time.isoformat(),
                "exit_time": (decision_time + timedelta(minutes=5)).isoformat(),
                "net_pnl": 5.0,
            }
            walk_forward_report = {
                "status": "research_only",
                "limitations": ["fixture limitation"],
                "variants": {
                    "fixture_variant": {
                        "universe_count": 1,
                        "symbols_with_signal": 1,
                        "folds": [{
                            "fold": 0,
                            "train_end": (start + timedelta(days=1)).isoformat(),
                            "validation_end": (start + timedelta(days=2)).isoformat(),
                            "out_of_sample_start": (start + timedelta(days=2)).isoformat(),
                            "out_of_sample_end": (start + timedelta(days=3)).isoformat(),
                            "portfolio": {"net_pnl": 5.0},
                            "metrics": {"net_pnl": 5.0},
                            "selection_bias_diagnostic": {
                                "approximate_deflated_sharpe_probability": None,
                                "trial_count": 1,
                            },
                            "candidate_signal_count": 1,
                            "per_symbol": {},
                            "_outcomes": [outcome],
                        }],
                    }
                },
                "selector": {
                    "policy": {
                        "minimum_validation_trades": 3,
                        "minimum_net_expectancy": 0.0,
                        "overlap_window_seconds": 300.0,
                    },
                    "folds": [{
                        "fold": 0,
                        "train_end": (start + timedelta(days=1)).isoformat(),
                        "validation_end": (start + timedelta(days=2)).isoformat(),
                        "out_of_sample_start": (start + timedelta(days=2)).isoformat(),
                        "out_of_sample_end": (start + timedelta(days=3)).isoformat(),
                        "selection": {"selected_count": 1, "no_trade": False},
                        "portfolio": {"net_pnl": 5.0},
                        "metrics": {"net_pnl": 5.0},
                        "_outcomes": [outcome],
                    }],
                },
            }
            output = io.StringIO()
            with patch(
                "research_platform.cli.evaluate_walk_forward_variants",
                return_value=walk_forward_report,
            ), redirect_stdout(output):
                result = main([
                    "backtest", "--db", str(db_path),
                    "--train-days", "1", "--validation-days", "1",
                    "--test-days", "1", "--step-days", "1",
                ])

            self.assertEqual(result, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["fold_count"], 1)
            self.assertNotIn("_outcomes", report["variants"]["fixture_variant"]["folds"][0])
            with ResearchDataStore(db_path, read_only=True) as store:
                experiments = store.list_experiments()
                simulations = store.list_paper_simulations()
            self.assertEqual(experiments["total"], 2)
            self.assertTrue(all(item["status"] == "completed" for item in experiments["items"]))
            self.assertEqual(simulations["total"], 2)
            self.assertTrue(all(item["result"]["net_pnl"] == 5.0 for item in simulations["items"]))


if __name__ == "__main__":
    unittest.main()
