# VPVR scalping engine — implementation plan

## Existing architecture (audited 2026-10-30)
- Exchange: Delta Exchange India perpetual futures, established by `delta_client.py`, `scanner.py`, and the configured API base URL in the existing runner.
- Runtime: one Python script (`main.py`) using the Delta REST client; `backtest.py` is a separate candle-based backtester.
- Existing strategies: modular `strategy-short/mhr_pump_short/` package and root compatibility module `strategy_short.py`; a standalone `long_strategy.py`.
- Dependencies: delta-rest-client, pandas, python-dotenv, requests.
- No README.md, AGENTS.md, automated tests, CI workflow, frontend, web service, or database models were present in the main tree at audit time.
- No exchange WebSocket/event stream or trade-level aggressor-side tape is present in the inspected source.

## Reuse
- Reuse the configured Delta REST client for public market reads and the existing product/ticker discovery logic where it is safe.
- Keep all MHR files and behavior unchanged.
- Use pandas already required by the repository.
- Keep the new VPVR subsystem isolated under `vpvr/`.

## New modules
- `vpvr/config.py`: environment-backed immutable settings and validation.
- `vpvr/profile.py`: fixed-range, rolling, session, and developing volume profiles; POC/VAH/VAL, HVN/LVN; explicit source metadata.
- `vpvr/indicators.py`: EMA, session VWAP, volume expansion, and explicit unavailable CVD result unless aggressor prints exist.
- `vpvr/regime.py`: conservative 15m regime classifier.
- `vpvr/strategies.py`: POC reclaim, value-area rejection, and LVN breakout signals, using only completed, aligned candles.
- `vpvr/risk.py`: centralized sizing and loss-lockout calculations.
- `vpvr/data.py`: Delta REST public candle/product adapter, UTC boundary validation, freshness checks, resampling and timeframe synchronization.
- `vpvr/paper.py`: persistent local paper account and deterministic mark/stop/target lifecycle, no exchange order APIs.
- `vpvr/backtest.py`: chronological event-loop simulator with conservative same-bar stop/target assumptions.
- `vpvr/cli.py`: config display, profile diagnostics, backtest/paper entry points.
- `tests/test_vpvr_*.py`: deterministic unit tests with synthetic data.
- `docs/VOLUME_PROFILE_IMPLEMENTATION_PLAN.md` and operating notes.

## Exchange and data constraints
Delta Exchange India is already wired in. Implement REST candles and product metadata first. Trade-level VP, bid/ask, funding and actual aggressor CVD are not currently represented by existing adapters; no placeholders will fabricate them. The data layer returns capability flags and blocks signals if required timeframe data is missing/stale. Contract multiplier/tick/lot/minimum quantity must be read from product metadata when fields are unambiguous; unknown metadata fails closed.

## Database / dashboard
No database or frontend exists in the inspected repository. Initial release uses atomic JSON state for paper simulation and CLI diagnostics only. A dashboard/database integration is intentionally not fabricated; add later when a service/UI exists.

## Sequence
1. Profile math + unit tests.
2. Indicators + regime + multi-timeframe synchronization.
3. Three independent strategy detectors + tests.
4. Risk engine.
5. Delta REST read adapter, freshness checks, and paper runner.
6. Event-driven backtest and reporting.
7. Documentation and compatibility checks.
8. Live order adapter is explicitly excluded from this milestone. Any later live integration requires Delta's current order/contract specs and separate execution safety review.

## Testing
Use synthetic candles with manually calculable volume distributions and mocked API responses. Never use credentials or submit real exchange orders in tests. Run unittest/pytest and static compile checks when available.
