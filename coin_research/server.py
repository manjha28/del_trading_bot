"""Read-only local HTTP API and frontend for the research database."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

from .store import ResearchStore

logger = logging.getLogger("coin_research.server")
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
    class ResearchRequestHandler(BaseHTTPRequestHandler):
        server_version = "CoinResearch/1.0"

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
                with ResearchStore(db_path, read_only=True) as store:
                    if path == "/api/summary":
                        result = store.summary()
                    elif path == "/api/products":
                        limit = _integer(params, "limit", 100, MAX_LIMIT)
                        offset = _integer(params, "offset", 0, 10_000_000)
                        execution = params.get("execution", [""])[0]
                        if execution not in ("", "eligible", "ineligible"):
                            raise ValueError("execution must be eligible or ineligible")
                        descending = params.get("direction", ["asc"])[0] == "desc"
                        result = store.list_products(
                            search=params.get("search", [""])[0],
                            state=params.get("state", [""])[0],
                            execution=execution,
                            sort=params.get("sort", ["symbol"])[0],
                            descending=descending,
                            limit=limit,
                            offset=offset,
                        )
                    elif path.startswith("/api/products/"):
                        tail = path[len("/api/products/"):]
                        symbol, *resource = tail.split("/")
                        symbol = unquote(symbol)
                        detail = store.product_detail(symbol)
                        if detail is None:
                            self._json(404, {"error": "symbol not found"})
                            return
                        if not resource:
                            result = detail
                        elif resource[0] == "candles":
                            timeframe = params.get("timeframe", ["1h"])[0]
                            if timeframe not in ("1m", "5m", "1h"):
                                raise ValueError("timeframe must be 1m, 5m, or 1h")
                            limit = _integer(params, "limit", 500, 5000)
                            result = store.list_candles(detail["product_id"], timeframe, limit)
                        else:
                            self._json(404, {"error": "not found"})
                            return
                    elif path == "/api/events":
                        result = store.list_events(
                            symbol=params.get("symbol", [None])[0],
                            event_type=params.get("type", [None])[0],
                            limit=_integer(params, "limit", 100, MAX_LIMIT),
                            offset=_integer(params, "offset", 0, 10_000_000),
                        )
                    elif path == "/api/outcomes":
                        horizon = params.get("horizon_hours", [None])[0]
                        result = store.list_outcomes(
                            symbol=params.get("symbol", [None])[0],
                            variant=params.get("variant", [None])[0],
                            horizon_hours=int(horizon) if horizon else None,
                            limit=_integer(params, "limit", 100, MAX_LIMIT),
                            offset=_integer(params, "offset", 0, 10_000_000),
                        )
                    else:
                        self._json(404, {"error": "not found"})
                        return
                self._json(200, result)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except (sqlite3.Error, OSError):
                logger.exception("Research API request failed")
                self._json(500, {"error": "research database request failed"})

    return ResearchRequestHandler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="coin-research-web",
        description="Serve the read-only local market-wide research dashboard.",
    )
    parser.add_argument("--db", default="data/sleeping_coin_research.sqlite3")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (defaults to localhost)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 0 < args.port < 65536:
        parser.error("--port must be between 1 and 65535")
    if args.host != "localhost":
        try:
            is_loopback = ipaddress.ip_address(args.host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            parser.error("dashboard binds to loopback only; use localhost or a loopback IP")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    httpd = ThreadingHTTPServer((args.host, args.port), create_handler(args.db))
    logger.info("Serving read-only research dashboard at http://%s:%d", args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Dashboard server stopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
