"""
ML Engine — Ensemble win-probability ADVISOR for Tiger trade entries.

24-years-experience ensemble: LightGBM + XGBoost vote together.

This module provides:
  - TigerMLGate: load ensemble models + predict_proba ADVISOR (never blocks)
  - train_model(): TimeSeriesSplit training with label purging (no leakage)
  - build_training_data(): construct labeled dataset from backtest trades

The ensemble is a binary classifier:
  label = 1 (win) if trade closed in profit, 0 (loss) otherwise.

Training uses sklearn TimeSeriesSplit (strictly chronological — no K-Fold)
with label purging: bars within PURGE_BARS of the train/test boundary are
dropped to prevent leakage from overlapping holding periods.

ADVISORY MODE: ML never blocks a trade. It provides win_probability as a
confidence signal. High win_prob → Tiger gets more aggressive. Low win_prob
→ Tiger gets an advisory warning but still decides. The money is Tiger's.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Lazy imports — heavy ML libs only loaded when needed
_LGBM_AVAILABLE = True
_XGB_AVAILABLE = True
_SKLEARN_AVAILABLE = True

try:
    import lightgbm as lgb
except ImportError:
    _LGBM_AVAILABLE = False

try:
    import xgboost as xgb
except ImportError:
    _XGB_AVAILABLE = False

try:
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import accuracy_score
except ImportError:
    _SKLEARN_AVAILABLE = False


class TigerMLGate:
    """Ensemble win-probability ADVISOR — LightGBM + XGBoost voting together.

    Loads both models (if available) and averages their predictions for a
    more robust win_probability. This is ADVISORY — it never blocks a trade.
    High win_prob → confidence boost. Low win_prob → advisory warning logged.
    Tiger decides whether to proceed — the money is Tiger's.

    Thread-safe for concurrent scanner reads. Models are loaded once at init
    and stay in memory. Retraining happens offline (jobs/retrain_model.py)
    and writes new .joblib files — the live process picks them up on restart.
    """

    def __init__(self, model_path: str, min_win_prob: float = 0.70,
                 feature_columns: Optional[list[str]] = None):
        self.model_path = model_path
        self.xgb_model_path = model_path.replace(".joblib", "_xgb.joblib")
        self.min_win_prob = min_win_prob
        self.feature_columns = feature_columns or [
            "zone_strength", "volume_velocity", "option_chain_pcr", "live_iv_skew",
            "setup_score", "body_pct", "vol_surge_ratio", "rsi",
            "brain_alignment", "is_scalper", "is_momentum_hunter",
        ]
        self.model = None        # LightGBM
        self.xgb_model = None   # XGBoost
        self._load_models()

    def _load_models(self):
        """Load both ensemble models from disk. Gracefully no-op if absent."""
        if not _LGBM_AVAILABLE:
            logger.warning("LightGBM not installed — ensemble half disabled")
        else:
            self._load_one(self.model_path, "LightGBM", "_load_lgbm")

        if not _XGB_AVAILABLE:
            logger.warning("XGBoost not installed — ensemble half disabled")
        else:
            self._load_one(self.xgb_model_path, "XGBoost", "_load_xgb")

    def _load_lgbm(self, bundle):
        """Extract LightGBM model from bundle."""
        if isinstance(bundle, dict) and "model" in bundle:
            self.model = bundle["model"]
            if "feature_columns" in bundle:
                self.feature_columns = bundle["feature_columns"]
        else:
            self.model = bundle

    def _load_xgb(self, bundle):
        """Extract XGBoost model from bundle."""
        if isinstance(bundle, dict) and "model" in bundle:
            self.xgb_model = bundle["model"]
        else:
            self.xgb_model = bundle

    def _load_one(self, path: str, name: str, loader_method: str):
        """Load a single model from a .joblib file."""
        if not os.path.exists(path):
            logger.info("%s model not found at %s — ensemble partial", name, path)
            return
        try:
            import joblib
            bundle = joblib.load(path)
            getattr(self, loader_method)(bundle)
            logger.info("🧠 %s model loaded: %s", name, path)
        except Exception as exc:
            logger.error("%s model load failed: %s — partial ensemble", name, exc)

    def is_enabled(self) -> bool:
        """True if at least one model in the ensemble is loaded."""
        return self.model is not None or self.xgb_model is not None

    def predict_win_probability(self, features: dict) -> float:
        """Ensemble predict_proba → averaged P(win) from LightGBM + XGBoost.

        Returns 1.0 (pass-through) if no models loaded, so the advisor
        never blocks trading when models aren't available yet.
        """
        if not self.is_enabled():
            return 1.0
        try:
            from data.features import feature_vector
            X = feature_vector(features)
            probs = []
            # LightGBM prediction
            if self.model is not None:
                lgb_proba = self.model.predict_proba(X)
                probs.append(float(lgb_proba[0][1]))
            # XGBoost prediction
            if self.xgb_model is not None:
                xgb_proba = self.xgb_model.predict_proba(X)
                probs.append(float(xgb_proba[0][1]))
            # Ensemble: average of available models
            return float(np.mean(probs)) if probs else 1.0
        except Exception as exc:
            logger.warning("ML ensemble predict failed: %s — pass-through", exc)
            return 1.0

    def check_gate(self, features: dict) -> tuple[bool, float]:
        """Run the ensemble inference — ADVISORY, never blocks.

        Returns:
            (passed, win_probability)
            passed = True ALWAYS (ML is advisory, Tiger decides).
            win_probability is logged as a confidence signal.
        """
        win_prob = self.predict_win_probability(features)
        # If no model loaded, use feature-based heuristic so ML still helps
        if win_prob >= 1.0:
            win_prob = heuristic_win_probability(features)
        # ADVISORY: always pass — Tiger decides, not ML
        return True, win_prob


def heuristic_win_probability(features: dict) -> float:
    """Feature-based win probability estimate when no ML model is trained yet.

    Uses the same features ML would learn from, but as simple weighted rules.
    This gives Tiger a meaningful confidence score from day one — ML takes
    over once a real model is trained (10+ closed trades).

    Base = 0.50 (neutral). Each strong feature adds confidence:
      - zone_strength > 0.8  → +0.10 (strong SMC zone)
      - brain_alignment >= 0.8 (normalized 0-1) → +0.08 (7 brains agree)
      - vol_surge > 1.5      → +0.06 (volume explosion)
      - body_pct > 50%       → +0.06 (momentum candle)
      - rsi <= 35 or >= 65   → +0.05 (momentum extreme = entry)
      - sniper_zone > 80     → +0.10 (strong sniper confluence)
    Capped at 0.95 (never 1.0 — always some uncertainty).
    """
    base = 0.50
    zone = float(features.get("zone_strength", 0) or 0)
    align = float(features.get("brain_alignment", 0) or 0)
    vol_surge = float(features.get("vol_surge_ratio", 0) or 0)
    body = float(features.get("body_pct", 0) or 0)
    rsi = float(features.get("rsi", 50) or 50)
    sniper_zone = float(features.get("sniper_zone_strength", 0) or 0)

    if zone > 0.8:
        base += 0.10
    if align >= 0.8:
        base += 0.08
    if vol_surge > 1.5:
        base += 0.06
    if body > 50:
        base += 0.06
    if rsi <= 35 or rsi >= 65:
        base += 0.05
    if sniper_zone > 80:
        base += 0.10

    return min(base, 0.95)


def required_confluence_for_win_prob(win_prob: float) -> int:
    """How many 7-brain alignments are needed to ALLOW a trade at this win_prob.

    Higher ML confidence → fewer brains need to agree (ML already trusts the
    setup). Lower confidence → demand more confluence to compensate.

      win_prob > 0.80 → required_confluence = 3
      win_prob > 0.70 → required_confluence = 4
      otherwise       → required_confluence = 5
    """
    if win_prob > 0.80:
        return 3
    if win_prob > 0.70:
        return 4
    return 5


# ============================================================
# TRAINING
# ============================================================

def _purge_boundary_labels(
    train_df: pd.DataFrame, test_df: pd.DataFrame, purge_bars: int,
    timestamp_col: str = "entry_ts",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Purge labels near the train/test boundary to prevent leakage.

    Drops the last `purge_bars` rows from train and first `purge_bars` from
    test (by timestamp order) so overlapping holding periods don't leak.
    """
    if purge_bars <= 0 or train_df.empty or test_df.empty:
        return train_df, test_df
    train_purged = train_df.iloc[:-purge_bars] if len(train_df) > purge_bars else train_df
    test_purged = test_df.iloc[purge_bars:] if len(test_df) > purge_bars else test_df
    return train_purged, test_purged


def train_model(
    df: pd.DataFrame,
    feature_columns: list[str],
    label_col: str = "label",
    n_splits: int = 5,
    purge_bars: int = 5,
    min_samples: int = 50,
    validated_acc_min: float = 0.55,
) -> Optional[dict]:
    """Train ENSEMBLE: LightGBM + XGBoost with TimeSeriesSplit (no leakage).

    24-years-experience ensemble: two advanced gradient boosting models
    vote together for more robust win-probability predictions.

    Args:
        df: training data with feature columns + label + entry_ts
        feature_columns: ordered feature names (must match inference)
        label_col: binary target (1=win, 0=loss)
        n_splits: TimeSeriesSplit folds
        purge_bars: rows to purge at each train/test boundary
        min_samples: skip training if fewer rows
        validated_acc_min: reject model if OOS accuracy below this

    Returns:
        {model (lgbm), xgb_model (xgboost), feature_columns, metrics} or None
    """
    if not _SKLEARN_AVAILABLE:
        logger.error("sklearn not available — cannot train")
        return None
    if not _LGBM_AVAILABLE and not _XGB_AVAILABLE:
        logger.error("Neither LightGBM nor XGBoost available — cannot train")
        return None

    if len(df) < min_samples:
        logger.warning(
            "Training skipped: %d samples < min %d", len(df), min_samples)
        return None

    # Sort by timestamp to ensure chronological order
    if "entry_ts" in df.columns:
        df = df.sort_values("entry_ts").reset_index(drop=True)

    X = df[feature_columns].values.astype(np.float64)
    y = df[label_col].values.astype(np.int32)

    # Replace NaN/Inf with 0
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

    tscv = TimeSeriesSplit(n_splits=n_splits)
    lgb_fold_accs = []
    xgb_fold_accs = []
    best_lgb = None
    best_xgb = None
    best_lgb_acc = 0.0
    best_xgb_acc = 0.0

    for fold_idx, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]

        # Purge boundary labels (leakage prevention)
        if purge_bars > 0 and len(X_train) > purge_bars and len(X_test) > purge_bars:
            X_train = X_train[:-purge_bars]
            y_train = y_train[:-purge_bars]
            X_test = X_test[purge_bars:]
            y_test = y_test[purge_bars:]

        if len(X_train) < 50 or len(X_test) < 10:
            logger.info("Fold %d skipped (insufficient after purge)", fold_idx)
            continue

        # LightGBM fold
        if _LGBM_AVAILABLE:
            lgb_model = _make_lgbm()
            lgb_model.fit(X_train, y_train)
            lgb_acc = accuracy_score(y_test, lgb_model.predict(X_test))
            lgb_fold_accs.append(lgb_acc)
            logger.info("Fold %d LGBM: train=%d test=%d acc=%.4f",
                        fold_idx, len(X_train), len(X_test), lgb_acc)
            if lgb_acc > best_lgb_acc:
                best_lgb_acc = lgb_acc
                best_lgb = lgb_model

        # XGBoost fold
        if _XGB_AVAILABLE:
            xgb_model = _make_xgb()
            xgb_model.fit(X_train, y_train)
            xgb_acc = accuracy_score(y_test, xgb_model.predict(X_test))
            xgb_fold_accs.append(xgb_acc)
            logger.info("Fold %d XGB:  train=%d test=%d acc=%.4f",
                        fold_idx, len(X_train), len(X_test), xgb_acc)
            if xgb_acc > best_xgb_acc:
                best_xgb_acc = xgb_acc
                best_xgb = xgb_model

    # Use the best available model's accuracy for validation
    best_acc = max(best_lgb_acc if lgb_fold_accs else 0,
                   best_xgb_acc if xgb_fold_accs else 0)
    all_accs = lgb_fold_accs + xgb_fold_accs
    if not all_accs or best_acc < validated_acc_min:
        logger.warning(
            "Model rejected: best OOS acc %.4f < threshold %.2f",
            best_acc, validated_acc_min)
        return None

    # Retrain on ALL data for final production models
    final_lgb = _make_lgbm().fit(X, y) if _LGBM_AVAILABLE and best_lgb else None
    final_xgb = _make_xgb().fit(X, y) if _XGB_AVAILABLE and best_xgb else None

    oos_accuracy = float(np.mean(all_accs))
    logger.info(
        "✅ Ensemble trained: LGBM acc=%.4f | XGB acc=%.4f | mean=%.4f | samples=%d",
        best_lgb_acc, best_xgb_acc, oos_accuracy, len(df))

    return {
        "model": final_lgb,
        "xgb_model": final_xgb,
        "feature_columns": feature_columns,
        "metrics": {
            "oos_accuracy": oos_accuracy,
            "lgb_accuracy": float(np.mean(lgb_fold_accs)) if lgb_fold_accs else 0.0,
            "xgb_accuracy": float(np.mean(xgb_fold_accs)) if xgb_fold_accs else 0.0,
            "fold_accuracies": all_accs,
            "n_samples": int(len(df)),
            "n_splits": len(all_accs) // 2 if (lgb_fold_accs and xgb_fold_accs) else len(all_accs),
        },
    }


def _make_lgbm():
    """Create a LightGBM classifier with tuned hyperparameters."""
    return lgb.LGBMClassifier(
        n_estimators=200,
        max_depth=6,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=20,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.1,
        reg_lambda=0.1,
        random_state=42,
        verbose=-1,
        n_jobs=1,
    )


def _make_xgb():
    """Create an XGBoost classifier with tuned hyperparameters."""
    return xgb.XGBClassifier(
        n_estimators=200,
        max_depth=6,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.1,
        reg_lambda=0.1,
        random_state=42,
        verbosity=0,
        n_jobs=1,
        use_label_encoder=False,
        eval_metric="logloss",
    )


def build_training_data(trade_log: list[dict], feature_columns: list[str],
                        sniper_only: bool = False,
                        sniper_min_pnl_pct: float = 30.0,
                        sniper_exit_reason: str = "SNIPER_TRAILING_EXIT",
                        ) -> pd.DataFrame:
    """Build a labeled training DataFrame from historical trade log.

    Each trade entry in the log must contain:
      - features dict (or individual feature keys)
      - outcome: realized P&L or win/loss flag
      - entry_ts: timestamp for chronological ordering

    label = 1 if trade was profitable, 0 if loss.

    Prefers CLOSED (exit) records, which carry both ml_features and pnl.
    OPEN (entry-only) records have no outcome yet and are skipped.

    SNIPER_ONLY mode (Sep 2026): train only on sniper trades where
    pnl_pct > sniper_min_pnl_pct AND exit_reason == sniper_exit_reason.
    We train sniper on sniper data only — big winners that trailed out.

    Returns empty DataFrame if trade_log is empty.
    """
    if not trade_log:
        return pd.DataFrame(columns=feature_columns + ["label", "entry_ts"])

    rows = []
    skipped_sniper = 0
    skipped_entry_quality = 0
    for t in trade_log:
        # Skip entry-only (OPEN) records — no outcome label available.
        # Retrain learns from CLOSED trades only (features + realized pnl).
        if t.get("status") == "OPEN":
            continue

        # --- ENTRY-QUALITY GATE (ML SAFETY — Sep 2026) ---
        # ML ko sirf TRUE sniper entries se sikhna chahiye. Ek lucky win
        # jo galt entry se aayi (no zone edge, no SMC), wo ML ko sikhayi
        # NAHI jaani — warna ML galt patterns reinforce karega.
        # is_true_sniper = zone_touched + smc_confluence + velocity + confirm.
        entry_quality = t.get("entry_quality", {})
        if sniper_only and entry_quality:
            if not entry_quality.get("is_true_sniper", False):
                skipped_entry_quality += 1
                continue
        elif sniper_only and not entry_quality:
            # Old trade records (pre-entry_quality) — fall back to outcome
            # filter but LOG a warning so we know it's legacy data.
            logger.debug(
                "Trade %s missing entry_quality — legacy record, "
                "using outcome-based filter as fallback",
                t.get("symbol", "?"),
            )

        # --- SNIPER-ONLY OUTCOME FILTER (secondary, for legacy records) ---
        # Only learn from high-conviction sniper winners that trailed out.
        if sniper_only and not entry_quality:
            exit_reason = t.get("exit_reason", "")
            if exit_reason != sniper_exit_reason:
                skipped_sniper += 1
                continue
            # pnl_pct = realized PnL as % of trade cost (entry premium basis)
            pnl = float(t.get("pnl", 0.0) or 0.0)
            cost = float(t.get("trade_cost", 0.0)
                         or t.get("entry_cost", 0.0) or 0.0)
            # Fall back to entry_price * qty if trade_cost missing
            if cost <= 0:
                entry_price = float(t.get("entry_price", 0.0) or 0.0)
                qty = float(t.get("quantity", 0.0) or 0.0)
                cost = entry_price * qty
            pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0
            if pnl_pct < sniper_min_pnl_pct:
                skipped_sniper += 1
                continue

        # Extract features from the trade dict
        feats = {}
        if "ml_features" in t and isinstance(t["ml_features"], dict):
            feats = t["ml_features"]
        else:
            for col in feature_columns:
                feats[col] = t.get(col, 0.0)

        # Determine label from outcome
        if "win" in t:
            label = int(t["win"])
        else:
            pnl = float(t.get("pnl", t.get("realized_pnl", 0.0)) or 0.0)
            if pnl > 0:
                label = 1
            elif pnl < 0:
                label = 0
            else:
                continue  # skip zero-pnl (unknown outcome)

        row = {col: float(feats.get(col, 0.0)) for col in feature_columns}
        row["label"] = label
        # Prefer entry_ts (from exit record linkage); fall back to entry_time/exit_time
        row["entry_ts"] = t.get("entry_ts") or t.get("entry_time") or t.get("exit_time", "")
        rows.append(row)

    if not rows:
        if sniper_only and skipped_entry_quality:
            logger.info("entry_quality gate: %d trades skipped (not true sniper entry — "
                        "ML refuses to learn from bad/lucky entries)", skipped_entry_quality)
        if sniper_only and skipped_sniper:
            logger.info("sniper_only fallback: %d legacy trades skipped (pnl<%.0f%% or non-sniper exit)",
                        skipped_sniper, sniper_min_pnl_pct)
        return pd.DataFrame(columns=feature_columns + ["label", "entry_ts"])

    if sniper_only and skipped_entry_quality:
        logger.info("entry_quality gate: %d bad-entry trades blocked from ML training "
                    "(not true sniper), %d true-sniper trades kept",
                    skipped_entry_quality, len(rows))
    if sniper_only and skipped_sniper:
        logger.info("sniper_only fallback: %d legacy trades skipped, %d kept",
                    skipped_sniper, len(rows))

    df = pd.DataFrame(rows)
    # Convert entry_ts to datetime for sorting
    if "entry_ts" in df.columns:
        df["entry_ts"] = pd.to_datetime(df["entry_ts"], errors="coerce")
        df = df.sort_values("entry_ts").reset_index(drop=True)
    return df


def save_model(bundle: dict, model_path: str) -> bool:
    """Save the ensemble model bundle to disk as .joblib files.

    Saves the full bundle (with both LightGBM + XGBoost) to model_path,
    and also saves the XGBoost model separately to {base}_xgb.joblib so
    the inference engine can load it independently.
    """
    try:
        import joblib
        os.makedirs(os.path.dirname(model_path) or ".", exist_ok=True)
        joblib.dump(bundle, model_path, compress=3)
        logger.info("✅ ML ensemble saved: %s", model_path)
        # Also save XGBoost model separately for the inference loader
        xgb_model = bundle.get("xgb_model")
        if xgb_model is not None:
            xgb_path = model_path.replace(".joblib", "_xgb.joblib")
            xgb_bundle = {
                "model": xgb_model,
                "feature_columns": bundle.get("feature_columns", []),
                "metrics": bundle.get("metrics", {}),
            }
            joblib.dump(xgb_bundle, xgb_path, compress=3)
            logger.info("✅ XGBoost model saved: %s", xgb_path)
        return True
    except Exception as exc:
        logger.error("Model save failed: %s", exc)
        return False
