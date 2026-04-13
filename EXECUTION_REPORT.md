# Repository execution and module analysis report

Generated on 2026-04-13 (UTC) in the sandbox environment.

## Module responsibilities

- `main.py`: live trading orchestrator. It fetches live perpetual futures, filters symbols, gets 5m candles, evaluates `short_signal`, asks for interactive confirmation, places entry order, waits for fill/position, then places SL/TP and exits after one trade per run.
- `scanner.py`: market discovery utility that fetches `/v2/products` (first via authenticated client, then fallback public REST) and returns only live perpetual futures.
- `strategy_short.py`: strategy library containing:
  - EMA short trigger (`short_signal`) used by `main.py`.
  - Multi-hour runner trigger (`multihour_runner_short_signal`) and its helpers used by `backtest.py`.
- `backtest.py`: offline historical backtester for the multi-hour strategy. Pulls hourly candles, simulates entry/exit logic with SL/TP/max-hold, and writes summary + CSV when trades exist.
- `delta_client.py`: API client bootstrap from environment variables (`DELTA_API_KEY`, `DELTA_API_SECRET`, optional `DELTA_BASE_URL`).
- `requirements.txt`: runtime dependency list.

## API key handling notes

- API credentials are loaded from environment variables through `dotenv`.
- If API credentials are missing, the code logs a warning and still allows public endpoints.
- Any private order routes in `main.py` require valid keys; otherwise order placement cannot succeed.

## Seamless runtime probability (sandbox estimate)

Estimated probability of running seamlessly end-to-end in this exact sandbox: **10%**.

Reasoning:
1. Outbound connectivity to Delta endpoints failed with network/proxy errors in this environment.
2. Credentials are not present in the environment.
3. `main.py` needs interactive confirmation + live API + auth for order placement.

With valid network + keys + symbol availability, probability would be materially higher.

## Executed commands and outputs

### 1) Syntax check
Command:

```bash
python -m py_compile *.py
```

Output:

```text
(no output; command succeeded)
```

### 2) Scanner script
Command:

```bash
python scanner.py
```

Output:

```text
⚠️ DELTA_API_KEY / DELTA_API_SECRET not found. Public endpoints will still work; private order routes will fail.
⚠️ Client /v2/products request failed: HTTPSConnectionPool(host='api.india.delta.exchange', port=443): Max retries exceeded with url: /v2/products (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))
❌ Public /v2/products fallback failed: HTTPSConnectionPool(host='api.india.delta.exchange', port=443): Max retries exceeded with url: /v2/products (Caused by NewConnectionError("HTTPSConnection(host='api.india.delta.exchange', port=443): Failed to establish a new connection: [Errno 101] Network is unreachable"))
Perpetual Futures Found: 0
```

### 3) Backtest script
Command:

```bash
python backtest.py --symbols BTCUSD --limit 200
```

Output:

```text
BTCUSD: skipped due to error -> HTTPSConnectionPool(host='api.india.delta.exchange', port=443): Max retries exceeded with url: /v2/history/candles?symbol=BTCUSD&resolution=1h&start=1775342718&end=1776062718 (Caused by NewConnectionError("HTTPSConnection(host='api.india.delta.exchange', port=443): Failed to establish a new connection: [Errno 101] Network is unreachable"))
No trades generated.
```

### 4) Strategy module (function-level execution)
Command:

```bash
python - <<'PY'
import pandas as pd
from strategy_short import short_signal,multihour_runner_short_signal

rows=[
    {'time':'2026-01-01 00:00:00','open':100,'high':101,'low':99.8,'close':100.5},
    {'time':'2026-01-01 00:05:00','open':100.6,'high':101.2,'low':100.4,'close':101.0},
    {'time':'2026-01-01 00:10:00','open':101.1,'high':101.8,'low':100.9,'close':101.5},
    {'time':'2026-01-01 00:15:00','open':101.6,'high':102.1,'low':101.4,'close':101.9},
    {'time':'2026-01-01 00:20:00','open':102.0,'high':102.4,'low':101.8,'close':102.2},
    {'time':'2026-01-01 00:25:00','open':102.3,'high':102.9,'low':102.1,'close':102.7},
    {'time':'2026-01-01 00:30:00','open':102.6,'high':102.8,'low':101.9,'close':102.0},
]
df=pd.DataFrame(rows)
print('short_signal:', short_signal(df))

rows2=[]
price=100
for i in range(6):
    o=price
    c=price+1
    rows2.append({'time':f'2026-01-01 {i:02d}:00:00','open':o,'high':c+0.3,'low':o-0.2,'close':c})
    price+=1
df2=pd.DataFrame(rows2)
print('multihour_runner_short_signal:', multihour_runner_short_signal(df2))
PY
```

Output:

```text
short_signal: {'signal': 'SHORT', 'entry': 102.1, 'stop_loss': 102.9, 'take_profit': 99.69999999999996, 'alert_candle_time': '2026-01-01 00:25:00', 'entry_candle_time': '2026-01-01 00:30:00', 'strategy': 'ema_short'}
multihour_runner_short_signal: {'signal': 'SHORT', 'entry': 105.0, 'stop_loss': 105.2625, 'take_profit': 99.75, 'quantity': 19.047619047619047, 'capital_deployed': 100.0, 'leverage': 20.0, 'setup_start_time': '2026-01-01 00:00:00', 'setup_end_time': '2026-01-01 04:00:00', 'entry_candle_time': '2026-01-01 05:00:00', 'strategy': 'multihour_runner_short'}
```

### 5) Main script
Command:

```bash
printf 'n\n' | python main.py
```

Output:

```text
⚠️ DELTA_API_KEY / DELTA_API_SECRET not found. Public endpoints will still work; private order routes will fail.
⚠️ Client /v2/products request failed: HTTPSConnectionPool(host='api.india.delta.exchange', port=443): Max retries exceeded with url: /v2/products (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))
❌ Public /v2/products fallback failed: HTTPSConnectionPool(host='api.india.delta.exchange', port=443): Max retries exceeded with url: /v2/products (Caused by NewConnectionError("HTTPSConnection(host='api.india.delta.exchange', port=443): Failed to establish a new connection: [Errno 101] Network is unreachable"))
❌ No live perpetual futures found. Check API connectivity/base URL.
```

## Clarification: why Delta API failed here, and why short signals still printed

- In this sandbox run, outbound HTTPS calls to `api.india.delta.exchange` failed with proxy/network errors (`403` tunnel and `Errno 101 Network is unreachable`).
- This is an environment connectivity issue, not proof that Delta API itself is globally down.
- `scanner.py`, `backtest.py`, and live `main.py` all depend on that network path; therefore they could not fetch live products/candles in this run.

How a short signal was still generated:
- The `short_signal` and `multihour_runner_short_signal` functions are pure data functions from `strategy_short.py`.
- They can run entirely offline if given a pandas DataFrame with OHLC columns.
- The previous run used synthetic sample candles in an inline Python snippet to validate strategy logic only; no Delta API call is required for that function-level test.

## Clarification: historical data question

Q: "How did historic data work if API was failing?"

- In this sandbox run, **historical market data from Delta was not fetched successfully**.
- The backtest historical call (`/v2/history/candles`) was attempted and failed with `Network is unreachable`.
- So no real historical candles were downloaded during this run.
- The only successful strategy outputs came from a separate offline snippet that built synthetic candle DataFrames locally and passed them to strategy functions.
- Therefore, there was no contradiction: live/public/historical API routes all depended on the same blocked network path in this environment.

## How to verify API connectivity on your machine

Use these checks in order from public to authenticated endpoints.

1) DNS + HTTPS reachability (public)

```bash
curl -i --max-time 20 "https://api.india.delta.exchange/v2/products"
```

Expected:
- HTTP status should be `200`.
- JSON should include `"success": true` and a `result` array.

2) Public candles endpoint test

```bash
END=$(date +%s)
START=$((END-3600*24))
curl -sS --max-time 20 "https://api.india.delta.exchange/v2/history/candles?symbol=BTCUSD&resolution=1h&start=${START}&end=${END}" | python -m json.tool | head -n 40
```

Expected:
- `success: true`
- non-empty `result` list for active symbols/times.

3) Verify project scanner path (same as bot uses)

```bash
python scanner.py
```

Expected:
- It should print `Live perpetual futures fetched: <n>` and `Perpetual Futures Found: <n>` with `n > 0`.

4) Verify backtest fetch path

```bash
python backtest.py --symbols BTCUSD --limit 50
```

Expected:
- No network/proxy exception.
- Either trade count lines per symbol, or `No trades generated.` if no setups occurred.

5) Verify authenticated route with your keys

Create `.env` in repo root:

```env
DELTA_API_KEY=your_key_here
DELTA_API_SECRET=your_secret_here
DELTA_BASE_URL=https://api.india.delta.exchange
```

Then run:

```bash
python - <<'PY'
from delta_client import client
try:
    r = client.request("GET", "/v2/products")
    print("status:", r.status_code)
    print("success:", r.json().get("success"))
except Exception as e:
    print("auth/client call failed:", e)
PY
```

If this fails while `curl` public calls succeed, focus on key validity, account permissions, system clock sync, and signature settings.

6) Optional: verbose HTTP diagnostics

```bash
curl -v --max-time 20 "https://api.india.delta.exchange/v2/products" -o /tmp/delta_products.json
```

Look for:
- proxy usage (`CONNECT`, `HTTP_PROXY`),
- TLS handshake errors,
- non-200 response.

If running behind a corporate proxy, compare behavior with and without proxy env vars:

```bash
env | grep -Ei 'http_proxy|https_proxy|no_proxy'
```

In Python, this repo disables proxy inheritance in `scanner.py` and `backtest.py` sessions (`session.trust_env = False`), so proxy-only networks may require code/config adjustments.
