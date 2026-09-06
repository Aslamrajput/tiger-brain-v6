# tiger-brain-v6
Tiger Brain V6.0 - Algorithmic Trading Platform

## 5-Brain Structural Architecture (V6.1)

System options-buying ke liye 5 functional brains mein divided hai. Har brain
ka apna module hai aur flow `pipeline/brain_flow.py` sabko wire karta hai:

| Brain | Module | Kaam |
|-------|--------|------|
| **Brain 1** — Market Scanner & Regime Detection | `pipeline/brain1_scanner.py` | Momentum & noise filter: choppy candles reject, sirf high volume velocity YA significant RS divergence wale setups aage jaate hain. Regime detection bhi yahan hai. |
| **Brain 2** — Setup Trigger & Entry Engine | `pipeline/smart_money_scanner.py` | SMC (Smart Money Concepts): Order Blocks, Liquidity Sweeps, RS score (0-100). Direction + entry + structural stop-loss nikalta hai. |
| **Brain 3** — Option Chain & Greeks/OI Velocity Selector | `broker/option_selector.py` | Direction se CE/PE chunta hai, delta band (ATM/slightly-ITM), liquidity gates (spread/OI), OI-velocity hot strikes preference, expiry window guard. |
| **Brain 4** — Capital Allocation & Trade Counter Guard | `risk/risk_management.py` + `broker/position_sizer.py` | Daily trade counters (global max 5-10, commodity-specific max 5-10 — limit hit to us category ke naye trades band). Live broker capital se dynamic sizing (max 10% per trade), kabhi hardcoded lot nahi. |
| **Brain 5** — Risk Guard, Gamma Tracking & Execution Exit | `risk/exit_brain.py` | Stop-loss/target/trailing/time-based exits + gamma tracking (expiry ke paas gamma spike se bachav) + structural stop (Brain 2 ka SMC level). |

```bash
# Pura flow demo (synthetic data pe):
python3 -m pipeline.brain_flow
```

Key rules (config `BRAIN1`-`BRAIN5` sections in `config/thresholds.py`):
- **Trade limits**: global 8/day (clamped 5-10), commodity 5/day (clamped 5-10). Nayadin pe auto-reset.
- **Capital**: Angel One `getRMS()` se live available cash; per-trade cap 10%, total exposure cap 50%. Broker fail + no fallback = trade block (fail-safe).
- **Market category**: MCX/NCDEX symbols = commodity (counter alag); NSE/BSE/NFO = equity.

Note: ye 5-Brain layer purane 5-stage pipeline (stage1-stage5) ke SAATH hai —
stage pipeline backtest engine use karta hai, brain architecture live flow
ke liye hai.


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

# Intraday backtest — 30 din ka 5-min data (cache-first, data/intraday.py se)
python3 -m backtest.cli --interval FIVE_MINUTE --intraday-days 30 --options-pnl
```

`--interval` ONE_DAY (default) chhodkar kuch bhi ho to run intraday ban jaata
hai: ek din mein kai bars, isliye kai decisions. Tab teen cheezein apne aap
badalti hain — walk-forward ke `--train-days`/`--test-days` bars mein convert
hote hain (5-min = 75 bars/din), har session ka **aakhri bar skip** hota hai
(position overnight nahi rakhi jaati, warna overnight gap intraday move gina
jaata), aur India VIX har bar ko **pichhle session** ka close deta hai (us din
ka VIX close intraday decision mein lookahead hota). Options P&L ab asli
holding period se theta lagata hai, isliye intraday exit pe decay kam hai.

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

## Gate diagnostics aur sensitivity analysis

```bash
# Kaun sa gate kitne din NO_TRADE kara raha hai
python3 -m backtest.cli --source csv --csv-path nifty_2y.csv --diagnose

# Ek hi dataset pe kai score-thresholds ka signal-count/accuracy
python3 -c "from backtest.cli import load_from_csv; \
from backtest.sensitivity import run_threshold_sweep, print_sweep_report; \
print_sweep_report(run_threshold_sweep(load_from_csv('nifty_2y.csv')))"
```

`--diagnose` regime distribution, final decisions, har NO_TRADE din ka
blocking gate (hard veto vs score cutoff), score distribution aur per
sub-brain vote counts chhaapta hai. `--score-threshold` / `--stage1-min`
se cutoff bina code badle override ho sakta hai.

**Index data ka volume 0 hota hai:** Angel ka NIFTY spot token har candle
pe `volume = 0` deta hai. Pehle sub-brains ise "volume confirmation fail"
maante the, isliye har din confidence structurally kat rahi thi aur 2 saal
ke real data pe **0 trades** aaye. Ab volume-0 ko "data available nahi"
maana jaata hai: us factor ka weight baaki factors mein redistribute hota
hai (waisa hi jaisa OI/zone gaps ke liye pehle se hota tha), VWAP rolling
window pe banta hai, aur Meta-Brain ka score sirf *participating* brains ke
weight se normalise hota hai — yani IV feed ke bina chup baitha Vol-Arb ab
score ko structurally cap nahi karta.

⚠️ Sweep se sabse acchi accuracy wala threshold utha kar production mein
daalna overfitting hai — sweep sirf ye dikhata hai ki gate kis level pe
khulta hai. `DECISION_SCORE_THRESHOLD` abhi bhi 65 (conservative) hai.

## Options P&L simulation (`--options-pnl`)

Har directional decision ko ek option trade ki tarah simulate karta hai:
aaj close pe 1 lot ATM option (BUY → CE, SELL → PE), agle din close pe exit.
Premium Black-Scholes se banta hai (IV = India VIX), aur exit pe expiry ek
din nazdeek hoti hai — isliye theta apne aap P&L mein aata hai, saath mein
slippage aur brokerage bhi. Report net P&L, win rate vs directional accuracy,
profit factor, expectancy aur max drawdown dikhati hai.

IV entry aur exit, dono par **us waqt ka** VIX se aata hai, isliye VIX ka
asli move P&L mein dikhta hai. Uske upar `--iv-crush-pct` exit IV pe ek
extra haircut lagata hai (weekly ATM option pe event/expiry crush VIX se
bada hota hai). Ye ek assumption hai, mapa hua number nahi — isliye har
run ke saath ek **IV-crush sensitivity table** bhi chhapta hai (0/5/10/20%
crush pe net P&L, PF, win rate). Result ko us range ki tarah padho.

Tuning: `--lot-size`, `--strike-step`, `--expiry-weekday` (0=Mon … 3=Thu),
`--iv-crush-pct`.

### Asli option-chain premiums (`--real-option-prices`)

Neeche wali saari limitations sirf MODEL wale trades ki hain. `data/option_chain.py`
Angel ke public scrip master se NIFTY ke NFO contracts (token + expiry + strike)
ek local **registry** mein rakhta hai, aur us token ki asli candles wahi cache-first
layer se laata hai jo underlying ke liye use hoti hai:

```bash
# Registry refresh — ise roz chalao (Angel ke master mein sirf ZINDA
# contracts hote hain; expire hote hi wo gayab ho jaate hain)
python3 -m data.option_chain --refresh

# Backtest asli option candles pe (intraday interval zaroori hai)
python3 -m backtest.cli --interval FIVE_MINUTE --intraday-days 30 \
    --options-pnl --real-option-prices
```

Jis trade ke **dono** legs ka asli bhaav mil jaata hai, us par na Black-Scholes
lagta hai na `--iv-crush-pct` — IV crush wahin premium ke andar aa jaata hai,
guess karne ki zaroorat nahi. Jis ka nahi milta wo trade **poora** model pe
chalta hai (aadha market + aadha model = sabse bhramak number), aur report
`real / model` ka count aur ek coverage report chhapti hai — miss ki teen
wajahein alag ginti hain: contract registry mein nahi, uski history nahi mili,
ya us bar pe trade hi nahi hua.

⚠️ Iski seema: jo contract **registry banne se pehle** expire ho chuka hai,
uska token ab kahin se nahi milta — us daur ke trades model pe hi rahenge.
Registry jitni purani hogi, real coverage utni behtar. Coverage kam ho to
P&L ko "asli" mat maano.

**Model wale trades ke premiums simulated hain, real option-chain quotes nahi:**
- IV har strike pe India VIX maana gaya hai (real chain mein skew hota hai).
- VIX 30-din ka index-level IV hai; weekly ATM option ka crush isse bada
  hota hai. `--iv-crush-pct 0` (default) pe result **optimistic** side pe
  hai — sensitivity table isi liye chhapta hai.
- Sirf close-to-close; intraday stop-loss/target ka path model nahi hota.
- Expiry Thursday maani gayi hai; holiday shift handle nahi hota.

Matlab positive P&L bhi "profitable system" ka proof nahi hai — sirf itna
ki aage paper trading test karne layak hai.

## Intraday data layer (`data/intraday.py`)

Daily candles pe din mein sirf 1 decision ban sakta hai. Intraday trading
ke liye pehla step ye data layer hai — download + cache + safai + quality
check:

```bash
# 30 din ka 5-min NIFTY data (cache-first, sirf missing tail download hoti hai)
python3 -m data.intraday --interval FIVE_MINUTE --days 30

# ek hi download se badi candles bhi banao, aur CSV mein save karo
python3 -m data.intraday --interval ONE_MINUTE --days 20 --resample 15 \
    --save-csv nifty_15min.csv

# bina network ke, sirf cache se (offline reproducible run)
python3 -m data.intraday --offline --interval FIVE_MINUTE --days 30
```

Ye layer kya karta hai:

- Cache `data_cache/<SYMBOL>_<EXCHANGE>_<TOKEN>_<INTERVAL>.csv.gz` mein
  (instrument-wise alag file); dobara chalane pe
  sirf naya hissa fetch hota hai (Angel rate limits bachane ke liye).
- Market hours (09:15–15:30) ke bahar ki candles, weekends aur NSE
  holidays hata deta hai; duplicate timestamps mein naya version rakhta
  hai.
- `--resample` se 1-min se 5/15/60-min candles banti hain, aur bins
  session ke andar hi rehti hain (do dino ki candles kabhi ek bin mein
  nahi milti).
- Har run pe quality report: kitne sessions, kitni candles missing,
  kaunse din adhoore, kitna zero-volume.

⚠️ Missing candles ko ye module **bharta nahi** — forward-fill se fake
candles banti hain aur indicators jhoothe ho jaate hain. Gaps sirf report
hote hain. Aur ye sirf underlying (index/stock) candles hain — option
premium history isme nahi hai, isliye intraday options P&L abhi bhi
simulate hi hoga.

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests -q
```

Tests poori tarah synthetic data pe chalte hain — na broker login chahiye,
na internet.

## Backtest Results — V16 vs V19

### V16 Baseline (HDFCBANK single-symbol)

| Metric | Value |
|--------|-------|
| Symbol | HDFCBANK |
| Return | +6.92% |
| Wins | 3 |
| Win Rate | 100% |
| Premium captures | +73% / +62% / +58% |

> Source: AGENTS.md — HDFCBANK baseline (must be preserved). V16 single-symbol
> backtest on explosive capture trades.

### V19 Backtest (7-symbol portfolio — synthetic trending data)

V19 ne V16 ke saari 6 weaknesses fix ki + Brain 6 (Premium) + Brain 7 (Session)
+ Brain 8 (Execution/Learning) add kiye. Neeche ka result 7-symbol portfolio
> (NIFTY, BANKNIFTY, RELIANCE, SBIN, HDFCBANK, CRUDEOIL, GOLD) pe synthetic
> trending data (20 trading days, 15m + 1m bars) par chala hai.

> ⚠️ Ye synthetic data pe demo run hai — real Angel One data + live broker
> login ke bina. Real P&L alag ho sakta hai. Ye sirf V19 exit engine + smart
> square-off ka behavior demonstrate karta hai. `run_v19_backtest_demo.py`
> chala ke reproduce kar sakte ho.

#### Portfolio Summary

| Metric | V16 (HDFCBANK) | V19 (7-symbol) |
|--------|---------------|----------------|
| Starting Capital | — | ₹150,000 |
| Final Equity | — | ₹267,515 |
| Total Return | +6.92% | **+78.34%** |
| Net P&L | — | ₹117,515 |
| Total Trades | 3 | 84 |
| Win Rate | 100% | 55.95% |
| Profit Factor | — | **2.71** |
| Avg P&L / Trade | — | ₹1,399 |
| Avg Winner | — | ₹3,964 |
| Avg Loser | — | ₹1,859 |
| Best Trade | — | ₹23,442 |
| Worst Trade | — | ₹2,310 (capped near ₹2,000 stop) |
| Max Drawdown | — | 5.99% |

#### Segment Breakdown

| Segment | Trades | Win% | Net P&L | PF |
|---------|--------|------|---------|-----|
| Index Options (NIFTY/BANKNIFTY) | 35 | 62.9% | ₹67,454 | 4.19 |
| Stock Options (Top F&O) | 32 | 46.9% | ₹9,700 | 1.30 |
| Commodity Options (CRUDE/GOLD) | 17 | 58.8% | ₹40,361 | 3.73 |

#### Exit Reason Breakdown (V19 Smart Square-Off in action)

| Exit Reason | Trades |
|-------------|--------|
| stop_loss_2000 (hard stop) | 23 |
| fixed_target_100pct_book50 (+100% target) | 10 |
| v19_trail_lock_65pct (dynamic trail) | 9 |
| 1m_exhaustion (momentum reversal) | 18 |
| iv_expansion_exit (Brain 6 premium sell) | 20 |
| opposing_zone_reached | 2 |
| square_off (loss/small-profit close) | 2 |
| pre_sqoff_trail_lock_80pct (smart profit exit) | 0* |

> *Pre-square-off smart trail exit (`pre_sqoff_trail_lock_80pct`) aur
> `sqoff_smart_profit` is synthetic run mein trigger nahi hue kyunki
> zyada-tar profitable trades pehle hi fixed-target / IV-expansion / trail
> pe exit ho gaye. Real data pe jab trades square-off tak open rahenge,
> tab ye smart exits active honge — loss trades blind close nahi honge,
> profit trades trail pe smart exit milenge.

### Smart Square-Off Behavior (V19+)

NSE 15:15 / MCX 23:15 pe ab **blind close nahi** hota:

| Trade state at square-off | Action |
|---------------------------|--------|
| Loss | close immediately (cut loss) |
| Small profit (<+20%) | close at square-off (lock profit, no trail) |
| Big profit (≥+20%) | exit on aggressive trail in pre-window (smart) |
| Market close (15:30 / 23:30) | NO order execution — sab pehle hi close |

Reproduce karne ke liye:
```bash
python3 run_v19_backtest_demo.py
```
