"""
Tiger Brain V6+V7 — Central Threshold Configuration
=====================================================
Ye file Master Blueprint ke Part B (Sections 17-29) ke saare concrete
numbers ko ek jagah rakhti hai.
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
    "OPENING_RANGE_MINUTES": 15,
    "OPENING_RANGE_ACTIVE_MULTIPLIER": 1.5,
}

VWAP = {
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

DECISION_SCORE_THRESHOLD = 65

PIPELINE = {
    "STAGE1_MIN_CONFIDENCE": 50,
    "STAGE2_MIN_CONFIDENCE": 50,
    "STAGE2_MAX_DIVERGENCE_FROM_STAGE1": 20,
    "STAGE3_MIN_CONFIDENCE": 65,
    "STAGE4_TIE_BREAK_SCORE_DIFF": 5,
}

RISK = {
    "MAX_RISK_PER_TRADE_PCT": 2.5,
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
    "RESCAN_INTERVAL_MINUTES": 20,
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
    "MARKET_CLOSE_TIME": "15:30",
    "NIGHTLY_REPLAY_TIME": "00:00",
}

import os
DRY_RUN = os.getenv("TIGER_BRAIN_DRY_RUN", "false").lower() == "true"
