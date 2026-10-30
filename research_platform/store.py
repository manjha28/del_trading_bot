from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from .market_data import (
    Candle,
    DataQuality,
    FundingObservation,
    Instrument,
    OrderBookSnapshot,
    TickerSnapshot,
    TradePrint,
)


SCHEMA_VERSION = 3


def _json(value: object) -> str:
    return json.dumps(value, default=str, sort_keys=True, allow_nan=False)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("stored timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored timestamp is missing timezone")
    return parsed.astimezone(timezone.utc)


class ResearchDataStore:
    """Versioned persistence for canonical research data; no order/execution state."""

    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path)
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError(f"research database does not exist: {self.path}")
            uri = f"file:{quote(str(self.path.resolve()))}?mode=ro"
            self.connection = sqlite3.connect(uri, uri=True, timeout=10)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if read_only:
            self.connection.execute("PRAGMA query_only = ON")
        else:
            self._initialize()

    def __enter__(self) -> ResearchDataStore:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"research data schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS instruments (
                product_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                contract_type TEXT NOT NULL,
                state TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                last_reconciled TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS instruments_symbol_idx ON instruments(symbol);
            CREATE TABLE IF NOT EXISTS candles (
                product_id TEXT NOT NULL REFERENCES instruments(product_id),
                timeframe TEXT NOT NULL,
                start_time TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                volume_unit TEXT,
                source TEXT NOT NULL,
                is_complete INTEGER NOT NULL,
                is_derived INTEGER NOT NULL,
                PRIMARY KEY(product_id,timeframe,start_time,source)
            );
            CREATE INDEX IF NOT EXISTS candles_lookup_idx
                ON candles(product_id,timeframe,start_time);
            CREATE TABLE IF NOT EXISTS tickers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL REFERENCES instruments(product_id),
                symbol TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                last_price REAL,
                mark_price REAL,
                index_price REAL,
                volume_24h_notional REAL,
                raw_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tickers_latest_idx
                ON tickers(product_id,observed_at DESC);
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL REFERENCES instruments(product_id),
                symbol TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                aggressor_side TEXT NOT NULL,
                source TEXT NOT NULL,
                trade_id TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS trades_source_id_idx
                ON trades(product_id,source,trade_id) WHERE trade_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS trades_history_idx
                ON trades(product_id,timestamp);
            CREATE TABLE IF NOT EXISTS order_books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL REFERENCES instruments(product_id),
                symbol TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                exchange_timestamp TEXT,
                source TEXT NOT NULL,
                sequence INTEGER,
                integrity TEXT NOT NULL,
                bids_json TEXT NOT NULL,
                asks_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS order_books_latest_idx
                ON order_books(product_id,observed_at DESC);
            CREATE TABLE IF NOT EXISTS funding (
                product_id TEXT NOT NULL REFERENCES instruments(product_id),
                symbol TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                rate REAL NOT NULL,
                source TEXT NOT NULL,
                PRIMARY KEY(product_id,timestamp,source)
            );
            CREATE TABLE IF NOT EXISTS data_quality (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL REFERENCES instruments(product_id),
                data_type TEXT NOT NULL,
                state TEXT NOT NULL,
                observed_at TEXT,
                source TEXT NOT NULL,
                completeness REAL,
                reasons_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS data_quality_lookup_idx
                ON data_quality(product_id,data_type,observed_at DESC);
            CREATE TABLE IF NOT EXISTS experiments (
                experiment_id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                dataset_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                split_json TEXT NOT NULL,
                config_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS experiments_created_idx
                ON experiments(created_at DESC);
            CREATE TABLE IF NOT EXISTS paper_simulations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT REFERENCES experiments(experiment_id),
                strategy_id TEXT NOT NULL,
                product_id TEXT NOT NULL REFERENCES instruments(product_id),
                symbol TEXT NOT NULL,
                decision_time TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS paper_simulations_time_idx
                ON paper_simulations(decision_time DESC);
            CREATE TABLE IF NOT EXISTS variant_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                variant_id TEXT NOT NULL,
                product_id TEXT NOT NULL REFERENCES instruments(product_id),
                symbol TEXT NOT NULL,
                decision_time TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                signal_json TEXT,
                UNIQUE(variant_id,product_id,decision_time)
            );
            CREATE INDEX IF NOT EXISTS variant_evaluations_time_idx
                ON variant_evaluations(evaluated_at DESC,id DESC);
        """)
        if version < SCHEMA_VERSION:
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.connection.commit()

    def reconcile_instruments(
        self,
        instruments: Iterable[Instrument],
        as_of: datetime | None = None,
        complete_catalog: bool = False,
    ) -> None:
        timestamp = _iso(as_of or datetime.now(timezone.utc))
        rows = list(instruments)
        returned = set()
        with self.connection:
            for instrument in rows:
                returned.add(instrument.product_id)
                existing = self.connection.execute(
                    "SELECT first_seen FROM instruments WHERE product_id=?",
                    (instrument.product_id,),
                ).fetchone()
                first_seen = existing["first_seen"] if existing else timestamp
                self.connection.execute("""
                    INSERT INTO instruments
                    (product_id,symbol,contract_type,state,first_seen,last_seen,last_reconciled,metadata_json)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(product_id) DO UPDATE SET
                    symbol=excluded.symbol,contract_type=excluded.contract_type,
                    state=excluded.state,last_seen=excluded.last_seen,
                    last_reconciled=excluded.last_reconciled,metadata_json=excluded.metadata_json
                """, (
                    instrument.product_id, instrument.symbol, instrument.contract_type,
                    instrument.state, first_seen, timestamp, timestamp,
                    _json(dict(instrument.metadata)),
                ))
            if complete_catalog:
                if returned:
                    placeholders = ",".join("?" for _ in returned)
                    self.connection.execute(
                        f"""UPDATE instruments SET state='not_returned',last_reconciled=?
                            WHERE product_id NOT IN ({placeholders})""",
                        (timestamp, *sorted(returned)),
                    )
                else:
                    self.connection.execute(
                        "UPDATE instruments SET state='not_returned',last_reconciled=?",
                        (timestamp,),
                    )

    def list_instruments(self) -> list[Instrument]:
        rows = self.connection.execute("""
            SELECT product_id,symbol,contract_type,state,metadata_json
            FROM instruments ORDER BY symbol,product_id
        """).fetchall()
        return [
            Instrument(
                product_id=row["product_id"], symbol=row["symbol"],
                contract_type=row["contract_type"], state=row["state"],
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]

    def dashboard_summary(self) -> dict:
        counts = {
            row["state"]: row["count"] for row in self.connection.execute("""
                SELECT state,COUNT(*) AS count FROM instruments GROUP BY state
            """)
        }
        data_counts = {
            row["data_type"]: row["count"] for row in self.connection.execute("""
                SELECT q.data_type,COUNT(*) AS count
                FROM (
                    SELECT data_type,product_id,MAX(id) AS latest_id
                    FROM data_quality GROUP BY data_type,product_id
                ) latest
                JOIN data_quality q ON q.id=latest.latest_id
                GROUP BY q.data_type
            """)
        }
        latest_capture = self.connection.execute("""
            SELECT MAX(observed_at) AS value FROM tickers
        """).fetchone()["value"]
        return {
            **self.coverage_summary(),
            "instrument_states": counts,
            "latest_quality_coverage": data_counts,
            "latest_ticker_time": latest_capture,
        }

    def list_instrument_rows(
        self, search: str = "", state: str = "", limit: int = 100, offset: int = 0
    ) -> dict:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset non-negative")
        conditions = []
        params: list[object] = []
        if search.strip():
            conditions.append("lower(i.symbol) LIKE ?")
            params.append(f"%{search.strip().casefold()}%")
        if state:
            conditions.append("i.state=?")
            params.append(state)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        count = self.connection.execute(
            f"SELECT COUNT(*) FROM instruments i{where}", params
        ).fetchone()[0]
        rows = self.connection.execute(f"""
            SELECT i.product_id,i.symbol,i.contract_type,i.state,i.first_seen,
                   i.last_seen,i.last_reconciled,i.metadata_json,
                   (SELECT MAX(c.start_time) FROM candles c
                    WHERE c.product_id=i.product_id) AS latest_candle_time,
                   (SELECT COUNT(*) FROM candles c
                    WHERE c.product_id=i.product_id) AS candle_count,
                   (SELECT MAX(t.observed_at) FROM tickers t
                    WHERE t.product_id=i.product_id) AS latest_ticker_time,
                   (SELECT t.last_price FROM tickers t
                    WHERE t.product_id=i.product_id
                    ORDER BY t.observed_at DESC,t.id DESC LIMIT 1) AS last_price,
                   (SELECT t.volume_24h_notional FROM tickers t
                    WHERE t.product_id=i.product_id
                    ORDER BY t.observed_at DESC,t.id DESC LIMIT 1) AS notional_24h
            FROM instruments i{where}
            ORDER BY i.symbol,i.product_id LIMIT ? OFFSET ?
        """, (*params, limit, offset)).fetchall()
        return {
            "items": [
                dict(row) | {"metadata": json.loads(row["metadata_json"])}
                for row in rows
            ],
            "total": count,
            "limit": limit,
            "offset": offset,
        }

    def instrument_detail(self, product_id: str) -> dict | None:
        row = self.connection.execute("""
            SELECT product_id,symbol,contract_type,state,first_seen,last_seen,
                   last_reconciled,metadata_json
            FROM instruments WHERE product_id=?
        """, (str(product_id),)).fetchone()
        if row is None:
            return None
        instrument = dict(row) | {"metadata": json.loads(row["metadata_json"])}
        instrument.pop("metadata_json")
        instrument["data_quality"] = [
            dict(item) | {"reasons": json.loads(item["reasons_json"])}
            for item in self.connection.execute("""
                SELECT data_type,state,observed_at,source,completeness,reasons_json
                FROM data_quality WHERE product_id=? ORDER BY observed_at DESC,id DESC
            """, (str(product_id),)).fetchall()
        ]
        latest_ticker = self.connection.execute("""
            SELECT observed_at,source,last_price,mark_price,index_price,
                   volume_24h_notional,raw_json
            FROM tickers WHERE product_id=? ORDER BY observed_at DESC,id DESC LIMIT 1
        """, (str(product_id),)).fetchone()
        instrument["latest_ticker"] = None
        if latest_ticker is not None:
            ticker = dict(latest_ticker)
            ticker["raw"] = json.loads(ticker.pop("raw_json"))
            instrument["latest_ticker"] = ticker
        latest_book = self.connection.execute("""
            SELECT observed_at,exchange_timestamp,source,sequence,integrity,
                   bids_json,asks_json
            FROM order_books WHERE product_id=? ORDER BY observed_at DESC,id DESC LIMIT 1
        """, (str(product_id),)).fetchone()
        instrument["latest_book"] = None
        if latest_book is not None:
            book = dict(latest_book)
            book["bids"] = json.loads(book.pop("bids_json"))
            book["asks"] = json.loads(book.pop("asks_json"))
            instrument["latest_book"] = book
        instrument["funding"] = [
            dict(item) for item in self.connection.execute("""
                SELECT timestamp,rate,source FROM funding
                WHERE product_id=? ORDER BY timestamp DESC LIMIT 100
            """, (str(product_id),)).fetchall()
        ]
        return instrument

    def save_candles(self, candles: Iterable[Candle]) -> None:
        rows = [(
            item.product_id, item.timeframe, _iso(item.start_time), item.open,
            item.high, item.low, item.close, item.volume, item.volume_unit,
            item.source, int(item.is_complete), int(item.is_derived),
        ) for item in candles]
        with self.connection:
            self.connection.executemany("""
                INSERT INTO candles
                (product_id,timeframe,start_time,open,high,low,close,volume,volume_unit,
                 source,is_complete,is_derived)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(product_id,timeframe,start_time,source) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                volume=excluded.volume,volume_unit=excluded.volume_unit,
                is_complete=excluded.is_complete,is_derived=excluded.is_derived
            """, rows)

    def load_candles(
        self,
        product_id: str,
        timeframe: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        source: str | None = None,
    ) -> list[Candle]:
        conditions = ["product_id=?", "timeframe=?"]
        params: list[object] = [str(product_id), timeframe]
        if start_time is not None:
            conditions.append("start_time>=?")
            params.append(_iso(start_time))
        if end_time is not None:
            conditions.append("start_time<?")
            params.append(_iso(end_time))
        if source is not None:
            conditions.append("source=?")
            params.append(source)
        rows = self.connection.execute(f"""
            SELECT c.*,i.symbol FROM candles c
            JOIN instruments i USING(product_id)
            WHERE {" AND ".join(conditions)}
            ORDER BY start_time,source
        """, params).fetchall()
        return [
            Candle(
                product_id=row["product_id"], symbol=row["symbol"],
                timeframe=row["timeframe"], start_time=_datetime(row["start_time"]),
                open=row["open"], high=row["high"], low=row["low"], close=row["close"],
                volume=row["volume"], volume_unit=row["volume_unit"], source=row["source"],
                is_complete=bool(row["is_complete"]), is_derived=bool(row["is_derived"]),
            )
            for row in rows
        ]

    def list_candle_rows(self, product_id: str, timeframe: str, limit: int = 500) -> list[dict]:
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self.connection.execute("""
            SELECT start_time,open,high,low,close,volume,volume_unit,source,
                   is_complete,is_derived
            FROM candles WHERE product_id=? AND timeframe=?
            ORDER BY start_time DESC LIMIT ?
        """, (str(product_id), timeframe, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def save_tickers(self, tickers: Iterable[TickerSnapshot]) -> None:
        rows = [(
            item.product_id, item.symbol, _iso(item.observed_at), item.source,
            item.last_price, item.mark_price, item.index_price,
            item.volume_24h_notional, _json(dict(item.raw)),
        ) for item in tickers]
        with self.connection:
            self.connection.executemany("""
                INSERT INTO tickers
                (product_id,symbol,observed_at,source,last_price,mark_price,index_price,
                 volume_24h_notional,raw_json) VALUES (?,?,?,?,?,?,?,?,?)
            """, rows)

    def save_trades(self, trades: Iterable[TradePrint]) -> None:
        rows = [(
            item.product_id, item.symbol, _iso(item.timestamp), item.price,
            item.quantity, item.aggressor_side, item.source, item.trade_id,
        ) for item in trades]
        with self.connection:
            self.connection.executemany("""
                INSERT INTO trades
                (product_id,symbol,timestamp,price,quantity,aggressor_side,source,trade_id)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(product_id,source,trade_id) WHERE trade_id IS NOT NULL DO NOTHING
            """, rows)

    def save_orderbook(self, snapshot: OrderBookSnapshot) -> None:
        with self.connection:
            self.connection.execute("""
                INSERT INTO order_books
                (product_id,symbol,observed_at,exchange_timestamp,source,sequence,integrity,
                 bids_json,asks_json) VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                snapshot.product_id, snapshot.symbol, _iso(snapshot.observed_at),
                _iso(snapshot.exchange_timestamp) if snapshot.exchange_timestamp else None,
                snapshot.source, snapshot.sequence, snapshot.integrity,
                _json([[level.price, level.quantity] for level in snapshot.bids]),
                _json([[level.price, level.quantity] for level in snapshot.asks]),
            ))

    def save_funding(self, observations: Iterable[FundingObservation]) -> None:
        rows = [(
            item.product_id, item.symbol, _iso(item.timestamp), item.rate, item.source,
        ) for item in observations]
        with self.connection:
            self.connection.executemany("""
                INSERT INTO funding(product_id,symbol,timestamp,rate,source)
                VALUES (?,?,?,?,?)
                ON CONFLICT(product_id,timestamp,source) DO UPDATE SET rate=excluded.rate
            """, rows)

    def save_quality(self, product_id: str, data_type: str, quality: DataQuality) -> None:
        if not data_type.strip():
            raise ValueError("data_type is required")
        with self.connection:
            self.connection.execute("""
                INSERT INTO data_quality
                (product_id,data_type,state,observed_at,source,completeness,reasons_json)
                VALUES (?,?,?,?,?,?,?)
            """, (
                str(product_id), data_type, quality.state,
                _iso(quality.observed_at) if quality.observed_at else None,
                quality.source, quality.completeness, _json(list(quality.reasons)),
            ))

    def save_experiment(
        self,
        experiment_id: str,
        strategy_id: str,
        strategy_version: str,
        dataset_version: str,
        split: dict,
        config: dict,
        metrics: dict | None = None,
        status: str = "registered",
        created_at: datetime | None = None,
    ) -> None:
        if not all((experiment_id.strip(), strategy_id.strip(), strategy_version.strip(),
                    dataset_version.strip(), status.strip())):
            raise ValueError("experiment identity, versions, and status are required")
        timestamp = _iso(created_at or datetime.now(timezone.utc))
        with self.connection:
            self.connection.execute("""
                INSERT INTO experiments
                (experiment_id,strategy_id,strategy_version,dataset_version,created_at,
                 split_json,config_json,metrics_json,status)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(experiment_id) DO UPDATE SET
                strategy_id=excluded.strategy_id,strategy_version=excluded.strategy_version,
                dataset_version=excluded.dataset_version,split_json=excluded.split_json,
                config_json=excluded.config_json,metrics_json=excluded.metrics_json,
                status=excluded.status
            """, (
                experiment_id, strategy_id, strategy_version, dataset_version, timestamp,
                _json(split), _json(config), _json(metrics or {}), status,
            ))

    def list_experiments(self, limit: int = 100, offset: int = 0) -> dict:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset non-negative")
        total = self.connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
        rows = self.connection.execute("""
            SELECT experiment_id,strategy_id,strategy_version,dataset_version,
                   created_at,split_json,config_json,metrics_json,status
            FROM experiments ORDER BY created_at DESC,experiment_id LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
        return {
            "items": [
                dict(row) | {
                    "split": json.loads(row["split_json"]),
                    "config": json.loads(row["config_json"]),
                    "metrics": json.loads(row["metrics_json"]),
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def save_paper_simulation(self, result, experiment_id: str | None = None) -> None:
        payload = result.__dict__ if hasattr(result, "__dict__") else dict(result)
        with self.connection:
            self.connection.execute("""
                INSERT INTO paper_simulations
                (experiment_id,strategy_id,product_id,symbol,decision_time,status,result_json)
                VALUES (?,?,?,?,?,?,?)
            """, (
                experiment_id, payload["strategy_id"], payload["product_id"], payload["symbol"],
                payload["decision_time"], payload["status"], _json(payload),
            ))

    def list_paper_simulations(
        self, limit: int = 100, offset: int = 0, symbol: str | None = None
    ) -> dict:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset non-negative")
        conditions = []
        params: list[object] = []
        if symbol:
            conditions.append("symbol=?")
            params.append(symbol)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        total = self.connection.execute(
            f"SELECT COUNT(*) FROM paper_simulations{where}", params
        ).fetchone()[0]
        rows = self.connection.execute(f"""
            SELECT id,experiment_id,strategy_id,product_id,symbol,decision_time,status,result_json
            FROM paper_simulations{where}
            ORDER BY decision_time DESC,id DESC LIMIT ? OFFSET ?
        """, (*params, limit, offset)).fetchall()
        return {
            "items": [
                dict(row) | {"result": json.loads(row["result_json"])}
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def save_variant_evaluation(
        self,
        variant_id: str,
        product_id: str,
        symbol: str,
        decision_time: datetime,
        status: str,
        reason: str,
        signal: dict | None = None,
        evaluated_at: datetime | None = None,
    ) -> None:
        if not all(value.strip() for value in (variant_id, product_id, symbol, status)):
            raise ValueError("variant evaluation identity and status are required")
        if status not in {
            "signal", "no_signal", "insufficient_data", "unavailable", "error",
        }:
            raise ValueError(f"unsupported variant evaluation status: {status}")
        with self.connection:
            self.connection.execute("""
                INSERT INTO variant_evaluations
                (variant_id,product_id,symbol,decision_time,evaluated_at,status,reason,signal_json)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(variant_id,product_id,decision_time) DO UPDATE SET
                evaluated_at=excluded.evaluated_at,status=excluded.status,
                reason=excluded.reason,signal_json=excluded.signal_json
            """, (
                variant_id, str(product_id), symbol, _iso(decision_time),
                _iso(evaluated_at or datetime.now(timezone.utc)), status, reason,
                _json(signal) if signal is not None else None,
            ))

    def list_variant_evaluations(
        self, limit: int = 100, offset: int = 0, symbol: str | None = None,
        variant_id: str | None = None,
    ) -> dict:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset non-negative")
        conditions = []
        params: list[object] = []
        if symbol:
            conditions.append("symbol=?")
            params.append(symbol)
        if variant_id:
            conditions.append("variant_id=?")
            params.append(variant_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        total = self.connection.execute(
            f"SELECT COUNT(*) FROM variant_evaluations{where}", params
        ).fetchone()[0]
        rows = self.connection.execute(f"""
            SELECT variant_id,product_id,symbol,decision_time,evaluated_at,
                   status,reason,signal_json
            FROM variant_evaluations{where}
            ORDER BY evaluated_at DESC,id DESC LIMIT ? OFFSET ?
        """, (*params, limit, offset)).fetchall()
        return {
            "items": [
                dict(row) | {
                    "signal": json.loads(row["signal_json"]) if row["signal_json"] else None
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def coverage_summary(self) -> dict:
        product_counts = {
            row["state"]: row["count"] for row in self.connection.execute("""
                SELECT state,COUNT(*) AS count FROM instruments GROUP BY state
            """)
        }
        data_counts = {
            row["data_type"]: row["count"] for row in self.connection.execute("""
                SELECT data_type,COUNT(*) AS count FROM data_quality GROUP BY data_type
            """)
        }
        return {
            "schema_version": int(self.connection.execute("PRAGMA user_version").fetchone()[0]),
            "instrument_count": sum(product_counts.values()),
            "instrument_states": product_counts,
            "data_quality_observations": data_counts,
            "candle_count": self.connection.execute("SELECT COUNT(*) FROM candles").fetchone()[0],
            "ticker_count": self.connection.execute("SELECT COUNT(*) FROM tickers").fetchone()[0],
            "trade_count": self.connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
            "orderbook_snapshot_count": self.connection.execute(
                "SELECT COUNT(*) FROM order_books"
            ).fetchone()[0],
            "funding_observation_count": self.connection.execute(
                "SELECT COUNT(*) FROM funding"
            ).fetchone()[0],
            "experiment_count": self.connection.execute(
                "SELECT COUNT(*) FROM experiments"
            ).fetchone()[0],
            "paper_simulation_count": self.connection.execute(
                "SELECT COUNT(*) FROM paper_simulations"
            ).fetchone()[0],
            "variant_evaluation_count": self.connection.execute(
                "SELECT COUNT(*) FROM variant_evaluations"
            ).fetchone()[0],
        }
