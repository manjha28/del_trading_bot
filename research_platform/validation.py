from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from collections import Counter
from statistics import NormalDist
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


def walk_forward_splits(
    frame: pd.DataFrame,
    *,
    timestamp_column: str = "time",
    train_window: timedelta,
    validation_window: timedelta,
    test_window: timedelta,
    step: timedelta,
    embargo: timedelta = timedelta(0),
) -> list[ChronologicalPeriods[pd.DataFrame]]:
    """Create expanding chronological train/validation/test folds."""
    for name, value in (
        ("train_window", train_window),
        ("validation_window", validation_window),
        ("test_window", test_window),
        ("step", step),
    ):
        if value <= timedelta(0):
            raise ValueError(f"{name} must be positive")
    if frame.empty or timestamp_column not in frame:
        raise ValueError("walk-forward validation requires a non-empty timestamped dataset")
    times = pd.to_datetime(frame[timestamp_column], errors="raise")
    if times.dt.tz is None or times.isna().any():
        raise ValueError("walk-forward timestamps must be valid and timezone-aware")
    if not times.is_monotonic_increasing or times.duplicated().any():
        raise ValueError("walk-forward rows must be sorted with unique timestamps")
    first = times.iloc[0].to_pydatetime().astimezone(timezone.utc)
    last = times.iloc[-1].to_pydatetime().astimezone(timezone.utc)
    train_end = first + train_window
    folds = []
    while train_end + validation_window + test_window <= last:
        validation_end = train_end + validation_window
        test_end = validation_end + test_window
        fold = chronological_split(
            frame,
            timestamp_column=timestamp_column,
            train_end=train_end,
            validation_end=validation_end,
            out_of_sample_end=test_end,
            embargo=embargo,
        )
        if not fold.train.empty and not fold.validation.empty and not fold.out_of_sample.empty:
            folds.append(fold)
        train_end += step
    return folds


def purged_chronological_split(
    frame: pd.DataFrame,
    *,
    timestamp_column: str = "time",
    train_end: datetime,
    validation_end: datetime,
    out_of_sample_end: datetime | None = None,
    purge: timedelta,
    embargo: timedelta = timedelta(0),
) -> ChronologicalPeriods[pd.DataFrame]:
    """Split chronologically and remove boundary labels that overlap adjacent periods."""
    if purge < timedelta(0):
        raise ValueError("purge cannot be negative")
    base = chronological_split(
        frame,
        timestamp_column=timestamp_column,
        train_end=train_end,
        validation_end=validation_end,
        out_of_sample_end=out_of_sample_end,
        embargo=embargo,
    )
    train_boundary = pd.Timestamp(train_end).tz_convert("UTC") - pd.Timedelta(purge)
    validation_boundary = pd.Timestamp(validation_end).tz_convert("UTC") - pd.Timedelta(purge)
    train_times = pd.to_datetime(base.train[timestamp_column], utc=True)
    validation_times = pd.to_datetime(base.validation[timestamp_column], utc=True)
    train = base.train.loc[train_times < train_boundary].reset_index(drop=True)
    validation = base.validation.loc[
        validation_times < validation_boundary
    ].reset_index(drop=True)
    return ChronologicalPeriods(
        train=train,
        validation=validation,
        out_of_sample=base.out_of_sample,
        train_end=base.train_end,
        validation_end=base.validation_end,
        out_of_sample_end=base.out_of_sample_end,
        embargo=embargo + purge,
    )


def bootstrap_expectancy_interval(
    values: Iterable[float],
    *,
    confidence: float = 0.95,
    iterations: int = 2000,
    seed: int = 0,
) -> tuple[float, float] | None:
    """IID bootstrap interval for mean PnL; descriptive and not dependence-robust."""
    import numpy as np

    sample = np.asarray(list(values), dtype=float)
    if sample.ndim != 1 or not np.isfinite(sample).all():
        raise ValueError("bootstrap values must be finite scalar observations")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    if iterations < 100:
        raise ValueError("iterations must be at least 100")
    if len(sample) < 2:
        return None
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, len(sample), size=(iterations, len(sample)))
    means = sample[indices].mean(axis=1)
    tail = (1 - confidence) / 2
    lower, upper = np.quantile(means, [tail, 1 - tail])
    return float(lower), float(upper)


def deflated_sharpe_ratio(returns: Iterable[float], trial_count: int) -> float | None:
    """Approximate Deflated Sharpe probability using skew/kurtosis and trial count."""
    import numpy as np

    sample = np.asarray(list(returns), dtype=float)
    if sample.ndim != 1 or not np.isfinite(sample).all():
        raise ValueError("returns must be finite scalar observations")
    if trial_count < 1:
        raise ValueError("trial_count must be positive")
    if len(sample) < 3:
        return None
    std = float(sample.std(ddof=1))
    if std <= 0:
        return None
    sharpe = float(sample.mean() / std)
    centered = sample - sample.mean()
    skew = float(np.mean(centered ** 3) / (np.mean(centered ** 2) ** 1.5))
    kurtosis = float(np.mean(centered ** 4) / (np.mean(centered ** 2) ** 2))
    sample_count = len(sample)
    if trial_count == 1:
        benchmark = 0.0
    else:
        normal = NormalDist()
        gamma = 0.5772156649015329
        sigma_sharpe = 1 / math.sqrt(sample_count - 1)
        benchmark = sigma_sharpe * (
            (1 - gamma) * normal.inv_cdf(1 - 1 / trial_count)
            + gamma * normal.inv_cdf(1 - 1 / (trial_count * math.e))
        )
    variance = (
        1 - skew * sharpe + ((kurtosis - 1) / 4) * sharpe * sharpe
    )
    if variance <= 0:
        return None
    z = (sharpe - benchmark) * math.sqrt(sample_count - 1) / math.sqrt(variance)
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


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
    costed = [
        row for row in simulated
        if _field(row, "net_pnl") is not None
    ]
    uncosted = [
        row for row in simulated
        if _field(row, "net_pnl") is None
    ]
    rejected_reasons = Counter(
        str(_field(row, "reason")) for row in rows
        if _field(row, "status") != "simulated"
    )
    pnl = [float(_field(row, "net_pnl")) for row in costed]
    if any(not math.isfinite(value) for value in pnl):
        raise ValueError("paper results contain non-finite PnL")
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    expectancy_ci = bootstrap_expectancy_interval(pnl)
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
        "costed_simulated_count": len(costed),
        "uncosted_exit_count": len(uncosted),
        "rejected_count": len(rows) - len(simulated),
        "rejection_reasons": dict(rejected_reasons),
        "net_pnl_total": sum(pnl) if pnl else None,
        "net_expectancy_per_simulated_trade": sum(pnl) / len(pnl) if pnl else None,
        "expectancy_bootstrap_iid_95pct": expectancy_ci,
        "win_rate_pct": len(wins) / len(pnl) * 100 if pnl else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "profit_factor_is_unbounded": bool(wins and not losses),
        "realized_pnl_sequence_drawdown": max_drawdown,
        "drawdown_basis": "chronologically ordered PnL sequence; not portfolio drawdown",
        "confidence_interval": None,
        "significance_warning": (
            "Descriptive in-sample/paper metrics only; uncosted exit-only outcomes "
            "are excluded from net PnL metrics. The reported expectancy interval "
            "is iid bootstrap and does not correct temporal dependence. No "
            "walk-forward correction, portfolio drawdown, or selection-bias "
            "adjustment is implied."
        ),
    }


def _field(value: object, key: str):
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)
