# VPVR scalping engine — operation guide

## Scope and safety boundary
The VPVR package is an additive research/paper subsystem. The existing MHR bot is not modified by its runtime. The VPVR package intentionally has no exchange order adapter, private account client, or live order command. `scan` is signal-only; `paper-loop` is virtual execution. The exchange is Delta Exchange India.

This is a low-latency research/scalping architecture, not institutional colocated HFT. It uses Python, REST bootstrap/backfill, and Delta's public WebSocket feed. It cannot claim deterministic microsecond latency, queue priority, or guaranteed fills.

## Install and configure
Use the repository virtual environment:

```bash
uv venv .venv
uv pip install -r requirements.txt
```

Configuration is environment-based. Defaults: 60 profile bins, 70% value area, 20-period EMA, 0.5% risk per trade, 1.5% daily drawdown lockout, three consecutive losses, 2R minimum reward/risk, 10x leverage (20x hard ceiling), one position, and paper balance 1000.

Relevant variables:
- `VPVR_PROFILE_ROWS` (50–70), `VPVR_VALUE_AREA_PCT` (default 0.70)
- `VPVR_RISK_PER_TRADE_PCT` (0.0025–0.005), `VPVR_DAILY_LOSS_LIMIT_PCT` (max 0.015)
- `VPVR_MAX_CONSECUTIVE_LOSSES`, `VPVR_MIN_REWARD_RISK` (at least 2)
- `VPVR_DEFAULT_LEVERAGE` (default 10, maximum 20), `VPVR_MAX_POSITIONS`
- `VPVR_MAX_DATA_AGE_SECONDS`, `VPVR_MAX_SPREAD_PCT`, `VPVR_MAX_ABS_FUNDING_RATE`, `VPVR_MIN_VOLUME_MULTIPLE`
- `VPVR_PAPER_BALANCE`, `VPVR_FEE_RATE`, `VPVR_SLIPPAGE_BPS`
- `VPVR_PROFILE_LOOKBACK_15M`, `VPVR_PAPER_STATE_FILE`

Do not put API secrets in these variables. Market-data endpoints used by this subsystem are public.

## Commands
```bash
# Print validated settings
python -m vpvr config

# One pass: signals only, no positions/orders
python -m vpvr scan --symbols BTCUSD ETHUSD

# One pass: queue eligible virtual stop-entry triggers
python -m vpvr scan --paper --symbols BTCUSD ETHUSD

# Persistent paper loop (Ctrl-C to stop)
python -m vpvr paper-loop --symbols BTCUSD ETHUSD --interval-seconds 15

# Public WebSocket connection and channel diagnostics
python -m vpvr stream --symbols BTCUSD ETHUSD

# Profile a supplied candle CSV
python -m vpvr profile sample_15m.csv

# Chronological multi-timeframe backtest from exported OHLCV
python -m vpvr backtest --symbol BTCUSD --csv-1m btc_1m.csv --csv-3m btc_3m.csv --csv-15m btc_15m.csv --output reports/btc_vpvr.json
```

Paper-state JSON is stored under `data/` by default and is ignored by Git. Do not manually edit it while the process is running. If the file is unreadable, the paper engine stops instead of resetting balances.

## Signal lifecycle
1. Load completed 1m, 3m and 15m candles and validate freshness and exact bar spacing.
2. Compute the 15m profile and rolling profile history; classify regime.
3. Evaluate POC reclaim, VAH/VAL rejection, LVN breakout independently.
4. Apply bid/ask spread, contract-metadata and central risk checks.
5. Queue a stop-entry trigger in paper state. It cannot fill on the signal candle.
6. On a later completed 1m bar, cancel if invalidated, expired or gap-chased; otherwise simulate a fill with configured slippage.
7. Manage protective virtual stop and partial take-profit; stop wins if both stop and target are touched in the same OHLC bar.

## Volume Profile provenance
- Trade prints from Delta's public trades endpoint carry `side` buy/sell and can be used for true trade-level volume-at-price and aggressor CVD.
- When trade history does not span the selected profile lookback, the engine uses OHLCV range allocation, explicitly labeled `ohlcv_uniform_range_approximation`. Each candle's volume is distributed proportionally across intersecting price bins. This is an estimate, not exact volume-at-price.
- Delta's public WebSocket trade event contains `buyer_role` maker/taker; that is not aggressor buy/sell and is never interpreted as CVD. REST public trade `side` is the source for signed aggressor delta.
- Session VWAP currently resets at UTC midnight. Configure a different session definition before using it for non-UTC exchange sessions.

## Data limitations and operational cautions
- Paper execution is blocked unless Delta reports an operational product with verified lot increment and minimum quantity. Signal-only scans may show signals without computing a position quantity when size metadata is missing. The paper adapter retains a 1-contract fallback solely for diagnostics and never uses it to queue paper positions.
- The entry filter reads Delta's public `FUNDING:<symbol>` candle history and fails closed when the latest rate is unavailable/stale or exceeds `VPVR_MAX_ABS_FUNDING_RATE`. Funding is an entry filter only. The paper broker charges funding only when supplied an explicit timestamped funding-settlement event; the REST polling loop does not source settlement events, so paper PnL does not include funding. Historical funding is not included in the first backtest engine.
- The backtester assumes stop-first if stop and target touch in the same 1m candle, includes configured fees/slippage, and cannot model queue position or partial exchange fills.
- Candle API history is chunked for backfill. Gaps, stale bars, incomplete bars, unknown contract multiplier/tick, missing spread quotes, or failed API requests cause no-trade behavior.
- A paper result is not a profitability claim. Validate out-of-sample, per-symbol, per-regime and after realistic fees before considering any separate live execution project.


## Strategy dashboard

The optional Streamlit dashboard displays the backend scan response, contract verification diagnostics, Volume Profile levels, indicators, strategy signals, paper positions, pending entries, closed trades, and backtest results. It uses the existing VPVR scanner, engine, backtester and local JSON paper journal; it does not add an order API or a live-execution control.

Install the dashboard dependency and run it from the repository root:

```bash
python -m pip install -r requirements-dashboard.txt
streamlit run vpvr/dashboard.py
```

Open the local URL printed by Streamlit (normally http://localhost:8501).

### Dashboard workflow

1. Select BTCUSD and/or ETHUSD, choose signal-only or paper scan, and click **Fetch latest market data**.
2. Inspect contract metadata verification, market regime, spread, funding, profile provenance, indicators, and all returned signals.
3. Review the local paper journal. The paper scan may queue a virtual trigger; it cannot submit exchange orders.
4. In Backtest workbench, upload aligned 1m, 3m and 15m OHLCV CSVs. When a recent scan has verified contract metadata, the backtester uses it. Otherwise, backtesting requires an explicit manual research-assumptions checkbox and labels the result as unverified; these values do not affect paper sizing.
5. Use Raw backend data to inspect the complete JSON scan response.

The dashboard does not automatically run continuous polling. Trigger a new scan manually to refresh the market snapshot. The paper journal is read from `VPVR_PAPER_STATE_FILE` (default `data/vpvr_paper_state.json`).
