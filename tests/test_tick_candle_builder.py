"""Tests for TickCandleBuilder — per-bar volume from cumulative WS volume."""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# TickCandleBuilder needs pandas for get_1m_dataframe, but add_tick doesn't.
# Import lazily inside tests that need it.
try:
    from broker.tiger_websocket import TickCandleBuilder
    HAS_BUILDER = True
except ImportError:
    HAS_BUILDER = False


def _make_ts(minutes: int) -> datetime:
    """Timestamp at given minute of a fixed test hour."""
    return datetime(2026, 9, 16, 10, minutes, 0)


class TestTickCandleBuilderVolume:
    """Verify per-bar volume is computed correctly from cumulative WS volume."""

    def test_cumulative_volume_diffs_to_per_bar(self):
        """WS sends cumulative day volume. Builder must diff to per-bar."""
        b = TickCandleBuilder()
        # Simulate ticks: cumulative volume increases over 3 minutes
        # Minute 1: ticks with cum vol 1000, 1100, 1200 → bar vol = 200
        # Minute 2: ticks with cum vol 1300, 1500          → bar vol = 300
        # Minute 3: ticks with cum vol 1800                → bar vol = 300
        b.add_tick("T1", 100.0, 1000, _make_ts(0))
        b.add_tick("T1", 101.0, 1100, _make_ts(0))
        b.add_tick("T1", 102.0, 1200, _make_ts(0))
        b.add_tick("T1", 103.0, 1300, _make_ts(1))
        b.add_tick("T1", 104.0, 1500, _make_ts(1))
        b.add_tick("T1", 105.0, 1800, _make_ts(2))

        bars = b._candles["T1"]
        assert len(bars) == 3
        # Bar 0: cumulative went 1000→1200, but first tick sets prev_cum=1000
        #   so delta = 0 + 100 + 100 = 200
        assert bars[0]["volume"] == 200
        # Bar 1: cumulative 1300→1500, delta = 100 + 200 = 300
        assert bars[1]["volume"] == 300
        # Bar 2: cumulative 1800, delta = 300
        assert bars[2]["volume"] == 300

    def test_volume_compatible_with_rest_per_bar(self):
        """WS per-bar volume should be in same range as REST per-bar volume."""
        b = TickCandleBuilder()
        # Typical stock: ~50000 shares per 1-minute bar
        # WS cumulative: starts at 50000, increases by ~50000 per minute
        for minute in range(5):
            cum = 50000 * (minute + 1)
            b.add_tick("STOCK", 100.0, cum, _make_ts(minute))

        bars = b._candles["STOCK"]
        # First bar: delta=0 (first tick sets baseline)
        # Bars 1-4: each should have ~50000 volume (compatible with REST)
        for i in range(1, 5):
            assert bars[i]["volume"] == 50000, \
                f"Bar {i} volume {bars[i]['volume']} != 50000 (REST incompatible)"

    def test_negative_delta_clamped_to_zero(self):
        """If cumulative volume decreases (WS glitch), delta clamped to 0."""
        b = TickCandleBuilder()
        b.add_tick("T2", 100.0, 5000, _make_ts(0))
        b.add_tick("T2", 99.0, 3000, _make_ts(0))  # cumulative decreased!
        b.add_tick("T2", 101.0, 6000, _make_ts(0))
        bars = b._candles["T2"]
        assert bars[0]["volume"] >= 0  # never negative
        # 5000 → 3000: delta=0 (clamped), 3000 → 6000: delta=3000
        assert bars[0]["volume"] == 3000

    def test_ohlc_correct(self):
        """OHLC values should reflect tick prices correctly."""
        b = TickCandleBuilder()
        b.add_tick("T3", 100.0, 1000, _make_ts(0))
        b.add_tick("T3", 105.0, 1100, _make_ts(0))
        b.add_tick("T3", 98.0,  1200, _make_ts(0))
        b.add_tick("T3", 102.0, 1300, _make_ts(0))
        bars = b._candles["T3"]
        assert bars[0]["open"] == 100.0
        assert bars[0]["high"] == 105.0
        assert bars[0]["low"] == 98.0
        assert bars[0]["close"] == 102.0

    def test_new_minute_creates_new_bar(self):
        """Each new minute should start a fresh candle."""
        b = TickCandleBuilder()
        b.add_tick("T4", 100.0, 1000, _make_ts(0))
        b.add_tick("T4", 101.0, 2000, _make_ts(1))
        assert len(b._candles["T4"]) == 2

    def test_first_tick_delta_zero(self):
        """First tick for a token should have zero delta (no reference)."""
        b = TickCandleBuilder()
        b.add_tick("T5", 100.0, 5000, _make_ts(0))
        assert b._candles["T5"][0]["volume"] == 0  # no previous cumulative
