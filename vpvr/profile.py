"""Deterministic Volume Profile with explicit source provenance.

Trade prints with aggressor/volume are accepted as true volume-at-price. Candle
input is an approximation: candle volume is spread uniformly across every bin
intersecting its low/high range, not an assertion about where trades occurred.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
import numpy as np
import pandas as pd

@dataclass(frozen=True)
class VolumeProfile:
    prices: tuple[float, ...]
    volumes: tuple[float, ...]
    poc: float
    vah: float
    val: float
    hvns: tuple[float, ...]
    lvns: tuple[float, ...]
    total_volume: float
    source: str
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None

    def as_dict(self) -> dict:
        return {"poc":self.poc,"vah":self.vah,"val":self.val,"hvns":list(self.hvns),"lvns":list(self.lvns),"total_volume":self.total_volume,"source":self.source,"rows":len(self.prices)}


def _value_area(volumes: np.ndarray, prices: np.ndarray, fraction: float) -> tuple[float,float,float]:
    if len(volumes)==0 or volumes.sum()<=0: raise ValueError("profile has no positive volume")
    poc_idx=int(np.argmax(volumes)); lo=hi=poc_idx; accumulated=float(volumes[poc_idx]); target=float(volumes.sum()*fraction)
    while accumulated < target and (lo>0 or hi<len(volumes)-1):
        left=float(volumes[lo-1]) if lo>0 else -1.0
        right=float(volumes[hi+1]) if hi<len(volumes)-1 else -1.0
        if right>left: hi+=1; accumulated+=float(volumes[hi])
        elif lo>0: lo-=1; accumulated+=float(volumes[lo])
        else: break
    return float(prices[poc_idx]),float(prices[hi]),float(prices[lo])


def _nodes(volumes: np.ndarray, prices: np.ndarray, window: int, threshold: float) -> tuple[tuple[float,...],tuple[float,...]]:
    smooth=pd.Series(volumes).rolling(window,center=True,min_periods=1).mean().to_numpy()
    maxv=float(smooth.max()) if len(smooth) else 0.0
    hvn=[]; lvn=[]
    for i in range(1,len(smooth)-1):
        if smooth[i]>=smooth[i-1] and smooth[i]>smooth[i+1] and smooth[i]>=maxv*threshold:
            hvn.append(float(prices[i]))
        if smooth[i]<=smooth[i-1] and smooth[i]<smooth[i+1] and smooth[i]<=maxv*threshold:
            lvn.append(float(prices[i]))
    return tuple(hvn),tuple(lvn)


def build_volume_profile(
    candles: pd.DataFrame | None = None, *, trades: pd.DataFrame | None = None,
    rows: int = 60, value_area_pct: float = 0.70, smoothing_window: int = 3,
    node_threshold_pct: float = 0.15, start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
) -> VolumeProfile:
    """Build fixed-range profile from trades or approximate it from OHLCV.

    Trades require price and volume. If side/aggressor is supplied, it is not
    currently used for VP bins; that field feeds CVD separately. Rows denotes
    number of uniform price bins and must be 50..70.
    """
    if not 50<=rows<=70: raise ValueError("rows must be between 50 and 70")
    if not 0.5<=value_area_pct<=0.9: raise ValueError("invalid value area fraction")
    source="trade_prints"
    if trades is not None and not trades.empty:
        if not {"price","volume"}.issubset(trades.columns): raise ValueError("trades require price and volume")
        frame=trades[["price","volume"]].copy()
        frame["price"]=pd.to_numeric(frame.price,errors="coerce"); frame["volume"]=pd.to_numeric(frame.volume,errors="coerce")
        frame=frame.replace([np.inf,-np.inf],np.nan).dropna()
        frame=frame[(frame.price>0)&(frame.volume>0)]
        if frame.empty: raise ValueError("no valid trade prints")
        low,high=float(frame.price.min()),float(frame.price.max()); source="trade_prints"
    else:
        if candles is None or candles.empty: raise ValueError("candles or trade prints required")
        required={"high","low","volume"}
        if not required.issubset(candles.columns): raise ValueError(f"candles missing {required-set(candles.columns)}")
        frame=candles[["high","low","volume"]].copy()
        for c in frame.columns: frame[c]=pd.to_numeric(frame[c],errors="coerce")
        frame=frame.replace([np.inf,-np.inf],np.nan).dropna()
        frame=frame[(frame.high>0)&(frame.low>0)&(frame.high>=frame.low)&(frame.volume>0)]
        if frame.empty: raise ValueError("no valid OHLCV volume")
        low,high=float(frame.low.min()),float(frame.high.max()); source="ohlcv_uniform_range_approximation"
    if high<=low:
        high=low+max(abs(low)*1e-6,1e-8)
    edges=np.linspace(low,high,rows+1); prices=(edges[:-1]+edges[1:])/2; volumes=np.zeros(rows,dtype=float)
    if source=="trade_prints":
        idx=np.clip(np.searchsorted(edges,frame.price.to_numpy(),side="right")-1,0,rows-1)
        np.add.at(volumes,idx,frame.volume.to_numpy())
    else:
        for row in frame.itertuples(index=False):
            lo=float(row.low); hi=float(row.high); vol=float(row.volume)
            if hi==lo:
                idx=int(np.clip(np.searchsorted(edges,lo,side="right")-1,0,rows-1)); volumes[idx]+=vol; continue
            overlap=np.maximum(0.0,np.minimum(edges[1:],hi)-np.maximum(edges[:-1],lo))
            total=float(overlap.sum())
            if total<=0: continue
            volumes += vol * overlap/total
    poc,vah,val=_value_area(volumes,prices,value_area_pct); hvn,lvn=_nodes(volumes,prices,smoothing_window,node_threshold_pct)
    s=pd.Timestamp(start) if start is not None else (pd.to_datetime(candles.time.min(),utc=True) if candles is not None and "time" in candles else None)
    e=pd.Timestamp(end) if end is not None else (pd.to_datetime(candles.time.max(),utc=True) if candles is not None and "time" in candles else None)
    return VolumeProfile(tuple(map(float,prices)),tuple(map(float,volumes)),poc,vah,val,hvn,lvn,float(volumes.sum()),source,s,e)


def rolling_profiles(candles: pd.DataFrame, rows: int=60, value_area_pct: float=.70, window: int=240, step: int=1, **kwargs) -> list[VolumeProfile]:
    if window<1 or step<1: raise ValueError("window and step must be positive")
    result=[]
    for end in range(window,len(candles)+1,step):
        chunk=candles.iloc[end-window:end]
        result.append(build_volume_profile(chunk,rows=rows,value_area_pct=value_area_pct,start=chunk.iloc[0].get("time"),end=chunk.iloc[-1].get("time"),**kwargs))
    return result


def session_profiles(candles: pd.DataFrame, rows: int=60, value_area_pct: float=.70, **kwargs) -> dict[str,VolumeProfile]:
    if "time" not in candles: raise ValueError("session profiles require time")
    times=pd.to_datetime(candles.time,utc=True)
    days=times.dt.strftime("%Y-%m-%d")
    return {day:build_volume_profile(candles.loc[days==day],rows=rows,value_area_pct=value_area_pct,**kwargs) for day in days.drop_duplicates()}
