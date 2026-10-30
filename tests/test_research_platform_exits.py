from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from research_platform.exit_adapters import (
    ExitSimulationContext,
    FastSlowMAExitAdapter,
    MHRExitAdapter,
    ScalpPullbackExitAdapter,
    vpvr_exit_plan,
)
from research_platform.market_data import Instrument
from research_platform.store import ResearchDataStore
from research_platform.strategies import StrategySignal
from research_platform.validation import summarize_paper_results


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def signal(strategy_id="mhr_exhaustion", direction="SHORT", targets=()):
    return StrategySignal(
        strategy_id=strategy_id,
        strategy_version="fixture-v1",
        product_id="1",
        symbol="TESTUSD",
        direction=direction,
        decision_time=T0,
        entry_price=None,
        entry_reference="next_1h_open",
        stop_price=110,
        stop_logic="fixture",
        target_prices=targets,
        target_logic="fixture",
        invalidation_conditions=("fixture invalidation",),
        position_sizing_inputs={},
        provenance={"1h": "fixture"},
    )


def candle_frame():
    return pd.DataFrame({
        "time": pd.date_range(T0, periods=3, freq="1h"),
        "open": [100.0, 100.0, 100.0],
        "high": [101.0, 102.0, 103.0],
        "low": [99.0, 98.0, 97.0],
        "close": [100.0, 101.0, 102.0],
        "volume": [10.0, 10.0, 10.0],
    })


def context(strategy_signal=None, quantity=4):
    return ExitSimulationContext(
        signal=strategy_signal or signal(),
        candles=candle_frame(),
        entry_index=0,
        entry_price=100,
        stop_price=110,
        quantity=quantity,
        max_hold=2,
    )


class ExitAdapterTests(unittest.TestCase):
    def test_mhr_exit_adapter_normalizes_native_partial_result(self):
        api = SimpleNamespace(
            MHRConfig=lambda: object(),
            simulate_trailing_exit=Mock(return_value=(
                40.0, 20.0, "MAX_HOLD_AFTER_PARTIAL",
                pd.Timestamp("2026-01-01T02:00:00Z"), 95.0, 2, 2,
            )),
        )
        with patch("research_platform.exit_adapters._legacy_mhr_api", return_value=api):
            result = MHRExitAdapter().simulate(context())

        self.assertEqual(result.realized_pnl, 60)
        self.assertEqual(result.partial_pnl, 40)
        self.assertEqual(result.remaining_pnl, 20)
        self.assertEqual(result.partial_quantity, 2)
        self.assertEqual(result.exit_price, 90)
        self.assertEqual(result.model, "mhr_trailing")
        self.assertEqual(result.as_record()["net_pnl"], None)
        self.assertEqual(result.as_record()["gross_pnl"], 60)
        api.simulate_trailing_exit.assert_called_once()

    def test_mhr_exit_adapter_rejects_non_short_and_fractional_quantity(self):
        api = SimpleNamespace(MHRConfig=lambda: object(), simulate_trailing_exit=Mock())
        with patch("research_platform.exit_adapters._legacy_mhr_api", return_value=api):
            adapter = MHRExitAdapter()
            with self.assertRaisesRegex(ValueError, "SHORT"):
                adapter.simulate(context(signal(direction="LONG")))
            with self.assertRaisesRegex(ValueError, "integer"):
                adapter.simulate(context(quantity=1.5))
        api.simulate_trailing_exit.assert_not_called()

    def test_scalp_exit_adapter_preserves_legacy_result(self):
        api = SimpleNamespace(
            ScalpConfig=lambda: object(),
            simulate_scalp_exit=Mock(return_value=(
                12.5, "MAX_HOLD_PARTIAL",
                pd.Timestamp("2026-01-01T02:00:00Z"), 95.0, 2,
            )),
        )
        with patch("research_platform.exit_adapters._legacy_mhr_api", return_value=api):
            result = ScalpPullbackExitAdapter().simulate(
                context(signal("scalp_pullback", "SHORT"), quantity=2.5)
            )
        self.assertEqual(result.realized_pnl, 12.5)
        self.assertEqual(result.exit_reason, "MAX_HOLD_PARTIAL")
        self.assertEqual(result.exit_price, 95)
        self.assertEqual(result.quantity, 2.5)
        self.assertEqual(result.model, "scalp_partial_trailing")

    def test_fast_slow_exit_adapter_preserves_legacy_result(self):
        legacy = SimpleNamespace(
            FAST_SLOW_DEFAULT_CONFIG=object(),
            simulate_fast_slow_exit=Mock(return_value=(
                25.0, "TP_HIT", pd.Timestamp("2026-01-01T01:00:00Z"), 105.0, 1,
            )),
        )
        with patch("research_platform.exit_adapters.importlib.import_module", return_value=legacy):
            result = FastSlowMAExitAdapter().simulate(
                context(signal("fast_slow_ma", "LONG", (105,)))
            )
        self.assertEqual(result.realized_pnl, 25)
        self.assertEqual(result.exit_reason, "TP_HIT")
        self.assertEqual(result.exit_price, 105)
        self.assertEqual(result.bars_held, 1)

    def test_vpvr_exit_plan_normalizes_native_target_ladder_and_runner(self):
        plan = vpvr_exit_plan(signal(
            "poc_reclaim", "LONG", targets=(105, 110, 115, 120)
        ))
        self.assertEqual(plan.targets, (105, 110, 115))
        self.assertEqual(plan.target_fractions, (0.30, 0.45, 0.25))
        self.assertEqual(plan.runner_fraction, 0)
        self.assertEqual(plan.execution_owner, "vpvr.paper.PaperAccount")

    def test_exit_outcome_persists_without_mislabeling_gross_as_net(self):
        api = SimpleNamespace(
            MHRConfig=lambda: object(),
            simulate_trailing_exit=Mock(return_value=(
                0.0, 20.0, "MAX_HOLD_EXIT",
                pd.Timestamp("2026-01-01T02:00:00Z"), 110.0, 2, 0,
            )),
        )
        with patch("research_platform.exit_adapters._legacy_mhr_api", return_value=api):
            result = MHRExitAdapter().simulate(context())
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "research.sqlite3"
            with ResearchDataStore(db_path) as store:
                store.reconcile_instruments([
                    Instrument("1", "TESTUSD", "perpetual_futures", "live")
                ], T0)
                store.save_paper_simulation(result.as_record())
                saved = store.list_paper_simulations()["items"][0]["result"]
        self.assertEqual(saved["gross_pnl"], 20)
        self.assertIsNone(saved["net_pnl"])
        self.assertIn("before fees", saved["pnl_basis"])
        summary = summarize_paper_results([saved])
        self.assertEqual(summary["uncosted_exit_count"], 1)
        self.assertEqual(summary["costed_simulated_count"], 0)
        self.assertIsNone(summary["net_pnl_total"])


if __name__ == "__main__":
    unittest.main()
