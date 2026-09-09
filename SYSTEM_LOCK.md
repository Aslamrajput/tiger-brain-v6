# 🔒 TIGER SYSTEM — LOCKED v1.0 — 09-SEP-2026 20:00 IST

```
OWNER:      TIGER OWNER (Mumbai)
LOCKED BY:  FINAL ARCHITECTURE AS ON 09-SEP-2026 20:00 IST
GIT COMMIT: 400b8d7 — "Allow STOCK options — liquidity pipeline filter (Top 10-11)"
GIT TAG:    v1.0-LOCKED
BRANCH:     main
EC2:        3.108.53.100  (~/tiger-brain-v6)
SERVICE:    tiger-brain.service  (systemd, active)
BALANCE:    ₹30,039.30

THIS IS FINAL. NO CHANGES ALLOWED WITHOUT OWNER PERMISSION.
```

---

## 1. INSTRUMENT MASTER

| Property | Value |
|---|---|
| Total Tokens | **42,029 ONLY** (from 145,599 filtered) |
| JSON Cache | **10.9 MB** (10,901,670 bytes) |
| Cache Date | 2026-09-09 |
| Cache File | `data/instruments_filtered.json` — DO NOT REGENERATE FULL |
| Allowed | INDEX + MCX + STOCK OPTIONS (OPTSTK) + EQ SPOT (-EQ) |
| Source | `data/loader.py` → `load_angel_instrument_master()` |

### Filter logic (`_filter_instruments` in `data/loader.py`)

```
Keep IF:
  (name IN {NIFTY, BANKNIFTY, FINNIFTY, SENSEX, GOLDM, SILVERM, CRUDEOIL, NATURALGAS}
   AND exch_seg IN {NSE, NFO, BSE, BFO, MCX}
   AND instrumenttype IN {AMXIDX, OPTIDX, FUTIDX, OPTFUT, FUTCOM, INDEX})
  OR
  (exch_seg == "NFO" AND instrumenttype == "OPTSTK")   ← ALL stock options
  OR
  (exch_seg == "NSE" AND symbol ENDS WITH "-EQ")       ← ALL stock spot tokens
```

### Config constants (`data/loader.py`)

```
ALLOWED_INDEX          = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
ALLOWED_MCX            = ["GOLDM", "SILVERM", "CRUDEOIL", "NATURALGAS"]
ALLOWED_INSTRUMENT_NAMES = ALLOWED_INDEX + ALLOWED_MCX
ALLOWED_EXCH_SEGS      = {"NSE", "NFO", "BSE", "BFO", "MCX"}
USEFUL_INSTRUMENT_TYPES = {"AMXIDX", "OPTIDX", "FUTIDX", "OPTFUT", "FUTCOM", "INDEX"}
```

---

## 2. INDEX TOKENS (FINAL)

### Underlying spot tokens (`INDEX_UNDERLYING_TOKENS` in `data/loader.py`)

```
NIFTY      → ("NSE", "99926000")
BANKNIFTY  → ("NSE", "99926009")
FINNIFTY   → ("NSE", "99926037")
SENSEX     → ("BSE", "99919000")
```

### Option instrument types (`OPTION_INSTRUMENT_TYPE` in `data/loader.py`)

```
NIFTY       → ("OPTIDX", "NFO")
BANKNIFTY   → ("OPTIDX", "NFO")
FINNIFTY    → ("OPTIDX", "NFO")
SENSEX      → ("OPTIDX", "BFO")
```

### Scan symbols (`INDEX_SYMBOLS` in `universe/fno_universe.py`)

```
NIFTY      → "^NSEI"
BANKNIFTY  → "^NSEBANK"
FINNIFTY   → "^CNXFIN"
SENSEX     → "^BSESN"
```

### Lot sizes (`LOT_SIZES` in `universe/fno_universe.py`)

```
NIFTY      → 75
BANKNIFTY  → 35
FINNIFTY   → 65
```

---

## 3. MCX TOKENS (FINAL)

### Scan symbols (`mcx_scan_symbols()` in `universe/fno_universe.py`)

```
GOLDM       → "GC=F"
SILVERM     → "SI=F"
CRUDEOIL    → "CL=F"
NATURALGAS → "NG=F"
```

### Option instrument types (`OPTION_INSTRUMENT_TYPE` in `data/loader.py`)

```
CRUDEOIL     → ("OPTFUT", "MCX")
CRUDEOILM    → ("OPTFUT", "MCX")
NATURALGAS   → ("OPTFUT", "MCX")
NATGASMINI   → ("OPTFUT", "MCX")
GOLD         → ("OPTFUT", "MCX")
GOLDM        → ("OPTFUT", "MCX")
SILVER       → ("OPTFUT", "MCX")
SILVERM      → ("OPTFUT", "MCX")
```

### MCX MINI fallback (`MCX_MINI_FALLBACK` in `data/loader.py`)

```
CRUDEOIL    → CRUDEOILM      (lot 100 → 10)
NATURALGAS → NATGASMINI      (lot 1250 → 250)
GOLD        → GOLDM           (lot 1 → 100, premium-based)
SILVER      → SILVERM         (lot 30 → 1)
```

---

## 4. SESSION SCHEDULE (FINAL)

```
09:00         Pre-market wake (login + NSE data fetch)
09:15-10:30   🔥 MORNING BURST    (NSE, threshold 78, ITM, HIGH aggression)
10:30-13:00   📈 TREND HUNT       (NSE, threshold 80, ATM, MEDIUM aggression)
13:00-14:30   💰 DISCOUNT BUY     (NSE, threshold 75, OTM, MEDIUM aggression)
14:30-15:15   ⚡ POWER HOUR      (NSE, threshold 78, ITM, MAX aggression)
15:00         Intraday entry CUTOFF (no new intraday, only exits/delivery)
15:15         NSE SQUARE-OFF (close all NSE positions)
15:15-15:30   NSE closing (no new entries)
15:30         MCX MARKET OPEN
15:30-17:00   TRANSITION (no active session — 999 threshold)
17:00-20:00   🛢️ COMMODITY OPEN   (MCX, threshold 70, ATM, MEDIUM aggression)
20:00-23:00   🌙 NIGHT RUSH       (MCX, threshold 72, ATM, HIGH aggression)
23:00-23:15   SQUARE OFF          (MCX, threshold 70, ATM, close positions)
23:15         MCX SQUARE-OFF (close all MCX positions + logout)
```

### Two-market rule — NEVER overlap

```
NSE:  09:15 → 15:15  (square-off 15:15, session end 15:30)
MCX:  15:30 → 23:15  (square-off 23:15)
Tiger hunts NSE FIRST, then MCX. Sequential only — never simultaneous.
```

### Config (`config/thresholds.py` → `AUTOMATION`)

```
PRE_MARKET_WAKE_TIME     = "09:00"
MARKET_OPEN_TIME         = "09:15"
MARKET_CLOSE_TIME        = "15:30"    ← NSE session end
NSE_SQUARE_OFF_TIME      = "15:15"    ← close NSE positions
MCX_OPEN_TIME            = "15:30"    ← MCX starts AFTER NSE square-off
MCX_CLOSE_TIME           = "23:15"    ← MCX session end
MCX_SQUARE_OFF_TIME      = "23:15"    ← close MCX positions
INTRADAY_ENTRY_CUTOFF    = "15:00"    ← no new NSE intraday after 3PM
DELIVERY_SNAPSHOT_TIME   = "15:00"    ← overnight direction decision
RESCAN_INTERVAL_MINUTES  = 20          ← scan every 20 min, active market only
TRADING_DAYS             = MON-FRI
WATCH_ONLY_DAYS          = SAT
OFF_DAYS                 = SUN
```

### Force Hunt (`should_force_hunt` in `backtest/tiger_session_brain.py`)

```
NSE 14:30-15:15  AND nse_trades  < 2  → force_hunt, threshold 72.0
MCX 20:00-22:00  AND total_trades < 3 → force_hunt, threshold 70.0
MCX 22:00-23:15  AND total_trades == 0 → force_hunt, threshold 65.0  ← ZERO trades rule
MCX 22:00-23:15  AND total_trades < 3  → force_hunt, threshold 68.0
```

---

## 5. STOCK LIQUIDITY PIPELINE (FINAL)

### Pipeline (`universe/stock_filter.py`)

```
All F&O Stocks (~180)
    ↓
Stage 1: Option Volume filter      (≥ 500,000 contracts/day)
    ↓
Stage 2: Premium Turnover filter   (≥ ₹500 lakh/day)
    ↓
Stage 3: Open Interest filter      (≥ 100,000 OI)
    ↓
Stage 4: Liquidity Score
    = 35% × volume_rank
    + 25% × turnover_rank
    + 25% × oi_rank
    + 15% × spread_proxy_rank   (active strikes count = bid-ask spread proxy)
    ↓
Top 10-11 Stocks
    ↓
Signal → CALL / PUT (options buying ONLY)
```

### Config (`universe/stock_filter.py`)

```
MIN_OPTION_VOLUME         = 500_000    (contracts/day)
MIN_PREMIUM_TURNOVER_LAKH = 500        (₹500 lakh/day)
MIN_OPEN_INTEREST         = 100_000    (1 lakh OI)
TOP_N_STOCKS              = 11
Liquidity Score weights   = 0.35 / 0.25 / 0.25 / 0.15
```

### Fallback top stocks (if Bhavcopy unavailable — holiday/weekend/NSE block)

```
RELIANCE, HDFCBANK, ICICIBANK, INFY, SBIN,
AXISBANK, LT, BHARTIARTL, ITC, KOTAKBANK,
BAJFINANCE
```

### Scan order (`nse_scan_symbols()` in `universe/fno_universe.py`)

```
1. INDEX FIRST (priority):  NIFTY → BANKNIFTY → FINNIFTY → SENSEX
2. STOCKS AFTER:            Top 10-11 (Bhavcopy liquidity filter)
```

### Signal priority (`scan_live_signals` in `automation/live_scanner.py`)

```
Signals sorted: INDEX signals FIRST, STOCK signals AFTER
_place_live_orders processes index trades before stock trades
```

### Symbol guard (`_is_symbol_allowed` in `backtest/run_tiger_brain_backtest.py`)

```
All symbols ALLOWED (safety net returns True)
Stock liquidity filter handles upstream — only top 10-11 in scan universe
```

---

## 6. OPTIONS BUYING ONLY (FINAL)

```
Tiger does options BUYING ONLY.
No option selling. No option writing. No short positions.

Entry direction:
  BUY  = demand zone touch → CALL  (CE)
  SELL = supply zone touch → PUT   (PE)

Both directions = options BUYING (long CE / long PE).
Tiger never sells/writes options.
```

---

## 7. V18 IV FILTER (LOOSE — FINAL)

### IV percentile thresholds (`backtest/tiger_premium_brain.py`)

```
IV_DEEP_DISCOUNT_MAX = 30.0    ← <30%  = cheapest premium (best entry)
IV_DISCOUNT_MAX      = 50.0    ← 30-50 = discount (good entry)
IV_FAIR_MAX          = 85.0    ← 50-85 = fair (V18 loose entry — was 65, too strict)
IV_EXPENSIVE_EXIT    = 90.0    ← >90%  = exit signal (was 70 — only exit truly expensive)
IV_PERCENTILE_LOOKBACK = 100   ← ~4 trading days of 25 bars/day
```

### Entry rules

```
IV percentile < 85  → entry ALLOWED (DEEP / DISCOUNT / FAIR)
IV percentile ≥ 90  → EXIT signal (holding only)
85-90 range         → no new entry, but existing position not exited
```

---

## 8. V19 EXIT ENGINE (FINAL)

### Exit parameters (`backtest/run_tiger_brain_backtest.py`)

```
V19_TRAIL_ACTIVATE_PCT      = 5.0     ← activate trail at +5% profit
V19_TRAIL_LOCK_PCT          = 70.0    ← lock 70% of peak (gives back only 30%)
V19_FIXED_TARGET_PCT        = 50.0    ← book profit at +50%
V19_FIXED_TARGET_BOOK       = 0.40    ← book 40% of position at target (ride 60%)
V19_RUNAWAY_EXIT_PCT        = 250.0   ← absolute safety exit (+250%)
V19_PRE_SQOFF_TRAIL_LOCK_PCT = 80.0   ← tighter lock in pre-sqoff window
```

### Exit logic flow

```
+5% gain    → trail activates (lock 70% of peak)
+50% gain   → book 40% position (ride remaining 60%)
+250% gain  → RUNAWAY EXIT (full close, safety)
Pre-sqoff   → trail lock tightens to 80% (15-min window before square-off)
```

---

## 9. DAILY QUOTA & SCAN CONFIG (FINAL)

### Brain 4 — Trade Counter Guard (`automation/live_scanner.py`)

```
max_entries_per_day = 5          ← max 5 trades/day
daily_entries_taken >= 5 → scan SKIPPED (quota full)
```

### Scan interval

```
RESCAN_INTERVAL_MINUTES = 20     ← every 20 min, active market only
Opening range wait      = 15 min ← no scan in first 15 min (09:15-09:30)
```

---

## 10. SEVEN BRAINS (FINAL)

```
Brain 1: Momentum & Noise Filter      (gate — bar quality check)
Brain 2: SMC Setup Detection          (gate — supply/demand zones + volume delta)
Brain 3: Scoring Pipeline             (rocket momentum scorer)
Brain 4: Trade Counter Guard          (daily quota = 5 trades/day)
Brain 5: Premium Exit Engine         (V19 trail/target/runaway — in monitor_open_positions)
Brain 6: Premium Discount Tracker    (V18 IV percentile — loose 85/90)
Brain 7: Session Commander            (session threshold + force hunt)
```

---

## 11. KEY FILES (FINAL)

```
data/loader.py                          ← instrument master + data fetch + token resolution
universe/fno_universe.py                ← symbol definitions + scan symbols
universe/stock_filter.py                ← stock liquidity pipeline (Bhavcopy filter)
universe/selector.py                    ← dynamic universe scoring (backtest helper)
backtest/run_tiger_brain_backtest.py    ← entry functions + V19 exit engine + IV filter
backtest/tiger_premium_brain.py         ← Brain 6 IV percentile tracker
backtest/tiger_session_brain.py         ← Brain 7 session commander + force hunt
automation/tiger_live.py                ← LIVE runner + scheduler + order placement
automation/live_scanner.py              ← real-time scanner (7 brains on latest bar)
automation/scheduler.py                 ← active market detection (NSE vs MCX)
config/thresholds.py                   ← all thresholds + session config
```

---

## 12. DEPLOYMENT (FINAL)

```
EC2:        3.108.53.100
Repo:       ~/tiger-brain-v6  (branch: main, HEAD: 400b8d7)
Service:    tiger-brain.service  (systemd, active)
Log:        ~/tiger_v19.log
Balance:    ₹30,039.30
Instrument: 42,029 tokens (cached 2026-09-09, 10.9 MB)
```

---

```
 ╔═══════════════════════════════════════════════════════════════╗
 ║  🔒  TIGER V19 — SYSTEM LOCKED v1.0 — 09-SEP-2026 20:00    ║
 ║                                                               ║
 ║  NO CHANGES ALLOWED WITHOUT OWNER PERMISSION.                 ║
 ║  This file is the SINGLE SOURCE OF TRUTH for Tiger's          ║
 ║  frozen architecture. Any code change MUST be reflected       ║
 ║  here and re-approved by the OWNER.                           ║
 ╚═══════════════════════════════════════════════════════════════╝
```
