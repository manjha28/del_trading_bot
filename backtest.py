import argparse
import inspect
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

from strategy_short import (
    MultiHourRunnerConfig,
    calculate_ema,
    calculate_rsi,
    calculate_sl_tp_prices,
    mhr_entry_filters,
)

DEFAULT_BASE_URL = "https://api.india.delta.exchange"
HTTP_TIMEOUT = 20
BACKTEST_BUILD = "2026-04-13-mhr-trailing-partial"
SUPPORTED_STRATEGIES = {"mhr_trailing_rr4"}


def _http_get(url: str, params: dict[str, Any] | None = None) -> requests.Response:
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(url, params=params, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response


@dataclass
class BacktestConfig:
    lookback_green_candles: int = 4
    capital_deployed: float = 100.0
    leverage: float = 20.0
    sl_capital_pct: float = 0.05
    pt_capital_pct: float = 1.0
    min_rr: float = 4.0
    max_holding_candles: int = 24
    charge_rate: float = 0.0005
    min_volume_usd: float = 5_000_000
    max_spread_pct: float = 0.5
    cooldown_candles_min: int = 3
    cooldown_candles_max: int = 5
    max_open_trades: int = 5


def fetch_live_perpetual_symbols(base_url: str = DEFAULT_BASE_URL) -> list[str]:
    response = _http_get(f"{base_url}/v2/products")
    payload = response.json()
    products = payload.get("result", [])
    return [
        p["symbol"] for p in products if p.get("contract_type") == "perpetual_futures" and p.get("state") == "live"
    ]


def fetch_market_stats(base_url: str = DEFAULT_BASE_URL) -> dict[str, dict[str, float | None]]:
    """Best-effort volume/spread map from public tickers payload."""
    stats: dict[str, dict[str, float | None]] = {}
    try:
        response = _http_get(f"{base_url}/v2/tickers")
        payload = response.json()
        tickers = payload.get("result", [])
        for t in tickers:
            symbol = t.get("symbol")
            if not symbol:
                continue

            volume = t.get("quote_volume") or t.get("turnover") or t.get("volume_usd") or t.get("volume")
            try:
                volume_val = float(volume) if volume is not None else None
            except Exception:
                volume_val = None

            bid = t.get("best_bid") or t.get("bid")
            ask = t.get("best_ask") or t.get("ask")
            spread_pct = None
            try:
                if bid is not None and ask is not None and float(bid) > 0:
                    spread_pct = ((float(ask) - float(bid)) / float(bid)) * 100.0
            except Exception:
                spread_pct = None

            stats[symbol] = {"volume_usd": volume_val, "spread_pct": spread_pct}
    except Exception:
        return {}

    return stats


def _to_unix_ts_utc(date_str: str, end_of_day: bool = False) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def fetch_hourly_candles(
    symbol: str,
    limit: int = 1500,
    base_url: str = DEFAULT_BASE_URL,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> pd.DataFrame:
    resolved_end_ts = end_ts if end_ts is not None else int(time.time())
    resolved_start_ts = start_ts if start_ts is not None else resolved_end_ts - (60 * 60 * limit)
    params = {"symbol": symbol, "resolution": "1h", "start": resolved_start_ts, "end": resolved_end_ts}
    response = _http_get(f"{base_url}/v2/history/candles", params=params)

    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Candles API returned success=false for {symbol}: {payload}")

    df = pd.DataFrame(payload["result"])
    if df.empty:
        return df

    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.sort_values("time").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _compute_fees(entry_price: float, exit_price: float, qty: int, charge_rate: float) -> float:
    return (entry_price * qty + exit_price * qty) * charge_rate


def _max_drawdown_from_pnl(trades: pd.DataFrame, pnl_col: str = "net_pnl") -> float:
    if trades.empty:
        return 0.0
    eq = trades.sort_values("exit_time")[pnl_col].cumsum()
    return float((eq - eq.cummax()).min())


def _simulate_mhr_trailing_partial_exit(
    candles: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    initial_sl: float,
    qty: int,
    max_holding_candles: int,
) -> tuple[float, float, str, pd.Timestamp, float, int, int]:
    """
    Rules:
    - Keep RR baseline 1:4.
    - Book 50% at 1:2.
    - Remaining qty trails with 1:1 ratio (lock best_r - 1R).
    """
    risk_per_unit = initial_sl - entry_price
    if risk_per_unit <= 0:
        t = candles.iloc[entry_idx]["time"]
        return 0.0, 0.0, "INVALID_RISK", t, initial_sl, 0, 0

    qty_first = qty // 2
    qty_rest = qty - qty_first
    partial_realized = 0.0
    partial_done = False
    partial_price = entry_price - 2.0 * risk_per_unit

    trailing_sl = initial_sl
    best_r = 0.0
    bars_held = 0
    max_exit_idx = min(len(candles) - 1, entry_idx + max_holding_candles)

    for idx in range(entry_idx + 1, max_exit_idx + 1):
        bars_held += 1
        candle = candles.iloc[idx]

        favorable_r = max(0.0, (entry_price - float(candle["low"])) / risk_per_unit)
        best_r = max(best_r, favorable_r)

        if not partial_done and qty_first > 0 and favorable_r >= 2.0:
            partial_done = True
            partial_realized = (entry_price - partial_price) * qty_first

        # trailing 1:1 for remainder once profitable
        if best_r > 1.0:
            lock_r = best_r - 1.0
            candidate = entry_price - lock_r * risk_per_unit
            trailing_sl = min(trailing_sl, candidate)

        if float(candle["high"]) >= trailing_sl:
            exit_rest = (entry_price - trailing_sl) * qty_rest
            reason = "TRAIL_STOP_AFTER_PARTIAL" if partial_done else "SL_OR_TRAIL"
            return partial_realized, exit_rest, reason, candle["time"], trailing_sl, bars_held, qty_first

    last = candles.iloc[max_exit_idx]
    exit_rest = (entry_price - float(last["close"])) * qty_rest
    reason = "MAX_HOLD_AFTER_PARTIAL" if partial_done else "MAX_HOLD_EXIT"
    return partial_realized, exit_rest, reason, last["time"], trailing_sl, bars_held, qty_first


def backtest_symbol(
    symbol: str,
    candles: pd.DataFrame,
    config: BacktestConfig,
    strategy_name: str | None = None,
    *_: Any,
    **__: Any,
) -> list[dict[str, Any]]:
    strategy_name = strategy_name or "mhr_trailing_rr4"
    if strategy_name not in SUPPORTED_STRATEGIES:
        raise ValueError(f"Unsupported strategy: {strategy_name}")

    trades: list[dict[str, Any]] = []
    lookback = config.lookback_green_candles
    if candles.empty or len(candles) < lookback + 2:
        return trades

    prepared = candles.copy().sort_values("time").reset_index(drop=True)
    prepared["ema20"] = calculate_ema(prepared["close"], 20)
    prepared["ema50"] = calculate_ema(prepared["close"], 50)
    prepared["rsi14"] = calculate_rsi(prepared["close"], 14)

    strategy_cfg = MultiHourRunnerConfig(
        lookback_green_candles=lookback,
        capital_deployed=config.capital_deployed,
        leverage=config.leverage,
        sl_capital_pct=config.sl_capital_pct,
        pt_capital_pct=config.pt_capital_pct,
        min_rr=config.min_rr,
    )

    idx = lookback
    open_trade_slots = config.max_open_trades
    while idx < len(candles) - 1:
        if open_trade_slots <= 0:
            break

        setup = prepared.iloc[idx - lookback: idx]
        entry = prepared.iloc[idx]

        filter_info = mhr_entry_filters(prepared, idx, lookback)
        if not filter_info:
            idx += 1
            continue

        entry_price = float(entry["open"])

        try:
            sl_price, tp_rr4, qty = calculate_sl_tp_prices(entry_price, strategy_cfg)
        except ValueError:
            idx += 1
            continue

        partial_pnl, rest_pnl, exit_reason, exit_time, final_sl, bars_held, qty_partial = _simulate_mhr_trailing_partial_exit(
            candles=prepared,
            entry_idx=idx,
            entry_price=entry_price,
            initial_sl=sl_price,
            qty=qty,
            max_holding_candles=config.max_holding_candles,
        )

        gross_pnl = partial_pnl + rest_pnl
        # approximate effective exit for fee calc
        approx_exit_price = entry_price - (gross_pnl / qty) if qty > 0 else entry_price
        fees = _compute_fees(entry_price, approx_exit_price, qty, config.charge_rate)
        net_pnl = gross_pnl - fees

        trades.append(
            {
                "symbol": symbol,
                "strategy": strategy_name,
                "entry_time": entry["time"],
                "exit_time": exit_time,
                "entry_price": entry_price,
                "initial_sl_price": sl_price,
                "final_sl_price": final_sl,
                "target_price_rr4": tp_rr4,
                "qty": qty,
                "partial_qty_at_2r": qty_partial,
                "partial_tp_hit": bool(qty_partial > 0 and partial_pnl > 0),
                "ema_distance_pct": filter_info["ema_distance_pct"],
                "rsi14": filter_info["rsi14"],
                "wick_body_ratio": filter_info["wick_body_ratio"],
                "bars_held": bars_held,
                "gross_pnl": gross_pnl,
                "fees": fees,
                "net_pnl": net_pnl,
                "roi_on_capital": net_pnl / config.capital_deployed,
                "exit_reason": exit_reason,
            }
        )

        open_trade_slots -= 1
        cooldown = random.randint(config.cooldown_candles_min, config.cooldown_candles_max)
        idx += max(1, cooldown)

    return trades


def _invoke_backtest_symbol(symbol: str, candles: pd.DataFrame, config: BacktestConfig, strategy_name: str) -> list[dict[str, Any]]:
    params = inspect.signature(backtest_symbol).parameters
    if "strategy_name" in params:
        return backtest_symbol(symbol, candles, config, strategy_name)
    return backtest_symbol(symbol, candles, config)


def build_summary(all_trades: pd.DataFrame) -> pd.DataFrame:
    if all_trades.empty:
        return pd.DataFrame()

    grouped = all_trades.groupby(["strategy", "symbol"], as_index=False).agg(
        trades=("net_pnl", "count"),
        wins=("net_pnl", lambda x: int((x > 0).sum())),
        losses=("net_pnl", lambda x: int((x <= 0).sum())),
        total_net_pnl=("net_pnl", "sum"),
        total_fees=("fees", "sum"),
        avg_net_pnl=("net_pnl", "mean"),
        avg_roi=("roi_on_capital", "mean"),
    )
    grouped["win_rate"] = grouped["wins"] / grouped["trades"]

    dd = (
        all_trades.groupby(["strategy", "symbol"])[["exit_time", "net_pnl"]]
        .apply(lambda g: _max_drawdown_from_pnl(g, pnl_col="net_pnl"))
        .reset_index(name="max_drawdown")
    )
    return grouped.merge(dd, on=["strategy", "symbol"], how="left")


def build_overall_stats(all_trades: pd.DataFrame) -> pd.DataFrame:
    if all_trades.empty:
        return pd.DataFrame()

    rows = []
    for strategy, s in all_trades.groupby("strategy"):
        rows.append(
            {
                "strategy": strategy,
                "trades": len(s),
                "wins": int((s["net_pnl"] > 0).sum()),
                "losses": int((s["net_pnl"] <= 0).sum()),
                "win_rate": float((s["net_pnl"] > 0).mean()),
                "total_net_pnl": float(s["net_pnl"].sum()),
                "avg_net_pnl": float(s["net_pnl"].mean()),
                "total_fees": float(s["fees"].sum()),
                "max_drawdown": _max_drawdown_from_pnl(s, "net_pnl"),
            }
        )
    return pd.DataFrame(rows)




def load_symbol_performance_filter(path: str) -> set[str] | None:
    try:
        df = pd.read_csv(path)
    except Exception:
        return None

    required = {"symbol", "trades", "win_rate"}
    if not required.issubset(df.columns):
        return None

    pnl_col = "total_net_pnl" if "total_net_pnl" in df.columns else ("total_pnl" if "total_pnl" in df.columns else None)
    if pnl_col is None:
        return None

    keep = df[(df[pnl_col] > 0) & (df["win_rate"] > 0.40) & (df["trades"] >= 20)]
    return set(keep["symbol"].astype(str).tolist())

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest MHR trailing strategy")
    parser.add_argument("--symbols", nargs="*", help="Symbols to backtest (default: all live perpetual futures)")
    parser.add_argument("--strategies", nargs="*", default=["mhr_trailing_rr4"], help="Only mhr_trailing_rr4 is supported")
    parser.add_argument("--limit", type=int, default=1500, help="Hourly candle limit (used if --days/start-date not set)")
    parser.add_argument("--days", type=int, default=30, help="Backtest lookback days when explicit dates are not provided")
    parser.add_argument("--interactive", action="store_true", help="Prompt for soft-coded parameter overrides")
    parser.add_argument("--limit", type=int, default=1500, help="Hourly candle limit (used if --days/start-date not set)")
    parser.add_argument("--days", type=int, default=30, help="Backtest lookback days when explicit dates are not provided")
    parser.add_argument("--interactive", action="store_true", help="Prompt for soft-coded parameter overrides")
    parser.add_argument("--start-date", help="UTC start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="UTC end date (YYYY-MM-DD)")
    parser.add_argument("--capital", type=float, default=100.0)
    parser.add_argument("--leverage", type=float, default=20.0)
    parser.add_argument("--max-hold", type=int, default=24)
    parser.add_argument("--charges", type=float, default=0.0005, help="Per-side charges, default 0.05%%")
    parser.add_argument("--min-volume", type=float, default=5_000_000)
    parser.add_argument("--max-spread", type=float, default=0.5, help="Spread %% threshold")
    parser.add_argument("--cooldown-min", type=int, default=3)
    parser.add_argument("--cooldown-max", type=int, default=5)
    parser.add_argument("--max-open-trades", type=int, default=5)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--performance-file", default="backtest_summary.csv", help="Historical summary CSV for symbol-quality filtering")
    parser.add_argument("--output", default="backtest_trades.csv")
    parser.add_argument("--summary-output", default="backtest_summary.csv")
    parser.add_argument("--overall-output", default="backtest_overall.csv")
    return parser.parse_args()




def _prompt_with_default(label: str, current: Any, cast_fn):
    raw = input(f"{label} [{current}]: ").strip()
    if raw == "":
        return current
    try:
        return cast_fn(raw)
    except Exception:
        print(f"Invalid input for {label}, using default {current}")
        return current


def apply_interactive_overrides(args: argparse.Namespace) -> argparse.Namespace:
    print("\n=== Interactive Backtest Config (press Enter to keep defaults) ===")
    args.min_volume = _prompt_with_default("Minimum volume (USD)", args.min_volume, float)
    args.max_spread = _prompt_with_default("Max spread (%)", args.max_spread, float)
    args.days = _prompt_with_default("Lookback days", args.days, int)
    args.capital = _prompt_with_default("Capital per trade", args.capital, float)
    args.leverage = _prompt_with_default("Leverage", args.leverage, float)
    args.max_hold = _prompt_with_default("Max hold candles", args.max_hold, int)
    args.charges = _prompt_with_default("Per-side charges", args.charges, float)
    args.cooldown_min = _prompt_with_default("Cooldown min candles", args.cooldown_min, int)
    args.cooldown_max = _prompt_with_default("Cooldown max candles", args.cooldown_max, int)
    args.max_open_trades = _prompt_with_default("Max open trades", args.max_open_trades, int)
    return args

def main() -> None:
    print(f"Backtest build: {BACKTEST_BUILD}")
    args = parse_args()
    if args.interactive:
        args = apply_interactive_overrides(args)
    if args.interactive:
        args = apply_interactive_overrides(args)

    if set(args.strategies) - SUPPORTED_STRATEGIES:
        raise ValueError(f"Unsupported strategy list. Allowed: {sorted(SUPPORTED_STRATEGIES)}")

    end_ts = _to_unix_ts_utc(args.end_date, end_of_day=True) if args.end_date else int(time.time())
    if args.start_date:
        start_ts = _to_unix_ts_utc(args.start_date)
    else:
        start_ts = end_ts - (args.days * 24 * 60 * 60)

    symbols = args.symbols or fetch_live_perpetual_symbols(args.base_url)
    market_stats = fetch_market_stats(args.base_url)

    filtered_symbols = []
    for s in symbols:
        st = market_stats.get(s, {})
        vol = st.get("volume_usd")
        spread = st.get("spread_pct")
        if vol is not None and vol < args.min_volume:
            continue
        if spread is not None and spread > args.max_spread:
            continue
        filtered_symbols.append(s)

    perf_allowlist = load_symbol_performance_filter(args.performance_file)
    if perf_allowlist is not None:
        filtered_symbols = [s for s in filtered_symbols if s in perf_allowlist]

    config = BacktestConfig(
        capital_deployed=args.capital,
        leverage=args.leverage,
        max_holding_candles=args.max_hold,
        charge_rate=args.charges,
        min_volume_usd=args.min_volume,
        max_spread_pct=args.max_spread,
        cooldown_candles_min=args.cooldown_min,
        cooldown_candles_max=args.cooldown_max,
        max_open_trades=args.max_open_trades,
    )

    trade_rows: list[dict[str, Any]] = []
    for symbol in filtered_symbols:
        try:
            candles = fetch_hourly_candles(symbol, limit=args.limit, base_url=args.base_url, start_ts=start_ts, end_ts=end_ts)
            for strategy_name in args.strategies:
                rows = _invoke_backtest_symbol(symbol, candles, config, strategy_name)
                trade_rows.extend(rows)
                print(f"{symbol} | {strategy_name}: {len(rows)} trades")
        except Exception as exc:
            print(f"{symbol}: skipped due to error -> {exc}")

    all_trades = pd.DataFrame(trade_rows)
    if all_trades.empty:
        print("No trades generated.")
        return

    all_trades.to_csv(args.output, index=False)
    summary = build_summary(all_trades)
    overall = build_overall_stats(all_trades)
    summary.to_csv(args.summary_output, index=False)
    overall.to_csv(args.overall_output, index=False)

    print("\n=== OVERALL ===")
    print(overall.to_string(index=False))
    print("\n=== SUMMARY ===")
    print(summary.to_string(index=False))

    print("\n=== EXIT REASON DISTRIBUTION ===")
    print(all_trades["exit_reason"].value_counts(dropna=False).to_string())

    print(f"\nSaved trades: {args.output}")
    print(f"Saved summary: {args.summary_output}")
    print(f"Saved overall: {args.overall_output}")


if __name__ == "__main__":
    main()
