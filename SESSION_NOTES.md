# Session Notes — 2026-04-19

## What We Built / Fixed / Investigated

### 1. Structural SL (previous candle high as stop level)
Replaced ATR-based stop-loss with structure-based SL: SL is placed at the high of the candle immediately preceding the entry candle (no buffer). This means the trade is invalidated only if price reclaims a level the market already visited — a meaningful structural level rather than a statistical distance.

**Sizing formula:**
```
sl_dist  = prev_candle_high − entry_price
max_loss = trade_capital × sl_capital_pct   (default: 5%)
qty      = floor(max_loss / sl_dist)
```
Fallback to ATR-based sizing if `prev_high ≤ entry_price` (gap-up candle) or `qty < 1` (wick too wide for given capital).

**Why it worked:** Structural SL brings the 2R partial-TP target much closer to entry (risk is small, so 2R is achievable). The 2R partial fill rate jumped from ~3.4% → 34%, which is the core lever that makes the strategy profitable.

### 2. Entry path isolation (`--entry-paths` CLI flag)
Added `--entry-paths` argument to `backtest.py` to run with one or more entry paths:
```bash
.venv/bin/python backtest.py --entry-paths MHR_EXHAUSTION
.venv/bin/python backtest.py --entry-paths PUMP_HESITATION
.venv/bin/python backtest.py --entry-paths REENTRY
```
**Finding:** PUMP_HESITATION has 13.9% WR and is deeply negative. REENTRY is also negative. MHR_EXHAUSTION alone carries the strategy. All future runs should use `--entry-paths MHR_EXHAUSTION`.

### 3. Fixed equity compounding bug in parallel mode
**Bug:** In parallel backtest mode (`--workers > 1`), all trades from all symbols were sorted chronologically and each trade's PnL was scaled by `(running_equity / original_trade_capital)`. As early symbols won, equity grew, and later trades were scaled 100×+, producing numbers in the millions from $100 starting capital.

**Fix:** Removed the scaling pass. Each symbol's `_process_symbol` worker now runs independently at `initial_capital` ($100). All trades are aggregated as-is from per-symbol runs. PnL numbers are now honest.

**Location:** `backtest.py` lines ~575–591. The old block replaced with:
```python
flat = []
for t in all_trades_map.values(): flat.extend(t)
flat.sort(key=lambda t: str(t["entry_time"]))
trade_rows.extend(flat)
equity = config.initial_capital + sum(t["net_pnl"] for t in flat)
```

### 4. Backtest output redirected to strategy folder
All CSV outputs and an auto-generated insights markdown now save to:
```
strategy-short/mhr_pump_short/backtest_results/{YYYYMMDD_HHMMSS}_{file}.csv
strategy-short/mhr_pump_short/backtest_results/{YYYYMMDD_HHMMSS}_insights.md
```
The `_insights.md` auto-generates: per-path breakdown, SL method split, exit reason distribution.

### 5. `structural_sl_tp_qty` added to sizing module
New public function in `strategy-short/mhr_pump_short/sizing.py`. Takes `prev_high` as input, tries structural sizing first, falls back to `sl_tp_qty()` (ATR) on failure. Exported from `__init__.py`.

---

## Exact Files Modified

| File | Change |
|------|--------|
| `strategy-short/mhr_pump_short/sizing.py` | Added `structural_sl_tp_qty()` function |
| `strategy-short/mhr_pump_short/__init__.py` | Exported `structural_sl_tp_qty` |
| `backtest.py` | Structural SL logic in `backtest_mhr_symbol()`, `--entry-paths` CLI arg, output dir, `_write_insights()`, fixed equity compounding, added `import os` |

### Key functions modified in `backtest.py`
- `backtest_mhr_symbol()` — structural SL with ATR fallback, entry path filter, `sl_method` field in trade rows
- `main()` — timestamped output dir, calls `_write_insights()`
- `parse_args()` — added `--entry-paths`
- Parallel aggregation block — removed equity scaling pass
- New: `_write_insights(all_trades, summary, overall, config, start_ts, end_ts, path)`

---

## Definitive Backtest Result (Run 003 — 2026-04-19)

**Config:** MHR_EXHAUSTION only, structural SL, 20 pump-coin symbols, 180 days, $100 per symbol  
**File:** `strategy-short/mhr_pump_short/backtest_results/20260419_153627_*`

| Metric | Value |
|--------|-------|
| Symbols profitable | 18 / 20 |
| Avg final equity | $202.45 (starting $100) |
| Total trades | 1,234 |
| Overall WR | 47.0% |
| Total gross PnL | $2,792.80 |
| Total fees | $743.88 (26.6% of gross) |
| **Total net PnL** | **$2,048.92** |
| Combined max drawdown | −$88.19 |
| 2R partial fill rate | 34% of trades |
| TRAIL_STOP_AFTER_PARTIAL WR | 97.1% |
| ≤1h WR | 6.4% (44.5% of trades) |
| 2–4h WR | 82.1% |

**Best symbols:** BLESSUSD (+440%), HUSD (+316%), AINUSD (+310%), KITEUSD (+281%)  
**Worst:** ARCUSD (−$2.87), PARTIUSD (−$2.63) — both have very few MHR_EXHAUSTION signals

---

## Key Gotchas / Non-Obvious Things

1. **System Python has no pandas.** Always run with `.venv/bin/python`, not `python3`. The `.venv` was created with `uv venv .venv` + `uv pip install -r requirements.txt`.

2. **`strategy-short` folder cannot be imported directly** (hyphen in name). To import the package: `sys.path.insert(0, "strategy-short")` then `from mhr_pump_short import ...`. `backtest.py` currently imports from `strategy_short.py` (the monolithic file), not the new package.

3. **`prev_high ≤ entry_price` on gap-up candles** triggers ATR fallback. ATR fallback trades hold 3× longer (avg 6.9h vs 2.3h) and generate much lower avg net ($0.08 vs $1.95). They are valid but weak.

4. **HYPEUSD has extreme fee drag** — $124 fees on $213 gross (58%), vs strategy avg 26.6%. High trade frequency (115 trades) + moderately priced coin = proportionally high fees. Still profitable (+$89 net) but marginal.

5. **PARTIUSD and ARCUSD barely fire** — only 1 and 13 MHR_EXHAUSTION setups in 180 days. These coins don't form 4-candle green runs consistently.

6. **The `tp_price` field in trade rows is not the actual exit target.** `_sim_trailing_exit` uses `initial_sl` to compute `risk`, then sets 2R as `entry_price − 2 × risk`. The `tp_price` column is a diagnostic from the sizing function that isn't used in the exit simulation.

7. **Parallel mode equity is computed as** `initial_capital + sum(all net_pnl)` after the fix — not a true sequential equity curve. It's the sum of all independent per-symbol net PnLs added to starting capital.

---

## Unresolved / Next Steps (Priority Order)

1. **[HIGH] 1-bar hold filter** — 549 trades (44.5%) stop out in ≤1h with only 6.4% WR. A filter that requires the trade to be alive at the close of bar 1 before it's counted (or: only enter on confirmation candle N+1) could eliminate the bulk of losers. Implement as `min_hold_candles=1` in `BacktestConfig` and test.

2. **[HIGH] Scale capital to test fee sensitivity** — At $100, fees are 26.6% of gross. At $1,000, fees drop proportionally. Run same 20 symbols at `--initial-capital 1000` to see actual tradeable returns.

3. **[MED] `entry_candle_rejection_ok()` filter** — Function exists in `strategy_short.py` but is unused. Requires upper wick > body on the entry candle. Given ≤1h WR is only 6.4%, this filter might help screen out bad entries without eliminating good ones.

4. **[MED] Wire `strategy-short/mhr_pump_short/` into `backtest.py`** — Currently `backtest.py` imports from `strategy_short.py`. The new package at `strategy-short/mhr_pump_short/` is cleaner and has `structural_sl_tp_qty`. Migrate gradually.

5. **[LOW] Wire `strategy-short/mhr_pump_short/` into `main.py`** — For live trading, `main.py` should use the packaged version eventually.

6. **[LOW] Persist `watching` state** — Reentry state lost on `main.py` restart. Serialize to `trade_state.json`.
