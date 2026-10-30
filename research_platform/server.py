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
from .variants import VARIANT_CATALOG
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


def _read_external_sources(source_paths: dict[str, str | Path]) -> dict:
    sources = {}
    coin_path = Path(source_paths["coin_research_db"])
    if not coin_path.is_file():
        sources["coin_research"] = {"status": "unavailable", "reason": "database not found"}
    else:
        try:
            from coin_research.store import ResearchStore

            with ResearchStore(coin_path, read_only=True) as store:
                sources["coin_research"] = {
                    "status": "available",
                    "summary": store.summary(),
                    "event_outcomes": store.list_outcomes(limit=20)["items"],
                }
        except (sqlite3.Error, OSError, ValueError):
            logger.exception("Could not read coin-research store")
            sources["coin_research"] = {
                "status": "error",
                "reason": "coin-research store could not be read",
            }

    paper_path = Path(source_paths["vpvr_paper_state"])
    if not paper_path.is_file():
        sources["vpvr_paper"] = {"status": "unavailable", "reason": "paper state not found"}
    else:
        try:
            paper = json.loads(paper_path.read_text(encoding="utf-8"))
            if paper.get("mode") != "PAPER_ONLY":
                sources["vpvr_paper"] = {
                    "status": "error",
                    "reason": "paper state is not marked PAPER_ONLY",
                }
            else:
                sources["vpvr_paper"] = {
                    "status": "available",
                    "mode": paper.get("mode"),
                    "balance": paper.get("balance"),
                    "position_count": len(paper.get("positions", {})),
                    "pending_count": len(paper.get("pending", {})),
                    "trade_count": len(paper.get("trades", [])),
                    "recent_trades": paper.get("trades", [])[-20:],
                }
        except (OSError, json.JSONDecodeError, AttributeError):
            logger.exception("Could not read VPVR paper state")
            sources["vpvr_paper"] = {
                "status": "error",
                "reason": "VPVR paper state is invalid or unreadable",
            }

    reports = Path(source_paths["mhr_results_dir"])
    if not reports.is_dir():
        sources["mhr_reports"] = {"status": "unavailable", "reason": "report directory not found"}
    else:
        files = sorted(
            (path for path in reports.glob("*.csv") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        sources["mhr_reports"] = {
            "status": "available",
            "report_count": len(files),
            "latest_reports": [
                {"name": path.name, "modified_at": path.stat().st_mtime, "bytes": path.stat().st_size}
                for path in files[:20]
            ],
        }
    return sources


def create_handler(
    db_path: str | Path,
    source_paths: dict[str, str | Path] | None = None,
):
    source_paths = source_paths or {
        "coin_research_db": "data/sleeping_coin_research.sqlite3",
        "vpvr_paper_state": "data/vpvr_paper_state.json",
        "mhr_results_dir": "strategy-short/mhr_pump_short/backtest_results",
    }

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
                                "liquidation_history", "unified_cross_strategy_exit_engine",
                                "live_strategy_exit_integration",
                                "cross_strategy_full_data_walk_forward_execution",
                                "dependence_robust_confidence_intervals",
                                "full_deflated_sharpe_analysis",
                            ],
                            "available_research_tools": [
                                "single_trade_ohlcv_paper_simulator",
                                "chronological_train_validation_oos_split",
                                "experiment_registry",
                                "mhr_scalp_fast_slow_normalized_exit_adapters",
                                "vpvr_normalized_exit_plan_delegating_to_paper_engine",
                                "capability_gated_research_variant_suite",
                                "portfolio_paper_simulator",
                                "walk_forward_and_purged_splits",
                                "iid_bootstrap_expectancy_interval",
                                "approximate_deflated_sharpe_probability",
                                "read_only_subsystem_summary_aggregation",
                                "capability_gated_walk_forward_variant_evaluation",
                                "paper_only_walk_forward_backtest_cli",
                                "trade_outcome_persistence",
                            ],
                            "exit_adapter_limitations": [
                                "legacy exit simulations do not apply fees or execution costs",
                                "VPVR exits are still processed by its native paper engine",
                            ],
                            "paper_simulator_limitations": [
                                "portfolio mode caps concurrent positions but uses independent OHLCV trade simulations",
                                "no partial fills, market impact, funding cash flows, maintenance margin, liquidation, or correlation stress",
                                "unverified contract specifications rejected",
                                "OHLCV ambiguity resolved stop-first",
                            ],
                        }
                    elif path == "/api/variants":
                        result = {
                            "items": [
                                {
                                    "strategy_id": item.strategy_id,
                                    "title": item.title,
                                    "status": item.status,
                                    "required_data": item.required_data,
                                    "minimum_history": item.minimum_history or {},
                                    "limitations": item.limitations,
                                }
                                for item in VARIANT_CATALOG
                            ],
                        }
                    elif path == "/api/sources":
                        result = _read_external_sources(source_paths)
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
                    elif path == "/api/variant-evaluations":
                        result = store.list_variant_evaluations(
                            _integer(params, "limit", 100, MAX_LIMIT),
                            _integer(params, "offset", 0, 10_000_000),
                            params.get("symbol", [None])[0],
                            params.get("variant_id", [None])[0],
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
