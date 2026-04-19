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
├── strategy_short.py    # All short-strategy logic + scalp logic
├── long_strategy.py     # Long MA-crossover strategy (WIP, not integrated)
├── delta_client.py      # Authenticated Delta REST client bootstrap
├── requirements.txt     # Runtime dependencies
├── trade_state.json     # Persisted live-trading state (daily counters, reentry watch)
├── STRATEGY_PROGRESS.md # Strategy specs, backtest run log, ideas log
└── .env                 # Not committed — holds API credentials
```

---

## Architecture

```
main.py  ──imports──▶  scanner.py          (market discovery)
                  ──▶  delta_client.py      (authenticated REST)
                  ──▶  strategy_short.py    (unified_entry_filter, sl/tp calc)

backtest.py ──imports──▶  strategy_short.py (all entry/exit logic)
                     ──▶  Delta REST API    (candle fetches, no auth needed)

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
Offline backtester. Fetches candles from Delta API, runs `backtest_mhr_symbol()` and/or `backtest_scalp_on_1m()` in parallel. Writes three CSV outputs. Build tag: `2026-04-17-dynamic-fees`.

### `main.py`
Live trading loop. Runs once per hour. Evaluates all live perpetuals, respects daily trade cap (6), max open trades (3), cooldown (1h), min balance ($10 USDT). Requires `.env` credentials for order placement. `AUTO_EXECUTE=false` by default — prompts `y/n` confirmation before placing orders.

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

**`MHR_EXHAUSTION`** (checked first when not a reentry):
1. `exhaustion_setup_ok()` — last `lookback_green_candles` (4) candles are all green; last candle body ratio not in the 1.3–2.0x mean range (that indicates a blow-off top, not a clean runner)
- Uses normal `atr_sl_multiplier` (1.5x ATR)

**`PUMP_HESITATION`** (fallback if exhaustion check fails):
1. `detect_pump()` — price gained 50%+ in last 24h AND volume ≥ 2x prior 24h average
2. `pump_hesitation_detected()` — reversal signal on current candle
- Uses `pump_atr_sl_multiplier` (2.5x ATR)

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

**Backtest diagnostics logged per trade:** `ema_distance_pct`, `rsi14`, `entry_path`, `pump_pct`, `volume_surge`, `bars_held`, `exit_reason`, `partial_qty_filled`

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
# Default: mhr_pump_short, last 30 days, all live perpetuals
python backtest.py

# Specific symbols and date range
python backtest.py --symbols BTCUSD ETHUSD --start-date 2026-01-01 --end-date 2026-04-01

# Both strategies side by side
python backtest.py --strategies mhr_pump_short scalp_pullback

# Filter by prior performance CSV
python backtest.py --performance-file backtest_summary.csv

# Key tunable parameters
python backtest.py \
  --pump-min-pct 50.0 \
  --pump-volume-surge 2.0 \
  --atr-multiplier 1.5 \
  --pump-atr-mult 2.5 \
  --max-reentries 3 \
  --leverage 20 \
  --charges 0.0005
```

**Output files:** `backtest_trades.csv`, `backtest_summary.csv`, `backtest_overall.csv`, `backtest.log`

**Performance filter gate** (`--performance-file`): keeps only symbols with `total_net_pnl > 0`, `win_rate > 40%`, `trades ≥ 20` from a prior summary CSV.

---

## Environment Setup

```bash
# Create .env in repo root
DELTA_API_KEY=your_key
DELTA_API_SECRET=your_secret
DELTA_BASE_URL=https://api.india.delta.exchange  # optional, this is the default

# Optional live trading overrides
AUTO_EXECUTE=false
MAX_TRADES_PER_DAY=6
MAX_OPEN_TRADES=3
MIN_BALANCE_TO_TRADE=10.0
```

```bash
pip install -r requirements.txt
# delta-rest-client, pandas, python-dotenv, requests
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

## Known Limitations and Open Items

| Area | Issue |
|------|-------|
| `watching` state | Lost on `main.py` restart — reentry tracking does not persist across sessions |
| Position monitoring | REST polling only, no WebSocket — 60s sleep between loop iterations |
| `AUTO_EXECUTE=false` | Bot is semi-automated; requires terminal supervision for confirmation |
| `long_strategy.py` | Complete in isolation but not wired into backtest or live trading |
| `scalp_pullback` | Not in `main.py`; planned for live HFT use eventually |
| EMA20/RSI14 gates | Captured as trade diagnostics but not active hard-gate filters |
| `entry_candle_rejection_ok()` | Defined but not currently called anywhere |
| No live results | Bot has never been run live — all performance data is simulated |
