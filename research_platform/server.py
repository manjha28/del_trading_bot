from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

from .market_data import TIMEFRAME_SECONDS
from .store import ResearchDataStore


logger = logging.getLogger("research_platform.server")
WEB_ROOT = Path(__file__).with_name("web")
MAX_LIMIT = 500


def _integer(params: dict[str, list[str]], name: str, default: int, maximum: int) -> int:
    try:
        value = int(params.get(name, [str(default)])[0])
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return min(value, maximum)


def create_handler(db_path: str | Path):
    class PlatformRequestHandler(BaseHTTPRequestHandler):
        server_version = "UnifiedResearch/1.0"

        def log_message(self, fmt, *args):
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, body) -> None:
            encoded = json.dumps(body, default=str, allow_nan=False).encode("utf-8")
            self._send(status, encoded, "application/json; charset=utf-8")

        def do_GET(self):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query, keep_blank_values=False)
            path = parsed.path.rstrip("/") or "/"
            try:
                if path == "/":
                    content = (WEB_ROOT / "index.html").read_bytes()
                    self._send(200, content, "text/html; charset=utf-8")
                    return
                if not path.startswith("/api/"):
                    self._json(404, {"error": "not found"})
                    return
                with ResearchDataStore(db_path, read_only=True) as store:
                    if path == "/api/summary":
                        result = store.dashboard_summary()
                    elif path == "/api/products":
                        state = params.get("state", [""])[0]
                        result = store.list_instrument_rows(
                            search=params.get("search", [""])[0],
                            state=state,
                            limit=_integer(params, "limit", 100, MAX_LIMIT),
                            offset=_integer(params, "offset", 0, 10_000_000),
                        )
                    elif path.startswith("/api/products/"):
                        parts = path[len("/api/products/"):].split("/")
                        product_id = unquote(parts[0])
                        detail = store.instrument_detail(product_id)
                        if detail is None:
                            self._json(404, {"error": "product not found"})
                            return
                        if len(parts) == 1:
                            result = detail
                        elif len(parts) == 2 and parts[1] == "candles":
                            timeframe = params.get("timeframe", ["1h"])[0]
                            if timeframe not in TIMEFRAME_SECONDS:
                                raise ValueError("unsupported candle timeframe")
                            result = store.list_candle_rows(
                                product_id, timeframe,
                                _integer(params, "limit", 500, 5000),
                            )
                        else:
                            self._json(404, {"error": "not found"})
                            return
                    elif path == "/api/capabilities":
                        result = {
                            "canonical_timeframes": sorted(TIMEFRAME_SECONDS),
                            "stored_data_types": [
                                "instruments", "candles", "tickers", "trades",
                                "orderbook_snapshots", "funding", "data_quality",
                            ],
                            "not_implemented": [
                                "orderbook_deltas", "open_interest_history",
                                    "liquidation_history", "normalized_strategy_exits",
                                    "portfolio_simulator", "walk_forward_metrics",
                                    "selection_bias_adjustment", "confidence_intervals",
                                ],
                                "available_research_tools": [
                                    "single_trade_ohlcv_paper_simulator",
                                    "chronological_train_validation_oos_split",
                                    "experiment_registry",
                                ],
                                "paper_simulator_limitations": [
                                    "single trade at a time",
                                    "no partial fills, market impact, funding cash flows, or portfolio netting",
                                    "unverified contract specifications rejected",
                                    "OHLCV ambiguity resolved stop-first",
                                ],
                        }
                    elif path == "/api/experiments":
                        result = store.list_experiments(
                            _integer(params, "limit", 100, MAX_LIMIT),
                            _integer(params, "offset", 0, 10_000_000),
                        )
                    elif path == "/api/simulations":
                        result = store.list_paper_simulations(
                            _integer(params, "limit", 100, MAX_LIMIT),
                            _integer(params, "offset", 0, 10_000_000),
                            params.get("symbol", [None])[0],
                        )
                    else:
                        self._json(404, {"error": "not found"})
                        return
                self._json(200, result)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except FileNotFoundError:
                self._json(503, {"error": "research database does not exist; run collector first"})
            except (sqlite3.Error, OSError):
                logger.exception("Unified research API request failed")
                self._json(500, {"error": "research database request failed"})

    return PlatformRequestHandler


def serve(db_path: str | Path, host: str = "127.0.0.1", port: int = 8766) -> None:
    if not 0 < port < 65536:
        raise ValueError("port must be between 1 and 65535")
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"
    if not is_loopback:
        raise ValueError("dashboard binds to loopback only")
    httpd = ThreadingHTTPServer((host, port), create_handler(db_path))
    logger.info("Serving read-only unified dashboard at http://%s:%d", host, port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="research-platform-web",
        description="Serve the read-only unified local research dashboard.",
    )
    parser.add_argument("--db", default="data/unified_research.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        serve(args.db, args.host, args.port)
    except ValueError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        logger.info("Unified research dashboard stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
