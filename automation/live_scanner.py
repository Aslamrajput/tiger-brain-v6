"""🐅 TIGER REAL-TIME LIVE SCANNER — बैकटेस्ट से इन्डिपेंडेंट।

यह V19 का असली फिक्स है। पहले tiger_live.py → run_tiger_brain_backtest()
को कॉल करता था जो पूरा दिन सिमुलेशन चलाता था। अगर सिमुलेशन में स्कोर नहीं
बनता तो "0 entries" आता और कोई buy order नहीं।

अब यह फाइल अभी के timestamp पर सिर्फ़ latest bar देखकर 7 ब्रेन चलाती है:
  - Brain 1: brain1_intraday_pass (gate)
  - Brain 2: detect_zones + zone_touched + volume_delta (gate)
  - Brain 3: scoring pipeline (rocket momentum)
  - Brain 4: TradeCounterGuard (daily quota)
  - Brain 6: Premium Discount Tracker (IV discount)
  - Brain 7: Session Commander (session threshold)

(Brain 5 exit लाइव रनर के monitor_open_positions में है।)

असली रियल-टाइम सिग्नल — बैकटेस्ट सिमुलेशन पर निर्भर नहीं।
"""
from __future__ import annotations

import logging
from datetime import datetime, time

import pandas as pd

from backtest.run_tiger_brain_backtest import (
    find_tiger_brain_entry,
    find_tiger_brain_entry_15m,
    get_vix_for_date,
)
from backtest.tiger_session_brain import (
    get_session_score_threshold,
    should_force_hunt,
)
from universe.fno_universe import segment_of, is_expiry_day

logger = logging.getLogger(__name__)

IST = pd.Timestamp("now").tz if pd.Timestamp("now").tz else None
try:
    IST = pd.Timezone("Asia/Kolkata") if False else None
except Exception:
    IST = None


def _latest_15m_index(df_15m: pd.DataFrame, now: datetime) -> int:
    """अभी के timestamp से latest CLOSED 15m bar का index दे।

    Live trading में हम सिर्फ़ CLOSED bar देखते हैं (current forming bar
    पर सिग्नल नहीं — वो अभी बन रहा है)।
    """
    if df_15m is None or len(df_15m) == 0:
        return -1
    ts = pd.Timestamp(now)
    if df_15m.index.tz is not None and ts.tz is None:
        ts = ts.tz_localize(df_15m.index.tz)
    elif df_15m.index.tz is None and ts.tz is not None:
        ts = ts.tz_localize(None)
    closed = df_15m.index[df_15m.index <= ts]
    if len(closed) == 0:
        return len(df_15m) - 1
    return df_15m.index.get_loc(closed[-1])


def scan_live_signals(
    data_map: dict[str, pd.DataFrame],
    data_map_1m: dict[str, pd.DataFrame] | None,
    broker,
    now: datetime | None = None,
    daily_entries_taken: int = 0,
    max_entries_per_day: int = 5,
) -> list[dict]:
    """अभी के timestamp पर हर symbol के लिए 7 ब्रेन चलाएँ → live signals।

    यह बैकटेस्ट सिमुलेशन नहीं है — सिर्फ़ latest closed 15m bar पर स्कैन।
    हर symbol के लिए find_tiger_brain_entry या find_tiger_brain_entry_15m
    कॉल होता है (1m data है या नहीं उसपर निर्भर)।

    Returns:
        list[dict]: सिग्नल जो setup_score >= session_threshold हैं।
        हर dict में symbol, direction, setup_score, strike_kind, आदि होंगे।
    """
    if now is None:
        now = datetime.now()
    ts_time = now.time()
    signals: list[dict] = []

    # Brain 4: daily quota check
    if daily_entries_taken >= max_entries_per_day:
        logger.info("Live scan: daily quota full (%d/%d) — skip.",
                    daily_entries_taken, max_entries_per_day)
        return signals

    pcr_cache: dict[str, float] = {}

    for sym, df_15m in data_map.items():
        if df_15m is None or len(df_15m) < 40:
            continue

        seg = segment_of(sym)
        # Brain 7: session threshold (NSE vs MCX)
        session_thresh = get_session_score_threshold(ts_time, seg)
        if session_thresh >= 999.0:
            continue  # off-hours — इस symbol का session बंद

        # force hunt check (Brain 7)
        force_hunt, fh_thresh, _ = should_force_hunt(
            ts_time, None, seg)
        min_score = min(session_thresh, fh_thresh) if force_hunt else session_thresh

        i_15m = _latest_15m_index(df_15m, now)
        if i_15m < 40:
            continue

        expiry = is_expiry_day(sym, now)
        vix_val = get_vix_for_date(now)

        # 1m data available? → sniper path, else 15m path
        df_1m = None
        if data_map_1m and sym in data_map_1m:
            df_1m = data_map_1m[sym]

        try:
            if df_1m is not None and len(df_1m) > 0:
                setup = find_tiger_brain_entry(
                    df_15m, i_15m, df_1m, seg, expiry, sym,
                    broker, pcr_cache, vix_val,
                    min_score=min_score, force_hunt=force_hunt)
            else:
                setup = find_tiger_brain_entry_15m(
                    df_15m, i_15m, seg, expiry, sym, vix_val,
                    min_score=min_score, force_hunt=force_hunt)
        except Exception as exc:
            logger.debug("Live scan %s error: %s", sym, exc)
            continue

        if setup is None:
            continue

        score = setup.get("setup_score", 0)
        if score < min_score:
            logger.debug("Live scan %s: score %.1f < threshold %.1f — skip",
                         sym, score, min_score)
            continue

        # सिग्नल मिला — लाइव ऑर्डर के लिए तैयार
        # _place_live_orders expects entry_ts, strike, option_type,
        # entry_premium, is_delivery — backtest path adds these when
        # creating positions. Live path must add them here or the signal
        # is silently skipped (entry_ts is None → continue).
        direction = setup.get("direction", "BUY")
        is_call = direction == "BUY"
        cur_underlying = setup.get("entry_price", 0.0)
        strike_kind = setup.get("strike_kind", "ATM")
        if strike_kind == "ITM":
            strike = round(cur_underlying * 0.99) if is_call else round(cur_underlying * 1.01)
        elif strike_kind == "OTM":
            strike = round(cur_underlying * 1.01) if is_call else round(cur_underlying * 0.99)
        else:
            strike = round(cur_underlying)
        setup["symbol"] = sym
        setup["segment"] = seg
        setup["scan_time"] = now.isoformat()
        setup["entry_ts"] = now
        setup["strike"] = strike
        setup["option_type"] = "CE" if is_call else "PE"
        setup["entry_premium"] = 0.0  # real LTP fetched in _place_live_orders
        setup["is_delivery"] = False  # intraday by default
        setup["exit_ts"] = None  # एंट्री सिग्नल — अभी exit नहीं
        logger.info("🐅 LIVE SIGNAL: %s %s score=%.1f (thresh %.1f) %s",
                    sym, setup.get("direction", ""),
                    score, min_score, setup.get("score_details", ""))
        signals.append(setup)

    logger.info("Live scan done @ %s: %d signal(s) from %d symbols",
                now.strftime("%H:%M"), len(signals), len(data_map))
    return signals
