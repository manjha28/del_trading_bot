from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Callable, Mapping

import pandas as pd

from .market_data import (
    OrderBookSnapshot,
    TradePrint,
)
from .strategies import StrategyContext, StrategySignal, _decision_time


@dataclass(frozen=True)
class ResearchVariant:
    strategy_id: str
    title: str
    status: str
    required_data: tuple[str, ...]
    limitations: tuple[str, ...]
    minimum_history: Mapping[str, int] | None = None


@dataclass(frozen=True)
class ResearchVariantContext:
    strategy: StrategyContext
    trades: tuple[TradePrint, ...] = ()
    order_books: tuple[OrderBookSnapshot, ...] = ()
    trade_history_complete: bool = False


VARIANT_CATALOG = (
    ResearchVariant(
        "parabolic_auction_failure_short", "Parabolic Auction Failure Short",
        "available_ohlcv", ("1h candles",),
        ("Uses per-symbol return/range outliers and OHLCV reversal structure; not order-book auction evidence.",),
        {"1h": 48},
    ),
    ResearchVariant(
        "failed_high_vwap_retest", "Failed High + VWAP Retest",
        "available_ohlcv", ("1h candles",),
        ("VWAP is volume-weighted candle typical price anchored to the detected swing high; no trade-tape VWAP.",),
        {"1h": 20},
    ),
    ResearchVariant(
        "absorption_price_rejection", "Absorption + Price Rejection",
        "unavailable", ("timestamped trades", "synchronized order-book history"),
        ("Historical order-book deltas and synchronized depth history are not currently collected; OHLCV cannot establish absorption.",),
    ),
    ResearchVariant(
        "liquidity_sweep_reclaim", "Liquidity Sweep + Reclaim",
        "available_price_proxy", ("5m candles",),
        ("Candle-extreme sweep proxy only; does not assert resting-order liquidity was swept.",),
        {"5m": 22},
    ),
    ResearchVariant(
        "order_book_imbalance_microprice", "Order Book Imbalance + Microprice",
        "available_if_valid_snapshots", ("synchronized order-book snapshots",),
        ("Requires at least two fresh synchronized snapshots; current adapters generally provide only a snapshot, so most histories are unavailable.",),
        {"1m": 15},
    ),
    ResearchVariant(
        "trade_flow_momentum", "Trade Flow Momentum",
        "available_if_complete_trade_tape", ("timestamped trades with explicit aggressor side",),
        ("Requires complete, fresh trade coverage; unknown aggressor-side prints are not inferred.",),
        {"1m": 15},
    ),
    ResearchVariant(
        "liquidation_cascade", "Liquidation Cascade",
        "unavailable", ("historical liquidation feed",),
        ("No historical liquidation feed is available; price/volume spikes are not treated as liquidations.",),
    ),
    ResearchVariant(
        "perpetual_basis_funding", "Perpetual Basis/Funding",
        "unavailable", ("historical funding", "mark/index history"),
        ("The canonical store currently lacks historical mark/index and reliable funding coverage for every instrument.",),
    ),
    ResearchVariant(
        "multi_day_trend_continuation", "Multi-day Trend Continuation",
        "available_ohlcv", ("4h candles",),
        ("OHLCV trend confirmation only; no derivative positioning input.",),
        {"4h": 26},
    ),
    ResearchVariant(
        "volatility_expansion_breakout", "Volatility Expansion Breakout",
        "available_ohlcv", ("1h candles",),
        ("Uses per-symbol rolling range and volume distributions; no absolute liquidity exclusion.",),
        {"1h": 48},
    ),
)

_BY_ID = {item.strategy_id: item for item in VARIANT_CATALOG}


def _frame(context: ResearchVariantContext, timeframe: str, minimum: int) -> pd.DataFrame | None:
    frame = context.strategy.completed_frame(timeframe)
    if len(frame) < minimum:
        return None
    return frame


def _signal(
    context: StrategyContext,
    strategy_id: str,
    timeframe: str,
    direction: str,
    entry: float,
    stop: float,
    reasons: tuple[str, ...],
    provenance_extra: Mapping[str, str] | None = None,
) -> StrategySignal | None:
    if not all(math.isfinite(value) and value > 0 for value in (entry, stop)):
        return None
    risk = stop - entry if direction == "SHORT" else entry - stop
    if risk <= 0:
        return None
    target = entry - 2 * risk if direction == "SHORT" else entry + 2 * risk
    candle = context.completed_frame(timeframe).iloc[-1]
    provenance = {timeframe: context.provenance.get(timeframe, "unspecified")}
    provenance.update(provenance_extra or {})
    return StrategySignal(
        strategy_id=strategy_id,
        strategy_version="research-variant-v1",
        product_id=context.instrument.product_id,
        symbol=context.instrument.symbol,
        direction=direction,
        decision_time=_decision_time(candle.time, timeframe),
        entry_price=None,
        entry_reference=f"next_{timeframe}_open",
        stop_price=stop,
        stop_logic="recent completed-candle structure",
        target_prices=(target,),
        target_logic="fixed 2R research target; not optimized or execution-adjusted",
        invalidation_conditions=("structural stop breached",),
        position_sizing_inputs={"quantity": "deferred to independent risk sizing"},
        provenance=provenance,
        diagnostics={"reasons": reasons, "research_only": True},
    )


def _parabolic_failure(ctx: ResearchVariantContext) -> list[StrategySignal]:
    frame = _frame(ctx, "1h", 48)
    if frame is None:
        return []
    closes = frame.close.astype(float)
    returns = closes.pct_change()
    current_return = closes.iloc[-1] / closes.iloc[-25] - 1
    baseline = returns.iloc[-48:-24].dropna().abs()
    robust_scale = float(baseline.median())
    range_pct = (frame.high.astype(float) - frame.low.astype(float)) / closes
    prior_range = range_pct.iloc[-48:-1].dropna()
    if robust_scale <= 0 or prior_range.empty:
        return []
    displacement = float(current_return)
    # Both event displacement and current range are compared with this symbol's own history.
    if displacement < max(4 * robust_scale, float(baseline.quantile(.9)) * 8):
        return []
    local_lows = frame.low.astype(float).iloc[-5:-1]
    close = float(closes.iloc[-1])
    if close >= float(local_lows.min()):
        return []
    stop = float(frame.high.astype(float).iloc[-3:].max())
    result = _signal(
        ctx.strategy, "parabolic_auction_failure_short", "1h", "SHORT",
        close, stop,
        ("per-symbol 24h displacement exceeded its rolling robust baseline",
         "completed candle closed below prior four-candle support",
         f"latest range percentile={float((prior_range <= range_pct.iloc[-1]).mean()):.3f}"),
        {"event_anchor": "rolling OHLCV high; order-book auction data unavailable"},
    )
    return [result] if result else []


def _failed_high_vwap_retest(ctx: ResearchVariantContext) -> list[StrategySignal]:
    frame = _frame(ctx, "1h", 20)
    if frame is None:
        return []
    recent = frame.iloc[-8:].copy()
    high_idx = recent.high.astype(float).idxmax()
    anchor_position = int(frame.index.get_loc(high_idx))
    after_high = frame.iloc[anchor_position + 1:]
    if len(after_high) < 2:
        return []
    anchor = float(frame.loc[high_idx, "high"])
    volume = frame.volume.astype(float).clip(lower=0)
    typical = (frame.high.astype(float) + frame.low.astype(float) + frame.close.astype(float)) / 3
    weights = volume.iloc[anchor_position:]
    total_volume = float(weights.sum())
    if total_volume <= 0:
        return []
    anchored_vwap = float((typical.iloc[anchor_position:] * weights).sum() / total_volume)
    previous, current = after_high.iloc[-2], after_high.iloc[-1]
    close = float(current.close)
    retest = float(current.high) >= anchor * .995 and close < anchor
    failed_vwap = close < anchored_vwap
    lower_high = float(current.high) < anchor
    if not (retest and failed_vwap and lower_high):
        return []
    stop = max(float(current.high), float(previous.high), anchor) * 1.001
    result = _signal(
        ctx.strategy, "failed_high_vwap_retest", "1h", "SHORT", close, stop,
        ("retest rejected below the recent completed-candle high",
         "close below OHLCV-anchored VWAP from swing-high candle",
         "lower-high structure confirmed"),
        {"vwap": "candle typical-price volume-weighted, anchored to detected high"},
    )
    return [result] if result else []


def _liquidity_sweep_reclaim(ctx: ResearchVariantContext) -> list[StrategySignal]:
    frame = _frame(ctx, "5m", 22)
    if frame is None:
        return []
    prior = frame.iloc[-21:-1]
    current = frame.iloc[-1]
    support = float(prior.low.min())
    resistance = float(prior.high.max())
    close, high, low = float(current.close), float(current.high), float(current.low)
    if low < support and close > support:
        direction, entry, stop = "LONG", close, low * .999
    elif high > resistance and close < resistance:
        direction, entry, stop = "SHORT", close, high * 1.001
    else:
        return []
    signal = _signal(
        ctx.strategy, "liquidity_sweep_reclaim", "5m", direction, entry, stop,
        ("completed candle breached a prior 20-candle extreme and reclaimed it",
         "price-structure proxy only; no resting liquidity inference"),
    )
    return [signal] if signal else []


def _trend_continuation(ctx: ResearchVariantContext) -> list[StrategySignal]:
    frame = _frame(ctx, "4h", 26)
    if frame is None:
        return []
    close = frame.close.astype(float)
    baseline = close.iloc[-25:-1]
    mean = float(baseline.mean())
    std = float(baseline.std(ddof=0))
    current = float(close.iloc[-1])
    prior_high = float(frame.high.astype(float).iloc[-25:-1].max())
    prior_low = float(frame.low.astype(float).iloc[-25:-1].min())
    if current > mean and current > prior_high:
        stop = float(frame.low.astype(float).iloc[-4:-1].min())
        direction = "LONG"
    elif current < mean and current < prior_low:
        stop = float(frame.high.astype(float).iloc[-4:-1].max())
        direction = "SHORT"
    else:
        return []
    if std <= 0:
        return []
    signal = _signal(
        ctx.strategy, "multi_day_trend_continuation", "4h",
        direction, current, stop,
        ("4h close broke the previous six-day range in the direction of its rolling mean",
         "trend condition calculated using completed candles only"),
    )
    return [signal] if signal else []


def _volatility_breakout(ctx: ResearchVariantContext) -> list[StrategySignal]:
    frame = _frame(ctx, "1h", 48)
    if frame is None:
        return []
    ranges = (frame.high.astype(float) - frame.low.astype(float)) / frame.close.astype(float)
    history = ranges.iloc[-48:-1]
    current_range = float(ranges.iloc[-1])
    if history.empty or current_range < float(history.quantile(.9)):
        return []
    volume = frame.volume.astype(float)
    typical_volume = float(volume.iloc[-48:-1].median())
    if typical_volume <= 0 or float(volume.iloc[-1]) < typical_volume:
        return []
    prior = frame.iloc[-25:-1]
    close = float(frame.close.iloc[-1])
    if close > float(prior.high.max()):
        direction = "LONG"
        stop = float(frame.low.iloc[-1])
    elif close < float(prior.low.min()):
        direction = "SHORT"
        stop = float(frame.high.iloc[-1])
    else:
        return []
    signal = _signal(
        ctx.strategy, "volatility_expansion_breakout", "1h",
        direction, close, stop,
        ("1h range exceeded this symbol's preceding 47-hour 90th percentile",
         "volume exceeded its preceding 47-hour median",
         "close broke the preceding 24-hour completed-candle range"),
    )
    return [signal] if signal else []


def _trade_flow_momentum(ctx: ResearchVariantContext) -> list[StrategySignal]:
    if not ctx.trade_history_complete:
        return []
    frame = _frame(ctx, "1m", 15)
    if frame is None:
        return []
    as_of = ctx.strategy.as_of
    trades = [
        trade for trade in ctx.trades
        if trade.timestamp <= as_of and trade.timestamp >= as_of - pd.Timedelta(minutes=5)
    ]
    if len(trades) < 20 or any(trade.aggressor_side == "unknown" for trade in trades):
        return []
    buy = sum(trade.quantity for trade in trades if trade.aggressor_side == "buy")
    sell = sum(trade.quantity for trade in trades if trade.aggressor_side == "sell")
    total = buy + sell
    if total <= 0 or abs(buy - sell) / total < .25:
        return []
    recent = frame.iloc[-6:]
    direction = "LONG" if buy > sell else "SHORT"
    close = float(recent.close.iloc[-1])
    price_change = close / float(recent.close.iloc[0]) - 1
    if (direction == "LONG" and price_change <= 0) or (direction == "SHORT" and price_change >= 0):
        return []
    stop = float(recent.low.min()) if direction == "LONG" else float(recent.high.max())
    signal = _signal(
        ctx.strategy, "trade_flow_momentum", "1m", direction, close, stop,
        ("complete recent trade tape has explicit aggressor-side labels",
         f"five-minute signed quantity imbalance={((buy-sell)/total):.3f}",
         "price progress is aligned with signed trade flow"),
        {"trades": "exchange-provided aggressor-side labels; unknown-side events rejected"},
    )
    return [signal] if signal else []


def _book_imbalance(ctx: ResearchVariantContext) -> list[StrategySignal]:
    frame = _frame(ctx, "1m", 15)
    if frame is None:
        return []
    snapshots = sorted(
        (book for book in ctx.order_books
         if book.observed_at <= ctx.strategy.as_of
         and book.integrity == "synchronized"
         and ctx.strategy.as_of - book.observed_at <= pd.Timedelta(minutes=2)),
        key=lambda book: book.observed_at,
    )
    if len(snapshots) < 2:
        return []
    first, last = snapshots[-2:]
    if last.observed_at - first.observed_at <= pd.Timedelta(0):
        return []
    depth = min(5, len(last.bids), len(last.asks))
    if depth < 1:
        return []
    bid_qty = sum(level.quantity for level in last.bids[:depth])
    ask_qty = sum(level.quantity for level in last.asks[:depth])
    total = bid_qty + ask_qty
    if total <= 0:
        return []
    imbalance = (bid_qty - ask_qty) / total
    if abs(imbalance) < .3:
        return []
    direction = "LONG" if imbalance > 0 else "SHORT"
    close = float(frame.close.iloc[-1])
    spread = last.asks[0].price - last.bids[0].price
    microprice = (
        last.asks[0].price * last.bids[0].quantity
        + last.bids[0].price * last.asks[0].quantity
    ) / max(last.bids[0].quantity + last.asks[0].quantity, 1e-12)
    if (direction == "LONG" and microprice <= close) or (direction == "SHORT" and microprice >= close):
        return []
    stop = close - 2 * spread if direction == "LONG" else close + 2 * spread
    signal = _signal(
        ctx.strategy, "order_book_imbalance_microprice", "1m",
        direction, close, stop,
        ("two fresh synchronized order-book snapshots available",
         f"top-{depth} depth imbalance={imbalance:.3f}",
         "microprice is aligned with imbalance"),
        {"book": "snapshot data only; no depletion/replenishment inference"},
    )
    return [signal] if signal else []


_EVALUATORS: Mapping[str, Callable[[ResearchVariantContext], list[StrategySignal]]] = {
    "parabolic_auction_failure_short": _parabolic_failure,
    "failed_high_vwap_retest": _failed_high_vwap_retest,
    "liquidity_sweep_reclaim": _liquidity_sweep_reclaim,
    "multi_day_trend_continuation": _trend_continuation,
    "volatility_expansion_breakout": _volatility_breakout,
    "trade_flow_momentum": _trade_flow_momentum,
    "order_book_imbalance_microprice": _book_imbalance,
}


class ResearchVariantSuite:
    @property
    def catalog(self) -> tuple[ResearchVariant, ...]:
        return VARIANT_CATALOG

    def evaluate(
        self, variant_id: str, context: ResearchVariantContext
    ) -> list[StrategySignal]:
        if variant_id not in _BY_ID:
            raise ValueError(f"unknown research variant: {variant_id}")
        variant = _BY_ID[variant_id]
        evaluator = _EVALUATORS.get(variant_id)
        if evaluator is None:
            return []
        return evaluator(context)

    def evaluate_available(
        self, context: ResearchVariantContext
    ) -> dict[str, list[StrategySignal]]:
        return {
            variant.strategy_id: self.evaluate(variant.strategy_id, context)
            for variant in VARIANT_CATALOG
            if variant.strategy_id in _EVALUATORS
        }
