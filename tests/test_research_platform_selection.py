from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

import pandas as pd

from research_platform.selection import (
    StrategySelectionPolicy,
    ValidatedStrategySelector,
    ValidationEvidence,
    build_validation_evidence,
    classify_market_regime,
    signal_key,
)
from research_platform.simulation import PaperTradeResult
from research_platform.strategies import StrategySignal


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def signal(strategy_id="variant-a", product_id="1", minute=0):
    return StrategySignal(
        strategy_id, "v1", product_id, f"TEST{product_id}USD", "SHORT",
        T0 + timedelta(minutes=minute), None, "next_1m_open",
        110, "fixture", (80,), "fixture", ("stop",), {},
        {"1m": "fixture"},
    )


class ValidatedSelectionTests(unittest.TestCase):
    def test_selector_chooses_only_positive_evidence_and_allows_no_trade(self):
        candidate = signal()
        unknown_regime_candidate = signal("variant-b", minute=1)
        evidence = {
            ("variant-a", "downtrend"): ValidationEvidence(
                "variant-a", "downtrend", 5, 2.0, 10.0, 0.6,
                (T0 - timedelta(days=1)).isoformat(),
            ),
            ("variant-b", "downtrend"): ValidationEvidence(
                "variant-b", "downtrend", 5, -1.0, -5.0, 0.2,
                (T0 - timedelta(days=1)).isoformat(),
            ),
        }
        selector = ValidatedStrategySelector(
            StrategySelectionPolicy(minimum_validation_trades=3)
        )
        selected = selector.select(
            [candidate, unknown_regime_candidate],
            evidence,
            {
                signal_key(candidate): "downtrend",
                signal_key(unknown_regime_candidate): "downtrend",
            },
        )
        self.assertEqual(selected.selected, (candidate,))
        self.assertEqual(selected.decisions[0].reason, "validated evidence")
        self.assertEqual(
            selected.decisions[1].reason, "validation expectancy not positive"
        )

        no_trade = selector.select(
            [candidate], {}, {signal_key(candidate): "downtrend"}
        )
        self.assertTrue(no_trade.as_record()["no_trade"])
        self.assertEqual(no_trade.decisions[0].reason, "no validation evidence for regime")

    def test_selector_enforces_validation_sample_and_same_product_overlap(self):
        first = signal(minute=0)
        second = signal("variant-b", minute=0)
        evidence = {
            (item.strategy_id, "range_bound"): ValidationEvidence(
                item.strategy_id, "range_bound", 2,
                1.0 if item.strategy_id == "variant-a" else 2.0,
                2.0 if item.strategy_id == "variant-a" else 4.0, 1.0,
                (T0 - timedelta(days=1)).isoformat(),
            )
            for item in (first, second)
        }
        selector = ValidatedStrategySelector(
            StrategySelectionPolicy(minimum_validation_trades=3)
        )
        insufficient = selector.select(
            [first], evidence, {signal_key(first): "range_bound"}
        )
        self.assertEqual(
            insufficient.decisions[0].reason, "insufficient validation trades"
        )

        enough = {
            key: ValidationEvidence(
                value.strategy_id, value.regime, 3,
                value.net_expectancy, value.net_expectancy * 3, 1.0,
                value.validation_end,
            )
            for key, value in evidence.items()
        }
        overlapped = selector.select(
            [first, second],
            enough,
            {
                signal_key(first): "range_bound",
                signal_key(second): "range_bound",
            },
        )
        self.assertEqual(len(overlapped.selected), 1)
        self.assertEqual(overlapped.selected[0].strategy_id, "variant-b")
        self.assertEqual(
            overlapped.decisions[1].reason, "overlapping same-product signal"
        )

    def test_future_validation_evidence_is_never_used_for_selection(self):
        candidate = signal()
        evidence = {
            ("variant-a", "downtrend"): ValidationEvidence(
                "variant-a", "downtrend", 50, 100.0, 5000.0, 1.0,
                (T0 + timedelta(seconds=1)).isoformat(),
            )
        }
        result = ValidatedStrategySelector().select(
            [candidate], evidence, {signal_key(candidate): "downtrend"}
        )
        self.assertEqual(result.selected, ())
        self.assertEqual(
            result.decisions[0].reason, "validation evidence is not prior to signal"
        )

    def test_validation_evidence_excludes_rejections_and_unknown_regimes(self):
        candidate = signal()
        identity = signal_key(candidate)
        outcomes = [
            PaperTradeResult(
                strategy_id=candidate.strategy_id,
                product_id=candidate.product_id,
                symbol=candidate.symbol,
                direction=candidate.direction,
                status="simulated",
                reason="closed",
                decision_time=candidate.decision_time.isoformat(),
                net_pnl=2.0,
            ),
            PaperTradeResult(
                strategy_id=candidate.strategy_id,
                product_id=candidate.product_id,
                symbol=candidate.symbol,
                direction=candidate.direction,
                status="rejected",
                reason="no fill",
                decision_time=(candidate.decision_time + timedelta(minutes=1)).isoformat(),
            ),
        ]
        evidence = build_validation_evidence(
            outcomes, {identity: "range_bound"}, T0 + timedelta(hours=1)
        )
        self.assertEqual(evidence[("variant-a", "range_bound")].simulated_trades, 1)
        self.assertEqual(evidence[("variant-a", "range_bound")].net_expectancy, 2.0)
        self.assertNotIn(("variant-a", None), evidence)

    def test_regime_uses_only_completed_candles_and_symbol_local_history(self):
        times = pd.date_range(T0, periods=80, freq="1h")
        close = [100.0 + index * 0.1 for index in range(80)]
        frame = pd.DataFrame({
            "time": times,
            "high": [value + 1 for value in close],
            "low": [value - 1 for value in close],
            "close": close,
        })
        decision_time = times[-1].to_pydatetime() + timedelta(hours=1)
        self.assertIn(
            classify_market_regime(frame, decision_time),
            {"uptrend", "range_bound", "high_volatility"},
        )
        self.assertIsNone(
            classify_market_regime(frame.iloc[:40], decision_time)
        )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            classify_market_regime(frame, datetime(2026, 1, 5))


if __name__ == "__main__":
    unittest.main()
