"""Delta India public REST market data. No private account or order functions are exposed."""
from __future__ import annotations
import time
from decimal import Decimal, InvalidOperation
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

    def fetch_orderbook(self, symbol: str, depth: int = 5):
        """Fetch public L2 book. Delta returns result.buy/result.sell price-size levels."""
        if not symbol or not 1 <= int(depth) <= 100:
            raise ValueError("symbol is required and order-book depth must be 1..100")
        payload = self._get_json(f"/v2/l2orderbook/{symbol}", {"depth": int(depth)})
        book = payload.get("result")
        if not isinstance(book, dict) or not isinstance(book.get("buy"), list) or not isinstance(book.get("sell"), list):
            raise MarketDataError("Delta order-book response is missing buy/sell levels")
        return book

    def fetch_funding_history(self, symbol, start_ts, end_ts, resolution="1h"):
        """Fetch Delta public funding candles using the documented FUNDING:<symbol> syntax."""
        supported = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}
        if resolution not in supported:
            raise ValueError("unsupported funding history resolution")
        if int(end_ts) <= int(start_ts):
            raise ValueError("funding history end must be greater than start")
        payload = self._get_json("/v2/history/candles", {
            "symbol": f"FUNDING:{symbol}", "resolution": resolution,
            "start": int(start_ts), "end": int(end_ts)
        })
        frame = pd.DataFrame(payload.get("result", []))
        if frame.empty or not {"time", "close"}.issubset(frame.columns):
            raise MarketDataError(f"funding history unavailable or malformed for {symbol}")
        frame["time"] = pd.to_datetime(frame.time, unit="s", utc=True, errors="coerce")
        frame["funding_rate"] = pd.to_numeric(frame.close, errors="coerce")
        frame = frame.dropna(subset=["time", "funding_rate"]).sort_values("time")
        if frame.empty:
            raise MarketDataError(f"funding history contains no valid observations for {symbol}")
        return frame[["time", "funding_rate"]].drop_duplicates("time").reset_index(drop=True)

    def fetch_current_funding_rate(self, symbol, now=None, max_age_seconds=9 * 3600):
        """Return the latest funding rate only when it is no more than nine hours old."""
        end = int(now or time.time())
        history = self.fetch_funding_history(symbol, end - 7 * 86400, end, resolution="1h")
        latest = history.iloc[-1]
        age = end - pd.Timestamp(latest.time).timestamp()
        if age < -1 or age > max_age_seconds:
            raise MarketDataError(f"funding rate is stale for {symbol} ({age:.0f}s old)")
        return float(latest.funding_rate)

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
        # Identical rows can represent distinct executions. The REST payload
        # does not guarantee a stable trade ID, so preserve every returned print.
        return frame.sort_values("time").reset_index(drop=True)

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
        """Normalize and verify supported Delta India perpetual contract sizing.

        Delta order size is an integer number of contracts. The minimum
        underlying-asset order sizes below come from Delta India's published
        futures minimum-order guidance; they are converted to contract units
        using the live product contract_value. Unknown products fail closed.
        """
        policies = {
            "BTCUSD": {
                "currency": "BTC",
                "contract_value": Decimal("0.001"),
                "minimum_underlying_qty": Decimal("0.001"),
            },
            "ETHUSD": {
                "currency": "ETH",
                "contract_value": Decimal("0.01"),
                "minimum_underlying_qty": Decimal("0.01"),
            },
        }
        metadata_keys = (
            "symbol", "contract_type", "state", "trading_status",
            "notional_type", "is_quanto", "contract_value",
            "contract_unit_currency", "tick_size", "position_size_limit",
        )
        metadata = {key: product.get(key) for key in metadata_keys}
        symbol = str(product.get("symbol") or "").upper()
        policy = policies.get(symbol)
        errors = []

        def decimal_field(key):
            raw = product.get(key)
            if raw is None:
                return None
            try:
                value = Decimal(str(raw))
            except (InvalidOperation, TypeError, ValueError):
                return None
            return value if value.is_finite() else None

        multiplier = decimal_field("contract_value")
        tick = decimal_field("tick_size")
        limit = decimal_field("position_size_limit")
        lot_increment = Decimal("1")
        minimum_contracts = None

        if policy is None:
            errors.append("unsupported_symbol: no verified BTCUSD/ETHUSD sizing policy")
        if not symbol or product.get("symbol") != symbol:
            errors.append("symbol_mismatch_or_missing")
        if product.get("contract_type") != "perpetual_futures":
            errors.append("contract_type_must_be_perpetual_futures")
        if product.get("state") != "live":
            errors.append("product_state_must_be_live")
        if product.get("trading_status") != "operational":
            errors.append("trading_status_must_be_operational")
        if product.get("notional_type") != "vanilla":
            errors.append("notional_type_must_be_vanilla")
        if product.get("is_quanto") is not False:
            errors.append("is_quanto_must_be_explicit_false")

        if multiplier is None or multiplier <= 0:
            errors.append("contract_value_missing_or_invalid")
        if tick is None or tick <= 0:
            errors.append("tick_size_missing_or_invalid")
        if limit is None or limit <= 0 or limit != limit.to_integral_value():
            errors.append("position_size_limit_missing_or_not_positive_integer")

        if policy is not None:
            if multiplier is None or multiplier != policy["contract_value"]:
                errors.append("contract_value_does_not_match_published_symbol_policy")
            if str(product.get("contract_unit_currency") or "").upper() != policy["currency"]:
                errors.append("contract_unit_currency_does_not_match_symbol_policy")
            if multiplier is not None and multiplier > 0:
                raw_contracts = policy["minimum_underlying_qty"] / multiplier
                if raw_contracts <= 0 or raw_contracts != raw_contracts.to_integral_value():
                    errors.append("minimum_underlying_quantity_does_not_convert_to_integer_contracts")
                else:
                    minimum_contracts = int(raw_contracts)
            else:
                errors.append("minimum_order_cannot_be_normalized_without_contract_value")

        lot_increment_verified = policy is not None
        minimum_verified = (
            policy is not None
            and minimum_contracts is not None
            and multiplier == policy["contract_value"]
            and str(product.get("contract_unit_currency") or "").upper() == policy["currency"]
        )
        if not lot_increment_verified:
            errors.append("integer_contract_increment_not_verified_for_symbol")

        verified = not errors
        return {
            "symbol": symbol or None,
            "contract_multiplier": float(multiplier) if multiplier is not None and multiplier > 0 else None,
            "contract_unit_currency": product.get("contract_unit_currency"),
            "tick_size": float(tick) if tick is not None and tick > 0 else None,
            "lot_size": int(lot_increment) if lot_increment_verified else None,
            "min_qty": minimum_contracts,
            "minimum_underlying_qty": str(policy["minimum_underlying_qty"]) if policy is not None else None,
            "minimum_order_contracts": minimum_contracts,
            "position_size_limit": int(limit) if limit is not None and limit > 0 and limit == limit.to_integral_value() else None,
            "max_order_size_contracts": int(limit) if limit is not None and limit > 0 and limit == limit.to_integral_value() else None,
            "spec_verified": verified,
            "lot_increment_verified": lot_increment_verified,
            "minimum_quantity_verified": minimum_verified,
            "verification_policy": "delta_india_btc_eth_minimums_v1" if policy is not None else None,
            "verification_source": {
                "minimum_order_guidance": "https://deltaexchangeindia.freshdesk.com/support/solutions/articles/80001132372-what-is-the-minimum-order-size-for-futures-trading-",
                "product_metadata_and_order_size_schema": "https://docs.delta.exchange/",
                "minimum_size_basis": "published minimum underlying-asset quantity divided by live contract_value",
                "increment_basis": "Delta order size is an integer number of contracts",
            } if policy is not None else None,
            "metadata_received": metadata,
            "verification_errors": errors,
            "sizing_assumption": None if verified else "Contract sizing is unverified; this symbol must remain blocked from paper entries.",
        }
