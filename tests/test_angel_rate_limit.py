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
    return slept


def test_rate_limit_detection():
    assert loader.is_rate_limit_error(RATE_LIMIT_MSG)
    assert loader.is_rate_limit_error("Too many requests")
    assert not loader.is_rate_limit_error("Invalid token")


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

    # backoff_sec * 2^attempt → 3.0, 6.0, 12.0 (ANGEL_RETRY_BACKOFF_SEC=3.0)
    assert no_real_sleep == [3.0, 6.0, 12.0]


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
        # interval must be >= 0.34s to stay under ~3 requests/sec.
        # Read the real value from source (the autouse fixture zeroes it).
        import re
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "data" / "loader.py").read_text()
        m = re.search(r"^ANGEL_MIN_CALL_INTERVAL_SEC\s*=\s*([0-9.]+)", src, re.M)
        assert m, "ANGEL_MIN_CALL_INTERVAL_SEC not found"
        assert float(m.group(1)) >= 0.34

    def test_gate_spaces_consecutive_calls(self, monkeypatch):
        slept = []
        monkeypatch.setattr(loader.time, "sleep", slept.append)
        monkeypatch.setattr(loader, "ANGEL_MIN_CALL_INTERVAL_SEC", 0.5)
        monkeypatch.setattr(loader, "_angel_last_call_ts", 0.0)
        # First call records a slot without waiting (no prior call).
        loader._angel_rate_limit_gate()
        assert slept == []
        # Second call immediately after must wait ~the full interval.
        loader._angel_rate_limit_gate()
        assert slept and slept[-1] == pytest.approx(0.5, abs=0.2)

    def test_gate_does_not_sleep_when_interval_elapsed(self, monkeypatch):
        slept = []
        monkeypatch.setattr(loader.time, "sleep", slept.append)
        monkeypatch.setattr(loader, "ANGEL_MIN_CALL_INTERVAL_SEC", 0.5)
        # Pretend last call was long ago.
        monkeypatch.setattr(loader, "_angel_last_call_ts", -1000.0)
        loader._angel_rate_limit_gate()
        assert slept == []
        monkeypatch.setattr(loader, "_angel_last_call_ts", 0.0)

