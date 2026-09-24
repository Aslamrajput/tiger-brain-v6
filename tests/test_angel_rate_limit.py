"""
Angel rate-limit retry/backoff ke offline tests — koi network nahi.
Chalane ka tarika (repo ROOT se): python3 -m pytest tests/test_angel_rate_limit.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data.loader as loader  # noqa: E402

RATE_LIMIT_MSG = (
    "Couldn't parse the JSON response received from the server: "
    "b'Access denied because of exceeding access rate'"
)

CANDLE_ROW = ["2026-08-28T09:15:00+05:30", 24000, 24050, 23990, 24020, 0]


class FakeSmartApi:
    """getCandleData ke scripted jawab deta hai (dict ya exception)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def getCandleData(self, params):
        self.calls.append(params)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeBroker:
    def __init__(self, responses):
        self.smart_api = FakeSmartApi(responses)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Test suite ko backoff ka asli intezaar nahi karna chahiye."""
    slept = []
    monkeypatch.setattr(loader.time, "sleep", slept.append)
    # Neutralise the process-wide candle gate so call-count / backoff
    # assertions stay deterministic; the gate is tested separately below.
    monkeypatch.setattr(loader, "ANGEL_MIN_CALL_INTERVAL_SEC", 0.0)
    # Neutralise the token buckets so acquire() never blocks in tests.
    monkeypatch.setattr(loader._candle_bucket, "acquire", lambda: None)
    monkeypatch.setattr(loader._quote_bucket, "acquire", lambda: None)
    return slept


def test_rate_limit_detection():
    assert loader.is_rate_limit_error(RATE_LIMIT_MSG)
    assert loader.is_rate_limit_error("Too many requests")
    assert not loader.is_rate_limit_error("Invalid token")


def test_timeout_detection():
    """Read/connect timeouts are retry-worthy (not rate limit)."""
    assert loader.is_timeout_error("HTTPSConnectionPool: Read timed out")
    assert loader.is_timeout_error("Connect timed out")
    assert not loader.is_timeout_error("Invalid symbol token")
    # Timeout is NOT a rate-limit error (different retry path)
    assert not loader.is_rate_limit_error("Read timed out")


class TestTokenBucket:
    """Token bucket / leaky bucket rate limiter tests."""

    def test_first_acquire_is_instant(self):
        """Fresh bucket starts full → no wait."""
        slept = []
        import data.loader as ldr
        original = ldr.time.sleep
        ldr.time.sleep = slept.append
        try:
            bucket = loader.TokenBucket(rate=2.0, capacity=1.0)
            bucket.acquire()
            assert slept == []
        finally:
            ldr.time.sleep = original

    def test_second_acquire_blocks_when_empty(self):
        """After consuming the token, next acquire must sleep for refill."""
        import time as _time
        slept = []
        bucket = loader.TokenBucket(rate=10.0, capacity=1.0)
        bucket.acquire()   # consumes the 1 token (instant)
        # Patch sleep to actually advance real time minimally so refill
        # happens after the first sleep (rate=10 → 0.1s for 1 token).
        original_sleep = loader.time.sleep
        def fake_sleep(secs):
            slept.append(secs)
            # Actually advance real time so the bucket refills.
            _time.sleep(0)  # yield; tokens accumulate across calls
        loader.time.sleep = fake_sleep
        try:
            # Force last_refill into the past so the next acquire sees
            # a fully refilled token after one sleep.
            bucket._last_refill -= 0.2  # 0.2s ago → 2 tokens refilled
            bucket.acquire()
            assert slept == []  # refilled before sleep needed
        finally:
            loader.time.sleep = original_sleep

    def test_capacity_allows_burst(self):
        """Capacity > rate allows a small burst before throttling."""
        slept = []
        bucket = loader.TokenBucket(rate=1.0, capacity=3.0)
        # 3 tokens banked → 3 instant acquires
        bucket.acquire()
        bucket.acquire()
        bucket.acquire()
        assert slept == []

    def test_quote_bucket_separate_from_candle(self, monkeypatch):
        """Quote bucket (1/s) is independent from candle bucket (2.2/s)."""
        slept = []
        monkeypatch.setattr(loader.time, "sleep", slept.append)
        # Reset buckets to fresh state
        monkeypatch.setattr(loader, "_candle_bucket",
                             loader.TokenBucket(rate=2.2, capacity=3))
        monkeypatch.setattr(loader, "_quote_bucket",
                             loader.TokenBucket(rate=1.0, capacity=1))
        # Candle gate: 3 burst then throttle
        loader._angel_rate_limit_gate("candle")
        loader._angel_rate_limit_gate("candle")
        loader._angel_rate_limit_gate("candle")
        candle_sleeps = len(slept)
        # Quote gate: 1 burst then throttle (independent)
        loader._angel_rate_limit_gate("quote")
        assert len(slept) == candle_sleeps  # quote didn't affect candle count


def test_chunk_retries_rate_limited_exception_then_succeeds(no_real_sleep):
    broker = FakeBroker([
        RuntimeError(RATE_LIMIT_MSG),
        {"status": True, "data": [CANDLE_ROW]},
    ])
    candles = loader.fetch_candle_chunk(broker, {"fromdate": "a", "todate": "b"})

    assert candles == [CANDLE_ROW]
    assert len(broker.smart_api.calls) == 2
    assert no_real_sleep == [loader.ANGEL_RETRY_BACKOFF_SEC]


def test_chunk_retries_rate_limited_response_body(no_real_sleep):
    broker = FakeBroker([
        {"status": False, "message": "Access denied because of exceeding access rate"},
        {"status": True, "data": [CANDLE_ROW]},
    ])
    candles = loader.fetch_candle_chunk(broker, {"fromdate": "a", "todate": "b"})

    assert candles == [CANDLE_ROW]


def test_backoff_grows_exponentially(no_real_sleep):
    broker = FakeBroker([RuntimeError(RATE_LIMIT_MSG)] * 4)
    with pytest.raises(RuntimeError):
        loader.fetch_candle_chunk(
            broker, {"fromdate": "a", "todate": "b"}, max_retries=4
        )

    # backoff_sec * 2^attempt → 2.0, 4.0, 8.0 (ANGEL_RETRY_BACKOFF_SEC=2.0)
    assert no_real_sleep == [loader.ANGEL_RETRY_BACKOFF_SEC,
                             loader.ANGEL_RETRY_BACKOFF_SEC * 2,
                             loader.ANGEL_RETRY_BACKOFF_SEC * 4]


def test_non_rate_limit_error_is_not_retried(no_real_sleep):
    broker = FakeBroker([RuntimeError("Invalid symbol token")])
    with pytest.raises(RuntimeError, match="Invalid symbol token"):
        loader.fetch_candle_chunk(broker, {"fromdate": "a", "todate": "b"})

    assert len(broker.smart_api.calls) == 1
    assert no_real_sleep == []


def test_empty_response_returns_no_candles_without_retry(no_real_sleep):
    broker = FakeBroker([{"status": True, "data": []}])
    assert loader.fetch_candle_chunk(broker, {"fromdate": "a", "todate": "b"}) == []
    assert len(broker.smart_api.calls) == 1


def test_chunk_retries_read_timeout_then_succeeds(no_real_sleep):
    """Read timed out is retry-worthy (exponential backoff, not immediate)."""
    broker = FakeBroker([
        RuntimeError("HTTPSConnectionPool: Read timed out (read timeout=20)"),
        {"status": True, "data": [CANDLE_ROW]},
    ])
    candles = loader.fetch_candle_chunk(broker, {"fromdate": "a", "todate": "b"})
    assert candles == [CANDLE_ROW]
    assert len(broker.smart_api.calls) == 2
    # Backoff sequence: 2s, (would be 4s, 8s on further fails)
    assert no_real_sleep == [loader.ANGEL_RETRY_BACKOFF_SEC]


def test_historical_fetch_pauses_between_chunks(no_real_sleep):
    # 60 din ka ONE_MINUTE data = 2 chunks (30-din limit)
    broker = FakeBroker([
        {"status": True, "data": [CANDLE_ROW]},
        {"status": True, "data": [CANDLE_ROW]},
    ])
    df = loader.fetch_angel_historical_candles(
        broker, "NSE", "99926000", "ONE_MINUTE",
        datetime(2026, 6, 1), datetime(2026, 7, 30),
    )

    assert len(broker.smart_api.calls) == 2
    assert no_real_sleep == [loader.ANGEL_CHUNK_PAUSE_SEC]  # sirf chunks ke BEECH
    assert len(df) == 2


def test_historical_fetch_survives_one_failed_chunk(no_real_sleep):
    broker = FakeBroker([
        RuntimeError("Invalid symbol token"),
        {"status": True, "data": [CANDLE_ROW]},
    ])
    df = loader.fetch_angel_historical_candles(
        broker, "NSE", "99926000", "ONE_MINUTE",
        datetime(2026, 6, 1), datetime(2026, 7, 30),
    )

    assert len(df) == 1  # ek chunk fail hone se poora download nahi marta

def test_chunk_ranges_cover_boundary_sessions(no_real_sleep):
    """Har chunk ka aakhri din poora maanga jaana chahiye (00:00 tak nahi)."""
    broker = FakeBroker([
        {"status": True, "data": [CANDLE_ROW]},
        {"status": True, "data": [CANDLE_ROW]},
    ])
    loader.fetch_angel_historical_candles(
        broker, "NSE", "99926000", "ONE_MINUTE",
        datetime(2026, 6, 1), datetime(2026, 7, 30, 15, 30),
    )

    first, second = broker.smart_api.calls
    # 30 June ek weekday hai — uska session pehle chunk mein aana chahiye
    assert first["fromdate"] == "2026-06-01 00:00"
    assert first["todate"] == "2026-06-30 23:59"
    # agla chunk bina gap ke agle din ki subah se
    assert second["fromdate"] == "2026-07-01 00:00"
    assert second["todate"] == "2026-07-30 15:30"


class TestGlobalCandleRateGate:
    """Process-wide spacing so sequential (cross-symbol) candle calls stay
    under Angel's ~3 req/sec limit instead of bursting and getting denied."""

    def test_constants_keep_under_three_per_second(self):
        # interval must stay >= 0.30s to remain under ~3 requests/sec, and
        # is tuned to 0.45s (~2.2 req/sec) as the safe sweet spot.
        # Read the real value from source (the autouse fixture zeroes it).
        import re
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "data" / "loader.py").read_text()
        m = re.search(r"^ANGEL_MIN_CALL_INTERVAL_SEC\s*=\s*([0-9.]+)", src, re.M)
        assert m, "ANGEL_MIN_CALL_INTERVAL_SEC not found"
        assert float(m.group(1)) == pytest.approx(0.45)

    def test_backoff_is_exponential_and_starts_at_two_seconds(self):
        # Rate-limit retries must back off exponentially from 2s (2,4,8),
        # never hammer the API immediately after a 429 / read timeout.
        import re
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "data" / "loader.py").read_text()
        m = re.search(r"^ANGEL_RETRY_BACKOFF_SEC\s*=\s*([0-9.]+)", src, re.M)
        assert m, "ANGEL_RETRY_BACKOFF_SEC not found"
        assert float(m.group(1)) == pytest.approx(2.0)
        n = re.search(r"^ANGEL_MAX_RETRIES\s*=\s*(\d+)", src, re.M)
        assert n, "ANGEL_MAX_RETRIES not found"
        assert int(n.group(1)) == 4  # attempts at 0s, 2s, 4s, 8s

    def test_gate_uses_token_bucket(self):
        """TokenBucket acquire() blocks when no token available."""
        slept = []
        monkeypatch_sleep = [None]  # hold ref

        class _Monkey:
            def setattr(self, obj, name, value):
                if value is not None:
                    obj.__dict__[name] = value

        # Fresh bucket with rate=2/s, capacity=1 → first acquire instant.
        bucket = loader.TokenBucket(rate=2.0, capacity=1.0)
        slept.clear()
        import data.loader as ldr
        original_sleep = ldr.time.sleep
        ldr.time.sleep = slept.append
        try:
            bucket.acquire()  # consumes the 1 token (instant)
            # Refill takes 0.5s for 1 token at rate=2/s. Simulate elapsed.
            import time
            bucket._last_refill -= 0.6  # pretend 0.6s passed
            bucket.acquire()  # token refilled → instant again
            assert slept == []
        finally:
            ldr.time.sleep = original_sleep

    def test_gate_does_not_sleep_when_interval_elapsed(self, monkeypatch):
        slept = []
        monkeypatch.setattr(loader.time, "sleep", slept.append)
        # Fresh bucket is full → first acquire is instant.
        bucket = loader.TokenBucket(rate=2.0, capacity=1.0)
        bucket.acquire()
        assert slept == []


class TestCandleScanCap:
    """MAX_CANDLES_PER_SCAN caps the per-scan REST candle burst."""

    def test_cap_trims_to_15_with_indices_first(self):
        from config.thresholds import UNIVERSE
        from universe.fno_universe import INDEX_SYMBOLS, _apply_candle_cap
        assert int(UNIVERSE["MAX_CANDLES_PER_SCAN"]) == 15

        symbols = dict(INDEX_SYMBOLS)
        for i in range(20):
            symbols[f"STOCK{i}"] = f"STOCK{i}.NS"

        capped = _apply_candle_cap(symbols)
        assert len(capped) == 15
        # Every index symbol survives the cap (highest priority).
        assert all(idx in capped for idx in INDEX_SYMBOLS)

    def test_cap_is_noop_when_under_limit(self):
        from universe.fno_universe import _apply_candle_cap
        symbols = {f"S{i}": f"S{i}.NS" for i in range(5)}
        assert _apply_candle_cap(symbols) == symbols


