"""Indicators for VPVR. CVD only uses explicit buyer/seller aggressor labels from Delta REST trades."""
from __future__ import annotations
import pandas as pd

def ema(series,length=20):
    if length<1: raise ValueError("EMA length must be positive")
    return series.ewm(span=length,adjust=False,min_periods=length).mean()
def session_vwap(candles):
    required={"time","high","low","close","volume"}
    if not required.issubset(candles): raise ValueError(f"VWAP missing columns: {required-set(candles)}")
    out=pd.Series(index=candles.index,dtype=float); times=pd.to_datetime(candles.time,utc=True); days=times.dt.strftime("%Y-%m-%d")
    typical=(candles.high+candles.low+candles.close)/3; pv=typical*candles.volume
    for day in days.drop_duplicates():
        mask=days==day; denom=candles.loc[mask,"volume"].cumsum().replace(0,float("nan")); out.loc[mask]=pv.loc[mask].cumsum()/denom
    return out
def volume_expansion(candles,lookback=5):
    if "volume" not in candles or len(candles)<lookback+1:return None
    prior=pd.to_numeric(candles.volume.iloc[-lookback-1:-1],errors="coerce").mean(); current=float(candles.volume.iloc[-1])
    if pd.isna(prior) or prior<=0:return None
    return current/float(prior)
def cumulative_volume_delta(trades):
    if trades is None or trades.empty or not {"volume","side"}.issubset(trades.columns):return {"available":False,"value":None,"reason":"Aggressor-side trade prints unavailable"}
    side=trades.side.astype(str).str.lower(); volume=pd.to_numeric(trades.volume,errors="coerce").fillna(0)
    if not side.isin(["buy","sell"]).all():return {"available":False,"value":None,"reason":"Trade side values do not identify the aggressor"}
    return {"available":True,"value":float(volume[side=="buy"].sum()-volume[side=="sell"].sum()),"reason":None}
