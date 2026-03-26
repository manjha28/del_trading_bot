# del_trading_bot Repository Analysis

## Purpose
This repository implements a **short-only automated trading bot** for Delta Exchange perpetual futures. The bot scans live perpetual contracts, fetches recent 5-minute candles, computes a strict non-repainting short setup, and (with interactive confirmation) places a limit short entry plus protective stop-loss and take-profit orders.

## High-Level Architecture
- `main.py` orchestrates the full flow: market scan → candle fetch → signal evaluation → optional order placement → fill waiting → risk order placement.
- `scanner.py` queries exchange products and filters only live perpetual futures.
- `strategy_short.py` contains the signal-generation logic (EMA and candle-pattern rules).
- `delta_client.py` initializes and exports an authenticated `DeltaRestClient` using `.env` credentials.
- `requirements.txt` lists runtime dependencies.

## Runtime Flow (End-to-End)
1. `run()` resets daily counters if calendar date changed.
2. Bot calls `get_live_perpetual_futures()` to retrieve tradable perpetual products.
3. Loop filters products against `ALLOWED_SYMBOLS` and daily trade cap.
4. For allowed symbols, it requests ~200 historical 5-minute candles.
5. It runs `short_signal(df)` to validate the two-candle setup.
6. On a signal, it prints levels and asks for `y/n` manual confirmation.
7. If confirmed, it places a limit short order.
8. It polls open orders until filled or timeout; on timeout it cancels order.
9. If position exists, it places stop-loss (buy stop order) and take-profit (buy reduce-only trigger/market style payload).
10. Increments daily trade count and exits after one trade per run.

## Strategy Logic
`short_signal()` is intentionally strict and only uses the last two closed candles:
- Compute EMA(5) on close prices.
- **Alert candle** = second-last closed candle.
- **Entry candle** = last closed candle.
- Alert candle must be fully above EMA (`low > ema`).
- Entry candle must break alert low (`entry_candle.low < alert.low`).
- Entry price = alert low.
- Stop-loss = alert high.
- Take-profit = entry - (`risk * RR`) with `RR = 3`.

This avoids repainting by avoiding the currently-forming candle.

## Risk Controls and Guardrails
- `ALLOWED_SYMBOLS` whitelist limits tradable contracts (currently only `HANAUSD`).
- `MAX_TRADES_PER_DAY` prevents overtrading in one day.
- Order-fill timeout cancels stale limit entries.
- Position check prevents SL/TP placement if no open position was detected.
- One-trade-per-run behavior (`return` after first completed setup).

## Notable Implementation Details
- Candle source URL is hardcoded to `https://api.india.delta.exchange`.
- `delta_client.py` currently initializes `client` twice (first with hardcoded base URL, then reinitialized with `DELTA_BASE_URL`), so only the second initialization is effective.
- `delta_client.py` prints a partial API key prefix at import time for debugging; this can leak key metadata in logs.
- `BASE_URL` is required implicitly by second client initialization; missing `DELTA_BASE_URL` could produce runtime issues depending on SDK behavior.
- `scanner.py` directly trusts `data["result"]` and does not defensive-check `success` or key presence.
- `main.py` uses `input()` confirmation, making the bot semi-automated rather than headless.

## Operational Dependencies
Python packages:
- `delta-rest-client`
- `pandas`
- `python-dotenv`
- `requests`

Environment variables expected by runtime:
- `DELTA_API_KEY`
- `DELTA_API_SECRET`
- `DELTA_BASE_URL` (effectively required by the final client initialization)

## Suggested Next Improvements
1. Remove duplicate client initialization and debug API key print.
2. Validate API responses consistently (`status_code`, `success`, key existence).
3. Convert hardcoded configuration to environment- or file-based config.
4. Add structured logging and exception handling around all exchange requests.
5. Add unit tests for `short_signal()` and integration tests for order flow stubs.
6. Replace `input()` with optional CLI flag (`--confirm`/`--paper`) for automation.
7. Ensure take-profit payload conforms exactly to Delta's current conditional order schema.

## Overall Assessment
The repository is small, focused, and understandable. The core trading loop and strategy are clearly separated, which is good. The main production-readiness gaps are around configuration hygiene, API hardening, secret-handling/logging, and test coverage.
