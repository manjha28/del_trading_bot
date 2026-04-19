# Strategy Progress

Running record of strategy specs, backtest results, and open hypotheses.

---

## Strategy Specs

### `mhr_pump_short`

**Status:** Active — integrated in backtest and live trading (`main.py`). Never live-traded.  
**Timeframe:** 1h candles  
**Direction:** Short only  
**Exchange:** Delta Exchange India perpetual futures

#### Entry paths (evaluated in order)

| Path | Trigger | ATR multiplier |
|------|---------|---------------|
| `REENTRY` | Pump still active (≥20% up in 24h) + price below previous entry + hesitation signal | 2.5x |
| `MHR_EXHAUSTION` | 4 consecutive green candles, last candle body ratio not a blow-off top | 1.5x |
| `PUMP_HESITATION` | Price pumped ≥50% in 24h with ≥2x volume surge + hesitation signal | 2.5x |

#### Hesitation signal (`pump_hesitation_detected`) — any one of:
- Current candle is red
- Upper wick present (any size)
- Current body < 50% of prior candle body
- Current high ≤ prior candle high

#### Exit model
- 50% of qty at TP = 2R (min: `sl_capital_pct × min_rr` = 5% × 4 = 20% of capital)
- Remaining 50% trails with SL at breakeven after partial fill
- Max hold: 24 candles
- SL hit on pump trade → reentry watch (up to 3 reentries, 1 candle cooldown between)

#### Current default parameters

```
lookback_green_candles = 4
min_pump_pct           = 50.0%
lookback_hours         = 24
min_volume_surge_pct   = 2.0x
leverage               = 20x
sl_capital_pct         = 5%
max_capital_usage_pct  = 50%
atr_sl_multiplier      = 1.5
pump_atr_sl_multiplier = 2.5
min_rr                 = 4.0
max_reentries          = 3
reentry_cooldown       = 1 candle
charge_rate            = 0.05%
max_hold               = 24h
```

#### History / lineage
- Preceded by `mhr_trailing_rr4` (trailing exit model) and `parabolic_short` (pump-detection focus) — neither produced results independently
- `mhr_rsi` (EMA20 distance + RSI > 65 + rejection wick as hard gates) also did not produce results
- Current strategy merges the best elements: trailing exit from `mhr_trailing_rr4`, pump detection from `parabolic_short`
- EMA20 distance and RSI14 are now logged per trade as diagnostics but are **not active hard-gate filters**
- `entry_candle_rejection_ok()` exists in code but is not currently called

---

### `scalp_pullback`

**Status:** Backtest only. Planned for live trading eventually (target: HFT use case).  
**Timeframe:** 5m candles (derived from 1m via resampling); trend filter from 1h  
**Direction:** Both (trend-filtered: long when 1h bullish, short when 1h bearish)

#### Entry rules

**Long:** 1h price > EMA50 + 5m signal candle green + 5m price > EMA20 + ≥4 consecutive red candles prior + signal candle volume ≥ average  
**Short:** 1h price < EMA50 + 5m signal candle red + 5m price < EMA20 + ≥4 consecutive green candles prior + signal candle volume ≥ average

#### Exit model
- TP1 at 1.4R → take 50% of position
- Trail SL to breakeven (1.0R) after TP1 fills
- SL hit on remainder → close trade
- Max hold: 30 candles (~2.5h on 5m)

#### Current parameters

```
ema_5m_length         = 20
ema_1h_length         = 50
min_pullback_candles  = 4    (Tweak 3: was 2)
max_pullback_candles  = 6
sl_buffer_pct         = 0.25%  (Tweak 5)
vol_lookback          = 20   (Tweak 4)
tp1_rr                = 1.4
tp1_qty_pct           = 50%
trail_lock_rr         = 1.0
leverage              = 20x
sl_capital_pct        = 2%
max_holding_candles   = 30
cooldown_candles      = 3
```

#### Tweaks applied
- Tweak 3: `min_pullback_candles` raised to 4 — requires deeper pullbacks
- Tweak 4: Volume confirmation on signal candle
- Tweak 5: SL buffer raised to 0.25% — reduces false stop-outs

---

### `fast_slow_ma_long` (in `long_strategy.py`)

**Status:** WIP. Infrastructure complete in isolation. Not integrated into `backtest.py` or `main.py`. Second priority after short strategy is stable in live trading.

#### What exists
- 12 MA type implementations (EMA, SMA, RMA, WMA, VWMA, TEMA, DEMA, TMA, ALMA, HMA, LSMA, ZLEMA)
- Crossover/crossunder signal generation
- ATR-based SL/TP calculation
- Exit simulator (checks SL, TP, and opposite crossover)
- `fast_slow_ma_entry_filter()` compatible with backtest dispatch pattern

#### What is missing before integration
- [ ] Wired into `backtest.py` `SUPPORTED_STRATEGIES` and `_process_symbol()`
- [ ] Wired into `main.py` for live execution
- [ ] Fetch 1h candles path tested
- [ ] Parameter sweep done

#### Default parameters
```
fast_length  = 24
slow_length  = 65
ma_type      = EMA
allow_long   = True
allow_short  = False
tp_rr        = 3.0
capital_pct  = 50%
```

---

## Backtest Run Log

Template for each run. Add a new entry below after every meaningful backtest.

---

### Run 001 — [DATE]

**Date:** YYYY-MM-DD  
**Strategy:** `mhr_pump_short`  
**Symbols:** [list or "all live perpetuals"]  
**Period:** YYYY-MM-DD → YYYY-MM-DD  
**Command:**
```bash
python backtest.py [args used]
```

**Config changes from default:**
- [parameter]: [value]

**Results (`backtest_overall.csv`):**

| Metric | Value |
|--------|-------|
| Initial capital | $100 |
| Final equity | |
| Total return % | |
| Total trades | |
| Win rate | |
| Total net PnL | |
| Avg net PnL/trade | |
| Max drawdown | |
| Entry path split (MHR / PUMP / REENTRY) | |

**Per-symbol highlights (`backtest_summary.csv`):**
- Best symbol:
- Worst symbol:

**Observations:**
- 

**Next action:**
- 

---

## Ideas / Hypothesis Log

Ordered loosely by priority. Mark tested ideas with their result.

---

### Open

**[IDEA-001] Activate EMA20 distance as a hard gate on MHR_EXHAUSTION**  
Hypothesis: requiring `close ≥ 2% above EMA20` before triggering `MHR_EXHAUSTION` will filter out weak setups and improve win rate at the cost of fewer trades. The field `ema_distance_pct` is already being logged per trade — analyze distribution on winners vs losers to pick a threshold.  
Implementation: add guard in `unified_entry_filter()` before `exhaustion_setup_ok()` check.

---

**[IDEA-002] Activate RSI14 gate on MHR_EXHAUSTION**  
Hypothesis: `RSI14 > 65` at entry indicates overbought condition and will improve the short signal quality. `rsi14` is already logged per trade.  
Dependency: needs data from [IDEA-001] run to validate threshold choice.

---

**[IDEA-003] Activate `entry_candle_rejection_ok()` as a filter**  
Hypothesis: requiring upper wick > body on the entry candle confirms sellers are stepping in. Function already exists and is tested. Add as a filter for `MHR_EXHAUSTION` path only.  
Risk: may over-filter on coins with small-body candles; check rejection rate on sample symbols first.

---

**[IDEA-004] Compare `MHR_EXHAUSTION` vs `PUMP_HESITATION` performance independently**  
Run a backtest with only `MHR_EXHAUSTION` trades (filter on `entry_path` column in output). Repeat for `PUMP_HESITATION`. Determine if one path is dragging down overall win rate.

---

**[IDEA-005] Integrate `scalp_pullback` into `main.py`**  
Planned for HFT use. Prerequisites: (1) complete and stable `mhr_pump_short` in live, (2) verify 5m candle fetch latency from Delta API, (3) decide on order type (market vs limit) for scalp entries given tight execution window.

---

**[IDEA-006] Integrate `fast_slow_ma_long` into `backtest.py`**  
Add `"fast_slow_ma_long"` to `SUPPORTED_STRATEGIES` in `backtest.py`, dispatch to `long_strategy.py` in `_process_symbol()`, fetch 1h candles. Run parameter sweep on `fast_length`, `slow_length`, and `ma_type`.

---

**[IDEA-007] Persist `watching` state across restarts**  
Currently `trade_state.json` only tracks `trade_count`, `last_trade_day`, `next_trade_allowed_ts`. The `watching` dict (reentry state) is initialised from an empty dict on load. On restart, any active pump watch is lost. Serialize `watching` to `trade_state.json` on every `save_state()` call.

---

### Tested / Resolved

**[IDEA-T001] Merge `mhr_trailing_rr4` + `parabolic_short` into a single strategy**  
Result: done → `mhr_pump_short`. Individual predecessors did not produce backtest results.

**[IDEA-T002] `mhr_rsi` as a separate strategy variant with hard EMA20/RSI gates**  
Result: did not produce results. EMA/RSI fields retained as diagnostics only in `mhr_pump_short`.

**[IDEA-T003] Remove `mhr_static_rr4`**  
Result: done. Trailing exit universally replaced the fixed RR exit.
