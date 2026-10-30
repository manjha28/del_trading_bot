"""Delta Exchange REST market data adapter. Read-only; no order methods here."""
from __future__ import annotations
import time
import requests
import pandas as pd
from .config import VPVRConfig

BASE_URL="https://api.india.delta.exchange"
RESOLUTION={"1m":"1m","3m":"3m","15m":"15m"}

class MarketDataError(RuntimeError): pass

class DeltaMarketData:
    def __init__(self,base_url:str=BASE_URL,timeout:int=15,config:VPVRConfig|None=None): self.base_url=base_url.rstrip("/"); self.timeout=timeout; self.config=config or VPVRConfig.from_env()
    def fetch_products(self)->list[dict]:
        try:
            with requests.Session() as s:
                s.trust_env=False; r=s.get(self.base_url+"/v2/products",timeout=self.timeout); r.raise_for_status(); d=r.json()
            if not d.get("success"): raise MarketDataError("Delta products endpoint returned success=false")
            return [p for p in d.get("result",[]) if p.get("contract_type")=="perpetual_futures" and p.get("state")=="live"]
        except (requests.RequestException,ValueError) as exc: raise MarketDataError(f"products fetch failed: {exc}") from exc
    def fetch_tickers(self)->list[dict]:
        try:
            with requests.Session() as s:
                s.trust_env=False; r=s.get(self.base_url+"/v2/tickers",timeout=self.timeout); r.raise_for_status(); d=r.json()
            if not d.get("success"): raise MarketDataError("Delta tickers endpoint returned success=false")
            return d.get("result",[])
        except (requests.RequestException,ValueError) as exc: raise MarketDataError(f"tickers fetch failed: {exc}") from exc
    def fetch_candles(self,symbol:str,timeframe:str,limit:int=300,end_ts:int|None=None)->pd.DataFrame:
        if timeframe not in RESOLUTION: raise ValueError(f"unsupported timeframe: {timeframe}")
        if not 10<=limit<=1500: raise ValueError("candle limit must be 10..1500")
        end=int(end_ts or time.time()); seconds={"1m":60,"3m":180,"15m":900}[timeframe]; start=end-seconds*limit
        try:
            with requests.Session() as s:
                s.trust_env=False; r=s.get(self.base_url+"/v2/history/candles",params={"symbol":symbol,"resolution":RESOLUTION[timeframe],"start":start,"end":end},timeout=self.timeout); r.raise_for_status(); d=r.json()
            if not d.get("success"): raise MarketDataError(f"candle fetch failed for {symbol} {timeframe}")
            frame=pd.DataFrame(d.get("result",[]))
            if frame.empty: return frame
            frame["time"]=pd.to_datetime(frame.time,unit="s",utc=True); frame=frame.sort_values("time").drop_duplicates("time",keep="last").reset_index(drop=True)
            for c in ("open","high","low","close","volume"): frame[c]=pd.to_numeric(frame[c],errors="coerce")
            return frame
        except (requests.RequestException,ValueError,KeyError) as exc:
            if isinstance(exc,MarketDataError): raise
            raise MarketDataError(f"candle fetch failed for {symbol} {timeframe}: {exc}") from exc
    def freshness(self,frame:pd.DataFrame,timeframe:str,now:float|None=None)->tuple[bool,str]:
        if frame.empty or "time" not in frame: return False,"empty candles"
        seconds={"1m":60,"3m":180,"15m":900}[timeframe]; last=pd.Timestamp(frame.time.iloc[-1]).timestamp(); age=(now or time.time())-(last+seconds)
        if age<0: return False,"latest candle is not complete"
        if age>self.config.max_data_age_seconds: return False,f"stale candles ({age:.0f}s old)"
        if len(frame)>1 and (pd.Timestamp(frame.time.iloc[-1])-pd.Timestamp(frame.time.iloc[-2])).total_seconds()!=seconds: return False,"candle gap detected"
        return True,"fresh"
    def contract_specs(self,product:dict)->dict|None:
        def first(keys):
            for k in keys:
                if product.get(k) is not None:
                    try: return float(product[k])
                    except (TypeError,ValueError): pass
            return None
        mult=first(("contract_value","contract_size")); lot=first(("lot_size",)); minq=first(("minimum_order_size","min_order_size")); tick=first(("tick_size",))
        if not all(x is not None and x>0 for x in (mult,lot,minq,tick)): return None
        return {"contract_multiplier":mult,"lot_size":lot,"min_qty":minq,"tick_size":tick}
