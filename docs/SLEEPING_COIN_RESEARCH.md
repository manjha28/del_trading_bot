# Market-Wide Sleeping-Coin Research

This subsystem is separate from the existing scanner, trading loop, VPVR paper
engine, and historical strategy reports. It is a public-data research monitor;
it has no authenticated client, order placement, or automatic short execution.

## Run

```bash
.venv/bin/python -m coin_research.cli
```

By default, the command reconciles the complete Delta India perpetual product
catalog, fetches ticker snapshots plus hourly/5-minute/1-minute candles, L2
books, and public aggressor-side trade samples for every live perpetual, then
persists observations in `data/sleeping_coin_research.sqlite3` and writes
`data/sleeping_coin_dashboard.html`. A product API failure stops the scan
explicitly. Per-symbol data failures remain visible in the catalog and reduce
confidence; they do not remove a product from discovery.

Use `--loop-seconds 300` to repeat full-universe scans. `--db`, `--dashboard`,
`--base-url`, `--request-interval`, `--execution-notional`,
`--max-spread-pct`, and `--max-impact-pct` override defaults. Execution
thresholds affect only the separate execution-eligibility diagnostic. Supply
both `--train-end YYYY-MM-DD --validation-end YYYY-MM-DD` to label chronological
train, validation, and later out-of-sample event windows; the dashboard labels
pooled results descriptive-only when cutoffs are not configured.

## Data and interpretation

- The product table records product IDs, symbols, complete exchange metadata,
  first/last-seen times, and reconciliation state. Inactive and subsequently
  unreturned products are retained; `not_returned` does not assert delisting.
- Candle history is cached by product ID and timeframe. Initial history targets
  60 days hourly, 7 days at 5 minutes, and 24 hours at 1 minute. New listings
  with less than 30 complete hourly days remain `insufficient_history`.
- Sleeping scores and compression percentiles compare each symbol to its own
  historical distributions. There is no fixed discovery-volume minimum,
  manual symbol list, popularity ranking, or prior-pump filter. BTC and ETH are
  included as ordinary controls if present in the exchange catalog.
- Ticker quote volume is preferred when available. Candle notional uses
  `volume × typical price` and is explicitly an estimate; compare with the
  ticker source before treating it as comparable USD notional.
- A latest REST trade sample can provide count, sampled frequency, and actual
  buy/sell aggressor flow. REST snapshots are not a continuous trade history.
  Order-book snapshots expose spread, top-five depth estimates, and changes
  between scans. They cannot establish historical absorption or replenishment.
- Open interest, funding, and mark/index deviations are populated only when
  the ticker exposes those fields. Historical changes are absent until both
  observations are available. No missing value is synthesized from OHLCV.
- Awakening, expansion/climax, failed-high, anchored-VWAP failure,
  lower-high/support-break, and failed-high retest are research labels, not
  orders. Absorption remains explicitly unavailable until aligned time-series
  trade and book events are captured.
- Execution eligibility is independent from discovery. It requires fresh
  hourly data, available book depth within configured spread/impact limits,
  explicit tick/lot/minimum/position limits, a public taker fee, and a
  verified linear contract-value basis. Any unverified field blocks this
  diagnostic; no order is submitted.

## Coverage limits and research discipline

The SQLite event log preserves state transitions, shadow signals, data gaps,
and execution rejections for every observed product. Triggered bearish shadow
signals receive prospective 1h/4h/24h forward-close labels as data matures.
The dashboard reports pooled and per-symbol gross/net price-return diagnostics,
and applies explicitly configured chronological cutoffs. The net estimate
uses the signal-time spread, estimated book impact, and exchange taker fee
when the linear depth basis and all cost inputs are verified; it is not
contract PnL or a realistic fill simulator. Signals without cost inputs remain
visible and are counted as missing-cost cases.

Historical delisted-contract backfill, aligned historical order-book/trade
events for absorption research, and realistic market-impact/fill simulation
require separately sourced data. Until those limitations are addressed,
prospective outcomes and train/validation/out-of-sample summaries are research
diagnostics, not evidence of a profitable strategy.

The current REST scan is rate-limited and sequential. It is designed for
research cadence rather than low-latency execution. The existing live order
execution and prior backtest outputs are not changed by this subsystem.
