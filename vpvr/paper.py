"""Persistent deterministic paper broker. Contains NO exchange order API calls.

Signals are stop-entry triggers. They can fill only on a later completed 1m
bar. Stops take priority if both stop and target touch in one bar because the
intrabar path cannot be inferred from OHLCV.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from pathlib import Path
import json
import os
import tempfile
import pandas as pd


@dataclass
class PaperPosition:
    symbol: str
    strategy: str
    direction: str
    entry: float
    stop: float
    targets: list[float]
    quantity: float
    remaining: float
    opened_at: str
    entry_fee: float
    contract_multiplier: float = 1.0
    target_index: int = 0
    realized_pnl: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    funding_paid: float = 0.0
    last_funding_at: str | None = None
    last_mark: float | None = None
    last_candle_low: float | None = None
    last_candle_high: float | None = None
    regime: str = "uncertain_no_trade"


class PaperBroker:
    def __init__(self, path="data/vpvr_paper_state.json", initial_balance=1000.0,
                 fee_rate=0.0005, slippage_bps=2.0, max_chase_bps=10.0,
                 trailing_buffer_pct=0.001, max_hold_minutes=180):
        if initial_balance <= 0 or fee_rate < 0 or slippage_bps < 0 or max_chase_bps < 0:
            raise ValueError("invalid paper broker configuration")
        if trailing_buffer_pct < 0 or max_hold_minutes < 1:
            raise ValueError("invalid trailing/holding configuration")
        self.path = Path(path)
        self.fee_rate = float(fee_rate)
        self.slippage_bps = float(slippage_bps)
        self.max_chase_bps = float(max_chase_bps)
        self.trailing_buffer_pct = float(trailing_buffer_pct)
        self.max_hold_minutes = int(max_hold_minutes)
        self.initial_balance = float(initial_balance)
        self.state = self._load()

    def _load(self):
        if self.path.exists():
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("paper state is unreadable; refusing to reset virtual account") from exc
            if state.get("mode") != "PAPER_ONLY":
                raise RuntimeError("paper state mode invalid; refusing to continue")
            state.setdefault("pending", {})
            state.setdefault("positions", {})
            state.setdefault("trades", [])
            state.setdefault("balance", self.initial_balance)
            state.setdefault("last_processed", {})
            state.setdefault("events", [])
            return state
        return {"mode": "PAPER_ONLY", "balance": self.initial_balance, "pending": {},
                "positions": {}, "trades": [], "last_processed": {}, "events": []}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix="vpvr-state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @property
    def balance(self):
        return float(self.state["balance"])

    def equity(self, marks=None):
        value = self.balance
        for symbol, position in self.state["positions"].items():
            mark = float((marks or {}).get(symbol, position.get("last_mark") or position["entry"]))
            qty = float(position["remaining"])
            mult = float(position.get("contract_multiplier", 1.0))
            value += ((mark - position["entry"]) if position["direction"] == "LONG"
                      else (position["entry"] - mark)) * qty * mult
        return value

    def queue_signal(self, signal, quantity, contract_multiplier=1.0, regime="uncertain_no_trade"):
        key = f"{signal.symbol}:{signal.strategy}:{signal.timestamp}"
        if signal.symbol in self.state["positions"]:
            return False, "position already open"
        if any(item["symbol"] == signal.symbol for item in self.state["pending"].values()):
            return False, "pending trigger already exists for symbol"
        if key in self.state["pending"]:
            return False, "signal already queued"
        if quantity <= 0 or contract_multiplier <= 0:
            return False, "invalid paper quantity"
        self.state["pending"][key] = {
            "key": key, "symbol": signal.symbol, "strategy": signal.strategy,
            "direction": signal.direction, "entry": float(signal.entry),
            "stop": float(signal.stop_loss), "targets": list(signal.targets[:3]),
            "quantity": float(quantity), "contract_multiplier": float(contract_multiplier),
            "created_at": pd.Timestamp(signal.timestamp).isoformat(),
            "bars_waited": 0, "expiry_candles": int(signal.expiry_candles),
            "regime": regime
        }
        self.state["events"].append({"event": "signal_queued", "key": key})
        self._save()
        return True, "trigger queued"

    def process_candle(self, symbol, candle, funding_event=None):
        """Process one unique completed 1m candle for a symbol."""
        timestamp = pd.Timestamp(candle["time"])
        last = self.state["last_processed"].get(symbol)
        if last is not None and timestamp <= pd.Timestamp(last):
            return []
        high, low = float(candle["high"]), float(candle["low"])
        open_price, close = float(candle["open"]), float(candle["close"])
        if not (0 < low <= high and open_price > 0 and close > 0):
            raise ValueError("invalid paper candle")
        events = []
        self.state["last_processed"][symbol] = timestamp.isoformat()

        position = self.state["positions"].get(symbol)
        if position:
            position["last_mark"] = close
            # Once target 2 is booked, trail the remaining runner behind the prior
            # completed candle's structural low/high; stops may only tighten.
            if position["target_index"] >= 2 and position["remaining"] > 1e-10:
                prior_low = position.get("last_candle_low")
                prior_high = position.get("last_candle_high")
                if position["direction"] == "LONG" and prior_low is not None:
                    position["stop"] = max(position["stop"], float(prior_low) * (1 - self.trailing_buffer_pct))
                elif position["direction"] == "SHORT" and prior_high is not None:
                    position["stop"] = min(position["stop"], float(prior_high) * (1 + self.trailing_buffer_pct))
            self._apply_funding(position, timestamp, funding_event, events)
            direction = position["direction"]
            stop_hit = low <= position["stop"] if direction == "LONG" else high >= position["stop"]
            if stop_hit:
                fill = position["stop"] * (1 - self.slippage_bps / 10000 if direction == "LONG"
                                           else 1 + self.slippage_bps / 10000)
                self._close(symbol, fill, "STOP_OR_TRAIL", timestamp, events)
                self._save()
                return events

            targets = position["targets"]
            fractions = [1.0] if len(targets) == 1 else ([0.30, 0.45] if len(targets) == 2 else [0.30, 0.45, 0.25])
            while position["target_index"] < len(targets):
                index = position["target_index"]
                target = float(targets[index])
                hit = high >= target if direction == "LONG" else low <= target
                if not hit:
                    break
                qty = min(position["remaining"], position["quantity"] * fractions[min(index, len(fractions) - 1)])
                gross = ((target - position["entry"]) if direction == "LONG"
                         else (position["entry"] - target)) * qty * position["contract_multiplier"]
                fee = abs(target * qty * position["contract_multiplier"]) * self.fee_rate
                net = gross - fee
                position["gross_pnl"] += gross
                position["fees"] += fee
                position["realized_pnl"] += net
                position["remaining"] -= qty
                self.state["balance"] += net
                position["target_index"] += 1
                if index == 0:
                    position["stop"] = position["entry"]
                elif index == 1:
                    position["stop"] = float(targets[0])
                events.append({"symbol": symbol, "event": "partial_target", "target": target,
                               "quantity": qty, "net_pnl": net})
            if position["remaining"] <= 1e-10:
                self._finish(symbol, timestamp, float(targets[-1]), "TARGETS")
                events.append({"symbol": symbol, "event": "closed", "reason": "TARGETS"})
            elif (timestamp - pd.Timestamp(position["opened_at"])).total_seconds() / 60 >= self.max_hold_minutes:
                self._close(symbol, close * (1 - self.slippage_bps / 10000 if direction == "LONG"
                                             else 1 + self.slippage_bps / 10000),
                            "TIMEOUT", timestamp, events)
            elif symbol in self.state["positions"]:
                position["last_candle_low"] = low
                position["last_candle_high"] = high
            self._save()
            return events

        # Pending entry trigger: not eligible until a later bar than signal timestamp.
        for key, pending in list(self.state["pending"].items()):
            if pending["symbol"] != symbol or timestamp <= pd.Timestamp(pending["created_at"]):
                continue
            pending["bars_waited"] += 1
            direction = pending["direction"]
            entry, stop = float(pending["entry"]), float(pending["stop"])
            if pending["bars_waited"] > pending["expiry_candles"]:
                self.state["pending"].pop(key)
                events.append({"symbol": symbol, "event": "pending_expired"})
                continue
            stop_invalidated = low <= stop if direction == "LONG" else high >= stop
            chase = (open_price > entry * (1 + self.max_chase_bps / 10000) if direction == "LONG"
                     else open_price < entry * (1 - self.max_chase_bps / 10000))
            triggered = high >= entry if direction == "LONG" else low <= entry
            if stop_invalidated or chase:
                self.state["pending"].pop(key)
                reason = "stop invalidation before entry" if stop_invalidated else "gap exceeded chase limit"
                events.append({"symbol": symbol, "event": "pending_cancelled", "reason": reason})
                continue
            if not triggered:
                continue
            reference = max(entry, open_price) if direction == "LONG" else min(entry, open_price)
            fill = reference * (1 + self.slippage_bps / 10000 if direction == "LONG"
                                else 1 - self.slippage_bps / 10000)
            qty, multiplier = float(pending["quantity"]), float(pending["contract_multiplier"])
            fee = abs(fill * qty * multiplier) * self.fee_rate
            position = asdict(PaperPosition(
                symbol=symbol, strategy=pending["strategy"], direction=direction, entry=fill,
                stop=stop, targets=list(pending["targets"]), quantity=qty, remaining=qty,
                opened_at=timestamp.isoformat(), entry_fee=fee, contract_multiplier=multiplier,
                fees=fee, realized_pnl=-fee, last_funding_at=None,
                last_mark=fill, last_candle_low=low, last_candle_high=high, regime=pending["regime"]
            ))
            self.state["balance"] -= fee
            self.state["positions"][symbol] = position
            self.state["pending"].pop(key)
            events.append({"symbol": symbol, "event": "entry_filled", "price": fill,
                           "quantity": qty, "entry_fee": fee})
        self._save()
        return events

    def _apply_funding(self, position, candle_start, funding_event, events):
        """Apply only an explicit, timestamped funding realization event.

        A current funding-rate quote is not proof that a settlement occurred.
        Never accrue synthetic 8-hour periods from the entry timestamp.
        """
        if not isinstance(funding_event, dict):
            return
        try:
            event_time = pd.Timestamp(funding_event["timestamp"])
            rate = float(funding_event["rate"])
        except (KeyError, TypeError, ValueError):
            return
        candle_start = pd.Timestamp(candle_start)
        candle_end = candle_start + pd.Timedelta(minutes=1)
        if event_time.tzinfo is None:
            event_time = event_time.tz_localize("UTC")
        else:
            event_time = event_time.tz_convert("UTC")
        if not (candle_start < event_time <= candle_end):
            return
        if event_time <= pd.Timestamp(position["opened_at"]):
            return
        if position.get("last_funding_at") == event_time.isoformat():
            return
        notional = (float(position["remaining"]) * float(position["entry"]) *
                    float(position["contract_multiplier"]))
        cash_flow = (-notional * rate if position["direction"] == "LONG"
                     else notional * rate)
        position["funding_paid"] = float(position.get("funding_paid", 0.0)) - cash_flow
        position["realized_pnl"] = float(position.get("realized_pnl", 0.0)) + cash_flow
        position["last_funding_at"] = event_time.isoformat()
        self.state["balance"] += cash_flow
        events.append({"symbol": position["symbol"], "event": "funding",
                       "timestamp": event_time.isoformat(), "rate": rate,
                       "cash_flow": cash_flow, "source": funding_event.get("source", "explicit_event")})

    def _close(self, symbol, price, reason, timestamp, events):
        position = self.state["positions"][symbol]
        qty = float(position["remaining"])
        gross = ((price - position["entry"]) if position["direction"] == "LONG"
                 else (position["entry"] - price)) * qty * position["contract_multiplier"]
        fee = abs(price * qty * position["contract_multiplier"]) * self.fee_rate
        net = gross - fee
        position["gross_pnl"] += gross
        position["fees"] += fee
        position["realized_pnl"] += net
        self.state["balance"] += net
        self._finish(symbol, timestamp, price, reason)
        events.append({"symbol": symbol, "event": "closed", "reason": reason,
                       "net_pnl": self.state["trades"][-1]["net_pnl"]})

    def _finish(self, symbol, timestamp, price, reason):
        position = self.state["positions"].pop(symbol)
        self.state["trades"].append({**position, "closed_at": pd.Timestamp(timestamp).isoformat(),
                                     "exit_price": price, "exit_reason": reason,
                                     "net_pnl": position["realized_pnl"]})
        self.state["events"].append({"event": "position_closed", "symbol": symbol, "reason": reason})
        self._save()

    def cancel_all_pending(self, reason="operator_cancel"):
        count = len(self.state["pending"])
        self.state["pending"] = {}
        self.state["events"].append({"event": "pending_cancel_all", "reason": reason, "count": count})
        self._save()
        return count
