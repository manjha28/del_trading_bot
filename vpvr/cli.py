"""Command line entry point for VPVR profile diagnostics and paper-only scans."""
from __future__ import annotations
import argparse,json
from .config import VPVRConfig
from .data import DeltaMarketData
from .profile import build_volume_profile
from .regime import classify_regime
from .strategies import generate_signals
from .risk import RiskEngine
from .paper import PaperBroker

def main(argv=None):
    p=argparse.ArgumentParser(prog="vpvr",description="VPVR perpetual scalping research and paper engine")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("config",help="print validated configuration")
    scan=sub.add_parser("scan",help="fetch completed Delta candles and print signals (no live orders)"); scan.add_argument("--symbols",nargs="+",required=True); scan.add_argument("--paper",action="store_true",help="persist candidate paper positions")
    bt=sub.add_parser("profile",help="build profile from a CSV with time,open,high,low,close,volume"); bt.add_argument("csv")
    args=p.parse_args(argv); cfg=VPVRConfig.from_env()
    if args.command=="config": print(json.dumps(cfg.__dict__,indent=2)); return 0
    if args.command=="profile":
        import pandas as pd
        frame=pd.read_csv(args.csv); prof=build_volume_profile(frame,rows=cfg.profile_rows,value_area_pct=cfg.value_area_pct); print(json.dumps(prof.as_dict(),indent=2)); return 0
    data=DeltaMarketData(config=cfg); products={p["symbol"]:p for p in data.fetch_products()}; tickers={t.get("symbol"):t for t in data.fetch_tickers()}; engine=RiskEngine(cfg); broker=PaperBroker(initial_balance=cfg.initial_paper_balance,fee_rate=cfg.fee_rate,slippage_bps=cfg.slippage_bps)
    out=[]
    for symbol in args.symbols:
        product=products.get(symbol)
        if not product: out.append({"symbol":symbol,"status":"not a live Delta perpetual"}); continue
        candles={tf:data.fetch_candles(symbol,tf,300) for tf in ("1m","3m","15m")}; states={tf:data.freshness(candles[tf],tf) for tf in candles}
        if not all(x[0] for x in states.values()): out.append({"symbol":symbol,"status":"no_trade","data":states}); continue
        pframe=candles["15m"].tail(96); profile=build_volume_profile(pframe,rows=cfg.profile_rows,value_area_pct=cfg.value_area_pct,node_threshold_pct=cfg.node_threshold_pct,smoothing_window=cfg.node_smoothing_window)
        regime=classify_regime(candles["15m"],candles["3m"],profile); signals=generate_signals(symbol,candles["1m"],candles["3m"],candles["15m"],profile,regime,cfg.min_reward_risk,cfg.min_volume_multiple)
        for sig in signals:
            specs=data.contract_specs(product)
            if specs is None: out.append({"symbol":symbol,"status":"signal_rejected","reason":"unknown contract specs"}); continue
            decision=engine.size(equity=broker.state["balance"],entry=sig.entry,stop=sig.stop_loss,direction=sig.direction,leverage=cfg.default_leverage,contract_multiplier=specs["contract_multiplier"],lot_size=specs["lot_size"],min_qty=specs["min_qty"],tick_size=specs["tick_size"],fee_rate=cfg.fee_rate,slippage_bps=cfg.slippage_bps)
            if not decision.accepted: out.append({"symbol":symbol,"status":"risk_rejected","reason":decision.reason}); continue
            if args.paper and len(broker.state["positions"])<cfg.max_positions: broker.open_position(sig,decision.quantity); out.append({"symbol":symbol,"status":"paper_opened","signal":sig.as_dict(),"risk":decision.__dict__})
            else: out.append({"symbol":symbol,"status":"signal_only","signal":sig.as_dict(),"risk":decision.__dict__})
    print(json.dumps(out,indent=2,default=str)); return 0

if __name__=="__main__": raise SystemExit(main())
