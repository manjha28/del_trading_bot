from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

import pandas as pd

from .market_data import Instrument, TIMEFRAME_SECONDS
from .portfolio import PortfolioPaperSimulator, PortfolioSimulationConfig
from .selection import (
    ValidatedStrategySelector,
    build_validation_evidence,
    classify_market_regime,
    signal_key,
)
from .strategies import StrategyContext
from .validation import ChronologicalPeriods, deflated_sharpe_ratio, summarize_paper_results
from .variants import ResearchVariantSuite, ResearchVariantContext


def evaluate_walk_forward_variants(
    instruments: Sequence[Instrument],
    frames_by_product: Mapping[str, Mapping[str, pd.DataFrame]],
    folds: Sequence[ChronologicalPeriods[pd.DataFrame]],
    *,
    initial_equity: float = 10_000.0,
    portfolio_config: PortfolioSimulationConfig | None = None,
    as_of: datetime | None = None,
) -> dict:
    """Evaluate every supported variant over every product and chronological OOS fold."""
    if not folds:
        raise ValueError("at least one walk-forward fold is required")
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")
    suite = ResearchVariantSuite()
    instruments_by_id = {item.product_id: item for item in instruments}
    signals: dict[str, list] = {
        variant.strategy_id: [] for variant in suite.catalog
    }
    data_status: dict[str, dict[str, str]] = {}
    evaluation_time = as_of or datetime.now(timezone.utc)
    if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    evaluation_time = evaluation_time.astimezone(timezone.utc)

    for instrument in instruments:
        supplied_frames = frames_by_product.get(instrument.product_id, {})
        data_status[instrument.product_id] = {}
        for variant in suite.catalog:
            data_status[instrument.product_id][variant.strategy_id] = "no_data"
        for timeframe, frame in supplied_frames.items():
            if timeframe not in TIMEFRAME_SECONDS:
                raise ValueError(f"unsupported timeframe for {instrument.symbol}: {timeframe}")
            if frame.empty:
                continue
            required = {"time", "open", "high", "low", "close", "volume"}
            if not required.issubset(frame.columns):
                raise ValueError(f"{instrument.symbol} {timeframe} frame missing OHLCV columns")
            times = pd.to_datetime(frame.time, errors="raise")
            if times.dt.tz is None or times.isna().any():
                raise ValueError(f"{instrument.symbol} {timeframe} timestamps must be timezone-aware")
            if not times.is_monotonic_increasing or times.duplicated().any():
                raise ValueError(f"{instrument.symbol} {timeframe} candles are unordered or duplicated")
        source_frames = {
            timeframe: frame for timeframe, frame in supplied_frames.items()
            if not frame.empty
        }
        times_by_timeframe = {
            tf: pd.to_datetime(tf_frame.time, utc=True)
            for tf, tf_frame in source_frames.items()
        }

        for variant in suite.catalog:
            if variant.status == "unavailable":
                data_status[instrument.product_id][variant.strategy_id] = "unavailable"
                continue
            if any("candles" not in requirement for requirement in variant.required_data):
                data_status[instrument.product_id][variant.strategy_id] = "insufficient_data"
                continue
            timeframe = variant.required_data[0].split()[0]
            frame = source_frames.get(timeframe)
            minimum = (variant.minimum_history or {}).get(timeframe, 1)
            if frame is None or len(frame) < minimum:
                data_status[instrument.product_id][variant.strategy_id] = "insufficient_data"
                continue
            data_status[instrument.product_id][variant.strategy_id] = "observed_no_signal"
            for index in range(minimum - 1, len(frame)):
                decision_time = (
                    pd.Timestamp(frame.time.iloc[index])
                    + pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
                ).to_pydatetime()
                if evaluation_time.tzinfo is not None and decision_time > evaluation_time:
                    continue
                available_frames = {}
                for tf, tf_frame in source_frames.items():
                    cutoff = pd.Timestamp(decision_time) - pd.Timedelta(
                        seconds=TIMEFRAME_SECONDS[tf]
                    )
                    end_index = int(times_by_timeframe[tf].searchsorted(cutoff, side="right"))
                    if end_index:
                        available_frames[tf] = tf_frame.iloc[max(0, end_index - 256):end_index]
                context = StrategyContext(
                    instrument=instrument,
                    as_of=decision_time,
                    frames=available_frames,
                    provenance={key: "walk-forward-dataset" for key in available_frames},
                )
                found = suite.evaluate(
                    variant.strategy_id, ResearchVariantContext(context)
                )
                if found:
                    data_status[instrument.product_id][variant.strategy_id] = "signal"
                    # Execute each strategy on a common 1m feed after signal time.
                    signals[variant.strategy_id].extend(
                        replace(signal, entry_reference="next_1m_open")
                        for signal in found
                    )

    all_product_ids = set(instruments_by_id)
    if not all_product_ids:
        raise ValueError("walk-forward evaluation requires the discovered instrument universe")
    outcomes = {}
    simulator = PortfolioPaperSimulator(portfolio_config)
    all_candles_1m = {
        product_id: _canonical_candles(frames.get("1m"), instruments_by_id[product_id])
        for product_id, frames in frames_by_product.items()
        if product_id in all_product_ids and frames.get("1m") is not None
    }
    for variant_id, variant_signals in signals.items():
        fold_results = []
        oos_signal_products: set[str] = set()
        for fold_index, fold in enumerate(folds):
            exit_boundary = fold.out_of_sample_end + timedelta(seconds=60)
            test_start = fold.validation_end + fold.embargo
            candidates = [
                signal for signal in variant_signals
                if test_start <= signal.decision_time <= fold.out_of_sample_end
            ]
            oos_signal_products.update(signal.product_id for signal in candidates)
            candles_1m = {
                product_id: [
                    candle for candle in candles
                    if candle.close_time <= exit_boundary
                ]
                for product_id, candles in all_candles_1m.items()
            }
            portfolio = simulator.simulate(
                candidates,
                instruments_by_id,
                candles_1m,
                initial_equity,
                max_exit_time=exit_boundary,
            )
            per_symbol = {}
            for instrument in instruments:
                symbol_outcomes = [
                    item for item in portfolio.outcomes if item.product_id == instrument.product_id
                ]
                candidate_count = sum(
                    signal.product_id == instrument.product_id for signal in candidates
                )
                coverage_state = _fold_coverage_state(
                    instrument.product_id,
                    variant_id,
                    test_start,
                    fold.out_of_sample_end,
                    data_status,
                    frames_by_product,
                    suite,
                )
                if coverage_state == "observed" and candidate_count:
                    coverage_state = "signal"
                elif coverage_state == "observed":
                    coverage_state = "observed_no_signal"
                per_symbol[instrument.product_id] = {
                    "symbol": instrument.symbol,
                    "candidate_signal_count": candidate_count,
                    "simulated_count": sum(item.status == "simulated" for item in symbol_outcomes),
                    "rejected_count": sum(item.status != "simulated" for item in symbol_outcomes),
                    "net_pnl": sum(
                        item.net_pnl for item in symbol_outcomes if item.status == "simulated"
                    ) if any(item.status == "simulated" for item in symbol_outcomes) else None,
                    "coverage_state": coverage_state,
                }
            fold_results.append({
                "fold": fold_index,
                "train_end": fold.train_end.isoformat(),
                "validation_end": fold.validation_end.isoformat(),
                "out_of_sample_end": fold.out_of_sample_end.isoformat(),
                "out_of_sample_start": test_start.isoformat(),
                "candidate_signal_count": len(candidates),
                "portfolio": {
                    "status": portfolio.status,
                    "initial_equity": portfolio.initial_equity,
                    "final_equity": portfolio.final_equity,
                    "net_pnl": portfolio.net_pnl,
                    "max_drawdown_pct": portfolio.realized_max_drawdown_pct,
                    "accepted_count": portfolio.accepted_count,
                    "rejected_count": portfolio.rejected_count,
                    "max_concurrent_positions": portfolio.max_concurrent_positions,
                },
                "metrics": summarize_paper_results(portfolio.outcomes),
                "selection_bias_diagnostic": {
                    "approximate_deflated_sharpe_probability": deflated_sharpe_ratio(
                        [
                            item.net_pnl / item.equity_before
                            for item in portfolio.outcomes
                            if item.status == "simulated" and item.equity_before > 0
                        ],
                        trial_count=len(suite.catalog),
                    ),
                    "trial_count": len(suite.catalog),
                    "interpretation": (
                        "Approximate probability from per-trade equity returns and "
                        "the declared variant family; not a substitute for independent replication."
                    ),
                },
                "per_symbol": per_symbol,
                "_outcomes": [asdict(item) for item in portfolio.outcomes],
            })
        outcomes[variant_id] = {
            "status": "evaluated",
            "universe_count": len(instruments),
            "symbols_with_signal": len(oos_signal_products),
            "symbols_without_signal_or_data": len(instruments) - len(oos_signal_products),
            "folds": fold_results,
        }

    selector = ValidatedStrategySelector()
    selection_folds = []
    for fold_index, fold in enumerate(folds):
        validation_start = fold.train_end + fold.embargo
        validation_candidates = [
            signal
            for variant_signals in signals.values()
            for signal in variant_signals
            if validation_start <= signal.decision_time <= fold.validation_end
        ]
        validation_regimes = {
            signal_key(signal): _regime_at(
                signal, frames_by_product.get(signal.product_id, {})
            )
            for signal in validation_candidates
        }
        validation_outcomes = []
        validation_exit_boundary = fold.validation_end + timedelta(seconds=60)
        validation_candles = {
            product_id: [
                candle for candle in candles
                if candle.close_time <= validation_exit_boundary
            ]
            for product_id, candles in all_candles_1m.items()
        }
        for variant_id, variant_signals in signals.items():
            candidates = [
                signal for signal in variant_signals
                if validation_start <= signal.decision_time <= fold.validation_end
            ]
            if candidates:
                validation_portfolio = simulator.simulate(
                    candidates,
                    instruments_by_id,
                    validation_candles,
                    initial_equity,
                    max_exit_time=validation_exit_boundary,
                )
                validation_outcomes.extend(validation_portfolio.outcomes)
        evidence = build_validation_evidence(
            validation_outcomes, validation_regimes, fold.validation_end
        )
        test_start = fold.validation_end + fold.embargo
        oos_candidates = [
            signal
            for variant_signals in signals.values()
            for signal in variant_signals
            if test_start <= signal.decision_time <= fold.out_of_sample_end
        ]
        oos_regimes = {
            signal_key(signal): _regime_at(
                signal, frames_by_product.get(signal.product_id, {})
            )
            for signal in oos_candidates
        }
        selection = selector.select(oos_candidates, evidence, oos_regimes)
        oos_exit_boundary = fold.out_of_sample_end + timedelta(seconds=60)
        oos_candles = {
            product_id: [
                candle for candle in candles
                if candle.close_time <= oos_exit_boundary
            ]
            for product_id, candles in all_candles_1m.items()
        }
        selected_portfolio = simulator.simulate(
            selection.selected,
            instruments_by_id,
            oos_candles,
            initial_equity,
            max_exit_time=oos_exit_boundary,
        )
        selection_folds.append({
            "fold": fold_index,
            "train_end": fold.train_end.isoformat(),
            "validation_end": fold.validation_end.isoformat(),
            "out_of_sample_start": test_start.isoformat(),
            "out_of_sample_end": fold.out_of_sample_end.isoformat(),
            "validation_candidate_count": len(validation_candidates),
            "validation_simulated_count": sum(
                item.status == "simulated" for item in validation_outcomes
            ),
            "selection": selection.as_record(),
            "portfolio": {
                "status": selected_portfolio.status,
                "initial_equity": selected_portfolio.initial_equity,
                "final_equity": selected_portfolio.final_equity,
                "net_pnl": selected_portfolio.net_pnl,
                "max_drawdown_pct": selected_portfolio.realized_max_drawdown_pct,
                "accepted_count": selected_portfolio.accepted_count,
                "rejected_count": selected_portfolio.rejected_count,
                "max_concurrent_positions": selected_portfolio.max_concurrent_positions,
                "constraints": selected_portfolio.constraints,
            },
            "metrics": summarize_paper_results(selected_portfolio.outcomes),
            "_outcomes": [asdict(item) for item in selected_portfolio.outcomes],
        })
    return {
        "status": "research_only",
        "variants": outcomes,
        "selector": {
            "status": "research_only",
            "policy": {
                "minimum_validation_trades": selector.policy.minimum_validation_trades,
                "minimum_net_expectancy": selector.policy.minimum_net_expectancy,
                "overlap_window_seconds": selector.policy.overlap_window.total_seconds(),
            },
            "folds": selection_folds,
        },
        "coverage": data_status,
        "limitations": [
            "Standalone fold metrics are OOS; the combined selector is fitted only from simulated validation-period net outcomes and must not be tuned using its OOS folds.",
            "OHLCV fills use the constrained paper simulator; depth/impact, funding, liquidation, and correlation stress are not modeled.",
            "Strategy selection uses only simulated validation outcomes grouped by per-symbol OHLCV regime; minimum samples and positive net expectancy are required, otherwise it selects no trade.",
            "Portfolio risk is bounded by open slots, notional, stop risk, same-product overlap, and a recent aligned-return correlation gate; the gate is not a full correlation stress model.",
            "Unavailable or insufficient-data symbols remain in the coverage matrix and are not counted as losing trades.",
        ],
    }


def _regime_at(
    signal,
    frames: Mapping[str, pd.DataFrame],
) -> str | None:
    hourly = frames.get("1h")
    if hourly is None or hourly.empty:
        return None
    return classify_market_regime(hourly, signal.decision_time)


def _fold_coverage_state(
    product_id: str,
    variant_id: str,
    test_start: datetime,
    test_end: datetime,
    data_status: Mapping[str, Mapping[str, str]],
    frames_by_product: Mapping[str, Mapping[str, pd.DataFrame]],
    suite: ResearchVariantSuite,
) -> str:
    status = data_status[product_id][variant_id]
    if status in ("unavailable", "no_data"):
        return status
    variant = next(item for item in suite.catalog if item.strategy_id == variant_id)
    if any("candles" not in requirement for requirement in variant.required_data):
        return "insufficient_data"
    timeframe = variant.required_data[0].split()[0]
    frame = frames_by_product.get(product_id, {}).get(timeframe)
    minimum = (variant.minimum_history or {}).get(timeframe, 1)
    if frame is None or frame.empty:
        return "insufficient_data"
    candle_ends = (
        pd.to_datetime(frame.time, utc=True)
        + pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    )
    fold_start = pd.Timestamp(test_start).tz_convert("UTC")
    fold_end = pd.Timestamp(test_end).tz_convert("UTC")
    available = candle_ends <= fold_end
    in_fold = (candle_ends >= fold_start) & available
    if int(available.sum()) < minimum or not in_fold.any():
        return "insufficient_data"
    return "observed"


def _canonical_candles(frame: pd.DataFrame, instrument: Instrument) -> list:
    from .market_data import Candle

    if frame is None or frame.empty:
        return []
    return [
        Candle(
            product_id=instrument.product_id,
            symbol=instrument.symbol,
            timeframe="1m",
            start_time=pd.Timestamp(row.time).to_pydatetime(),
            open=float(row.open), high=float(row.high), low=float(row.low),
            close=float(row.close), volume=float(row.volume),
            source="walk-forward-dataset", is_complete=True,
        )
        for row in frame.itertuples(index=False)
    ]
