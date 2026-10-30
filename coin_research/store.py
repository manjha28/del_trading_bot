from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, allow_nan=False)


class ResearchStore:
    """Persistent catalog, candle history, observations, and shadow event log."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                product_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                state TEXT,
                contract_type TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                last_reconciled TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS products_symbol_idx ON products(symbol);
            CREATE TABLE IF NOT EXISTS candles (
                product_id TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                candle_time TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                PRIMARY KEY(product_id, timeframe, candle_time),
                FOREIGN KEY(product_id) REFERENCES products(product_id)
            );
            CREATE INDEX IF NOT EXISTS candles_history_idx
                ON candles(product_id, timeframe, candle_time);
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                run_at TEXT NOT NULL,
                state TEXT NOT NULL,
                sleeping_score REAL,
                confidence REAL,
                data_json TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(product_id)
            );
            CREATE INDEX IF NOT EXISTS observations_latest_idx
                ON observations(product_id, run_at DESC);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                event_time TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(product_id)
            );
            CREATE INDEX IF NOT EXISTS events_product_idx
                ON events(product_id, event_time DESC);
            CREATE TABLE IF NOT EXISTS event_outcomes (
                event_id INTEGER NOT NULL,
                horizon_hours INTEGER NOT NULL,
                evaluated_at TEXT NOT NULL,
                reference_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                gross_return_pct REAL NOT NULL,
                estimated_cost_pct REAL,
                net_return_pct REAL,
                PRIMARY KEY(event_id,horizon_hours),
                FOREIGN KEY(event_id) REFERENCES events(id)
            );
        """)
        self.connection.commit()

    @staticmethod
    def product_id(product: dict) -> str:
        value = product.get("id")
        return str(value if value is not None else product["symbol"])

    def reconcile_products(self, products: list[dict], now: str | None = None) -> None:
        timestamp = now or datetime.now(timezone.utc).isoformat()
        returned = set()
        with self.connection:
            for product in products:
                if product.get("contract_type") != "perpetual_futures":
                    continue
                product_id = self.product_id(product)
                returned.add(product_id)
                existing = self.connection.execute(
                    "SELECT first_seen FROM products WHERE product_id=?", (product_id,)
                ).fetchone()
                first_seen = existing["first_seen"] if existing else timestamp
                self.connection.execute("""
                    INSERT INTO products
                    (product_id,symbol,state,contract_type,first_seen,last_seen,last_reconciled,metadata_json)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(product_id) DO UPDATE SET
                    symbol=excluded.symbol,state=excluded.state,contract_type=excluded.contract_type,
                    last_seen=excluded.last_seen,last_reconciled=excluded.last_reconciled,
                    metadata_json=excluded.metadata_json
                """, (product_id, product["symbol"], product.get("state"), product.get("contract_type"),
                      first_seen, timestamp, timestamp, _json(product)))
            if returned:
                placeholders = ",".join("?" for _ in returned)
                self.connection.execute(
                    f"""UPDATE products SET state='not_returned',last_reconciled=?
                        WHERE product_id NOT IN ({placeholders})""",
                    (timestamp, *sorted(returned)),
                )

    def save_candles(self, product_id: str, timeframe: str, frame) -> None:
        if frame is None or frame.empty:
            return
        rows = []
        for row in frame.itertuples(index=False):
            stamp = row.time.isoformat() if hasattr(row.time, "isoformat") else str(row.time)
            rows.append((str(product_id), timeframe, stamp, float(row.open), float(row.high),
                         float(row.low), float(row.close), float(row.volume)))
        with self.connection:
            self.connection.executemany("""
                INSERT INTO candles
                (product_id,timeframe,candle_time,open,high,low,close,volume)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(product_id,timeframe,candle_time) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,
                close=excluded.close,volume=excluded.volume
            """, rows)

    def latest_candle_time(self, product_id: str, timeframe: str) -> str | None:
        row = self.connection.execute("""
            SELECT MAX(candle_time) AS candle_time FROM candles
            WHERE product_id=? AND timeframe=?
        """, (str(product_id), timeframe)).fetchone()
        return row["candle_time"]

    def load_candles(self, product_id: str, timeframe: str, limit: int):
        import pandas as pd

        rows = self.connection.execute("""
            SELECT candle_time AS time,open,high,low,close,volume FROM candles
            WHERE product_id=? AND timeframe=? ORDER BY candle_time DESC LIMIT ?
        """, (str(product_id), timeframe, int(limit))).fetchall()
        frame = pd.DataFrame([dict(row) for row in reversed(rows)])
        if not frame.empty:
            frame["time"] = pd.to_datetime(frame.time, utc=True)
        return frame

    def previous_observation(self, product_id: str) -> dict | None:
        row = self.connection.execute("""
            SELECT state,data_json FROM observations
            WHERE product_id=? ORDER BY run_at DESC,id DESC LIMIT 1
        """, (str(product_id),)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["data_json"])
        payload["_state"] = row["state"]
        return payload

    def historical_feature_values(
        self, product_id: str, feature_names: tuple[str, ...], limit: int = 120
    ) -> dict[str, list[float]]:
        rows = self.connection.execute("""
            SELECT data_json FROM observations WHERE product_id=?
            ORDER BY run_at DESC,id DESC LIMIT ?
        """, (str(product_id), int(limit))).fetchall()
        values = {name: [] for name in feature_names}
        for row in reversed(rows):
            features = json.loads(row["data_json"]).get("features", {})
            for name in feature_names:
                try:
                    value = float(features[name])
                    if value == value and abs(value) != float("inf"):
                        values[name].append(value)
                except (KeyError, TypeError, ValueError):
                    continue
        return values

    def record_observation(self, product_id: str, observation: dict) -> None:
        run_at = observation.get("as_of") or datetime.now(timezone.utc).isoformat()
        score = observation.get("sleeping_score")
        if score is None:
            components = observation.get("features", {})
            percentiles = [
                components.get("volume_compression_percentile_60d"),
                components.get("range_compression_percentile"),
                components.get("price_inactivity_percentile"),
            ]
            valid = [100 - float(value) for value in percentiles if value is not None]
            score = sum(valid) / len(valid) if len(valid) >= 2 else None
        with self.connection:
            self.connection.execute("""
                INSERT INTO observations(product_id,run_at,state,sleeping_score,confidence,data_json)
                VALUES (?,?,?,?,?,?)
            """, (str(product_id), run_at, observation.get("state", "unknown"), score,
                  observation.get("data_quality", {}).get("confidence"), _json(observation)))
            previous = self.connection.execute("""
                SELECT state FROM observations WHERE product_id=?
                ORDER BY run_at DESC,id DESC LIMIT 1 OFFSET 1
            """, (str(product_id),)).fetchone()
            if previous is not None and previous["state"] != observation.get("state"):
                self._record_event(product_id, run_at, "state_transition", {
                    "from": previous["state"], "to": observation.get("state"),
                    "reasons": observation.get("state_reasons", []),
                })
            if observation.get("awakening", {}).get("active_components"):
                self._record_event(product_id, run_at, "awakening", observation["awakening"])
            for signal in observation.get("signals", []):
                if not signal.get("available") or signal.get("triggered") is False:
                    continue
                details = dict(signal)
                market = observation.get("market", {})
                specs = observation.get("execution", {}).get("contract_specs", {})
                details.update({
                    "signal_candle_close_time": observation.get("signal_candle_close_time"),
                    "reference_price": market.get("price"),
                    "execution_notional": market.get("execution_notional"),
                    "spread_pct": market.get("spread_pct"),
                    "estimated_buy_impact_pct": market.get("estimated_buy_impact_pct"),
                    "estimated_sell_impact_pct": market.get("estimated_sell_impact_pct"),
                    "depth_notional_verified": market.get("depth_notional_verified", False),
                    "taker_fee_rate": specs.get("taker_fee_rate"),
                    "data_fresh": observation.get("data_quality", {}).get("hourly_fresh", False),
                })
                close_time = details.get("signal_candle_close_time")
                recent = self.connection.execute("""
                    SELECT details_json FROM events
                    WHERE product_id=? AND event_type='shadow_signal'
                    ORDER BY id DESC LIMIT 50
                """, (str(product_id),)).fetchall()
                duplicate = any(
                    (item := json.loads(row["details_json"])).get("variant") == details.get("variant")
                    and item.get("signal_candle_close_time") == close_time
                    for row in recent
                )
                if not duplicate:
                    self._record_event(product_id, run_at, "shadow_signal", details)
            for issue in observation.get("data_quality", {}).get("issues", []):
                self._record_event(product_id, run_at, "data_gap", {"reason": issue})
            for reason in observation.get("execution", {}).get("reasons", []):
                self._record_event(product_id, run_at, "execution_rejection", {"reason": reason})

    def _record_event(self, product_id: str, event_time: str, event_type: str, details: dict) -> None:
        self.connection.execute(
            "INSERT INTO events(product_id,event_time,event_type,details_json) VALUES (?,?,?,?)",
            (str(product_id), event_time, event_type, _json(details)),
        )

    def latest_observations(self) -> list[dict]:
        rows = self.connection.execute("""
            SELECT p.product_id,p.symbol,p.state AS product_state,o.run_at,o.state,
                   o.sleeping_score,o.confidence,o.data_json
            FROM products p
            LEFT JOIN observations o ON o.id=(
                SELECT id FROM observations WHERE product_id=p.product_id
                ORDER BY run_at DESC,id DESC LIMIT 1
            )
            ORDER BY p.symbol
        """).fetchall()
        return [dict(row) | {"observation": json.loads(row["data_json"])
                             if row["data_json"] else None} for row in rows]

    def event_counts(self) -> dict[str, int]:
        return {row["event_type"]: row["count"] for row in self.connection.execute(
            "SELECT event_type,COUNT(*) AS count FROM events GROUP BY event_type"
        )}

    def update_signal_outcomes(self, product_id: str, hourly, now: str | None = None) -> None:
        if hourly is None or hourly.empty:
            return
        import pandas as pd

        frame = hourly.copy()
        frame["time"] = pd.to_datetime(frame.time, utc=True)
        frame["close"] = pd.to_numeric(frame.close, errors="coerce")
        frame = frame.dropna(subset=["time", "close"]).sort_values("time")
        frame["close_time"] = frame.time + pd.Timedelta(hours=1)
        evaluated_at = now or datetime.now(timezone.utc).isoformat()
        now_stamp = pd.Timestamp(evaluated_at)
        if now_stamp.tzinfo is None:
            now_stamp = now_stamp.tz_localize("UTC")
        events = self.connection.execute("""
            SELECT e.id,e.event_time,e.details_json FROM events e
            LEFT JOIN event_outcomes o ON o.event_id=e.id
            WHERE e.product_id=? AND e.event_type='shadow_signal'
            ORDER BY e.id
        """, (str(product_id),)).fetchall()
        additions = []
        for event in events:
            details = json.loads(event["details_json"])
            if not details.get("data_fresh", False):
                continue
            reference = details.get("reference_price")
            signal_close = details.get("signal_candle_close_time")
            try:
                reference = float(reference)
                signal_close = pd.Timestamp(signal_close)
                if signal_close.tzinfo is None:
                    signal_close = signal_close.tz_localize("UTC")
            except (TypeError, ValueError):
                continue
            direction = details.get("direction")
            if reference <= 0 or direction not in (
                "bearish_research_only", "bullish_research_only"
            ):
                continue
            cost = None
            spread = details.get("spread_pct")
            buy_impact = details.get("estimated_buy_impact_pct")
            sell_impact = details.get("estimated_sell_impact_pct")
            fee = details.get("taker_fee_rate")
            try:
                if (
                    details.get("depth_notional_verified") is True
                    and None not in (spread, buy_impact, sell_impact, fee)
                ):
                    cost = float(spread) + float(buy_impact) + float(sell_impact) + 2 * float(fee) * 100
                    if cost < 0:
                        cost = None
            except (TypeError, ValueError):
                cost = None
            for horizon in (1, 4, 24):
                exists = self.connection.execute(
                    "SELECT 1 FROM event_outcomes WHERE event_id=? AND horizon_hours=?",
                    (event["id"], horizon),
                ).fetchone()
                if exists:
                    continue
                target = signal_close + pd.Timedelta(hours=horizon)
                candidates = frame.loc[(frame.close_time >= target) & (frame.close_time <= now_stamp)]
                if candidates.empty:
                    continue
                exit_price = float(candidates.iloc[0].close)
                gross_return = (
                    (reference - exit_price) / reference * 100
                    if direction == "bearish_research_only"
                    else (exit_price / reference - 1) * 100
                )
                additions.append((
                    event["id"], horizon, evaluated_at, reference, exit_price, gross_return,
                    cost, gross_return - cost if cost is not None else None,
                ))
        if additions:
            with self.connection:
                self.connection.executemany("""
                    INSERT OR IGNORE INTO event_outcomes
                    (event_id,horizon_hours,evaluated_at,reference_price,exit_price,
                     gross_return_pct,estimated_cost_pct,net_return_pct)
                    VALUES (?,?,?,?,?,?,?,?)
                """, additions)

    def outcome_records(self) -> list[dict]:
        rows = self.connection.execute("""
            SELECT p.symbol,e.event_time,e.details_json,o.horizon_hours,
                   o.reference_price,o.exit_price,o.gross_return_pct,
                   o.estimated_cost_pct,o.net_return_pct
            FROM event_outcomes o JOIN events e ON e.id=o.event_id
            JOIN products p ON p.product_id=e.product_id
            WHERE e.event_type='shadow_signal'
            ORDER BY e.event_time,o.horizon_hours
        """).fetchall()
        return [
            dict(row) | {"variant": json.loads(row["details_json"]).get("variant")}
            for row in rows
        ]

    def close(self) -> None:
        self.connection.close()
