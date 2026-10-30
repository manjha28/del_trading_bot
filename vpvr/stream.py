"""Delta India public WebSocket market-data stream.

Trade WebSocket messages expose buyer_role (maker/taker), which is NOT the
aggressor side. This stream therefore never emits a fabricated CVD side.
Use Delta's REST trade endpoint side field for real aggressor CVD.
"""
from __future__ import annotations
import json
import threading
import time
from collections import defaultdict, deque

PUBLIC_WS_URL = "wss://public-socket.india.delta.exchange"


class DeltaPublicStream:
    def __init__(self, symbols, url=PUBLIC_WS_URL, max_events=5000, on_event=None):
        symbols = list(symbols)
        if not symbols or len(symbols) > 100 or len(set(symbols)) != len(symbols):
            raise ValueError("provide 1..100 unique symbols")
        self.symbols, self.url, self.max_events, self.on_event = symbols, url, max_events, on_event
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = self._ws = None
        self._events = defaultdict(lambda: deque(maxlen=max_events))
        self._seen = set()
        self._last_event_at = 0.0
        self._last_error = None
        self._connected = False
        self._reconnects = 0

    def subscription(self):
        names = ("ticker", "trades", "funding_rate", "ob_l2",
                 "candlesticks_1m", "candlesticks_3m", "candlesticks_15m")
        return {"type": "subscribe", "payload": {"channels": [
            {"name": name, "symbols": self.symbols} for name in names
        ]}}

    def _publish(self, event):
        symbol = event.get("symbol")
        if not symbol:
            return
        with self._lock:
            self._events[symbol].append(event)
            self._last_event_at = time.time()
        if self.on_event:
            try:
                self.on_event(event)
            except Exception as exc:
                self._last_error = f"event callback failed: {exc}"

    def _on_open(self, ws):
        self._connected = True
        self._last_error = None
        ws.send(json.dumps(self.subscription()))

    def _on_close(self, ws, code, message):
        self._connected = False

    def _on_error(self, ws, error):
        self._connected = False
        self._last_error = str(error)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except (TypeError, ValueError):
            self._last_error = "invalid WebSocket JSON"
            return
        kind = data.get("type")
        if kind in ("subscriptions", "heartbeat", "pong"):
            return
        symbol = data.get("sy") or data.get("symbol") or data.get("product_symbol")
        if not symbol:
            return
        event = {"type": kind, "symbol": symbol, "received_at": time.time(), "raw": data}
        if kind == "trades":
            try:
                event.update(price=float(data["p"]), volume=float(data["s"]),
                             buyer_role=data.get("r"), timestamp_us=int(data["t"]),
                             aggressor_side=None)
            except (KeyError, TypeError, ValueError):
                return
            key = (symbol, event["timestamp_us"], event["price"], event["volume"], event["buyer_role"])
            with self._lock:
                if key in self._seen:
                    return
                self._seen.add(key)
                if len(self._seen) > self.max_events * 2:
                    self._seen.clear()
        elif kind and kind.startswith("candlesticks_"):
            try:
                event.update(time=int(data.get("t") or data.get("time")),
                             open=float(data.get("o") or data.get("open")),
                             high=float(data.get("h") or data.get("high")),
                             low=float(data.get("l") or data.get("low")),
                             close=float(data.get("c") or data.get("close")),
                             volume=float(data.get("v") or data.get("volume")))
            except (TypeError, ValueError):
                return
        elif kind == "ticker":
            for name in ("mark_price", "close", "best_bid", "best_ask", "turnover", "funding_rate"):
                if data.get(name) is not None:
                    try:
                        event[name] = float(data[name])
                    except (TypeError, ValueError):
                        pass
        elif kind == "funding_rate":
            try:
                event["funding_rate"] = float(data["fr"])
                event["next_funding_us"] = int(data.get("nfr", 0))
            except (KeyError, TypeError, ValueError):
                return
        elif kind in ("ob_l2", "ob_updates"):
            event["book"] = data
        self._publish(event)

    def _worker(self):
        try:
            import websocket
        except ImportError:
            self._last_error = "websocket-client dependency is missing"
            return
        delay = 1.0
        while not self._stop.is_set():
            self._ws = websocket.WebSocketApp(
                self.url, on_open=self._on_open, on_message=self._on_message,
                on_error=self._on_error, on_close=self._on_close
            )
            self._ws.run_forever(ping_interval=20, ping_timeout=10, reconnect=0)
            self._connected = False
            if self._stop.wait(delay):
                break
            self._reconnects += 1
            delay = min(delay * 2, 30.0)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="vpvr-delta-public-ws", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        self._connected = False

    def snapshot(self, symbol):
        with self._lock:
            return list(self._events.get(symbol, ()))

    def status(self):
        with self._lock:
            age = None if not self._last_event_at else time.time() - self._last_event_at
            return {"connected": self._connected, "last_event_age_seconds": age,
                    "last_error": self._last_error, "reconnects": self._reconnects}
