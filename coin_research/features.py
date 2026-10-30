from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

import numpy as np
import pandas as pd


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _percentile(value: float | None, history: pd.Series) -> float | None:
    clean = pd.to_numeric(history, errors="coerce").dropna()
    if value is None or len(clean) < 5:
        return None
    below = int((clean < value).sum())
    equal = int((clean == value).sum())
    return float((below + equal * 0.5) / len(clean) * 100.0)


def _frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    result = frame.copy()
    result["time"] = pd.to_datetime(result["time"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=["time", "open", "high", "low", "close", "volume"])
    result = result[(result.close > 0) & (result.high >= result.low) & (result.volume >= 0)]
    return result.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)


def _annualized_realized_vol(frame: pd.DataFrame, bars_per_day: int, lookback: int) -> float | None:
    if len(frame) < min(lookback + 1, 10):
        return None
    returns = np.log(frame.close).diff().dropna().tail(lookback)
    if len(returns) < 5:
        return None
    return float(returns.std(ddof=1) * math.sqrt(bars_per_day * 365) * 100)


def _book_features(book: dict | None, product: dict, execution_notional: float) -> dict:
    unavailable = {
        "spread_pct": None, "bid_depth_usd_5": None, "ask_depth_usd_5": None,
        "estimated_buy_impact_pct": None, "estimated_sell_impact_pct": None,
        "book_available": False,
    }
    if not isinstance(book, dict):
        return unavailable
    bids = book.get("buy") or book.get("bids") or []
    asks = book.get("sell") or book.get("asks") or []

    def levels(rows):
        parsed = []
        for row in rows:
            try:
                price, size = (row.get("price"), row.get("size")) if isinstance(row, dict) else row[:2]
                price, size = float(price), float(size)
                if price > 0 and size >= 0:
                    parsed.append((price, size))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        return parsed

    bid_levels, ask_levels = levels(bids), levels(asks)
    if not bid_levels or not ask_levels:
        return unavailable
    best_bid = max(price for price, _ in bid_levels)
    best_ask = min(price for price, _ in ask_levels)
    if best_bid <= 0 or best_ask <= best_bid:
        return unavailable
    midpoint = (best_bid + best_ask) / 2
    try:
        multiplier = float(product.get("contract_value") or 1.0)
    except (TypeError, ValueError):
        multiplier = 1.0
    multiplier_verified = (
        _number(product.get("contract_value")) is not None
        and _number(product.get("contract_value")) > 0
        and product.get("notional_type") == "vanilla"
        and product.get("is_quanto") is False
    )

    def depth(rows, asks_side):
        ordered = sorted(rows, key=lambda level: level[0], reverse=not asks_side)[:5]
        return sum(price * size * multiplier for price, size in ordered)

    def impact(rows, asks_side):
        ordered = sorted(rows, key=lambda level: level[0], reverse=not asks_side)
        remaining, filled_notional = execution_notional, 0.0
        for price, size in ordered:
            available = price * size * multiplier
            take = min(remaining, available)
            filled_notional += take
            remaining -= take
            if remaining <= 1e-9:
                break
        if remaining > 1e-9 or filled_notional <= 0:
            return None
        left, quantity = execution_notional, 0.0
        for price, size in ordered:
            take = min(left, price * size * multiplier)
            quantity += take / price
            left -= take
            if left <= 1e-9:
                break
        average_price = execution_notional / quantity if quantity > 0 else None
        if average_price is None:
            return None
        return abs(average_price - midpoint) / midpoint * 100.0

    return {
        "spread_pct": (best_ask - best_bid) / midpoint * 100,
        "bid_depth_usd_5": depth(bid_levels, False),
        "ask_depth_usd_5": depth(ask_levels, True),
        "estimated_buy_impact_pct": impact(ask_levels, True),
        "estimated_sell_impact_pct": impact(bid_levels, False),
        "book_available": True,
        "depth_notional_verified": multiplier_verified,
        "depth_notional_basis": "price × size × contract_value; linear vanilla contract",
        "best_bid": best_bid,
        "best_ask": best_ask,
    }


def evaluate_symbol(
    symbol: str,
    product: dict,
    ticker: dict | None,
    candles: dict[str, pd.DataFrame],
    book: dict | None = None,
    trades: pd.DataFrame | None = None,
    previous_features: dict | None = None,
    historical_features: dict[str, list[float]] | None = None,
    first_seen: str | None = None,
    now: datetime | None = None,
    execution_notional: float = 1000.0,
    max_spread_pct: float = 0.5,
    max_impact_pct: float = 0.5,
) -> dict:
    """Return observable, per-symbol state and diagnostics; never create orders."""
    now = now or datetime.now(timezone.utc)
    frames = {tf: _frame(candles.get(tf)) for tf in ("1m", "5m", "1h")}
    hourly = frames["1h"]
    ticker = ticker or {}
    previous_features = previous_features or {}
    historical_features = historical_features or {}
    result: dict[str, Any] = {
        "symbol": symbol,
        "as_of": now.isoformat(),
        "listing_age_days": None,
        "first_observed_age_days": None,
        "data_quality": {"confidence": 0.0, "issues": [], "available_timeframes": []},
        "features": {},
        "signals": [],
        "state": "insufficient_history",
        "state_reasons": [],
        "time_in_state_hours": None,
    }

    listing = product.get("created_at") or product.get("launch_time")
    try:
        if isinstance(listing, (int, float)):
            listing_dt = pd.to_datetime(
                listing, unit="ms" if listing > 1e11 else "s", utc=True
            )
        else:
            listing_dt = pd.Timestamp(listing)
        if listing_dt.tzinfo is None:
            listing_dt = listing_dt.tz_localize("UTC")
        result["listing_age_days"] = max(0.0, (now.timestamp() - listing_dt.timestamp()) / 86400)
    except (TypeError, ValueError):
        if first_seen:
            result["first_observed_age_days"] = max(
                0.0, (now.timestamp() - pd.Timestamp(first_seen).timestamp()) / 86400
            )
        result["data_quality"]["issues"].append("exchange listing timestamp unavailable")

    coverage = {}
    for timeframe, frame in frames.items():
        if not frame.empty:
            result["data_quality"]["available_timeframes"].append(timeframe)
            seconds = {"1m": 60, "5m": 300, "1h": 3600}[timeframe]
            expected = max(
                1, int((frame.time.iloc[-1] - frame.time.iloc[0]).total_seconds() / seconds) + 1
            )
            coverage[timeframe] = min(1.0, len(frame) / expected)
    if hourly.empty:
        result["data_quality"]["issues"].append("hourly OHLCV unavailable")
        result["market"] = _book_features(book, product, execution_notional)
        result["execution"] = {"eligible": False, "reason": "insufficient market data"}
        return result

    hourly = hourly.copy()
    hourly["notional_estimate"] = hourly.volume * ((hourly.high + hourly.low + hourly.close) / 3)
    hourly["range_pct"] = (hourly.high - hourly.low) / hourly.close * 100
    absolute_returns = hourly.close.pct_change().abs()
    hourly["efficiency"] = (
        (hourly.close / hourly.close.shift(24) - 1).abs()
        / absolute_returns.rolling(24, min_periods=18).sum().replace(0, np.nan)
    )
    hourly["day"] = hourly.time.dt.floor("D")
    daily = hourly.groupby("day", as_index=False).agg(
        notional=("notional_estimate", "sum"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        range_pct=("range_pct", "mean"),
        efficiency=("efficiency", "mean"),
        bars=("close", "count"),
    )
    daily["range_pct"] = (daily.high - daily.low) / daily.close * 100
    complete_days = daily.loc[daily.bars >= 23].copy()
    latest = hourly.iloc[-1]
    cutoff_24h = latest.time - pd.Timedelta(hours=24)
    cutoff_7d = latest.time - pd.Timedelta(days=7)
    recent_24 = hourly.loc[hourly.time > cutoff_24h]
    recent_7d = hourly.loc[hourly.time > cutoff_7d]
    if recent_24.empty:
        result["data_quality"]["issues"].append("no recent hourly observations")
        result["market"] = _book_features(book, product, execution_notional)
        result["execution"] = {"eligible": False, "reason": "no recent hourly observations"}
        return result

    current_notional_24h = float(recent_24.notional_estimate.sum())
    current_notional_7d = (
        float(recent_7d.notional_estimate.sum()) if len(recent_7d) >= 7 * 24 - 1 else None
    )
    daily_baseline_30 = complete_days.notional.tail(30)
    daily_baseline_60 = complete_days.notional.tail(60)
    rolling_24h = hourly.notional_estimate.rolling(24, min_periods=18).sum().dropna()
    volume_p30 = (
        _percentile(current_notional_24h, rolling_24h.iloc[-30 * 24:])
        if len(rolling_24h) >= 30 * 24 - 24 else None
    )
    volume_p60 = (
        _percentile(current_notional_24h, rolling_24h.iloc[-60 * 24:])
        if len(rolling_24h) >= 60 * 24 - 24 else None
    )
    volume_30_median = float(daily_baseline_30.median()) if len(daily_baseline_30) >= 30 else None
    volume_60_median = float(daily_baseline_60.median()) if len(daily_baseline_60) >= 60 else None
    volume_ratio_30 = current_notional_24h / volume_30_median if volume_30_median and volume_30_median > 0 else None
    volume_ratio_60 = current_notional_24h / volume_60_median if volume_60_median and volume_60_median > 0 else None

    rolling_abs_return = (
        (hourly.close / hourly.close.shift(24) - 1).abs().dropna()
    )
    range_24 = float(recent_24.range_pct.mean())
    range_history = hourly.range_pct.rolling(24, min_periods=18).mean().dropna()
    range_percentile = (
        _percentile(range_24, range_history.iloc[-60 * 24:])
        if len(range_history) >= 30 * 24 - 24 else None
    )
    displacement_24 = float((latest.close / recent_24.close.iloc[0] - 1) * 100)
    displacement_7d = (
        float((latest.close / recent_7d.close.iloc[0] - 1) * 100)
        if not recent_7d.empty else None
    )
    displacement_abs_percentile = _percentile(abs(displacement_24), rolling_abs_return.iloc[-60 * 24:])
    efficiency = float(recent_24.efficiency.mean()) if recent_24.efficiency.notna().any() else None

    previous_close = hourly.close.shift(1)
    true_range = pd.concat(
        [hourly.high - hourly.low, (hourly.high - previous_close).abs(),
         (hourly.low - previous_close).abs()], axis=1
    ).max(axis=1)
    atr_pct = float(true_range.tail(14).mean() / latest.close * 100) if len(hourly) >= 14 else None
    rv_1m = _annualized_realized_vol(frames["1m"], 1440, 60)
    rv_5m = _annualized_realized_vol(frames["5m"], 288, 48)
    rv_1h = _annualized_realized_vol(hourly, 24, 24)
    rv_1m_percentile = _percentile(
        rv_1m, pd.Series(historical_features.get("realized_volatility_1m_annualized_pct", []))
    )
    rv_5m_percentile = _percentile(
        rv_5m, pd.Series(historical_features.get("realized_volatility_5m_annualized_pct", []))
    )
    rv_1h_percentile = _percentile(
        rv_1h, pd.Series(historical_features.get("realized_volatility_1h_annualized_pct", []))
    )

    ticker_notional = None
    for field in ("quote_volume", "turnover_usd", "turnover"):
        ticker_notional = _number(ticker.get(field))
        if ticker_notional is not None:
            break
    market = _book_features(book, product, execution_notional)
    market["ticker_24h_notional"] = ticker_notional
    market["ticker_notional_source"] = (
        next((field for field in ("quote_volume", "turnover_usd", "turnover")
              if _number(ticker.get(field)) is not None), None)
    )
    market["candle_24h_notional_estimate"] = current_notional_24h

    tape_count, buy_notional, sell_notional = None, None, None
    trade_price_progress = None
    trade_frequency = None
    trade_span_seconds = None
    if trades is not None and not trades.empty and {"side", "volume", "price"}.issubset(trades):
        tape = trades.copy()
        tape["side"] = tape.side.astype(str).str.lower()
        tape["volume"] = pd.to_numeric(tape.volume, errors="coerce")
        tape["price"] = pd.to_numeric(tape.price, errors="coerce")
        tape = tape.dropna(subset=["side", "volume", "price"])
        tape = tape[(tape.volume > 0) & (tape.price > 0) & tape.side.isin(["buy", "sell"])]
        if not tape.empty:
            if "time" in tape:
                tape["time"] = pd.to_datetime(tape.time, utc=True, errors="coerce")
                tape = tape.dropna(subset=["time"]).sort_values("time")
            tape_count = int(len(tape))
            buy_notional = float((tape.loc[tape.side == "buy", "volume"] *
                                  tape.loc[tape.side == "buy", "price"]).sum())
            sell_notional = float((tape.loc[tape.side == "sell", "volume"] *
                                   tape.loc[tape.side == "sell", "price"]).sum())
            if tape_count >= 2 and "time" in tape:
                trade_span_seconds = max(
                    0.0, (tape.time.iloc[-1] - tape.time.iloc[0]).total_seconds()
                )
                if trade_span_seconds > 0:
                    trade_frequency = tape_count / trade_span_seconds
                trade_price_progress = (
                    float(tape.price.iloc[-1]) / float(tape.price.iloc[0]) - 1
                ) * 100 if trade_span_seconds > 0 else None
    prior_trade_frequency = _number(previous_features.get("trade_frequency_per_second"))
    trade_count_ratio = (
        trade_frequency / prior_trade_frequency
        if trade_frequency is not None and prior_trade_frequency is not None
        and prior_trade_frequency > 0 else None
    )
    flow_imbalance = None
    if buy_notional is not None and sell_notional is not None and buy_notional + sell_notional > 0:
        flow_imbalance = (buy_notional - sell_notional) / (buy_notional + sell_notional)
    aggressor_price_divergence = bool(
        flow_imbalance is not None and trade_price_progress is not None
        and ((flow_imbalance >= 0.3 and trade_price_progress <= 0)
             or (flow_imbalance <= -0.3 and trade_price_progress >= 0))
    )
    prior_oi = _number(previous_features.get("open_interest"))
    open_interest = _number(
        ticker.get("open_interest", ticker.get("oi"))
    )
    oi_change_pct = (
        (open_interest / prior_oi - 1) * 100
        if open_interest is not None and prior_oi is not None and prior_oi > 0 else None
    )
    funding_rate = _number(ticker.get("funding_rate", ticker.get("funding")))
    prior_funding = _number(previous_features.get("funding_rate"))
    funding_change = (
        funding_rate - prior_funding
        if funding_rate is not None and prior_funding is not None else None
    )
    oi_change_percentile = _percentile(
        oi_change_pct, pd.Series(historical_features.get("open_interest_change_pct", []))
    )
    funding_change_percentile = _percentile(
        funding_change, pd.Series(historical_features.get("funding_rate_change", []))
    )
    oi_change_extremeness = (
        max(oi_change_percentile, 100 - oi_change_percentile)
        if oi_change_percentile is not None else None
    )
    funding_change_extremeness = (
        max(funding_change_percentile, 100 - funding_change_percentile)
        if funding_change_percentile is not None else None
    )
    mark_price = _number(ticker.get("mark_price"))
    index_price = _number(ticker.get("index_price"))
    mark_index_deviation = (
        (mark_price / index_price - 1) * 100
        if mark_price is not None and index_price is not None and index_price > 0 else None
    )

    features = {
        "notional_24h_ticker": ticker_notional,
        "notional_24h_candle_estimate": current_notional_24h,
        "notional_7d_candle_estimate": current_notional_7d,
        "volume_30d_median_daily_estimate": volume_30_median,
        "volume_60d_median_daily_estimate": volume_60_median,
        "volume_ratio_30d": volume_ratio_30,
        "volume_ratio_60d": volume_ratio_60,
        "volume_compression_percentile_30d": volume_p30,
        "volume_compression_percentile_60d": volume_p60,
        "realized_volatility_1m_annualized_pct": rv_1m,
        "realized_volatility_5m_annualized_pct": rv_5m,
        "realized_volatility_1h_annualized_pct": rv_1h,
        "realized_volatility_1m_percentile": rv_1m_percentile,
        "realized_volatility_5m_percentile": rv_5m_percentile,
        "realized_volatility_1h_percentile": rv_1h_percentile,
        "atr_14h_pct": atr_pct,
        "range_compression_percentile": range_percentile,
        "price_displacement_24h_pct": displacement_24,
        "price_displacement_7d_pct": displacement_7d,
        "price_inactivity_percentile": (
            100 - displacement_abs_percentile if displacement_abs_percentile is not None else None
        ),
        "directional_efficiency_24h": efficiency,
        "trade_count": tape_count,
        "trade_observation_span_seconds": trade_span_seconds,
        "trade_frequency_per_second": trade_frequency,
        "trade_count_change_ratio": trade_count_ratio,
        "aggressor_flow_imbalance": flow_imbalance,
        "trade_price_progress_pct": trade_price_progress,
        "aggressor_price_divergence": aggressor_price_divergence,
        "funding_rate": funding_rate,
        "funding_rate_change": funding_change,
        "funding_rate_change_percentile": funding_change_percentile,
        "funding_rate_change_extremeness_percentile": funding_change_extremeness,
        "open_interest": open_interest,
        "open_interest_change_pct": oi_change_pct,
        "open_interest_change_percentile": oi_change_percentile,
        "open_interest_change_extremeness_percentile": oi_change_extremeness,
        "mark_index_deviation_pct": mark_index_deviation,
    }

    known_components = [
        value for value in (volume_p60, range_percentile,
                            displacement_abs_percentile)
        if value is not None
    ]
    enough_history = len(complete_days) >= 30
    if enough_history and len(known_components) >= 2:
        sleeping_score = float(np.mean([100 - value for value in known_components]))
    else:
        sleeping_score = None

    if not enough_history:
        result["data_quality"]["issues"].append(
            f"only {len(complete_days)} complete hourly days; 30 required for classification"
        )
    if len(hourly) < 24 * 20:
        result["data_quality"]["issues"].append("less than 20 days of hourly candles")
    if frames["1m"].empty:
        result["data_quality"]["issues"].append("1m realized volatility unavailable")
    if frames["5m"].empty:
        result["data_quality"]["issues"].append("5m realized volatility unavailable")
    for timeframe, ratio in coverage.items():
        if ratio < 0.9:
            result["data_quality"]["issues"].append(
                f"{timeframe} candle coverage is {ratio:.1%} over the loaded interval"
            )

    awakening_components = {
        "volume": bool((volume_p60 is not None and volume_p60 >= 95) or
                       (volume_ratio_30 is not None and volume_ratio_30 >= 3)),
        "volatility": bool(
            (range_percentile is not None and range_percentile >= 95)
            or any(value is not None and value >= 95 for value in
                   (rv_1m_percentile, rv_5m_percentile, rv_1h_percentile))
        ),
        "price": bool(displacement_abs_percentile is not None and
                      displacement_abs_percentile >= 95),
        "trade_count": bool(trade_count_ratio is not None and trade_count_ratio >= 3),
        "open_interest": bool(
            oi_change_extremeness is not None and oi_change_extremeness >= 95
        ),
        "funding_rate": bool(
            funding_change_extremeness is not None and funding_change_extremeness >= 95
        ),
        "volume_price_divergence": bool(
            volume_p60 is not None and volume_p60 >= 95
            and displacement_abs_percentile is not None
            and displacement_abs_percentile < 50
        ),
    }
    awakenings = [name for name, active in awakening_components.items() if active]

    history_high = hourly.high.shift(1).rolling(24, min_periods=12).max()
    previous_high = _number(history_high.iloc[-1]) if len(history_high) else None
    failed_high = bool(
        previous_high is not None and latest.high > previous_high and latest.close < previous_high
    )
    expansion_anchor = None
    if previous_features.get("expansion_anchor_time"):
        try:
            expansion_anchor = pd.Timestamp(previous_features["expansion_anchor_time"])
        except (TypeError, ValueError):
            expansion_anchor = None
    if expansion_anchor is not None and (now.timestamp() - expansion_anchor.timestamp()) > 7 * 86400:
        expansion_anchor = None
    if expansion_anchor is None and awakenings:
        expansion_anchor = latest.time
    anchored_vwap = None
    if expansion_anchor is not None:
        anchor_frame = hourly.loc[hourly.time >= expansion_anchor]
        if not anchor_frame.empty and float(anchor_frame.volume.sum()) > 0:
            anchored_vwap = float(
                (((anchor_frame.high + anchor_frame.low + anchor_frame.close) / 3)
                 * anchor_frame.volume).sum() / anchor_frame.volume.sum()
            )
    vwap_failure = bool(
        anchored_vwap is not None and latest.close < anchored_vwap
        and len(hourly) > 1 and hourly.close.iloc[-2] >= anchored_vwap
    )
    lower_high = bool(
        len(hourly) >= 4
        and hourly.high.iloc[-1] < hourly.high.iloc[-3]
        and hourly.high.iloc[-2] < hourly.high.iloc[-3]
    )
    support_level = _number(hourly.low.shift(1).rolling(6, min_periods=4).min().iloc[-1])
    support_break = bool(support_level is not None and latest.close < support_level)
    failed_high_retest = bool(
        previous_features.get("failed_high_level") is not None
        and latest.high >= float(previous_features["failed_high_level"]) * 0.997
        and latest.close < float(previous_features["failed_high_level"])
    )
    book_imbalance = None
    if market.get("book_available"):
        bid_depth, ask_depth = market["bid_depth_usd_5"], market["ask_depth_usd_5"]
        if bid_depth + ask_depth > 0:
            book_imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
    prior_book_imbalance = _number(previous_features.get("book_imbalance"))
    depth_change = (
        book_imbalance - prior_book_imbalance
        if book_imbalance is not None and prior_book_imbalance is not None else None
    )
    market["book_imbalance"] = book_imbalance
    market["book_imbalance_change"] = depth_change
    for side in ("bid", "ask"):
        current_depth = _number(market.get(f"{side}_depth_usd_5"))
        prior_depth = _number(previous_features.get(f"{side}_depth_usd_5"))
        market[f"{side}_depth_change_pct"] = (
            (current_depth / prior_depth - 1) * 100
            if current_depth is not None and prior_depth is not None and prior_depth > 0 else None
        )
    historical_depth_changes = [
        _number(value)
        for side in ("bid", "ask")
        for value in historical_features.get(f"{side}_depth_change_pct", [])
    ]
    historical_depth_changes = [value for value in historical_depth_changes if value is not None]
    current_depth_changes = [
        market.get("bid_depth_change_pct"), market.get("ask_depth_change_pct")
    ]
    depth_percentiles = []
    for value in current_depth_changes:
        percentile = _percentile(value, pd.Series(historical_depth_changes))
        if percentile is not None:
            depth_percentiles.append(max(percentile, 100 - percentile))
    book_depth_change_percentile = max(depth_percentiles) if depth_percentiles else None
    market["depth_change_percentile"] = book_depth_change_percentile
    awakening_components["order_book_depth_change"] = bool(
        book_depth_change_percentile is not None and book_depth_change_percentile >= 95
    )
    market["order_book_absorption"] = {
        "available": False,
        "reason": "time-aligned trade and order-book event history is required; a snapshot is insufficient",
    }
    if failed_high:
        result["signals"].append({
            "variant": "failed_high", "direction": "bearish_research_only",
            "available": True, "level": previous_high,
        })
    if vwap_failure:
        result["signals"].append({
            "variant": "pump_anchor_vwap_failure", "direction": "bearish_research_only",
            "available": True, "level": anchored_vwap,
        })
    if lower_high and support_break:
        result["signals"].append({
            "variant": "lower_high_support_break", "direction": "bearish_research_only",
            "available": True, "level": support_level,
        })
    frame_5m = frames["5m"]
    frame_1m = frames["1m"]
    mtf_bearish_confirmation = bool(
        vwap_failure and len(frame_5m) >= 6 and len(frame_1m) >= 15
        and frame_5m.close.iloc[-1] < frame_5m.low.iloc[-6:-1].min()
        and frame_1m.close.iloc[-1] < frame_1m.low.iloc[-15:-1].min()
    )
    result["signals"].append({
        "variant": "multi_timeframe_failed_auction",
        "available": len(frame_5m) >= 6 and len(frame_1m) >= 15,
        "triggered": mtf_bearish_confirmation,
        "direction": "bearish_research_only",
        "reason": "requires pump-anchor VWAP failure and 5m/1m support breaks",
    })
    if failed_high_retest:
        result["signals"].append({
            "variant": "failed_high_retest", "direction": "bearish_research_only",
            "available": True, "level": previous_features.get("failed_high_level"),
        })
    result["signals"].append({
        "variant": "volume_price_divergence",
        "available": volume_p60 is not None and displacement_abs_percentile is not None,
        "triggered": awakening_components["volume_price_divergence"],
        "direction": "non_directional_research_only",
        "volume_percentile_60d": volume_p60,
        "absolute_price_displacement_percentile": displacement_abs_percentile,
    })
    result["signals"].append({
        "variant": "order_book_depth_change",
        "available": book_depth_change_percentile is not None,
        "triggered": awakening_components["order_book_depth_change"],
        "direction": "non_directional_research_only",
        "depth_change_percentile": book_depth_change_percentile,
        "bid_depth_change_pct": market.get("bid_depth_change_pct"),
        "ask_depth_change_pct": market.get("ask_depth_change_pct"),
    })
    result["signals"].append({
        "variant": "aggressor_flow_divergence",
        "available": flow_imbalance is not None and trade_price_progress is not None,
        "triggered": aggressor_price_divergence,
        "direction": (
            "bearish_research_only" if flow_imbalance is not None
            and flow_imbalance >= 0.3 and trade_price_progress is not None
            and trade_price_progress <= 0
            else "bullish_research_only" if flow_imbalance is not None
            and flow_imbalance <= -0.3 and trade_price_progress is not None
            and trade_price_progress >= 0
            else "non_directional_research_only"
        ),
        "flow_imbalance": flow_imbalance,
        "trade_price_progress_pct": trade_price_progress,
        "reason": None if trade_price_progress is not None
        else "valid, timestamped aggressor-side tape unavailable",
    })
    result["signals"].append({
        "variant": "order_book_absorption",
        "available": False,
        "reason": market["order_book_absorption"]["reason"],
    })

    price = _number(latest.close)
    ticker_stamp = ticker.get("timestamp") or ticker.get("time")
    ticker_age_seconds = None
    try:
        if isinstance(ticker_stamp, (int, float)) or (
            isinstance(ticker_stamp, str) and ticker_stamp.strip().replace(".", "", 1).isdigit()
        ):
            numeric_stamp = float(ticker_stamp)
            unit = "us" if numeric_stamp > 1e14 else "ms" if numeric_stamp > 1e11 else "s"
            stamp = pd.to_datetime(numeric_stamp, unit=unit, utc=True)
        else:
            stamp = pd.Timestamp(ticker_stamp)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        ticker_age_seconds = max(0.0, now.timestamp() - stamp.timestamp())
    except (TypeError, ValueError):
        pass
    market["ticker_age_seconds"] = ticker_age_seconds
    latest_age = now.timestamp() - (latest.time.timestamp() + 3600)
    hourly_fresh = -1 <= latest_age <= 3600
    market["hourly_data_age_seconds"] = max(0.0, latest_age)

    confidence_parts = [
        min(1.0, len(complete_days) / 60),
        1.0 if hourly_fresh else 0.0,
        sum(not value for value in (frames["1m"].empty, frames["5m"].empty)) / 2,
        1.0 if market["book_available"] else 0.0,
        float(np.mean(list(coverage.values()))) if coverage else 0.0,
    ]
    confidence = float(np.mean(confidence_parts))
    result["data_quality"]["confidence"] = confidence
    result["data_quality"]["hourly_fresh"] = hourly_fresh
    result["data_quality"]["hourly_age_seconds"] = max(0.0, latest_age)
    result["data_quality"]["history_days"] = float(
        (hourly.time.iloc[-1] - hourly.time.iloc[0]).total_seconds() / 86400
    )
    result["data_quality"]["candle_coverage"] = coverage

    active_return = displacement_abs_percentile is not None and displacement_abs_percentile >= 95
    expansion = len(awakenings) >= 2
    if not enough_history or sleeping_score is None:
        state = "insufficient_history"
        reasons = result["data_quality"]["issues"] or ["rolling baseline unavailable"]
    elif expansion:
        state = (
            "climax" if len(awakenings) >= 3 and volume_p60 is not None
            and volume_p60 >= 99 and range_percentile is not None and range_percentile >= 99
            else "expansion" if active_return else "awakening"
        )
        reasons = awakenings
    elif failed_high or vwap_failure or (lower_high and support_break):
        state, reasons = "failed_auction", ["upward auction rejection confirmed by OHLCV structure"]
    elif sleeping_score >= 70:
        state, reasons = "sleeping", ["per-symbol volume, range, and displacement are compressed"]
    else:
        state, reasons = "active", ["activity is not classified as compressed or expanding"]

    market["price"] = price
    market["execution_notional"] = execution_notional
    buy_impact = market.get("estimated_buy_impact_pct")
    sell_impact = market.get("estimated_sell_impact_pct")
    fee_keys = ("taker_commission", "taker_commission_rate", "taker_fee")
    taker_fee = next(
        (_number(product.get(field)) for field in fee_keys
         if _number(product.get(field)) is not None
         and 0 <= _number(product.get(field)) < 1),
        None,
    )
    contract_limit = _number(product.get("position_size_limit"))
    minimum_quantity = next(
        (_number(product.get(field)) for field in ("minimum_order_size", "min_order_size")
         if _number(product.get(field)) is not None),
        None,
    )
    lot_size = _number(product.get("lot_size"))
    contract_multiplier = _number(product.get("contract_value"))
    estimated_quantity = (
        math.floor(
            (execution_notional / (price * contract_multiplier)) / lot_size
        ) * lot_size
        if price is not None and price > 0 and contract_multiplier is not None
        and contract_multiplier > 0 and lot_size is not None and lot_size > 0 else None
    )
    lot_aligned = (
        estimated_quantity is not None and lot_size is not None and lot_size > 0
        and abs(estimated_quantity / lot_size - round(estimated_quantity / lot_size)) < 1e-8
    )
    quantity_within_exchange_limits = (
        estimated_quantity is not None and minimum_quantity is not None
        and contract_limit is not None
        and minimum_quantity <= estimated_quantity <= contract_limit
        and lot_aligned
    )
    spec_verified = (
        _number(product.get("tick_size")) is not None and _number(product.get("tick_size")) > 0
        and lot_size is not None and lot_size > 0
        and minimum_quantity is not None and minimum_quantity > 0
        and contract_limit is not None and contract_limit > 0
        and taker_fee is not None
        and market.get("depth_notional_verified", False)
    )
    execution_reasons = []
    if not market["book_available"]:
        execution_reasons.append("order book unavailable")
    if market.get("spread_pct") is None or market["spread_pct"] > max_spread_pct:
        execution_reasons.append("spread exceeds configured execution limit or is unavailable")
    if buy_impact is None or sell_impact is None or max(buy_impact, sell_impact) > max_impact_pct:
        execution_reasons.append("estimated market impact exceeds configured limit or depth is insufficient")
    if not hourly_fresh:
        execution_reasons.append("hourly market data is stale")
    if not spec_verified:
        missing_fields = []
        if _number(product.get("tick_size")) is None or _number(product.get("tick_size")) <= 0:
            missing_fields.append("tick_size")
        if lot_size is None or lot_size <= 0:
            missing_fields.append("lot_size")
        if not any(_number(product.get(field)) is not None
                   and _number(product.get(field)) > 0
                   for field in ("minimum_order_size", "min_order_size")):
            missing_fields.append("minimum_order_size")
        if contract_limit is None or contract_limit <= 0:
            missing_fields.append("position_size_limit")
        if taker_fee is None:
            missing_fields.append("taker fee")
        if not market.get("depth_notional_verified", False):
            missing_fields.append("verified linear contract-value basis")
        if not quantity_within_exchange_limits:
            missing_fields.append("estimated order quantity violates minimum, lot, or position limit")
        execution_reasons.append(
            "contract metadata is unverified: " + ", ".join(missing_fields)
        )

    prior_state = previous_features.get("_state")
    prior_state_since = previous_features.get("state_since")
    if prior_state == state and prior_state_since:
        try:
            time_in_state = max(
                0.0, (now.timestamp() - pd.Timestamp(prior_state_since).timestamp()) / 3600
            )
            state_since = prior_state_since
        except (TypeError, ValueError):
            time_in_state, state_since = 0.0, now.isoformat()
    else:
        time_in_state, state_since = 0.0, now.isoformat()
    if failed_high:
        features["failed_high_level"] = previous_high
        features["failed_high_at"] = now.isoformat()
    else:
        prior_failed_at = previous_features.get("failed_high_at")
        try:
            is_recent_failed_high = (
                prior_failed_at is not None
                and (now.timestamp() - pd.Timestamp(prior_failed_at).timestamp()) <= 24 * 3600
            )
        except (TypeError, ValueError):
            is_recent_failed_high = False
        if is_recent_failed_high:
            features["failed_high_level"] = previous_features.get("failed_high_level")
            features["failed_high_at"] = prior_failed_at
        else:
            features["failed_high_level"] = None
            features["failed_high_at"] = None
    features["trade_count"] = tape_count
    features["trade_frequency_per_second"] = trade_frequency
    features["book_imbalance"] = book_imbalance
    features["bid_depth_usd_5"] = market.get("bid_depth_usd_5")
    features["ask_depth_usd_5"] = market.get("ask_depth_usd_5")
    features["bid_depth_change_pct"] = market.get("bid_depth_change_pct")
    features["ask_depth_change_pct"] = market.get("ask_depth_change_pct")
    features["order_book_depth_change_percentile"] = book_depth_change_percentile
    features["realized_volatility_1m_annualized_pct"] = rv_1m
    features["realized_volatility_5m_annualized_pct"] = rv_5m
    features["realized_volatility_1h_annualized_pct"] = rv_1h
    features["funding_rate"] = funding_rate
    features["funding_rate_change"] = funding_change
    features["open_interest"] = open_interest
    features["open_interest_change_pct"] = oi_change_pct
    features["expansion_anchor_time"] = (
        expansion_anchor.isoformat() if expansion_anchor is not None else None
    )
    result.update({
        "state": state,
        "state_reasons": reasons,
        "state_since": state_since,
        "time_in_state_hours": time_in_state,
        "sleeping_score": sleeping_score,
        "signal_candle_close_time": (latest.time + pd.Timedelta(hours=1)).isoformat(),
        "features": features,
        "market": market,
        "awakening": {
            "components": awakening_components,
            "active_components": awakenings,
            "expansion_anchor_time": (
                expansion_anchor.isoformat() if expansion_anchor is not None else None
            ),
            "anchor_vwap": anchored_vwap,
            "price_acceleration_percentile": displacement_abs_percentile,
            "volume_acceleration_percentile_30d": volume_p30,
            "higher_lows": bool(
                len(hourly) >= 3 and hourly.low.iloc[-1] > hourly.low.iloc[-3]
            ),
            "failed_high": failed_high,
            "vwap_failure": vwap_failure,
            "lower_high": lower_high,
            "support_break": support_break,
            "failed_high_retest": failed_high_retest,
            "open_interest_change": oi_change_pct,
            "funding_rate_change": funding_change,
            "open_interest_change_pct": oi_change_pct,
            "mark_index_deviation_pct": mark_index_deviation,
        },
        "execution": {
            "eligible": not execution_reasons,
            "estimated_notional": execution_notional,
            "estimated_market_impact_pct": max(
                (value for value in (buy_impact, sell_impact) if value is not None),
                default=None,
            ),
            "contract_specs_verified": spec_verified,
            "contract_specs": {
                "tick_size": _number(product.get("tick_size")),
                "lot_size": _number(product.get("lot_size")),
                "minimum_order_size": minimum_quantity,
                "position_size_limit": contract_limit,
                "taker_fee_rate": taker_fee,
                "fee_source": next((field for field in fee_keys
                                    if _number(product.get(field)) is not None), None),
                "depth_notional_verified": market.get("depth_notional_verified", False),
                "estimated_contract_quantity": estimated_quantity,
                "quantity_within_exchange_limits": quantity_within_exchange_limits,
                "round_trip_taker_fee_pct": taker_fee * 2 * 100 if taker_fee is not None else None,
            },
            "reasons": execution_reasons,
            "note": "research eligibility is independent; no order is submitted",
        },
    })
    if ticker_age_seconds is None:
        result["data_quality"]["issues"].append("ticker timestamp unavailable")
    if ticker_notional is None:
        result["data_quality"]["issues"].append(
            "ticker traded notional unavailable; candle notional is a price-volume estimate"
        )
    return result
