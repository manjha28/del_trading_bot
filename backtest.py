import argparse
import math
import os
import random
import time
import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

from strategy_short import (
    UnifiedShortConfig, ScalpConfig,
    calculate_ema, calculate_rsi, calculate_atr,
    calculate_sl_tp_prices, calculate_trade_capital,
    cap_pnl, estimate_candle_volume_usd,
    unified_entry_filter, detect_pump, is_pump_still_active,
    backtest_scalp_on_1m,
    calculate_dynamic_fees,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.india.delta.exchange"
HTTP_TIMEOUT = 20
BACKTEST_BUILD = "2026-04-17-dynamic-fees"
SUPPORTED_STRATEGIES = {"mhr_pump_short", "scalp_pullback"}

_last_api_call = 0.0
MIN_API_INTERVAL = 0.05


def _http_get(url, params=None):
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < MIN_API_INTERVAL:
        time.sleep(MIN_API_INTERVAL - elapsed)
    _last_api_call = time.time()
    with requests.Session() as s:
        s.trust_env = False
        r = s.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r


@dataclass
class BacktestConfig:
    lookback_green_candles: int = 4
    pump_min_pct: float = 50.0
    pump_volume_surge: float = 2.0
    initial_capital: float = 100.0
    max_capital_usage_pct: float = 0.50
    leverage: float = 20.0
    sl_capital_pct: float = 0.05
    pt_capital_pct: float = 1.0
    min_rr: float = 4.0
    max_holding_candles: int = 24
    charge_rate: float = 0.0005
    min_atr_pct: float = 0.3
    min_trade_capital: float = 5.0
    max_pnl_impact_pct: float = 2.0
    atr_sl_multiplier: float = 1.5
    pump_atr_sl_multiplier: float = 2.5
    cooldown_candles_min: int = 1
    cooldown_candles_max: int = 3
    use_1m_resolution: bool = True
    max_reentries: int = 3
    reentry_cooldown_candles: int = 1
    entry_paths: tuple = ("MHR_EXHAUSTION", "PUMP_HESITATION", "REENTRY")
    scalp_ema_5m: int = 20
    scalp_ema_1h: int = 50
    scalp_cooldown: int = 3
    scalp_max_session_trades: int = 1000
    scalp_max_hold: int = 60


def fetch_live_perpetual_symbols(base_url=DEFAULT_BASE_URL):
    r = _http_get(f"{base_url}/v2/products")
    return [p["symbol"] for p in r.json().get("result", [])
            if p.get("contract_type") == "perpetual_futures" and p.get("state") == "live"]


def _to_unix(ds, eod=False):
    dt = datetime.strptime(ds, "%Y-%m-%d")
    if eod:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _fetch_candles_bulk(symbol, resolution, start_ts, end_ts, base_url=DEFAULT_BASE_URL):
    all_frames = []
    secs = {"1m": 60, "5m": 300, "1h": 3600}.get(resolution, 3600)
    chunk = secs * 1500
    cs = start_ts
    while cs < end_ts:
        ce = min(cs + chunk, end_ts)
        try:
            r = _http_get(f"{base_url}/v2/history/candles",
                          {"symbol": symbol, "resolution": resolution, "start": cs, "end": ce})
            p = r.json()
            if p.get("success") and p.get("result"):
                all_frames.append(pd.DataFrame(p["result"]))
        except Exception:
            pass
        cs = ce
    if not all_frames:
        return pd.DataFrame()
    df = pd.concat(all_frames, ignore_index=True)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
    for c in ["open", "high", "low", "close", "volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def fetch_hourly_candles(symbol, limit=1500, base_url=DEFAULT_BASE_URL, start_ts=None, end_ts=None):
    end = end_ts or int(time.time())
    start = start_ts or end - 3600 * limit
    return _fetch_candles_bulk(symbol, "1h", start, end, base_url)


def resolve_1m(candle_time, sl, tp, bulk_1m):
    if bulk_1m is None or bulk_1m.empty:
        return "UNKNOWN"
    ht = pd.Timestamp(candle_time.timestamp() if hasattr(candle_time, 'timestamp') else candle_time,
                      unit="s", tz="UTC")
    mask = (bulk_1m["time"] >= ht) & (bulk_1m["time"] < ht + pd.Timedelta(hours=1))
    mdf = bulk_1m[mask]
    if mdf.empty:
        return "UNKNOWN"
    for _, mc in mdf.iterrows():
        h, l = float(mc["high"]), float(mc["low"])
        s_hit, t_hit = h >= sl, l <= tp
        if s_hit and t_hit:
            o = float(mc["open"])
            return "TP_FIRST" if abs(o - tp) < abs(o - sl) else "SL_FIRST"
        if s_hit: return "SL_FIRST"
        if t_hit: return "TP_FIRST"
    return "UNKNOWN"


def _max_dd(trades, col="net_pnl"):
    if trades.empty: return 0.0
    eq = trades.sort_values("exit_time")[col].cumsum()
    return float((eq - eq.cummax()).min())


# ═══════════════════════════════════════════════════════════════════════
# MHR TRAILING EXIT [5]
# ═══════════════════════════════════════════════════════════════════════
def _sim_trailing_exit(candles, entry_idx, entry_price, initial_sl, qty,
                       max_hold, bulk_1m=None, use_1m=True):
    risk = initial_sl - entry_price
    if risk <= 0:
        return 0.0, 0.0, "INVALID_RISK", candles.iloc[entry_idx]["time"], initial_sl, 0, 0
    q1 = qty // 2; q2 = qty - q1
    partial = 0.0; partial_done = False
    partial_price = entry_price - 2.0 * risk
    trail_sl = initial_sl; best_r = 0.0; bars = 0
    max_idx = min(len(candles) - 1, entry_idx + max_hold)
    for idx in range(entry_idx + 1, max_idx + 1):
        bars += 1; c = candles.iloc[idx]
        ch, cl = float(c["high"]), float(c["low"])
        sl_hit = ch >= trail_sl; tp_2r = entry_price - 2.0 * risk
        fav_r = max(0.0, (entry_price - cl) / risk)
        tp_hit = (not partial_done) and fav_r >= 2.0
        if sl_hit and tp_hit:
            res = resolve_1m(c["time"], trail_sl, tp_2r, bulk_1m) if use_1m and bulk_1m is not None else "UNKNOWN"
            if res == "UNKNOWN":
                o = float(c["open"])
                res = "TP_FIRST" if abs(o - tp_2r) < abs(o - trail_sl) else "SL_FIRST"
            if res == "TP_FIRST":
                partial_done = True; partial = (entry_price - partial_price) * q1
                return partial, (entry_price - trail_sl) * q2, "TRAIL_STOP_AFTER_PARTIAL", c["time"], trail_sl, bars, q1
            else:
                if partial_done:
                    return partial, (entry_price - trail_sl) * q2, "TRAIL_STOP_AFTER_PARTIAL", c["time"], trail_sl, bars, q1
                return 0.0, (entry_price - trail_sl) * qty, "SL_OR_TRAIL", c["time"], trail_sl, bars, 0
        if sl_hit:
            if partial_done:
                return partial, (entry_price - trail_sl) * q2, "TRAIL_STOP_AFTER_PARTIAL", c["time"], trail_sl, bars, q1
            return 0.0, (entry_price - trail_sl) * qty, "SL_OR_TRAIL", c["time"], trail_sl, bars, 0
        best_r = max(best_r, fav_r)
        if not partial_done and q1 > 0 and fav_r >= 2.0:
            partial_done = True; partial = (entry_price - partial_price) * q1
        if best_r > 1.0:
            trail_sl = min(trail_sl, entry_price - (best_r - 1.0) * risk)
    last = candles.iloc[max_idx]; ep = float(last["close"])
    if partial_done:
        return partial, (entry_price - ep) * q2, "MAX_HOLD_AFTER_PARTIAL", last["time"], trail_sl, bars, q1
    return 0.0, (entry_price - ep) * qty, "MAX_HOLD_EXIT", last["time"], trail_sl, bars, 0


# ─── MHR Backtest per symbol — uses dynamic fees ─────────────────────
def backtest_mhr_symbol(symbol, candles, config, running_equity=None,
                        base_url=DEFAULT_BASE_URL, bulk_1m=None):
    trades = []; equity = running_equity if running_equity is not None else config.initial_capital
    if candles.empty or len(candles) < 55: return trades, equity
    prepared = candles.copy().sort_values("time").reset_index(drop=True)
    prepared["ema20"] = calculate_ema(prepared["close"], 20)
    prepared["ema50"] = calculate_ema(prepared["close"], 50)
    prepared["rsi14"] = calculate_rsi(prepared["close"], 14)
    prepared["atr14"] = calculate_atr(prepared, 14)
    strat_cfg = UnifiedShortConfig(
        lookback_green_candles=config.lookback_green_candles,
        min_pump_pct=config.pump_min_pct, min_volume_surge_pct=config.pump_volume_surge,
        capital_deployed=config.initial_capital, leverage=config.leverage,
        sl_capital_pct=config.sl_capital_pct, pt_capital_pct=config.pt_capital_pct,
        min_rr=config.min_rr, max_capital_usage_pct=config.max_capital_usage_pct,
        max_pnl_impact_pct=config.max_pnl_impact_pct, atr_sl_multiplier=config.atr_sl_multiplier,
        pump_atr_sl_multiplier=config.pump_atr_sl_multiplier,
        max_reentries=config.max_reentries, reentry_cooldown_candles=config.reentry_cooldown_candles)
    idx = max(config.lookback_green_candles, 24)
    mhr_t = pump_t = reentry_t = f_rej = sl_rej = cap_rej = atr_rej = 0
    active_re = False; re_used = 0; last_ep = 0.0; last_sl_idx = -1
    while idx < len(candles) - 1:
        entry = prepared.iloc[idx]; entry_price = float(entry["open"])
        atr_val = float(entry["atr14"])
        if atr_val <= 0: sl_rej += 1; idx += 1; continue
        atr_pct = (atr_val / entry_price) * 100.0 if entry_price > 0 else 0
        if atr_pct < config.min_atr_pct: atr_rej += 1; idx += 1; continue
        is_re = (active_re and re_used < config.max_reentries and last_sl_idx > 0
                 and idx >= last_sl_idx + config.reentry_cooldown_candles)
        fi = unified_entry_filter(prepared, idx, strat_cfg, is_reentry=is_re, previous_entry_price=last_ep)
        if not fi: f_rej += 1; idx += 1; continue
        if fi.get("entry_path") not in config.entry_paths: f_rej += 1; idx += 1; continue
        trade_cap = calculate_trade_capital(equity, strat_cfg)
        if trade_cap < config.min_trade_capital: cap_rej += 1; idx += 1; continue
        # Structural SL: place stop at prev candle high (a real market level).
        # This makes 2R achievable — small risk distance means 2R target is close.
        # Falls back to ATR when prev_high <= entry_price (gap-up) or qty floors to 0.
        prev_high = float(prepared.iloc[idx - 1]["high"]) if idx > 0 else entry_price
        sl_method = "atr"
        sl_price = tp_price = 0.0; qty = 0
        if prev_high > entry_price:
            _max_loss = trade_cap * strat_cfg.sl_capital_pct
            _raw = math.floor(_max_loss / (prev_high - entry_price))
            if _raw >= 1:
                _qty = min(_raw, math.floor(trade_cap * strat_cfg.leverage / entry_price))
                if _qty >= 1:
                    sl_price = prev_high; qty = _qty; sl_method = "structural"
                    _tp = max(trade_cap * strat_cfg.pt_capital_pct, _max_loss * strat_cfg.min_rr)
                    tp_price = entry_price - _tp / qty
        if sl_method == "atr":
            try:
                sl_price, tp_price, qty = calculate_sl_tp_prices(
                    entry_price, strat_cfg, trade_cap, atr_val, fi.get("use_pump_atr", False))
            except ValueError: sl_rej += 1; idx += 1; continue
        part, rest, reason, exit_time, final_sl, bars, q_part = _sim_trailing_exit(
            prepared, idx, entry_price, sl_price, qty, config.max_holding_candles, bulk_1m, config.use_1m_resolution)
        gross = part + rest
        approx_exit = entry_price - (gross / qty) if qty > 0 else entry_price

        # DYNAMIC FEE: BTC/ETH 30min, others 15min
        fees = calculate_dynamic_fees(
            symbol, entry_price, approx_exit, qty,
            config.charge_rate, entry["time"], exit_time
        )

        net = gross - fees; net = cap_pnl(net, trade_cap, strat_cfg)
        eq_before = equity; equity += net
        if equity < 0: equity = 0.0
        ep = fi.get("entry_path", "UNKNOWN")
        if ep == "MHR_EXHAUSTION": mhr_t += 1
        elif ep == "PUMP_HESITATION": pump_t += 1
        elif ep == "REENTRY": reentry_t += 1
        trades.append({
            "symbol": symbol, "strategy": "mhr_pump_short",
            "entry_time": entry["time"], "exit_time": exit_time, "entry_price": entry_price,
            "initial_sl_price": sl_price, "final_sl_price": final_sl,
            "target_price": tp_price, "qty": qty, "atr14": atr_val, "atr_pct": atr_pct,
            "trade_capital": trade_cap, "equity_before_trade": eq_before, "equity_after_trade": equity,
            "partial_qty_filled": q_part, "entry_path": ep,
            "is_reentry": fi.get("is_reentry", False),
            "pump_pct": fi.get("pump_pct", 0), "volume_surge": fi.get("volume_surge", 0),
            "ema_distance_pct": fi.get("ema_distance_pct"), "rsi14": fi.get("rsi14"),
            "bars_held": bars, "gross_pnl": gross, "fees": fees, "net_pnl": net,
            "roi_on_capital": net / trade_cap if trade_cap > 0 else 0.0,
            "exit_reason": reason, "sl_method": sl_method})
        if reason == "SL_OR_TRAIL":
            pw = prepared.iloc[max(0, idx - strat_cfg.lookback_hours):idx + 1]
            if detect_pump(pw, strat_cfg) or fi.get("pump_pct", 0) > 0:
                active_re = True; re_used = re_used + 1 if is_re else 1
                last_ep = entry_price; last_sl_idx = idx + bars
                idx = last_sl_idx + config.reentry_cooldown_candles
            else:
                active_re = False; re_used = 0
                idx += max(1, bars + random.randint(config.cooldown_candles_min, config.cooldown_candles_max))
        else:
            active_re = False; re_used = 0; last_ep = 0.0; last_sl_idx = -1
            idx += max(1, bars + random.randint(config.cooldown_candles_min, config.cooldown_candles_max))
        if equity <= 0: break
    logger.info(f"  {symbol} | mhr: mhr={mhr_t}, pump={pump_t}, re={reentry_t}, "
                f"f_rej={f_rej}, sl={sl_rej}, trades={len(trades)}, eq={equity:.2f}")
    return trades, equity


# ─── Parallel worker ──────────────────────────────────────────────────
def _process_symbol(args_tuple):
    (sym, strategies, config_dict, equity_snap,
     start_ts, end_ts, base_url, use_1m) = args_tuple
    config = BacktestConfig(**config_dict)
    results = []
    try:
        bulk_1m = _fetch_candles_bulk(sym, "1m", start_ts, end_ts, base_url)
        for strat in strategies:
            if strat == "scalp_pullback":
                if bulk_1m.empty: continue
                scalp_cfg = ScalpConfig(
                    ema_5m_length=config.scalp_ema_5m,
                    ema_1h_length=config.scalp_ema_1h,
                    capital_deployed=config.initial_capital,
                    leverage=config.leverage,
                    cooldown_candles=config.scalp_cooldown,
                    max_trades_per_session=config.scalp_max_session_trades,
                    max_holding_candles=config.scalp_max_hold,
                )
                rows, _ = backtest_scalp_on_1m(sym, bulk_1m, scalp_cfg, equity_snap, config.charge_rate)
                results.extend(rows)
            elif strat == "mhr_pump_short":
                candles_1h = _fetch_candles_bulk(sym, "1h", start_ts, end_ts, base_url)
                if candles_1h.empty: continue
                rows, _ = backtest_mhr_symbol(sym, candles_1h, config, equity_snap, base_url,
                                              bulk_1m if use_1m else None)
                results.extend(rows)
        return sym, results, equity_snap, "ok"
    except Exception as e:
        return sym, [], equity_snap, str(e)


# ─── Summary builders ─────────────────────────────────────────────────
def build_summary(all_trades):
    if all_trades.empty: return pd.DataFrame()
    g = all_trades.groupby(["strategy", "symbol"], as_index=False).agg(
        trades=("net_pnl", "count"),
        wins=("net_pnl", lambda x: int((x > 0).sum())),
        losses=("net_pnl", lambda x: int((x <= 0).sum())),
        total_net_pnl=("net_pnl", "sum"), total_fees=("fees", "sum"),
        avg_net_pnl=("net_pnl", "mean"), avg_trade_capital=("trade_capital", "mean"),
        avg_bars_held=("bars_held", "mean"))
    g["win_rate"] = g["wins"] / g["trades"]
    dd = all_trades.groupby(["strategy", "symbol"])[["exit_time", "net_pnl"]].apply(
        lambda x: _max_dd(x)).reset_index(name="max_drawdown")
    return g.merge(dd, on=["strategy", "symbol"], how="left")


def build_overall(all_trades, init_cap):
    if all_trades.empty: return pd.DataFrame()
    rows = []
    for strat, s in all_trades.groupby("strategy"):
        final = init_cap + float(s["net_pnl"].sum())
        paths = s["entry_path"].value_counts().to_dict() if "entry_path" in s.columns else {}
        rows.append({
            "strategy": strat, "initial_capital": init_cap, "final_equity": final,
            "total_return_pct": ((final - init_cap) / init_cap) * 100,
            "trades": len(s), "entry_paths": str(paths),
            "wins": int((s["net_pnl"] > 0).sum()), "losses": int((s["net_pnl"] <= 0).sum()),
            "win_rate": float((s["net_pnl"] > 0).mean()),
            "total_net_pnl": float(s["net_pnl"].sum()), "avg_net_pnl": float(s["net_pnl"].mean()),
            "avg_bars_held": float(s["bars_held"].mean()), "total_fees": float(s["fees"].sum()),
            "max_drawdown": _max_dd(s)})
    return pd.DataFrame(rows)


def load_perf_filter(path):
    if not path: return None
    try: df = pd.read_csv(path)
    except: return None
    if df.empty or not {"symbol", "trades", "win_rate"}.issubset(df.columns): return None
    pc = "total_net_pnl" if "total_net_pnl" in df.columns else ("total_pnl" if "total_pnl" in df.columns else None)
    if not pc: return None
    keep = df[(df[pc] > 0) & (df["win_rate"] > 0.40) & (df["trades"] >= 20)]
    return set(keep["symbol"].astype(str)) if not keep.empty else None


def _write_insights(all_trades, summary, overall, config, start_ts, end_ts, path):
    ts_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_from = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    date_to   = datetime.fromtimestamp(end_ts,   tz=timezone.utc).strftime("%Y-%m-%d")
    n     = len(all_trades)
    wins  = int((all_trades["net_pnl"] > 0).sum())
    wr    = wins / n if n > 0 else 0.0
    gross = float(all_trades["gross_pnl"].sum()) if "gross_pnl" in all_trades.columns else 0.0
    fees  = float(all_trades["fees"].sum())      if "fees"      in all_trades.columns else 0.0
    net   = float(all_trades["net_pnl"].sum())
    pct   = (net / config.initial_capital) * 100

    lines = [
        f"# Backtest Insights — {ts_label}",
        "",
        "## Run Parameters",
        f"- Period: {date_from} → {date_to}",
        f"- Capital: ${config.initial_capital:.2f} | Leverage: {config.leverage}x",
        f"- Entry paths: {', '.join(config.entry_paths)}",
        f"- ATR mult: {config.atr_sl_multiplier} | Pump ATR mult: {config.pump_atr_sl_multiplier}",
        f"- Max hold: {config.max_holding_candles}h | Max reentries: {config.max_reentries}",
        f"- Charges: {config.charge_rate * 100:.3f}%",
        "",
        "## Overall Results",
        f"- Trades: {n} | Wins: {wins} | Win Rate: {wr:.1%}",
        f"- Gross PnL: ${gross:.2f} | Fees: ${fees:.2f} | Net PnL: ${net:.2f}",
        f"- Return on capital: {pct:.2f}%",
    ]

    if not summary.empty and len(summary) > 1:
        best  = summary.loc[summary["total_net_pnl"].idxmax()]
        worst = summary.loc[summary["total_net_pnl"].idxmin()]
        lines += [
            "",
            "## Symbol Highlights",
            f"- Best:  {best['symbol']} — net ${best['total_net_pnl']:.2f}, "
            f"WR {best['win_rate']:.1%}, {int(best['trades'])} trades",
            f"- Worst: {worst['symbol']} — net ${worst['total_net_pnl']:.2f}, "
            f"WR {worst['win_rate']:.1%}, {int(worst['trades'])} trades",
        ]

    if "entry_path" in all_trades.columns:
        lines += ["", "## Entry Path Breakdown"]
        for ep, cnt in all_trades["entry_path"].value_counts().items():
            ep_sub = all_trades[all_trades["entry_path"] == ep]
            ep_wr  = (ep_sub["net_pnl"] > 0).mean()
            ep_net = ep_sub["net_pnl"].sum()
            lines.append(f"- {ep}: {cnt} trades, WR {ep_wr:.1%}, net ${ep_net:.2f}")

    if "sl_method" in all_trades.columns:
        lines += ["", "## SL Method Split"]
        for method, cnt in all_trades["sl_method"].value_counts().items():
            m_sub = all_trades[all_trades["sl_method"] == method]
            m_wr  = (m_sub["net_pnl"] > 0).mean()
            m_net = m_sub["net_pnl"].sum()
            lines.append(f"- {method}: {cnt} trades, WR {m_wr:.1%}, net ${m_net:.2f}")

    if "exit_reason" in all_trades.columns:
        lines += ["", "## Exit Reason Distribution"]
        for reason, cnt in all_trades["exit_reason"].value_counts().items():
            lines.append(f"- {reason}: {cnt}")

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def parse_args():
    p = argparse.ArgumentParser(description="Unified Backtest: MHR + Pump + Scalp 5m (Dynamic Fees)")
    p.add_argument("--symbols", nargs="*")
    p.add_argument("--strategies", nargs="*", default=["mhr_pump_short"])
    p.add_argument("--limit", type=int, default=1500)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--start-date"); p.add_argument("--end-date")
    p.add_argument("--initial-capital", type=float, default=100.0)
    p.add_argument("--max-capital-pct", type=float, default=0.50)
    p.add_argument("--leverage", type=float, default=20.0)
    p.add_argument("--max-hold", type=int, default=24)
    p.add_argument("--charges", type=float, default=0.0005)
    p.add_argument("--cooldown-min", type=int, default=1)
    p.add_argument("--cooldown-max", type=int, default=3)
    p.add_argument("--atr-multiplier", type=float, default=1.5)
    p.add_argument("--pump-atr-mult", type=float, default=2.5)
    p.add_argument("--min-atr-pct", type=float, default=0.3)
    p.add_argument("--min-trade-capital", type=float, default=5.0)
    p.add_argument("--pump-min-pct", type=float, default=50.0)
    p.add_argument("--pump-volume-surge", type=float, default=2.0)
    p.add_argument("--max-reentries", type=int, default=3)
    p.add_argument("--reentry-cooldown", type=int, default=1)
    p.add_argument("--entry-paths", nargs="*",
                   default=["MHR_EXHAUSTION", "PUMP_HESITATION", "REENTRY"],
                   choices=["MHR_EXHAUSTION", "PUMP_HESITATION", "REENTRY"])
    p.add_argument("--no-1m-resolution", action="store_true")
    p.add_argument("--scalp-ema-5m", type=int, default=20)
    p.add_argument("--scalp-ema-1h", type=int, default=50)
    p.add_argument("--scalp-cooldown", type=int, default=3)
    p.add_argument("--scalp-max-session-trades", type=int, default=100)
    p.add_argument("--scalp-max-hold", type=int, default=60)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--performance-file", default="")
    p.add_argument("--output", default="backtest_trades.csv")
    p.add_argument("--summary-output", default="backtest_summary.csv")
    p.add_argument("--overall-output", default="backtest_overall.csv")
    return p.parse_args()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("backtest.log")])
    logger.info(f"Build: {BACKTEST_BUILD}")
    args = parse_args()
    if set(args.strategies) - SUPPORTED_STRATEGIES:
        raise ValueError(f"Only: {sorted(SUPPORTED_STRATEGIES)}")
    if args.performance_file == args.summary_output:
        args.performance_file = ""
    end_ts = _to_unix(args.end_date, True) if args.end_date else int(time.time())
    start_ts = _to_unix(args.start_date) if args.start_date else end_ts - args.days * 86400
    logger.info(
        f"Range: {datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%Y-%m-%d')} → "
        f"{datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime('%Y-%m-%d')} | "
        f"Strategies: {args.strategies} | Workers: {args.workers} | Dynamic Fees: ON")
    symbols = args.symbols or fetch_live_perpetual_symbols(args.base_url)
    filtered = list(symbols)
    perf = load_perf_filter(args.performance_file)
    if perf and len(perf) > 0: filtered = [s for s in filtered if s in perf]
    if not filtered: logger.warning("No symbols."); return
    logger.info(f"Symbols: {len(filtered)}")

    config = BacktestConfig(
        lookback_green_candles=4,
        pump_min_pct=args.pump_min_pct, pump_volume_surge=args.pump_volume_surge,
        initial_capital=args.initial_capital, max_capital_usage_pct=args.max_capital_pct,
        leverage=args.leverage, max_holding_candles=args.max_hold,
        charge_rate=args.charges,
        cooldown_candles_min=args.cooldown_min, cooldown_candles_max=args.cooldown_max,
        atr_sl_multiplier=args.atr_multiplier, pump_atr_sl_multiplier=args.pump_atr_mult,
        min_atr_pct=args.min_atr_pct, min_trade_capital=args.min_trade_capital,
        use_1m_resolution=not args.no_1m_resolution,
        max_reentries=args.max_reentries, reentry_cooldown_candles=args.reentry_cooldown,
        entry_paths=tuple(args.entry_paths),
        scalp_ema_5m=args.scalp_ema_5m, scalp_ema_1h=args.scalp_ema_1h,
        scalp_cooldown=args.scalp_cooldown,
        scalp_max_session_trades=args.scalp_max_session_trades,
        scalp_max_hold=args.scalp_max_hold,
    )

    config_dict = {f.name: getattr(config, f.name) for f in config.__dataclass_fields__.values()}

    equity = config.initial_capital
    trade_rows = []
    max_workers = min(args.workers, len(filtered))

    if max_workers <= 1:
        for i, sym in enumerate(filtered, 1):
            if equity <= 0: break
            try:
                bulk_1m = _fetch_candles_bulk(sym, "1m", start_ts, end_ts, args.base_url)
                logger.info(f"  [{i}/{len(filtered)}] {sym}: 1m={len(bulk_1m)}")
                for strat in args.strategies:
                    if strat == "scalp_pullback" and not bulk_1m.empty:
                        scalp_cfg = ScalpConfig(
                            ema_5m_length=config.scalp_ema_5m,
                            ema_1h_length=config.scalp_ema_1h,
                            capital_deployed=config.initial_capital,
                            leverage=config.leverage,
                            cooldown_candles=config.scalp_cooldown,
                            max_trades_per_session=config.scalp_max_session_trades,
                            max_holding_candles=config.scalp_max_hold,
                        )
                        rows, equity = backtest_scalp_on_1m(sym, bulk_1m, scalp_cfg, equity, config.charge_rate)
                        trade_rows.extend(rows)
                    elif strat == "mhr_pump_short":
                        candles_1h = fetch_hourly_candles(sym, args.limit, args.base_url, start_ts, end_ts)
                        if not candles_1h.empty:
                            rows, equity = backtest_mhr_symbol(
                                sym, candles_1h, config, equity, args.base_url,
                                bulk_1m if config.use_1m_resolution else None)
                            trade_rows.extend(rows)
            except Exception as e:
                logger.error(f"  [{i}/{len(filtered)}] {sym}: {e}")
    else:
        logger.info(f"Parallel: {max_workers} workers")
        tasks = [(sym, args.strategies, config_dict, config.initial_capital,
                  start_ts, end_ts, args.base_url, config.use_1m_resolution) for sym in filtered]
        all_trades_map = {}; done = 0
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_process_symbol, t): t[0] for t in tasks}
            for future in as_completed(futures):
                done += 1; sym = futures[future]
                try:
                    sn, trades, _, status = future.result()
                    if status == "ok" and trades:
                        all_trades_map[sn] = trades
                        logger.info(f"  [{done}/{len(filtered)}] {sn}: {len(trades)} trades")
                    else:
                        logger.info(f"  [{done}/{len(filtered)}] {sn}: {status}")
                except Exception as e:
                    logger.error(f"  [{done}/{len(filtered)}] {sym}: {e}")
        flat = []
        for t in all_trades_map.values(): flat.extend(t)
        flat.sort(key=lambda t: str(t["entry_time"]))
        trade_rows.extend(flat)
        # Each symbol ran independently at initial_capital — do NOT re-scale PnL by
        # running equity. The old scaling pass compounded wins across symbols and
        # produced unrealistic numbers (e.g. $7.5M from $100). Aggregate as-is.
        equity = config.initial_capital + sum(t["net_pnl"] for t in flat)

    all_trades = pd.DataFrame(trade_rows)
    if all_trades.empty: logger.warning("No trades."); return

    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "strategy-short", "mhr_pump_short", "backtest_results")
    os.makedirs(out_dir, exist_ok=True)
    trades_path  = os.path.join(out_dir, f"{ts_str}_backtest_trades.csv")
    summary_path = os.path.join(out_dir, f"{ts_str}_backtest_summary.csv")
    overall_path = os.path.join(out_dir, f"{ts_str}_backtest_overall.csv")
    insights_path = os.path.join(out_dir, f"{ts_str}_insights.md")

    all_trades.to_csv(trades_path, index=False)
    summary = build_summary(all_trades)
    overall = build_overall(all_trades, config.initial_capital)
    summary.to_csv(summary_path, index=False)
    overall.to_csv(overall_path, index=False)

    pct = ((equity - config.initial_capital) / config.initial_capital) * 100
    print(f"\n{'='*70}\nEQUITY CURVE\n{'='*70}")
    print(f"  Start: ${config.initial_capital:.2f}\n  Final: ${equity:.2f}\n  Return: {pct:.2f}%\n  Trades: {len(all_trades)}")
    print(f"\n{'='*70}\nOVERALL\n{'='*70}\n{overall.to_string(index=False)}")
    print(f"\n{'='*70}\nSUMMARY\n{'='*70}\n{summary.to_string(index=False)}")
    print(f"\n{'='*70}\nEXIT REASONS\n{'='*70}\n{all_trades['exit_reason'].value_counts().to_string()}")
    if "entry_path" in all_trades.columns:
        print(f"\n{'='*70}\nENTRY PATHS\n{'='*70}\n{all_trades['entry_path'].value_counts().to_string()}")
    _write_insights(all_trades, summary, overall, config, start_ts, end_ts, insights_path)
    print(f"\nSaved:\n  {trades_path}\n  {summary_path}\n  {overall_path}\n  {insights_path}")


if __name__ == "__main__":
    main()