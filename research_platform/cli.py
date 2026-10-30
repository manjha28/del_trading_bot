from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
import time
import uuid

import pandas as pd

from .collector import ResearchCollector
from .market_data import DeltaResearchAdapter, VPVRDataAdapter
from .server import serve
from .store import ResearchDataStore
from .strategies import StrategyContext
from .variants import ResearchVariantContext, ResearchVariantSuite, VARIANT_CATALOG
from .evaluation import evaluate_walk_forward_variants
from .portfolio import PortfolioSimulationConfig
from .simulation import PaperSimulationConfig
from .validation import walk_forward_splits


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
    capture = None
    if args.websocket_capture:
        from .live_capture import DurableMarketCapture

        capture = DurableMarketCapture(store)
    try:
        while True:
            result = _run_scan(collector, args)
            if capture is not None:
                capture.sync_universe(store.list_instruments())
                capture.drain()
            print(json.dumps(result, default=str, allow_nan=False), flush=True)
            if args.loop_seconds == 0:
                return 1 if result["failures"] else 0
            if capture is None:
                time.sleep(args.loop_seconds)
            else:
                deadline = time.monotonic() + args.loop_seconds
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(1.0, remaining))
                    capture.drain()
    except KeyboardInterrupt:
        logger.info("Collection stopped")
        return 0
    finally:
        if capture is not None:
            capture.close()
        store.close()
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _evaluate(args) -> int:
    suite = ResearchVariantSuite()
    selected = set(args.variants or [item.strategy_id for item in VARIANT_CATALOG])
    unknown = selected.difference(item.strategy_id for item in VARIANT_CATALOG)
    if unknown:
        raise ValueError(f"unknown research variants: {', '.join(sorted(unknown))}")
    as_of = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    counts = {"signal": 0, "no_signal": 0, "insufficient_data": 0, "unavailable": 0, "error": 0}
    with ResearchDataStore(args.db) as store:
        for instrument in store.list_instruments():
            frames = {}
            for timeframe in ("1m", "5m", "1h", "4h"):
                candles = store.load_candles(instrument.product_id, timeframe)
                complete = [candle for candle in candles if candle.is_complete]
                if complete:
                    frame = pd.DataFrame([{
                        "time": item.start_time,
                        "open": item.open, "high": item.high, "low": item.low,
                        "close": item.close, "volume": item.volume,
                    } for item in complete])
                    frames[timeframe] = frame
            context = StrategyContext(
                instrument, as_of, frames,
                provenance={key: "ResearchDataStore" for key in frames},
            )
            for variant in VARIANT_CATALOG:
                if variant.strategy_id not in selected:
                    continue
                evaluation_time = as_of
                signal_record = None
                try:
                    if variant.status == "unavailable":
                        status = "unavailable"
                        reason = "; ".join(variant.limitations)
                    elif any("candles" not in requirement for requirement in variant.required_data):
                        status = "insufficient_data"
                        reason = (
                            "required synchronized historical trades/book/funding/OI "
                            "coverage is not established by the canonical store"
                        )
                    else:
                        missing = []
                        for requirement in variant.required_data:
                            timeframe = requirement.split()[0]
                            needed = (variant.minimum_history or {}).get(timeframe, 1)
                            if len(frames.get(timeframe, ())) < needed:
                                missing.append(f"{timeframe} ({needed} required)")
                        if missing:
                            status = "insufficient_data"
                            reason = f"missing/short completed candle history: {', '.join(missing)}"
                        else:
                            signals = suite.evaluate(
                                variant.strategy_id, ResearchVariantContext(context)
                            )
                            if signals:
                                signal_record = asdict(signals[0])
                                status = "signal"
                                reason = "strategy conditions satisfied"
                            else:
                                status = "no_signal"
                                reason = "no strategy condition satisfied on available completed data"
                    store.save_variant_evaluation(
                        variant.strategy_id, instrument.product_id, instrument.symbol,
                        evaluation_time, status, reason, signal_record,
                    )
                except (ValueError, RuntimeError) as exc:
                    logger.exception(
                        "Variant evaluation failed for %s on %s",
                        variant.strategy_id, instrument.symbol,
                    )
                    status = "error"
                    reason = f"{type(exc).__name__}: {exc}"
                    store.save_variant_evaluation(
                        variant.strategy_id, instrument.product_id, instrument.symbol,
                        evaluation_time, status, reason,
                    )
                counts[status] += 1
    print(json.dumps({
        "evaluated_at": as_of.isoformat(),
        "selected_variants": sorted(selected),
        "status_counts": counts,
        "coverage_warning": (
            "Only persisted completed OHLCV is evaluated here. Trade-flow and "
            "order-book variants require explicit complete/synchronized coverage "
            "and are not inferred from candle data."
        ),
    }, allow_nan=False), flush=True)
    return 0


def _backtest(args) -> int:
    if args.train_days <= 0 or args.validation_days <= 0 or args.test_days <= 0 or args.step_days <= 0:
        raise ValueError("walk-forward windows and step must be positive")
    if args.embargo_hours < 0:
        raise ValueError("embargo hours cannot be negative")
    if args.initial_equity <= 0:
        raise ValueError("initial equity must be positive")
    with ResearchDataStore(args.db) as store:
        instruments = store.list_instruments()
        if not instruments:
            raise ValueError("no instruments are stored; collect market data first")
        frames_by_product = {}
        digest = hashlib.sha256()
        clock = set()
        coverage = {}
        for instrument in instruments:
            digest.update(json.dumps({
                "product_id": instrument.product_id,
                "symbol": instrument.symbol,
                "contract_type": instrument.contract_type,
                "state": instrument.state,
                "metadata": dict(instrument.metadata),
                "contract_specs": asdict(instrument.contract_specs)
                if instrument.contract_specs else None,
            }, sort_keys=True, default=str).encode("utf-8") + b"\n")
            frames_by_product[instrument.product_id] = {}
            coverage[instrument.product_id] = {}
            for timeframe in ("1m", "5m", "1h", "4h"):
                candles = [
                    item for item in store.load_candles(instrument.product_id, timeframe)
                    if item.is_complete
                ]
                if not candles:
                    coverage[instrument.product_id][timeframe] = 0
                    continue
                by_time = {}
                for candle in candles:
                    values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
                    previous = by_time.get(candle.start_time)
                    if previous is not None and previous != values:
                        raise ValueError(
                            f"conflicting {timeframe} candles for {instrument.symbol} at "
                            f"{candle.start_time.isoformat()}"
                        )
                    by_time[candle.start_time] = values
                rows = [
                    {"time": stamp, "open": values[0], "high": values[1],
                     "low": values[2], "close": values[3], "volume": values[4]}
                    for stamp, values in sorted(by_time.items())
                ]
                frame = pd.DataFrame(rows)
                frames_by_product[instrument.product_id][timeframe] = frame
                coverage[instrument.product_id][timeframe] = len(frame)
                for row in rows:
                    digest.update(
                        f"{instrument.product_id}|{timeframe}|{row['time'].isoformat()}|"
                        f"{row['open']}|{row['high']}|{row['low']}|{row['close']}|{row['volume']}\n"
                        .encode("utf-8")
                    )
                if timeframe == "1m":
                    clock.update(frame.time.tolist())
        if not clock:
            raise ValueError("walk-forward simulation requires stored completed 1m candles")
        clock_frame = pd.DataFrame({"time": sorted(clock)})
        folds = walk_forward_splits(
            clock_frame,
            train_window=pd.Timedelta(days=args.train_days).to_pytimedelta(),
            validation_window=pd.Timedelta(days=args.validation_days).to_pytimedelta(),
            test_window=pd.Timedelta(days=args.test_days).to_pytimedelta(),
            step=pd.Timedelta(days=args.step_days).to_pytimedelta(),
            embargo=pd.Timedelta(hours=args.embargo_hours).to_pytimedelta(),
        )
        if not folds:
            raise ValueError(
                "insufficient 1m history for the requested train/validation/test windows"
            )
        report = evaluate_walk_forward_variants(
            instruments,
            frames_by_product,
            folds,
            initial_equity=args.initial_equity,
            portfolio_config=PortfolioSimulationConfig(
                max_open_positions=args.max_open_positions,
                max_portfolio_risk_pct=args.max_portfolio_risk_pct,
                max_gross_leverage=args.max_gross_leverage,
                max_pairwise_correlation=args.max_pairwise_correlation,
                correlation_lookback_minutes=args.correlation_lookback_minutes,
                minimum_correlation_observations=args.minimum_correlation_observations,
                trade=PaperSimulationConfig(
                    risk_per_trade_pct=args.trade_risk_pct,
                    leverage=max(1.0, args.max_gross_leverage),
                    fee_rate=args.fee_rate,
                    spread_bps=args.spread_bps,
                    slippage_bps=args.slippage_bps,
                ),
            ),
        )
        dataset_version = f"sha256:{digest.hexdigest()}"
        run_tag = uuid.uuid4().hex[:12]
        for variant_id, variant_report in report["variants"].items():
            for fold in variant_report["folds"]:
                outcomes = fold.pop("_outcomes", [])
                experiment_id = f"wf-{run_tag}-{variant_id}-{fold['fold']:03d}"
                split = {
                    "train_end": fold["train_end"],
                    "validation_end": fold["validation_end"],
                    "out_of_sample_start": fold["out_of_sample_start"],
                    "out_of_sample_end": fold["out_of_sample_end"],
                    "fold": fold["fold"],
                }
                metrics = {
                    "portfolio": fold["portfolio"],
                    "summary": fold["metrics"],
                    "selection_bias_diagnostic": fold["selection_bias_diagnostic"],
                    "coverage": {
                        "universe_count": variant_report["universe_count"],
                        "symbols_with_signal": variant_report["symbols_with_signal"],
                        "candidate_signal_count": fold["candidate_signal_count"],
                    },
                    "per_symbol": fold["per_symbol"],
                    "limitations": report["limitations"],
                }
                store.save_experiment(
                    experiment_id, variant_id, "research-variant-v1",
                    dataset_version, split,
                    {
                        "mode": "paper_only_walk_forward",
                        "initial_equity": args.initial_equity,
                        "max_open_positions": args.max_open_positions,
                        "max_portfolio_risk_pct": args.max_portfolio_risk_pct,
                        "max_gross_leverage": args.max_gross_leverage,
                        "max_pairwise_correlation": args.max_pairwise_correlation,
                        "correlation_lookback_minutes": args.correlation_lookback_minutes,
                        "minimum_correlation_observations": args.minimum_correlation_observations,
                        "trade_risk_pct": args.trade_risk_pct,
                        "trade_leverage": max(1.0, args.max_gross_leverage),
                        "fee_rate": args.fee_rate,
                        "spread_bps": args.spread_bps,
                        "slippage_bps": args.slippage_bps,
                        "source_coverage": coverage,
                    },
                    metrics,
                    status="completed",
                )
                for outcome in outcomes:
                    store.save_paper_simulation(outcome, experiment_id)
        for fold in report["selector"]["folds"]:
            outcomes = fold.pop("_outcomes", [])
            experiment_id = f"wf-{run_tag}-validated-selector-{fold['fold']:03d}"
            store.save_experiment(
                experiment_id,
                "validated_strategy_selector",
                "validated-selector-v1",
                dataset_version,
                {
                    "train_end": fold["train_end"],
                    "validation_end": fold["validation_end"],
                    "out_of_sample_start": fold["out_of_sample_start"],
                    "out_of_sample_end": fold["out_of_sample_end"],
                    "fold": fold["fold"],
                },
                {
                    "mode": "paper_only_walk_forward_selector",
                    "initial_equity": args.initial_equity,
                    "selector_policy": report["selector"]["policy"],
                    "portfolio": {
                        "max_open_positions": args.max_open_positions,
                        "max_portfolio_risk_pct": args.max_portfolio_risk_pct,
                        "max_gross_leverage": args.max_gross_leverage,
                        "max_pairwise_correlation": args.max_pairwise_correlation,
                        "correlation_lookback_minutes": args.correlation_lookback_minutes,
                        "minimum_correlation_observations": args.minimum_correlation_observations,
                        "trade_risk_pct": args.trade_risk_pct,
                        "trade_leverage": max(1.0, args.max_gross_leverage),
                        "fee_rate": args.fee_rate,
                        "spread_bps": args.spread_bps,
                        "slippage_bps": args.slippage_bps,
                    },
                },
                {
                    "selection": fold["selection"],
                    "portfolio": fold["portfolio"],
                    "summary": fold["metrics"],
                    "limitations": report["limitations"],
                },
                status="completed",
            )
            for outcome in outcomes:
                store.save_paper_simulation(outcome, experiment_id)
        report["dataset_version"] = dataset_version
        report["fold_count"] = len(folds)
        report["instrument_count"] = len(instruments)
        report["candle_coverage"] = coverage
    print(json.dumps(report, default=str, allow_nan=False), flush=True)
    return 0


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
    collect.add_argument(
        "--websocket-capture", action="store_true",
        help="persist public WebSocket events; requires --loop-seconds > 0",
    )

    web = commands.add_parser("serve", help="run the read-only local dashboard")
    web.add_argument("--db", default="data/unified_research.sqlite3")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8766)

    evaluate = commands.add_parser(
        "evaluate", help="evaluate supported research variants on stored completed data"
    )
    evaluate.add_argument("--db", default="data/unified_research.sqlite3")
    evaluate.add_argument("--variants", nargs="*", default=None)

    backtest = commands.add_parser(
        "backtest", help="run capability-gated variant walk-forward paper evaluation"
    )
    backtest.add_argument("--db", default="data/unified_research.sqlite3")
    backtest.add_argument("--train-days", type=int, default=30)
    backtest.add_argument("--validation-days", type=int, default=7)
    backtest.add_argument("--test-days", type=int, default=7)
    backtest.add_argument("--step-days", type=int, default=7)
    backtest.add_argument("--embargo-hours", type=float, default=0.0)
    backtest.add_argument("--initial-equity", type=float, default=10_000.0)
    backtest.add_argument("--max-open-positions", type=int, default=3)
    backtest.add_argument("--max-portfolio-risk-pct", type=float, default=0.015)
    backtest.add_argument("--max-gross-leverage", type=float, default=2.0)
    backtest.add_argument("--max-pairwise-correlation", type=float, default=0.8)
    backtest.add_argument("--correlation-lookback-minutes", type=int, default=60)
    backtest.add_argument("--minimum-correlation-observations", type=int, default=30)
    backtest.add_argument("--trade-risk-pct", type=float, default=0.005)
    backtest.add_argument("--fee-rate", type=float, default=0.0005)
    backtest.add_argument("--spread-bps", type=float, default=5.0)
    backtest.add_argument("--slippage-bps", type=float, default=2.0)

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
    if args.command == "evaluate":
        try:
            return _evaluate(args)
        except (ValueError, RuntimeError) as exc:
            logger.error("Variant evaluation failed: %s", exc)
            return 1
    if args.command == "backtest":
        try:
            return _backtest(args)
        except (ValueError, RuntimeError) as exc:
            logger.error("Walk-forward backtest failed: %s", exc)
            return 1

    if args.lookback_hours < 1:
        parser.error("--lookback-hours must be positive")
    if args.loop_seconds < 0:
        parser.error("--loop-seconds cannot be negative")
    if args.websocket_capture and args.loop_seconds == 0:
        parser.error("--websocket-capture requires a positive --loop-seconds")
    if args.timeframe is None:
        args.timeframe = "1h" if args.provider == "coin-research" else "15m"
    try:
        return _collect(args)
    except (ValueError, RuntimeError) as exc:
        logger.error("Collection failed: %s", exc)
        return 1
