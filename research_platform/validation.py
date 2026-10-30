from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections import Counter
import math
from typing import Generic, Iterable, TypeVar

import pandas as pd


T = TypeVar("T")


@dataclass(frozen=True)
class ChronologicalPeriods(Generic[T]):
    train: T
    validation: T
    out_of_sample: T
    train_end: datetime
    validation_end: datetime
    out_of_sample_end: datetime
    embargo: timedelta


def chronological_split(
    frame: pd.DataFrame,
    *,
    timestamp_column: str = "time",
    train_end: datetime,
    validation_end: datetime,
    out_of_sample_end: datetime | None = None,
    embargo: timedelta = timedelta(0),
) -> ChronologicalPeriods[pd.DataFrame]:
    """Split observations in time order; embargo removes boundary-adjacent rows."""
    for name, value in (
        ("train_end", train_end),
        ("validation_end", validation_end),
    ):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
    if out_of_sample_end is not None and (
        out_of_sample_end.tzinfo is None or out_of_sample_end.utcoffset() is None
    ):
        raise ValueError("out_of_sample_end must be timezone-aware")
    if train_end >= validation_end:
        raise ValueError("train_end must be before validation_end")
    if out_of_sample_end is not None and validation_end >= out_of_sample_end:
        raise ValueError("validation_end must be before out_of_sample_end")
    if embargo < timedelta(0):
        raise ValueError("embargo cannot be negative")
    if timestamp_column not in frame.columns:
        raise ValueError(f"missing timestamp column: {timestamp_column}")
    if frame.empty:
        raise ValueError("cannot split an empty dataset")
    times = pd.to_datetime(frame[timestamp_column], errors="raise")
    if times.dt.tz is None or times.isna().any():
        raise ValueError("split timestamps must be valid and timezone-aware")
    if not times.is_monotonic_increasing:
        raise ValueError("input rows must be chronologically sorted")
    if times.duplicated().any():
        raise ValueError("input timestamps must be unique")

    train_cutoff = pd.Timestamp(train_end).tz_convert("UTC")
    validation_cutoff = pd.Timestamp(validation_end).tz_convert("UTC")
    final_cutoff = (
        pd.Timestamp(out_of_sample_end).tz_convert("UTC")
        if out_of_sample_end is not None else times.iloc[-1]
    )
    gap = pd.Timedelta(embargo)
    train = frame.loc[times < train_cutoff].copy()
    validation = frame.loc[
        (times >= train_cutoff + gap) & (times < validation_cutoff)
    ].copy()
    out_of_sample = frame.loc[
        (times >= validation_cutoff + gap) & (times <= final_cutoff)
    ].copy()
    return ChronologicalPeriods(
        train=train.reset_index(drop=True),
        validation=validation.reset_index(drop=True),
        out_of_sample=out_of_sample.reset_index(drop=True),
        train_end=train_end.astimezone(timezone.utc),
        validation_end=validation_end.astimezone(timezone.utc),
        out_of_sample_end=final_cutoff.to_pydatetime().astimezone(timezone.utc),
        embargo=embargo,
    )


def summarize_paper_results(results: Iterable[object]) -> dict:
    """Report simple realized metrics and candidate coverage without implying OOS significance."""
    rows = list(results)
    rows.sort(key=lambda row: str(
        _field(row, "exit_time") or _field(row, "decision_time") or ""
    ))
    simulated = [
        row for row in rows
        if _field(row, "status") == "simulated"
    ]
    rejected_reasons = Counter(
        str(_field(row, "reason")) for row in rows
        if _field(row, "status") != "simulated"
    )
    pnl = [float(_field(row, "net_pnl")) for row in simulated]
    if any(not math.isfinite(value) for value in pnl):
        raise ValueError("paper results contain non-finite PnL")
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in pnl:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return {
        "candidate_count": len(rows),
        "simulated_count": len(simulated),
        "rejected_count": len(rows) - len(simulated),
        "rejection_reasons": dict(rejected_reasons),
        "net_pnl_total": sum(pnl),
        "net_expectancy_per_simulated_trade": sum(pnl) / len(pnl) if pnl else None,
        "win_rate_pct": len(wins) / len(pnl) * 100 if pnl else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "profit_factor_is_unbounded": bool(wins and not losses),
        "realized_pnl_sequence_drawdown": max_drawdown,
        "drawdown_basis": "chronologically ordered PnL sequence; not portfolio drawdown",
        "confidence_interval": None,
        "significance_warning": (
            "Descriptive in-sample/paper metrics only; no confidence interval, "
            "walk-forward correction, portfolio drawdown, or selection-bias "
            "adjustment is computed."
        ),
    }


def _field(value: object, key: str):
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)
