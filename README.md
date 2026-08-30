# tiger-brain-v6
Tiger Brain V6.0 - Algorithmic Trading Platform

## Backtest chalana (Phase 2)

Saare commands repo ROOT se chalane hain.

```bash
pip install -r requirements.txt

# 2 saal NIFTY daily data Angel One se, walk-forward validation
python3 -m backtest.cli --years 2

# Data ek baar save karke usi snapshot pe baar-baar chalao (reproducible)
python3 -m backtest.cli --years 2 --save-csv nifty_2y.csv
python3 -m backtest.cli --source csv --csv-path nifty_2y.csv

# Purana single in-sample/out-of-sample split
python3 -m backtest.cli --source csv --csv-path nifty_2y.csv --mode split
```

`--mode walkforward` (default) data ko kai sequential folds mein todta hai —
har fold ka test window ek alag, unseen period hota hai. Report per-fold
accuracy, aggregate out-of-sample accuracy, aur consistency stats (best vs
worst fold, spread) dikhati hai, kyunki ek average number ye chhupa sakta hai
ki system sirf ek lucky period mein chala tha.

**Kya ye backtest NAHI batata:**
- Ye sirf directional accuracy hai (agle din price sahi disha mein gaya ya
  nahi) — real options P&L nahi (premium, theta decay, IV crush shaamil nahi).
  55% accuracy ka matlab profit nahi hota.
- Train window pe abhi koi parameter tuning nahi hoti (thresholds
  `config/thresholds.py` mein fixed hain), isliye ye classic walk-forward
  *optimization* nahi — uska validation-only version hai.

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests -q
```

Tests poori tarah synthetic data pe chalte hain — na broker login chahiye,
na internet.
