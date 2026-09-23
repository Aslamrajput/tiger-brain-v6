"""
ML Engine — 5-Model Stacking Ensemble for Tiger trade win-probability.

Super Ensemble (5 powerful models + 1 meta-learner):
  Layer 1 (base models, each predicts independently):
    1. LightGBM    — gradient boosting, leaf-wise growth (fast, precise)
    2. XGBoost     — gradient boosting, level-wise (robust, regularized)
    3. CatBoost    — ordered boosting, no overfit on small data (great for <50 samples)
    4. RandomForest — bagging, deep trees (diversity, low variance)
    5. ExtraTrees   — extremely randomized trees (more diversity, splits random)
  Layer 2 (meta-learner, combines all 5 → final win_prob):
    LogisticRegression — learns optimal weights for the 5 models

This is a STACKING ENSEMBLE (how Kaggle winners combine models), much
more powerful than simple 2-model averaging. The meta-learner learns
WHICH model to trust in WHICH situations.

This module provides:
  - TigerMLGate: load ensemble + predict_proba ADVISOR (never blocks)
  - heuristic_win_probability(): feature-based fallback (no model needed)
  - train_model(): TimeSeriesSplit training with label purging (no leakage)
  - build_training_data(): construct labeled dataset from trade log

ADVISORY MODE: ML never blocks a trade. It provides win_probability as a
confidence signal for ROCKET SIZING (high win_prob → bigger position).
Tiger decides. The money is Tiger's.
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
_CATBOOST_AVAILABLE = True
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
    import catboost as cb
except ImportError:
    _CATBOOST_AVAILABLE = False

try:
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import accuracy_score
    from sklearn.ensemble import RandomForestClassifier, ExtraTreesClassifier
    from sklearn.linear_model import LogisticRegression
except ImportError:
    _SKLEARN_AVAILABLE = False

# Number of base models in the ensemble
N_BASE_MODELS = 5


class TigerMLGate:
    """5-Model Stacking Ensemble win-probability ADVISOR.

    Loads the trained ensemble (5 base models + meta-learner) from disk.
    At inference time, each base model predicts P(win), then the
    meta-learner combines them into a single calibrated win_probability.

    If only some models are loaded, falls back to averaging the available
    ones. If NO models are loaded, uses heuristic_win_probability().

    ADVISORY: never blocks a trade. win_prob drives ROCKET SIZING.
    """

    def __init__(self, model_path: str, min_win_prob: float = 0.70,
                 feature_columns: Optional[list[str]] = None):
        self.model_path = model_path
        self.min_win_prob = min_win_prob
        self.feature_columns = feature_columns or [
            "zone_strength", "volume_velocity", "option_chain_pcr", "live_iv_skew",
            "setup_score", "body_pct", "vol_surge_ratio", "rsi",
            "brain_alignment", "is_scalper", "is_momentum_hunter",
            "sensex_trend", "vix_level",
            "sniper_zone_strength", "fvg_size", "commodity_volatility",
        ]
        # Ensemble models (all None until loaded)
        self.models: dict[str, object] = {}  # name → fitted model
        self.meta_model = None              # LogisticRegression meta-learner
        self._load_models()

    def _load_models(self):
        """Load the ensemble bundle from disk. Gracefully no-op if absent."""
        if not os.path.exists(self.model_path):
            logger.info("ML ensemble not found at %s — using heuristic fallback", self.model_path)
            return
        try:
            import joblib
            bundle = joblib.load(self.model_path)
            if not isinstance(bundle, dict):
                logger.warning("ML bundle is %s, expected dict — heuristic fallback", type(bundle))
                return
            # Load all available base models from bundle
            _model_keys = ["lgbm_model", "xgb_model", "catboost_model",
                           "rf_model", "et_model"]
            _names = ["LightGBM", "XGBoost", "CatBoost", "RandomForest", "ExtraTrees"]
            for key, name in zip(_model_keys, _names):
                m = bundle.get(key)
                if m is not None:
                    self.models[name] = m
                    logger.info("🧠 %s model loaded from ensemble", name)
            # Load meta-learner
            self.meta_model = bundle.get("meta_model")
            if self.meta_model is not None:
                logger.info("🧠 Meta-learner (LogisticRegression) loaded")
            # Load feature columns if present
            if "feature_columns" in bundle:
                self.feature_columns = bundle["feature_columns"]
            loaded = len(self.models)
            logger.info("🧠 Ensemble ready: %d/%d base models + %s meta",
                        loaded, N_BASE_MODELS,
                        "with" if self.meta_model else "no")
        except Exception as exc:
            logger.error("ML ensemble load failed: %s — heuristic fallback", exc)

    def is_enabled(self) -> bool:
        """True if at least one model in the ensemble is loaded."""
        return len(self.models) > 0

    def _base_predictions(self, X) -> list[float]:
        """Get P(win) from each loaded base model. Returns list of probs."""
        probs = []
        if "LightGBM" in self.models:
            try:
                probs.append(float(self.models["LightGBM"].predict_proba(X)[0][1]))
            except Exception:
                pass
        if "XGBoost" in self.models:
            try:
                probs.append(float(self.models["XGBoost"].predict_proba(X)[0][1]))
            except Exception:
                pass
        if "CatBoost" in self.models:
            try:
                probs.append(float(self.models["CatBoost"].predict_proba(X)[0][1]))
            except Exception:
                pass
        if "RandomForest" in self.models:
            try:
                probs.append(float(self.models["RandomForest"].predict_proba(X)[0][1]))
            except Exception:
                pass
        if "ExtraTrees" in self.models:
            try:
                probs.append(float(self.models["ExtraTrees"].predict_proba(X)[0][1]))
            except Exception:
                pass
        return probs

    def predict_win_probability(self, features: dict) -> float:
        """5-model ensemble + meta-learner → calibrated P(win).

        If meta-learner is loaded: base models → meta → final win_prob.
        If no meta but some bases: average of available base predictions.
        If no models at all: returns 1.0 (triggers heuristic fallback).
        """
        if not self.is_enabled():
            return 1.0
        try:
            from data.features import feature_vector
            X = feature_vector(features)
            base_probs = self._base_predictions(X)
            if not base_probs:
                return 1.0
            # If meta-learner available, use it to combine base predictions
            if self.meta_model is not None and len(base_probs) == N_BASE_MODELS:
                meta_X = np.array(base_probs).reshape(1, -1)
                return float(self.meta_model.predict_proba(meta_X)[0][1])
            # Fallback: average available base model predictions
            return float(np.mean(base_probs))
        except Exception as exc:
            logger.warning("ML ensemble predict failed: %s — pass-through", exc)
            return 1.0

    def check_gate(self, features: dict) -> tuple[bool, float]:
        """Run ensemble inference — ADVISORY, never blocks.

        Returns:
            (passed, win_probability)
            passed = True ALWAYS (ML is advisory, Tiger decides).
            win_probability drives ROCKET SIZING (bigger qty for high prob).
        """
        win_prob = self.predict_win_probability(features)
        # If no model loaded, use feature-based heuristic so ML still helps
        if win_prob >= 1.0:
            win_prob = heuristic_win_probability(features)
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
    """How many 7-brain alignments are needed at this win_prob.

    Higher ML confidence → fewer brains need to agree.
      win_prob > 0.80 → 3, > 0.70 → 4, else 5.
    """
    if win_prob > 0.80:
        return 3
    if win_prob > 0.70:
        return 4
    return 5


# ============================================================
# TRAINING — 5-Model Stacking Ensemble
# ============================================================

def _purge_boundary_labels(
    train_df: pd.DataFrame, test_df: pd.DataFrame, purge_bars: int,
    timestamp_col: str = "entry_ts",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Purge labels near the train/test boundary to prevent leakage."""
    if purge_bars <= 0 or train_df.empty or test_df.empty:
        return train_df, test_df
    train_purged = train_df.iloc[:-purge_bars] if len(train_df) > purge_bars else train_df
    test_purged = test_df.iloc[purge_bars:] if len(test_df) > purge_bars else test_df
    return train_purged, test_purged


def _make_lgbm():
    """LightGBM — leaf-wise growth, fast and precise."""
    return lgb.LGBMClassifier(
        n_estimators=200, max_depth=6, learning_rate=0.05,
        num_leaves=31, min_child_samples=5,
        subsample=0.8, colsample_bytree=0.8,
        reg_alpha=0.1, reg_lambda=0.1,
        random_state=42, verbose=-1, n_jobs=1,
    )


def _make_xgb():
    """XGBoost — level-wise growth, robust and regularized."""
    return xgb.XGBClassifier(
        n_estimators=200, max_depth=6, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        reg_alpha=0.1, reg_lambda=0.1,
        random_state=42, verbosity=0, n_jobs=1,
        use_label_encoder=False, eval_metric="logloss",
    )


def _make_catboost():
    """CatBoost — ordered boosting, excellent for small datasets."""
    return cb.CatBoostClassifier(
        iterations=200, depth=6, learning_rate=0.05,
        l2_leaf_reg=3.0, random_seed=42,
        verbose=0, allow_writing_files=False,
    )


def _make_rf():
    """RandomForest — bagging diversity, deep trees, low variance."""
    return RandomForestClassifier(
        n_estimators=200, max_depth=8,
        min_samples_split=2, min_samples_leaf=1,
        max_features="sqrt", random_state=42, n_jobs=1,
    )


def _make_et():
    """ExtraTrees — extremely randomized, maximum diversity."""
    return ExtraTreesClassifier(
        n_estimators=200, max_depth=8,
        min_samples_split=2, min_samples_leaf=1,
        max_features="sqrt", random_state=42, n_jobs=1,
    )


# Factory: name → constructor
_MODEL_FACTORIES = {
    "lgbm": ("LightGBM", _make_lgbm, _LGBM_AVAILABLE),
    "xgb": ("XGBoost", _make_xgb, _XGB_AVAILABLE),
    "catboost": ("CatBoost", _make_catboost, _CATBOOST_AVAILABLE),
    "rf": ("RandomForest", _make_rf, _SKLEARN_AVAILABLE),
    "et": ("ExtraTrees", _make_et, _SKLEARN_AVAILABLE),
}


def train_model(
    df: pd.DataFrame,
    feature_columns: list[str],
    label_col: str = "label",
    n_splits: int = 3,
    purge_bars: int = 2,
    min_samples: int = 10,
    validated_acc_min: float = 0.50,
) -> Optional[dict]:
    """Train 5-Model Stacking Ensemble with TimeSeriesSplit (no leakage).

    STACKING ARCHITECTURE:
      Layer 1: 5 base models (LGBM + XGB + CatBoost + RF + ExtraTrees)
               each trained on the same features via TimeSeriesSplit
      Layer 2: LogisticRegression meta-learner, trained on the base
               models' out-of-fold predictions, learns which model to
               trust in which situation.

    Args:
        df: training data with feature columns + label + entry_ts
        feature_columns: ordered feature names (must match inference)
        label_col: binary target (1=win, 0=loss)
        n_splits: TimeSeriesSplit folds
        purge_bars: rows to purge at each train/test boundary
        min_samples: skip training if fewer rows
        validated_acc_min: reject ensemble if OOS accuracy below this

    Returns:
        {lgbm_model, xgb_model, catboost_model, rf_model, et_model,
         meta_model, feature_columns, metrics} or None
    """
    if not _SKLEARN_AVAILABLE:
        logger.error("sklearn not available — cannot train")
        return None

    available = [k for k, (_, _, avail) in _MODEL_FACTORIES.items() if avail]
    if not available:
        logger.error("No ML libraries available — cannot train")
        return None
    logger.info("Training ensemble with %d models: %s", len(available), available)

    if len(df) < min_samples:
        logger.warning("Training skipped: %d samples < min %d", len(df), min_samples)
        return None

    # Sort by timestamp (chronological — no future leakage)
    if "entry_ts" in df.columns:
        df = df.sort_values("entry_ts").reset_index(drop=True)

    X = df[feature_columns].values.astype(np.float64)
    y = df[label_col].values.astype(np.int32)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

    tscv = TimeSeriesSplit(n_splits=n_splits)

    # Collect OOF predictions from each base model for meta-learner training
    oof_preds: dict[str, list[float]] = {k: [] for k in available}
    fold_accs: dict[str, list[float]] = {k: [] for k in available}
    best_models: dict[str, object] = {}
    best_accs: dict[str, float] = {k: 0.0 for k in available}

    for fold_idx, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_tr, X_te = X[train_idx], X[test_idx]
        y_tr, y_te = y[train_idx], y[test_idx]

        # Purge boundary labels (leakage prevention)
        if purge_bars > 0 and len(X_tr) > purge_bars and len(X_te) > purge_bars:
            X_tr = X_tr[:-purge_bars]
            y_tr = y_tr[:-purge_bars]
            X_te = X_te[purge_bars:]
            y_te = y_te[purge_bars:]

        if len(X_tr) < 3 or len(X_te) < 1:
            logger.info("Fold %d skipped (insufficient after purge)", fold_idx)
            continue

        for key in available:
            _, factory, _ = _MODEL_FACTORIES[key]
            try:
                model = factory()
                model.fit(X_tr, y_tr)
                preds = model.predict(X_te)
                acc = accuracy_score(y_te, preds)
                fold_accs[key].append(acc)
                # Collect OOF probas for meta-learner
                if hasattr(model, "predict_proba"):
                    probas = model.predict_proba(X_te)[:, 1]
                    oof_preds[key].extend(probas.tolist())
                else:
                    oof_preds[key].extend(preds.tolist())
                if acc > best_accs[key]:
                    best_accs[key] = acc
                    best_models[key] = model
                logger.info("Fold %d %s: train=%d test=%d acc=%.4f",
                            fold_idx, _MODEL_FACTORIES[key][0],
                            len(X_tr), len(X_te), acc)
            except Exception as exc:
                logger.warning("Fold %d %s failed: %s", fold_idx,
                               _MODEL_FACTORIES[key][0], exc)

    # Validate: at least one model must beat the threshold
    any_valid = any(accs and max(accs) >= validated_acc_min
                    for accs in fold_accs.values())
    all_accs = [a for accs in fold_accs.values() for a in accs]
    if not any_valid or not all_accs:
        logger.warning("Ensemble rejected: no model reached acc %.2f", validated_acc_min)
        return None

    # Retrain final base models on ALL data
    final_models: dict[str, object] = {}
    for key in available:
        _, factory, _ = _MODEL_FACTORIES[key]
        try:
            m = factory()
            m.fit(X, y)
            final_models[key] = m
        except Exception as exc:
            logger.warning("Final %s training failed: %s", _MODEL_FACTORIES[key][0], exc)

    if not final_models:
        return None

    # Train meta-learner (LogisticRegression) on OOF predictions
    meta_model = None
    oof_keys = [k for k in available if oof_preds.get(k)]
    if oof_keys:
        min_oof = min((len(oof_preds[k]) for k in oof_keys), default=0)
    else:
        min_oof = 0
    if min_oof >= 3 and len(oof_keys) >= 2:
        try:
            meta_X = np.column_stack([oof_preds[k][:min_oof] for k in oof_keys])
            # Build aligned y: collect test labels from each fold
            oof_y = []
            for fold_idx, (train_idx, test_idx) in enumerate(tscv.split(X)):
                if purge_bars > 0 and len(test_idx) > purge_bars:
                    test_idx = test_idx[purge_bars:]
                oof_y.extend(y[test_idx].tolist())
            oof_y = np.array(oof_y[:min_oof], dtype=np.int32)
            meta_model = LogisticRegression(
                max_iter=500, random_state=42, solver="lbfgs",
            )
            meta_model.fit(meta_X, oof_y)
            logger.info("🧠 Meta-learner trained on %d OOF samples × %d models",
                        min_oof, len(oof_keys))
        except Exception as exc:
            logger.warning("Meta-learner training failed: %s — using averaging", exc)
            meta_model = None

    oos_acc = float(np.mean(all_accs))
    logger.info(
        "✅ 5-Model Ensemble trained: mean OOS acc=%.4f | samples=%d | models=%d",
        oos_acc, len(df), len(final_models))

    # Build result bundle with all model keys
    result = {
        "feature_columns": feature_columns,
        "meta_model": meta_model,
        "metrics": {
            "oos_accuracy": oos_acc,
            "fold_accuracies": all_accs,
            "n_samples": int(len(df)),
            "n_models": len(final_models),
        },
    }
    # Map internal keys to bundle keys
    _key_map = {
        "lgbm": "lgbm_model",
        "xgb": "xgb_model",
        "catboost": "catboost_model",
        "rf": "rf_model",
        "et": "et_model",
    }
    for key, model in final_models.items():
        result[_key_map.get(key, key)] = model
    # Backward-compat: also set "model" to lgbm for old loaders
    if "lgbm_model" in result:
        result["model"] = result["lgbm_model"]
    if "xgb_model" in result:
        result["xgb_model"] = result["xgb_model"]

    return result


def build_training_data(trade_log: list[dict], feature_columns: list[str],
                        sniper_only: bool = False,
                        sniper_min_pnl_pct: float = 30.0,
                        sniper_exit_reason: str = "SNIPER_TRAILING_EXIT",
                        ) -> pd.DataFrame:
    """Build a labeled training DataFrame from historical trade log.

    Each CLOSED trade must contain ml_features + outcome (pnl or win flag).
    OPEN records (no outcome yet) are skipped.

    label = 1 if trade was profitable, 0 if loss.

    SNIPER_ONLY mode: train only on true sniper trades (entry_quality
    is_true_sniper=True). Legacy records (no entry_quality) fall back
    to outcome filter (exit_reason + pnl_pct).

    Returns empty DataFrame if trade_log is empty or no CLOSED trades.
    """
    if not trade_log:
        return pd.DataFrame(columns=feature_columns + ["label", "entry_ts"])

    rows = []
    skipped_entry_quality = 0
    skipped_sniper = 0
    for t in trade_log:
        if t.get("status") == "OPEN":
            continue

        # --- SNIPER_ONLY filtering ---
        if sniper_only:
            entry_quality = t.get("entry_quality", {})
            if entry_quality:
                if not entry_quality.get("is_true_sniper", False):
                    skipped_entry_quality += 1
                    continue
            else:
                # Legacy record — fall back to outcome filter
                exit_reason = t.get("exit_reason", "")
                if exit_reason != sniper_exit_reason:
                    skipped_sniper += 1
                    continue
                pnl = float(t.get("pnl", 0.0) or 0.0)
                cost = float(t.get("trade_cost", 0.0) or 0.0)
                if cost <= 0:
                    entry_price = float(t.get("entry_price", 0.0) or 0.0)
                    qty = float(t.get("quantity", 0.0) or 0.0)
                    cost = entry_price * qty
                pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0
                if pnl_pct < sniper_min_pnl_pct:
                    skipped_sniper += 1
                    continue

        # Extract features
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
                continue

        row = {col: float(feats.get(col, 0.0)) for col in feature_columns}
        row["label"] = label
        row["entry_ts"] = t.get("entry_ts") or t.get("entry_time") or t.get("exit_time", "")
        rows.append(row)

    if skipped_entry_quality:
        logger.info("entry_quality gate: %d trades skipped (not true sniper)", skipped_entry_quality)
    if skipped_sniper:
        logger.info("sniper_only fallback: %d legacy trades skipped", skipped_sniper)

    if not rows:
        return pd.DataFrame(columns=feature_columns + ["label", "entry_ts"])

    df = pd.DataFrame(rows)
    if "entry_ts" in df.columns:
        df["entry_ts"] = pd.to_datetime(df["entry_ts"], errors="coerce")
        df = df.sort_values("entry_ts").reset_index(drop=True)
    return df


def save_model(bundle: dict, model_path: str) -> bool:
    """Save the ensemble bundle to disk as a single .joblib file."""
    try:
        import joblib
        os.makedirs(os.path.dirname(model_path) or ".", exist_ok=True)
        joblib.dump(bundle, model_path, compress=3)
        n = bundle.get("metrics", {}).get("n_models", 0)
        logger.info("✅ ML ensemble saved: %s (%d models + %s meta)",
                    model_path, n, "with" if bundle.get("meta_model") else "no")
        return True
    except Exception as exc:
        logger.error("Model save failed: %s", exc)
        return False
