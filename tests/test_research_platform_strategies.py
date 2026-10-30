from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from research_platform.market_data import Instrument
from research_platform.strategies import (
    FastSlowMAAdapter,
    MHRExhaustionAdapter,
    ScalpPullbackAdapter,
    StrategyContext,
    VPVRStrategyAdapter,
)
from vpvr.strategies import StrategySignal as VPVRSignal


UTC = timezone.utc


def candles(start, count, timeframe):
    times = pd.date_range(start, periods=count, freq=timeframe, tz="UTC")
    return pd.DataFrame({
        "time": times,
        "open": [100.0] * count,
        "high": [102.0] * count,
        "low": [98.0] * count,
        "close": [101.0] * count,
        "volume": [20.0] * count,
    })


class StrategyAdapterTests(unittest.TestCase):
    def test_context_excludes_candles_not_closed_by_decision_time(self):
        frame = candles("2026-01-01", 3, "1h")
        context = StrategyContext(
            Instrument("1", "TESTUSD", "perpetual_futures", "live"),
            datetime(2026, 1, 1, 2, 30, tzinfo=UTC),
            {"1h": frame},
        )
        closed = context.completed_frame("1h")
        self.assertEqual(len(closed), 2)
        self.assertEqual(closed.time.iloc[-1], pd.Timestamp("2026-01-01T01:00:00Z"))

    def test_context_accepts_empty_canonical_frame(self):
        context = StrategyContext(
            Instrument("1", "TESTUSD", "perpetual_futures", "live"),
            datetime(2026, 1, 1, tzinfo=UTC),
            {"1h": pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])},
        )
        self.assertTrue(context.completed_frame("1h").empty)

    def test_mhr_adapter_wraps_legacy_entry_and_defers_quantity(self):
        frame = candles("2026-01-01", 30, "1h")
        api = SimpleNamespace(
            MHRConfig=lambda: SimpleNamespace(
                leverage=10, sl_capital_pct=0.05, max_capital_usage_pct=0.5,
            ),
            entry_signal=Mock(return_value={
                "entry_path": "MHR_EXHAUSTION", "pump_pct": 12.0,
            }),
        )
        with patch("research_platform.strategies._mhr_api", return_value=api):
            adapter = MHRExhaustionAdapter()
            context = StrategyContext(
                Instrument("1", "TESTUSD", "perpetual_futures", "live"),
                datetime(2026, 1, 2, 7, tzinfo=UTC),
                {"1h": frame}, provenance={"1h": "fixture"},
            )
            signal = adapter.evaluate(context)[0]
        api.entry_signal.assert_called_once()
        self.assertEqual(signal.strategy_id, "mhr_exhaustion")
        self.assertEqual(signal.direction, "SHORT")
        self.assertIsNone(signal.entry_price)
        self.assertEqual(signal.entry_reference, "next_1h_open")
        self.assertEqual(signal.position_sizing_inputs["quantity"], "deferred to independent risk sizing")
        self.assertEqual(signal.provenance["1h"], "fixture")

    def test_scalp_adapter_wraps_legacy_detector_with_separate_sizing(self):
        frame_5m = candles("2026-01-01", 8, "5min")
        frame_1h = candles("2026-01-01", 3, "1h")
        config = SimpleNamespace(
            ema_5m_length=2, max_pullback_candles=2, ema_1h_length=2,
            tp1_rr=1.5, trail_lock_rr=1, leverage=10,
            sl_capital_pct=0.02, max_capital_usage_pct=0.5,
        )
        api = SimpleNamespace(
            ema=lambda series, length: pd.Series([99.0] * len(series), index=series.index),
            atr=lambda frame, length: pd.Series([1.0] * len(frame), index=frame.index),
            scalp_entry=Mock(return_value={
                "direction": "SHORT", "sl_price": 103, "pullback_candles": 3,
            }),
        )
        with patch("research_platform.strategies._mhr_api", return_value=api):
            adapter = ScalpPullbackAdapter(config)
            context = StrategyContext(
                Instrument("1", "TESTUSD", "perpetual_futures", "live"),
                datetime(2026, 1, 1, 8, tzinfo=UTC),
                {"5m": frame_5m, "1h": frame_1h},
            )
            signal = adapter.evaluate(context)[0]
        self.assertEqual(signal.direction, "SHORT")
        self.assertEqual(signal.stop_price, 103)
        self.assertIsNone(signal.entry_price)
        self.assertEqual(signal.position_sizing_inputs["quantity"], "deferred to independent risk sizing")

    def test_fast_slow_adapter_normalizes_legacy_signal_output(self):
        frame = candles("2026-01-01", 3, "1h")
        config = object()
        strategy = SimpleNamespace(
            FAST_SLOW_DEFAULT_CONFIG=config,
            get_long_signal=Mock(return_value={
                "signal": "LONG", "entry": 101.0, "stop_loss": 99.0,
                "take_profit": 105.0, "quantity": 3, "leverage": 10,
                "entry_candle_time": pd.Timestamp("2026-01-01T01:00:00Z"),
                "strategy": "fast_slow_ma_long",
            }),
            get_short_signal=Mock(return_value=None),
        )
        with patch("research_platform.strategies.importlib.import_module", return_value=strategy):
            adapter = FastSlowMAAdapter(timeframe="1h")
            context = StrategyContext(
                Instrument("1", "TESTUSD", "perpetual_futures", "live"),
                datetime(2026, 1, 1, 3, tzinfo=UTC),
                {"1h": frame}, provenance={"1h": "fixture"},
            )
            signal = adapter.evaluate(context)[0]
        self.assertEqual(signal.direction, "LONG")
        self.assertEqual(signal.stop_price, 99)
        self.assertEqual(signal.target_prices, (105,))
        self.assertEqual(signal.position_sizing_inputs["legacy_quantity_reference"], 3)

    def test_vpvr_adapter_normalizes_signal_and_keeps_provenance(self):
        frames = {
            "1m": candles("2026-01-01", 10, "1min"),
            "3m": candles("2026-01-01", 10, "3min"),
            "15m": candles("2026-01-01", 10, "15min"),
        }
        source_signal = VPVRSignal(
            "TESTUSD", "poc_reclaim", "LONG", 101, 99, (105, 110), 2,
            "2026-01-01T02:00:00+00:00", ("reclaim",), ("lose POC",), 100,
        )
        with patch("vpvr.strategies.poc_reclaim", return_value=source_signal):
            adapter = VPVRStrategyAdapter("poc_reclaim")
            context = StrategyContext(
                Instrument("1", "TESTUSD", "perpetual_futures", "live"),
                datetime(2026, 1, 1, 3, tzinfo=UTC),
                frames,
                regime=object(),
                profile=object(),
                provenance={key: "fixture" for key in frames},
            )
            signal = adapter.evaluate(context)[0]
        self.assertEqual(signal.target_prices, (105, 110))
        self.assertEqual(signal.invalidation_conditions, ("lose POC",))
        self.assertEqual(signal.provenance["3m"], "fixture")
        self.assertEqual(signal.decision_time, datetime(2026, 1, 1, 2, 1, tzinfo=UTC))

    def test_vpvr_adapter_rejects_unknown_strategy(self):
        with self.assertRaisesRegex(ValueError, "unknown VPVR strategy"):
            VPVRStrategyAdapter("made_up")


if __name__ == "__main__":
    unittest.main()
