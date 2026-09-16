"""Tests for Angel One token auto-relogin.

Verifies that when Angel One's JWT token expires mid-session (AG8003 /
"Token missing"), Tiger automatically detects it, performs a fresh login,
and retries the failed API call — never gets stuck in a broken-token loop.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from broker.angel_connect import AngelBroker


def _make_broker():
    """Create a real AngelBroker instance without calling __init__ (no .env needed)."""
    from broker.angel_connect import AngelBroker
    b = AngelBroker.__new__(AngelBroker)
    b.client_id = "test"
    b.mpin = "test"
    b.totp_secret = "test"
    b.api_key = "test"
    b.smart_api = MagicMock()
    b.session_data = {"data": {"jwtToken": "old_expired"}}
    b.login_time = datetime.now() - timedelta(hours=2)
    b._token_healthy = True
    b._last_relogin_attempt = None
    b.websocket = None
    b._ws_enabled = False
    return b


# ============================================================
# TOKEN ERROR DETECTION
# ============================================================

class TestTokenErrorDetection:
    def test_detects_ag8003(self):
        assert AngelBroker._is_token_error("AG8003 Token missing")

    def test_detects_token_missing(self):
        assert AngelBroker._is_token_error({"message": "Token missing"})

    def test_detects_ag8002(self):
        assert AngelBroker._is_token_error("AG8002 Invalid Token")

    def test_detects_session_expired(self):
        assert AngelBroker._is_token_error("Session expired")

    def test_detects_jwt(self):
        assert AngelBroker._is_token_error("jwt verification failed")

    def test_does_not_detect_rate_limit(self):
        assert not AngelBroker._is_token_error("Too many requests AB1021")

    def test_does_not_detect_network_error(self):
        assert not AngelBroker._is_token_error("Connection reset by peer")


# ============================================================
# SESSION VALIDITY WITH TOKEN HEALTH
# ============================================================

class TestSessionValidity:
    def test_valid_when_healthy_and_recent(self):
        b = _make_broker()
        assert b.is_session_valid() is True

    def test_invalid_when_token_unhealthy(self):
        b = _make_broker()
        b._token_healthy = False
        assert b.is_session_valid() is False

    def test_invalid_after_20_hours(self):
        b = _make_broker()
        b.login_time = datetime.now() - timedelta(hours=21)
        assert b.is_session_valid() is False

    def test_invalid_when_no_session(self):
        b = _make_broker()
        b.session_data = None
        assert b.is_session_valid() is False


# ============================================================
# AUTO RELOGIN
# ============================================================

class TestAutoRelogin:
    def test_auto_relogin_calls_login(self):
        b = _make_broker()
        b._token_healthy = False
        with patch.object(b, 'login') as mock_login:
            result = b._auto_relogin()
            assert mock_login.call_count == 1
            assert result is True

    def test_auto_relogin_sets_token_healthy(self):
        b = _make_broker()
        b._token_healthy = False
        # Mock login to simulate what real login does: set _token_healthy = True
        def fake_login():
            b._token_healthy = True
        with patch.object(b, 'login', side_effect=fake_login):
            result = b._auto_relogin()
            assert result is True
            assert b._token_healthy is True

    def test_auto_relogin_cooldown_30s(self):
        b = _make_broker()
        b._token_healthy = False
        b._last_relogin_attempt = datetime.now()
        with patch.object(b, 'login') as mock_login:
            result = b._auto_relogin()
            assert mock_login.call_count == 0
            assert result is False

    def test_auto_relogin_after_cooldown_expires(self):
        b = _make_broker()
        b._token_healthy = False
        b._last_relogin_attempt = datetime.now() - timedelta(seconds=31)
        with patch.object(b, 'login') as mock_login:
            result = b._auto_relogin()
            assert mock_login.call_count == 1
            assert result is True

    def test_auto_relogin_returns_false_on_login_failure(self):
        b = _make_broker()
        b._token_healthy = False
        with patch.object(b, 'login', side_effect=Exception("Login failed")):
            result = b._auto_relogin()
            assert result is False


# ============================================================
# ENSURE LOGGED IN WITH FORCE
# ============================================================

class TestEnsureLoggedIn:
    def test_ensure_logged_in_no_action_when_valid(self):
        b = _make_broker()
        with patch.object(b, 'login') as mock_login:
            b.ensure_logged_in()
            assert mock_login.call_count == 0

    def test_ensure_logged_in_forces_relogin(self):
        b = _make_broker()
        with patch.object(b, 'login') as mock_login:
            b.ensure_logged_in(force=True)
            assert mock_login.call_count == 1

    def test_ensure_logged_in_relogin_when_unhealthy(self):
        b = _make_broker()
        b._token_healthy = False
        with patch.object(b, 'login') as mock_login:
            b.ensure_logged_in()
            assert mock_login.call_count == 1


# ============================================================
# LTP FETCH WITH TOKEN RECOVERY
# ============================================================

class TestLtpTokenRecovery:
    def test_ltp_auto_relogin_on_token_error(self):
        b = _make_broker()
        # First call returns token error, second (after relogin) returns data
        b.smart_api.ltpData.side_effect = [
            {"status": False, "message": "Token missing", "errorCode": "AG8003",
             "data": ""},
            {"status": True, "data": {"ltp": 150.5}},
        ]
        with patch.object(b, '_auto_relogin', return_value=True) as mock_relogin:
            with patch.object(b, 'ensure_logged_in'):
                ltp = b.get_ltp("NIFTY", "123", "NFO")
                assert mock_relogin.call_count == 1
                assert ltp == 150.5

    def test_ltp_returns_zero_if_relogin_fails(self):
        b = _make_broker()
        b.smart_api.ltpData.return_value = {
            "status": False, "message": "Token missing",
            "errorCode": "AG8003", "data": ""}
        with patch.object(b, '_auto_relogin', return_value=False):
            with patch.object(b, 'ensure_logged_in'):
                ltp = b.get_ltp("NIFTY", "123", "NFO")
                assert ltp == 0.0

    def test_ltp_token_error_exception_triggers_relogin(self):
        b = _make_broker()
        b.smart_api.ltpData.side_effect = [
            Exception("AG8003 Token missing"),
            {"status": True, "data": {"ltp": 200.0}},
        ]
        with patch.object(b, '_auto_relogin', return_value=True):
            with patch.object(b, 'ensure_logged_in'):
                ltp = b.get_ltp("NIFTY", "123", "NFO")
                assert ltp == 200.0


# ============================================================
# ORDER PLACEMENT WITH TOKEN RECOVERY
# ============================================================

class TestOrderTokenRecovery:
    def test_order_auto_relogin_on_token_error(self):
        b = _make_broker()
        b.smart_api.placeOrder.side_effect = [
            Exception("AG8003 Token missing"),
            "ORD12345",
        ]
        with patch.object(b, '_auto_relogin', return_value=True):
            with patch.object(b, 'ensure_logged_in'):
                result = b.place_option_order(
                    "NIFTY24SEP22500CE", "123", "NFO", "BUY", 50)
                assert result["success"] is True
                assert result["order_id"] == "ORD12345"

    def test_order_fails_if_relogin_fails(self):
        b = _make_broker()
        b.smart_api.placeOrder.side_effect = Exception("AG8003 Token missing")
        with patch.object(b, '_auto_relogin', return_value=False):
            with patch.object(b, 'ensure_logged_in'):
                result = b.place_option_order(
                    "NIFTY24SEP22500CE", "123", "NFO", "BUY", 50)
                assert result["success"] is False


# ============================================================
# CANDLE FETCH WITH TOKEN RECOVERY (data/loader.py)
# ============================================================

class TestCandleTokenRecovery:
    def test_candle_chunk_relogin_on_token_error_response(self):
        from data.loader import fetch_candle_chunk
        broker = MagicMock()
        broker._token_healthy = True
        broker._auto_relogin.return_value = True
        broker.smart_api.getCandleData.side_effect = [
            {"status": False, "message": "Token missing",
             "errorCode": "AG8003", "data": ""},
            {"status": True, "data": [["09:15", 100, 102, 99, 101, 5000]]},
        ]
        params = {"exchange": "NSE", "symboltoken": "123",
                  "interval": "FIFTEEN_MINUTE",
                  "fromdate": "2026-09-08 09:00",
                  "todate": "2026-09-08 15:15"}
        result = fetch_candle_chunk(broker, params)
        assert len(result) == 1
        assert broker._auto_relogin.call_count == 1
        assert broker._token_healthy is False  # marked unhealthy before relogin

    def test_candle_chunk_relogin_on_token_error_exception(self):
        from data.loader import fetch_candle_chunk
        broker = MagicMock()
        broker._token_healthy = True
        broker._auto_relogin.return_value = True
        broker.smart_api.getCandleData.side_effect = [
            Exception("AG8003 Token missing"),
            {"status": True, "data": [["09:15", 100, 102, 99, 101, 5000]]},
        ]
        params = {"exchange": "NSE", "symboltoken": "123",
                  "interval": "FIFTEEN_MINUTE",
                  "fromdate": "2026-09-08 09:00",
                  "todate": "2026-09-08 15:15"}
        result = fetch_candle_chunk(broker, params)
        assert len(result) == 1
        assert broker._auto_relogin.call_count == 1

    def test_candle_chunk_returns_empty_if_relogin_fails(self):
        from data.loader import fetch_candle_chunk
        broker = MagicMock()
        broker._token_healthy = True
        broker._auto_relogin.return_value = False
        broker.smart_api.getCandleData.return_value = {
            "status": False, "message": "Token missing", "data": ""}
        params = {"exchange": "NSE", "symboltoken": "123",
                  "interval": "FIFTEEN_MINUTE",
                  "fromdate": "2026-09-08 09:00",
                  "todate": "2026-09-08 15:15"}
        result = fetch_candle_chunk(broker, params)
        assert result == []
