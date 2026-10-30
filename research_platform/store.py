from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .market_data import (
    Candle,
    DataQuality,
    FundingObservation,
    Instrument,
    OrderBookSnapshot,
    TickerSnapshot,
    TradePrint,
)


SCHEMA_VERSION = 1


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

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
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
        """)
        if version == 0:
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
        }
