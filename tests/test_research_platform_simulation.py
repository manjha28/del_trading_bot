from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from research_platform.market_data import Candle, Instrument
from research_platform.simulation import PaperSimulationConfig, PaperSimulator
from research_platform.strategies import StrategySignal


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def instrument(verified=True):
    verification = {
        "spec_verified": verified,
        "lot_size": 1,
        "minimum_order_contracts": 1,
        "verification_policy": "fixture",
        "verification_errors": [] if verified else ["unverified"],
    }
    return Instrument.from_delta({
        "id": 1, "symbol": "TESTUSD", "contract_type": "perpetual_futures",
        "state": "live", "contract_value": "1", "tick_size": "0.5",
        "contract_unit_currency": "USD", "position_size_limit": "1000",
        "contract_spec_verification": verification,
    })


def signal(
    direction="LONG",
    decision=T0,
    entry=None,
    stop=95,
    target=110,
    reference="next_1m_open",
):
    return StrategySignal(
        strategy_id="fixture", strategy_version="1",
        product_id="1", symbol="TESTUSD", direction=direction,
        decision_time=decision, entry_price=entry, entry_reference=reference,
        stop_price=stop, stop_logic="fixture", target_prices=(target,),
        target_logic="fixture", invalidation_conditions=("stop",),
        position_sizing_inputs={}, provenance={"1m": "fixture"},
    )


def candle(start, open_, high, low, close):
    return Candle(
        "1", "TESTUSD", "1m", start, open_, high, low, close, 10,
        "fixture", True,
    )


class PaperSimulatorTests(unittest.TestCase):
    def test_requires_verified_contract_specifications(self):
        result = PaperSimulator().simulate(
            signal(), instrument(False),
            [candle(T0, 100, 101, 99, 100)], 1000,
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn("not verified", result.reason)

    def test_no_lookahead_and_entry_occurs_at_or_after_signal_time(self):
        decision = T0 + timedelta(minutes=1)
        bars = [
            candle(T0, 100, 110, 90, 100),
            candle(decision, 100, 101, 99, 100),
            candle(decision + timedelta(minutes=1), 100, 111, 99, 110),
        ]
        result = PaperSimulator().simulate(
            signal(decision=decision), instrument(), bars, 10_000,
        )
        self.assertEqual(result.status, "simulated")
        self.assertEqual(result.entry_time, decision.isoformat())
        self.assertEqual(result.exit_reason, "TARGET")
        self.assertGreaterEqual(result.quantity, 1)
        self.assertGreater(result.fees, 0)
        self.assertEqual(result.equity_after, 10_000 + result.net_pnl)

    def test_configured_latency_skips_candles_before_the_delay_expires(self):
        decision = T0 + timedelta(minutes=1)
        bars = [
            candle(decision, 100, 105, 99, 104),
            candle(decision + timedelta(minutes=1), 100, 111, 99, 110),
        ]
        simulator = PaperSimulator(PaperSimulationConfig(latency_seconds=60))
        result = simulator.simulate(
            signal(decision=decision), instrument(), bars, 10_000,
        )
        self.assertEqual(result.status, "simulated")
        self.assertEqual(result.entry_time, (decision + timedelta(minutes=1)).isoformat())
        self.assertTrue(any("delayed by 60s" in item for item in result.assumptions))

    def test_execution_latency_must_be_non_negative(self):
        with self.assertRaisesRegex(ValueError, "latency_seconds"):
            PaperSimulationConfig(latency_seconds=-1)

    def test_fold_boundary_excludes_future_target_and_marks_open_trade(self):
        boundary = T0 + timedelta(minutes=2)
        bars = [
            candle(T0, 100, 101, 99, 100),
            candle(T0 + timedelta(minutes=1), 100, 104, 99, 103),
            candle(T0 + timedelta(minutes=2), 103, 112, 102, 110),
        ]
        result = PaperSimulator().simulate(
            signal(target=110), instrument(), bars, 10_000,
            max_exit_time=boundary,
        )
        self.assertEqual(result.status, "simulated")
        self.assertEqual(result.exit_reason, "FOLD_END")
        self.assertEqual(result.exit_time, boundary.isoformat())
        self.assertLess(result.exit_price, 110)
        self.assertTrue(any("fold boundary" in item for item in result.assumptions))

    def test_fold_boundary_excludes_candle_that_closes_after_boundary(self):
        boundary = T0 + timedelta(seconds=90)
        bars = [
            candle(T0, 100, 101, 99, 100),
            candle(T0 + timedelta(minutes=1), 100, 112, 99, 110),
        ]
        result = PaperSimulator().simulate(
            signal(target=110), instrument(), bars, 10_000,
            max_exit_time=boundary,
        )
        self.assertEqual(result.status, "simulated")
        self.assertEqual(result.exit_reason, "FOLD_END")
        self.assertEqual(result.exit_time, (T0 + timedelta(minutes=1)).isoformat())
        self.assertNotEqual(result.exit_price, 110)

    def test_fold_boundary_must_be_timezone_aware_and_after_decision(self):
        simulator = PaperSimulator()
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            simulator.simulate(
                signal(), instrument(), [candle(T0, 100, 101, 99, 100)], 1000,
                max_exit_time=datetime(2026, 1, 1, 0, 2),
            )
        rejected = simulator.simulate(
            signal(), instrument(), [candle(T0, 100, 101, 99, 100)], 1000,
            max_exit_time=T0,
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertIn("boundary", rejected.reason)

    def test_stop_wins_ambiguous_same_bar_and_gap_stops_fill_worse(self):
        simulator = PaperSimulator(PaperSimulationConfig(spread_bps=0, slippage_bps=0))
        ambiguous = simulator.simulate(
            signal(), instrument(), [candle(T0, 100, 112, 90, 105)], 10_000,
        )
        self.assertEqual(ambiguous.exit_reason, "STOP_FIRST")
        self.assertEqual(ambiguous.exit_price, 95)

        gap = simulator.simulate(
            signal(target=120), instrument(),
            [
                candle(T0, 100, 101, 99, 100),
                candle(T0 + timedelta(minutes=1), 90, 100, 88, 92),
            ], 10_000,
        )
        self.assertEqual(gap.exit_reason, "STOP_FIRST")
        self.assertEqual(gap.exit_price, 90)

    def test_trigger_entry_rejects_ambiguous_entry_and_stop_touch(self):
        result = PaperSimulator().simulate(
            signal(entry=105, reference="legacy trigger entry"), instrument(),
            [candle(T0, 100, 110, 90, 105)], 10_000,
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn("ordering ambiguous", result.reason)

    def test_trigger_entry_uses_only_future_bars_and_accounts_for_gaps(self):
        simulator = PaperSimulator(PaperSimulationConfig(spread_bps=0, slippage_bps=0))
        bars = [
            candle(T0, 100, 104, 99, 101),
            candle(T0 + timedelta(minutes=1), 108, 112, 106, 110),
        ]
        result = simulator.simulate(
            signal(entry=105, reference="legacy trigger entry"), instrument(), bars, 10_000,
        )
        self.assertEqual(result.status, "simulated")
        self.assertEqual(result.entry_time, (T0 + timedelta(minutes=1)).isoformat())
        self.assertEqual(result.entry_price, 108)

    def test_invalid_signal_identity_or_incomplete_candles_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "identity"):
            PaperSimulator().simulate(
                signal(), instrument().__class__(
                    "2", "OTHER", "perpetual_futures", "live", {}, instrument().contract_specs
                ), [candle(T0, 100, 101, 99, 100)], 1000,
            )
        incomplete = Candle(
            "1", "TESTUSD", "1m", T0, 100, 101, 99, 100, 10, "fixture", False,
        )
        rejected = PaperSimulator().simulate(signal(), instrument(), [incomplete], 1000)
        self.assertEqual(rejected.status, "rejected")
        self.assertIn("incomplete", rejected.reason)


if __name__ == "__main__":
    unittest.main()
