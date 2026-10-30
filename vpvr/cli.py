"""VPVR CLI: diagnostics, signal scan, paper loop and historical backtests.

There is intentionally no live-order command. Paper trading is isolated from
Delta's private/authenticated trading client.
"""
from __future__ import annotations
import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
from .config import VPVRConfig
from .data import DeltaMarketData, MarketDataError
from .profile import build_volume_profile
from .engine import VPVREngine
from .risk import RiskEngine
from .paper import PaperBroker
from .backtest import run_backtest, BacktestConfig

logger=logging.getLogger("vpvr")


def _spread_from_book(book):
    bids=book.get("buy") or book.get("bids") or []
    asks=book.get("sell") or book.get("asks") or []
    def price(level):
        try:
            if isinstance(level,dict): return float(level.get("price"))
            return float(level[0])
        except (TypeError,ValueError,IndexError,KeyError): return None
    bid=max((x for x in (price(level) for level in bids) if x is not None),default=None)
    ask=min((x for x in (price(level) for level in asks) if x is not None),default=None)
    return bid,ask


def _best_quote(data, ticker, symbol):
    bid=ticker.get("best_bid") or ticker.get("bid")
    ask=ticker.get("best_ask") or ticker.get("ask")
    try:
        bid,ask=float(bid),float(ask)
    except (TypeError,ValueError):
        book=data.fetch_orderbook(symbol)
        bid,ask=_spread_from_book(book)
    if bid is None or ask is None or bid<=0 or ask<=bid:
        return None,None,None
    mid=(bid+ask)/2
    return bid,ask,(ask-bid)/mid*100


def _restore_risk(engine, broker, today):
    state=broker.state
    key=today.isoformat()
    if state.get("risk_day")!=key:
        state["risk_day"]=key
        state["risk_day_start_equity"]=broker.equity()
        broker._save()
    engine.reset_day(float(state["risk_day_start_equity"]),today)
    closed=sorted(state.get("trades",[]),key=lambda row:row.get("closed_at",""))
    for trade in closed:
        engine.consecutive_losses=engine.consecutive_losses+1 if float(trade.get("net_pnl",0))<0 else 0
    engine.daily_realized_pnl=sum(float(row.get("net_pnl",0)) for row in closed
                                   if str(row.get("closed_at","")).startswith(key))


def _process_new_paper_bars(broker, symbol, candles):
    last=broker.state.get("last_processed",{}).get(symbol)
    if last is None:
        new_rows=candles.tail(1)
    else:
        new_rows=candles.loc[candles.time>pd.Timestamp(last)]
    events=[]
    for row in new_rows.itertuples(index=False):
        candle={"time":row.time,"open":row.open,"high":row.high,"low":row.low,
                "close":row.close,"volume":row.volume}
        # Funding is charged only when an explicit timestamped settlement event
        # is supplied. A rate quote alone is not a realized funding event.
        events.extend(broker.process_candle(symbol,candle))
    return events


def scan_once(symbols, paper=False, config=None):
    cfg=config or VPVRConfig.from_env()
    data=DeltaMarketData(config=cfg)
    products={p["symbol"]:p for p in data.fetch_products()}
    tickers={t.get("symbol"):t for t in data.fetch_tickers() if t.get("symbol")}
    broker=PaperBroker(path=cfg.paper_state_file,initial_balance=cfg.initial_paper_balance,
                       fee_rate=cfg.fee_rate,slippage_bps=cfg.slippage_bps,
                       trailing_buffer_pct=cfg.stop_buffer_pct,max_hold_minutes=cfg.max_hold_minutes)
    risk=RiskEngine(cfg)
    today=datetime.now(timezone.utc).date()
    _restore_risk(risk,broker,today)
    output=[]

    for symbol in symbols:
        product=products.get(symbol)
        if product is None:
            output.append({"symbol":symbol,"status":"not_a_live_delta_perpetual"})
            continue
        specs=data.contract_specs(product)
        try:
            frames={tf:data.fetch_candles(symbol,tf,300) for tf in ("1m","3m","15m")}
            freshness={tf:data.freshness(frames[tf],tf) for tf in frames}
            if not all(ok for ok,_ in freshness.values()):
                output.append({"symbol":symbol,"status":"no_trade_stale_or_incomplete_data","freshness":freshness,
                    "contract_specs":specs})
                continue
            ticker=tickers.get(symbol,{})
            bid,ask,spread=_best_quote(data,ticker,symbol)
            if spread is None or spread>cfg.max_spread_pct:
                output.append({"symbol":symbol,"status":"no_trade_spread_unavailable_or_too_wide",
                    "bid":bid,"ask":ask,"spread_pct":spread,"max_spread_pct":cfg.max_spread_pct,
                    "contract_specs":specs})
                continue
            # Funding is an entry filter only. We require a fresh public FUNDING
            # candle; unavailable/stale funding is a no-trade condition.
            try:
                funding=data.fetch_current_funding_rate(symbol)
            except MarketDataError as exc:
                output.append({"symbol":symbol,"status":"no_trade_funding_rate_unavailable",
                    "reason":str(exc),"contract_specs":specs})
                continue
            if abs(funding)>cfg.max_abs_funding_rate:
                output.append({"symbol":symbol,"status":"no_trade_funding_rate_exceeded",
                    "funding_rate":funding,"max_abs_funding_rate":cfg.max_abs_funding_rate,
                    "contract_specs":specs})
                continue

            # Process fresh, unique completed 1m bars and reconcile any closes into risk state.
            trade_count_before=len(broker.state["trades"])
            paper_events=_process_new_paper_bars(broker,symbol,frames["1m"]) if paper else []
            if paper:
                for closed_trade in broker.state["trades"][trade_count_before:]:
                    risk.record_close(float(closed_trade.get("net_pnl",0.0)))
            # Mark every open paper position, not just the symbol currently scanned.
            # Missing tickers retain the last confirmed mark; they never default to zero.
            marks={}
            for open_symbol, position in broker.state["positions"].items():
                open_ticker=tickers.get(open_symbol,{})
                open_mark=open_ticker.get("mark_price") or open_ticker.get("close")
                try:
                    marks[open_symbol]=float(open_mark) if open_mark is not None else float(position.get("last_mark") or position["entry"])
                except (TypeError,ValueError):
                    marks[open_symbol]=float(position.get("last_mark") or position["entry"])
            current_mark=ticker.get("mark_price") or frames["1m"].close.iloc[-1]
            marks[symbol]=float(current_mark)
            equity=broker.equity(marks)
            risk.update_equity(equity,today)

            # Pure analysis orchestrator owns the profile/regime/strategy pipeline.
            try:
                trades=data.fetch_public_trades(symbol)
            except MarketDataError:
                trades=pd.DataFrame(columns=["time","price","volume","side"])
            analysis=VPVREngine(cfg).analyse(
                symbol,frames["1m"],frames["3m"],frames["15m"],trades
            )
            profile=analysis.profile
            regime=analysis.regime
            signals=list(analysis.signals)
            diagnostics=analysis.diagnostics
            tape_covers_window=bool(diagnostics["trade_tape_covers_profile_window"])
            cvd=diagnostics["cvd"]
            divergence=diagnostics["delta_divergence"]
            if specs is None:
                output.append({"symbol":symbol,"status":"contract_metadata_incomplete",
                    "profile":profile.as_dict(),"regime":regime.regime.value,
                    "signals":[s.as_dict() for s in signals],"paper_events":paper_events})
                continue

            signal_rows=[]
            if not specs["spec_verified"]:
                for signal in signals:
                    signal_rows.append({"signal":signal.as_dict(),
                        "status":"paper_blocked_unverified_contract_size" if paper else
                                 "signal_only_contract_size_unverified",
                        "reason":"Quantity is not computed without a confirmed exchange lot increment and minimum order quantity.",
                        "verification_errors":specs.get("verification_errors",[])})
                output.append({"symbol":symbol,"status":"contract_size_unverified",
                    "profile":profile.as_dict(),"regime":regime.regime.value,
                    "indicators":diagnostics,"spread_pct":spread,"funding_rate":funding,
                    "contract_specs":specs,"paper_events":paper_events,"signals":signal_rows})
                continue
            for signal in signals:
                if paper and (len(broker.state["positions"])+len(broker.state["pending"]))>=cfg.max_positions:
                    signal_rows.append({"signal":signal.as_dict(),"status":"max_positions_reached"})
                    continue
                decision=risk.size(equity=equity,entry=signal.entry,stop=signal.stop_loss,
                    direction=signal.direction,leverage=cfg.default_leverage,
                    contract_multiplier=specs["contract_multiplier"],lot_size=specs["lot_size"],
                    min_qty=specs["min_qty"],tick_size=specs["tick_size"],
                    fee_rate=cfg.fee_rate,slippage_bps=cfg.slippage_bps,
                    max_qty=specs.get("position_size_limit"))
                if not decision.accepted:
                    signal_rows.append({"signal":signal.as_dict(),"status":"risk_rejected","reason":decision.reason})
                    continue
                if paper:
                    queued,reason=broker.queue_signal(signal,decision.quantity,
                        contract_multiplier=specs["contract_multiplier"],regime=regime.regime.value)
                    signal_rows.append({"signal":signal.as_dict(),
                        "status":"paper_trigger_queued" if queued else "paper_rejected",
                        "reason":reason,"risk":decision.__dict__,
                        "contract_specs_verified":specs["spec_verified"]})
                else:
                    signal_rows.append({"signal":signal.as_dict(),"status":"signal_only",
                        "risk":decision.__dict__,"contract_specs_verified":specs["spec_verified"]})

            output.append({"symbol":symbol,"status":"ok",
                "regime":{"name":regime.regime.value,"confidence":regime.confidence,"reasons":regime.reasons},
                "profile":profile.as_dict(),"spread_pct":spread,"funding_rate_quote":funding,
                "funding_model_status":"fresh public funding rate used as entry filter; no timestamped settlement event supplied, so no funding cash flow was fabricated",
                "indicators":diagnostics,"cvd":cvd,"delta_divergence":divergence,
                "trade_tape_covers_profile_window":tape_covers_window,
                "contract_specs":specs,"paper_events":paper_events,"signals":signal_rows})
        except (MarketDataError,ValueError,KeyError) as exc:
            output.append({"symbol":symbol,"status":"data_or_signal_error","reason":str(exc)})
    return {"mode":"PAPER_ONLY" if paper else "SIGNAL_ONLY","exchange":"Delta Exchange India",
        "account_balance":broker.balance,"account_equity":broker.equity(),
        "open_paper_positions":len(broker.state["positions"]),
        "pending_paper_entries":len(broker.state["pending"]),"results":output}


def _load_csv(path):
    frame=pd.read_csv(path)
    if "time" not in frame:raise ValueError(f"{path} has no time column")
    return frame


def main(argv=None):
    parser=argparse.ArgumentParser(prog="vpvr",description="Delta VPVR scalping research and paper engine (no live orders)")
    subs=parser.add_subparsers(dest="command",required=True)
    subs.add_parser("config",help="print validated configuration")
    profile=subs.add_parser("profile",help="build a profile from an OHLCV CSV")
    profile.add_argument("csv")
    scan=subs.add_parser("scan",help="fetch complete Delta candles and print signals")
    scan.add_argument("--symbols",nargs="+",required=True)
    scan.add_argument("--paper",action="store_true",help="queue trigger entries in the isolated paper account")
    loop=subs.add_parser("paper-loop",help="continuously scan and paper trade; no live orders")
    loop.add_argument("--symbols",nargs="+",required=True)
    loop.add_argument("--interval-seconds",type=float,default=15.0)
    loop.add_argument("--iterations",type=int,default=0,help="0 runs until Ctrl-C")
    stream=subs.add_parser("stream",help="diagnose Delta public WebSocket market-data feed")
    stream.add_argument("--symbols",nargs="+",required=True)
    stream.add_argument("--duration-seconds",type=int,default=0)
    bt=subs.add_parser("backtest",help="event-driven backtest from three OHLCV CSVs")
    bt.add_argument("--symbol",required=True)
    bt.add_argument("--csv-1m",required=True);bt.add_argument("--csv-3m",required=True);bt.add_argument("--csv-15m",required=True)
    bt.add_argument("--initial-balance",type=float,default=1000)
    bt.add_argument("--contract-multiplier",type=float,default=1.0)
    bt.add_argument("--lot-size",type=float,default=1.0)
    bt.add_argument("--min-qty",type=float,default=1.0)
    bt.add_argument("--tick-size",type=float,default=None)
    bt.add_argument("--position-size-limit",type=float,default=None)
    bt.add_argument("--output",default="vpvr_backtest.json")
    args=parser.parse_args(argv)
    cfg=VPVRConfig.from_env()

    if args.command=="config":
        print(json.dumps(cfg.__dict__,indent=2));return 0
    if args.command=="profile":
        frame=_load_csv(args.csv)
        result=build_volume_profile(frame,rows=cfg.profile_rows,value_area_pct=cfg.value_area_pct,
            smoothing_window=cfg.node_smoothing_window,node_threshold_pct=cfg.node_threshold_pct)
        print(json.dumps(result.as_dict(),indent=2));return 0
    if args.command=="scan":
        print(json.dumps(scan_once(args.symbols,paper=args.paper,config=cfg),indent=2,default=str));return 0
    if args.command=="paper-loop":
        if args.interval_seconds<5:raise ValueError("paper polling interval must be at least five seconds")
        count=0
        try:
            while not args.iterations or count<args.iterations:
                result=scan_once(args.symbols,paper=True,config=cfg)
                print(json.dumps(result,default=str),flush=True)
                count+=1
                if args.iterations and count>=args.iterations:break
                time.sleep(args.interval_seconds)
        except KeyboardInterrupt:
            print("\nPaper loop stopped; virtual positions and pending entries remain persisted.",flush=True)
        return 0
    if args.command=="stream":
        from .stream import DeltaPublicStream
        stream=DeltaPublicStream(args.symbols);stream.start()
        started=time.time()
        try:
            while not args.duration_seconds or time.time()-started<args.duration_seconds:
                status=stream.status()
                status["buffered_events"]={symbol:len(stream.snapshot(symbol)) for symbol in args.symbols}
                print(json.dumps(status),flush=True);time.sleep(5)
        except KeyboardInterrupt:
            pass
        finally:
            stream.close()
        return 0
    if args.command=="backtest":
        result=run_backtest(args.symbol,_load_csv(args.csv_1m),_load_csv(args.csv_3m),_load_csv(args.csv_15m),
            BacktestConfig(initial_balance=args.initial_balance,profile_rows=cfg.profile_rows,
                value_area_pct=cfg.value_area_pct,min_reward_risk=cfg.min_reward_risk,
                volume_multiple=cfg.min_volume_multiple,fee_rate=cfg.fee_rate,
                slippage_bps=cfg.slippage_bps,leverage=cfg.default_leverage,
                risk_per_trade_pct=cfg.risk_per_trade_pct,
                trailing_buffer_pct=cfg.stop_buffer_pct,
                max_hold_minutes=cfg.max_hold_minutes),
            contract_multiplier=args.contract_multiplier,lot_size=args.lot_size,
            min_qty=args.min_qty,tick_size=args.tick_size,
            position_size_limit=args.position_size_limit)
        Path(args.output).parent.mkdir(parents=True,exist_ok=True)
        Path(args.output).write_text(json.dumps(result,indent=2,default=str),encoding="utf-8")
        print(json.dumps(result["metrics"],indent=2));print(f"Detailed report: {args.output}");return 0
    return 2

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
