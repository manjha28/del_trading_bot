"""Persistent deterministic paper trading. This module contains no exchange order API calls."""
from __future__ import annotations
from dataclasses import dataclass,asdict
from pathlib import Path
import json,os,tempfile
import pandas as pd

@dataclass
class PaperPosition:
    symbol:str; strategy:str; direction:str; entry:float; stop:float; targets:list[float]; quantity:float; remaining:float; opened_at:str; entry_fee:float; slippage:float; target_index:int=0; realized_pnl:float=0.0

class PaperBroker:
    def __init__(self,path:str="data/vpvr_paper_state.json",initial_balance:float=1000,fee_rate:float=.0005,slippage_bps:float=2): self.path=Path(path); self.initial_balance=initial_balance; self.fee_rate=fee_rate; self.slippage_bps=slippage_bps; self.state=self._load()
    def _load(self):
        if self.path.exists():
            try: return json.loads(self.path.read_text())
            except (OSError,json.JSONDecodeError): raise RuntimeError("paper state file is corrupt; refusing to reset account")
        return {"balance":self.initial_balance,"positions":{},"trades":[],"mode":"PAPER_ONLY"}
    def _save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True); fd,tmp=tempfile.mkstemp(dir=self.path.parent,prefix="vpvr-")
        try:
            with os.fdopen(fd,"w") as f: json.dump(self.state,f,indent=2)
            os.replace(tmp,self.path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    def open_position(self,signal,quantity:float)->dict:
        symbol=signal.symbol
        if symbol in self.state["positions"]: raise ValueError("paper position already exists for symbol")
        slip=self.slippage_bps/10000; fill=signal.entry*(1+slip if signal.direction=="LONG" else 1-slip); fee=abs(fill*quantity)*self.fee_rate
        pos=PaperPosition(symbol,signal.strategy,signal.direction,fill,signal.stop_loss,list(signal.targets),quantity,quantity,signal.timestamp,fee,slip)
        self.state["balance"]-=fee; self.state["positions"][symbol]=asdict(pos); self._save(); return self.state["positions"][symbol]
    def mark(self,symbol:str,candle:dict)->dict|None:
        pos=self.state["positions"].get(symbol)
        if not pos:return None
        high=float(candle["high"]); low=float(candle["low"]); close=float(candle["close"]); direction=pos["direction"]
        stop_hit=low<=pos["stop"] if direction=="LONG" else high>=pos["stop"]
        if stop_hit:
            fill=pos["stop"]*(1-self.slippage_bps/10000 if direction=="LONG" else 1+self.slippage_bps/10000)
            pnl=(fill-pos["entry"])*pos["remaining"] if direction=="LONG" else (pos["entry"]-fill)*pos["remaining"]
            fee=abs(fill*pos["remaining"])*self.fee_rate; pnl-=fee; self._close(symbol,fill,pnl,"STOP",fee); return {"closed":True,"reason":"STOP","pnl":pnl}
        while pos["target_index"]<len(pos["targets"]):
            target=float(pos["targets"][pos["target_index"]]); hit=high>=target if direction=="LONG" else low<=target
            if not hit: break
            fraction=.30 if pos["target_index"]==0 else (.45 if pos["target_index"]==1 else 1.0)
            qty=min(pos["remaining"],pos["quantity"]*fraction); pnl=(target-pos["entry"])*qty if direction=="LONG" else (pos["entry"]-target)*qty
            fee=abs(target*qty)*self.fee_rate; pnl-=fee; pos["remaining"]-=qty; pos["realized_pnl"]+=pnl; pos["target_index"]+=1; self.state["balance"]+=pnl
            if pos["target_index"]==1: pos["stop"]=pos["entry"]
            if pos["remaining"]<=1e-12: self.state["trades"].append({**pos,"exit_reason":"TARGETS","exit_price":target,"net_pnl":pos["realized_pnl"]}); self.state["positions"].pop(symbol,None); self._save(); return {"closed":True,"reason":"TARGETS","pnl":pos["realized_pnl"]}
        self._save(); return {"closed":False,"mark":close,"unrealized_pnl":(close-pos["entry"])*pos["remaining"] if direction=="LONG" else (pos["entry"]-close)*pos["remaining"]}
    def _close(self,symbol,price,pnl,reason,fee):
        pos=self.state["positions"].pop(symbol); total=pos["realized_pnl"]+pnl; self.state["balance"]+=pnl; self.state["trades"].append({**pos,"exit_price":price,"exit_reason":reason,"exit_fee":fee,"net_pnl":total}); self._save()
