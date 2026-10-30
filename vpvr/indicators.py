"""VPVR indicator engine. Aggressor delta is calculated only from explicit trade side."""
from __future__ import annotations
import pandas as pd


def ema(series: pd.Series, length: int = 20) -> pd.Series:
    if length < 1:
        raise ValueError("EMA length must be positive")
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def session_vwap(candles: pd.DataFrame) -> pd.Series:
    """UTC calendar-day VWAP approximation from OHLCV typical price."""
    required={"time","high","low","close","volume"}
    if not required.issubset(candles):
        raise ValueError(f"VWAP missing columns: {required-set(candles.columns)}")
    result=pd.Series(index=candles.index,dtype=float)
    times=pd.to_datetime(candles.time,utc=True)
    sessions=times.dt.strftime("%Y-%m-%d")
    typical=(candles.high+candles.low+candles.close)/3.0
    price_volume=typical*candles.volume
    for session in sessions.drop_duplicates():
        mask=sessions==session
        denominator=candles.loc[mask,"volume"].cumsum().replace(0,float("nan"))
        result.loc[mask]=price_volume.loc[mask].cumsum()/denominator
    return result


def volume_expansion(candles: pd.DataFrame, lookback: int = 5) -> float | None:
    """Latest completed bar volume divided by the preceding N completed bars' average."""
    if lookback<1 or "volume" not in candles or len(candles)<lookback+1:
        return None
    previous=pd.to_numeric(candles.volume.iloc[-lookback-1:-1],errors="coerce").mean()
    current=float(candles.volume.iloc[-1])
    if pd.isna(previous) or previous<=0:
        return None
    return current/float(previous)


def cumulative_volume_delta(trades: pd.DataFrame | None) -> dict:
    """Return signed aggressor volume from REST trade side labels; never infer from candle colour."""
    if trades is None or trades.empty or not {"volume","side"}.issubset(trades.columns):
        return {"available":False,"value":None,"reason":"Aggressor-side trade prints unavailable"}
    side=trades.side.astype(str).str.lower()
    volume=pd.to_numeric(trades.volume,errors="coerce").fillna(0)
    if not side.isin(["buy","sell"]).all():
        return {"available":False,"value":None,"reason":"Trade side values do not identify aggressor buy/sell"}
    return {"available":True,"value":float(volume[side=="buy"].sum()-volume[side=="sell"].sum()),"reason":None}


def trade_delta_bars(trades: pd.DataFrame | None, timeframe: str = "1min") -> pd.DataFrame:
    """Aggregate aggressor buys/sells into bars and compute CVD; missing tape returns empty."""
    if trades is None or trades.empty or not {"time","volume","side"}.issubset(trades.columns):
        return pd.DataFrame(columns=["buy_volume","sell_volume","delta","cvd"])
    side=trades.side.astype(str).str.lower()
    if not side.isin(["buy","sell"]).all():
        return pd.DataFrame(columns=["buy_volume","sell_volume","delta","cvd"])
    frame=trades.copy()
    frame["time"]=pd.to_datetime(frame.time,utc=True)
    frame["volume"]=pd.to_numeric(frame.volume,errors="coerce")
    frame=frame.dropna(subset=["time","volume"])
    frame=frame[frame.volume>0].copy()
    frame["buy_volume"]=frame.volume.where(frame.side.astype(str).str.lower()=="buy",0.0)
    frame["sell_volume"]=frame.volume.where(frame.side.astype(str).str.lower()=="sell",0.0)
    grouped=frame.set_index("time").resample(timeframe).agg(
        buy_volume=("buy_volume","sum"),sell_volume=("sell_volume","sum"))
    grouped["delta"]=grouped.buy_volume-grouped.sell_volume
    grouped["cvd"]=grouped.delta.cumsum()
    return grouped.dropna(how="all")


def delta_divergence(candles: pd.DataFrame, delta_bars: pd.DataFrame, lookback: int = 5) -> dict:
    """Compare aligned price swing extremes with genuine aggressor CVD swing extremes."""
    if lookback < 4 or candles is None or delta_bars is None or len(candles) < lookback:
        return {"available": False, "bullish": False, "bearish": False,
                "reason": "insufficient aligned price and trade-delta history"}
    if "time" not in candles or "cvd" not in delta_bars:
        return {"available": False, "bullish": False, "bearish": False,
                "reason": "timestamped CVD series unavailable"}
    price_frame = candles.tail(lookback).copy()
    price_frame["time"] = pd.to_datetime(price_frame.time, utc=True).dt.floor("min")
    cvd_frame = delta_bars.copy()
    cvd_frame.index = pd.to_datetime(cvd_frame.index, utc=True).floor("min")
    joined = price_frame.set_index("time").join(cvd_frame[["cvd"]], how="left")
    if joined.cvd.isna().mean() > 0.20:
        return {"available": False, "bullish": False, "bearish": False,
                "reason": "trade tape does not cover enough matching 1m bars"}
    price = pd.to_numeric(joined.close, errors="coerce").reset_index(drop=True)
    cvd = pd.to_numeric(joined.cvd, errors="coerce").reset_index(drop=True)
    if price.isna().any() or cvd.isna().any():
        return {"available": False, "bullish": False, "bearish": False,
                "reason": "price/CVD contains missing values"}
    mid = max(2, lookback // 2)
    price_low_1, price_low_2 = float(price.iloc[:mid].min()), float(price.iloc[mid:].min())
    cvd_low_1, cvd_low_2 = float(cvd.iloc[:mid].min()), float(cvd.iloc[mid:].min())
    price_high_1, price_high_2 = float(price.iloc[:mid].max()), float(price.iloc[mid:].max())
    cvd_high_1, cvd_high_2 = float(cvd.iloc[:mid].max()), float(cvd.iloc[mid:].max())
    return {"available": True,
            "bullish": price_low_2 < price_low_1 and cvd_low_2 > cvd_low_1,
            "bearish": price_high_2 > price_high_1 and cvd_high_2 < cvd_high_1,
            "reason": None}
