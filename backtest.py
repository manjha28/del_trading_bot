import argparse
import time
from dataclasses import dataclass
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


def _http_get(url: str, params: dict[str, Any] | None = None) -> requests.Response:
    """
    Wrapper that avoids inheriting proxy env vars from restricted environments.
    """
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
    max_holding_candles: int = 24


def fetch_live_perpetual_symbols(base_url: str = DEFAULT_BASE_URL) -> list[str]:
    response = _http_get(f"{base_url}/v2/products")

    payload = response.json()
    products = payload.get("result", [])

    return [
        p["symbol"]
        for p in products
        if p.get("contract_type") == "perpetual_futures" and p.get("state") == "live"
    ]


def fetch_hourly_candles(symbol: str, limit: int = 1500, base_url: str = DEFAULT_BASE_URL) -> pd.DataFrame:
    end_ts = int(time.time())
    start_ts = end_ts - (60 * 60 * limit)

    params = {
        "symbol": symbol,
        "resolution": "1h",
        "start": start_ts,
        "end": end_ts,
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
    numeric_cols = ["open", "high", "low", "close", "volume"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _trade_exit_price_for_candle(candle: pd.Series, sl_price: float, tp_price: float) -> tuple[float, str] | None:
    hit_sl = candle["high"] >= sl_price
    hit_tp = candle["low"] <= tp_price

    if hit_sl and hit_tp:
        # conservative fill ordering for shorts: assume SL gets hit first
        return sl_price, "SL_AND_TP_SAME_CANDLE_SL_FIRST"
    if hit_sl:
        return sl_price, "SL"
    if hit_tp:
        return tp_price, "TP"
    return None


def backtest_symbol(symbol: str, candles: pd.DataFrame, config: BacktestConfig) -> list[dict[str, Any]]:
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
    )

    for entry_idx in range(lookback, len(candles) - 1):
        setup = candles.iloc[entry_idx - lookback: entry_idx]
        if not is_multihour_runner_trigger(setup):
            continue

        entry = candles.iloc[entry_idx]
        entry_price = float(entry["open"])
        sl_price, tp_price, qty = calculate_sl_tp_prices(entry_price, strategy_config)

        exit_price = float(candles.iloc[entry_idx + 1]["close"])
        exit_reason = "TIME_EXIT"
        exit_time = candles.iloc[entry_idx + 1]["time"]

        max_exit_idx = min(len(candles) - 1, entry_idx + config.max_holding_candles)
        for idx in range(entry_idx + 1, max_exit_idx + 1):
            maybe_exit = _trade_exit_price_for_candle(candles.iloc[idx], sl_price, tp_price)
            if maybe_exit is not None:
                exit_price, exit_reason = maybe_exit
                exit_time = candles.iloc[idx]["time"]
                break
            if idx == max_exit_idx:
                exit_price = float(candles.iloc[idx]["close"])
                exit_reason = "MAX_HOLD_EXIT"
                exit_time = candles.iloc[idx]["time"]

        pnl = (entry_price - float(exit_price)) * qty
        roi_on_capital = pnl / config.capital_deployed

        trades.append(
            {
                "symbol": symbol,
                "entry_time": entry["time"],
                "exit_time": exit_time,
                "entry_price": entry_price,
                "exit_price": float(exit_price),
                "sl_price": float(sl_price),
                "tp_price": float(tp_price),
                "qty": float(qty),
                "pnl": float(pnl),
                "roi_on_capital": float(roi_on_capital),
                "exit_reason": exit_reason,
            }
        )

    return trades


def build_summary(all_trades: pd.DataFrame) -> pd.DataFrame:
    if all_trades.empty:
        return pd.DataFrame()

    grouped = all_trades.groupby("symbol", as_index=False).agg(
        trades=("pnl", "count"),
        wins=("pnl", lambda x: int((x > 0).sum())),
        losses=("pnl", lambda x: int((x <= 0).sum())),
        total_pnl=("pnl", "sum"),
        avg_pnl=("pnl", "mean"),
        avg_roi=("roi_on_capital", "mean"),
    )
    grouped["win_rate"] = grouped["wins"] / grouped["trades"]
    return grouped.sort_values("total_pnl", ascending=False).reset_index(drop=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest five-green-candles short strategy on Delta futures")
    parser.add_argument("--symbols", nargs="*", help="Symbols to backtest (default: all live perpetual futures)")
    parser.add_argument("--limit", type=int, default=1500, help="Number of hourly candles to request")
    parser.add_argument("--capital", type=float, default=100.0, help="Capital deployed per trade")
    parser.add_argument("--leverage", type=float, default=20.0, help="Leverage used to convert capital targets to price")
    parser.add_argument("--max-hold", type=int, default=24, help="Max candles to hold before forced exit")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Delta API base URL")
    parser.add_argument("--output", default="backtest_trades.csv", help="CSV path for trade-level results")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    symbols = args.symbols or fetch_live_perpetual_symbols(base_url=args.base_url)

    config = BacktestConfig(
        capital_deployed=args.capital,
        leverage=args.leverage,
        max_holding_candles=args.max_hold,
    )

    trade_rows: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            candles = fetch_hourly_candles(symbol=symbol, limit=args.limit, base_url=args.base_url)
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

    print("\n=== SUMMARY ===")
    print(summary.to_string(index=False))
    print(f"\nSaved trade log: {args.output}")


if __name__ == "__main__":
    main()
