import argparse
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

from strategy_short import (
    MultiHourRunnerConfig,
    calculate_sl_tp_prices,
    is_multihour_runner_trigger,
)


DEFAULT_BASE_URL = "https://api.india.delta.exchange"
HTTP_TIMEOUT = 20


# Supported backtest variants for multi-hour-runner setup.
SUPPORTED_STRATEGIES = {"mhr_static_rr4", "mhr_trailing_rr4"}


def _http_get(url: str, params: dict[str, Any] | None = None) -> requests.Response:
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(url, params=params, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response


@dataclass
class BacktestConfig:
    lookback_green_candles: int = 5
    capital_deployed: float = 100.0
    leverage: float = 20.0
    sl_capital_pct: float = 0.05
    pt_capital_pct: float = 1.0
    min_rr: float = 4.0
    max_holding_candles: int = 24
    charge_rate: float = 0.0005  # 0.05% per side


def fetch_live_perpetual_symbols(base_url: str = DEFAULT_BASE_URL) -> list[str]:
    response = _http_get(f"{base_url}/v2/products")
    payload = response.json()
    products = payload.get("result", [])

    return [
        p["symbol"]
        for p in products
        if p.get("contract_type") == "perpetual_futures" and p.get("state") == "live"
    ]


def _to_unix_ts_utc(date_str: str, end_of_day: bool = False) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def fetch_hourly_candles(
    symbol: str,
    limit: int = 1500,
    base_url: str = DEFAULT_BASE_URL,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> pd.DataFrame:
    resolved_end_ts = end_ts if end_ts is not None else int(time.time())
    resolved_start_ts = start_ts if start_ts is not None else resolved_end_ts - (60 * 60 * limit)

    params = {
        "symbol": symbol,
        "resolution": "1h",
        "start": resolved_start_ts,
        "end": resolved_end_ts,
    }
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
    entry_notional = entry_price * qty
    exit_notional = exit_price * qty
    return (entry_notional + exit_notional) * charge_rate


def _max_drawdown_from_pnl(trades: pd.DataFrame, pnl_col: str = "net_pnl") -> float:
    if trades.empty:
        return 0.0

    ordered = trades.sort_values("exit_time").reset_index(drop=True)
    equity = ordered[pnl_col].cumsum()
    running_peak = equity.cummax()
    drawdown = equity - running_peak
    return float(drawdown.min())


def _simulate_static_rr_exit(
    candles: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    max_holding_candles: int,
) -> tuple[float, str, pd.Timestamp, int]:
    """Static SL/TP simulation."""
    max_exit_idx = min(len(candles) - 1, entry_idx + max_holding_candles)
    bars_held = 0

    for idx in range(entry_idx + 1, max_exit_idx + 1):
        bars_held += 1
        candle = candles.iloc[idx]
        hit_sl = candle["high"] >= sl_price
        hit_tp = candle["low"] <= tp_price

        if hit_sl and hit_tp:
            return sl_price, "SL_AND_TP_SAME_CANDLE_SL_FIRST", candle["time"], bars_held
        if hit_sl:
            return sl_price, "SL", candle["time"], bars_held
        if hit_tp:
            return tp_price, "TP", candle["time"], bars_held

    last_candle = candles.iloc[max_exit_idx]
    return float(last_candle["close"]), "MAX_HOLD_EXIT", last_candle["time"], bars_held


def _simulate_mhr_trailing_exit(
    candles: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    initial_sl: float,
    max_holding_candles: int,
) -> tuple[float, str, pd.Timestamp, float, int]:
    """
    MHR trailing rules for SHORT:
    - Initial SL at 1R above entry.
    - At 2R move in favor, SL -> breakeven.
    - At 4R move in favor, SL -> 1:2 locked (entry - 2R).
    - Beyond 4R, for each additional 0.5R favorable move, trail SL by +0.5R.
    """
    risk_per_unit = initial_sl - entry_price
    if risk_per_unit <= 0:
        return entry_price, "INVALID_RISK", candles.iloc[entry_idx]["time"], entry_price, 0

    trailing_sl = initial_sl
    best_r = 0.0

    max_exit_idx = min(len(candles) - 1, entry_idx + max_holding_candles)
    bars_held = 0

    for idx in range(entry_idx + 1, max_exit_idx + 1):
        bars_held += 1
        candle = candles.iloc[idx]

        favorable_r = max(0.0, (entry_price - float(candle["low"])) / risk_per_unit)
        if favorable_r > best_r:
            best_r = favorable_r

        # Trail 1: at 2R -> breakeven
        if best_r >= 2.0:
            trailing_sl = min(trailing_sl, entry_price)

        # Trail 2: at 4R -> lock 2R
        if best_r >= 4.0:
            trailing_sl = min(trailing_sl, entry_price - 2.0 * risk_per_unit)

            # Aggressive trailing after 4R: every +0.5R improve stop by 0.5R
            steps = int((best_r - 4.0) / 0.5)
            if steps > 0:
                locked_r = 2.0 + (steps * 0.5)
                target_sl = entry_price - locked_r * risk_per_unit
                trailing_sl = min(trailing_sl, target_sl)

        if float(candle["high"]) >= trailing_sl:
            reason = "TRAIL_STOP"
            if trailing_sl == initial_sl:
                reason = "SL"
            elif trailing_sl == entry_price:
                reason = "TRAIL_TO_BREAKEVEN"
            return float(trailing_sl), reason, candle["time"], float(trailing_sl), bars_held

    last_candle = candles.iloc[max_exit_idx]
    return float(last_candle["close"]), "MAX_HOLD_EXIT", last_candle["time"], float(trailing_sl), bars_held


def backtest_symbol(
    symbol: str,
    candles: pd.DataFrame,
    config: BacktestConfig,
    strategy_name: str,
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    lookback = config.lookback_green_candles

    if candles.empty or len(candles) < lookback + 2:
        return trades

    strategy_config = MultiHourRunnerConfig(
        lookback_green_candles=lookback,
        capital_deployed=config.capital_deployed,
        leverage=config.leverage,
        sl_capital_pct=config.sl_capital_pct,
        pt_capital_pct=config.pt_capital_pct,
        min_rr=config.min_rr,
    )

    for entry_idx in range(lookback, len(candles) - 1):
        setup = candles.iloc[entry_idx - lookback: entry_idx]
        if not is_multihour_runner_trigger(setup):
            continue

        entry_candle = candles.iloc[entry_idx]
        entry_price = float(entry_candle["open"])

        try:
            sl_price, tp_price, qty = calculate_sl_tp_prices(entry_price, strategy_config)
        except ValueError:
            continue

        if strategy_name == "mhr_static_rr4":
            exit_price, exit_reason, exit_time, bars_held = _simulate_static_rr_exit(
                candles=candles,
                entry_idx=entry_idx,
                entry_price=entry_price,
                sl_price=sl_price,
                tp_price=tp_price,
                max_holding_candles=config.max_holding_candles,
            )
            final_sl = sl_price
        elif strategy_name == "mhr_trailing_rr4":
            exit_price, exit_reason, exit_time, final_sl, bars_held = _simulate_mhr_trailing_exit(
                candles=candles,
                entry_idx=entry_idx,
                entry_price=entry_price,
                initial_sl=sl_price,
                max_holding_candles=config.max_holding_candles,
            )
        else:
            raise ValueError(f"Unsupported strategy: {strategy_name}")

        gross_pnl = (entry_price - exit_price) * qty
        fees = _compute_fees(entry_price, exit_price, qty, config.charge_rate)
        net_pnl = gross_pnl - fees
        roi_on_capital = net_pnl / config.capital_deployed

        trades.append(
            {
                "symbol": symbol,
                "strategy": strategy_name,
                "entry_time": entry_candle["time"],
                "exit_time": exit_time,
                "entry_price": entry_price,
                "exit_price": float(exit_price),
                "initial_sl_price": float(sl_price),
                "final_sl_price": float(final_sl),
                "target_price_rr4": float(tp_price),
                "qty": int(qty),
                "bars_held": int(bars_held),
                "gross_pnl": float(gross_pnl),
                "fees": float(fees),
                "net_pnl": float(net_pnl),
                "roi_on_capital": float(roi_on_capital),
                "exit_reason": exit_reason,
            }
        )

    return trades


def _max_drawdown_from_pnl(trades: pd.DataFrame) -> float:
    if trades.empty:
        return 0.0

    ordered = trades.sort_values("exit_time").reset_index(drop=True)
    equity = ordered["pnl"].cumsum()
    running_peak = equity.cummax()
    drawdown = equity - running_peak
    return float(drawdown.min())


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

    drawdowns = (
        all_trades.groupby("symbol")[["exit_time", "pnl"]]
        .apply(_max_drawdown_from_pnl)
        .reset_index(name="max_drawdown")
    )

    merged = grouped.merge(drawdowns, on="symbol", how="left")
    return merged.sort_values("total_pnl", ascending=False).reset_index(drop=True)


def build_overall_stats(all_trades: pd.DataFrame) -> pd.DataFrame:
    if all_trades.empty:
        return pd.DataFrame()

    trades = len(all_trades)
    wins = int((all_trades["pnl"] > 0).sum())
    losses = int((all_trades["pnl"] <= 0).sum())
    total_pnl = float(all_trades["pnl"].sum())
    avg_pnl = float(all_trades["pnl"].mean())
    win_rate = wins / trades if trades else 0.0
    max_drawdown = _max_drawdown_from_pnl(all_trades)

    return pd.DataFrame(
        [
            {
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "avg_pnl": avg_pnl,
                "max_drawdown": max_drawdown,
            }
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest multi-strategy short models on Delta futures")
    parser.add_argument("--symbols", nargs="*", help="Symbols to backtest (default: all live perpetual futures)")
    parser.add_argument("--limit", type=int, default=1500, help="Number of hourly candles to request when start/end not provided")
    parser.add_argument("--start-date", help="UTC start date (YYYY-MM-DD), inclusive")
    parser.add_argument("--end-date", help="UTC end date (YYYY-MM-DD), inclusive")
    parser.add_argument("--capital", type=float, default=100.0, help="Capital deployed per trade")
    parser.add_argument("--leverage", type=float, default=20.0, help="Leverage used for quantity sizing")
    parser.add_argument("--max-hold", type=int, default=24, help="Max candles to hold before forced exit")
    parser.add_argument("--charges", type=float, default=0.0005, help="Per-side charge rate (default 0.0005 = 0.05%%)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Delta API base URL")
    parser.add_argument("--output", default="backtest_trades.csv", help="CSV path for trade-level results")
    parser.add_argument("--summary-output", default="backtest_summary.csv", help="CSV path for per-symbol summary")
    parser.add_argument("--overall-output", default="backtest_overall.csv", help="CSV path for overall metrics")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    start_ts = _to_unix_ts_utc(args.start_date) if args.start_date else None
    end_ts = _to_unix_ts_utc(args.end_date, end_of_day=True) if args.end_date else None

    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError("--start-date cannot be later than --end-date")

    symbols = args.symbols or fetch_live_perpetual_symbols(base_url=args.base_url)

    config = BacktestConfig(
        capital_deployed=args.capital,
        leverage=args.leverage,
        max_holding_candles=args.max_hold,
        charge_rate=args.charges,
    )

    trade_rows: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            candles = fetch_hourly_candles(
                symbol=symbol,
                limit=args.limit,
                base_url=args.base_url,
                start_ts=start_ts,
                end_ts=end_ts,
            )
            symbol_trades = backtest_symbol(symbol, candles, config)
            trade_rows.extend(symbol_trades)
            print(f"{symbol}: {len(symbol_trades)} trades")
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
    print("\n=== BY SYMBOL (sorted by total_pnl) ===")
    print(summary.to_string(index=False))

    print(f"\nSaved trade log: {args.output}")
    print(f"Saved symbol summary: {args.summary_output}")
    print(f"Saved overall metrics: {args.overall_output}")


if __name__ == "__main__":
    main()
