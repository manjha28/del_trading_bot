from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time

import pandas as pd

from .dashboard import build_dashboard
from .features import evaluate_symbol
from .market import DeltaResearchData, ResearchDataError
from .store import ResearchStore

logger = logging.getLogger("coin_research")


def _load_or_fetch(data, store, product_id, symbol, timeframe, start, end):
    try:
        return data.fetch_candles(symbol, timeframe, start, end), None
    except ResearchDataError as exc:
        logger.exception("%s %s candle request failed; retaining cached observations", symbol, timeframe)
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"]), str(exc)


def scan_once(
    db_path: str = "data/sleeping_coin_research.sqlite3",
    dashboard_path: str = "data/sleeping_coin_dashboard.html",
    base_url: str = "https://api.india.delta.exchange",
    request_interval: float = 0.1,
    execution_notional: float = 1000.0,
    max_spread_pct: float = 0.5,
    max_impact_pct: float = 0.5,
    train_end: str | None = None,
    validation_end: str | None = None,
) -> dict:
    if execution_notional <= 0 or max_spread_pct <= 0 or max_impact_pct <= 0:
        raise ValueError("execution notional and execution limits must be positive")
    store = ResearchStore(db_path)
    data = DeltaResearchData(base_url=base_url, request_interval=request_interval)
    try:
        products = data.fetch_products()
        tickers = {}
        ticker_error = None
        try:
            tickers = data.fetch_tickers()
        except ResearchDataError as exc:
            ticker_error = str(exc)
            logger.error("Ticker snapshot unavailable: %s", exc)
        now = datetime.now(timezone.utc)
        now_ts = int(now.timestamp())
        store.reconcile_products(products, now.isoformat())
        observations = []

        for product in products:
            product_id = store.product_id(product)
            symbol = product["symbol"]
            if product.get("state") != "live":
                observation = {
                    "symbol": symbol, "as_of": now.isoformat(), "state": "not_live",
                    "state_reasons": [f"catalog state is {product.get('state')}"],
                    "sleeping_score": None, "features": {},
                    "data_quality": {"confidence": 0.0, "issues": ["not a live contract"]},
                    "execution": {"eligible": False, "reasons": ["not a live contract"]},
                    "signals": [],
                }
                store.record_observation(product_id, observation)
                cached_hourly = store.load_candles(product_id, "1h", 60 * 24 + 24)
                store.update_signal_outcomes(
                    product_id, cached_hourly, now=now.isoformat()
                )
                observations.append(observation)
                continue

            first_seen = store.connection.execute(
                "SELECT first_seen FROM products WHERE product_id=?", (product_id,)
            ).fetchone()["first_seen"]
            frames, issues = {}, []
            windows = {"1h": 60 * 24, "1m": 24 * 60, "5m": 7 * 24 * 12}
            for timeframe, window_bars in windows.items():
                last = store.latest_candle_time(product_id, timeframe)
                start = (
                    int(pd.Timestamp(last).timestamp()) - 2 * {"1m": 60, "5m": 300, "1h": 3600}[timeframe]
                    if last else now_ts - window_bars * {"1m": 60, "5m": 300, "1h": 3600}[timeframe]
                )
                fetched, fetch_error = _load_or_fetch(
                    data, store, product_id, symbol, timeframe, start, now_ts
                )
                if fetch_error:
                    issues.append(f"{timeframe} candle refresh failed: {fetch_error}")
                if not fetched.empty:
                    seconds = {"1m": 60, "5m": 300, "1h": 3600}[timeframe]
                    fetched = fetched.loc[
                        fetched.time + pd.to_timedelta(seconds, unit="s") <= pd.Timestamp(now)
                    ]
                    store.save_candles(product_id, timeframe, fetched)
                frames[timeframe] = store.load_candles(
                    product_id, timeframe, {"1h": 60 * 24 + 24, "1m": 24 * 60, "5m": 7 * 24 * 12}[timeframe]
                )
                if frames[timeframe].empty:
                    issues.append(f"{timeframe} OHLCV request unavailable")

            book = trades = None
            try:
                book = data.fetch_book(symbol)
            except ResearchDataError as exc:
                issues.append(f"order book unavailable: {exc}")
            try:
                trades = data.fetch_trades(symbol)
            except ResearchDataError as exc:
                issues.append(f"aggressor-side trade sample unavailable: {exc}")
            previous = store.previous_observation(product_id)
            previous_features = {}
            if previous:
                previous_features = {
                    **(previous.get("features") or {}),
                    "_state": previous.get("_state"),
                    "state_since": previous.get("state_since"),
                    "expansion_anchor_time": (previous.get("awakening") or {}).get(
                        "expansion_anchor_time"
                    ),
                    "failed_high_level": (previous.get("features") or {}).get(
                        "failed_high_level"
                    ),
                }
            historical_features = store.historical_feature_values(
                product_id,
                (
                    "realized_volatility_1m_annualized_pct",
                    "realized_volatility_5m_annualized_pct",
                    "realized_volatility_1h_annualized_pct",
                    "open_interest_change_pct",
                    "funding_rate_change",
                    "bid_depth_change_pct",
                    "ask_depth_change_pct",
                ),
            )
            observation = evaluate_symbol(
                symbol, product, tickers.get(symbol), frames, book, trades,
                previous_features=previous_features, historical_features=historical_features,
                first_seen=first_seen, now=now,
                execution_notional=execution_notional,
                max_spread_pct=max_spread_pct, max_impact_pct=max_impact_pct,
            )
            observation["data_quality"]["issues"].extend(issues)
            if ticker_error:
                observation["data_quality"]["issues"].append(
                    f"ticker endpoint unavailable: {ticker_error}"
                )
            # Sampling error lowers confidence; it never removes the product from discovery.
            if issues or ticker_error:
                observation["data_quality"]["confidence"] *= 0.75
            store.record_observation(product_id, observation)
            store.update_signal_outcomes(product_id, frames["1h"], now=now.isoformat())
            observations.append(observation)
            logger.info("%s: %s, sleeping=%s, execution=%s", symbol, observation["state"],
                        observation.get("sleeping_score"), observation["execution"]["eligible"])

        dashboard = build_dashboard(store, dashboard_path, train_end, validation_end)
        state_counts = {}
        for observation in observations:
            state_counts[observation["state"]] = state_counts.get(observation["state"], 0) + 1
        return {
            "mode": "SHADOW_RESEARCH_ONLY",
            "exchange": "Delta Exchange India",
            "catalog_perpetuals": len(products),
            "observations": len(observations),
            "states": state_counts,
            "database": str(Path(db_path)),
            "dashboard": str(dashboard),
            "event_counts": store.event_counts(),
        }
    finally:
        data.close()
        store.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="coin-research",
        description="Market-wide Delta perpetual discovery and shadow research (no orders).",
    )
    parser.add_argument("--db", default=os.getenv(
        "COIN_RESEARCH_DB", "data/sleeping_coin_research.sqlite3"
    ))
    parser.add_argument("--dashboard", default=os.getenv(
        "COIN_RESEARCH_DASHBOARD", "data/sleeping_coin_dashboard.html"
    ))
    parser.add_argument("--base-url", default=os.getenv(
        "DELTA_BASE_URL", "https://api.india.delta.exchange"
    ))
    parser.add_argument("--request-interval", type=float, default=0.1)
    parser.add_argument("--execution-notional", type=float, default=1000.0,
                        help="notional used only to estimate depth/impact")
    parser.add_argument("--max-spread-pct", type=float, default=0.5)
    parser.add_argument("--max-impact-pct", type=float, default=0.5)
    parser.add_argument("--train-end", help="UTC YYYY-MM-DD inclusive training cutoff")
    parser.add_argument("--validation-end", help="UTC YYYY-MM-DD inclusive validation cutoff")
    parser.add_argument("--loop-seconds", type=float, default=0,
                        help="repeat full-universe scans at this interval; 0 scans once")
    args = parser.parse_args(argv)
    if args.request_interval < 0 or args.loop_seconds < 0:
        parser.error("intervals cannot be negative")
    if bool(args.train_end) != bool(args.validation_end):
        parser.error("--train-end and --validation-end must be provided together")
    if args.train_end and args.train_end >= args.validation_end:
        parser.error("--train-end must precede --validation-end")
    try:
        if args.train_end:
            datetime.strptime(args.train_end, "%Y-%m-%d")
            datetime.strptime(args.validation_end, "%Y-%m-%d")
    except ValueError:
        parser.error("chronological cutoffs must use YYYY-MM-DD")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        while True:
            result = scan_once(
                args.db, args.dashboard, args.base_url, args.request_interval,
                args.execution_notional, args.max_spread_pct, args.max_impact_pct,
                args.train_end, args.validation_end,
            )
            print(json.dumps(result, indent=2), flush=True)
            if not args.loop_seconds:
                break
            time.sleep(args.loop_seconds)
    except (ResearchDataError, OSError, ValueError) as exc:
        logger.error("Research scan failed: %s", exc)
        return 2
    except KeyboardInterrupt:
        logger.info("Research loop stopped; no live orders were submitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
