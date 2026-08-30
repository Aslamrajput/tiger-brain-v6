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

# Directional accuracy ke saath simulated options P&L (theta + costs)
python3 -m backtest.cli --source csv --csv-path nifty_2y.csv --options-pnl
```

`--mode walkforward` (default) data ko kai sequential folds mein todta hai —
har fold ka test window ek alag, unseen period hota hai. Report per-fold
accuracy, aggregate out-of-sample accuracy, aur consistency stats (best vs
worst fold, spread) dikhati hai, kyunki ek average number ye chhupa sakta hai
ki system sirf ek lucky period mein chala tha.

**Kya ye backtest NAHI batata:**
- Bina `--options-pnl` ke ye sirf directional accuracy hai (agle din price
  sahi disha mein gaya ya nahi). 55% accuracy ka matlab profit nahi hota.
- Train window pe abhi koi parameter tuning nahi hoti (thresholds
  `config/thresholds.py` mein fixed hain), isliye ye classic walk-forward
  *optimization* nahi — uska validation-only version hai.

## Options P&L simulation (`--options-pnl`)

Har directional decision ko ek option trade ki tarah simulate karta hai:
aaj close pe 1 lot ATM option (BUY → CE, SELL → PE), agle din close pe exit.
Premium Black-Scholes se banta hai (IV = India VIX), aur exit pe expiry ek
din nazdeek hoti hai — isliye theta apne aap P&L mein aata hai, saath mein
slippage aur brokerage bhi. Report net P&L, win rate vs directional accuracy,
profit factor, expectancy aur max drawdown dikhati hai.

Tuning: `--lot-size`, `--strike-step`, `--expiry-weekday` (0=Mon … 3=Thu).

**Ye simulated premiums hain, real option-chain quotes nahi:**
- IV har strike pe India VIX maana gaya hai (real chain mein skew hota hai).
- Entry se exit tak IV constant hai — yani IV crush ka nuksaan MISSING hai,
  isliye real result is simulation se **kharab** hoga, behtar nahi.
- Sirf close-to-close; intraday stop-loss/target ka path model nahi hota.
- Expiry Thursday maani gayi hai; holiday shift handle nahi hota.

Matlab positive P&L bhi "profitable system" ka proof nahi hai — sirf itna
ki aage paper trading test karne layak hai.

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests -q
```

Tests poori tarah synthetic data pe chalte hain — na broker login chahiye,
na internet.
