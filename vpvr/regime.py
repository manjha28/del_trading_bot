"""Conservative regime classification using multi-timeframe structure, VWAP and profile evolution."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import pandas as pd
from .indicators import ema, session_vwap

class MarketRegime(str, Enum):
    BULLISH_TREND = "bullish_trending"
    BEARISH_TREND = "bearish_trending"
    RANGE = "range_bound"
    HIGH_VOL = "high_volatility"
    LOW_VOL = "low_volatility"
    UNCERTAIN = "uncertain_no_trade"

@dataclass(frozen=True)
class RegimeResult:
    regime: MarketRegime
    confidence: float
    reasons: tuple[str, ...]


def classify_regime(c15: pd.DataFrame, c3: pd.DataFrame, profile=None, profile_history=None) -> RegimeResult:
    if len(c15) < 30 or len(c3) < 30:
        return RegimeResult(MarketRegime.UNCERTAIN, 0.0, ("insufficient completed higher-timeframe candles",))
    e15 = ema(c15.close, 20)
    e3 = ema(c3.close, 20)
    if pd.isna(e15.iloc[-1]) or pd.isna(e3.iloc[-1]) or pd.isna(e15.iloc[-4]):
        return RegimeResult(MarketRegime.UNCERTAIN, 0.0, ("EMA warmup incomplete",))
    last15, last3 = c15.iloc[-1], c3.iloc[-1]
    atr_pct = float((c15.high - c15.low).tail(14).mean() / max(float(last15.close), 1e-12))
    if atr_pct >= 0.035:
        return RegimeResult(MarketRegime.HIGH_VOL, min(1.0, atr_pct / 0.05), ("15m realized range volatility elevated",))
    if atr_pct <= 0.0015:
        return RegimeResult(MarketRegime.LOW_VOL, 0.8, ("15m realized range volatility compressed",))
    vwap = float(session_vwap(c15).iloc[-1])
    poc_rising = poc_falling = value_expanding = False
    if profile_history and len(profile_history) >= 2:
        first, latest = profile_history[0], profile_history[-1]
        poc_rising = latest.poc > first.poc * 1.001
        poc_falling = latest.poc < first.poc * 0.999
        first_width = first.vah - first.val
        latest_width = latest.vah - latest.val
        value_expanding = first_width > 0 and latest_width > first_width * 1.1
    bull = float(last15.close) > float(e15.iloc[-1]) and float(last3.close) > float(e3.iloc[-1]) and float(e15.iloc[-1]) > float(e15.iloc[-4]) and float(last15.close) >= vwap
    bear = float(last15.close) < float(e15.iloc[-1]) and float(last3.close) < float(e3.iloc[-1]) and float(e15.iloc[-1]) < float(e15.iloc[-4]) and float(last15.close) <= vwap
    if bull and (poc_rising or profile_history is None or len(profile_history) < 2):
        return RegimeResult(MarketRegime.BULLISH_TREND, 0.75, ("15m/3m EMA20 bullish structure", "15m close above session VWAP", "POC rising or unavailable"))
    if bear and (poc_falling or profile_history is None or len(profile_history) < 2):
        return RegimeResult(MarketRegime.BEARISH_TREND, 0.75, ("15m/3m EMA20 bearish structure", "15m close below session VWAP", "POC falling or unavailable"))
    if profile is not None and float(profile.val) <= float(last15.close) <= float(profile.vah) and not value_expanding:
        return RegimeResult(MarketRegime.RANGE, 0.65, ("price inside value area", "value area not expanding materially"))
    if abs(float(last15.close) - vwap) / max(vwap, 1e-12) < 0.002 and not value_expanding:
        return RegimeResult(MarketRegime.RANGE, 0.5, ("price rotating near session VWAP",))
    return RegimeResult(MarketRegime.UNCERTAIN, 0.25, ("timeframes, VWAP or developing profile are not aligned",))
