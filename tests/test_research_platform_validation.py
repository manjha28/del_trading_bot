from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

import pandas as pd

from research_platform.validation import (
    bootstrap_expectancy_interval,
    chronological_split,
    deflated_sharpe_ratio,
    moving_block_bootstrap_expectancy_interval,
    purged_chronological_split,
    summarize_paper_results,
    walk_forward_splits,
)


UTC = timezone.utc


class ChronologicalValidationTests(unittest.TestCase):
    def setUp(self):
        self.frame = pd.DataFrame({
            "time": pd.date_range("2026-01-01", periods=10, freq="D", tz="UTC"),
            "value": range(10),
        })

    def test_splits_are_disjoint_ordered_and_apply_embargo(self):
        train_end = datetime(2026, 1, 5, tzinfo=UTC)
        validation_end = datetime(2026, 1, 8, tzinfo=UTC)
        split = chronological_split(
            self.frame, train_end=train_end, validation_end=validation_end,
            embargo=timedelta(days=1),
        )
        self.assertEqual(list(split.train.value), [0, 1, 2, 3])
        self.assertEqual(list(split.validation.value), [5, 6])
        self.assertEqual(list(split.out_of_sample.value), [8, 9])
        self.assertLess(split.train.time.max(), split.validation.time.min())
        self.assertLess(split.validation.time.max(), split.out_of_sample.time.min())

    def test_split_rejects_unsorted_duplicate_naive_or_invalid_boundaries(self):
        kwargs = {
            "train_end": datetime(2026, 1, 5, tzinfo=UTC),
            "validation_end": datetime(2026, 1, 8, tzinfo=UTC),
        }
        with self.assertRaisesRegex(ValueError, "chronologically sorted"):
            chronological_split(self.frame.iloc[::-1].reset_index(drop=True), **kwargs)
        duplicate = pd.concat([self.frame.iloc[:2], self.frame.iloc[[1]]], ignore_index=True)
        with self.assertRaisesRegex(ValueError, "unique"):
            chronological_split(duplicate, **kwargs)
        naive = self.frame.copy()
        naive["time"] = naive.time.dt.tz_localize(None)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            chronological_split(naive, **kwargs)
        with self.assertRaisesRegex(ValueError, "before validation_end"):
            chronological_split(
                self.frame,
                train_end=datetime(2026, 1, 8, tzinfo=UTC),
                validation_end=datetime(2026, 1, 5, tzinfo=UTC),
            )
        with self.assertRaisesRegex(ValueError, "empty dataset"):
            chronological_split(
                self.frame.iloc[:0],
                train_end=datetime(2026, 1, 5, tzinfo=UTC),
                validation_end=datetime(2026, 1, 8, tzinfo=UTC),
            )

    def test_explicit_oos_end_clips_results(self):
        split = chronological_split(
            self.frame,
            train_end=datetime(2026, 1, 4, tzinfo=UTC),
            validation_end=datetime(2026, 1, 6, tzinfo=UTC),
            out_of_sample_end=datetime(2026, 1, 9, tzinfo=UTC),
        )
        self.assertEqual(list(split.out_of_sample.value), [5, 6, 7, 8])

    def test_paper_summary_counts_rejections_and_uses_chronological_drawdown(self):
        results = [
            {"status": "simulated", "net_pnl": -4, "exit_time": "2026-01-03T00:00:00Z"},
            {"status": "rejected", "reason": "missing data", "decision_time": "2026-01-02T00:00:00Z"},
            {"status": "simulated", "net_pnl": 10, "exit_time": "2026-01-01T00:00:00Z"},
        ]
        summary = summarize_paper_results(results)
        self.assertEqual(summary["candidate_count"], 3)
        self.assertEqual(summary["simulated_count"], 2)
        self.assertEqual(summary["rejected_count"], 1)
        self.assertEqual(summary["rejection_reasons"], {"missing data": 1})
        self.assertEqual(summary["net_pnl_total"], 6)
        self.assertEqual(summary["realized_pnl_sequence_drawdown"], 4)
        self.assertIn("not portfolio", summary["drawdown_basis"])
        self.assertEqual(summary["profit_factor"], 2.5)
        self.assertIsNone(summary["confidence_interval"])

    def test_paper_summary_serialization_is_finite_for_all_winners(self):
        import json

        summary = summarize_paper_results([
            {"status": "simulated", "net_pnl": 1, "decision_time": "2026-01-01"}
        ])
        self.assertIsNone(summary["profit_factor"])
        self.assertTrue(summary["profit_factor_is_unbounded"])
        self.assertIsNone(summary["expectancy_bootstrap_iid_95pct"])
        json.dumps(summary, allow_nan=False)

    def test_moving_block_bootstrap_is_deterministic_and_requires_sequence_length(self):
        pnl = [1.0, 1.1, -0.5, -0.4, 0.8, 0.9, -0.2, -0.1]
        first = moving_block_bootstrap_expectancy_interval(
            pnl, iterations=200, seed=7
        )
        second = moving_block_bootstrap_expectancy_interval(
            pnl, iterations=200, seed=7
        )
        self.assertEqual(first, second)
        self.assertIsNotNone(first)
        self.assertIsNone(
            moving_block_bootstrap_expectancy_interval([1.0, -1.0, 0.5])
        )
        with self.assertRaisesRegex(ValueError, "block_length"):
            moving_block_bootstrap_expectancy_interval(pnl, block_length=1)

    def test_purged_split_removes_rows_with_overlapping_label_windows(self):
        split = purged_chronological_split(
            self.frame,
            train_end=datetime(2026, 1, 5, tzinfo=UTC),
            validation_end=datetime(2026, 1, 8, tzinfo=UTC),
            purge=timedelta(days=1),
        )
        self.assertEqual(list(split.train.value), [0, 1, 2])
        self.assertEqual(list(split.validation.value), [4, 5])
        self.assertEqual(list(split.out_of_sample.value), [7, 8, 9])

    def test_walk_forward_splits_expand_training_window_and_keep_oos_later(self):
        folds = walk_forward_splits(
            self.frame,
            train_window=timedelta(days=3),
            validation_window=timedelta(days=2),
            test_window=timedelta(days=2),
            step=timedelta(days=2),
        )
        self.assertEqual(len(folds), 2)
        self.assertLess(len(folds[0].train), len(folds[1].train))
        for fold in folds:
            self.assertLess(fold.train.time.max(), fold.validation.time.min())
            self.assertLess(fold.validation.time.max(), fold.out_of_sample.time.min())

    def test_bootstrap_and_deflated_sharpe_validate_inputs_and_are_deterministic(self):
        values = [-1.0, 0.5, 1.0, 2.0, -0.5, 1.5]
        first = bootstrap_expectancy_interval(values, seed=42)
        self.assertEqual(first, bootstrap_expectancy_interval(values, seed=42))
        self.assertLess(first[0], first[1])
        with self.assertRaisesRegex(ValueError, "finite"):
            bootstrap_expectancy_interval([1.0, float("nan")])
        returns = [0.01, -0.002, 0.012, -0.001, 0.008, 0.003] * 10
        single_trial = deflated_sharpe_ratio(returns, trial_count=1)
        many_trials = deflated_sharpe_ratio(returns, trial_count=100)
        self.assertGreaterEqual(single_trial, many_trials)
        self.assertGreaterEqual(many_trials, 0)
        self.assertLessEqual(single_trial, 1)


if __name__ == "__main__":
    unittest.main()
