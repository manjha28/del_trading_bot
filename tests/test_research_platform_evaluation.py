from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

import pandas as pd

from research_platform.evaluation import evaluate_walk_forward_variants
from research_platform.market_data import Instrument
from research_platform.validation import chronological_split


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def frame(count, freq):
    times = pd.date_range(T0, periods=count, freq=freq)
    return pd.DataFrame({
        "time": times,
        "open": [100.0] * count,
        "high": [101.0] * count,
        "low": [99.0] * count,
        "close": [100.0] * count,
        "volume": [10.0] * count,
    })


def verified_instrument():
    return Instrument.from_delta({
        "id": "1", "symbol": "TESTUSD",
        "contract_type": "perpetual_futures", "state": "live",
        "contract_value": "1", "tick_size": "0.5",
        "contract_unit_currency": "USD", "position_size_limit": "1000",
        "contract_spec_verification": {
            "spec_verified": True,
            "lot_size": 1,
            "minimum_order_contracts": 1,
            "verification_policy": "fixture",
            "verification_errors": [],
        },
    })


class WalkForwardEvaluationTests(unittest.TestCase):
    def test_oos_evaluation_keeps_unobserved_products_in_coverage(self):
        five_minute = frame(30, "5min")
        five_minute.loc[22, ["open", "high", "low", "close"]] = [100, 101, 98, 100]
        minute = frame(170, "1min")
        minute.loc[116:120, ["open", "high", "low", "close"]] = [
            [100, 101, 99, 100],
            [100, 101, 99, 100],
            [100, 101, 99, 100],
            [100, 105, 99, 104],
            [104, 105, 103, 104],
        ]
        universe = [
            verified_instrument(),
            Instrument("2", "NOHISTORYUSD", "perpetual_futures", "live"),
        ]
        frame_times = five_minute[["time"]]
        fold = chronological_split(
            frame_times,
            train_end=T0 + timedelta(minutes=45),
            validation_end=T0 + timedelta(minutes=90),
            out_of_sample_end=T0 + timedelta(minutes=145),
        )
        report = evaluate_walk_forward_variants(
            universe,
            {"1": {"5m": five_minute, "1m": minute}, "2": {}},
            [fold],
            initial_equity=10_000,
        )
        sweep = report["variants"]["liquidity_sweep_reclaim"]["folds"][0]
        self.assertEqual(sweep["per_symbol"]["2"]["coverage_state"], "insufficient_data")
        self.assertEqual(report["variants"]["liquidity_sweep_reclaim"]["universe_count"], 2)
        self.assertGreaterEqual(
            sweep["per_symbol"]["1"]["candidate_signal_count"], 1
        )
        self.assertEqual(sweep["portfolio"]["accepted_count"], 1)
        self.assertEqual(len(sweep["_outcomes"]), sweep["candidate_signal_count"])
        self.assertIn("simulated", {item["status"] for item in sweep["_outcomes"]})
        self.assertEqual(sweep["per_symbol"]["1"]["coverage_state"], "signal")
        self.assertEqual(
            report["variants"]["liquidity_sweep_reclaim"]["symbols_with_signal"], 1
        )
        self.assertEqual(report["status"], "research_only")
        self.assertIn("correlation", " ".join(report["limitations"]))

    def test_runner_rejects_empty_folds_and_missing_universe(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            evaluate_walk_forward_variants([], {}, [])


if __name__ == "__main__":
    unittest.main()
