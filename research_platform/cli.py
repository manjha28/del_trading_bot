from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import time

from .collector import ResearchCollector
from .market_data import DeltaResearchAdapter, VPVRDataAdapter
from .server import serve
from .store import ResearchDataStore


logger = logging.getLogger("research_platform.cli")


def _collector(args) -> tuple[ResearchCollector, object, ResearchDataStore]:
    if args.provider == "coin-research":
        from coin_research.market import DeltaResearchData

        client = DeltaResearchData()
        provider = DeltaResearchAdapter(client)
    else:
        from vpvr.config import VPVRConfig
        from vpvr.data import DeltaMarketData

        client = DeltaMarketData(config=VPVRConfig.from_env())
        provider = VPVRDataAdapter(client)
    store = ResearchDataStore(args.db)
    return ResearchCollector(provider, store), client, store


def _run_scan(collector: ResearchCollector, args) -> dict:
    instruments = collector.reconcile_universe()
    tickers = collector.provider.fetch_tickers() if collector.provider.capabilities.ticker_snapshots else []
    collector.store.save_tickers(tickers)
    ticker_by_id = {ticker.product_id: ticker for ticker in tickers}
    end = int(time.time())
    start = end - args.lookback_hours * 3600
    results = []
    for instrument in instruments:
        try:
            results.append(collector.capture(
                instrument, args.timeframe, start, end,
                ticker_snapshots=[ticker_by_id[instrument.product_id]]
                if instrument.product_id in ticker_by_id else [],
            ))
        except (ValueError, RuntimeError) as exc:
            collector.record_failure(instrument, f"collection:{args.timeframe}", exc)
            logger.exception("Collection failed for %s (%s)", instrument.symbol, instrument.product_id)
            results.append({
                "product_id": instrument.product_id,
                "symbol": instrument.symbol,
                "status": "error",
                "reason": f"{type(exc).__name__}: {exc}",
            })
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "provider": collector.provider.capabilities.provider,
        "catalog_count": len(instruments),
        "ticker_count": len(tickers),
        "timeframe": args.timeframe,
        "results": results,
        "failures": sum(result.get("status") == "error" for result in results),
        "coverage": collector.store.coverage_summary(),
    }


def _collect(args) -> int:
    collector, client, store = _collector(args)
    try:
        while True:
            result = _run_scan(collector, args)
            print(json.dumps(result, default=str, allow_nan=False), flush=True)
            if args.loop_seconds == 0:
                return 1 if result["failures"] else 0
            time.sleep(args.loop_seconds)
    except KeyboardInterrupt:
        logger.info("Collection stopped")
        return 0
    finally:
        store.close()
        close = getattr(client, "close", None)
        if callable(close):
            close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="research-platform",
        description="Collect and inspect public Delta market data for offline research.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="reconcile universe and collect public observations")
    collect.add_argument("--db", default="data/unified_research.sqlite3")
    collect.add_argument("--provider", choices=("coin-research", "vpvr"), default="coin-research")
    collect.add_argument("--timeframe", default=None)
    collect.add_argument("--lookback-hours", type=int, default=24)
    collect.add_argument("--loop-seconds", type=int, default=0,
                         help="repeat scans at this interval; zero performs one scan")

    web = commands.add_parser("serve", help="run the read-only local dashboard")
    web.add_argument("--db", default="data/unified_research.sqlite3")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8766)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if args.command == "serve":
        try:
            serve(args.db, args.host, args.port)
        except ValueError as exc:
            parser.error(str(exc))
        except KeyboardInterrupt:
            logger.info("Dashboard stopped")
        return 0

    if args.lookback_hours < 1:
        parser.error("--lookback-hours must be positive")
    if args.loop_seconds < 0:
        parser.error("--loop-seconds cannot be negative")
    if args.timeframe is None:
        args.timeframe = "1h" if args.provider == "coin-research" else "15m"
    try:
        return _collect(args)
    except (ValueError, RuntimeError) as exc:
        logger.error("Collection failed: %s", exc)
        return 1
