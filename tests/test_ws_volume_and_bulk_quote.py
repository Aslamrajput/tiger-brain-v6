"""
Tests for WS-first volume accessor + bulk quote API — no network.

Covers the 3 rate-limit fixes:
  1. WS get_day_volume() accessor (live tick cache, zero REST).
  2. AngelBroker.get_option_volume() WS-first path.
  3. AngelBroker.get_option_volumes_bulk() bulk market-data endpoint.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.tiger_websocket import TigerWebSocket  # noqa: E402

import threading


def _make_ws_with_cache(cache: dict) -> TigerWebSocket:
    """Build a TigerWebSocket with only the volume cache populated.

    Bypasses the singleton __new__ by constructing the instance and
    setting just the attributes get_day_volume() needs.
    """
    ws = object.__new__(TigerWebSocket)  # bypass singleton guard
    ws._volume_cache = cache
    ws._lock = threading.Lock()
    return ws


class TestWSVolumeAccessor:
    """get_day_volume() reads cumulative day volume from the live tick cache."""

    def test_returns_zero_when_no_tick(self):
        ws = _make_ws_with_cache({})
        assert ws.get_day_volume("99926000") == 0

    def test_returns_cached_volume(self):
        ws = _make_ws_with_cache({"99926000": 12500})
        assert ws.get_day_volume("99926000") == 12500

    def test_accepts_int_token(self):
        ws = _make_ws_with_cache({"12345": 800})
        assert ws.get_day_volume(12345) == 800


class TestGetOptionVolumeWSFirst:
    """get_option_volume() must try WS before REST."""

    def test_uses_ws_when_healthy_and_tick_present(self):
        from broker.angel_connect import AngelBroker
        broker = AngelBroker.__new__(AngelBroker)

        class FakeWS:
            def is_healthy(self):
                return True
            def get_day_volume(self, token):
                return 42000

        broker.websocket = FakeWS()
        # Should return WS volume and never call REST.
        result = broker.get_option_volume("NIFTY24SEP21500CE", "99999", "NFO")
        assert result == 42000

    def test_falls_back_to_rest_when_ws_unhealthy(self, monkeypatch):
        from broker.angel_connect import AngelBroker
        broker = AngelBroker.__new__(AngelBroker)

        class FakeWS:
            def is_healthy(self):
                return False
            def get_day_volume(self, token):
                return 0

        broker.websocket = FakeWS()

        # Stub the REST fallback.
        called = {}
        def fake_rest(ts, tok, exch):
            called["rest"] = True
            return 777
        broker._rest_option_volume = fake_rest

        result = broker.get_option_volume("NIFTY24SEP21500CE", "99999", "NFO")
        assert result == 777
        assert called.get("rest") is True

    def test_falls_back_to_rest_when_ws_volume_zero(self, monkeypatch):
        from broker.angel_connect import AngelBroker
        broker = AngelBroker.__new__(AngelBroker)

        class FakeWS:
            def is_healthy(self):
                return True
            def get_day_volume(self, token):
                return 0  # no tick yet

        broker.websocket = FakeWS()
        broker._rest_option_volume = lambda ts, tok, exch: 333
        result = broker.get_option_volume("X", "Y", "NFO")
        assert result == 333

    def test_returns_zero_when_no_websocket(self):
        from broker.angel_connect import AngelBroker
        broker = AngelBroker.__new__(AngelBroker)
        broker.websocket = None
        broker._rest_option_volume = lambda ts, tok, exch: 0
        assert broker.get_option_volume("X", "Y", "NFO") == 0


class TestBulkQuoteAPI:
    """get_option_volumes_bulk() fetches 50 symbols in ONE request."""

    def test_empty_input_returns_empty(self):
        from broker.angel_connect import AngelBroker
        broker = AngelBroker.__new__(AngelBroker)
        assert broker.get_option_volumes_bulk({}) == {}

    def test_parses_fetched_volumes(self):
        from broker.angel_connect import AngelBroker
        broker = AngelBroker.__new__(AngelBroker)
        broker.ensure_logged_in = lambda: None

        class FakeSmartApi:
            def getMarketData(self, mode, exchangeTokens):
                return {
                    "data": {
                        "fetched": [
                            {"symbolToken": "111", "tradeVolume": 500},
                            {"symbolToken": "222", "tradeVolume": 1200},
                            {"symbolToken": "333", "volume": 300},
                        ]
                    }
                }
        broker.smart_api = FakeSmartApi()
        result = broker.get_option_volumes_bulk({"NFO": ["111", "222", "333"]})
        assert result == {"111": 500, "222": 1200, "333": 300}

    def test_returns_empty_on_api_failure(self):
        from broker.angel_connect import AngelBroker
        broker = AngelBroker.__new__(AngelBroker)
        broker.ensure_logged_in = lambda: None

        class FakeSmartApi:
            def getMarketData(self, mode, exchangeTokens):
                raise RuntimeError("connection failed")
        broker.smart_api = FakeSmartApi()
        assert broker.get_option_volumes_bulk({"NFO": ["111"]}) == {}

    def test_returns_empty_when_no_data(self):
        from broker.angel_connect import AngelBroker
        broker = AngelBroker.__new__(AngelBroker)
        broker.ensure_logged_in = lambda: None

        class FakeSmartApi:
            def getMarketData(self, mode, exchangeTokens):
                return {"data": None}
        broker.smart_api = FakeSmartApi()
        assert broker.get_option_volumes_bulk({"NFO": ["111"]}) == {}


class TestSmartConnectTimeoutOverride:
    """SmartConnect SDK default timeout is 7s — Tiger overrides to 20s.

    This fixes the "Read timed out (read timeout=7)" errors during peak
    market load. The override is set right after SmartConnect init.
    """

    def test_timeout_override_present_in_source(self):
        """The login() method must set self.smart_api.timeout = 20."""
        import re
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "broker" / "angel_connect.py").read_text()
        # Must explicitly set timeout to 20 (not the SDK default 7).
        assert re.search(r"\.timeout\s*=\s*20", src), (
            "SmartConnect timeout override (20s) not found in login()")

    def test_login_sets_timeout_to_20(self):
        """login() must set self.smart_api.timeout = 20 right after init."""
        import broker.angel_connect as ac
        from broker.angel_connect import AngelBroker
        from unittest.mock import patch
        broker = AngelBroker.__new__(AngelBroker)
        broker.client_id = "X"
        broker.mpin = "X"
        broker.totp_secret = "X"
        broker.api_key = "X"
        broker.websocket = None
        broker._ws_enabled = False
        broker._relogin_in_progress = False

        # Capture the timeout assignment on the fake SmartConnect.
        created = {}

        class FakeSmartConnect:
            def __init__(self, api_key=None):
                self.api_key = api_key
                self.timeout = 7  # SDK default
                created["instance"] = self
            def generateSession(self, client_code, mpin, totp):
                return {"status": True, "data": {"jwtToken": "t",
                                                 "refreshToken": "r"}}

        # SmartConnect is imported locally inside login() via
        # `from SmartApi import SmartConnect` — patch the source module.
        import SmartApi  # noqa: may be absent in CI; patched below
        with patch.object(SmartApi, "SmartConnect", FakeSmartConnect):
            with patch.object(broker, "_generate_totp", return_value="123456"):
                broker.login()

        # The login must have overridden the default 7s → 20s.
        assert created["instance"].timeout == 20
