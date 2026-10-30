from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from research_platform.market_data import Candle, Instrument
from research_platform.portfolio import PortfolioPaperSimulator, PortfolioSimulationConfig
from research_platform.simulation import PaperSimulationConfig
from research_platform.strategies import StrategySignal


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def product(product_id, symbol):
    return Instrument.from_delta({
        "id": product_id,
        "symbol": symbol,
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


def signal(product_id, symbol, decision=T0, strategy="fixture"):
    return StrategySignal(
        strategy, "1", product_id, symbol, "LONG", decision,
        None, "next_1m_open", 95, "fixture", (120,), "fixture",
        ("stop",), {}, {"1m": "fixture"},
    )


def bars(product_id, symbol, target=120):
    return [
        Candle(
            product_id, symbol, "1m", T0 + timedelta(minutes=i),
            100, 101, 99, 100, 10, "fixture", True,
        )
        for i in range(4)
    ] + [
        Candle(
            product_id, symbol, "1m", T0 + timedelta(minutes=4),
            100, target + 1, 99, target, 10, "fixture", True,
        )
    ]


def correlation_bars(product_id, symbol, return_pattern, exit_index=16):
    closes = [100.0]
    for value in return_pattern:
        closes.append(closes[-1] * (1 + value / 10_000))
    closes.extend([closes[-1]] * (exit_index + 1 - len(closes)))
    result = []
    for index, close in enumerate(closes):
        high = max(101.0, close + 0.1)
        if index == exit_index:
            high = 121
        result.append(Candle(
            product_id, symbol, "1m", T0 + timedelta(minutes=index),
            100, high, min(99.0, close - 0.1), close, 10, "fixture", True,
        ))
    return result


class PortfolioSimulationTests(unittest.TestCase):
    def test_multiple_independent_products_fit_configured_open_slots(self):
        config = PortfolioSimulationConfig(
            max_open_positions=2,
            correlation_lookback_minutes=10,
            minimum_correlation_observations=5,
        )
        simulator = PortfolioPaperSimulator(config)
        instruments = {
            "1": product("1", "AAAUSD"),
            "2": product("2", "BBBUSD"),
        }
        result = simulator.simulate(
            [
                signal("1", "AAAUSD", T0 + timedelta(minutes=10)),
                signal("2", "BBBUSD", T0 + timedelta(minutes=10)),
            ],
            instruments,
            {
                "1": correlation_bars(
                    "1", "AAAUSD", [1, -1] * 5
                ),
                "2": correlation_bars(
                    "2", "BBBUSD", [-1, 1] * 5
                ),
            },
            10_000,
        )
        self.assertEqual(result.status, "simulated")
        self.assertEqual(result.accepted_count, 2)
        self.assertEqual(result.max_concurrent_positions, 2)
        self.assertAlmostEqual(result.final_equity, 10_000 + result.net_pnl)
        self.assertEqual(result.rejected_count, 0)

    def test_high_same_direction_return_correlation_rejects_second_position(self):
        config = PortfolioSimulationConfig(
            max_open_positions=2,
            correlation_lookback_minutes=10,
            minimum_correlation_observations=5,
        )
        result = PortfolioPaperSimulator(config).simulate(
            [
                signal("1", "AAAUSD", T0 + timedelta(minutes=10)),
                signal("2", "BBBUSD", T0 + timedelta(minutes=11)),
            ],
            {
                "1": product("1", "AAAUSD"),
                "2": product("2", "BBBUSD"),
            },
            {
                "1": correlation_bars("1", "AAAUSD", [1, -1] * 5),
                "2": correlation_bars("2", "BBBUSD", [1, -1] * 5),
            },
            10_000,
        )
        self.assertEqual(result.accepted_count, 1)
        rejected = next(item for item in result.outcomes if item.status == "rejected")
        self.assertIn("correlated exposure", rejected.reason)

    def test_open_slot_and_same_product_overlap_are_rejected(self):
        simulator = PortfolioPaperSimulator(
            PortfolioSimulationConfig(max_open_positions=1)
        )
        signals = [
            signal("1", "AAAUSD", T0, "first"),
            signal("1", "AAAUSD", T0 + timedelta(minutes=1), "second"),
        ]
        result = simulator.simulate(
            signals,
            {"1": product("1", "AAAUSD")},
            {"1": bars("1", "AAAUSD")},
            10_000,
        )
        self.assertEqual(result.accepted_count, 1)
        self.assertEqual(result.rejected_count, 1)
        rejected = next(item for item in result.outcomes if item.status == "rejected")
        self.assertIn("already open", rejected.reason)

    def test_gross_exposure_constraint_fails_closed(self):
        simulator = PortfolioPaperSimulator(PortfolioSimulationConfig(
            max_open_positions=2,
            max_gross_leverage=0.01,
            trade=PaperSimulationConfig(leverage=1),
        ))
        result = simulator.simulate(
            [signal("1", "AAAUSD")],
            {"1": product("1", "AAAUSD")},
            {"1": bars("1", "AAAUSD")},
            10_000,
        )
        self.assertEqual(result.accepted_count, 0)
        self.assertEqual(result.outcomes[0].reason, "maximum gross portfolio exposure exceeded")

    def test_duplicate_signal_identity_is_rejected(self):
        simulator = PortfolioPaperSimulator()
        candidate = signal("1", "AAAUSD")
        result = simulator.simulate(
            [candidate, candidate],
            {"1": product("1", "AAAUSD")},
            {"1": bars("1", "AAAUSD")},
            10_000,
        )
        self.assertEqual(result.accepted_count, 1)
        self.assertEqual(result.rejected_count, 1)
        self.assertIn("duplicate", next(
            item.reason for item in result.outcomes if item.status == "rejected"
        ))

    def test_result_is_no_trades_for_empty_universe(self):
        result = PortfolioPaperSimulator().simulate([], {}, {}, 1000)
        self.assertEqual(result.status, "no_trades")
        self.assertEqual(result.final_equity, 1000)
        self.assertEqual(result.realized_max_drawdown_pct, 0)


if __name__ == "__main__":
    unittest.main()
