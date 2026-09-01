"""
Tiger Brain V6+V7 — Options P&L Simulation (Phase 2, next step)
==================================================================
Ab tak backtest sirf DIRECTIONAL accuracy batata tha: "price sahi
disha mein gaya ya nahi". Par option BUYING mein disha sahi hone ke
baad bhi loss ho sakta hai — theta har din premium khata hai, aur IV
girne pe premium aur girta hai. Ye module us gap ko bharta hai: har
directional decision ko ek REAL option trade ki tarah simulate karta
hai (ATM option kharida, agle din close pe becha) aur rupees mein P&L
nikalta hai.

Flow (ek trade):
    decision BUY  -> ATM CE kharido (aaj ke close pe)
    decision SELL -> ATM PE kharido
    exit          -> agle din ke close pe becho
    premium       -> Black-Scholes se (IV = India VIX, ya fallback)
    T             -> weekly expiry tak ke din (exit pe 1 din kam)
    cost          -> slippage (% of premium) + flat brokerage, dono side

⚠️ HONESTY GUARDRAILS — ye SIMULATED premiums hain, real option-chain
quotes nahi. Isliye ye numbers "indicative" hain, "actual" nahi:

1. IV — hum India VIX (index-level IV) ko har strike pe laga rahe hain.
   Real chain mein har strike ka apna IV hota hai (skew/smile), aur
   ATM option ka IV aksar VIX se alag hota hai.
2. IV DYNAMICS — entry aur exit, dono par us waqt ka India VIX use
   hota hai, isliye VIX ka asli move P&L mein aata hai. Par VIX daily
   aur 30-din ka index-level IV hai: weekly ATM option ka event-ke-baad
   wala IV CRUSH usse poora capture nahi hota. Isliye `iv_crush_pct`
   ek explicit assumption hai (exit IV pe % haircut) aur report ek
   sensitivity table chhapti hai — number ko ek aankda nahi, ek range
   ki tarah padho.
3. NO INTRADAY PATH — sirf close-to-close. Beech mein stop-loss hit
   hua ya target — kuch pata nahi chalta.
4. NO REAL LIQUIDITY — bid-ask spread ek flat % maan liya gaya hai;
   real mein OTM/illiquid strikes mein ye bahut zyada ho sakta hai.
5. DIVIDEND/carry ignore, European-style pricing (index options ke
   liye theek hai, stock options ke liye nahi).

Matlab: agar yahan bhi P&L negative aata hai, to real mein aur bura
hoga. Aur positive aane ka matlab bhi "profitable system" nahi —
sirf "aage paper trading test karne layak" hai.
"""

from __future__ import annotations

import logging
import math

import pandas as pd

logger = logging.getLogger("tiger_brain.backtest.options_sim")

# --- Simulation defaults (NIFTY index options ke hisaab se) ---
DEFAULT_STRIKE_STEP = 50  # NIFTY strikes 50 ke multiple mein
DEFAULT_LOT_SIZE = 75  # NIFTY lot (exchange badalta rehta hai — check karo)
DEFAULT_RISK_FREE_RATE = 0.065  # ~6.5% annual
DEFAULT_FALLBACK_IV_PCT = 14.0  # VIX na mile to ye maan lo
DEFAULT_EXPIRY_WEEKDAY = 3  # 0=Mon ... 3=Thu (NIFTY weekly expiry)
DEFAULT_SLIPPAGE_PCT = 1.0  # premium ka %, har side pe
DEFAULT_BROKERAGE_PER_ORDER = 20.0  # flat ₹, entry + exit dono pe
DEFAULT_IV_CRUSH_PCT = 0.0  # exit IV pe % haircut (0 = sirf VIX ka asli move)
IV_CRUSH_SENSITIVITY_LEVELS = (0.0, 5.0, 10.0, 20.0)
MIN_IV = 0.01  # 1% se neeche IV na realistic hai na numerically stable

MIN_MEANINGFUL_TRADES = 20
MIN_T_YEARS = 0.5 / 365  # expiry-day pe T=0 se divide-by-zero na ho


def _norm_cdf(x: float) -> float:
    """Standard normal CDF — scipy ke bina (erf se)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_price(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    option_type: str,
    rate: float = DEFAULT_RISK_FREE_RATE,
) -> float:
    """
    European option ka theoretical premium (Black-Scholes).

    Args:
        spot: underlying price
        strike: strike price
        t_years: expiry tak ka time, SAALON mein (5 din = 5/365)
        iv: implied volatility as decimal (14% = 0.14)
        option_type: 'CE' ya 'PE'
        rate: risk-free rate (decimal)

    Returns:
        premium (float, per unit — lot size se multiply karna hoga)
    """
    if option_type not in ("CE", "PE"):
        raise ValueError(f"option_type 'CE' ya 'PE' hona chahiye, mila: {option_type}")

    if t_years <= 0:
        # Expiry pe option ki koi time value nahi bachti — sirf intrinsic
        intrinsic = spot - strike if option_type == "CE" else strike - spot
        return max(intrinsic, 0.0)

    t_years = max(t_years, MIN_T_YEARS)
    if iv <= 0 or spot <= 0 or strike <= 0:
        # Degenerate input — sirf intrinsic value lauta do
        intrinsic = spot - strike if option_type == "CE" else strike - spot
        return max(intrinsic, 0.0)

    d1 = (math.log(spot / strike) + (rate + 0.5 * iv**2) * t_years) / (
        iv * math.sqrt(t_years)
    )
    d2 = d1 - iv * math.sqrt(t_years)
    discount = math.exp(-rate * t_years)

    if option_type == "CE":
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def atm_strike(spot: float, step: int = DEFAULT_STRIKE_STEP) -> float:
    """Spot ke sabse nazdeek wala tradable strike."""
    if step <= 0:
        raise ValueError(f"strike step 0 se bada hona chahiye, mila: {step}")
    return round(spot / step) * step


def days_to_expiry(date, expiry_weekday: int = DEFAULT_EXPIRY_WEEKDAY) -> int:
    """
    Agli weekly expiry tak kitne calendar din bache hain.

    Expiry-day pe 0 nahi, 1 lauta rahe hain — kyunki us din bhi option
    ka kuch time value hota hai (aur T=0 pricing todta hai). Ye holiday
    shifts (expiry Wednesday ho jaana) handle nahi karta — ek known
    approximation hai.
    """
    if not 0 <= expiry_weekday <= 6:
        raise ValueError(
            f"expiry_weekday 0-6 mein hona chahiye, mila: {expiry_weekday}"
        )
    days_ahead = (expiry_weekday - date.weekday()) % 7
    return days_ahead if days_ahead > 0 else 7


def _strip_tz(index: pd.Index) -> pd.Index:
    """
    Timezone hata deta hai taaki price index aur VIX index match karein.

    Angel One / saved CSV ka index tz-aware ho sakta hai jabki VIX loader
    naive index deta hai — bina iske har lookup chupchaap fail hota aur
    saare trades fallback IV pe price hote.
    """
    if isinstance(index, pd.DatetimeIndex) and index.tz is not None:
        return index.tz_localize(None)
    return index


def _normalise_vix(vix_series: pd.Series) -> pd.Series:
    if vix_series is None:
        return None
    normalised = vix_series.copy()
    normalised.index = _strip_tz(normalised.index)
    return normalised


def _iv_for_date(vix_series: pd.Series, date, fallback_pct: float) -> float:
    """India VIX (percent) ko decimal IV mein badalta hai, warna fallback.

    `vix_series` ka index tz-naive hona chahiye (`_normalise_vix` dekhein).
    """
    if vix_series is not None:
        key = pd.Timestamp(date)
        if key.tz is not None:
            key = key.tz_localize(None)
        # Intraday bars ka exact timestamp daily VIX index mein nahi hota —
        # tab us din ki midnight key try karte hain, warna har intraday
        # trade chupchaap fallback IV pe price hota
        for candidate in (key, key.normalize()):
            if candidate in vix_series.index:
                value = vix_series.loc[candidate]
                if isinstance(value, pd.Series):
                    value = value.iloc[0]
                if pd.notna(value) and float(value) > 0:
                    return float(value) / 100.0
    return fallback_pct / 100.0


def simulate_trade_log(
    df: pd.DataFrame,
    trade_log: list,
    vix_series: pd.Series = None,
    lot_size: int = DEFAULT_LOT_SIZE,
    strike_step: int = DEFAULT_STRIKE_STEP,
    expiry_weekday: int = DEFAULT_EXPIRY_WEEKDAY,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    brokerage_per_order: float = DEFAULT_BROKERAGE_PER_ORDER,
    fallback_iv_pct: float = DEFAULT_FALLBACK_IV_PCT,
    iv_crush_pct: float = DEFAULT_IV_CRUSH_PCT,
) -> dict:
    """
    MAIN ENTRY POINT — backtest ke trade_log ko option trades mein
    badalke rupee P&L nikalta hai.

    Har trade: aaj close pe 1 lot ATM option (BUY->CE, SELL->PE),
    agle din close pe exit. Entry aur exit dono pe slippage +
    brokerage lagta hai, aur exit pe T ek din kam hota hai — isliye
    theta apne aap P&L mein aa jaata hai.

    Args:
        df: poora OHLCV data (trade_log ke 'date_index' isi pe point karte hain)
        trade_log: engine.backtest_range() ka trade_log
        vix_series: India VIX (percent), df ke index pe aligned
        lot_size / strike_step / expiry_weekday: instrument ki settings
        slippage_pct: premium ka % jo har side pe kharch maana jaaye
        brokerage_per_order: flat cost per order (entry aur exit alag)
        fallback_iv_pct: jab us din ka VIX na mile
        iv_crush_pct: exit IV pe % haircut (0-100) — VIX se upar ka extra
                      crush jo weekly ATM option event ke baad khaata hai

    Returns:
        dict:
            'trades': per-trade dicts (premium in/out, pnl, correct?)
            'total_pnl' / 'gross_pnl' / 'total_costs': float (₹)
            'win_rate_pct', 'avg_win', 'avg_loss', 'profit_factor',
            'expectancy', 'max_drawdown': summary stats
            'directional_accuracy_pct': comparison ke liye
            'warnings': list[str] — honest limitations + small sample
    """
    if lot_size <= 0:
        raise ValueError(f"lot_size 0 se bada hona chahiye, mila: {lot_size}")
    if strike_step <= 0:
        raise ValueError(f"strike_step 0 se bada hona chahiye, mila: {strike_step}")
    if not 0 <= expiry_weekday <= 6:
        raise ValueError(
            f"expiry_weekday 0-6 mein hona chahiye, mila: {expiry_weekday}"
        )
    if not 0 <= iv_crush_pct < 100:
        raise ValueError(
            f"iv_crush_pct 0 se 100 ke beech hona chahiye, mila: {iv_crush_pct}"
        )

    vix_series = _normalise_vix(vix_series)
    trades = []
    equity_curve = []
    running = 0.0

    for entry in trade_log:
        # 'date' se resolve karte hain, 'date_index' se nahi — split mode ka
        # trade log sliced df pe bana hota hai, uske indexes yahan match nahi karte
        if entry["date"] not in df.index:
            logger.warning(f"{entry['date']} data mein nahi mila — trade skip.")
            continue
        i = df.index.get_loc(entry["date"])
        if i + 1 >= len(df):
            continue

        entry_date = df.index[i]
        exit_date = df.index[i + 1]
        spot_in = float(df["close"].iloc[i])
        spot_out = float(df["close"].iloc[i + 1])

        option_type = "CE" if entry["decision"] == "BUY" else "PE"
        strike = atm_strike(spot_in, strike_step)
        iv_in = _iv_for_date(vix_series, entry_date, fallback_iv_pct)
        # Exit pe us waqt ka apna IV — VIX ka asli move ab P&L mein aata
        # hai; uske upar crush ek explicit assumption hai
        iv_out = max(
            _iv_for_date(vix_series, exit_date, fallback_iv_pct)
            * (1 - iv_crush_pct / 100),
            MIN_IV,
        )

        dte_in = days_to_expiry(entry_date, expiry_weekday)
        # Holding period asli timestamps se — daily bars pe ye 1 (ya
        # weekend pe 3) din hai, intraday bars pe din ka ek hissa
        holding_days = max(
            (pd.Timestamp(exit_date) - pd.Timestamp(entry_date)).total_seconds()
            / 86400.0,
            0.0,
        )
        dte_out = max(dte_in - holding_days, 0.0)

        premium_in = black_scholes_price(
            spot_in, strike, dte_in / 365, iv_in, option_type
        )
        premium_out = black_scholes_price(
            spot_out, strike, dte_out / 365, iv_out, option_type
        )

        slippage = (premium_in + premium_out) * (slippage_pct / 100) * lot_size
        costs = slippage + 2 * brokerage_per_order
        gross = (premium_out - premium_in) * lot_size
        net = gross - costs

        running += net
        equity_curve.append(running)

        trades.append({
            "entry_date": entry_date,
            "exit_date": exit_date,
            "decision": entry["decision"],
            "option_type": option_type,
            "strike": strike,
            "iv_pct": round(iv_in * 100, 2),
            "iv_out_pct": round(iv_out * 100, 2),
            "days_to_expiry": dte_in,
            "holding_days": round(holding_days, 4),
            "premium_in": round(premium_in, 2),
            "premium_out": round(premium_out, 2),
            "gross_pnl": round(gross, 2),
            "costs": round(costs, 2),
            "net_pnl": round(net, 2),
            "direction_correct": entry["correct"],
        })

    return _summarise(trades, equity_curve, vix_series, iv_crush_pct)


def crush_sensitivity(
    df: pd.DataFrame,
    trade_log: list,
    levels: tuple = IV_CRUSH_SENSITIVITY_LEVELS,
    **kwargs,
) -> list:
    """
    Wahi trades alag-alag IV-crush assumptions pe.

    Real option-chain data ke bina hum sach mein nahi jaante ki crush
    kitna tha, isliye ek akela P&L number bharosemand nahi — ye table
    dikhata hai ki result us assumption pe kitna tikka hai.

    Returns: list of
        {'iv_crush_pct', 'total_pnl', 'profit_factor', 'win_rate_pct'}
    """
    rows = []
    for level in levels:
        result = simulate_trade_log(df, trade_log, iv_crush_pct=level, **kwargs)
        rows.append({
            "iv_crush_pct": level,
            "total_pnl": result["total_pnl"],
            "profit_factor": result["profit_factor"],
            "win_rate_pct": result["win_rate_pct"],
        })
    return rows


def _summarise(
    trades: list, equity_curve: list, vix_series,
    iv_crush_pct: float = DEFAULT_IV_CRUSH_PCT,
) -> dict:
    warnings = [
        "Ye SIMULATED option premiums hain (Black-Scholes + India VIX), "
        "real option-chain quotes nahi — module docstring mein poori list hai.",
    ]
    if iv_crush_pct > 0:
        warnings.append(
            f"Exit IV pe {iv_crush_pct}% crush maana gaya hai — ye ek "
            f"assumption hai, mapa hua number nahi. Sensitivity table dekho."
        )
    else:
        warnings.append(
            "IV ab entry/exit dono pe us waqt ke VIX se aata hai, par uske "
            "upar koi crush nahi maana (--iv-crush-pct 0). Weekly ATM option "
            "pe event ke baad ka crush VIX se bada hota hai, isliye ye result "
            "OPTIMISTIC side pe hai."
        )

    if vix_series is None:
        warnings.append(
            f"India VIX nahi mila — har trade pe flat {DEFAULT_FALLBACK_IV_PCT}% IV "
            f"maana gaya. Ye sabse kamzor assumption hai is run mein."
        )

    if not trades:
        return {
            "trades": [], "iv_crush_pct": iv_crush_pct,
            "total_pnl": 0.0, "gross_pnl": 0.0, "total_costs": 0.0,
            "win_rate_pct": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "profit_factor": 0.0, "expectancy": 0.0, "max_drawdown": 0.0,
            "directional_accuracy_pct": 0.0,
            "warnings": [
                *warnings,
                "Ek bhi trade simulate nahi hua — koi conclusion nahi.",
            ],
        }

    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] <= 0]
    total_pnl = sum(t["net_pnl"] for t in trades)
    gross_pnl = sum(t["gross_pnl"] for t in trades)
    total_costs = sum(t["costs"] for t in trades)
    gross_loss = abs(sum(losses))

    peak = 0.0
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)

    correct = sum(1 for t in trades if t["direction_correct"])

    if len(trades) < MIN_MEANINGFUL_TRADES:
        warnings.append(
            f"Sirf {len(trades)} simulated trades — {MIN_MEANINGFUL_TRADES} se kam. "
            f"P&L chahe accha dikhe ya bura, itne kam samples pe conclusion mat nikalo."
        )

    directional_accuracy = round(correct / len(trades) * 100, 1)
    win_rate = round(len(wins) / len(trades) * 100, 1)
    if directional_accuracy - win_rate >= 10:
        warnings.append(
            f"Direction {directional_accuracy}% baar sahi tha par paise sirf "
            f"{win_rate}% trades mein bane — yahi theta + cost ka asar hai. "
            f"Accuracy ko profitability mat samjho."
        )

    return {
        "trades": trades,
        "iv_crush_pct": iv_crush_pct,
        "total_pnl": round(total_pnl, 2),
        "gross_pnl": round(gross_pnl, 2),
        "total_costs": round(total_costs, 2),
        "win_rate_pct": win_rate,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": (
            round(sum(wins) / gross_loss, 2)
            if gross_loss > 0
            else (float("inf") if wins else 0.0)
        ),
        "expectancy": round(total_pnl / len(trades), 2),
        "max_drawdown": round(max_dd, 2),
        "directional_accuracy_pct": directional_accuracy,
        "warnings": warnings,
    }


def print_options_report(result: dict, lot_size: int = DEFAULT_LOT_SIZE):
    """Human-readable options P&L report, saari warnings ke saath."""
    print("\n" + "=" * 66)
    print("OPTIONS P&L SIMULATION (1 lot ATM, close-to-close)")
    print("=" * 66)
    print(f"Simulated trades       : {len(result['trades'])} (lot size {lot_size})")
    print(f"Net P&L                : ₹{result['total_pnl']:,.2f}")
    print(f"  gross                : ₹{result['gross_pnl']:,.2f}")
    print(f"  costs (slip+broker)  : ₹{result['total_costs']:,.2f}")
    print(f"Win rate (paise banne) : {result['win_rate_pct']}%")
    print(f"Directional accuracy   : {result['directional_accuracy_pct']}%")
    print(
        f"Avg win / avg loss     : ₹{result['avg_win']:,.2f} / "
        f"₹{result['avg_loss']:,.2f}"
    )
    print(f"Profit factor          : {result['profit_factor']}")
    print(f"Expectancy per trade   : ₹{result['expectancy']:,.2f}")
    print(f"Max drawdown           : ₹{result['max_drawdown']:,.2f}")

    print("\n" + "-" * 66)
    for warning in result["warnings"]:
        print(f"⚠️ {warning}")
    print("=" * 66 + "\n")
