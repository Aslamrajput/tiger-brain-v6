#!/usr/bin/env python3
"""Nightly ML ensemble retrain — runs at midnight via cron, no live-thread blocking.

Usage (crontab):
  0 0 * * * cd /home/ec2-user/tiger-brain-v6 && /usr/bin/python3 -m jobs.retrain_model >> /home/ec2-user/ml_retrain.log 2>&1

Flow:
  1. Load historical trade log (trade_log.json)
  2. Build labeled training data (win=1, loss=0)
  3. Train ENSEMBLE (LightGBM + XGBoost) with TimeSeriesSplit (no leakage)
  4. Validate OOS accuracy >= threshold
  5. Save both .joblib models to ML_ENGINE["MODEL_PATH"] (+ _xgb.joblib)

ML is ADVISORY — never blocks Tiger's trades. The ensemble provides a
win-probability confidence signal. Tiger decides whether to proceed.
"""
from __future__ import annotations

import logging
import os
import sys

# Ensure project root on path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config.thresholds import ML_ENGINE
from pipeline.ml_engine import build_training_data, train_model, save_model

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [retrain] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    import datetime
    logger.info("=" * 60)
    logger.info("TIGER NIGHTLY ML RETRAIN — %s", datetime.datetime.now())
    logger.info("=" * 60)

    model_path = ML_ENGINE["MODEL_PATH"]
    feature_cols = ML_ENGINE["FEATURE_COLUMNS"]
    train_cfg = ML_ENGINE["TRAINING"]

    # Load trade log
    trade_log_path = os.path.join(_ROOT, "trade_log.json")
    if not os.path.exists(trade_log_path):
        logger.warning("No trade_log.json found — cannot train yet. Skipping.")
        return

    import json
    try:
        with open(trade_log_path) as f:
            trade_log = json.load(f)
    except Exception as exc:
        logger.error("Failed to load trade_log.json: %s", exc)
        return

    if not trade_log:
        logger.warning("Trade log empty — nothing to train on. Skipping.")
        return

    logger.info("Loaded %d historical trades", len(trade_log))

    # Build labeled training data (sniper-only: big winners that trailed out)
    sniper_cfg = train_cfg.get("SNIPER_ONLY", False)
    df = build_training_data(
        trade_log, feature_cols,
        sniper_only=sniper_cfg,
        sniper_min_pnl_pct=train_cfg.get("SNIPER_MIN_PNL_PCT", 30.0),
        sniper_exit_reason=train_cfg.get("SNIPER_EXIT_REASON", "SNIPER_TRAILING_EXIT"),
    )
    if df.empty:
        logger.warning("No labeled trades (missing outcomes) — skipping.")
        return

    logger.info(
        "Training data: %d rows | wins=%d losses=%d | features=%s%s",
        len(df), int(df["label"].sum()), int((df["label"] == 0).sum()),
        ", ".join(feature_cols),
        " [SNIPER_ONLY]" if sniper_cfg else "",
    )

    # Train with TimeSeriesSplit + purge
    bundle = train_model(
        df=df,
        feature_columns=feature_cols,
        n_splits=train_cfg["N_SPLITS"],
        purge_bars=train_cfg["PURGE_BARS"],
        min_samples=train_cfg["MIN_SAMPLES"],
        validated_acc_min=train_cfg["VALIDATED_ACC_MIN"],
    )

    if bundle is None:
        logger.warning("Training did not produce a valid model — old model retained.")
        return

    # Save model artifact
    full_path = os.path.join(_ROOT, model_path)
    if save_model(bundle, full_path):
        metrics = bundle.get("metrics", {})
        logger.info(
            "Retrain complete: OOS acc=%.4f | folds=%d | saved to %s",
            metrics.get("oos_accuracy", 0),
            metrics.get("n_splits", 0),
            full_path,
        )
    else:
        logger.error("Model training succeeded but save failed!")


if __name__ == "__main__":
    main()
