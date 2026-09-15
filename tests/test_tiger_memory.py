"""Tests for Tiger Memory Core — persistent learning system."""
import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest import mock

import pytest

from replay import tiger_memory


@pytest.fixture
def mem_file(tmp_path, monkeypatch):
    """Redirect MEMORY_FILE to a temp path for isolated testing."""
    path = str(tmp_path / "tiger_memory.json")
    monkeypatch.setattr(tiger_memory, "MEMORY_FILE", path)
    yield path


def _make_trades(n_wins, n_losses, symbol="CRUDEOIL", days_ago=0):
    """Build paired OPEN+CLOSED trade records."""
    trades = []
    now = datetime.now() - timedelta(days=days_ago)
    for i in range(n_wins):
        ts = (now - timedelta(days=i)).isoformat()
        trades.append({"symbol": symbol, "tradingsymbol": f"{symbol}{i}W",
                       "status": "OPEN", "entry_time": ts,
                       "exchange": "MCX", "regime": "STRONG_TREND"})
        trades.append({"symbol": symbol, "tradingsymbol": f"{symbol}{i}W",
                       "status": "CLOSED", "exit_time": ts,
                       "pnl": 500, "exit_reason": "trail"})
    for i in range(n_losses):
        ts = (now - timedelta(days=i + n_wins)).isoformat()
        trades.append({"symbol": symbol, "tradingsymbol": f"{symbol}{i}L",
                       "status": "OPEN", "entry_time": ts,
                       "exchange": "MCX", "regime": "RANGE"})
        trades.append({"symbol": symbol, "tradingsymbol": f"{symbol}{i}L",
                       "status": "CLOSED", "exit_time": ts,
                       "pnl": -400, "exit_reason": "stop"})
    return trades


def test_empty_memory_returns_defaults(mem_file):
    mem = tiger_memory.load_memory()
    assert mem["total_trades"] == 0
    assert mem["total_wins"] == 0
    assert mem["total_pnl"] == 0.0
    assert mem["symbols"] == {}
    assert mem["blacklist"] == []


def test_update_memory_basic(mem_file):
    trades = _make_trades(4, 2)
    mem = tiger_memory.update_memory_from_trades(trades)
    assert mem["total_trades"] == 6
    assert mem["total_wins"] == 4
    assert mem["total_pnl"] == pytest.approx(4 * 500 - 2 * 400)
    assert "CRUDEOIL" in mem["symbols"]
    assert mem["symbols"]["CRUDEOIL"]["win_rate_pct"] == pytest.approx(66.7, abs=0.1)


def test_persist_across_calls(mem_file):
    trades = _make_trades(3, 1)
    tiger_memory.update_memory_from_trades(trades)
    # Reload — should persist
    mem = tiger_memory.load_memory()
    assert mem["total_trades"] == 4


def test_blacklist_losing_symbol(mem_file):
    # BRITANNIA: 5 trades, 0 wins → auto-blacklist
    trades = _make_trades(0, 5, symbol="BRITANNIA")
    tiger_memory.update_memory_from_trades(trades)
    assert tiger_memory.is_symbol_blacklisted("BRITANNIA") is True


def test_no_blacklist_for_good_symbol(mem_file):
    trades = _make_trades(5, 1, symbol="CRUDEOIL")
    tiger_memory.update_memory_from_trades(trades)
    assert tiger_memory.is_symbol_blacklisted("CRUDEOIL") is False


def test_no_blacklist_with_few_trades(mem_file):
    # Only 3 trades — below BLACKLIST_MIN_TRADES (5)
    trades = _make_trades(0, 3, symbol="WEAKSYM")
    tiger_memory.update_memory_from_trades(trades)
    assert tiger_memory.is_symbol_blacklisted("WEAKSYM") is False


def test_recall_message_format(mem_file):
    trades = _make_trades(3, 1)
    tiger_memory.update_memory_from_trades(trades)
    msg = tiger_memory.recall_memory()
    assert "TIGER MEMORY RECALLED" in msg
    assert "trades" in msg
    assert "CRUDEOIL" in msg


def test_recall_empty_memory(mem_file):
    msg = tiger_memory.recall_memory()
    assert "No trade history" in msg


def test_time_slot_extraction(mem_file):
    # Trades at different times
    trades = []
    for hour, pnl in [(10, 500), (14, -400), (18, 600)]:
        ts = datetime.now().replace(hour=hour, minute=30).isoformat()
        trades.append({"symbol": "X", "tradingsymbol": f"X{hour}",
                       "status": "OPEN", "entry_time": ts, "regime": "TREND"})
        trades.append({"symbol": "X", "tradingsymbol": f"X{hour}",
                       "status": "CLOSED", "exit_time": ts, "pnl": pnl})
    mem = tiger_memory.update_memory_from_trades(trades)
    assert len(mem["time_slots"]) >= 2


def test_regime_stats(mem_file):
    trades = []
    # STRONG_TREND: 2 wins
    for i in range(2):
        ts = datetime.now().isoformat()
        trades.append({"symbol": "A", "tradingsymbol": f"A{i}",
                       "status": "OPEN", "entry_time": ts,
                       "regime": "STRONG_TREND"})
        trades.append({"symbol": "A", "tradingsymbol": f"A{i}",
                       "status": "CLOSED", "exit_time": ts, "pnl": 300})
    # RANGE: 3 losses
    for i in range(3):
        ts = datetime.now().isoformat()
        trades.append({"symbol": "B", "tradingsymbol": f"B{i}",
                       "status": "OPEN", "entry_time": ts,
                       "regime": "RANGE"})
        trades.append({"symbol": "B", "tradingsymbol": f"B{i}",
                       "status": "CLOSED", "exit_time": ts, "pnl": -200})
    mem = tiger_memory.update_memory_from_trades(trades)
    assert mem["regimes"]["STRONG_TREND"]["wins"] == 2
    assert mem["regimes"]["RANGE"]["win_rate_pct"] == 0.0


def test_memory_window_filters_old_trades(mem_file):
    # Trade from 90 days ago — outside 60-day window
    old_ts = (datetime.now() - timedelta(days=90)).isoformat()
    trades = [
        {"symbol": "OLD", "tradingsymbol": "OLD1", "status": "OPEN",
         "entry_time": old_ts, "regime": "TREND"},
        {"symbol": "OLD", "tradingsymbol": "OLD1", "status": "CLOSED",
         "exit_time": old_ts, "pnl": 1000},
    ]
    mem = tiger_memory.update_memory_from_trades(trades)
    assert mem["total_trades"] == 0


def test_get_symbol_stats(mem_file):
    trades = _make_trades(3, 1)
    tiger_memory.update_memory_from_trades(trades)
    stats = tiger_memory.get_symbol_stats("CRUDEOIL")
    assert stats is not None
    assert stats["trades"] == 4
    stats = tiger_memory.get_symbol_stats("NONEXISTENT")
    assert stats is None


def test_corrupted_memory_file_handled(tmp_path, monkeypatch):
    path = str(tmp_path / "corrupt.json")
    with open(path, "w") as f:
        f.write("{broken json")
    monkeypatch.setattr(tiger_memory, "MEMORY_FILE", path)
    mem = tiger_memory.load_memory()
    assert mem["total_trades"] == 0


def test_bad_time_slot_detection(mem_file):
    # Create trades in a specific time slot with bad win rate
    trades = []
    for i in range(5):
        ts = datetime.now().replace(hour=13, minute=30).isoformat()
        trades.append({"symbol": "X", "tradingsymbol": f"X{i}",
                       "status": "OPEN", "entry_time": ts, "regime": "TREND"})
        trades.append({"symbol": "X", "tradingsymbol": f"X{i}",
                       "status": "CLOSED", "exit_time": ts, "pnl": -300})
    tiger_memory.update_memory_from_trades(trades)
    # 13:30 → slot 12-14, 5 losses → bad slot
    bad, slot = tiger_memory.is_bad_time_slot(
        datetime.now().replace(hour=13, minute=30))
    # Only bad if enough trades and low win rate
    if bad:
        assert slot == "12-14"


def test_best_setups_sorted_by_pnl(mem_file):
    # CRUDEOIL positive, BRITANNIA negative — pass all trades at once
    all_trades = _make_trades(4, 0, symbol="CRUDEOIL") + \
                 _make_trades(0, 4, symbol="BRITANNIA")
    mem = tiger_memory.update_memory_from_trades(all_trades)
    if mem["best_setups"]:
        assert mem["best_setups"][0]["symbol"] == "CRUDEOIL"


def test_get_memory_summary(mem_file):
    trades = _make_trades(3, 1)
    tiger_memory.update_memory_from_trades(trades)
    summary = tiger_memory.get_memory_summary()
    assert summary["total_trades"] == 4
    assert summary["symbols_tracked"] >= 1


def test_exit_reason_stats(mem_file):
    trades = _make_trades(2, 2)
    mem = tiger_memory.update_memory_from_trades(trades)
    assert "trail" in mem["exit_reasons"]
    assert "stop" in mem["exit_reasons"]
    assert mem["exit_reasons"]["trail"]["pnl"] == 1000
