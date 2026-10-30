"""Delta India public REST market-data adapter. It has no order-entry methods."""
from __future__ import annotations
import time
import requests
import pandas as pd
from .config import VPVRConfig
BASE_URL="https://api.india.delta.exchange"
RESOLUTION_SECONDS={"1m":60,"3m":180,"15m":900}
class MarketDataError(RuntimeError): pass
class DeltaMarketData:
    def __init__(self,base_url=BASE_URL,timeout=15,config=None): self.base_url=base_url.rstrip("/"); self.timeout=timeout; self.config=config or VPVRConfig.from_env()
    def _get_json(self,path,params=None):
        try:
            with requests.Session() as session:
                session.trust_env=False; response=session.get(self.base_url+path,params=params,headers={"Accept":"application/json"},timeout=self.timeout); response.raise_for_status(); payload=response.json()
            if payload.get("success") is False: raise MarketDataError(f"Delta returned success=false for {path}")
            return payload
        except (requests.RequestException,ValueError) as exc: raise MarketDataError(f"Delta request failed for {path}: {exc}") from exc
    def fetch_products(self):
        p=self._get_json("/v2/products",{"contract_types":"perpetual_futures","states":"live","page_size":1000})
        return [x for x in p.get("result",[]) if x.get("contract_type")=="perpetual_futures" and x.get("state")=="live"]
    def fetch_tickers(self): return self._get_json("/v2/tickers").get("result",[])
    def fetch_public_trades(self,symbol):
        rows=self._get_json(f"/v2/trades/{symbol}").get("result",{}).get("trades",[])
        if not rows:return pd.DataFrame(columns=["price","volume","side","time"])
        f=pd.DataFrame(rows)
        if not {"price","size","side","timestamp"}.issubset(f.columns): raise MarketDataError("public trade response lacks price/size/side/timestamp")
        f=f.rename(columns={"size":"volume"}); ts=pd.to_numeric(f.timestamp,errors="coerce")
        # Delta's REST trade timestamps use Unix microseconds in recent API responses.
        if ts.dropna().empty: raise MarketDataError("trade timestamps are invalid")
        unit="us" if float(ts.max())>1e14 else ("ms" if float(ts.max())>1e11 else "s")
        f["time"]=pd.to_datetime(ts,unit=unit,utc=True,errors="coerce")
        f["price"]=pd.to_numeric(f.price,errors="coerce"); f["volume"]=pd.to_numeric(f.volume,errors="coerce"); f["side"]=f.side.astype(str).str.lower()
        f=f.dropna(subset=["time","price","volume"]); f=f[(f.price>0)&(f.volume>0)&f.side.isin(["buy","sell"])]
        return f.sort_values("time").drop_duplicates(["time","price","volume","side"]).reset_index(drop=True)
    def fetch_candles(self,symbol,timeframe,limit=300,end_ts=None):
        if timeframe not in RESOLUTION_SECONDS: raise ValueError(f"unsupported timeframe {timeframe}")
        if not 10<=limit<=2000: raise ValueError("candle limit must be 10..2000")
        seconds=RESOLUTION_SECONDS[timeframe]; end=int(end_ts or time.time()); start=end-seconds*limit
        p=self._get_json("/v2/history/candles",{"symbol":symbol,"resolution":timeframe,"start":start,"end":end})
        f=pd.DataFrame(p.get("result",[]))
        if f.empty:return f
        if not {"time","open","high","low","close","volume"}.issubset(f.columns): raise MarketDataError("OHLCV response is missing required columns")
        f["time"]=pd.to_datetime(f.time,unit="s",utc=True)
        for col in ("open","high","low","close","volume"):f[col]=pd.to_numeric(f[col],errors="coerce")
        f=f.replace([float("inf"),-float("inf")],float("nan")).dropna(subset=["time","open","high","low","close","volume"])
        f=f[(f.open>0)&(f.high>0)&(f.low>0)&(f.close>0)&(f.volume>=0)&(f.high>=f.low)]
        f=f.sort_values("time").drop_duplicates("time",keep="last").reset_index(drop=True)
        now=end; completed=f.time.map(lambda t:t.timestamp()+seconds<=now)
        return f.loc[completed].reset_index(drop=True)
    def freshness(self,frame,timeframe,now=None):
        if frame is None or frame.empty or "time" not in frame:return False,"empty candles"
        sec=RESOLUTION_SECONDS[timeframe]; ts=pd.Timestamp(frame.time.iloc[-1]); age=(now or time.time())-(ts.timestamp()+sec)
        if age< -1:return False,"latest candle is incomplete"
        if age>sec+self.config.max_data_age_seconds:return False,f"stale candles ({age:.0f}s after last close)"
        if len(frame)>1:
            gap=(pd.Timestamp(frame.time.iloc[-1])-pd.Timestamp(frame.time.iloc[-2])).total_seconds()
            if gap!=sec:return False,f"candle gap detected ({gap:.0f}s; expected {sec}s)"
        return True,"fresh"
    def fetch_orderbook(self,symbol,depth=10): return self._get_json(f"/v2/l2orderbook/{symbol}",{"depth":depth}).get("result",{})
    def contract_specs(self,product):
        def number(key):
            try: value=float(product[key]); return value if value>0 else None
            except (KeyError,TypeError,ValueError): return None
        multiplier=number("contract_value"); tick=number("tick_size")
        # Delta's published product schema documents contract_value, tick_size,
        # and position_size_limit but does not publish lot increment/minimum size.
        # Use 1 contract as a PAPER-ONLY assumption and preserve the unverified flag.
        min_qty=number("minimum_order_size") or number("min_order_size") or 1.0
        lot=number("lot_size") or 1.0
        if product.get("is_quanto") is True or product.get("notional_type") not in (None,"vanilla"): return None
        if multiplier is None or tick is None:return None
        if product.get("trading_status") not in (None,"operational"):return None
        return {"contract_multiplier":multiplier,"tick_size":tick,"lot_size":lot,"min_qty":min_qty,"spec_verified":bool(product.get("minimum_order_size") or product.get("min_order_size") or product.get("lot_size")),"position_size_limit":number("position_size_limit")}
