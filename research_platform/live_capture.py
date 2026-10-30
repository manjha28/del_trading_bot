from __future__ import annotations

from datetime import datetime, timezone
import queue
import threading
import time
from typing import Callable, Iterable

from .market_data import Instrument
from .store import ResearchDataStore


class DurableMarketCapture:
    """Persist public Delta WebSocket events on the SQLite-owning thread."""

    def __init__(
        self,
        store: ResearchDataStore,
        stream_factory: Callable | None = None,
        *,
        batch_size: int = 100,
        queue_capacity: int = 50_000,
    ):
        if batch_size < 1 or batch_size > 100:
            raise ValueError("batch_size must be between 1 and 100")
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        self.store = store
        self.stream_factory = stream_factory or self._delta_stream
        self.batch_size = batch_size
        self.events: queue.Queue[tuple[str, dict]] = queue.Queue(queue_capacity)
        self.streams: dict[str, object] = {}
        self.products_by_symbol: dict[str, Instrument] = {}
        self.dropped_events: dict[str, int] = {}
        self._drop_lock = threading.Lock()
        self._last_health: dict[str, tuple] = {}

    @staticmethod
    def _delta_stream(symbols, on_event):
        from vpvr.stream import DeltaPublicStream

        return DeltaPublicStream(symbols, on_event=on_event)

    def sync_universe(self, instruments: Iterable[Instrument]) -> None:
        live = sorted(
            (item for item in instruments
             if item.contract_type == "perpetual_futures" and item.state == "live"),
            key=lambda item: (item.symbol, item.product_id),
        )
        self.products_by_symbol = {item.symbol: item for item in live}
        symbols = sorted(self.products_by_symbol)
        desired = [
            symbols[index:index + self.batch_size]
            for index in range(0, len(symbols), self.batch_size)
        ]
        desired_keys = {
            f"delta-public-{index:03d}": chunk
            for index, chunk in enumerate(desired)
        }
        for stream_id in set(self.streams).difference(desired_keys):
            stream = self.streams.pop(stream_id)
            stream.close()
            self._record_health(
                stream_id, stream, "stopped",
                tuple(getattr(stream, "symbols", ())),
            )
        for stream_id, chunk in desired_keys.items():
            stream = self.streams.get(stream_id)
            if stream is not None and tuple(getattr(stream, "symbols", ())) == tuple(chunk):
                continue
            if stream is not None:
                stream.close()
                self._record_health(
                    stream_id, stream, "stopped",
                    tuple(getattr(stream, "symbols", ())),
                )
            stream = self.stream_factory(
                chunk, lambda event, key=stream_id: self._enqueue(key, event)
            )
            self.streams[stream_id] = stream
            stream.start()
            status = stream.status()
            state = "connected" if status["connected"] else (
                "error" if status.get("last_error") else "connecting"
            )
            self._record_health(
                stream_id, stream, state, tuple(chunk), force=True, status=status
            )

    def _enqueue(self, stream_id: str, event: dict) -> None:
        try:
            self.events.put_nowait((stream_id, event))
        except queue.Full:
            self._drop(stream_id)

    def _drop(self, stream_id: str) -> None:
        with self._drop_lock:
            self.dropped_events[stream_id] = self.dropped_events.get(stream_id, 0) + 1

    def _drop_count(self, stream_id: str) -> int:
        with self._drop_lock:
            return self.dropped_events.get(stream_id, 0)

    @staticmethod
    def _exchange_time(event: dict) -> datetime | None:
        raw = event.get("raw") or {}
        timestamp = event.get("timestamp_us")
        if timestamp is None and str(event.get("type", "")).startswith("candlestick_"):
            timestamp = event.get("time")
            unit = "s"
        else:
            unit = "us"
        if timestamp is None:
            timestamp = raw.get("timestamp") or raw.get("ts")
            magnitude = abs(float(timestamp)) if timestamp is not None else 0
            unit = "s" if magnitude < 10**11 else ("ms" if magnitude < 10**14 else "us")
        if timestamp is None:
            return None
        numeric = float(timestamp)
        scale = {"s": 1, "ms": 1_000, "us": 1_000_000}[unit]
        return datetime.fromtimestamp(numeric / scale, timezone.utc)

    def drain(self, maximum: int = 10_000) -> int:
        if maximum < 1:
            raise ValueError("maximum must be positive")
        persisted = 0
        while persisted < maximum:
            try:
                stream_id, event = self.events.get_nowait()
            except queue.Empty:
                break
            symbol = str(event.get("symbol") or "")
            instrument = self.products_by_symbol.get(symbol)
            if instrument is None:
                self._drop(stream_id)
                continue
            raw = event.get("raw") or {}
            source_id = None
            if event.get("type") == "trades":
                source_id = next(
                    (str(raw[key]) for key in ("id", "trade_id", "i") if raw.get(key) is not None),
                    None,
                )
            raw_sequence = raw.get("sequence", raw.get("seq"))
            try:
                sequence = int(raw_sequence) if raw_sequence is not None else None
            except (TypeError, ValueError):
                sequence = None
            if sequence is not None and sequence < 0:
                sequence = None
            received = event.get("received_at")
            received_at = (
                datetime.fromtimestamp(float(received), timezone.utc)
                if received is not None else datetime.now(timezone.utc)
            )
            self.store.save_market_event(
                instrument.product_id,
                instrument.symbol,
                str(event.get("type") or "unknown"),
                received_at,
                event,
                exchange_time=self._exchange_time(event),
                source_event_id=source_id,
                sequence=sequence,
                integrity="not_provided",
            )
            persisted += 1
        self.update_health()
        return persisted

    def update_health(self, force: bool = False) -> None:
        now = datetime.now(timezone.utc)
        for stream_id, stream in self.streams.items():
            status = stream.status()
            state = "connected" if status["connected"] else (
                "error" if status.get("last_error") else "connecting"
            )
            signature = (
                state, status.get("reconnects", 0), self._drop_count(stream_id),
                status.get("last_error"),
            )
            previous, last_written = self._last_health.get(stream_id, (None, 0.0))
            if not force and signature == previous and time.monotonic() - last_written < 30:
                continue
            self._record_health(
                stream_id, stream, state, tuple(getattr(stream, "symbols", ())),
                status=status, observed_at=now,
            )
            self._last_health[stream_id] = (signature, time.monotonic())

    def _record_health(
        self,
        stream_id: str,
        stream: object,
        state: str,
        symbols: tuple[str, ...],
        *,
        force: bool = False,
        status: dict | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        status = status or stream.status()
        age = status.get("last_event_age_seconds")
        last_event_at = (
            datetime.fromtimestamp(time.time() - age, timezone.utc)
            if age is not None else None
        )
        self.store.record_stream_health(
            stream_id=stream_id,
            observed_at=observed_at or datetime.now(timezone.utc),
            state=state,
            connected=bool(status.get("connected")),
            reconnects=int(status.get("reconnects") or 0),
            dropped_events=self._drop_count(stream_id),
            symbols=symbols,
            last_event_at=last_event_at,
            last_error=status.get("last_error"),
        )
        if force:
            self._last_health.pop(stream_id, None)

    def close(self) -> None:
        for stream_id, stream in list(self.streams.items()):
            stream.close()
        while self.drain():
            pass
        for stream_id, stream in list(self.streams.items()):
            self._record_health(
                stream_id, stream, "stopped",
                tuple(getattr(stream, "symbols", ())),
            )
        self.streams.clear()
