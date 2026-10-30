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

| Path | Trigger | SL method | Backtest verdict |
|------|---------|-----------|-----------------|
| `REENTRY` | Pump still active (≥20% up in 24h) + price below previous entry + hesitation signal | structural → ATR fallback | **NEGATIVE** — do not use |
| `MHR_EXHAUSTION` | 4 consecutive green candles, last candle body ratio not a blow-off top | structural → ATR fallback | **POSITIVE** — sole productive path |
| `PUMP_HESITATION` | Price pumped ≥50% in 24h with ≥2x volume surge + hesitation signal | structural → ATR fallback | **13.9% WR** — deeply negative, do not use |

**Recommended config:** `--entry-paths MHR_EXHAUSTION` only.

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
atr_sl_multiplier      = 1.5      # used as fallback only
pump_atr_sl_multiplier = 2.5      # used as fallback only
min_rr                 = 4.0
max_reentries          = 3
reentry_cooldown       = 1 candle
charge_rate            = 0.05%
max_hold               = 24h
```

#### SL method (structural — primary)
SL is placed at `prev_candle_high` (high of candle before entry). No buffer.  
`qty = floor(max_loss / (prev_high - entry_price))`  
Falls back to ATR if gap-up (prev_high ≤ entry_price) or qty < 1.

**Why structural:** brings the 2R partial-TP target to a reachable distance. 2R fill rate was 3.4% on ATR-based SL → 34% on structural SL. TRAIL_STOP_AFTER_PARTIAL exit has 97.1% WR once 2R is reached.

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

---

### Run 001 — 2026-04-19 (ATR baseline, 20 pump coins)

**Date:** 2026-04-19  
**Strategy:** `mhr_pump_short`, all 3 entry paths, ATR-based SL  
**Symbols:** 20 (AINUSD AIOUSD ARCUSD BLESSUSD COAIUSD CROSSUSD EVAAUSD HANAUSD HUSD HYPEUSD KITEUSD LIGHTUSD MONUSD MUSD PARTIUSD PIPPINUSD RIVERUSD SAHARAUSD SOLVUSD VVVUSD)  
**Period:** 2025-10-21 → 2026-04-19 (180 days)  
**Command:**
```bash
.venv/bin/python backtest.py --symbols [20 symbols] --strategies mhr_pump_short --days 180 --workers 4
```

**Results (after equity compounding bug — NOT reliable aggregate, use per-symbol):**
- Gross: +$167, Fees: −$173, Net: −$6.35
- Profitable symbols: 10/20
- 2R partial fill rate: ~3.4%

**Key finding:** Fee drag exceeded gross profit. Strategy was capital-starved at $100 with ATR-based SL producing very wide stops → tiny qty → tiny gross PnL unable to cover fees.

---

### Run 002 — 2026-04-19 (structural SL, all 3 paths)

**Date:** 2026-04-19  
**Config change:** structural SL (prev candle high), all 3 entry paths  
**Command:**
```bash
.venv/bin/python backtest.py --symbols [20 symbols] --strategies mhr_pump_short --days 180 --workers 4
```

**Per-symbol standalone results (reliable):**
- Profitable symbols: 19/20
- Avg standalone return: +$96.31 on $100

**Key finding from entry path split:**
- MHR_EXHAUSTION: 95% of trades, WR 47%, strongly positive
- PUMP_HESITATION: 13.9% WR, deeply negative
- REENTRY: 41.4% WR, negative

**Action taken:** re-run with `--entry-paths MHR_EXHAUSTION` only.

---

### Run 003 — 2026-04-19 ✅ DEFINITIVE (MHR_EXHAUSTION + structural SL)

**Date:** 2026-04-19  
**Strategy:** `mhr_pump_short`, MHR_EXHAUSTION only, structural SL  
**Symbols:** same 20  
**Period:** 2025-10-21 → 2026-04-19 (180 days)  
**Command:**
```bash
.venv/bin/python backtest.py \
  --symbols AINUSD AIOUSD ARCUSD BLESSUSD COAIUSD CROSSUSD EVAAUSD HANAUSD \
            HUSD HYPEUSD KITEUSD LIGHTUSD MONUSD MUSD PARTIUSD PIPPINUSD \
            RIVERUSD SAHARAUSD SOLVUSD VVVUSD \
  --strategies mhr_pump_short --days 180 --workers 4 --entry-paths MHR_EXHAUSTION
```

**Results:**
| Metric | Value |
|--------|-------|
| Profitable symbols | 18 / 20 |
| Avg final equity | $202.45 (start: $100) |
| Total trades | 1,234 |
| Win rate | 47.0% |
| Total gross PnL | $2,792.80 |
| Total fees | $743.88 (26.6% of gross) |
| **Total net PnL** | **$2,048.92** |
| Max drawdown | −$88.19 |
| 2R partial fill rate | 34% |
| ≤1h WR | 6.4% (44.5% of trades) |
| 2–4h WR | 82.1% |

**Top symbols:** BLESSUSD +440%, HUSD +316%, AINUSD +310%, KITEUSD +281%  
**Worst:** ARCUSD −$2.87, PARTIUSD −$2.63 (too few signals on these coins)

**Output files:** `strategy-short/mhr_pump_short/backtest_results/20260419_153627_*`

**Observations:**
- Structural SL works because it makes 2R achievable: 2R fill rate 3.4% → 34%
- TRAIL_STOP_AFTER_PARTIAL WR is 97.1% — once 2R is hit, the trail exit almost never fails
- ≤1h cluster is main drag: 549 trades, 6.4% WR. Every trade surviving bar 1 wins 73–82%
- ATR fallback fires on gap-up candles; those hold 3× longer (avg 6.9h) but weak return ($0.08 avg net vs $1.95 structural)
- HYPEUSD: fee drag 58% of gross due to high frequency + small-priced coin

**Next action:** implement 1-bar hold filter to eliminate ≤1h losers.

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

**[IDEA-008] 1-bar hold filter on MHR_EXHAUSTION entries**  
Hypothesis: 44.5% of trades stop out in ≤1h with only 6.4% WR. These are almost certainly false setups where the market immediately negates the entry. Requiring the trade to still be open at close of bar 1 (or using bar N+1 as the actual entry candle) should eliminate most of these without touching the profitable 2–4h trades (82.1% WR).  
Implementation: add `min_hold_candles: int = 0` to `BacktestConfig`; in `_sim_trailing_exit` skip SL hits until bar 1 completes (or adjust entry to next candle).

---

**[IDEA-009] Test at higher capital ($1,000)**  
Fee drag is 26.6% of gross at $100 capital. At $1,000, gross scales up while fees remain proportionally similar → net return as % of capital should improve. Run same 20 symbols at `--initial-capital 1000` to confirm strategy is viable at live-trading capital levels.

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

**[IDEA-T004] Activate EMA20 / RSI14 as hard entry gates (IDEA-001, IDEA-002)**  
Tested during pump coin backtest analysis. Result: RSI≥65 gate drops WR from 48.4% to 38.8%; EMA distance >2% also makes results worse. Winners have *lower* RSI (57.0) and *lower* EMA distance (2.49%) than losers. Gates are counterproductive. EMA/RSI remain diagnostics only. Close IDEA-001 and IDEA-002.

**[IDEA-T005] Compare MHR_EXHAUSTION vs PUMP_HESITATION individually (IDEA-004)**  
Done in Run 002 / Run 003. Result: PUMP_HESITATION = 13.9% WR, deeply negative. REENTRY = 41.4% WR, also negative. MHR_EXHAUSTION is the only positive path. Closed. Use `--entry-paths MHR_EXHAUSTION`.

**[IDEA-T006] Structural SL (prev candle high as stop level)**  
Done in Run 002 / Run 003. Result: 2R fill rate 3.4% → 34%, profitable symbols 10/20 → 18/20, avg return −$0.32 → +$102.45. This is the primary SL method going forward.
