"""Three deterministic VPVR signal generators. Inputs must be completed, time-aligned candles."""
from __future__ import annotations
from dataclasses import dataclass,asdict
from datetime import datetime,timezone
import pandas as pd
from .profile import VolumeProfile
from .regime import MarketRegime,RegimeResult
from .indicators import volume_expansion

@dataclass(frozen=True)
class StrategySignal:
    symbol:str; strategy:str; direction:str; entry:float; stop_loss:float; targets:tuple[float,...]; risk_reward:float; timestamp:str; reasons:tuple[str,...]; invalidation:tuple[str,...]; setup_level:float
    def as_dict(self): return asdict(self)


def _valid(df:pd.DataFrame,n:int=8)->bool:
    return df is not None and len(df)>=n and {"time","open","high","low","close","volume"}.issubset(df.columns) and pd.to_datetime(df.time,utc=True).is_monotonic_increasing

def _rr(entry,stop,target,direction):
    risk=entry-stop if direction=="LONG" else stop-entry; reward=target-entry if direction=="LONG" else entry-target
    return reward/risk if risk>0 else 0.0

def _make(symbol,name,direction,entry,stop,targets,ts,why,invalid,level,min_rr=2.0):
    valid_targets=[float(t) for t in targets if (t>entry if direction=="LONG" else t<entry)]
    if not valid_targets: return None
    rr=max(_rr(entry,stop,t,direction) for t in valid_targets)
    if rr<min_rr: return None
    return StrategySignal(symbol,name,direction,float(entry),float(stop),tuple(valid_targets),float(rr),pd.Timestamp(ts).isoformat(),tuple(why),tuple(invalid),float(level))

def poc_reclaim(symbol,c1,c3,c15,p,regime,min_rr=2.0,volume_multiple=1.0):
    if not all(_valid(x,10) for x in (c1,c3,c15)): return None
    prev=c3.iloc[-2]; confirm=c3.iloc[-1]; retest=c1.iloc[-1]
    vm=volume_expansion(c3)
    if vm is None or vm<volume_multiple: return None
    if regime.regime not in (MarketRegime.BULLISH_TREND,MarketRegime.BEARISH_TREND,MarketRegime.RANGE): return None
    poc=float(p.poc)
    # Reclaim up: previous 3m close at/below POC, completed 3m closes above; latest 1m retests POC and closes bullish.
    if float(prev.close)<=poc<float(confirm.close) and float(retest.low)<=poc*1.001 and float(retest.close)>poc and float(retest.close)>float(retest.open) and regime.regime!=MarketRegime.BEARISH_TREND:
        entry=float(retest.high); stop=float(min(retest.low,float(c1.low.tail(4).min())))*(1-0.001)
        targets=[x for x in (p.vah,*p.hvns) if x>entry]
        return _make(symbol,"poc_reclaim","LONG",entry,stop,sorted(targets),retest.time,("3m POC reclaim","volume expansion","1m bullish POC retest"),("1m close below POC","15m regime turns bearish"),poc,min_rr)
    if float(prev.close)>=poc>float(confirm.close) and float(retest.high)>=poc*.999 and float(retest.close)<poc and float(retest.close)<float(retest.open) and regime.regime!=MarketRegime.BULLISH_TREND:
        entry=float(retest.low); stop=float(max(retest.high,float(c1.high.tail(4).max())))*(1+0.001)
        targets=[x for x in (p.val,*p.hvns) if x<entry]
        return _make(symbol,"poc_reclaim","SHORT",entry,stop,sorted(targets,reverse=True),retest.time,("3m POC breakdown","volume expansion","1m bearish POC retest"),("1m close above POC","15m regime turns bullish"),poc,min_rr)
    return None

def value_area_rejection(symbol,c1,c3,c15,p,regime,min_rr=2.0,volume_multiple=1.0):
    if not all(_valid(x,10) for x in (c1,c3,c15)) or regime.regime!=MarketRegime.RANGE: return None
    c=c1.iloc[-1]; vm=volume_expansion(c1)
    if vm is None or vm<volume_multiple: return None
    body=abs(float(c.close)-float(c.open)); lower=float(min(c.open,c.close))-float(c.low); upper=float(c.high)-float(max(c.open,c.close))
    if float(c.low)<=p.val*1.001 and float(c.close)>p.val and float(c.close)>float(c.open) and lower>body:
        entry=float(c.high); stop=float(c.low)*(1-0.001)
        return _make(symbol,"value_area_rejection","LONG",entry,stop,[p.poc,p.vah],c.time,("range regime","VAL rejection","volume confirmation"),("1m close below VAL","value area expands lower"),p.val,min_rr)
    if float(c.high)>=p.vah*.999 and float(c.close)<p.vah and float(c.close)<float(c.open) and upper>body:
        entry=float(c.low); stop=float(c.high)*(1+0.001)
        return _make(symbol,"value_area_rejection","SHORT",entry,stop,[p.poc,p.val],c.time,("range regime","VAH rejection","volume confirmation"),("1m close above VAH","value area expands higher"),p.vah,min_rr)
    return None

def lvn_breakout(symbol,c1,c3,c15,p,regime,min_rr=2.0,volume_multiple=1.0):
    if not all(_valid(x,10) for x in (c1,c3,c15)): return None
    c=c1.iloc[-1]; prev3=c3.iloc[-2]; now3=c3.iloc[-1]; vm=volume_expansion(c3)
    if vm is None or vm<volume_multiple or regime.regime not in (MarketRegime.BULLISH_TREND,MarketRegime.BEARISH_TREND,MarketRegime.HIGH_VOL): return None
    for level in p.lvns:
        level=float(level)
        if float(prev3.close)<=level<float(now3.close) and float(c.low)<=level*1.001 and float(c.close)>level and regime.regime!=MarketRegime.BEARISH_TREND:
            targets=[x for x in (p.poc,*p.hvns,p.vah) if x>float(c.high)]
            stop=float(c.low)*(1-.001)
            return _make(symbol,"lvn_breakout","LONG",float(c.high),stop,sorted(set(targets)),c.time,("3m close above LVN boundary","volume expansion","1m retest acceptance"),("1m close back below LVN","15m bias reverses"),level,min_rr)
        if float(prev3.close)>=level>float(now3.close) and float(c.high)>=level*.999 and float(c.close)<level and regime.regime!=MarketRegime.BULLISH_TREND:
            targets=[x for x in (p.poc,*p.hvns,p.val) if x<float(c.low)]
            stop=float(c.high)*(1+.001)
            return _make(symbol,"lvn_breakout","SHORT",float(c.low),stop,sorted(set(targets),reverse=True),c.time,("3m close below LVN boundary","volume expansion","1m retest rejection"),("1m close back above LVN","15m bias reverses"),level,min_rr)
    return None

def generate_signals(symbol,c1,c3,c15,p,regime,min_rr=2.0,volume_multiple=1.0):
    signals=[poc_reclaim(symbol,c1,c3,c15,p,regime,min_rr,volume_multiple),value_area_rejection(symbol,c1,c3,c15,p,regime,min_rr,volume_multiple),lvn_breakout(symbol,c1,c3,c15,p,regime,min_rr,volume_multiple)]
    return [s for s in signals if s is not None]
