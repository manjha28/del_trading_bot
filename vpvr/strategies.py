"""Three deterministic VPVR strategies. All candles passed in must be complete and aligned."""
from __future__ import annotations
from dataclasses import dataclass, asdict
import pandas as pd
from .profile import VolumeProfile
from .regime import MarketRegime, RegimeResult
from .indicators import ema, session_vwap, volume_expansion

@dataclass(frozen=True)
class StrategySignal:
    symbol: str
    strategy: str
    direction: str
    entry: float
    stop_loss: float
    targets: tuple[float, ...]
    risk_reward: float
    timestamp: str
    reasons: tuple[str, ...]
    invalidation: tuple[str, ...]
    setup_level: float
    expiry_candles: int = 5
    def as_dict(self): return asdict(self)

def _valid(df, n=10):
    return df is not None and len(df)>=n and {"time","open","high","low","close","volume"}.issubset(df.columns) and pd.to_datetime(df.time, utc=True).is_monotonic_increasing

def _after_3m_confirmation(c1, c3):
    # The triggering 1m candle must start at or after the already-completed 3m candle close.
    end = pd.Timestamp(c3.time.iloc[-1]) + pd.Timedelta(minutes=3)
    return pd.Timestamp(c1.time.iloc[-1]) >= end

def _rr(entry, stop, target, direction):
    risk = entry-stop if direction=="LONG" else stop-entry
    reward = target-entry if direction=="LONG" else entry-target
    return reward/risk if risk>0 else 0.0

def _make(symbol,name,direction,entry,stop,targets,ts,why,invalid,level,min_rr=2.0):
    targets=sorted({float(t) for t in targets if (t>entry if direction=="LONG" else t<entry)}, reverse=(direction=="SHORT"))
    if not targets: return None
    rr=max(_rr(entry,stop,t,direction) for t in targets)
    if rr < min_rr: return None
    return StrategySignal(symbol,name,direction,float(entry),float(stop),tuple(targets),float(rr),pd.Timestamp(ts).isoformat(),tuple(why),tuple(invalid),float(level))

def _micro_context(c1):
    e=ema(c1.close,20)
    v=session_vwap(c1)
    return float(e.iloc[-1]), float(v.iloc[-1])

def poc_reclaim(symbol,c1,c3,c15,p,regime,min_rr=2.0,volume_multiple=1.0):
    if not all(_valid(x) for x in (c1,c3,c15)) or not _after_3m_confirmation(c1,c3): return None
    prev, confirm, retest = c3.iloc[-2], c3.iloc[-1], c1.iloc[-1]
    vm=volume_expansion(c3)
    if vm is None or vm<volume_multiple: return None
    if regime.regime not in (MarketRegime.BULLISH_TREND,MarketRegime.BEARISH_TREND,MarketRegime.RANGE): return None
    e20,vwap=_micro_context(c1); poc=float(p.poc)
    if float(prev.close)<=poc<float(confirm.close) and float(retest.low)<=poc*1.001 and float(retest.close)>poc and float(retest.close)>float(retest.open) and float(retest.close)>=e20 and float(retest.close)>=vwap and regime.regime!=MarketRegime.BEARISH_TREND:
        entry=float(retest.high); stop=float(min(retest.low,float(c1.low.tail(4).min())))*(1-0.001)
        targets=[x for x in (p.vah,*p.hvns) if x>entry]
        return _make(symbol,"poc_reclaim","LONG",entry,stop,targets,retest.time,("completed 3m close reclaimed POC","3m volume expansion","subsequent 1m bullish retest","1m close above EMA20 and session VWAP"),("1m close below POC","15m regime becomes bearish"),poc,min_rr)
    if float(prev.close)>=poc>float(confirm.close) and float(retest.high)>=poc*.999 and float(retest.close)<poc and float(retest.close)<float(retest.open) and float(retest.close)<=e20 and float(retest.close)<=vwap and regime.regime!=MarketRegime.BULLISH_TREND:
        entry=float(retest.low); stop=float(max(retest.high,float(c1.high.tail(4).max())))*(1+0.001)
        targets=[x for x in (p.val,*p.lvns) if x<entry]
        return _make(symbol,"poc_reclaim","SHORT",entry,stop,targets,retest.time,("completed 3m close lost POC","3m volume expansion","subsequent 1m bearish retest","1m close below EMA20 and session VWAP"),("1m close above POC","15m regime becomes bullish"),poc,min_rr)
    return None

def value_area_rejection(symbol,c1,c3,c15,p,regime,min_rr=2.0,volume_multiple=1.0):
    if not all(_valid(x) for x in (c1,c3,c15)) or regime.regime!=MarketRegime.RANGE: return None
    c=c1.iloc[-1]; vm=volume_expansion(c1)
    if vm is None or vm<volume_multiple: return None
    e20,vwap=_micro_context(c1)
    body=abs(float(c.close)-float(c.open)); lower=float(min(c.open,c.close))-float(c.low); upper=float(c.high)-float(max(c.open,c.close))
    vwap_near=abs(float(c.close)-vwap)/max(vwap,1e-12)<=0.003
    if float(c.low)<=p.val*1.001 and float(c.close)>p.val and float(c.close)>float(c.open) and lower>body and (float(c.close)>=vwap or vwap_near) and float(c.close)>=e20:
        entry=float(c.high); stop=float(c.low)*(1-0.001)
        return _make(symbol,"value_area_rejection","LONG",entry,stop,[p.poc,p.vah],c.time,("range regime confirmed","VAL rejection wick","volume expansion","VWAP/EMA20 context non-adverse"),("1m close below VAL","15m value area expands lower"),p.val,min_rr)
    if float(c.high)>=p.vah*.999 and float(c.close)<p.vah and float(c.close)<float(c.open) and upper>body and (float(c.close)<=vwap or vwap_near) and float(c.close)<=e20:
        entry=float(c.low); stop=float(c.high)*(1+0.001)
        return _make(symbol,"value_area_rejection","SHORT",entry,stop,[p.poc,p.val],c.time,("range regime confirmed","VAH rejection wick","volume expansion","VWAP/EMA20 context non-adverse"),("1m close above VAH","15m value area expands higher"),p.vah,min_rr)
    return None

def lvn_breakout(symbol,c1,c3,c15,p,regime,min_rr=2.0,volume_multiple=1.0):
    if not all(_valid(x) for x in (c1,c3,c15)) or not _after_3m_confirmation(c1,c3): return None
    c=c1.iloc[-1]; prev3=c3.iloc[-2]; now3=c3.iloc[-1]; vm=volume_expansion(c3)
    if vm is None or vm<volume_multiple or regime.regime not in (MarketRegime.BULLISH_TREND,MarketRegime.BEARISH_TREND,MarketRegime.HIGH_VOL): return None
    e20,vwap=_micro_context(c1)
    for level in p.lvns:
        level=float(level)
        if float(prev3.close)<=level<float(now3.close) and float(c.low)<=level*1.001 and float(c.close)>level and float(c.close)>=e20 and float(c.close)>=vwap and regime.regime!=MarketRegime.BEARISH_TREND:
            stop=float(c.low)*(1-.001); targets=[x for x in (p.poc,*p.hvns,p.vah) if x>float(c.high)]
            return _make(symbol,"lvn_breakout","LONG",float(c.high),stop,targets,c.time,("completed 3m close above LVN boundary","3m volume expansion","subsequent 1m retest acceptance","price above EMA20 and session VWAP"),("1m close below LVN","15m bias reverses"),level,min_rr)
        if float(prev3.close)>=level>float(now3.close) and float(c.high)>=level*.999 and float(c.close)<level and float(c.close)<=e20 and float(c.close)<=vwap and regime.regime!=MarketRegime.BULLISH_TREND:
            stop=float(c.high)*(1+.001); targets=[x for x in (p.poc,*p.lvns,p.val) if x<float(c.low)]
            return _make(symbol,"lvn_breakout","SHORT",float(c.low),stop,targets,c.time,("completed 3m close below LVN boundary","3m volume expansion","subsequent 1m retest rejection","price below EMA20 and session VWAP"),("1m close above LVN","15m bias reverses"),level,min_rr)
    return None

def generate_signals(symbol,c1,c3,c15,p,regime,min_rr=2.0,volume_multiple=1.0):
    signals=[poc_reclaim(symbol,c1,c3,c15,p,regime,min_rr,volume_multiple),value_area_rejection(symbol,c1,c3,c15,p,regime,min_rr,volume_multiple),lvn_breakout(symbol,c1,c3,c15,p,regime,min_rr,volume_multiple)]
    return [s for s in signals if s is not None]
