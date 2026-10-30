"""Chronological VPVR backtester with conservative 1m execution simulation."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import asdict, dataclass
import math
import numpy as np
import pandas as pd
from .profile import build_volume_profile, rolling_profiles
from .regime import classify_regime
from .strategies import generate_signals

@dataclass(frozen=True)
class BacktestConfig:
    initial_balance: float = 1000.0
    fee_rate: float = 0.0005
    slippage_bps: float = 2.0
    leverage: float = 10.0
    risk_per_trade_pct: float = 0.005
    daily_loss_limit_pct: float = 0.015
    max_consecutive_losses: int = 3
    max_hold_minutes: int = 180
    profile_lookback_15m: int = 96
    profile_rows: int = 60
    value_area_pct: float = 0.70
    min_reward_risk: float = 2.0
    volume_multiple: float = 1.0
    max_chase_bps: float = 10.0
    trailing_buffer_pct: float = 0.001

def _clean(frame, interval_minutes=None):
    required = {"time","open","high","low","close","volume"}
    if frame is None or not required.issubset(frame.columns):
        raise ValueError(f"candles must contain {sorted(required)}")
    x = frame.copy()
    x["time"] = pd.to_datetime(x.time, utc=True)
    x = x.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    for col in ("open","high","low","close","volume"):
        x[col] = pd.to_numeric(x[col], errors="coerce")
    x = x.replace([np.inf,-np.inf],np.nan).dropna(subset=list(required))
    if (x[["open","high","low","close"]] <= 0).any().any() or (x.volume < 0).any():
        raise ValueError("OHLCV contains non-positive prices or negative volume")
    if (x.high < x.low).any():
        raise ValueError("candle high is below candle low")
    x=x.reset_index(drop=True)
    if interval_minutes is not None and len(x) > 1:
        gaps=x.time.diff().dropna().dt.total_seconds()
        if (gaps != interval_minutes * 60).any():
            raise ValueError(f"candle history contains gaps for {interval_minutes}m timeframe")
    return x

def _completed_1m_history(candles, current_index):
    """Only candles strictly before current_index are completed at its open."""
    return candles.iloc[:current_index].copy()

def _quantity(equity, entry, stop, direction, multiplier, cfg,
              lot_size=1.0, min_qty=1.0, tick_size=None, position_size_limit=None):
    """Return risk-sized quantity and a conservatively tick-rounded protective stop."""
    if (equity <= 0 or multiplier <= 0 or lot_size <= 0 or min_qty <= 0 or
            (tick_size is not None and tick_size <= 0)):
        return 0.0, float(stop)
    if tick_size is not None:
        # Round away from entry so the sizing model never understates stop distance.
        if direction == "LONG":
            stop = math.floor(stop / tick_size + 1e-12) * tick_size
        else:
            stop = math.ceil(stop / tick_size - 1e-12) * tick_size
    distance = entry-stop if direction=="LONG" else stop-entry
    if distance <= 0:
        return 0.0, float(stop)
    unit_risk=(distance + entry*(2*cfg.fee_rate + 2*cfg.slippage_bps/10000))*multiplier
    if unit_risk <= 0:
        return 0.0, float(stop)
    risk_qty=(equity*cfg.risk_per_trade_pct)/unit_risk
    qty=math.floor((risk_qty/lot_size)+1e-12)*lot_size
    max_notional=equity*cfg.leverage*0.9
    leverage_qty=math.floor((max_notional/(entry*multiplier)/lot_size)+1e-12)*lot_size
    qty=min(qty,leverage_qty)
    if position_size_limit is not None:
        if position_size_limit <= 0:
            return 0.0, float(stop)
        exchange_qty=math.floor((position_size_limit/lot_size)+1e-12)*lot_size
        qty=min(qty,exchange_qty)
    if qty+1e-12 < min_qty:
        return 0.0, float(stop)
    return float(qty), float(stop)

def _metrics(start, trades, equity_curve):
    pnl=np.array([t["net_pnl"] for t in trades],dtype=float)
    wins,losses=pnl[pnl>0],pnl[pnl<0]
    daily=defaultdict(float)
    for trade in trades:
        daily[str(pd.Timestamp(trade["exit_time"]).date())]+=trade["net_pnl"]
    dr=np.array([v/start for v in daily.values()],dtype=float)
    std=np.std(dr,ddof=1) if len(dr)>1 else 0.0
    sharpe=float(np.mean(dr)/std*math.sqrt(252)) if std>0 else 0.0
    downside=dr[dr<0]
    dstd=float(np.sqrt(np.mean(downside**2))) if len(downside) else 0.0
    sortino=float(np.mean(dr)/dstd*math.sqrt(252)) if dstd>0 else 0.0
    peak=start;maxdd=0.0
    for value in equity_curve:
        peak=max(peak,value);maxdd=max(maxdd,(peak-value)/peak if peak else 0.0)
    gp=float(wins.sum());gl=abs(float(losses.sum()))
    return {"trades":len(trades),"win_rate":len(wins)/len(trades) if trades else 0.0,
        "gross_pnl":float(sum(t["gross_pnl"] for t in trades)),
        "net_pnl":float(pnl.sum()) if len(pnl) else 0.0,
        "fees":float(sum(t["fees"] for t in trades)),
        "profit_factor":gp/gl if gl else None,
        "average_win":float(wins.mean()) if len(wins) else 0.0,
        "average_loss":float(losses.mean()) if len(losses) else 0.0,
        "expectancy":float(pnl.mean()) if len(pnl) else 0.0,
        "max_drawdown_pct":maxdd,"sharpe_daily_annualized":sharpe,
        "sortino_daily_annualized":sortino,
        "average_holding_minutes":float(np.mean([t["holding_minutes"] for t in trades])) if trades else 0.0,
        "ending_equity":equity_curve[-1]}

def run_backtest(symbol,candles_1m,candles_3m,candles_15m,config=None,contract_multiplier=1.0):
    """Run one symbol chronologically. Stops win when stop/target both touch in one 1m bar.

    Signals queue a trigger for a later 1m bar. Profiles from historical candles
    are explicitly approximations; no queue priority or historical funding is
    fabricated.
    """
    cfg=config or BacktestConfig()
    c1,c3,c15=_clean(candles_1m,1),_clean(candles_3m,3),_clean(candles_15m,15)
    if min(len(c1),len(c3),len(c15))<35:
        raise ValueError("insufficient candles for EMA/profile warm-up")
    if (contract_multiplier<=0 or lot_size<=0 or min_qty<=0 or
            (tick_size is not None and tick_size<=0) or
            (position_size_limit is not None and position_size_limit<=0)):
        raise ValueError("invalid contract multiplier, lot, minimum quantity, tick or position-size limit")
    equity=cfg.initial_balance
    pending=position=None
    trades=[];equity_curve=[equity]
    skipped=defaultdict(int);signal_counts=defaultdict(int)
    daily_start=equity;daily_date=None;consecutive_losses=0

    for i in range(35,len(c1)):
        candle=c1.iloc[i];ts=pd.Timestamp(candle.time);day=ts.date()
        if day!=daily_date:
            daily_date=day;daily_start=equity
        bar={k:float(candle[k]) for k in ("open","high","low","close","volume")}

        # Existing position management: stop-first resolves same-bar ambiguity.
        if position:
            pos=position
            direction=pos["direction"]
            if pos["target_index"]>=2 and pos["remaining"]>1e-10:
                if direction=="LONG" and pos.get("last_candle_low") is not None:
                    pos["stop"]=max(pos["stop"],pos["last_candle_low"]*(1-cfg.trailing_buffer_pct))
                elif direction=="SHORT" and pos.get("last_candle_high") is not None:
                    pos["stop"]=min(pos["stop"],pos["last_candle_high"]*(1+cfg.trailing_buffer_pct))
            stop_hit=bar["low"]<=pos["stop"] if direction=="LONG" else bar["high"]>=pos["stop"]
            if stop_hit:
                fill=pos["stop"]*(1-cfg.slippage_bps/10000 if direction=="LONG" else 1+cfg.slippage_bps/10000)
                qty=pos["remaining"]
                gross=((fill-pos["entry"]) if direction=="LONG" else (pos["entry"]-fill))*qty*contract_multiplier
                fee=abs(fill*qty*contract_multiplier)*cfg.fee_rate
                net=gross-fee
                pos["gross_pnl"]+=gross
                pos["fees"]+=fee
                pos["net_pnl"]+=net
                equity+=net
                trades.append({**pos,"exit_time":ts.isoformat(),"exit_price":fill,"exit_reason":"STOP",
                    "holding_minutes":(ts-pd.Timestamp(pos["opened_at"])).total_seconds()/60})
                consecutive_losses=consecutive_losses+1 if net<0 else 0
                position=None
            else:
                targets=pos["targets"]
                target_index_before=pos["target_index"]
                fractions=[1.0] if len(targets)==1 else ([.30,.45] if len(targets)==2 else [.30,.45,.25])
                while pos["target_index"]<len(targets):
                    ix=pos["target_index"]
                    target=targets[ix]
                    hit=bar["high"]>=target if direction=="LONG" else bar["low"]<=target
                    if not hit:
                        break
                    qty=min(pos["remaining"],pos["quantity"]*fractions[min(ix,len(fractions)-1)])
                    gross=((target-pos["entry"]) if direction=="LONG" else (pos["entry"]-target))*qty*contract_multiplier
                    fee=abs(target*qty*contract_multiplier)*cfg.fee_rate
                    net=gross-fee
                    pos["gross_pnl"]+=gross
                    pos["fees"]+=fee
                    pos["net_pnl"]+=net
                    pos["remaining"]-=qty
                    equity+=net
                    pos["target_index"]+=1
                    if ix==0:
                        pos["stop"]=pos["entry"]
                    elif ix==1:
                        pos["stop"]=targets[0]
                    if pos["remaining"]<=1e-10:
                        break
                if pos["remaining"]>1e-10 and pos["target_index"]>target_index_before:
                    tightened_stop_hit=bar["low"]<=pos["stop"] if direction=="LONG" else bar["high"]>=pos["stop"]
                    if tightened_stop_hit:
                        fill=pos["stop"]*(1-cfg.slippage_bps/10000 if direction=="LONG" else 1+cfg.slippage_bps/10000)
                        qty=pos["remaining"]
                        gross=((fill-pos["entry"]) if direction=="LONG" else (pos["entry"]-fill))*qty*contract_multiplier
                        fee=abs(fill*qty*contract_multiplier)*cfg.fee_rate
                        pos["gross_pnl"]+=gross
                        pos["fees"]+=fee
                        pos["net_pnl"]+=gross-fee
                        equity+=gross-fee
                        trades.append({**pos,"exit_time":ts.isoformat(),"exit_price":fill,"exit_reason":"SAME_BAR_TIGHTENED_STOP",
                            "holding_minutes":(ts-pd.Timestamp(pos["opened_at"])).total_seconds()/60})
                        consecutive_losses=consecutive_losses+1 if pos["net_pnl"]<0 else 0
                        position=None
                if position is pos and pos["remaining"]<=1e-10:
                    trades.append({**pos,"exit_time":ts.isoformat(),"exit_price":targets[-1],"exit_reason":"TARGETS",
                        "holding_minutes":(ts-pd.Timestamp(pos["opened_at"])).total_seconds()/60})
                    consecutive_losses=consecutive_losses+1 if pos["net_pnl"]<0 else 0
                    position=None
                elif (ts-pd.Timestamp(pos["opened_at"])).total_seconds()/60>=cfg.max_hold_minutes:
                    fill=bar["close"]*(1-cfg.slippage_bps/10000 if direction=="LONG" else 1+cfg.slippage_bps/10000)
                    qty=pos["remaining"]
                    gross=((fill-pos["entry"]) if direction=="LONG" else (pos["entry"]-fill))*qty*contract_multiplier
                    fee=abs(fill*qty*contract_multiplier)*cfg.fee_rate
                    pos["gross_pnl"]+=gross
                    pos["fees"]+=fee
                    pos["net_pnl"]+=gross-fee
                    equity+=gross-fee
                    trades.append({**pos,"exit_time":ts.isoformat(),"exit_price":fill,"exit_reason":"TIMEOUT",
                        "holding_minutes":cfg.max_hold_minutes})
                    consecutive_losses=consecutive_losses+1 if pos["net_pnl"]<0 else 0
                    position=None
                if position is pos:
                    pos["last_candle_low"]=bar["low"]
                    pos["last_candle_high"]=bar["high"]

        # Process an older queued trigger on this candle only.
        if pending and position is None:
            sig=pending["signal"];pending["bars_waited"]+=1;direction=sig.direction
            if pending["bars_waited"]>sig.expiry_candles:
                pending=None
            else:
                invalid=bar["low"]<=sig.stop_loss if direction=="LONG" else bar["high"]>=sig.stop_loss
                chase=bar["open"]>sig.entry*(1+cfg.max_chase_bps/10000) if direction=="LONG" else bar["open"]<sig.entry*(1-cfg.max_chase_bps/10000)
                trigger=bar["high"]>=sig.entry if direction=="LONG" else bar["low"]<=sig.entry
                if invalid or chase:
                    pending=None
                elif trigger:
                    reference=max(sig.entry,bar["open"]) if direction=="LONG" else min(sig.entry,bar["open"])
                    fill=reference*(1+cfg.slippage_bps/10000 if direction=="LONG" else 1-cfg.slippage_bps/10000)
                    qty,rounded_stop=_quantity(equity,fill,sig.stop_loss,direction,
                        contract_multiplier,cfg,lot_size,min_qty,tick_size,position_size_limit)
                    if equity<=daily_start*(1-cfg.daily_loss_limit_pct):
                        qty=0.0;skipped["daily_loss_lockout"]+=1
                    if consecutive_losses>=cfg.max_consecutive_losses:
                        qty=0.0;skipped["consecutive_loss_lockout"]+=1
                    if qty>0:
                        fee=abs(fill*qty*contract_multiplier)*cfg.fee_rate;equity-=fee
                        position={"symbol":symbol,"strategy":sig.strategy,"direction":direction,"entry":fill,
                            "stop":rounded_stop,"targets":list(sig.targets[:3]),"quantity":qty,"remaining":qty,
                            "opened_at":ts.isoformat(),"target_index":0,"gross_pnl":0.0,"fees":fee,"net_pnl":-fee,
                            "last_candle_low":bar["low"],"last_candle_high":bar["high"],"regime":pending["regime"]}

                    else:
                        skipped["risk_or_sizing_reject"]+=1
                    pending=None

        # Only bars whose end timestamp is <= the current 1m candle start are usable.
        if position is None and pending is None:
            c1hist=_completed_1m_history(c1,i)
            c3hist=c3.loc[(c3.time+pd.Timedelta(minutes=3))<=ts]
            c15hist=c15.loc[(c15.time+pd.Timedelta(minutes=15))<=ts]
            if len(c3hist)>=30 and len(c15hist)>=30:
                try:
                    pf=c15hist.tail(cfg.profile_lookback_15m)
                    profile=build_volume_profile(pf,rows=cfg.profile_rows,value_area_pct=cfg.value_area_pct)
                    history=rolling_profiles(c15hist.tail(96),rows=cfg.profile_rows,
                        value_area_pct=cfg.value_area_pct,window=32,step=16)
                    regime=classify_regime(c15hist,c3hist,profile,history)
                    signals=generate_signals(symbol,c1hist,c3hist,c15hist,profile,regime,
                        cfg.min_reward_risk,cfg.volume_multiple)
                    if signals:
                        sig=signals[0];signal_counts[sig.strategy]+=1
                        pending={"signal":sig,"bars_waited":0,"regime":regime.regime.value}
                except ValueError:
                    skipped["profile_or_signal_error"]+=1

        # Mark open exposure to market and include the adverse intrabar excursion.
        # This improves drawdown visibility; it is not an exchange liquidation model.
        if position is not None:
            direction=position["direction"]
            qty=float(position["remaining"])*contract_multiplier
            entry=float(position["entry"])
            adverse_price=bar["low"] if direction=="LONG" else bar["high"]
            close_price=bar["close"]
            adverse_unrealized=((adverse_price-entry) if direction=="LONG" else (entry-adverse_price))*qty
            close_unrealized=((close_price-entry) if direction=="LONG" else (entry-close_price))*qty
            equity_curve.append(equity+min(adverse_unrealized,close_unrealized))
            equity_curve.append(equity+close_unrealized)
        else:
            equity_curve.append(equity)

    metrics=_metrics(cfg.initial_balance,trades,equity_curve)
    by_strategy={}
    for name in sorted({t["strategy"] for t in trades}):
        sub=[t for t in trades if t["strategy"]==name];curve=[cfg.initial_balance]
        for trade in sub:curve.append(curve[-1]+trade["net_pnl"])
        by_strategy[name]=_metrics(cfg.initial_balance,sub,curve)
    return {"symbol":symbol,"config":asdict(cfg),
        "contract_specs":{"contract_multiplier":contract_multiplier,"lot_size":lot_size,
            "min_qty":min_qty,"tick_size":tick_size,"position_size_limit":position_size_limit},
        "metrics":metrics,"strategy_metrics":by_strategy,
        "signals_detected":dict(signal_counts),"skipped":dict(skipped),"trades":trades,
        "equity_curve":equity_curve,"open_position_at_end":position,"pending_signal_at_end":pending,
        "limitations":["Volume Profile from historical OHLCV is a range-volume approximation, not historical trade-level volume-at-price.",
            "Historical funding rates are not included; PnL includes configured fees and slippage only.",
            "If stop and target touch in one 1m bar, stop is assumed first.",
            "OHLCV cannot model queue priority, latency or exchange partial fills."]}
