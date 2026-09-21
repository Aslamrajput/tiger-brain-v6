"""
Intraday data layer ke offline tests — koi network/broker nahi.
Chalane ka tarika (repo ROOT se): python3 -m pytest tests/test_intraday.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data.intraday as intraday  # noqa: E402
from data.intraday import (  # noqa: E402
    cache_path,
    candle_quality_report,
    candles_per_session,
    clean_intraday,
    load_cached,
    load_intraday,
    merge_candles,
    missing_ranges,
    now_ist,
    resample_candles,
    save_cache,
    trading_days_between,
)


def make_session(day: str, n: int = 75, freq: str = "5min", volume: int = 100):
    """Ek trading din ki candles (09:15 se), default 5-min × 75 = poora din."""
    index = pd.date_range(f"{day} 09:15", periods=n, freq=freq)
    return pd.DataFrame(
        {
            "open": range(1, n + 1),
            "high": range(2, n + 2),
            "low": range(0, n),
            "close": range(1, n + 1),
            "volume": [volume] * n,
        },
        index=index,
    ).astype(float)


def test_expected_candles_per_session():
    assert candles_per_session("FIVE_MINUTE") == 75
    assert candles_per_session("ONE_MINUTE") == 375
    assert candles_per_session("FIFTEEN_MINUTE") == 25


def test_clean_drops_rows_outside_market_hours():
    df = make_session("2026-06-15", n=80)  # 75 candles ke baad 15:30+ chala jaata hai
    cleaned = clean_intraday(df, "FIVE_MINUTE")
    assert len(cleaned) == 75
    assert cleaned.index[-1].strftime("%H:%M") == "15:25"


def test_clean_drops_weekends_and_holidays():
    weekend = make_session("2026-06-13", n=5)  # Saturday
    holiday = make_session("2026-01-26", n=5)  # Republic Day
    normal = make_session("2026-06-15", n=5)   # Monday
    cleaned = clean_intraday(pd.concat([weekend, holiday, normal]), "FIVE_MINUTE")
    assert len(cleaned) == 5
    assert set(cleaned.index.date) == {pd.Timestamp("2026-06-15").date()}


def test_clean_dedupes_and_sorts():
    df = make_session("2026-06-15", n=3)
    dupe = df.iloc[[0]].copy()
    dupe["close"] = 999.0
    cleaned = clean_intraday(pd.concat([df.iloc[::-1], dupe]), "FIVE_MINUTE")
    assert len(cleaned) == 3
    assert cleaned.index.is_monotonic_increasing
    assert cleaned["close"].iloc[0] == 999.0


def test_clean_strips_timezone():
    df = make_session("2026-06-15", n=3)
    df.index = df.index.tz_localize("Asia/Kolkata")
    assert clean_intraday(df, "FIVE_MINUTE").index.tz is None


def test_clean_rejects_missing_columns():
    df = make_session("2026-06-15", n=3).drop(columns=["volume"])
    with pytest.raises(ValueError, match="volume"):
        clean_intraday(df, "FIVE_MINUTE")


def test_resample_aggregates_ohlcv_correctly():
    df = make_session("2026-06-15", n=75)
    fifteen = resample_candles(df, 15)

    assert len(fifteen) == 25
    first = fifteen.iloc[0]
    assert first["open"] == df["open"].iloc[0]
    assert first["close"] == df["close"].iloc[2]
    assert first["high"] == df["high"].iloc[:3].max()
    assert first["low"] == df["low"].iloc[:3].min()
    assert first["volume"] == df["volume"].iloc[:3].sum()


def test_resample_does_not_merge_two_days_into_one_candle():
    two_days = pd.concat(
        [make_session("2026-06-15", n=75), make_session("2026-06-16", n=75)]
    )
    hourly = resample_candles(two_days, 60)

    per_day = hourly.groupby(hourly.index.normalize()).size()
    assert len(per_day) == 2
    assert set(per_day) == {7}  # 375 min / 60 → 6 poori + 1 aakhri chhoti candle
    assert all(ts.strftime("%H:%M") == "09:15" for ts in per_day.index.map(
        lambda day: hourly[hourly.index.normalize() == day].index[0]
    ))


def test_resample_rejects_bad_interval():
    with pytest.raises(ValueError):
        resample_candles(make_session("2026-06-15", n=5), 0)


def test_quality_report_counts_missing_candles():
    full = make_session("2026-06-15", n=75)
    partial = make_session("2026-06-16", n=40)
    report = candle_quality_report(pd.concat([full, partial]), "FIVE_MINUTE")

    assert report["sessions"] == 2
    assert report["expected_per_session"] == 75
    assert report["missing_candles"] == 35
    assert report["incomplete_sessions"] == [("2026-06-16", 40)]


def test_quality_report_counts_fully_missing_sessions():
    # 15 aur 17 June trading din hain; 16 ka data hai hi nahi
    df = pd.concat([make_session("2026-06-15", n=75), make_session("2026-06-17", n=75)])
    report = candle_quality_report(df, "FIVE_MINUTE")

    assert report["missing_sessions"] == ["2026-06-16"]
    assert report["missing_candles"] == 75  # poora gayab din bhi ginta hai
    assert report["incomplete_sessions"] == []


def test_quality_report_flags_missing_sessions_at_window_edges():
    """Window ke shuru/aakhir ka fail hua chunk data se dikhta hi nahi."""
    df = make_session("2026-06-16", n=75)
    report = candle_quality_report(
        df, "FIVE_MINUTE",
        datetime(2026, 6, 15), datetime(2026, 6, 17, 15, 30),
    )

    assert report["missing_sessions"] == ["2026-06-15", "2026-06-17"]
    assert report["missing_candles"] == 150


def test_quality_report_flags_fully_failed_window():
    """Poora window fail ho jaye to khaali report "sab theek" nahi dikhni chahiye."""
    empty = make_session("2026-06-16", n=0)
    report = candle_quality_report(
        empty, "FIVE_MINUTE",
        datetime(2026, 6, 15), datetime(2026, 6, 17, 15, 30),
    )

    assert report["missing_sessions"] == ["2026-06-15", "2026-06-16", "2026-06-17"]
    assert report["missing_candles"] == 225


def test_quality_report_does_not_flag_session_still_running():
    """Market ke beech chalaya gaya run aaj ka adhoora din gayab na bataye."""
    df = make_session("2026-06-16", n=12)  # 09:15 se 10:10 tak
    report = candle_quality_report(
        df, "FIVE_MINUTE",
        datetime(2026, 6, 16), datetime(2026, 6, 16, 10, 15),
    )

    assert report["missing_sessions"] == []
    assert report["incomplete_sessions"] == []
    assert report["missing_candles"] == 0


def test_quality_report_ignores_day_before_market_opens():
    """Market khulne se pehle ka run aaj ke din ko session hi na maane."""
    df = make_session("2026-06-16", n=75)
    report = candle_quality_report(
        df, "FIVE_MINUTE",
        datetime(2026, 6, 16), datetime(2026, 6, 17, 8, 30),
    )

    assert report["missing_sessions"] == []
    assert report["missing_candles"] == 0


def test_trading_days_ignores_years_without_holiday_calendar():
    # 2025 ki holiday list repo mein nahi hai — us saal ke weekday holidays
    # ko jhoota "gayab session" banane se behtar hai kuch na kehna
    days = trading_days_between(pd.Timestamp("2025-06-15"), pd.Timestamp("2025-06-20"))
    assert days == []


def test_trading_days_skips_weekend_and_holiday():
    days = trading_days_between(pd.Timestamp("2026-01-23"), pd.Timestamp("2026-01-27"))
    dates = [day.date().isoformat() for day in days]

    assert dates == ["2026-01-23", "2026-01-27"]  # 24-25 weekend, 26 Republic Day


def test_quality_report_flags_zero_volume():
    df = make_session("2026-06-15", n=10, volume=0)
    assert candle_quality_report(df, "FIVE_MINUTE")["zero_volume_pct"] == 100.0


def test_quality_report_on_empty_frame():
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    report = candle_quality_report(empty, "FIVE_MINUTE")
    assert report["rows"] == 0
    assert report["sessions"] == 0
    assert report["first"] is None


def test_merge_prefers_newer_candle_on_overlap():
    old = make_session("2026-06-15", n=3)
    new = old.iloc[[2]].copy()
    new["close"] = 555.0
    merged = merge_candles(old, new)

    assert len(merged) == 3
    assert merged["close"].iloc[-1] == 555.0


def test_cache_round_trip(tmp_path):
    df = make_session("2026-06-15", n=5)
    save_cache(df, "NIFTY", "FIVE_MINUTE", cache_dir=str(tmp_path))
    loaded = load_cached("NIFTY", "FIVE_MINUTE", cache_dir=str(tmp_path))

    pd.testing.assert_frame_equal(loaded, df, check_freq=False)


def test_cache_is_keyed_by_exchange_and_token(tmp_path):
    nifty = make_session("2026-06-15", n=5)
    other = make_session("2026-06-15", n=5)
    other["close"] = 999.0

    save_cache(nifty, "NIFTY", "FIVE_MINUTE", str(tmp_path), "NSE", "99926000")
    save_cache(other, "NIFTY", "FIVE_MINUTE", str(tmp_path), "NFO", "12345")

    assert cache_path("NIFTY", "FIVE_MINUTE", str(tmp_path), "NSE", "99926000") != \
        cache_path("NIFTY", "FIVE_MINUTE", str(tmp_path), "NFO", "12345")
    spot = load_cached("NIFTY", "FIVE_MINUTE", str(tmp_path), "NSE", "99926000")
    assert spot["close"].iloc[0] != 999.0


def test_cli_prints_report_when_nothing_downloaded(tmp_path, capsys, monkeypatch):
    """Khaali fetch pe bhi CLI bataye ki kaunse trading din maange gaye the."""
    monkeypatch.setattr(intraday, "now_ist", lambda: datetime(2026, 6, 19, 16, 0))
    exit_code = intraday.main([
        "--offline", "--interval", "FIVE_MINUTE", "--days", "5",
        "--cache-dir", str(tmp_path),
    ])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "INTRADAY DATA QUALITY" in out
    assert "GAYAB sessions" in out
    assert "Koi intraday candle nahi mili" in out


def test_cli_report_window_does_not_drift_with_clock(tmp_path, capsys, monkeypatch):
    """Fetch ke dauraan ghadi aage badhe to report nayi candles na maange."""
    ticks = iter([
        datetime(2026, 6, 19, 10, 15),  # window banti hai
        datetime(2026, 6, 19, 10, 40),  # fetch ke baad ka waqt
    ])
    monkeypatch.setattr(intraday, "now_ist", lambda: next(ticks))
    cached = pd.concat([
        make_session("2026-06-18", n=75),   # poora din
        make_session("2026-06-19", n=12),   # aaj 10:15 tak
    ])
    save_cache(cached, "NIFTY", "FIVE_MINUTE", cache_dir=str(tmp_path))

    intraday.main([
        "--offline", "--interval", "FIVE_MINUTE", "--days", "1",
        "--cache-dir", str(tmp_path),
    ])
    out = capsys.readouterr().out

    assert "Adhoore sessions" not in out
    assert "Missing candles   : 0" in out


def test_load_cached_missing_file_is_empty(tmp_path):
    assert load_cached("NIFTY", "ONE_MINUTE", cache_dir=str(tmp_path)).empty


def test_offline_mode_never_touches_network(tmp_path):
    today = pd.Timestamp.now().normalize()
    day = today - pd.Timedelta(days=1)
    # Skip weekends AND NSE holidays — cached candles on a holiday get
    # filtered out by clean_intraday, so the test must use a trading day.
    from automation.holidays import is_market_holiday
    while day.dayofweek >= 5 or is_market_holiday(day.date())[0]:
        day -= pd.Timedelta(days=1)

    df = make_session(day.strftime("%Y-%m-%d"), n=10)
    save_cache(df, "NIFTY", "FIVE_MINUTE", cache_dir=str(tmp_path))

    loaded = load_intraday(
        interval="FIVE_MINUTE", days=5, cache_dir=str(tmp_path), offline=True
    )
    assert len(loaded) == 10


def test_now_ist_is_ahead_of_utc():
    delta = now_ist() - datetime.now(timezone.utc).replace(tzinfo=None)
    assert 5.4 < delta.total_seconds() / 3600 < 5.6


def test_fetch_window_starts_at_midnight_ist(monkeypatch, tmp_path):
    """Chunk boundaries se subah ki candles na katein, isliye start 00:00 ho."""
    captured = {}

    def fake_fetch(broker, exchange, token, interval, start, end):
        captured["start"] = start
        captured["end"] = end
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    monkeypatch.setattr(intraday, "_fetch_from_angel", fake_fetch)
    load_intraday(interval="ONE_MINUTE", days=45, cache_dir=str(tmp_path))

    assert captured["start"].hour == 0 and captured["start"].minute == 0
    assert (captured["end"] - captured["start"]).days >= 45


def test_missing_ranges_backfills_history_older_than_cache():
    cached = make_session("2026-08-31")
    start = datetime(2026, 7, 1)
    end = datetime(2026, 9, 1, 15, 30)

    ranges = missing_ranges(cached, start, end)

    assert len(ranges) == 2
    assert ranges[0][0] == start                      # purana hissa backfill
    assert ranges[0][1] == datetime(2026, 9, 1)
    assert ranges[1] == (datetime(2026, 8, 31), end)  # aakhri din + tail


def test_missing_ranges_only_fetches_tail_when_cache_covers_start():
    cached = make_session("2026-08-31")
    start = datetime(2026, 8, 31)
    end = datetime(2026, 9, 1, 15, 30)

    assert missing_ranges(cached, start, end) == [(start, end)]


def test_load_intraday_backfills_older_window(monkeypatch, tmp_path):
    """20 din cache karke 60 din maango to purana hissa bhi fetch ho."""
    # Pin `end` and derive the cache day from it — otherwise the backfill gap
    # shrinks by a day every day the wall clock advances and the >=40 assertion
    # flips to 39 (brittle, date-dependent failure).
    end = datetime(2026, 9, 21, 15, 30)
    cache_day = end - timedelta(days=20)
    save_cache(make_session(cache_day.strftime("%Y-%m-%d")),
               "NIFTY", "FIVE_MINUTE", str(tmp_path))
    calls = []

    def fake_fetch(broker, exchange, token, interval, start, end):
        calls.append((start, end))
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    monkeypatch.setattr(intraday, "_fetch_from_angel", fake_fetch)
    load_intraday(interval="FIVE_MINUTE", days=60, cache_dir=str(tmp_path), end=end)

    assert len(calls) == 2
    assert (calls[0][1] - calls[0][0]).days >= 40


def test_load_intraday_validates_arguments(tmp_path):
    with pytest.raises(ValueError, match="Interval"):
        load_intraday(interval="TWO_MINUTE", cache_dir=str(tmp_path), offline=True)
    with pytest.raises(ValueError, match="days"):
        load_intraday(days=0, cache_dir=str(tmp_path), offline=True)


def test_window_end_bounded_to_a_dead_instruments_last_day():
    """Expire ho chuke contract ka window aaj tak khinchega to har run
    khaali post-expiry tail dobara maangega."""
    expiry_end = intraday.now_ist().replace(
        hour=23, minute=59, second=0, microsecond=0
    ) - timedelta(days=30)
    start, end = intraday.intraday_window(10, end=expiry_end)

    assert end == expiry_end
    assert start == (expiry_end - timedelta(days=10)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # future end abhi pe clamp hota hai
    _, clamped = intraday.intraday_window(10, end=datetime(2999, 1, 1))
    assert clamped <= intraday.now_ist()
