from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from research_platform.live_capture import DurableMarketCapture
from research_platform.market_data import Instrument
from research_platform.store import ResearchDataStore


UTC = timezone.utc


class FixtureStream:
    def __init__(self, symbols, on_event):
        self.symbols = tuple(symbols)
        self.on_event = on_event
        self.connected = False
        self.closed = False

    def start(self):
        self.connected = True

    def close(self):
        self.connected = False
        self.closed = True

    def status(self):
        return {
            "connected": self.connected,
            "last_event_age_seconds": None,
            "last_error": None,
            "reconnects": 0,
        }

    def emit(self, event):
        self.on_event(event)


class DurableMarketCaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ResearchDataStore(Path(self.directory.name) / "research.sqlite3")
        self.instrument = Instrument("1", "TESTUSD", "perpetual_futures", "live")
        self.inactive = Instrument("2", "OLDUSD", "perpetual_futures", "expired")
        self.store.reconcile_instruments([self.instrument, self.inactive])
        self.created = []

        def factory(symbols, on_event):
            stream = FixtureStream(symbols, on_event)
            self.created.append(stream)
            return stream

        self.capture = DurableMarketCapture(
            self.store, factory, batch_size=1, queue_capacity=1,
        )

    def tearDown(self):
        self.capture.close()
        self.store.close()
        self.directory.cleanup()

    def test_persists_timestamped_events_and_marks_book_integrity_unknown(self):
        self.capture.sync_universe([self.instrument, self.inactive])
        self.assertEqual(len(self.created), 1)
        stream = self.created[0]
        stream.emit({
            "type": "trades",
            "symbol": "TESTUSD",
            "received_at": 1767225601.0,
            "timestamp_us": 1767225600000000,
            "price": 100.5,
            "volume": 2.0,
            "aggressor_side": None,
            "raw": {"id": "trade-1", "r": "taker"},
        })
        self.assertEqual(self.capture.drain(), 1)
        trade = self.store.list_market_events(event_type="trades")["items"][0]
        self.assertEqual(trade["product_id"], "1")
        self.assertEqual(trade["source_event_id"], "trade-1")
        self.assertEqual(
            trade["exchange_time"], datetime(2026, 1, 1, tzinfo=UTC).isoformat()
        )
        self.assertIsNone(trade["payload"]["aggressor_side"])

        stream.emit({
            "type": "ob_l2",
            "symbol": "TESTUSD",
            "received_at": 1767225602.0,
            "book": {"buy": [], "sell": []},
            "raw": {"b": [], "a": []},
        })
        self.capture.drain()
        book = self.store.list_market_events(event_type="ob_l2")["items"][0]
        self.assertEqual(book["integrity"], "not_provided")
        self.assertIsNone(book["sequence"])
        self.assertEqual(self.store.coverage_summary()["market_event_count"], 2)

    def test_queue_overflow_and_unknown_symbols_are_visible_as_drops(self):
        self.capture.sync_universe([self.instrument])
        stream = self.created[0]
        event = {"type": "ticker", "symbol": "TESTUSD", "received_at": 1.0, "raw": {}}
        stream.emit(event)
        stream.emit(event)
        self.assertEqual(self.capture.dropped_events["delta-public-000"], 1)
        self.capture.drain()
        stream.emit({**event, "symbol": "UNKNOWN"})
        self.capture.drain()
        health = self.store.list_stream_health()["items"][0]
        self.assertEqual(health["dropped_events"], 2)
        self.assertEqual(health["symbols"], ["TESTUSD"])

    def test_sync_shards_live_universe_and_records_stop_state(self):
        other = Instrument("3", "OTHERUSD", "perpetual_futures", "live")
        self.store.reconcile_instruments([self.instrument, self.inactive, other])
        self.capture.sync_universe([self.instrument, self.inactive, other])
        self.assertEqual(len(self.created), 2)
        self.assertEqual(
            {symbol for stream in self.created for symbol in stream.symbols},
            {"TESTUSD", "OTHERUSD"},
        )
        self.capture.sync_universe([])
        self.assertTrue(all(stream.closed for stream in self.created))
        states = {item["state"] for item in self.store.list_stream_health()["items"]}
        self.assertIn("stopped", states)


if __name__ == "__main__":
    unittest.main()
