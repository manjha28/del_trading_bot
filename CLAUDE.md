# del_trading_bot — Claude Code Project Reference

Exchange: **Delta Exchange India** (`https://api.india.delta.exchange`)
Instrument type: **perpetual futures** only
Status: **research / backtest phase** — bot has never been live-traded with real funds

---

## Repository Structure

```
del_trading_bot/
├── main.py              # Live trading orchestrator (hourly loop)
├── backtest.py          # Offline backtester (multi-strategy, parallel)
├── scanner.py           # Market discovery — live perpetual futures
├── strategy_short.py    # Legacy monolithic strategy file (still imported by backtest.py + main.py)
├── strategy-short/      # New modular package tree (import via sys.path.insert)
│   └── mhr_pump_short/
│       ├── __init__.py      # Re-exports all public symbols
│       ├── config.py        # MHRConfig dataclass
│       ├── indicators.py    # ema, rsi, atr, candle_volume_usd
│       ├── fees.py          # calculate_fees, BTC_ETH_SYMBOLS
│       ├── sizing.py        # trade_capital, sl_tp_qty, structural_sl_tp_qty, cap_pnl
│       ├── entry.py         # entry_signal and all entry helpers
│       ├── exit.py          # simulate_trailing_exit
│       └── backtest_results/  # Timestamped CSVs + insights markdown per run
├── long_strategy.py     # Long MA-crossover strategy (WIP, not integrated)
├── delta_client.py      # Authenticated Delta REST client bootstrap
├── requirements.txt     # Runtime dependencies
├── trade_state.json     # Persisted live-trading state (daily counters, reentry watch)
├── STRATEGY_PROGRESS.md # Strategy specs, backtest run log, ideas log
├── SESSION_NOTES.md     # Latest session context and next steps
└── .env                 # Not committed — holds API credentials
```

---

## Architecture

```
main.py  ──imports──▶  scanner.py          (market discovery)
                  ──▶  delta_client.py      (authenticated REST)
                  ──▶  strategy_short.py    (unified_entry_filter, sl/tp calc)

backtest.py ──imports──▶  strategy_short.py (entry/exit/fee/indicator logic)
                     ──▶  Delta REST API    (candle fetches, no auth needed)
                     # structural_sl_tp_qty implemented inline — not yet importing from package

strategy-short/mhr_pump_short/  ──  clean modular package (not yet imported by backtest.py/main.py)
                     ──▶  sizing.py         (structural_sl_tp_qty, sl_tp_qty, trade_capital)
                     ──▶  entry.py          (entry_signal, exhaustion_setup, detect_pump …)
                     ──▶  exit.py           (simulate_trailing_exit)

long_strategy.py   (standalone, not yet imported anywhere)
```

**Data flow (live):**  
`run()` → scan live perpetuals → filter by volume/spread → fetch 100h of 1h candles → `unified_entry_filter()` → if signal: place entry → place SL + 50% TP at 2R → manage trailing SL on remainder

**Data flow (backtest):**  
CLI args → fetch 1h + optional 1m candles → `ProcessPoolExecutor` (8 workers) → per-symbol strategy simulation → CSV output

---

## Module Responsibilities

### `delta_client.py`
Initializes a `DeltaRestClient` from `.env` (`DELTA_API_KEY`, `DELTA_API_SECRET`, `DELTA_BASE_URL`). Exports `client` and `is_authenticated()`. Logs a warning if credentials are absent — public endpoints still work without them.

### `scanner.py`
Fetches `/v2/products`, filters `type=perpetual_futures`, `state=live`. Fetches `/v2/tickers` for volume and spread data. All sessions set `trust_env=False` (proxy disabled).

### `strategy_short.py`
Contains all strategy logic. Key exports used by both `main.py` and `backtest.py`:
- `unified_entry_filter()` — single entry point for `mhr_pump_short` signal evaluation
- `calculate_sl_tp_prices()` — ATR-based SL, min-RR TP, quantity sizing
- `calculate_trade_capital()` — 50% of available equity
- `calculate_dynamic_fees()` — fee model (see below)
- `detect_pump()`, `is_pump_still_active()`, `pump_hesitation_detected()` — pump helpers
- `backtest_scalp_on_1m()` — scalp strategy simulation (backtest only)

### `backtest.py`
Offline backtester. Fetches candles from Delta API, runs `backtest_mhr_symbol()` and/or `backtest_scalp_on_1m()` in parallel. Build tag: `2026-04-17-dynamic-fees`.

Key behavioural details:
- **Structural SL is primary** — SL placed at `prev_candle_high`; falls back to ATR if prev_high ≤ entry_price or resulting qty < 1.
- **Per-symbol independence** — each parallel worker starts at `initial_capital`; results are aggregated without equity scaling (bug was fixed 2026-04-19).
- **Output location** — all CSVs + `_insights.md` written to `strategy-short/mhr_pump_short/backtest_results/` with `YYYYMMDD_HHMMSS_` prefix.
- **Entry path filter** — `--entry-paths` arg limits which paths fire; default is all three but `MHR_EXHAUSTION` only is recommended.

### `main.py`
Live trading loop. Runs once per hour. Evaluates all live perpetuals, respects daily trade cap (6), max open trades (3), cooldown (1h), min balance ($10 USDT). Requires `.env` credentials and `ENABLE_LIVE_ORDERS=true` to submit new entries; the latter defaults to false. When armed, `AUTO_EXECUTE=false` by default and prompts for confirmation before placing orders.

Ambiguous entry status or an unconfirmed protective stop sets a persistent
`execution_halt` in `trade_state.json` and blocks further new entries. Clear
that state only after reconciling all exchange orders and positions manually.

### `long_strategy.py`
WIP. Not imported anywhere. Contains fast/slow MA crossover logic with 12 MA types. **Second priority after short strategy is stable in live trading.**

---

## Strategies

### 1. `mhr_pump_short` — ACTIVE (backtest complete, live integration complete, never live-traded)

**Origin:** Merger of `mhr_trailing_rr4` + `parabolic_short`. Both predecessor strategies did not individually produce results in backtesting, so they were consolidated. `mhr_rsi` was also not producing results and was folded in.

**Timeframe:** 1-hour candles  
**Direction:** Short only  
**Config (`UnifiedShortConfig` defaults):**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `lookback_green_candles` | 4 | Candles checked for exhaustion setup |
| `min_pump_pct` | 50.0% | Minimum 24h gain to classify as a pump |
| `lookback_hours` | 24 | Lookback window for pump detection |
| `min_volume_surge_pct` | 2.0x | Required volume ratio vs prior 24h |
| `leverage` | 20x | |
| `sl_capital_pct` | 5% | Max capital risked per trade |
| `max_capital_usage_pct` | 50% | Trade size cap as fraction of equity |
| `atr_sl_multiplier` | 1.5x | ATR multiplier for normal SL |
| `pump_atr_sl_multiplier` | 2.5x | ATR multiplier for pump/reentry SL |
| `min_rr` | 4.0 | Minimum reward:risk for TP calculation |
| `max_reentries` | 3 | Max reentries per pump event |

**Entry paths** (evaluated in this order inside `unified_entry_filter()`):

**`REENTRY`** (checked first when `is_reentry=True`):
1. `is_pump_still_active()` — price still 20%+ above 24h-ago open
2. Current candle open price < previous entry price
3. `pump_hesitation_detected()` — any of: candle is red, upper wick present, body < 50% of prior body, high ≤ prior high
- Uses `pump_atr_sl_multiplier` (2.5x ATR)

**`MHR_EXHAUSTION`** (checked first when not a reentry) — **ONLY PATH THAT PRODUCES POSITIVE RESULTS**:
1. `exhaustion_setup_ok()` — last `lookback_green_candles` (4) candles are all green; last candle body ratio not in the 1.3–2.0x mean range (that indicates a blow-off top, not a clean runner)
- SL: structural (prev candle high), fallback to `atr_sl_multiplier` (1.5x ATR) on gap-ups

**`PUMP_HESITATION`** (fallback if exhaustion check fails) — **NEGATIVE: 13.9% WR — DO NOT USE**:
1. `detect_pump()` — price gained 50%+ in last 24h AND volume ≥ 2x prior 24h average
2. `pump_hesitation_detected()` — reversal signal on current candle
- SL: structural fallback to `pump_atr_sl_multiplier` (2.5x ATR)

**Note on EMA20/RSI14 diagnostics:** `ema_distance_pct` and `rsi14` are computed and logged per trade in backtest output but are **NOT currently used as hard entry gates**. `entry_candle_rejection_ok()` is also defined in `strategy_short.py` but is not currently called in the entry filter. These exist for future use or analysis.

**Exit model:**
- 50% of position taken at TP (2R from entry, minimum `min_rr × sl_capital`)
- Remaining 50% trails: SL moves to breakeven after partial fill, trails further if price extends
- Max hold: 24 candles (hours)
- If SL hit on a pump trade → reentry watch activated (up to 3 reentries)

**Fee model (`calculate_dynamic_fees()`):**
- BTC/ETH symbols: exit within 30 minutes → only entry fee (0.05%)
- Other symbols: exit within 15 minutes → only entry fee (0.05%)
- Otherwise: round-trip fees (entry + exit) × `charge_rate` (default 0.05%)

**Backtest diagnostics logged per trade:** `ema_distance_pct`, `rsi14`, `entry_path`, `pump_pct`, `volume_surge`, `bars_held`, `exit_reason`, `partial_qty_filled`, `sl_method` (structural/atr)

**Validated backtest results (Run 003 — 2026-04-19, MHR_EXHAUSTION + structural SL, 20 pump coins, 180 days, $100/symbol):**

| Metric | Value |
|--------|-------|
| Profitable symbols | 18 / 20 |
| Avg final equity | $202.45 (start: $100) |
| Total trades | 1,234 |
| Win rate | 47.0% |
| Gross PnL | $2,792.80 |
| Fees | $743.88 (26.6% of gross) |
| Net PnL | $2,048.92 |
| Max drawdown | −$88.19 |
| 2R partial fill rate | 34% of trades |
| ≤1h WR | 6.4% (44.5% of trades — main drag) |
| 2–4h WR | 82.1% |

Best: BLESSUSD (+440%), HUSD (+316%), AINUSD (+310%), KITEUSD (+281%)  
Worst: ARCUSD (−$2.87), PARTIUSD (−$2.63) — too few MHR_EXHAUSTION setups on these coins

---

### 2. `scalp_pullback` — BACKTEST ONLY (eventually planned for live HFT)

**Timeframe:** 5-minute candles (resampled from 1m)  
**Direction:** Both long and short (trend-filtered)  
**Config (`ScalpConfig` defaults):**

| Parameter | Default | Notes |
|-----------|---------|-------|
| `ema_5m_length` | 20 | Signal EMA on 5m |
| `ema_1h_length` | 50 | Trend EMA (from 1h resampled data) |
| `min_pullback_candles` | 4 | Min consecutive opposing candles (Tweak 3: was 2) |
| `max_pullback_candles` | 6 | Upper bound on pullback depth |
| `sl_buffer_pct` | 0.25% | SL buffer beyond pullback extreme (Tweak 5) |
| `vol_lookback` | 20 | Bars for average volume (Tweak 4) |
| `tp1_rr` | 1.4 | Partial TP at 1.4R (50% of position) |
| `tp1_qty_pct` | 50% | Fraction taken at TP1 |
| `trail_lock_rr` | 1.0 | Trail locks in at 1R after partial fill |
| `leverage` | 20x | |
| `sl_capital_pct` | 2% | |
| `max_holding_candles` | 30 | ~2.5 hours |

**Entry rules:**
- **Long:** 1h trend = BULLISH (price above EMA50 on 1h), signal candle is green, price above EMA20 on 5m, preceded by ≥4 consecutive red candles, signal candle volume ≥ average
- **Short:** 1h trend = BEARISH (price below EMA50 on 1h), signal candle is red, price below EMA20 on 5m, preceded by ≥4 consecutive green candles, signal candle volume ≥ average

**Tweaks applied (documented in code):**
- Tweak 3: `min_pullback_candles` → 4 (deeper pullbacks only)
- Tweak 4: Volume confirmation on signal candle
- Tweak 5: SL buffer → 0.25% (reduces false stop-outs)

---

### 3. `fast_slow_ma_long` — WIP, NOT INTEGRATED

**File:** `long_strategy.py`  
**Status:** Backtest infrastructure complete. Not imported by `backtest.py` or `main.py`. Second priority after short strategy is stable in live trading.

**Supports 12 MA types:** EMA, SMA, RMA, WMA, VWMA, TEMA, DEMA, TMA, ALMA, HMA, LSMA, ZLEMA

**Config (`FastSlowMAConfig` defaults):**
- `fast_length`: 24, `slow_length`: 65, `ma_type`: EMA
- `allow_long`: True, `allow_short`: False (currently long-only)
- Capital: 50%, TP: 3× risk distance

**What's built:** crossover/crossunder signal generation, exit simulator (SL/TP vs opposite crossover), `prepare_indicators()`, `fast_slow_ma_entry_filter()`, `calculate_long_sl_tp()`.

**What's missing:** integration into `backtest.py` dispatch, integration into `main.py`.

---

## Live Trading Configuration (main.py)

| Setting | Default | Env var override |
|---------|---------|-----------------|
| Enable new live entries | false | `ENABLE_LIVE_ORDERS=true` |
| Auto-execute orders | false | `AUTO_EXECUTE=true` |
| Max trades per day | 6 | `MAX_TRADES_PER_DAY` |
| Max concurrent open trades | 3 | `MAX_OPEN_TRADES` |
| Min USDT balance to trade | $10 | `MIN_BALANCE_TO_TRADE` |
| Min 24h volume (USD) | $5,000,000 | hardcoded |
| Max spread | 0.5% | hardcoded |
| Cooldown between trades | 1 hour | hardcoded |
| Max hold per trade | 24 hours | hardcoded |

**State file (`trade_state.json`):** Persists `trade_count`, `last_trade_day`, `next_trade_allowed_ts`, `watching` (reentry tracking per symbol). Resets daily counter at midnight. `watching` state is lost on process restart.

**Order flow:**
1. `place_entry()` — limit short at open of next candle
2. `wait_fill()` — polls for 120s; cancels if not filled
3. `place_brackets()` — places full SL (stop buy) + 50% TP at 2R (limit buy, reduce-only)
4. `manage_trailing()` — monitors remaining 50%; moves SL to breakeven after TP1 fills, trails further; handles reentry watch activation on SL hit

---

## Backtest Usage

```bash
# IMPORTANT: use .venv/bin/python — system Python has no pandas
# Recommended config (validated 2026-04-19):
.venv/bin/python backtest.py \
  --symbols AINUSD AIOUSD ARCUSD BLESSUSD COAIUSD CROSSUSD EVAAUSD HANAUSD \
            HUSD HYPEUSD KITEUSD LIGHTUSD MONUSD MUSD PARTIUSD PIPPINUSD \
            RIVERUSD SAHARAUSD SOLVUSD VVVUSD \
  --strategies mhr_pump_short \
  --days 180 \
  --workers 4 \
  --entry-paths MHR_EXHAUSTION

# Isolate a single entry path (for comparison runs)
.venv/bin/python backtest.py --entry-paths MHR_EXHAUSTION   # recommended
.venv/bin/python backtest.py --entry-paths PUMP_HESITATION  # AVOID: 13.9% WR
.venv/bin/python backtest.py --entry-paths REENTRY          # AVOID: negative

# Specific symbols and date range
.venv/bin/python backtest.py --symbols BTCUSD ETHUSD --start-date 2026-01-01 --end-date 2026-04-01

# Both strategies side by side
.venv/bin/python backtest.py --strategies mhr_pump_short scalp_pullback

# Filter by prior performance CSV
.venv/bin/python backtest.py --performance-file strategy-short/mhr_pump_short/backtest_results/20260419_153627_backtest_summary.csv

# Key tunable parameters
.venv/bin/python backtest.py \
  --atr-multiplier 1.5 \
  --pump-atr-mult 2.5 \
  --max-reentries 3 \
  --leverage 20 \
  --charges 0.0005 \
  --initial-capital 100
```

**Output files:** saved to `strategy-short/mhr_pump_short/backtest_results/` with datetime prefix:
- `YYYYMMDD_HHMMSS_backtest_trades.csv` — every trade (columns: symbol, strategy, entry_time, exit_time, entry_price, initial_sl_price, final_sl_price, target_price, qty, atr14, atr_pct, trade_capital, equity_before_trade, equity_after_trade, partial_qty_filled, entry_path, is_reentry, pump_pct, volume_surge, ema_distance_pct, rsi14, bars_held, gross_pnl, fees, net_pnl, roi_on_capital, exit_reason, sl_method)
- `YYYYMMDD_HHMMSS_backtest_summary.csv` — per-symbol aggregates
- `YYYYMMDD_HHMMSS_backtest_overall.csv` — strategy-wide totals
- `YYYYMMDD_HHMMSS_insights.md` — auto-generated analysis (entry path split, SL method split, exit reason dist)
- `backtest.log` — in repo root

**Performance filter gate** (`--performance-file`): keeps only symbols with `total_net_pnl > 0`, `win_rate > 40%`, `trades ≥ 20` from a prior summary CSV.

---

## Environment Setup

```bash
# Create .env in repo root
DELTA_API_KEY=your_key
DELTA_API_SECRET=your_secret
DELTA_BASE_URL=https://api.india.delta.exchange  # optional, this is the default

# Optional live trading overrides
ENABLE_LIVE_ORDERS=false
AUTO_EXECUTE=false
MAX_TRADES_PER_DAY=6
MAX_OPEN_TRADES=3
MIN_BALANCE_TO_TRADE=10.0
```

```bash
# System Python 3.10.12 has no pandas — always use the venv
uv venv .venv
uv pip install -r requirements.txt
# then run everything with: .venv/bin/python
```

---

## API Connectivity Check

Public endpoints work without credentials. Use this to verify network access:

```bash
# 1. Public reachability
curl -i --max-time 20 "https://api.india.delta.exchange/v2/products"

# 2. Scanner (same path the bot uses)
python scanner.py

# 3. Backtest candle fetch
python backtest.py --symbols BTCUSD --limit 50

# 4. Authenticated route (requires .env)
python - <<'PY'
from delta_client import client
r = client.request("GET", "/v2/products")
print("status:", r.status_code, "| success:", r.json().get("success"))
PY
```

Note: `scanner.py` and `backtest.py` set `session.trust_env = False`, so they bypass system proxy. If running behind a corporate proxy, this will cause connection failures — either disable proxy or patch sessions to re-enable `trust_env`.

---

## Active TODOs (Priority Order)

| Priority | Area | Task |
|----------|------|------|
| HIGH | Backtest | Implement `min_hold_candles=1` filter — 44.5% of trades stop out ≤1h with 6.4% WR; surviving bar 1 wins 73–82% |
| HIGH | Backtest | Re-run at `--initial-capital 1000` — fees are 26.6% of gross at $100; scale test needed |
| MED | Entry | Test `entry_candle_rejection_ok()` as MHR_EXHAUSTION filter — function exists, not called |
| MED | Architecture | Migrate `backtest.py` imports from `strategy_short.py` → `strategy-short/mhr_pump_short/` |
| MED | Live trading | Migrate `main.py` to use structural SL (prev candle high) for order placement |
| LOW | Live trading | Persist `watching` reentry state in `trade_state.json` across `main.py` restarts |
| LOW | Strategy | Integrate `fast_slow_ma_long` into `backtest.py` and `main.py` |
| LOW | Strategy | Integrate `scalp_pullback` into `main.py` for live HFT |

## Known Limitations and Open Items

| Area | Issue |
|------|-------|
| PUMP_HESITATION path | 13.9% WR — deeply negative. Do not enable. Use `--entry-paths MHR_EXHAUSTION` |
| REENTRY path | Negative net. Dependent on prior MHR_EXHAUSTION trades to trigger; marginal at best |
| `watching` state | Lost on `main.py` restart — reentry tracking does not persist across sessions |
| Position monitoring | REST polling only, no WebSocket — 60s sleep between loop iterations |
| `ENABLE_LIVE_ORDERS=false` | New entries are blocked; protective/close operations remain available for existing positions |
| `AUTO_EXECUTE=false` | When entries are armed, requires terminal supervision for confirmation |
| `long_strategy.py` | Complete in isolation but not wired into backtest or live trading |
| `scalp_pullback` | Not in `main.py`; planned for live HFT use eventually |
| EMA20/RSI14 gates | Tested — adding gates makes performance worse (winners have lower RSI/EMA dist than losers) |
| `strategy_short.py` | Legacy monolith; `backtest.py` and `main.py` still import from it |
| `strategy-short/mhr_pump_short/` | Clean package exists but not yet imported by backtest.py or main.py |
| No live results | Bot has never been run live — all performance data is simulated |
