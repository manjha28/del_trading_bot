"""Delta India public REST market data. No private account or order functions are exposed."""
from __future__ import annotations
import time
import requests
import pandas as pd
from .config import VPVRConfig

BASE_URL = "https://api.india.delta.exchange"
RESOLUTION_SECONDS = {"1m": 60, "3m": 180, "15m": 900}


class MarketDataError(RuntimeError):
    pass


class DeltaMarketData:
    def __init__(self, base_url=BASE_URL, timeout=15, config=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.config = config or VPVRConfig.from_env()

    def _get_json(self, path, params=None):
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(self.base_url + path, params=params,
                                        headers={"Accept": "application/json"}, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
            if payload.get("success") is False:
                raise MarketDataError(f"Delta success=false for {path}")
            return payload
        except MarketDataError:
            raise
        except (requests.RequestException, ValueError) as exc:
            raise MarketDataError(f"Delta request failed for {path}: {exc}") from exc

    def fetch_products(self):
        payload = self._get_json("/v2/products", {
            "contract_types": "perpetual_futures", "states": "live", "page_size": 1000
        })
        return [p for p in payload.get("result", [])
                if p.get("contract_type") == "perpetual_futures" and p.get("state") == "live"]

    def fetch_tickers(self):
        return self._get_json("/v2/tickers").get("result", [])

    def fetch_public_trades(self, symbol):
        """Return actual Delta aggressor-side trades for CVD and trade-level VP."""
        result = self._get_json(f"/v2/trades/{symbol}").get("result", {})
        rows = result.get("trades", []) if isinstance(result, dict) else []
        if not rows:
            return pd.DataFrame(columns=["price", "volume", "side", "time"])
        frame = pd.DataFrame(rows)
        if not {"price", "size", "side", "timestamp"}.issubset(frame.columns):
            raise MarketDataError("public trades response lacks price/size/side/timestamp")
        frame = frame.rename(columns={"size": "volume"})
        raw_ts = pd.to_numeric(frame.timestamp, errors="coerce")
        if raw_ts.dropna().empty:
            raise MarketDataError("invalid public trade timestamps")
        maximum = float(raw_ts.max())
        unit = "us" if maximum > 1e14 else ("ms" if maximum > 1e11 else "s")
        frame["time"] = pd.to_datetime(raw_ts, unit=unit, utc=True, errors="coerce")
        frame["price"] = pd.to_numeric(frame.price, errors="coerce")
        frame["volume"] = pd.to_numeric(frame.volume, errors="coerce")
        frame["side"] = frame.side.astype(str).str.lower()
        frame = frame.dropna(subset=["time", "price", "volume"])
        frame = frame[(frame.price > 0) & (frame.volume > 0) & frame.side.isin(["buy", "sell"])]
        return frame.sort_values("time").drop_duplicates(
            ["time", "price", "volume", "side"]).reset_index(drop=True)

    def fetch_funding_history(self, symbol, start_ts, end_ts, resolution="1h"):
        """Fetch public historical funding candles using Delta's FUNDING:<symbol> syntax."""
        if resolution not in ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"):
            raise ValueError("unsupported funding history resolution")
        if int(end_ts) <= int(start_ts):
            raise ValueError("funding history end must be greater than start")
        payload = self._get_json("/v2/history/candles", {
            "symbol": f"FUNDING:{symbol}", "resolution": resolution,
            "start": int(start_ts), "end": int(end_ts)
        })
        frame = pd.DataFrame(payload.get("result", []))
        if frame.empty:
            raise MarketDataError(f"funding history unavailable for {symbol}")
        if not {"time", "close"}.issubset(frame.columns):
            raise MarketDataError("funding candle response has no time/close fields")
        frame["time"] = pd.to_datetime(frame.time, unit="s", utc=True, errors="coerce")
        frame["funding_rate"] = pd.to_numeric(frame.close, errors="coerce")
        frame = frame.dropna(subset=["time", "funding_rate"]).sort_values("time")
        if frame.empty:
            raise MarketDataError(f"funding history contains no valid values for {symbol}")
        return frame[["time", "funding_rate"]].drop_duplicates("time").reset_index(drop=True)

    def fetch_current_funding_rate(self, symbol, now=None, max_age_seconds=9 * 3600):
        """Return last public funding rate only when recent enough to support the filter."""
        end = int(now or time.time())
        history = self.fetch_funding_history(symbol, end - 7 * 86400, end, resolution="1h")
        latest = history.iloc[-1]
        age = end - pd.Timestamp(latest.time).timestamp()
        if age < -1 or age > max_age_seconds:
            raise MarketDataError(f"funding rate is stale for {symbol} ({age:.0f}s old)")
        return float(latest.funding_rate)

    def _fetch_candle_chunk(self, symbol, timeframe, start_ts, end_ts):
        payload = self._get_json("/v2/history/candles", {
            "symbol": symbol, "resolution": timeframe,
            "start": int(start_ts), "end": int(end_ts)
        })
        frame = pd.DataFrame(payload.get("result", []))
        if frame.empty:
            return frame
        required = {"time", "open", "high", "low", "close", "volume"}
        if not required.issubset(frame.columns):
            raise MarketDataError(f"OHLCV result missing {required - set(frame.columns)}")
        frame["time"] = pd.to_datetime(frame.time, unit="s", utc=True)
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.replace([float("inf"), -float("inf")], float("nan"))
        frame = frame.dropna(subset=list(required))
        frame = frame[(frame.open > 0) & (frame.high > 0) & (frame.low > 0) &
                      (frame.close > 0) & (frame.volume >= 0) & (frame.high >= frame.low)]
        return frame.sort_values("time").drop_duplicates("time", keep="last")

    def _completed_only(self, frame, timeframe, end_ts):
        if frame.empty:
            return frame.reset_index(drop=True)
        seconds = RESOLUTION_SECONDS[timeframe]
        complete = frame.time.map(lambda stamp: stamp.timestamp() + seconds <= end_ts)
        return frame.loc[complete].sort_values("time").drop_duplicates("time").reset_index(drop=True)

    def fetch_candles(self, symbol, timeframe, limit=300, end_ts=None):
        if timeframe not in RESOLUTION_SECONDS:
            raise ValueError(f"unsupported timeframe {timeframe}")
        if not 10 <= limit <= 2000:
            raise ValueError("limit must be 10..2000")
        end = int(end_ts or time.time())
        start = end - RESOLUTION_SECONDS[timeframe] * limit
        frame = self._fetch_candle_chunk(symbol, timeframe, start, end)
        return self._completed_only(frame, timeframe, end)

    def fetch_candles_range(self, symbol, timeframe, start_ts, end_ts, chunk_size=1800):
        """Historical backfill with bounded requests; API max is kept below 2000 candles."""
        if timeframe not in RESOLUTION_SECONDS:
            raise ValueError(f"unsupported timeframe {timeframe}")
        if not 10 <= chunk_size <= 2000:
            raise ValueError("chunk_size must be between 10 and 2000")
        step = RESOLUTION_SECONDS[timeframe] * chunk_size
        cursor, finish = int(start_ts), int(end_ts)
        if finish <= cursor:
            raise ValueError("end_ts must be greater than start_ts")
        frames = []
        while cursor < finish:
            next_cursor = min(finish, cursor + step)
            part = self._fetch_candle_chunk(symbol, timeframe, cursor, next_cursor)
            if not part.empty:
                frames.append(part)
            cursor = next_cursor
        if not frames:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        combined = pd.concat(frames, ignore_index=True).sort_values("time")
        combined = combined.drop_duplicates("time", keep="last").reset_index(drop=True)
        return self._completed_only(combined, timeframe, finish)

    def freshness(self, frame, timeframe, now=None):
        """Fail closed if empty, stale, incomplete, or any interval is missing."""
        if timeframe not in RESOLUTION_SECONDS:
            return False, "unsupported timeframe"
        if frame is None or frame.empty or "time" not in frame:
            return False, "empty candles"
        seconds = RESOLUTION_SECONDS[timeframe]
        timestamps = pd.to_datetime(frame.time, utc=True).sort_values()
        age = (now or time.time()) - (timestamps.iloc[-1].timestamp() + seconds)
        if age < -1:
            return False, "latest candle is incomplete"
        if age > seconds + self.config.max_data_age_seconds:
            return False, f"stale candles ({age:.0f}s after latest candle close)"
        if len(timestamps) > 1:
            gaps = timestamps.diff().dropna().dt.total_seconds()
            if (gaps != seconds).any():
                return False, "candle gap detected in supplied lookback"
        return True, "fresh"

    def contract_specs(self, product):
        """Read product metadata. Unpublished size increments remain unverified paper assumptions."""
        def number(key):
            try:
                value = float(product[key])
                return value if value > 0 else None
            except (KeyError, TypeError, ValueError):
                return None
        multiplier = number("contract_value")
        tick = number("tick_size")
        actual_lot = number("lot_size")
        actual_minimum = number("minimum_order_size") or number("min_order_size")
        lot = actual_lot or 1.0
        minimum = actual_minimum or 1.0
        if product.get("is_quanto") is True or product.get("notional_type") not in (None, "vanilla"):
            return None
        if multiplier is None or tick is None:
            return None
        if product.get("trading_status") != "operational":
            return None
        return {
            "contract_multiplier": multiplier, "tick_size": tick,
            "lot_size": lot, "min_qty": minimum,
            "spec_verified": actual_lot is not None and actual_minimum is not None,
            "position_size_limit": number("position_size_limit")
        }
