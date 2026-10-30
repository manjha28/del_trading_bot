"""Candle and trade-print indicators. Aggressor CVD is never inferred from candle colour."""
from __future__ import annotations
import pandas as pd


def ema(series: pd.Series, length: int=20) -> pd.Series:
    if length<1: raise ValueError("EMA length must be positive")
    return series.ewm(span=length,adjust=False,min_periods=length).mean()


def session_vwap(candles: pd.DataFrame) -> pd.Series:
    if not {"time","high","low","close","volume"}.issubset(candles): raise ValueError("VWAP requires time/high/low/close/volume")
    out=pd.Series(index=candles.index,dtype=float); ts=pd.to_datetime(candles.time,utc=True); key=ts.dt.strftime("%Y-%m-%d")
    typical=(candles.high+candles.low+candles.close)/3.0; pv=typical*candles.volume
    for day in key.drop_duplicates():
        mask=key==day; den=candles.loc[mask,"volume"].cumsum().replace(0,float("nan")); out.loc[mask]=pv.loc[mask].cumsum()/den
    return out


def volume_expansion(candles: pd.DataFrame, lookback: int=5) -> float | None:
    if "volume" not in candles or len(candles)<lookback+1: return None
    prev=pd.to_numeric(candles.volume.iloc[-lookback-1:-1],errors="coerce").mean(); current=float(candles.volume.iloc[-1])
    if pd.isna(prev) or prev<=0: return None
    return current/float(prev)


def cumulative_volume_delta(trades: pd.DataFrame | None) -> dict:
    if trades is None or trades.empty or not {"volume","side"}.issubset(trades.columns):
        return {"available":False,"value":None,"reason":"Aggressor-side trade prints are not available from the current Delta adapter."}
    side=trades.side.astype(str).str.lower(); vol=pd.to_numeric(trades.volume,errors="coerce").fillna(0)
    if not side.isin(["buy","sell","buyer","seller","buy_aggressor","sell_aggressor"]).all():
        return {"available":False,"value":None,"reason":"Trade side values are not recognized as aggressor-side labels."}
    buy=side.isin(["buy","buyer","buy_aggressor"]); return {"available":True,"value":float(vol[buy].sum()-vol[~buy].sum()),"reason":None}
