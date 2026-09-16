# Tiger Brain V6.6 — Repository Memory

## Project
Algorithmic trading platform for pure intraday Call/Put options buying on NSE/MCX.
Core strategy: pure Supply/Demand zones (no VWAP/RS/EMA/Black-Scholes).

## Momentum Hunter (Sep 2026)
New module `subbrains/momentum_hunter.py` — Tiger's 3rd hunting layer:
- ORB Breakout (9:15-9:30 range breakout with volume)
- Momentum Spike (3-bar acceleration + volume explosion)
- VWAP Reclaim (institutional re-entry signal)
- Options Math Gate: IV percentile < 65% + delta 0.40-0.75 (MANDATORY)
Integrated in scan_live_signals as Priority 1.5 (after 7-Brain, before scalper).
Scalper also has options math gate (Gate 9) — no more overpriced premium entries.
Morning golden window scalper activation reduced to 10 min (was 30).

## V16 Architecture (Current — Tiger Brain ARMY)
- **Fund Announcement Brain** (Brain 0): `backtest/tiger_fund_brain.py`
  - Reads account capital (₹10k to ₹10cr), classifies tier (MICRO/SMALL/MID/LARGE/WHALE)
  - Pre-market: announces max trades, risk per trade, capital allocation
  - Growth strategy: MICRO=30% monthly aggressive, WHALE=8% preservation
  - Capital-based sizing (NOT lot-based): `size_trade_with_fund_brain()`
- **Delivery Mode**: 2-3 day rocket holding for ultra-high-conviction setups
  - `DELIVERY_ROCKET_MIN_SCORE = 90` — only score 90+ trades get delivery
  - Delivery trades skip square-off, hold up to 3 days, wider stops (30%)
- **Full Market Scanning**: `--full-scan` flag activates 150+ F&O universe
- **3-month (90-day) backtest window** (was 60-day)
- Backtest CLI: `python3 -m backtest.run_tiger_brain_backtest --capital 100000 [--full-scan]`

## Locked V6.6 Strategy Thresholds (DO NOT CHANGE)
- `delta_spike_confirms`: 1.8x spike ratio (volume_delta proxy)
- `zone_explosive_quality`: min_expansion_atr=1.0, expansion_lookback=5
- `detect_zones_explosive`: impulse_min_pct=0.4, cluster_min=3
- `find_sniper_entry`: spike_mult=1.8 fallback
- Brain 3 spread gate: MAX_SPREAD_PCT=0.5%
- Exit rules: +30% trail, 55% lock, +15% final, ₹2000 hard stop, square-off
- Expiry ITM selection: unchanged

## Known Data Limitations (yfinance)
- **Index tickers (^NSEI/^NSEBANK) report ZERO volume** in both 15m and 1m data.
  - `zone_explosive_quality`: vol_surge gate skipped when vol_base=0 (index-volume fix).
  - `volume_delta`: price-pressure fallback (direction * body-fraction) when volume=0.
    Scale-invariant — preserves the 1.8x spike ratio exactly.
  - `one_min_exhaustion`: body-fraction proxy (≥0.5) when avg_vol=0.
- **1m data capped at 7 days**; 15m covers ~60 days. Zone formation scans the full
  15m history (lookback=zone_idx) so unbroken zones from the broader history remain
  visible during the 7-day 1m execution window.
- **Commodity futures (CL=F/GC=F/NG=F) trade in America/New_York tz** — converted
  to IST for MCX entry-window checks.
- **Commodities are tier-2/3** → spread model (0.55-0.85%) exceeds the 0.5% gate →
  0 commodity trades. This is correct behavior (Brain 3 spread safety gate).

## Universe
- 35 NSE F&O stocks + NIFTY/BANKNIFTY + 3 MCX commodities = 40 symbols.
- All stocks MUST have a LIQUIDITY_TIER entry (tier-3 default blocks spread gate).
- V6.6.1 expanded to 48 stocks WITHOUT tier mappings → all blocked. Reverted.

## Key Commands
- Tests: `python3 -m pytest tests/ -q` (329 tests, ~9s)
- Full backtest: `python3 -m backtest.intraday_backtest`
- Tiger V16 backtest: `python3 -m backtest.run_tiger_brain_backtest --capital 150000 [--full-scan]`
- Original V6.6 commit: `1020b30`. Current: `ef005ae` (V6.6 bugfix).

## HDFCBANK Baseline (must be preserved)
+6.92% return, 3 wins, 100% win rate, +73%/+62%/+58% premium captures.
All 3 HDFCBANK explosive captures preserved in the bugfix commit.


## MASTER BRAIN OVERRIDE (commit 9087c74) — 5-module SMC + Greeks engine
- Module 1: SMC Zone Calculator — detect_fvg() + detect_absorption_pivots() in
  pipeline/intraday_strategies.py. Zone enrichment via _enrich_zones_with_smc().
  FVG confluence + absorption pivot boost zone score. Strict 0.3% entry gate tolerance
  in zone_touched_on_1m() — break-through = instant reject.
- Module 2: Options Greeks Layer — find_affordable_option() in data/loader.py now
  fetches live IV + Delta from Angel One optionGreek API. Delta >= 0.35 gate rejects
  dead zero-delta junk. ATM IV crush risk detection (>50% = warning). Max 15 OTM steps,
  balance-aware. Falls back to moneyness delta estimate if greeks unavailable.
- Module 3: 1m Rocket Filter — _verify_1m_velocity() in tiger_live.py now has 4 gates:
  body>=65%, vol>=1.4x, direction match, RSI>=60 (CE) / <=40 (PE).
- Module 4: Anti-loop State Tracking — state_lock.json disk-backed daily_trade_count
  ledger in tiger_live.py. Hard cap 6 trades/day — permanent lock at 6 until session
  reset. Restart-safe. 2-hour directional cooldown per symbol (existing, verified).
- Module 5: Fixed 1:2 RR + Risk Ceiling — Scalper exit in monitor_open_positions()
  now uses dynamic 1:2 RR target = 2 x effective_stop_pct. SL cap 7% or Rs 1500 max.
  +3% breakeven lock. Catastrophic -12% instant exit.
- Deploy: EC2 (3.108.53.100), commit 9087c74, service active, PID running.
  Fresh log at 16:51 confirms optionGreek API calls (Module 2 live).
- Tests: 543 passed, 0 failed.
