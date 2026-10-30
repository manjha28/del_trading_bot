from __future__ import annotations

import time
import requests
import pandas as pd


BASE_URL = "https://api.india.delta.exchange"


class ResearchDataError(RuntimeError):
    pass


class DeltaResearchData:
    def __init__(self, base_url: str = BASE_URL, timeout: int = 20, request_interval: float = 0.1):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.request_interval = request_interval
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.trust_env = False

    def close(self) -> None:
        self.session.close()

    def _get(self, path: str, params: dict | None = None) -> dict:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        try:
            response = self.session.get(
                self.base_url + path, params=params, timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
            self._last_request = time.monotonic()
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ResearchDataError(f"Delta public request failed for {path}: {exc}") from exc
        if payload.get("success") is False:
            raise ResearchDataError(f"Delta returned success=false for {path}")
        return payload

    def fetch_products(self) -> list[dict]:
        products, page = [], 1
        while True:
            payload = self._get("/v2/products", {"page": page, "page_size": 1000})
            batch = payload.get("result")
            if not isinstance(batch, list):
                raise ResearchDataError("Delta product catalog is not a list")
            products.extend(
                product for product in batch
                if product.get("contract_type") == "perpetual_futures"
            )
            if len(batch) < 1000:
                return products
            page += 1

    def fetch_tickers(self) -> dict[str, dict]:
        rows = self._get("/v2/tickers").get("result", [])
        if not isinstance(rows, list):
            raise ResearchDataError("Delta ticker response is not a list")
        return {row["symbol"]: row for row in rows if row.get("symbol")}

    def fetch_candles(self, symbol: str, timeframe: str, start_ts: int, end_ts: int,
                      chunk_size: int = 1800) -> pd.DataFrame:
        seconds = {"1m": 60, "5m": 300, "1h": 3600}.get(timeframe)
        if seconds is None:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        frames, cursor = [], int(start_ts)
        while cursor < int(end_ts):
            finish = min(int(end_ts), cursor + seconds * chunk_size)
            payload = self._get("/v2/history/candles", {
                "symbol": symbol, "resolution": timeframe, "start": cursor, "end": finish,
            })
            rows = payload.get("result", [])
            if rows:
                frame = pd.DataFrame(rows)
                required = {"time", "open", "high", "low", "close", "volume"}
                if not required.issubset(frame.columns):
                    raise ResearchDataError(
                        f"{symbol} {timeframe} candles missing fields: {required-set(frame.columns)}"
                    )
                frame["time"] = pd.to_datetime(frame.time, unit="s", utc=True, errors="coerce")
                for column in ("open", "high", "low", "close", "volume"):
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")
                frame = frame.dropna(subset=list(required))
                frame = frame[(frame.close > 0) & (frame.high >= frame.low) & (frame.volume >= 0)]
                frames.append(frame)
            cursor = finish
        if not frames:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        return (pd.concat(frames, ignore_index=True).sort_values("time")
                .drop_duplicates("time", keep="last").reset_index(drop=True))

    def fetch_book(self, symbol: str, depth: int = 20) -> dict:
        payload = self._get(f"/v2/l2orderbook/{symbol}", {"depth": depth})
        book = payload.get("result")
        if not isinstance(book, dict) or not isinstance(book.get("buy"), list) or not isinstance(book.get("sell"), list):
            raise ResearchDataError(f"order book unavailable or malformed for {symbol}")
        return book

    def fetch_trades(self, symbol: str) -> pd.DataFrame:
        result = self._get(f"/v2/trades/{symbol}").get("result", {})
        rows = result.get("trades", []) if isinstance(result, dict) else []
        frame = pd.DataFrame(rows)
        if frame.empty:
            return pd.DataFrame(columns=["time", "price", "volume", "side"])
        required = {"price", "size", "side", "timestamp"}
        if not required.issubset(frame.columns):
            raise ResearchDataError(f"public trades response lacks fields for {symbol}")
        raw_ts = pd.to_numeric(frame.timestamp, errors="coerce")
        maximum = float(raw_ts.dropna().max()) if raw_ts.notna().any() else 0.0
        unit = "us" if maximum > 1e14 else ("ms" if maximum > 1e11 else "s")
        frame["time"] = pd.to_datetime(raw_ts, unit=unit, utc=True, errors="coerce")
        frame["price"] = pd.to_numeric(frame.price, errors="coerce")
        frame["volume"] = pd.to_numeric(frame["size"], errors="coerce")
        frame["side"] = frame.side.astype(str).str.lower()
        return frame.dropna(subset=["time", "price", "volume"]).loc[
            lambda x: (x.price > 0) & (x.volume > 0) & x.side.isin(["buy", "sell"])
        ].reset_index(drop=True)
