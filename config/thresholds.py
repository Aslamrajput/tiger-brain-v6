"""
Tiger Brain V6+V7 — Central Threshold Configuration
=====================================================
This file holds all the concrete numbers from Master Blueprint
Part B (Sections 17-29) in one place.
"""

REGIME = {
    "STRONG_TREND_ADX_MIN": 25,
    "WEAK_TREND_ADX_MIN": 20,
    "WEAK_TREND_ADX_MAX": 25,
    "RANGE_ADX_MAX": 20,
    "RANGE_ATR_BAND_MULTIPLIER": 1.5,
    "RANGE_LOOKBACK_CANDLES": 20,
    "COMPRESSION_BB_WIDTH_DROP_PCT": 40,
    "EXPANSION_BB_WIDTH_RISE_PCT": 30,
    "HIGH_VOL_VIX_MIN": 18,
    "HIGH_VOL_ATR_RISE_PCT": 50,
    "LOW_VOL_VIX_MAX": 12,
    "LOW_VOL_ATR_DROP_PCT": 30,
    "VOL_SHOCK_VIX_DAILY_JUMP_PCT": 15,
    "LIQUIDITY_STRESS_SPREAD_MULTIPLIER": 3,
    "LIQUIDITY_STRESS_DEPTH_DROP_PCT": 50,
}

IV = {
    "PERCENTILE_LOW_MAX": 25,
    "PERCENTILE_HIGH_MIN": 75,
    "PERCENTILE_LOOKBACK_DAYS": 60,
    "SHOCK_RISE_PCT_1HR": 15,
    "CRUSH_DROP_PCT_1DAY": 20,
    "SKEW_ANOMALY_PCT": 20,
    "SAFE_BUYING_ZONE_MIN": 20,
    "SAFE_BUYING_ZONE_MAX": 60,
    "EVENT_DANGER_HOURS": 24,
}

OI = {
    "SIGNIFICANT_CHANGE_PCT": 20,
    "HEAVY_ZONE_MULTIPLIER": 2,
    "BUILDUP_CONFIRM_PCT": 10,
    "UNUSUAL_OTM_SPIKE_MULTIPLIER": 5,
}

VOLUME = {
    "SPIKE_MULTIPLIER": 2,
    "WEAK_VOLUME_PCT_OF_AVG": 50,
    "OPENING_RANGE_MINUTES": 0,
    "OPENING_RANGE_ACTIVE_MULTIPLIER": 1.5,
}

VWAP = {
    "ROLLING_CANDLES": 20,
    "MEANINGFUL_DISTANCE_INDEX_PCT": 0.3,
    "MEANINGFUL_DISTANCE_STOCK_PCT": 0.5,
    "EXTREME_DISTANCE_PCT": 1.5,
    "RECLAIM_CONFIRM_CANDLES": 2,
    "SLOPE_LOOKBACK_CANDLES": 10,
}

PCR = {
    "EXTREME_BEARISH_MIN": 1.7,
    "EXTREME_BULLISH_MAX": 0.6,
    "NORMAL_RANGE_MIN": 0.8,
    "NORMAL_RANGE_MAX": 1.3,
    "TREND_SIGNAL_MOVE_PCT": 15,
}

SUBBRAIN_TREND_FOLLOW = {
    "MIN_ADX": REGIME["STRONG_TREND_ADX_MIN"],
    "MIN_VWAP_DISTANCE_PCT": VWAP["MEANINGFUL_DISTANCE_INDEX_PCT"],
    "MIN_VOLUME_MULTIPLIER": 1.5,
    "MIN_OI_BUILDUP_PCT": OI["BUILDUP_CONFIRM_PCT"],
    "CONFIDENCE_WEIGHTS": {
        "adx": 0.25,
        "supertrend": 0.20,
        "vwap": 0.20,
        "volume": 0.15,
        "oi": 0.20,
    },
}

SUBBRAIN_MEAN_REVERSION = {
    "MAX_ADX": REGIME["RANGE_ADX_MAX"],
    "RSI_OVERSOLD": 30,
    "RSI_OVERBOUGHT": 70,
    "ZONE_PROXIMITY_PCT": 0.2,
    "MIN_VWAP_EXTREME_DISTANCE_PCT": VWAP["EXTREME_DISTANCE_PCT"],
    "CONFIDENCE_WEIGHTS": {
        "rsi_extreme": 0.30,
        "zone_freshness": 0.30,
        "vwap_distance": 0.25,
        "volume": 0.15,
    },
}

SUBBRAIN_BREAKOUT = {
    "COMPRESSION_BB_DROP_PCT": REGIME["COMPRESSION_BB_WIDTH_DROP_PCT"],
    "MIN_VOLUME_MULTIPLIER": 2,
    "MIN_OI_NEW_BUILDUP_PCT": 15,
    "CONFIDENCE_WEIGHTS": {
        "compression_to_expansion": 0.25,
        "volume_spike": 0.30,
        "oi_confirm": 0.25,
        "range_boundary_clarity": 0.20,
    },
}

SUBBRAIN_VOL_ARB = {
    "BUY_FAVORABLE_IV_PERCENTILE_MAX": 30,
    "BUY_FAVORABLE_EVENT_HOURS": 48,
    "NO_TRADE_IV_PERCENTILE_MIN": 75,
    "SKEW_CAUTION_PCT": IV["SKEW_ANOMALY_PCT"],
    "HARD_VETO_EVENT_HOURS": IV["EVENT_DANGER_HOURS"],
    "HARD_VETO_IV_PERCENTILE_MIN": 60,
}

SUBBRAIN_RANGE_SCALP = {
    "MIN_RANGE_STABLE_MINUTES": 30,
    "BOUNDARY_PROXIMITY_PCT": 0.15,
    "TARGET_RANGE_WIDTH_PCT_MIN": 40,
    "TARGET_RANGE_WIDTH_PCT_MAX": 50,
    "STOPLOSS_PCT_MIN": 0.1,
    "STOPLOSS_PCT_MAX": 0.2,
}

META_BRAIN_WEIGHTS = {
    "STRONG_TREND": {
        "trend_follow": 0.65, "mean_reversion": 0.05, "breakout": 0.15,
        "vol_arb": 0.10, "range_scalp": 0.05,
    },
    "WEAK_TREND": {
        "trend_follow": 0.40, "mean_reversion": 0.20, "breakout": 0.20,
        "vol_arb": 0.10, "range_scalp": 0.10,
    },
    "RANGE": {
        "trend_follow": 0.10, "mean_reversion": 0.35, "breakout": 0.05,
        "vol_arb": 0.15, "range_scalp": 0.35,
    },
    "COMPRESSION": {
        "trend_follow": 0.10, "mean_reversion": 0.10, "breakout": 0.55,
        "vol_arb": 0.20, "range_scalp": 0.05,
    },
    "HIGH_VOL": {
        "trend_follow": 0.15, "mean_reversion": 0.10, "breakout": 0.10,
        "vol_arb": 0.55, "range_scalp": 0.10,
    },
    "EVENT_NEARBY": {
        "trend_follow": 0.05, "mean_reversion": 0.05, "breakout": 0.05,
        "vol_arb": 0.80, "range_scalp": 0.05,
    },
}

DECISION_SCORE_THRESHOLD = 72

# The Meta-Brain score is the weighted average of all weights. If a sub-brain
# is silent due to missing data (e.g. Vol-Arb without an IV feed), its weight
# drags the score down and the threshold can structurally never be crossed.
# Therefore the score is normalised only by the weights of brains that have
# data and whose regime-fit is above the minimum.
META_BRAIN = {
    "NORMALISE_BY_PARTICIPATING_WEIGHT": True,
    "MIN_REGIME_FIT_TO_PARTICIPATE": 20,
}

PIPELINE = {
    "STAGE1_MIN_CONFIDENCE": 50,
    "STAGE2_MIN_CONFIDENCE": 50,
    "STAGE2_MAX_DIVERGENCE_FROM_STAGE1": 20,
    "STAGE3_MIN_CONFIDENCE": 72,
    "STAGE4_TIE_BREAK_SCORE_DIFF": 5,
}

RISK = {
    "MAX_RISK_PER_TRADE_PCT": 2.0,
    "DAILY_MAX_LOSS_PCT": 5.5,
    "MAX_CONSECUTIVE_LOSSES": 3,
    "CONSECUTIVE_LOSS_PAUSE_MINUTES": 35,
    "MAX_POSITION_PER_SYMBOL_PCT": 25,
    "HIGH_CONFIDENCE_STAGE3_SCORE_MIN": 85,
    "HIGH_CONFIDENCE_SIZE_MULTIPLIER": 1.5,
    "MEDIUM_CONFIDENCE_STAGE3_SCORE_MIN": 65,
    "THETA_DECAY_0DTE_SIZE_MULTIPLIER": 0.5,
    "DEPLOYABLE_POOL_PCT": 80,
    "HARD_RESERVE_PCT": 20,
}

REPLAY = {
    "MIN_PATTERN_OCCURRENCE_DAYS": 10,
    "CHALLENGER_PROMOTION_MIN_WINRATE_EDGE_PCT": 10,
    "WEIGHT_ADJUSTMENT_CAP_PCT_PER_WEEK": 10,
    "WEIGHT_ADJUSTMENT_CAP_MATURE_PCT_PER_WEEK": 5,
}

UNIVERSE = {
    "TOP_N_SYMBOLS": 5,
    # Live NSE scan: 4 index + 19 stocks = 23 total symbols.
    # 23 symbols × 1s delay = 23s per refresh — well within Angel rate limits.
    # WebSocket streams live ticks for all 23 (zero rate limits on LTP/1m).
    "TOP_N_LIQUID_STOCKS": 19,
    # Live scan cap: REST candle fetch ke liye max NSE symbols (indices
    # pehle, phir top liquid stocks). 27-symbol burst startup pe Angel ka
    # rate limit tod deta tha — 15 rakho (4 index + 11 stocks).
    "MAX_CANDLES_PER_SCAN": 15,
    "RESCAN_INTERVAL_MINUTES": 1,
    "SCORE_WEIGHTS": {
        "volume_rank": 0.35,
        "oi_velocity": 0.25,
        "volatility_rank": 0.20,
        "liquidity_rank": 0.20,
    },
    "MIN_LIQUIDITY_SPREAD_PCT_OF_PREMIUM": 1.0,
}

STAGE0 = {
    "MIN_SIGNALS_REQUIRED": 3,
    "TOTAL_SIGNALS_TRACKED": 5,
}

AUTOMATION = {
    "TRADING_DAYS": ["MON", "TUE", "WED", "THU", "FRI"],
    "WATCH_ONLY_DAYS": ["SAT"],
    "OFF_DAYS": ["SUN"],
    "PRE_MARKET_WAKE_TIME": "09:00",
    "MARKET_OPEN_TIME": "09:15",
    "OPENING_RANGE_WAIT_MINUTES": VOLUME["OPENING_RANGE_MINUTES"],
    "MARKET_CLOSE_TIME": "15:30",   # NSE market close (NSE session ends)
    # MCX (commodity) session — 09:00 to 23:15.
    # MCX opens at 09:00 AM (before NSE). Tiger scans NSE + MCX
    # SIMULTANEOUSLY during 09:15-15:15 so commodity opportunities
    # (morning crude/gold spikes) are never missed.
    "MCX_OPEN_TIME": "09:00",          # MCX opens at 9 AM (simultaneous with NSE)
    "MCX_CLOSE_TIME": "23:15",         # MCX trading ends (square-off at 23:15)
    "NIGHTLY_REPLAY_TIME": "00:00",
    # Intraday entry cutoff — after 3:00 PM NO new NSE/equity intraday
    # orders, only profit booking (exits). MCX session starts at 15:30.
    "INTRADAY_ENTRY_CUTOFF_TIME": "15:00",
    # Delivery snapshot time — at 3:00 PM Tiger decides next-day direction
    # and places delivery (overnight) orders.
    "DELIVERY_SNAPSHOT_TIME": "15:00",
    # NSE square-off — 15:15 (15 min before NSE close 15:30)
    "NSE_SQUARE_OFF_TIME": "15:15",
    # MCX square-off — 23:15 (15 min before MCX close 23:30)
    "MCX_SQUARE_OFF_TIME": "23:15",
    # === STALE DATA GUARD (Bug 1 fix) ===
    # A signal is only valid for this many seconds after it is generated.
    # If _place_live_orders picks up a signal older than this, it is rejected
    # — no executing stale-buffer signals when the user changes score mid-scan.
    "SIGNAL_MAX_AGE_SEC": 90,
    # The latest 1m bar for a symbol must be newer than this many seconds for
    # an entry to be allowed. If the WS feed stalled, Tiger skips the symbol
    # rather than trading on stale ticks.
    "DATA_MAX_AGE_SEC": 120,
}

# ============================================================
# 5-BRAIN STRUCTURAL ARCHITECTURE (V6.1)
# Brain 1: Market Scanner & Regime Detection
# Brain 2: Setup Trigger & Entry Engine (SMC)
# Brain 3: Option Chain & Greeks/OI Velocity Selector
# Brain 4: Capital Allocation & Trade Counter Guard
# Brain 5: Risk Guard, Gamma Tracking & Execution Exit
# ============================================================

BRAIN1 = {
    # --- Momentum & Noise Filter (Brain 1 must reject chop) ---
    # A candle body must be at least this fraction of its total range to
    # count as directional (small body = wick-heavy chop).
    "MIN_BODY_TO_RANGE_RATIO": 0.5,
    # Volume velocity: current bar volume must exceed the rolling average
    # by this multiplier to signal genuine momentum.
    "VOLUME_VELOCITY_MULTIPLIER": 1.8,
    # RS divergence: symbol's N-bar return must diverge from the benchmark
    # (index) return by at least this many percentage points.
    "RS_DIVERGENCE_MIN_PCT": 1.0,
    # RS lookback window (bars) for divergence measurement.
    "RS_LOOKBACK_BARS": 10,
    # Volume average lookback (bars).
    "VOLUME_LOOKBACK_BARS": 20,
    # Choppy-range filter: if the last N bars' combined range is less than
    # this multiple of ATR, the market is dead chop — skip.
    "CHOP_LOOKBACK_BARS": 5,
    "CHOP_RANGE_ATR_MULTIPLIER": 1.2,
}

BRAIN2 = {
    # --- SMC Order Block detection ---
    # Order block = last opposite-direction candle before a displacement
    # move that breaks market structure. Displacement must cover at least
    # this many ATRs to be structural, not noise.
    "ORDER_BLOCK_MIN_DISPLACEMENT_ATR": 1.5,
    # Max bars to look back for an order block from the current bar.
    "ORDER_BLOCK_LOOKBACK_BARS": 30,
    # --- Liquidity Sweep detection ---
    # A sweep wick must pierce a swing level by at least this fraction of
    # ATR, and the close must come back inside the level (rejection).
    "LIQUIDITY_SWEEP_MIN_PIERCE_ATR": 0.25,
    "SWING_LOOKBACK_BARS": 20,
    # --- RS score (0-100) ---
    # Relative strength vs benchmark, scaled to a 0-100 score for the
    # setup confidence. RS >= this threshold counts as significant.
    "RS_SCORE_SIGNIFICANT_MIN": 60,
    # Minimum combined setup score (SMC + RS) to emit a setup to Brain 3.
    "MIN_SETUP_SCORE": 55,
    # Confidence weights within the setup score.
    "SETUP_SCORE_WEIGHTS": {
        "order_block": 0.35,
        "liquidity_sweep": 0.30,
        "rs_score": 0.35,
    },
}

BRAIN3 = {
    # Max acceptable bid-ask spread as % of option premium.
    "MAX_SPREAD_PCT_OF_PREMIUM": 2.0,
    # Minimum OI on the contract for liquidity.
    "MIN_OPEN_INTEREST": 500,
    # OI velocity (percent change in OI over lookback) above which a
    # strike is considered "hot" (prefer these strikes).
    "OI_VELOCITY_HOT_PCT": 15,
    # Preferred option moneyness: buy slightly ITM to ATM options.
    "MIN_DELTA": 0.45,
    "MAX_DELTA": 0.75,
    # Avoid options too close to expiry (gamma/theta burn) — minimum days.
    "MIN_DAYS_TO_EXPIRY": 1,
    # Max days to expiry we buy (avoid far-month illiquidity).
    "MAX_DAYS_TO_EXPIRY": 14,
}

BRAIN4 = {
    # Global daily trade counter limit across ALL markets.
    "MAX_TRADES_PER_DAY_GLOBAL": 20,
    "MAX_TRADES_PER_DAY_GLOBAL_MIN": 10,
    "MAX_TRADES_PER_DAY_GLOBAL_MAX": 20,
    # Commodity-market-specific daily trade counter limit.
    "MAX_TRADES_PER_DAY_COMMODITY": 10,
    "MAX_TRADES_PER_DAY_COMMODITY_MIN": 10,
    "MAX_TRADES_PER_DAY_COMMODITY_MAX": 20,
    # === CAPITAL FIX (Sep 2026) — quality over quantity ===
    # Account ₹29,453. Split into 2 slots of ₹14,500 each so every trade
    # has enough for one lot (₹12,800+). Stops the 99% reject rate caused
    # by conviction-multiplier allocating < one_lot_cost.
    "MAX_OPEN_POSITIONS": 2,
    "ALLOCATED_PER_TRADE": 14500.0,
    # === SEQUENTIAL MARKET — 100% capital to active market ===
    # User mandate: "50/50 nahi chaiye — ak time pe ak market"
    # No split — whoever is active (NSE morning / MCX evening) gets full wallet.
    "MARKET_CAPITAL_SPLIT_PCT": 100.0,
    # === DAILY PROFIT TARGET — ₹8,000 for Ujjivan Foundation ===
    # User mandate: "Tiger ka dimag me gussa do — daily ₹8,000 profit target.
    # ₹8,000+ profit → alert for withdrawal to Ujjivan (gareeb/aunty/bacche).
    # More than ₹8,000 → excess goes to capital (reinvest, no withdrawal of excess).
    # Less than ₹8,000 → no withdrawal, capital safe."
    "DAILY_PROFIT_TARGET": 8000.0,
    # Dynamic position sizing: full Angel One capital available for trading.
    "MAX_CAPITAL_PER_TRADE_PCT": 100.0,
    # Full capital deployable across trades (Angel One balance = 100% trading money).
    "MAX_TOTAL_EXPOSURE_PCT": 100.0,
    # Confidence-based sizing — high score = more capital allocated.
    # Tiger uses 80%+ capital only when fully certain.
    "CONFIDENCE_TIER_ROCKET_MIN": 90,   # 90+ score → 100% allocatable
    "CONFIDENCE_TIER_STRONG_MIN": 80,   # 80-89 score → 80% allocatable (fully sure)
    "CONFIDENCE_TIER_DECENT_MIN": 75,   # 75-79 score → 60% allocatable
    "CONFIDENCE_ROCKET_PCT": 100.0,
    "CONFIDENCE_STRONG_PCT": 80.0,
    "CONFIDENCE_DECENT_PCT": 60.0,
    # If broker capital fetch fails, fall back to this (None = block trade).
    "FALLBACK_CAPITAL_ON_BROKER_FAIL": None,
}

BRAIN5 = {
    # Hard stop-loss on premium (% below entry).
    "STOP_LOSS_PCT": 25.0,
    # Profit target on premium (% above entry).
    "TARGET_PCT": 50.0,
    # Trailing stop activation (premium % gain before trail engages).
    "TRAIL_ACTIVATION_PCT": 30.0,
    # Trailing stop give-back (premium % from peak once trailing).
    "TRAIL_GIVEBACK_PCT": 15.0,
    # Time-based exit: square off intraday positions this many minutes
    # before market close, regardless of P&L.
    "SQUARE_OFF_MINUTES_BEFORE_CLOSE": 15,
    # Gamma guard: if gamma (as % of premium per % move in underlying)
    # exceeds this, position is too sensitive near expiry — tighten stop.
    "GAMMA_RISK_THRESHOLD_PCT": 2.0,
    # Theta guard: days-to-expiry at which gamma/theta exit rules engage.
    "GAMMA_RISK_DAYS_TO_EXPIRY": 2,
}

# Market-category mapping for the trade counter guard.
MARKET_CATEGORIES = {
    "MCX": "commodity",
    "NCDEX": "commodity",
    "NSE": "equity",
    "BSE": "equity",
    "NFO": "equity",
    "BFO": "equity",
    "CDS": "currency",
}

import os
DRY_RUN = os.getenv("TIGER_BRAIN_DRY_RUN", "false").lower() == "true"

# ============================================================
# TIGER FALLBACK SCALPER MODE
# "Never go home empty-handed" — Tiger's last-resort micro-momentum
# ============================================================
# Activates when Tiger has 0 trades near session close, or has been
# idle 2+ hours. Relaxes ALL gates — no zone touch required, just a
# momentum candle with volume. Fast in, fast out.
SCALPER = {
    # === ROCKET FILTER (dynamic — high-frequency scalping, anti-freeze) ===
    # Volume is highly sensitive: 1.3x-1.5x rolling average captures the
    # initial impulse wave without freezing. Body 65% allows bottom sweeps
    # and sharp wick turnarounds on 1m timeframe. Score>=75 keeps quality.
    "MIN_SCORE": 65,              # high-conviction scalps only (default fallback)
    # === EXCHANGE ISOLATION — NSE and MCX have different microstructure.
    # A single global score caused NSE/MCX clashes (MCX commodities need a
    # lower bar than NSE index/stock options). Each exchange now has its own
    # scalper score threshold. Use get_scalper_min_score(segment).
    "MIN_SCORE_NSE": 65,          # NSE equity/index — high-conviction scalps
    "MIN_SCORE_MCX": 60,          # MCX commodity — lower bar (matches session brain)
    "MIN_BODY_PCT": 30,           # 1m body ≥ 30% of range (relaxed for aggressive hunting)
    "MIN_VOLUME_SURGE": 1.4,      # dynamic — instantaneous vol 1.3x-1.5x of trailing avg
    "MIN_RSI_BUY": 60,            # CE: RSI >= 60 (bullish momentum)
    "MAX_RSI_SELL": 40,           # PE: RSI <= 40 (bearish momentum)
    "REQUIRE_SUPERTREND": False,  # REMOVED per user: supertrend blocks good trades
    # === EXIT RULES (momentum-aware — give trades room to breathe) ===
    "TARGET_PCT": 15.0,           # +15% = exit (let momentum run, don't exit too early)
    "MAX_STOP_PCT": 7.0,          # -7% = hard stop (premium-aware: gives pullback room)
    "MAX_STOP_RUPEES": 1500,      # -₹1500 = absolute cap (protects capital, but gives room)
    "MIN_HOLD_SECONDS": 180,      # 3-min minimum hold before SL triggers (momentum needs time)
    "CATASTROPHIC_STOP_PCT": 12.0, # -12% = instant exit even during min hold (black swan protection)
    "MAX_TRADES_PER_DAY": 20,     # aggressive hunting — 10-20 quality scalps per day
    # === VOLUME PROFILE (institutional intelligence — informational only) ===
    "VP_LOOKBACK": 50,            # bars for volume histogram
    "VP_BINS": 20,                # histogram resolution
    "VP_VALUE_AREA_PCT": 70.0,    # standard 70% Value Area
    # === RE-ENTRY (Tiger doesn't give up on a fish) ===
    "REENTRY_COOLDOWN_MINUTES": 2,  # after SL, wait 2 min before re-entering SAME symbol+direction
    "REENTRY_MAX_PER_SYMBOL": 3,    # max 3 re-entries per symbol per day (don't chase losses)
    # === ACTIVATION ===
    "ACTIVATION_IDLE_MINUTES": 3,    # 3 min idle → activate (aggressive hunting)
    "ACTIVATION_ZERO_TRADE_TIME": {  # OR: 0 trades at these times
        "NSE": "09:30",
        "MCX": "15:45",
    },
}


def get_scalper_min_score(segment: str) -> float:
    """Exchange-isolated scalper score threshold (Bug 3 fix).

    NSE and MCX no longer share one global MIN_SCORE. MCX commodities get
    a lower bar (matches the session-brain thresholds); NSE stays strict.

    Args:
        segment: "nse", "mcx", "equity", or "commodity".
    """
    seg = (segment or "").lower()
    if seg in ("mcx", "commodity"):
        return float(SCALPER.get("MIN_SCORE_MCX", SCALPER["MIN_SCORE"]))
    return float(SCALPER.get("MIN_SCORE_NSE", SCALPER["MIN_SCORE"]))


# === RISK MANAGER — consecutive loss protection ===
# Prevents death spirals: 4 losses → pause 15min, 5 losses → stop for day.
RISK_MANAGER = {
    "MAX_CONSECUTIVE_LOSSES": 5,       # 5 losses in a row → STOP for the day
    "PAUSE_AFTER_LOSSES": 4,           # 4 losses → pause 15 min
    "PAUSE_DURATION_MINUTES": 15,      # pause length (shorter — more aggressive)
    "MAX_TRADES_PER_DAY": 20,          # hard daily cap (all strategies combined)
}

# === ML ENGINE — LightGBM win-probability gate ===
# Trained nightly via jobs/retrain_model.py using TimeSeriesSplit (no leakage).
# Inference gate: if win_probability < ML_MIN_WIN_PROB, reject trade instantly.
ML_ENGINE = {
    "MODEL_PATH": "models/tiger_lgbm.joblib",   # nightly-retrained artifact
    "MIN_WIN_PROB": 0.70,                        # hard reject below this (legacy)
    "SNIPER_MIN_WIN_PROB": 0.80,                 # sniper-only: high conviction
    "FEATURE_COLUMNS": [
        "zone_strength", "volume_velocity", "option_chain_pcr", "live_iv_skew",
        "setup_score", "body_pct", "vol_surge_ratio", "rsi", "brain_alignment",
        "is_scalper", "is_momentum_hunter",
        "sensex_trend", "vix_level",
        # --- sniper features (14-16) ---
        "sniper_zone_strength", "fvg_size", "commodity_volatility",
    ],
    "TRAINING": {
        "N_SPLITS": 3,           # TimeSeriesSplit folds (3 for small data)
        "PURGE_BARS": 2,         # purge labels within N bars of train/test boundary
        "MIN_SAMPLES": 10,       # minimum samples to train (lowered — ML starts sooner)
        "VALIDATED_ACC_MIN": 0.50,  # accept model if OOS accuracy >= 50% (better than random)
        "SNIPER_ONLY": False,    # learn from ALL trades (not just sniper)
        "SNIPER_MIN_PNL_PCT": 30.0,
        "SNIPER_EXIT_REASON": "SNIPER_TRAILING_EXIT",
    },
    "ROCKET_SIZING": {
        "ENABLED": True,
        "HIGH_CONFIDENCE": 0.75,   # win_prob >= 0.75 → qty × 1.5 (rocket)
        "MID_CONFIDENCE": 0.50,    # win_prob >= 0.50 → qty × 1.0 (normal)
        "LOW_FACTOR": 0.5,         # win_prob < 0.50 → qty × 0.5 (cautious)
        "ROCKET_FACTOR": 1.5,      # rocket multiplier
        "NORMAL_FACTOR": 1.0,      # normal multiplier
    },
    # --- ADVANCED TRAINING MODULES (backtest validation) ---
    "VOLATILITY_FILTER": {
        "ENABLED": True,
        "MIN_ATR_PCT": 0.3,       # skip if ATR < 0.3% of price (dead market)
        "MIN_IV_RANK": 30,        # skip if IV rank < 30 (low vol = theta kill)
        "MAX_IV_RANK": 95,        # skip if IV rank > 95 (too expensive premium)
    },
    "SLIPPAGE_MODEL": {
        "ENABLED": True,
        "SLIPPAGE_PCT": 0.75,     # 0.75% slippage per side (entry + exit)
        "STT_PCT": 0.05,          # STT on options selling side
        "EXCHANGE_FEE_PCT": 0.05, # NSE/BSE transaction charges
        "GST_PCT": 18.0,          # GST on brokerage + exchange fees
        "BROKERAGE_FLAT": 20.0,   # flat brokerage per order (Angel)
    },
    "WALK_FORWARD": {
        "TRAIN_WINDOW_DAYS": 60,  # train on 60 days of backtest history
        "TEST_WINDOW_DAYS": 15,   # test on next 15 days (out-of-sample)
        "STEP_DAYS": 15,          # roll forward 15 days each iteration
        "MIN_TRADES_PER_WINDOW": 5,  # skip window if too few trades
    },
}

# ============================================================
# TIGER SNIPER ADVANCED V2 — pure SMC sniper engine.
# Pure SMC + Supply/Demand. No fixed targets. Trailing momentum ride.
# Runs on BOTH MCX commodities AND NSE index/stock options.
# ============================================================
SNIPER = {
    "MAX_TRADES_PER_DAY": 3,        # sniper doesn't overtrade
    "MIN_ZONE_STRENGTH": 80.0,      # backtest: ZS 80 = 67% win, ZS 70 = 31% win (TRAP)
    "MIN_WIN_PROB": 0.80,           # ML conviction gate (high only)
    # === SEQUENTIAL TIMING: NSE morning → MCX evening (user mandate) ===
    # NSE: 9:15 AM - 3:00 PM (50% capital, morning session)
    # MCX: 3:30 PM - 11:30 PM (50% capital, evening session — starts AFTER NSE)
    # Never overlap — clean sequential, each market gets full focus.
    "SESSION_START": "15:30",       # MCX entry starts AFTER NSE closes (was 10:30)
    "SESSION_END": "23:30",
    "NSE_SESSION_START": "09:15",
    "NSE_SESSION_END": "15:00",     # entry cutoff (square-off 15:15 separate)
    # === CHEAP OPTIONS ONLY (user: "sasta sa options buying kar leta") ===
    # Max ₹50 per option premium. Even if ATM is affordable, if premium > this,
    # Tiger walks OTM to find cheap ₹5-50 options. No more ₹125 ATM buys.
    "MAX_OPTION_PREMIUM": 50.0,
    # === VOLUME GATE (user: "jha buying selling ho rhi hai volumes hai wha jaye") ===
    # Minimum option trade volume — reject dead options with no buying/selling.
    "MIN_OPTION_VOLUME": 50,
    # Entry: OB retest + CHOCH + wick rejection on 1m
    "OB_BUFFER_PCT": 0.35,          # SL = OB edge +/- 0.35% buffer
    "CHOCH_LOOKBACK": 20,           # 1m structure-shift lookback
    "WICK_REJECTION_MIN": 0.50,     # min wick/range ratio for rejection candle
    # Scanner: require 3+ agreeing SMC components for a true rocket setup
    "MIN_CONFLUENCE_COMPONENTS": 2,     # MCX — 2+ SMC components (was 3, too strict)
    "NSE_MIN_CONFLUENCE_COMPONENTS": 2, # NSE — 2+ SMC components
    # Exit: NO FIXED TARGET. Pure momentum ride — let the rocket run.
    #   1. Trail arms at +5% (was 10 — too high, surrendered small gains).
    #   2. Once active, trail = 50% of peak (lock half, ride the rest).
    #   3. OB hard stop survives the pre-rocket pullback (entry*0.88 = -12%).
    #   4. 5m opposite BOS = structure reversal exit — but ONLY checked AFTER
    #      the trail arms (before that, first pullbacks must not kill the rocket).
    "ATR_PERIOD": 14,
    "TRAIL_ACTIVATE_PCT": 3.0,      # trail arms at +3% (was 5/10 — most wins are +5-6%, arm early)
    "TRAIL_LOCK_PCT_OF_PEAK": 60.0, # lock 60% of peak once armed (tighter lock)
    "OB_STOP_PCT": 5.0,             # OB stop at -5% (was 12 — 7 losses × -12% = -84% killed account)
    "SELL_MIN_ZONE_STRENGTH": 70.0, # SELL/PE needs higher score (24% win rate vs BUY 50%)
    "EXIT_REASON": "SNIPER_TRAILING_EXIT",
}
