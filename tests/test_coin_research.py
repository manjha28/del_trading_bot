from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from unittest.mock import patch

from coin_research.dashboard import build_dashboard
from coin_research.cli import scan_once
from coin_research.features import _percentile, evaluate_symbol
from coin_research.market import DeltaResearchData
from coin_research.store import ResearchStore


def hourly_history(days=65, sleepy=False, awakening=False):
    count = days * 24
    times = pd.date_range("2026-01-01", periods=count, freq="h", tz="UTC")
    index = np.arange(count, dtype=float)
    close = 100 + 2 * np.sin(index / 13) + 0.02 * index
    volume = 20 + 10 * (1 + np.sin(index / 19))
    spread = 0.8
    if sleepy:
        close[-24:] = close[-25]
        volume[-24:] = 0.1
        spread = 0.002
    if awakening:
        volume[-24:] = 500
        close[-24:] = close[-25] * (1 + np.linspace(0.001, 0.2, 24))
        spread = 2.0
    frame = pd.DataFrame({
        "time": times,
        "open": close * (1 - spread / 300),
        "high": close * (1 + spread / 100),
        "low": close * (1 - spread / 100),
        "close": close,
        "volume": volume,
    })
    if sleepy:
        frame.loc[frame.index[-24:], "open"] = close[-25]
        frame.loc[frame.index[-24:], "high"] = close[-25] * 1.00001
        frame.loc[frame.index[-24:], "low"] = close[-25] * 0.99999
        frame.loc[frame.index[-24:], "close"] = close[-25]
    return frame


def short_frame(timeframe, count):
    interval = {"1m": "min", "5m": "5min"}[timeframe]
    times = pd.date_range("2026-03-05", periods=count, freq=interval, tz="UTC")
    close = 100 + np.sin(np.arange(count) / 5)
    return pd.DataFrame({
        "time": times, "open": close, "high": close + 0.2,
        "low": close - 0.2, "close": close, "volume": 10.0,
    })


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 3, 7, 0, tzinfo=timezone.utc)
        self.product = {"id": 10, "symbol": "TESTUSD", "state": "live",
                        "contract_type": "perpetual_futures"}
        self.frames = {
            "1h": hourly_history(),
            "1m": short_frame("1m", 100),
            "5m": short_frame("5m", 80),
        }

    def evaluate(self, frames=None, book=None, ticker=None):
        return evaluate_symbol(
            "TESTUSD", self.product, ticker or {}, frames or self.frames,
            book=book, now=self.now,
        )

    def test_equal_observations_do_not_create_false_extreme_percentiles(self):
        history = pd.Series([5.0] * 100)
        self.assertEqual(_percentile(5.0, history), 50.0)

    def test_full_history_exposes_own_baselines_and_unavailable_derived_data(self):
        observation = self.evaluate()
        self.assertIn(observation["state"], {"sleeping", "active", "awakening", "expansion", "climax"})
        self.assertIsNotNone(observation["sleeping_score"])
        self.assertIsNotNone(observation["features"]["volume_ratio_30d"])
        self.assertIsNotNone(observation["features"]["realized_volatility_1m_annualized_pct"])
        absorption = next(x for x in observation["signals"]
                          if x["variant"] == "order_book_absorption")
        self.assertFalse(absorption["available"])
        self.assertFalse(observation["execution"]["eligible"])

    def test_new_listing_with_short_history_is_not_classified_as_sleeping(self):
        frames = {**self.frames, "1h": hourly_history(days=15)}
        observation = self.evaluate(frames)
        self.assertEqual(observation["state"], "insufficient_history")
        self.assertIsNone(observation["sleeping_score"])
        self.assertTrue(any("30 required" in reason
                            for reason in observation["data_quality"]["issues"]))

    def test_low_volume_candidate_remains_discoverable_when_untradeable(self):
        frames = {**self.frames, "1h": hourly_history(sleepy=True)}
        observation = self.evaluate(frames)
        self.assertEqual(observation["state"], "sleeping")
        self.assertFalse(observation["execution"]["eligible"])
        self.assertIn("order book unavailable", observation["execution"]["reasons"])

    def test_expansion_features_are_relative_and_signals_are_shadow_only(self):
        frames = {**self.frames, "1h": hourly_history(awakening=True)}
        observation = self.evaluate(frames)
        self.assertTrue(observation["awakening"]["active_components"])
        self.assertIn(observation["state"], {"awakening", "expansion", "climax"})
        self.assertTrue(all(signal["direction"].endswith("research_only")
                            for signal in observation["signals"]
                            if signal.get("available") and signal.get("direction")))

    def test_failed_high_uses_completed_candle_structure(self):
        frames = {**self.frames, "1h": hourly_history()}
        frame = frames["1h"].copy()
        prior_high = frame.high.iloc[-25:-1].max()
        frame.loc[frame.index[-1], "high"] = prior_high * 1.01
        frame.loc[frame.index[-1], "close"] = prior_high * 0.995
        frame.loc[frame.index[-1], "open"] = prior_high * 0.99
        frames["1h"] = frame
        observation = self.evaluate(frames)
        self.assertTrue(any(signal["variant"] == "failed_high"
                            for signal in observation["signals"]))

    def test_depth_and_contract_metadata_are_required_for_execution_eligibility(self):
        product = {
            **self.product, "contract_value": 1, "notional_type": "vanilla",
            "is_quanto": False, "tick_size": 0.01, "lot_size": 1,
            "minimum_order_size": 1, "position_size_limit": 1000,
            "taker_commission": 0.0005,
        }
        book = {
            "buy": [{"price": 99.9, "size": 1000}],
            "sell": [{"price": 100.1, "size": 1000}],
        }
        observation = evaluate_symbol("TESTUSD", product, {}, self.frames, book=book,
                                      now=self.now, max_spread_pct=1.0, max_impact_pct=1.0)
        self.assertTrue(observation["market"]["book_available"])
        self.assertTrue(observation["execution"]["contract_specs_verified"])
        self.assertTrue(observation["execution"]["eligible"], observation["execution"]["reasons"])


class StoreTests(unittest.TestCase):
    def test_catalog_retains_new_inactive_and_unreturned_products_and_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchStore(Path(directory) / "research.sqlite3")
            store.reconcile_products([
                {"id": 1, "symbol": "BTCUSD", "contract_type": "perpetual_futures", "state": "live"},
                {"id": 2, "symbol": "OLDUSD", "contract_type": "perpetual_futures", "state": "expired"},
            ], "2026-01-01T00:00:00+00:00")
            store.record_observation("1", {
                "as_of": "2026-01-01T00:00:00+00:00", "state": "sleeping",
                "sleeping_score": 80, "features": {"volume_compression_percentile_60d": 10},
                "data_quality": {"confidence": 0.8, "issues": []},
                "execution": {"eligible": False, "reasons": ["wide spread"]},
                "signals": [{"variant": "failed_high", "available": True}],
            })
            store.reconcile_products([
                {"id": 1, "symbol": "BTCUSD", "contract_type": "perpetual_futures", "state": "live"},
            ], "2026-01-02T00:00:00+00:00")
            rows = {row["symbol"]: row for row in store.latest_observations()}
            self.assertIn("OLDUSD", rows)
            self.assertEqual(rows["OLDUSD"]["product_state"], "not_returned")
            self.assertEqual(rows["BTCUSD"]["state"], "sleeping")
            self.assertEqual(store.event_counts()["shadow_signal"], 1)
            store.close()

    def test_signal_outcomes_mature_at_fixed_horizons_and_subtract_observed_costs(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchStore(Path(directory) / "research.sqlite3")
            store.reconcile_products([
                {"id": 1, "symbol": "TESTUSD", "contract_type": "perpetual_futures", "state": "live"},
            ], "2026-01-01T00:00:00+00:00")
            observation = {
                "as_of": "2026-01-01T01:01:00+00:00",
                "state": "failed_auction",
                "signal_candle_close_time": "2026-01-01T01:00:00+00:00",
                "signals": [{"variant": "failed_high", "available": True,
                             "direction": "bearish_research_only"}],
                "market": {"price": 100.0, "execution_notional": 1000.0, "spread_pct": 0.1,
                           "estimated_buy_impact_pct": 0.05, "estimated_sell_impact_pct": 0.05,
                           "depth_notional_verified": True},
                "execution": {"contract_specs": {"taker_fee_rate": 0.0005}},
                "data_quality": {"confidence": 1.0, "issues": [], "hourly_fresh": True},
            }
            store.record_observation("1", observation)
            store.record_observation("1", observation)
            candles = pd.DataFrame({
                "time": pd.date_range("2026-01-01", periods=30, freq="h", tz="UTC"),
                "close": [100.0, 90.0] + [90.0] * 28,
            })
            store.update_signal_outcomes("1", candles, "2026-01-02T06:00:00+00:00")
            rows = store.outcome_records()
            one_hour = next(row for row in rows if row["horizon_hours"] == 1)
            self.assertEqual(len([row for row in rows if row["horizon_hours"] == 1]), 1)
            self.assertAlmostEqual(one_hour["gross_return_pct"], (100 / 90 - 1) * 100)
            self.assertAlmostEqual(one_hour["estimated_cost_pct"], 0.3)
            self.assertAlmostEqual(one_hour["net_return_pct"], one_hour["gross_return_pct"] - 0.3)
            dashboard = build_dashboard(
                store, Path(directory) / "dashboard.html", "2026-01-01", "2026-01-02"
            )
            self.assertIn("Prospective shadow-event outcomes",
                          dashboard.read_text(encoding="utf-8"))
            self.assertIn("out-of-sample", dashboard.read_text(encoding="utf-8"))
            store.close()


class DiscoveryTests(unittest.TestCase):
    def test_product_loader_keeps_every_perpetual_state_without_symbol_filters(self):
        data = DeltaResearchData(request_interval=0)
        first_page = [
            {"id": index, "symbol": f"SYM{index}", "contract_type": "perpetual_futures",
             "state": "expired" if index == 999 else "live"}
            for index in range(1000)
        ]
        second_page = [{"id": 1001, "symbol": "NEWUSD",
                        "contract_type": "perpetual_futures", "state": "live"}]
        with patch.object(data, "_get", side_effect=[
            {"result": first_page}, {"result": second_page},
        ]) as get:
            products = data.fetch_products()
        self.assertEqual(len(products), 1001)
        self.assertEqual(products[-2]["state"], "expired")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0].args[1], {"page": 1, "page_size": 1000})
        data.close()

    def test_scan_records_live_and_inactive_catalog_without_order_execution(self):
        class FakeMarketData:
            def __init__(self, **kwargs):
                self.closed = False

            def fetch_products(self):
                return [
                    {"id": 1, "symbol": "TESTUSD", "contract_type": "perpetual_futures",
                     "state": "live"},
                    {"id": 2, "symbol": "OLDUSD", "contract_type": "perpetual_futures",
                     "state": "expired"},
                ]

            def fetch_tickers(self):
                return {}

            def fetch_candles(self, symbol, timeframe, start, end):
                return hourly_history() if timeframe == "1h" else short_frame(
                    timeframe, 100 if timeframe == "1m" else 80
                )

            def fetch_book(self, symbol):
                return {"buy": [], "sell": []}

            def fetch_trades(self, symbol):
                return pd.DataFrame(columns=["time", "price", "volume", "side"])

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "research.sqlite3"
            dashboard = Path(directory) / "dashboard.html"
            with patch("coin_research.cli.DeltaResearchData", FakeMarketData):
                result = scan_once(
                    str(database), str(dashboard), request_interval=0,
                )
            self.assertEqual(result["catalog_perpetuals"], 2)
            self.assertEqual(result["observations"], 2)
            self.assertEqual(result["states"]["not_live"], 1)
            self.assertTrue(dashboard.exists())
            store = ResearchStore(database)
            products = store.latest_observations()
            store.close()
            self.assertEqual(len(products), 2)


if __name__ == "__main__":
    unittest.main()
