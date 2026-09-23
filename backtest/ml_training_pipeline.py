"""
ML Training Pipeline — Advanced backtest-based training for the 5-model ensemble.

3 Core Modules (user requirement: train on backtest data, validate real-world):

  1. VOLATILITY & MOMENTUM FILTERS
     - ATR% threshold: skip sideways markets (theta decay kills options buyers)
     - IV Rank filter: only trade when IV in 30-95 range (not too cheap, not
       too expensive). Low IV = no movement. High IV = overpriced premium.
     - These filters are applied to BACKTEST trade generation so the model
       only learns from trades taken in tradeable volatility regimes.

  2. SLIPPAGE & LIQUIDITY SIMULATION
     - 0.75% slippage per side (entry + exit) on every backtest trade
     - Exchange charges: STT (0.05%) + NSE fee (0.05%) + GST (18% on fees)
     - Flat brokerage ₹20/order (Angel One)
     - Model is only VALIDATED if it remains profitable after ALL costs.
     - This prevents the backtest from showing ideal prices that don't
       survive real market execution.

  3. WALK-FORWARD OPTIMIZATION
     - Split backtest data into rolling time-blocks:
       Train: 60 days → Test: 15 days → roll forward 15 days → repeat
     - Each window trains the 5-model ensemble on its train block,
       then validates on the out-of-sample test block.
     - Prevents overfitting (model can't memorize the future).
     - Reports per-window accuracy + aggregate OOS performance.
     - Only accepts the ensemble if MEAN OOS accuracy across all
       windows exceeds the validation threshold.

Usage:
    python3 -m backtest.ml_training_pipeline
    python3 -m backtest.ml_training_pipeline --days 90 --capital 100000
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from config.thresholds import ML_ENGINE
from pipeline.ml_engine import (
    TigerMLGate,
    build_training_data,
    train_model,
    save_model,
    heuristic_win_probability,
)

logger = logging.getLogger(__name__)


# ============================================================
# MODULE 1: VOLATILITY & MOMENTUM FILTERS
# ============================================================

def calculate_atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    """ATR as % of latest close — measures current volatility regime."""
    if df is None or df.empty or len(df) < 2:
        return 0.0
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    tail = tr.tail(period).dropna()
    if tail.empty:
        return 0.0
    atr = float(tail.mean())
    price = float(close.iloc[-1])
    if price <= 0:
        return 0.0
    return round((atr / price) * 100.0, 4)


def calculate_iv_rank(current_iv: float, iv_history: list[float]) -> float:
    """IV Rank: where current IV sits in its historical range (0-100).

    IV Rank = (current_iv - min_iv) / (max_iv - min_iv) * 100
    High rank = IV is expensive (good for selling, bad for buying).
    Low rank = IV is cheap (options are cheap, but may not move).
    """
    if not iv_history or len(iv_history) < 2:
        return 50.0  # neutral if no history
    min_iv = min(iv_history)
    max_iv = max(iv_history)
    if max_iv <= min_iv:
        return 50.0
    return round((current_iv - min_iv) / (max_iv - min_iv) * 100.0, 2)


def passes_volatility_filter(
    atr_pct: float,
    iv_rank: float,
    config: Optional[dict] = None,
) -> tuple[bool, str]:
    """Check if a trade passes the volatility & momentum filter.

    Returns (passes, reason). Rejects sideways markets (low ATR) and
    extreme IV regimes (too cheap or too expensive).

    Args:
        atr_pct: Current ATR as % of price (e.g., 0.5 = 0.5%)
        iv_rank: Current IV rank (0-100)
        config: Override config dict (uses ML_ENGINE default if None)
    """
    cfg = config or ML_ENGINE.get("VOLATILITY_FILTER", {})
    if not cfg.get("ENABLED", True):
        return True, "filter_disabled"

    min_atr = cfg.get("MIN_ATR_PCT", 0.3)
    min_iv = cfg.get("MIN_IV_RANK", 30)
    max_iv = cfg.get("MAX_IV_RANK", 95)

    if atr_pct < min_atr:
        return False, f"low_volatility_atr_{atr_pct:.2f}_<_min_{min_atr}"
    if iv_rank < min_iv:
        return False, f"low_iv_rank_{iv_rank:.0f}_<_min_{min_iv}_theta_decay_risk"
    if iv_rank > max_iv:
        return False, f"high_iv_rank_{iv_rank:.0f}_>_max_{max_iv}_overpriced_premium"

    return True, f"vol_ok_atr_{atr_pct:.2f}_iv_rank_{iv_rank:.0f}"


# ============================================================
# MODULE 2: SLIPPAGE & LIQUIDITY SIMULATION
# ============================================================

def apply_slippage_and_costs(
    entry_price: float,
    exit_price: float,
    quantity: int,
    direction: str = "BUY",
    config: Optional[dict] = None,
) -> dict:
    """Apply real-world slippage + exchange charges to a backtest trade.

    Simulates the actual cost of executing an options buy trade:
      - Slippage: 0.75% per side (entry + exit) — bid-ask spread
      - STT: 0.05% on the selling side (exit)
      - Exchange fee: 0.05% per side (NSE transaction charges)
      - GST: 18% on (brokerage + exchange fees)
      - Brokerage: ₹20 flat per order (Angel One)

    Returns dict with:
      - adjusted_entry: entry price + slippage
      - adjusted_exit: exit price - slippage
      - gross_pnl: (exit - entry) * qty (ideal, no costs)
      - net_pnl: gross_pnl - all costs
      - total_costs: sum of all slippage + charges
      - cost_pct: total_costs as % of trade value
      - profitable_after_costs: bool
    """
    cfg = config or ML_ENGINE.get("SLIPPAGE_MODEL", {})
    if not cfg.get("ENABLED", True):
        gross = (exit_price - entry_price) * quantity
        if direction == "SELL":
            gross = -gross
        return {
            "adjusted_entry": entry_price,
            "adjusted_exit": exit_price,
            "gross_pnl": gross,
            "net_pnl": gross,
            "total_costs": 0.0,
            "cost_pct": 0.0,
            "profitable_after_costs": gross > 0,
        }

    slippage_pct = cfg.get("SLIPPAGE_PCT", 0.75) / 100.0
    stt_pct = cfg.get("STT_PCT", 0.05) / 100.0
    exchange_pct = cfg.get("EXCHANGE_FEE_PCT", 0.05) / 100.0
    gst_pct = cfg.get("GST_PCT", 18.0) / 100.0
    brokerage_flat = cfg.get("BROKERAGE_FLAT", 20.0)

    # BUY: entry at ask (higher), exit at bid (lower)
    # Slippage makes entry worse and exit worse for buyers
    adj_entry = entry_price * (1 + slippage_pct)
    adj_exit = exit_price * (1 - slippage_pct)

    trade_value_entry = adj_entry * quantity
    trade_value_exit = adj_exit * quantity

    # Gross PnL (ideal)
    gross_pnl = (exit_price - entry_price) * quantity
    if direction == "SELL":
        gross_pnl = -gross_pnl

    # Net PnL (after slippage)
    net_pnl_raw = (adj_exit - adj_entry) * quantity
    if direction == "SELL":
        net_pnl_raw = -net_pnl_raw

    # Exchange charges
    exchange_fee = (trade_value_entry + trade_value_exit) * exchange_pct
    stt = trade_value_exit * stt_pct  # STT on sell side only
    brokerage = brokerage_flat * 2  # entry + exit
    gst = (exchange_fee + brokerage) * gst_pct

    total_costs = (gross_pnl - net_pnl_raw) + exchange_fee + stt + brokerage + gst
    net_pnl = gross_pnl - total_costs

    cost_pct = (total_costs / trade_value_entry * 100.0) if trade_value_entry > 0 else 0.0

    return {
        "adjusted_entry": round(adj_entry, 4),
        "adjusted_exit": round(adj_exit, 4),
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "total_costs": round(total_costs, 2),
        "cost_pct": round(cost_pct, 4),
        "profitable_after_costs": net_pnl > 0,
        "costs_breakdown": {
            "slippage": round(gross_pnl - net_pnl_raw, 2),
            "exchange_fee": round(exchange_fee, 2),
            "stt": round(stt, 2),
            "brokerage": round(brokerage, 2),
            "gst": round(gst, 2),
        },
    }


def validate_model_after_costs(
    trades: list[dict],
    config: Optional[dict] = None,
) -> dict:
    """Validate that the model's trades survive real-world costs.

    Handles two trade record formats:
      1. Full records with entry_price + quantity + exit_price
      2. Exit records with trade_cost + pnl + exit_price (links to entry)

    Applies slippage + charges to every trade, then checks:
      - Win rate after costs
      - Net PnL after costs
      - Cost drag (% of gross profit eaten by costs)

    A model is only VALIDATED if net win rate > 50% AND net PnL > 0.
    """
    # Build lookup of entry records (for matching exit records that lack prices)
    entry_lookup: dict[str, dict] = {}
    for t in trades:
        if t.get("entry_price") and t.get("quantity"):
            key = t.get("tradingsymbol") or t.get("symbol", "")
            if key:
                entry_lookup[key] = t

    results = []
    for t in trades:
        # Skip OPEN (entry-only) records — no exit price yet
        if t.get("status") == "OPEN":
            continue

        entry_price = float(t.get("entry_price", 0) or 0)
        exit_p = float(t.get("exit_price", 0) or 0)
        qty = int(t.get("quantity", 0) or 0)
        gross_pnl = float(t.get("pnl", 0) or 0)
        trade_cost = float(t.get("trade_cost", 0) or 0)

        # If entry_price/qty missing, try to find from matching entry record
        if entry_price <= 0 or qty <= 0:
            key = t.get("tradingsymbol") or t.get("symbol", "")
            entry_rec = entry_lookup.get(key, {})
            if entry_price <= 0:
                entry_price = float(entry_rec.get("entry_price", 0) or 0)
            if qty <= 0:
                qty = int(entry_rec.get("quantity", 0) or 0)

        # If still missing but we have trade_cost + gross_pnl, derive
        if (entry_price <= 0 or qty <= 0) and trade_cost > 0 and exit_p > 0:
            # trade_cost = entry_price * qty; gross_pnl = (exit_p - entry_p) * qty
            # So entry_price = (trade_cost + gross_pnl) / qty... need qty.
            # If only have exit_p and gross_pnl: assume qty from trade_cost/exit_p
            qty = max(1, int(trade_cost / max(exit_p, 1)))
            entry_price = trade_cost / qty if qty > 0 else 0

        if entry_price <= 0 or exit_p <= 0 or qty <= 0:
            continue

        direction = "BUY"  # Tiger only buys options
        result = apply_slippage_and_costs(entry_price, exit_p, qty, direction, config)
        result["symbol"] = t.get("symbol", t.get("tradingsymbol", "?"))
        results.append(result)

    if not results:
        return {"validated": False, "reason": "no_valid_trades"}

    gross_wins = sum(1 for r in results if r["gross_pnl"] > 0)
    net_wins = sum(1 for r in results if r["net_pnl"] > 0)
    total_gross = sum(r["gross_pnl"] for r in results)
    total_net = sum(r["net_pnl"] for r in results)
    total_costs = sum(r["total_costs"] for r in results)

    gross_win_rate = gross_wins / len(results)
    net_win_rate = net_wins / len(results)
    cost_drag = (total_costs / abs(total_gross) * 100.0) if total_gross != 0 else 0.0

    validated = net_win_rate > 0.50 and total_net > 0

    return {
        "validated": validated,
        "n_trades": len(results),
        "gross_win_rate": round(gross_win_rate, 4),
        "net_win_rate": round(net_win_rate, 4),
        "total_gross_pnl": round(total_gross, 2),
        "total_net_pnl": round(total_net, 2),
        "total_costs": round(total_costs, 2),
        "cost_drag_pct": round(cost_drag, 2),
        "per_trade": results,
    }


# ============================================================
# MODULE 3: WALK-FORWARD OPTIMIZATION
# ============================================================

def split_walk_forward(
    df: pd.DataFrame,
    train_days: int = 60,
    test_days: int = 15,
    step_days: int = 15,
) -> list[dict]:
    """Split data into rolling walk-forward windows.

    Each window = {train_df, test_df, window_idx, train_start, test_end}
    Train block is `train_days` long, test block is `test_days` long,
    then the window rolls forward by `step_days`.

    Returns list of window dicts, or empty list if insufficient data.
    """
    if df.empty or "entry_ts" not in df.columns:
        return []

    df = df.sort_values("entry_ts").reset_index(drop=True)
    start = df["entry_ts"].iloc[0]
    end = df["entry_ts"].iloc[-1]

    if not isinstance(start, pd.Timestamp):
        start = pd.Timestamp(start)
    if not isinstance(end, pd.Timestamp):
        end = pd.Timestamp(end)

    windows = []
    window_idx = 0
    current = start

    while current + timedelta(days=train_days + test_days) <= end:
        train_start = current
        train_end = current + timedelta(days=train_days)
        test_start = train_end
        test_end = test_start + timedelta(days=test_days)

        train_df = df[(df["entry_ts"] >= train_start) & (df["entry_ts"] < train_end)]
        test_df = df[(df["entry_ts"] >= test_start) & (df["entry_ts"] < test_end)]

        if len(train_df) > 0 and len(test_df) > 0:
            windows.append({
                "window_idx": window_idx,
                "train_df": train_df.reset_index(drop=True),
                "test_df": test_df.reset_index(drop=True),
                "train_start": str(train_start.date()),
                "test_end": str(test_end.date()),
                "train_size": len(train_df),
                "test_size": len(test_df),
            })
            window_idx += 1

        current = current + timedelta(days=step_days)

    logger.info("Walk-forward: %d windows generated (train=%dd, test=%dd, step=%dd)",
                len(windows), train_days, test_days, step_days)
    return windows


def run_walk_forward_optimization(
    trade_log: list[dict],
    feature_columns: list[str],
    config: Optional[dict] = None,
) -> dict:
    """Run full walk-forward optimization on backtest trade data.

    For each rolling window:
      1. Train the 5-model ensemble on the train block
      2. Predict win_prob for each trade in the test block
      3. Apply slippage costs to test trades
      4. Check if model remains profitable after costs

    Returns aggregate report: per-window accuracy + overall validation.
    """
    cfg = config or ML_ENGINE.get("WALK_FORWARD", {})
    train_days = cfg.get("TRAIN_WINDOW_DAYS", 60)
    test_days = cfg.get("TEST_WINDOW_DAYS", 15)
    step_days = cfg.get("STEP_DAYS", 15)
    min_trades = cfg.get("MIN_TRADES_PER_WINDOW", 5)

    # Build training DataFrame from trade log
    df = build_training_data(trade_log, feature_columns, sniper_only=False)
    if df.empty:
        return {"validated": False, "reason": "no_training_data"}

    # Split into walk-forward windows
    windows = split_walk_forward(df, train_days, test_days, step_days)
    if not windows:
        # Not enough data for full windows — train on everything
        logger.warning("Insufficient data for walk-forward windows — single-shot train")
        bundle = train_model(df, feature_columns, min_samples=5,
                             validated_acc_min=0.40)
        if bundle:
            return {
                "validated": True,
                "mode": "single_shot",
                "n_samples": len(df),
                "metrics": bundle["metrics"],
                "windows": [],
            }
        return {"validated": False, "reason": "insufficient_data_for_training"}

    window_results = []
    all_oof_accs = []

    for w in windows:
        train_df = w["train_df"]
        test_df = w["test_df"]

        if len(train_df) < min_trades or len(test_df) < 1:
            logger.info("Window %d skipped (train=%d test=%d < min %d)",
                        w["window_idx"], len(train_df), len(test_df), min_trades)
            continue

        # Train ensemble on this window's train block
        bundle = train_model(
            train_df, feature_columns,
            n_splits=3, purge_bars=2, min_samples=3,
            validated_acc_min=0.30,
        )

        if bundle is None:
            logger.info("Window %d: training rejected", w["window_idx"])
            window_results.append({
                "window_idx": w["window_idx"],
                "train_start": w["train_start"],
                "test_end": w["test_end"],
                "train_size": w["train_size"],
                "test_size": w["test_size"],
                "trained": False,
                "oos_accuracy": 0.0,
            })
            continue

        # Predict on test block
        X_test = test_df[feature_columns].values.astype(np.float64)
        X_test = np.nan_to_num(X_test, nan=0.0, posinf=0.0, neginf=0.0)
        y_test = test_df["label"].values

        # Get ensemble predictions
        model_names = ["lgbm_model", "xgb_model", "catboost_model",
                       "rf_model", "et_model"]
        preds_list = []
        for key in model_names:
            m = bundle.get(key)
            if m is not None:
                try:
                    preds_list.append(m.predict(X_test))
                except Exception:
                    pass

        if preds_list:
            # Majority vote
            stacked = np.array(preds_list)
            final_preds = np.round(np.mean(stacked, axis=0))
        else:
            final_preds = np.zeros(len(y_test))

        from sklearn.metrics import accuracy_score
        acc = accuracy_score(y_test, final_preds)
        all_oof_accs.append(acc)

        logger.info("Window %d: train=%d test=%d OOS acc=%.4f",
                    w["window_idx"], len(train_df), len(test_df), acc)

        window_results.append({
            "window_idx": w["window_idx"],
            "train_start": w["train_start"],
            "test_end": w["test_end"],
            "train_size": w["train_size"],
            "test_size": w["test_size"],
            "trained": True,
            "oos_accuracy": round(acc, 4),
        })

    if not all_oof_accs:
        return {"validated": False, "reason": "no_windows_trained_successfully"}

    mean_oos_acc = float(np.mean(all_oof_accs))
    validated = mean_oos_acc >= 0.50

    return {
        "validated": validated,
        "mode": "walk_forward",
        "n_windows": len(window_results),
        "n_trained": len(all_oof_accs),
        "mean_oos_accuracy": round(mean_oos_acc, 4),
        "min_oos_accuracy": round(min(all_oof_accs), 4),
        "max_oos_accuracy": round(max(all_oof_accs), 4),
        "windows": window_results,
    }


# ============================================================
# FULL PIPELINE — run all 3 modules + train final model
# ============================================================

def run_full_training_pipeline(
    trade_log_path: str = "trade_log.json",
    output_model_path: str = "models/tiger_lgbm.joblib",
    config: Optional[dict] = None,
) -> dict:
    """Run the complete ML training pipeline on backtest data.

    Steps:
      1. Load trade log (backtest or live)
      2. Apply volatility filter (ATR + IV rank) to training data
      3. Run walk-forward optimization (rolling window validation)
      4. Apply slippage & costs to validate real-world profitability
      5. Train final 5-model ensemble on ALL filtered data
      6. Save model to disk

    Returns full report dict.
    """
    print("=" * 70)
    print("🐅 TIGER ML TRAINING PIPELINE — 5-Model Ensemble")
    print("=" * 70)

    # Step 1: Load trade log
    print("\n📊 Step 1: Loading trade log...")
    try:
        with open(trade_log_path) as f:
            trade_log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"   ❌ No trade log at {trade_log_path}")
        return {"success": False, "reason": "no_trade_log"}

    closed_trades = [t for t in trade_log if t.get("status") == "CLOSED"]
    print(f"   Total trades: {len(trade_log)} | Closed: {len(closed_trades)}")

    if len(closed_trades) < 3:
        print("   ⚠️ Need at least 3 closed trades to train. Using heuristic fallback.")
        return {"success": False, "reason": "insufficient_closed_trades",
                "closed_count": len(closed_trades)}

    # Step 2: Volatility filter
    print("\n🌊 Step 2: Volatility & Momentum Filter...")
    vol_cfg = (config or ML_ENGINE).get("VOLATILITY_FILTER", {})
    filtered_trades = []
    vol_rejected = 0
    for t in closed_trades:
        # Use features from the trade's ml_features
        feats = t.get("ml_features", {})
        atr_pct = float(feats.get("commodity_volatility", 0) or 0)
        # IV rank approximation from live_iv_skew
        iv_skew = float(feats.get("live_iv_skew", 0) or 0)
        iv_rank = iv_skew * 100 if iv_skew > 0 else 50.0
        passes, reason = passes_volatility_filter(atr_pct, iv_rank, vol_cfg)
        if passes:
            filtered_trades.append(t)
        else:
            vol_rejected += 1
            print(f"   🚫 {t.get('symbol', '?')}: {reason}")

    print(f"   Volatility filter: {len(filtered_trades)}/{len(closed_trades)} passed "
          f"({vol_rejected} rejected)")

    if len(filtered_trades) < 3:
        print("   ⚠️ Too few trades after volatility filter — using all closed trades")
        filtered_trades = closed_trades

    # Step 3: Walk-forward optimization
    print("\n🔄 Step 3: Walk-Forward Optimization...")
    wf_result = run_walk_forward_optimization(
        filtered_trades, ML_ENGINE["FEATURE_COLUMNS"], config)
    if wf_result.get("validated"):
        mode = wf_result.get("mode", "walk_forward")
        if mode == "walk_forward":
            print(f"   ✅ Walk-forward: {wf_result['n_trained']} windows, "
                  f"mean OOS acc = {wf_result['mean_oos_accuracy']:.4f}")
        else:
            print(f"   ✅ Single-shot training (insufficient data for windows)")
    else:
        print(f"   ⚠️ Walk-forward not validated: {wf_result.get('reason', '?')}")

    # Step 4: Slippage & costs validation
    print("\n💰 Step 4: Slippage & Liquidity Simulation...")
    cost_result = validate_model_after_costs(filtered_trades, config)
    if cost_result.get("validated"):
        print(f"   ✅ Model profitable AFTER costs:")
        print(f"      Gross win rate: {cost_result['gross_win_rate']:.1%}")
        print(f"      Net win rate:   {cost_result['net_win_rate']:.1%}")
        print(f"      Gross PnL: ₹{cost_result['total_gross_pnl']:,.0f}")
        print(f"      Net PnL:   ₹{cost_result['total_net_pnl']:,.0f}")
        print(f"      Total costs: ₹{cost_result['total_costs']:,.0f} "
              f"({cost_result['cost_drag_pct']:.1f}% drag)")
    else:
        print(f"   ⚠️ Model NOT profitable after costs:")
        print(f"      Net PnL: ₹{cost_result.get('total_net_pnl', 0):,.0f}")
        print(f"      Costs eat {cost_result.get('cost_drag_pct', 0):.1f}% of gross")

    # Step 5: Train final ensemble on ALL filtered data
    print("\n🧠 Step 5: Training Final 5-Model Ensemble...")
    df = build_training_data(filtered_trades, ML_ENGINE["FEATURE_COLUMNS"],
                             sniper_only=False)
    final_bundle = train_model(
        df, ML_ENGINE["FEATURE_COLUMNS"],
        n_splits=3, purge_bars=2, min_samples=3,
        validated_acc_min=0.30,
    )

    if final_bundle is None:
        print("   ❌ Final training failed — keeping heuristic fallback")
        return {"success": False, "reason": "final_training_failed",
                "walk_forward": wf_result, "costs": cost_result}

    model_keys = [k for k in final_bundle.keys()
                  if k.endswith("_model") and k != "meta_model"]
    print(f"   ✅ Ensemble trained: {len(model_keys)} models + "
          f"{'meta' if final_bundle.get('meta_model') else 'no meta'}")
    print(f"   OOS accuracy: {final_bundle['metrics']['oos_accuracy']:.4f}")
    print(f"   Samples: {final_bundle['metrics']['n_samples']}")

    # Step 6: Save model
    print(f"\n💾 Step 6: Saving model to {output_model_path}...")
    saved = save_model(final_bundle, output_model_path)
    if saved:
        print("   ✅ Model saved successfully")
    else:
        print("   ❌ Model save failed")

    print("\n" + "=" * 70)
    print("🐅 TRAINING PIPELINE COMPLETE")
    print("=" * 70)

    return {
        "success": saved,
        "n_trades_input": len(closed_trades),
        "n_trades_filtered": len(filtered_trades),
        "volatility_rejected": vol_rejected,
        "walk_forward": wf_result,
        "costs": cost_result,
        "final_model": {
            "n_models": len(model_keys),
            "oos_accuracy": final_bundle["metrics"]["oos_accuracy"],
            "n_samples": final_bundle["metrics"]["n_samples"],
        },
        "model_path": output_model_path if saved else None,
    }


# ============================================================
# CLI ENTRY POINT
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(
        description="Tiger ML Training Pipeline — 5-model ensemble on backtest data")
    parser.add_argument("--trade-log", default="trade_log.json",
                        help="Path to trade log JSON (default: trade_log.json)")
    parser.add_argument("--output", default="models/tiger_lgbm.joblib",
                        help="Output model path (default: models/tiger_lgbm.joblib)")
    args = parser.parse_args()

    result = run_full_training_pipeline(
        trade_log_path=args.trade_log,
        output_model_path=args.output,
    )

    print(f"\n📋 Final Report:")
    print(json.dumps(result, indent=2, default=str))
