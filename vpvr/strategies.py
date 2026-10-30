"""Three VPVR strategy detectors with completed-candle sequencing and target hierarchy."""
from __future__ import annotations
from dataclasses import dataclass, asdict
import pandas as pd
from .regime import MarketRegime
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

def _valid(frame, minimum=10):
    required={"time","open","high","low","close","volume"}
    return frame is not None and len(frame)>=minimum and required.issubset(frame.columns) and pd.to_datetime(frame.time,utc=True).is_monotonic_increasing

def _after_3m_confirmation(c1,c3):
    three_min_close=pd.Timestamp(c3.time.iloc[-1])+pd.Timedelta(minutes=3)
    return pd.Timestamp(c1.time.iloc[-1])>=three_min_close

def _rr(entry,stop,target,direction):
    risk=entry-stop if direction=="LONG" else stop-entry
    reward=target-entry if direction=="LONG" else entry-target
    return reward/risk if risk>0 else 0.0

def _make(symbol,name,direction,entry,stop,targets,timestamp,reasons,invalidation,level,min_rr=2.0):
    ordered=sorted({float(x) for x in targets if (float(x)>entry if direction=="LONG" else float(x)<entry)},reverse=(direction=="SHORT"))[:3]
    if not ordered:return None
    rr=_rr(entry,stop,ordered[0],direction)
    if rr<min_rr:return None
    return StrategySignal(symbol,name,direction,float(entry),float(stop),tuple(ordered),float(rr),pd.Timestamp(timestamp).isoformat(),tuple(reasons),tuple(invalidation),float(level))

def _micro_context(c1):
    e=ema(c1.close,20);v=session_vwap(c1)
    if pd.isna(e.iloc[-1]) or pd.isna(v.iloc[-1]):return None,None
    return float(e.iloc[-1]),float(v.iloc[-1])

def poc_reclaim(symbol,c1,c3,c15,profile,regime,min_rr=2.0,volume_multiple=1.0):
    if not all(_valid(x) for x in (c1,c3,c15)) or not _after_3m_confirmation(c1,c3):return None
    previous,confirmation,retest=c3.iloc[-2],c3.iloc[-1],c1.iloc[-1]
    expansion=volume_expansion(c3)
    if expansion is None or expansion<volume_multiple:return None
    if regime.regime not in (MarketRegime.BULLISH_TREND,MarketRegime.BEARISH_TREND,MarketRegime.RANGE):return None
    e20,vwap=_micro_context(c1)
    if e20 is None:return None
    poc=float(profile.poc)
    if float(previous.close)<=poc<float(confirmation.close) and float(retest.low)<=poc*1.001 and float(retest.close)>poc and float(retest.close)>float(retest.open) and float(retest.close)>=e20 and float(retest.close)>=vwap and regime.regime!=MarketRegime.BEARISH_TREND:
        entry=float(retest.high);stop=min(float(retest.low),float(c1.low.tail(4).min()))*(1-0.001)
        targets=[x for x in (*profile.hvns,profile.vah) if x>entry]
        return _make(symbol,"poc_reclaim","LONG",entry,stop,targets,retest.time,("completed 3m close reclaimed POC","3m volume expanded vs previous five completed bars","subsequent 1m bullish POC retest","1m close above EMA20 and session VWAP"),("1m close below POC","15m regime turns bearish"),poc,min_rr)
    if float(previous.close)>=poc>float(confirmation.close) and float(retest.high)>=poc*.999 and float(retest.close)<poc and float(retest.close)<float(retest.open) and float(retest.close)<=e20 and float(retest.close)<=vwap and regime.regime!=MarketRegime.BULLISH_TREND:
        entry=float(retest.low);stop=max(float(retest.high),float(c1.high.tail(4).max()))*(1+0.001)
        targets=[x for x in (*profile.hvns,profile.val) if x<entry]
        return _make(symbol,"poc_reclaim","SHORT",entry,stop,targets,retest.time,("completed 3m close lost POC","3m volume expanded vs previous five completed bars","subsequent 1m bearish POC retest","1m close below EMA20 and session VWAP"),("1m close above POC","15m regime turns bullish"),poc,min_rr)
    return None

def value_area_rejection(symbol,c1,c3,c15,profile,regime,min_rr=2.0,volume_multiple=1.0):
    if not all(_valid(x) for x in (c1,c3,c15)) or regime.regime!=MarketRegime.RANGE:return None
    candle=c1.iloc[-1];expansion=volume_expansion(c1)
    if expansion is None or expansion<volume_multiple:return None
    e20,vwap=_micro_context(c1)
    if e20 is None:return None
    body=abs(float(candle.close)-float(candle.open));lower=float(min(candle.open,candle.close))-float(candle.low);upper=float(candle.high)-float(max(candle.open,candle.close))
    near_vwap=abs(float(candle.close)-vwap)/max(vwap,1e-12)<=0.003
    if float(candle.low)<=profile.val*1.001 and float(candle.close)>profile.val and float(candle.close)>float(candle.open) and lower>body and (float(candle.close)>=vwap or near_vwap) and float(candle.close)>=e20:
        return _make(symbol,"value_area_rejection","LONG",float(candle.high),float(candle.low)*(1-0.001),[profile.poc,profile.vah],candle.time,("range regime","VAL rejection wick","1m volume expansion","EMA20/VWAP context non-adverse"),("1m close below VAL","15m value area expands lower"),profile.val,min_rr)
    if float(candle.high)>=profile.vah*.999 and float(candle.close)<profile.vah and float(candle.close)<float(candle.open) and upper>body and (float(candle.close)<=vwap or near_vwap) and float(candle.close)<=e20:
        return _make(symbol,"value_area_rejection","SHORT",float(candle.low),float(candle.high)*(1+0.001),[profile.poc,profile.val],candle.time,("range regime","VAH rejection wick","1m volume expansion","EMA20/VWAP context non-adverse"),("1m close above VAH","15m value area expands higher"),profile.vah,min_rr)
    return None

def lvn_breakout(symbol,c1,c3,c15,profile,regime,min_rr=2.0,volume_multiple=1.0):
    if not all(_valid(x) for x in (c1,c3,c15)) or not _after_3m_confirmation(c1,c3):return None
    candle=c1.iloc[-1];previous,confirmation=c3.iloc[-2],c3.iloc[-1];expansion=volume_expansion(c3)
    if expansion is None or expansion<volume_multiple or regime.regime not in (MarketRegime.BULLISH_TREND,MarketRegime.BEARISH_TREND,MarketRegime.HIGH_VOL):return None
    e20,vwap=_micro_context(c1)
    if e20 is None:return None
    for boundary in profile.lvns:
        level=float(boundary)
        if float(previous.close)<=level<float(confirmation.close) and float(candle.low)<=level*1.001 and float(candle.close)>level and float(candle.close)>=e20 and float(candle.close)>=vwap and regime.regime!=MarketRegime.BEARISH_TREND:
            targets=[x for x in (*profile.hvns,profile.vah) if x>float(candle.high)]
            return _make(symbol,"lvn_breakout","LONG",float(candle.high),float(candle.low)*(1-0.001),targets,candle.time,("completed 3m close above LVN","3m volume expansion","subsequent 1m retest acceptance","price above EMA20/session VWAP"),("1m close below LVN","15m bias reverses"),level,min_rr)
        if float(previous.close)>=level>float(confirmation.close) and float(candle.high)>=level*.999 and float(candle.close)<level and float(candle.close)<=e20 and float(candle.close)<=vwap and regime.regime!=MarketRegime.BULLISH_TREND:
            targets=[x for x in (*profile.hvns,profile.val) if x<float(candle.low)]
            return _make(symbol,"lvn_breakout","SHORT",float(candle.low),float(candle.high)*(1+0.001),targets,candle.time,("completed 3m close below LVN","3m volume expansion","subsequent 1m retest rejection","price below EMA20/session VWAP"),("1m close above LVN","15m bias reverses"),level,min_rr)
    return None

def generate_signals(symbol,c1,c3,c15,profile,regime,min_rr=2.0,volume_multiple=1.0):
    candidates=[poc_reclaim(symbol,c1,c3,c15,profile,regime,min_rr,volume_multiple),value_area_rejection(symbol,c1,c3,c15,profile,regime,min_rr,volume_multiple),lvn_breakout(symbol,c1,c3,c15,profile,regime,min_rr,volume_multiple)]
    return [signal for signal in candidates if signal is not None]
