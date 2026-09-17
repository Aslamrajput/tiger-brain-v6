"""
Tiger Brain V6+V7 — ATM IV Series (KADAM 3)
==============================================
Vol-Arb sub-brain ko IV **history** chahiye (20+ points), sirf aaj ka IV
nahi. Angel do cheezein deta hai:

  * `optionGreek` — ABHI ka IV/delta/gamma/theta/vega, har strike pe.
    Live decisions ke liye perfect, par ye ek SNAPSHOT hai — history
    nahi. Isse akela IV percentile nahi ban sakta.
  * option candles — asli traded premium. Isse spot ke saath
    Black-Scholes ULTA chala kar us bar ka IV nikala ja sakta hai.

Isliye yahan dono hain: backtest ke liye candles se banaya gaya real ATM
IV series, aur live pipeline ke liye `optionGreek` snapshot.

⚠️ HONESTY NOTES:
  - Reverse Black-Scholes ka IV market ke quoted IV se thoda alag hota
    hai (dividend, discrete rate, illiquid strike ka stale close). Ye
    approximation hai — par ANUMAAN nahi: input asli traded bhaav hai.
  - Jis bar pe option candle nahi mili wahan IV `NaN` rehta hai (bhara
    nahi jaata). Vol-Arb ko tabhi series milti hai jab 20+ asli points
    ho jaayein.
  - Ye ATM IV hai. Poora smile/skew abhi nahi — sirf CE aur PE ka ATM IV
    (skew caution check ke liye utna hi chahiye).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time

import pandas as pd

try:
    from backtest.options_sim import (
        DEFAULT_RISK_FREE_RATE,
        DEFAULT_STRIKE_STEP,
        atm_strike,
        black_scholes_price,
    )
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'data/' ke andar se nahi.")

logger = logging.getLogger("tiger_brain.data.iv_series")
logging.basicConfig(level=logging.INFO)


MIN_IV = 0.005    # 0.5% — isse neeche practically zero vol
MAX_IV = 3.0      # 300% — expiry-day ke pagal quotes bhi isse neeche
IV_TOLERANCE = 1e-5
IV_MAX_ITERATIONS = 100
EXPIRY_TIME = time(15, 30)
MINUTES_PER_YEAR = 365 * 24 * 60


def implied_volatility(
    price: float,
    spot: float,
    strike: float,
    t_years: float,
    option_type: str,
    rate: float = DEFAULT_RISK_FREE_RATE,
) -> float | None:
    """
    Traded premium se IV (decimal, 0.14 = 14%) — bisection se.

    None lautata hai jab bhaav se koi IV ban hi nahi sakta (deep-ITM
    stale close jo intrinsic se bhi neeche hai, ya arbitrage-tod bhaav).
    Aisi jagah 0 ya koi default IV bhar dena poore series ko jhootha
    bana deta, isliye wo bar khaali chhodte hain.
    """
    if price is None or price <= 0 or spot <= 0 or strike <= 0 or t_years <= 0:
        return None

    low, high = MIN_IV, MAX_IV
    price_low = black_scholes_price(spot, strike, t_years, low, option_type, rate)
    price_high = black_scholes_price(spot, strike, t_years, high, option_type, rate)
    if not price_low <= price <= price_high:
        return None

    for _ in range(IV_MAX_ITERATIONS):
        mid = (low + high) / 2
        value = black_scholes_price(spot, strike, t_years, mid, option_type, rate)
        if abs(value - price) < IV_TOLERANCE:
            return mid
        if value < price:
            low = mid
        else:
            high = mid
        if high - low < IV_TOLERANCE:
            break
    return (low + high) / 2


def time_to_expiry_years(timestamp, expiry: date) -> float:
    """Bar se expiry (15:30 IST) tak ka time, saalon mein."""
    now = pd.Timestamp(timestamp).to_pydatetime()
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    minutes = (datetime.combine(expiry, EXPIRY_TIME) - now).total_seconds() / 60
    return max(minutes, 0.0) / MINUTES_PER_YEAR


def build_atm_iv_series(
    df: pd.DataFrame,
    provider,
    strike_step: int = DEFAULT_STRIKE_STEP,
    rate: float = DEFAULT_RISK_FREE_RATE,
    sample_every_bars: int = 1,
) -> pd.DataFrame:
    """
    Har (sampled) bar pe ATM CE/PE ke ASLI bhaav se IV nikaal kar series
    banata hai.

    Args:
        df: underlying OHLCV (index = bar timestamps)
        provider: `data.option_chain.AngelOptionChain` — real premiums.
                  Iske apne miss-counters isi kaam ke ho jaate hain,
                  isliye trade-pricing wale provider se ALAG instance do.
        sample_every_bars: har bar pe do option lookups mehenge hain;
                           >1 dene par beech ke bars pichhle IV se
                           forward-fill hote hain (naya data nahi banta,
                           sirf aakhri asli reading carry hoti hai).

    Returns:
        DataFrame(index=df.index, columns=[atm_iv, call_iv, put_iv]) —
        percent mein (12.5 = 12.5%), jahan IV nahi bana wahan NaN.
    """
    if sample_every_bars <= 0:
        raise ValueError("sample_every_bars 0 se bada hona chahiye")

    columns = ["atm_iv", "call_iv", "put_iv"]
    if df.empty:
        return pd.DataFrame(columns=columns, dtype="float64")

    rows = {}
    for position, timestamp in enumerate(df.index):
        if position % sample_every_bars:
            continue
        spot = float(df["close"].iloc[position])
        strike = atm_strike(spot, strike_step)

        legs = {}
        for option_type, key in (("CE", "call_iv"), ("PE", "put_iv")):
            contract = provider.contract_for(timestamp, strike, option_type)
            if contract is None:
                continue
            premium = provider.premium(timestamp, strike, option_type)
            if premium is None:
                continue
            t_years = time_to_expiry_years(
                timestamp, date.fromisoformat(contract["expiry"])
            )
            iv = implied_volatility(
                premium, spot, strike, t_years, option_type, rate
            )
            if iv is not None:
                legs[key] = iv * 100

        if not legs:
            continue
        rows[pd.Timestamp(timestamp)] = {
            "call_iv": legs.get("call_iv"),
            "put_iv": legs.get("put_iv"),
            "atm_iv": sum(legs.values()) / len(legs),
        }

    if not rows:
        return pd.DataFrame(index=df.index, columns=columns, dtype="float64")

    sampled = pd.DataFrame.from_dict(rows, orient="index")[columns].sort_index()
    if sample_every_bars == 1:
        return sampled.reindex(df.index)
    # Beech ke bars: aakhri ASLI reading carry hoti hai — future se kuch
    # nahi aata (ffill sirf peeche se aage jaata hai). Carry sirf agle
    # scheduled sample tak chalti hai: agar wo lookup fail ho gaya to
    # purani reading uske paar stale data ban kar nahi chalni chahiye.
    return sampled.reindex(df.index).ffill(limit=sample_every_bars - 1)


# ============================================================
# LIVE — optionGreek snapshot (backtest ke liye nahi)
# ============================================================

def fetch_live_greeks(broker, name: str, expiry: date) -> pd.DataFrame:
    """
    `optionGreek` ka live snapshot — har strike ka IV + Greeks.

    Returns: DataFrame [strike, option_type, iv, delta, gamma, theta,
             vega, trade_volume]. Khali DataFrame agar kuch na mile.
    """
    response = broker.smart_api.optionGreek({
        "name": name.upper(),
        "expirydate": expiry.strftime("%d%b%Y").upper(),
    })
    rows = response.get("data") or []
    if not rows:
        logger.warning(f"optionGreek khali: {response.get('message', 'unknown')}")
        return pd.DataFrame()

    frame = pd.DataFrame(rows)
    # === INDEX VOLUME FIX: optionGreek also returns openInterest +
    # totalBuyQuantity + totalSellQuantity per strike. These are NOT parsed
    # by the original 8-field extraction. For indices (NIFTY/BANKNIFTY) the
    # spot feed reports volume=0, so summed options OI + tradeVolume become
    # the REAL volume proxy for the underlying index.
    result = pd.DataFrame({
        "strike": frame["strikePrice"].astype(float),
        "option_type": frame["optionType"],
        "iv": frame["impliedVolatility"].astype(float),
        "delta": frame["delta"].astype(float),
        "gamma": frame["gamma"].astype(float),
        "theta": frame["theta"].astype(float),
        "vega": frame["vega"].astype(float),
        "trade_volume": frame["tradeVolume"].astype(float),
    })
    # OI fields are present in the API response but may be missing for some
    # underlyings — coerce to 0 so the sum never fails.
    for _oi_col, _src in [
        ("open_interest", "openInterest"),
        ("total_buy_qty", "totalBuyQuantity"),
        ("total_sell_qty", "totalSellQuantity"),
    ]:
        if _src in frame.columns:
            result[_oi_col] = frame[_src].astype(float)
        else:
            result[_oi_col] = 0.0
    return result


def live_atm_iv(
    greeks: pd.DataFrame, spot: float, strike_step: int = DEFAULT_STRIKE_STEP
) -> dict:
    """Live snapshot se ATM ka call/put IV (percent) — skew check ke liye."""
    result = {"strike": None, "call_iv": None, "put_iv": None, "atm_iv": None}
    if greeks.empty:
        return result

    strike = atm_strike(spot, strike_step)
    at_strike = greeks[greeks["strike"] == strike]
    if at_strike.empty:
        # Us strike ka greek row nahi hai — sabse nazdeek strike lo,
        # par batao ki ye exact ATM nahi hai
        nearest = (greeks["strike"] - strike).abs().idxmin()
        strike = float(greeks.loc[nearest, "strike"])
        at_strike = greeks[greeks["strike"] == strike]

    result["strike"] = strike
    values = []
    for option_type, key in (("CE", "call_iv"), ("PE", "put_iv")):
        leg = at_strike[at_strike["option_type"] == option_type]
        if leg.empty:
            continue
        iv = float(leg["iv"].iloc[0])
        if iv > 0:
            result[key] = iv
            values.append(iv)
    if values:
        result["atm_iv"] = sum(values) / len(values)
    return result
