from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

import pandas as pd

from research_platform.market_data import BookLevel, Instrument, OrderBookSnapshot, TradePrint
from research_platform.strategies import StrategyContext
from research_platform.variants import ResearchVariantContext, ResearchVariantSuite


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def frame(timeframe: str, count: int, step: str) -> pd.DataFrame:
    times = pd.date_range(T0, periods=count, freq=step)
    return pd.DataFrame({
        "time": times,
        "open": [100.0] * count,
        "high": [101.0] * count,
        "low": [99.0] * count,
        "close": [100.0] * count,
        "volume": [10.0] * count,
    })


def context(frames: dict[str, pd.DataFrame], *, trades=(), books=(), complete=False):
    last_end = max(
        pd.Timestamp(df.time.iloc[-1]) + pd.Timedelta(timeframe)
        for timeframe, df in frames.items()
    )
    strategy = StrategyContext(
        Instrument("1", "TESTUSD", "perpetual_futures", "live"),
        last_end.to_pydatetime(),
        frames,
        provenance={key: "fixture" for key in frames},
    )
    return ResearchVariantContext(strategy, tuple(trades), tuple(books), complete)


class ResearchVariantTests(unittest.TestCase):
    def setUp(self):
        self.suite = ResearchVariantSuite()

    def test_catalog_explicitly_marks_unavailable_derived_data_variants(self):
        catalog = {item.strategy_id: item for item in self.suite.catalog}
        self.assertEqual(
            catalog["absorption_price_rejection"].status, "unavailable"
        )
        self.assertEqual(catalog["liquidation_cascade"].status, "unavailable")
        self.assertEqual(catalog["perpetual_basis_funding"].status, "unavailable")
        self.assertIn("cannot establish absorption", " ".join(
            catalog["absorption_price_rejection"].limitations
        ))

    def test_ohlcv_sweep_reclaim_is_labeled_as_price_proxy(self):
        candles = frame("5m", 22, "5min")
        candles.loc[20, ["high", "low", "close"]] = [102, 98, 100]
        candles.loc[21, ["open", "high", "low", "close"]] = [100, 101, 97, 99]
        result = self.suite.evaluate(
            "liquidity_sweep_reclaim", context({"5m": candles})
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].direction, "LONG")
        self.assertIn("price-structure proxy", result[0].diagnostics["reasons"][-1])

    def test_failed_high_vwap_retest_uses_only_completed_candles(self):
        candles = frame("1h", 20, "1h")
        candles.loc[15, ["high", "low", "close"]] = [120, 110, 115]
        candles.loc[16, ["high", "low", "close"]] = [117, 110, 114]
        candles.loc[17, ["high", "low", "close"]] = [118, 109, 113]
        candles.loc[18, ["high", "low", "close"]] = [119.7, 108, 114]
        candles.loc[19, ["high", "low", "close"]] = [119.8, 107, 110]
        result = self.suite.evaluate(
            "failed_high_vwap_retest", context({"1h": candles})
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].direction, "SHORT")
        self.assertEqual(result[0].decision_time, pd.Timestamp(candles.time.iloc[19] + pd.Timedelta(hours=1)).to_pydatetime())
        self.assertEqual(result[0].provenance["1h"], "fixture")

    def test_trade_flow_rejects_incomplete_or_unknown_side_tape(self):
        candles = frame("1m", 20, "1min")
        candles.loc[15:, "close"] = [101, 102, 103, 104, 105]
        timestamp = pd.Timestamp(candles.time.iloc[-1]).to_pydatetime() + timedelta(seconds=30)
        prints = tuple(
            TradePrint("1", "TESTUSD", timestamp, 105, 1, "buy", "fixture", str(i))
            for i in range(19)
        ) + (
            TradePrint("1", "TESTUSD", timestamp, 105, 1, "unknown", "fixture", "unknown"),
        )
        self.assertEqual(self.suite.evaluate(
            "trade_flow_momentum", context({"1m": candles}, trades=prints, complete=False)
        ), [])
        self.assertEqual(self.suite.evaluate(
            "trade_flow_momentum", context({"1m": candles}, trades=prints, complete=True)
        ), [])

    def test_trade_flow_momentum_requires_explicit_complete_aggressor_tape(self):
        candles = frame("1m", 20, "1min")
        candles.loc[15:, "close"] = [101, 102, 103, 104, 105]
        timestamp = pd.Timestamp(candles.time.iloc[-1]).to_pydatetime() + timedelta(seconds=30)
        prints = tuple(
            TradePrint("1", "TESTUSD", timestamp, 105, 2, "buy", "fixture", str(i))
            for i in range(20)
        )
        result = self.suite.evaluate(
            "trade_flow_momentum",
            context({"1m": candles}, trades=prints, complete=True),
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].direction, "LONG")

    def test_book_imbalance_requires_fresh_synchronized_snapshots(self):
        candles = frame("1m", 20, "1min")
        now = pd.Timestamp(candles.time.iloc[-1]).to_pydatetime() + timedelta(minutes=1)
        book_args = {
            "product_id": "1", "symbol": "TESTUSD", "source": "fixture",
            "bids": tuple(BookLevel(99 - i * .1, 10) for i in range(5)),
            "asks": tuple(BookLevel(101 + i * .1, 1) for i in range(5)),
            "integrity": "synchronized",
        }
        books = (
            OrderBookSnapshot(observed_at=now - timedelta(seconds=30), **book_args),
            OrderBookSnapshot(observed_at=now - timedelta(seconds=10), **book_args),
        )
        result = self.suite.evaluate(
            "order_book_imbalance_microprice",
            context({"1m": candles}, books=books),
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].direction, "LONG")

    def test_unknown_variant_raises_and_available_sweep_has_no_future_candle(self):
        with self.assertRaisesRegex(ValueError, "unknown research variant"):
            self.suite.evaluate("invented", context({"5m": frame("5m", 22, "5min")}))
        candles = frame("5m", 22, "5min")
        candles.loc[20, ["high", "low", "close"]] = [102, 98, 100]
        candles.loc[21, ["open", "high", "low", "close"]] = [100, 101, 97, 99]
        cut_context = context({"5m": candles.iloc[:-1].copy()})
        self.assertEqual(self.suite.evaluate("liquidity_sweep_reclaim", cut_context), [])


if __name__ == "__main__":
    unittest.main()
