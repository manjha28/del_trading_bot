from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import Mock

import pandas as pd

from research_platform.market_data import (
    Candle,
    DataQuality,
    DeltaResearchAdapter,
    Instrument,
    OrderBookSnapshot,
    UnsupportedCapabilityError,
    VPVRDataAdapter,
)


class SharedMarketContractTests(unittest.TestCase):
    def test_instrument_preserves_identity_metadata_and_inactive_state(self):
        instrument = Instrument.from_delta({
            "id": 912, "symbol": "OLDUSD", "contract_type": "perpetual_futures",
            "state": "expired", "tick_size": "0.01",
        })
        self.assertEqual(instrument.product_id, "912")
        self.assertEqual(instrument.state, "expired")
        self.assertEqual(instrument.metadata["tick_size"], "0.01")

    def test_contract_metadata_is_visible_but_unverified_without_a_trusted_policy(self):
        instrument = Instrument.from_delta({
            "id": 77, "symbol": "TESTUSD", "contract_type": "perpetual_futures",
            "state": "live", "contract_value": "1", "tick_size": "0.01",
            "position_size_limit": "1000",
        })
        self.assertEqual(instrument.contract_specs.contract_multiplier, 1)
        self.assertEqual(instrument.contract_specs.tick_size, 0.01)
        self.assertFalse(instrument.contract_specs.verified)
        self.assertTrue(instrument.contract_specs.verification_errors)

    def test_market_records_require_timezone_and_valid_ohlc(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            Candle(
                "1", "BTCUSD", "1m", datetime(2026, 1, 1), 10, 11, 9, 10, 1,
                "test", True,
            )
        with self.assertRaisesRegex(ValueError, "high is inconsistent"):
            Candle(
                "1", "BTCUSD", "1m", datetime(2026, 1, 1, tzinfo=timezone.utc),
                10, 9, 8, 10, 1, "test", True,
            )
        with self.assertRaisesRegex(ValueError, "completeness"):
            DataQuality("available", None, "test", completeness=1.1)
        with self.assertRaisesRegex(ValueError, "quality state"):
            DataQuality("missing", None, "test")

    def test_delta_adapter_declares_supported_fields_without_inventing_history(self):
        adapter = DeltaResearchAdapter(Mock())
        self.assertEqual(
            adapter.capabilities.catalog_states,
            frozenset({"upcoming", "live", "expired", "settled"}),
        )
        self.assertEqual(adapter.capabilities.candle_timeframes, frozenset({"1m", "5m", "1h"}))
        self.assertTrue(adapter.capabilities.complete_catalog_snapshot)
        self.assertFalse(adapter.capabilities.open_interest_history)
        self.assertFalse(adapter.capabilities.orderbook_deltas)
        self.assertFalse(adapter.capabilities.liquidation_history)

    def test_vpvr_contract_verification_flows_into_canonical_instrument(self):
        client = Mock()
        client.fetch_products.return_value = [{
            "id": 77, "symbol": "BTCUSD", "contract_type": "perpetual_futures",
            "state": "live", "contract_value": "0.001", "tick_size": "0.5",
            "position_size_limit": "125000", "contract_unit_currency": "BTC",
        }]
        client.contract_specs.return_value = {
            "spec_verified": True,
            "lot_size": 1,
            "minimum_order_contracts": 1,
            "verification_policy": "verified-policy",
            "verification_errors": [],
        }
        instrument = VPVRDataAdapter(client).fetch_products()[0]
        self.assertTrue(instrument.contract_specs.verified)
        self.assertEqual(instrument.contract_specs.lot_increment, 1)
        self.assertEqual(instrument.contract_specs.verification_source, "verified-policy")

    def test_coin_research_adapter_normalizes_candle_and_completeness(self):
        client = Mock()
        client.fetch_candles.return_value = pd.DataFrame([{
            "time": pd.Timestamp("2026-01-01T00:00:00Z"),
            "open": 100, "high": 105, "low": 99, "close": 104, "volume": 12,
        }])
        adapter = DeltaResearchAdapter(client)
        instrument = Instrument("77", "TESTUSD", "perpetual_futures", "live")
        candles = adapter.fetch_candles(instrument, "1m", 1767225600, 1767225660)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].product_id, "77")
        self.assertEqual(candles[0].volume, 12)
        self.assertTrue(candles[0].is_complete)
        self.assertEqual(candles[0].close_time, datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))

    def test_coin_adapter_keeps_trade_aggressor_side_and_source_time(self):
        client = Mock()
        client.fetch_trades.return_value = pd.DataFrame([{
            "time": pd.Timestamp("2026-01-01T00:00:01Z"),
            "price": 101.5, "volume": 2, "side": "buy", "trade_id": "t1",
        }])
        adapter = DeltaResearchAdapter(client)
        trade = adapter.fetch_trades(Instrument("77", "TESTUSD", "perpetual_futures", "live"))[0]
        self.assertEqual(trade.aggressor_side, "buy")
        self.assertEqual(trade.trade_id, "t1")
        self.assertEqual(trade.timestamp, datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc))

    def test_book_snapshot_marks_missing_sequence_integrity_as_unprovided(self):
        client = Mock()
        client.fetch_book.return_value = {
            "buy": [{"price": "99.5", "size": "2"}],
            "sell": [["100.5", "3"]],
        }
        adapter = DeltaResearchAdapter(client)
        book = adapter.fetch_orderbook(Instrument("77", "TESTUSD", "perpetual_futures", "live"))
        self.assertEqual(book.bids[0].price, 99.5)
        self.assertEqual(book.asks[0].quantity, 3)
        self.assertIsNone(book.sequence)
        self.assertEqual(book.integrity, "not_provided")
        self.assertIsNone(book.exchange_timestamp)

    def test_vpvr_adapter_rejects_timeframes_it_does_not_provide(self):
        adapter = VPVRDataAdapter(Mock())
        with self.assertRaisesRegex(UnsupportedCapabilityError, "does not provide 5m"):
            adapter.fetch_candles(
                Instrument("1", "BTCUSD", "perpetual_futures", "live"),
                "5m", 1767225600, 1767225900,
            )

    def test_vpvr_funding_history_retains_exchange_timestamps(self):
        client = Mock()
        client.fetch_funding_history.return_value = pd.DataFrame([{
            "time": pd.Timestamp("2026-01-01T08:00:00Z"), "funding_rate": 0.0001,
        }])
        adapter = VPVRDataAdapter(client)
        observations = adapter.fetch_funding_history(
            Instrument("1", "BTCUSD", "perpetual_futures", "live"),
            1767225600, 1767254400,
        )
        self.assertEqual(observations[0].timestamp, datetime(2026, 1, 1, 8, tzinfo=timezone.utc))
        self.assertAlmostEqual(observations[0].rate, 0.0001)


if __name__ == "__main__":
    unittest.main()
