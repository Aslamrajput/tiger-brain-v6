"""
Tiger V19 — 7-Brain Multi-Strategy Architecture
================================================

Seven parallel analytical brains scan every market tick. Each brain
operates independently and contributes a score component. The execution
core aggregates all seven, filters through the WebSocket LTP cache,
checks available capital, and routes to Angel One's order API.

BRAIN MAP
=========

Brain 1 — Scanner & Regime Detection
    Entry-window gate + momentum filter + RS divergence.
    Source: pipeline.brain1_scanner / backtest.brain1_intraday_pass
    Role: HARD GATE — rejects chop. Only directional bars with volume
    velocity >= 1.8x and body-to-range >= 0.5 pass.

Brain 2 — SMC Setup Trigger (Supply/Demand)
    Zone detection + zone touch on 1m + volume delta confirmation.
    Source: pipeline.smart_money_scanner / detect_zones / zone_touched_on_1m
    Boosters: explosive quality (+10), liquidity sweep (+8),
    delta spike (+5), trend alignment (+5), PDH/PDL (+5), VWAP (+5).
    Role: HARD GATE + primary scorer. Score = zone_quality + boosters.

Brain 3 — Option Chain & Greeks Selector
    Strike selection (ITM/ATM), delta band (0.45-0.75), OI velocity,
    spread check, days-to-expiry filter.
    Source: broker.option_selector
    Role: Selects the best CE/PE contract for the setup direction.

Brain 4 — Capital Allocation & Trade Counter Guard
    Live RMS balance fetch, daily trade counter (global + commodity),
    confidence-based dynamic position sizing, margin blocking.
    Source: risk.risk_management + risk.capital_manager + broker.position_sizer
    Role: BLOCKS order if no free disposable capital. Allocates
    dynamically based on 7-brain conviction tier (SURE_SHOT/STRONG/DECENT).

Brain 5 — Risk Guard & Exit Engine
    Premium stop-loss (25%), profit target (50%), trailing stop,
    time-based square-off, gamma guard near expiry.
    Source: risk.exit_brain / check_intraday_exit
    Role: Manages open positions. Never lets a winner turn into a loser.

Brain 6 — Premium Discount Tracker
    IV expansion/crushing detection, premium fairness check.
    Source: backtest.tiger_premium_brain / PremiumDiscountTracker
    Role: Exits when premium becomes expensive (IV expansion exit).
    Also blocks new entries when IV percentile > 60 (overpriced).

Brain 7 — Session Commander
    Time-of-day session scoring, golden window bonus, force-hunt logic,
    daily hunt quota tracking.
    Source: backtest.tiger_session_brain / should_force_hunt / HuntStatus
    Role: Adjusts score threshold by session. Morning golden window
    (09:15-11:30) gets +3 bonus. Force-hunts near close if 0 trades.

AGGREGATION FLOW
================

  1. Brains 1-2 scan every 1m bar for zone touch + volume confirmation.
  2. Brain 2 computes setup_score with all booster contributions.
  3. Brain 7 adjusts score threshold by session + applies golden window.
  4. Brain 3 selects the best option contract for the signal direction.
  5. Brain 6 checks IV percentile — blocks if premium is overpriced.
  6. Brain 4 fetches live RMS balance, checks free disposable capital,
     and allocates based on conviction tier (RULE A) or blocks (RULE B).
  7. Execution core reads LTP from WebSocket cache (zero rate limits),
     places BUY order via Angel One API, verifies acceptance.
  8. Brain 5 monitors the open position for stop/target/trail/time exits.

CONVICTION TIERS (7-Brain Alignment)
====================================

  SURE_SHOT  — 7/7 brains aligned, score >= 90 → 100% of margin
  STRONG     — 6/7 brains aligned, score >= 80 →  80% of margin
  DECENT     — 5/7 brains aligned, score >= 75 →  60% of margin
  WEAK       — below thresholds → NO TRADE (0% allocation)

OPTIONS BUYING ONLY
===================

Tiger NEVER sells options. Every order is transaction_type="BUY".
No equity delivery, no futures, no option writing.
CE = bullish direction, PE = bearish direction.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BrainResult:
    """Individual brain contribution to the aggregate score."""
    brain_id: int
    name: str
    passed: bool
    score: float = 0.0
    detail: str = ""


SEVEN_BRAINS = [
    {"id": 1, "name": "Scanner & Regime Detection",
     "module": "pipeline.brain1_scanner"},
    {"id": 2, "name": "SMC Setup Trigger (Supply/Demand)",
     "module": "pipeline.smart_money_scanner"},
    {"id": 3, "name": "Option Chain & Greeks Selector",
     "module": "broker.option_selector"},
    {"id": 4, "name": "Capital Allocation & Trade Counter",
     "module": "risk.capital_manager"},
    {"id": 5, "name": "Risk Guard & Exit Engine",
     "module": "risk.exit_brain"},
    {"id": 6, "name": "Premium Discount Tracker",
     "module": "backtest.tiger_premium_brain"},
    {"id": 7, "name": "Session Commander",
     "module": "backtest.tiger_session_brain"},
]


def count_aligned_brains(setup: dict) -> int:
    """Count how many of the 7 brains are aligned on a signal.

    Args:
        setup: The signal dict from find_tiger_brain_entry / scan_live_signals.
            Expected keys: confluence_count, setup_score, pcr_aligned,
            vwap_confluence, trend, swept, explosive, rocket_grade.

    Returns:
        Number of aligned brains (1-7).
    """
    aligned = 0

    # Brain 1: Scanner passed (momentum + regime)
    if setup.get("setup_score", 0) > 0:
        aligned += 1

    # Brain 2: SMC setup with boosters
    boosters = sum([
        bool(setup.get("sweep")),
        setup.get("delta_spike_mult", 0) >= 1.8,
        bool(setup.get("explosive")),
        bool(setup.get("trend") in ("up", "down")),
    ])
    if boosters >= 2:
        aligned += 1

    # Brain 3: Option selection viable (score high enough for ITM/ATM)
    if setup.get("setup_score", 0) >= 75:
        aligned += 1

    # Brain 4: Capital available (checked at execution time — assume yes)
    # This brain's alignment is determined by CapitalManager at order time.
    if setup.get("setup_score", 0) >= 80:
        aligned += 1

    # Brain 5: Exit engine ready (always ready in live mode)
    aligned += 1

    # Brain 6: Premium not overpriced (PCR aligned or VIX low)
    if setup.get("pcr_aligned") or setup.get("vix", 99) < 15:
        aligned += 1

    # Brain 7: Session commander (golden window or force hunt)
    if setup.get("confluence_count", 0) >= 3 or setup.get("setup_score", 0) >= 85:
        aligned += 1

    return min(aligned, 7)
