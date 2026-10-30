"""Conservative market-regime classification using completed 15m and 3m candles."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import pandas as pd
from .indicators import ema

class MarketRegime(str,Enum):
    BULLISH_TREND="bullish_trending"; BEARISH_TREND="bearish_trending"; RANGE="range_bound"; HIGH_VOL="high_volatility"; LOW_VOL="low_volatility"; UNCERTAIN="uncertain_no_trade"

@dataclass(frozen=True)
class RegimeResult:
    regime: MarketRegime
    confidence: float
    reasons: tuple[str,...]


def classify_regime(c15: pd.DataFrame, c3: pd.DataFrame, profile=None) -> RegimeResult:
    if len(c15)<25 or len(c3)<25: return RegimeResult(MarketRegime.UNCERTAIN,0.0,("insufficient completed candles",))
    a=c15.iloc[-1]; b=c3.iloc[-1]
    e15=ema(c15.close,20).iloc[-1]; e3=ema(c3.close,20).iloc[-1]
    if pd.isna(e15) or pd.isna(e3): return RegimeResult(MarketRegime.UNCERTAIN,0.0,("EMA warmup incomplete",))
    r15=float(c15.high.tail(20).max()-c15.low.tail(20).min())/float(c15.close.iloc[-1])
    atr_pct=float((c15.high-c15.low).tail(14).mean()/c15.close.iloc[-1])
    reasons=[]
    if atr_pct>0.035: return RegimeResult(MarketRegime.HIGH_VOL,min(1,atr_pct/.05),("15m range volatility elevated",))
    if atr_pct<0.0015: return RegimeResult(MarketRegime.LOW_VOL,.8,("15m range volatility compressed",))
    bull=float(a.close)>float(e15) and float(b.close)>float(e3) and e15>float(ema(c15.close,20).iloc[-4])
    bear=float(a.close)<float(e15) and float(b.close)<float(e3) and e15<float(ema(c15.close,20).iloc[-4])
    if bull: return RegimeResult(MarketRegime.BULLISH_TREND,.7,("15m and 3m above EMA20",))
    if bear: return RegimeResult(MarketRegime.BEARISH_TREND,.7,("15m and 3m below EMA20",))
    if profile is not None and float(profile.val)<=float(b.close)<=float(profile.vah):
        return RegimeResult(MarketRegime.RANGE,.6,("price is inside value area",))
    if r15<0.025: return RegimeResult(MarketRegime.RANGE,.5,("15m range remains contained",))
    return RegimeResult(MarketRegime.UNCERTAIN,.25,("timeframes disagree or no clean regime",))
