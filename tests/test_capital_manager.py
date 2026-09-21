"""
Tests for the Dynamic Capital Management module.

Validates:
  - GATE 1: Live funds fetch (zero funds = block)
  - GATE 2: Free disposable capital (no free margin = block)
  - GATE 3: Conviction-based allocation (SURE_SHOT/STRONG/DECENT/WEAK)
  - RULE A: Sure Shot allocation (all 7 brains + score >= 90)
  - RULE B: Order blocking (insufficient free margin)
"""

import unittest
from unittest.mock import Mock, patch
from datetime import date

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from risk.capital_manager import CapitalManager, CapitalCheck
from config.thresholds import BRAIN4


class MockBroker:
    """Mock broker for testing — avoids real API calls."""
    def __init__(self, balance: float = 100000.0):
        self._balance = balance

    def get_balance(self) -> float:
        if self._balance <= 0:
            raise ValueError("RMS returned 0 funds")
        return self._balance


class TestCapitalManagerGate1(unittest.TestCase):
    """GATE 1: Live funds fetch."""

    def test_zero_funds_blocks_order(self):
        """Zero available funds = hard block, no order."""
        broker = MockBroker(balance=0.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=5000,
            open_positions_cost=0,
        )
        self.assertFalse(check.allowed)
        self.assertTrue(check.blocked)
        self.assertIn("0 available funds", check.reason)

    def test_positive_funds_allows_check(self):
        """Positive funds passes gate 1."""
        broker = MockBroker(balance=100000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=5000,
            open_positions_cost=0,
        )
        self.assertTrue(check.allowed)
        self.assertEqual(check.available_funds, 100000.0)


class TestCapitalManagerGate2(unittest.TestCase):
    """GATE 2: Free disposable capital check."""

    def test_no_free_disposable_blocks_order(self):
        """All funds deployed = no secondary trade."""
        broker = MockBroker(balance=100000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=5000,
            open_positions_cost=100000.0,  # all deployed
        )
        self.assertFalse(check.allowed)
        self.assertTrue(check.blocked)
        self.assertIn("No free disposable capital", check.reason)

    def test_partial_deployment_allows_order(self):
        """Partial deployment leaves room for secondary trade."""
        broker = MockBroker(balance=100000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=5000,
            open_positions_cost=30000.0,  # 30k deployed, 70k free
        )
        self.assertTrue(check.allowed)
        self.assertEqual(check.deployed_capital, 30000.0)
        self.assertEqual(check.free_disposable, 70000.0)


class TestConvictionTiers(unittest.TestCase):
    """GATE 3: Conviction-based allocation (RULE A)."""

    def test_sure_shot_tier(self):
        """7/7 brains aligned + score >= 90 = SURE_SHOT (100%)."""
        tier, mult = CapitalManager.conviction_tier(
            setup_score=92.0, brain_alignment=7)
        self.assertEqual(tier, "SURE_SHOT")
        self.assertAlmostEqual(mult, 1.0)

    def test_strong_tier(self):
        """6/7 brains + score >= 80 = STRONG (80%)."""
        tier, mult = CapitalManager.conviction_tier(
            setup_score=85.0, brain_alignment=6)
        self.assertEqual(tier, "STRONG")
        self.assertAlmostEqual(mult, 0.8)

    def test_decent_tier(self):
        """Score >= 75 = DECENT (60%)."""
        tier, mult = CapitalManager.conviction_tier(
            setup_score=77.0, brain_alignment=5)
        self.assertEqual(tier, "DECENT")
        self.assertAlmostEqual(mult, 0.6)

    def test_weak_tier_advisory_only(self):
        """Score < 75 = WEAK (0%) — ADVISORY only, never blocks the trade.
        Tiger decides whether to proceed."""
        tier, mult = CapitalManager.conviction_tier(
            setup_score=70.0, brain_alignment=4)
        self.assertEqual(tier, "WEAK")
        self.assertAlmostEqual(mult, 0.0)

    def test_sure_shot_allocation(self):
        """Advisory: SURE_SHOT allocates full free disposable (Tiger's money)."""
        broker = MockBroker(balance=100000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=5000,
            open_positions_cost=0,
        )
        self.assertTrue(check.allowed)
        self.assertEqual(check.conviction_tier, "SURE_SHOT")
        # allocated = free_disposable (Tiger takes what it needs, no arbitrary cap)
        self.assertAlmostEqual(check.allocated_capital, 100000.0)

    def test_sure_shot_capped_by_free_disposable(self):
        """Allocation = free_disposable when partially deployed."""
        broker = MockBroker(balance=100000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=3000,
            open_positions_cost=60000.0,  # 60k deployed, 40k free
        )
        self.assertTrue(check.allowed)
        self.assertEqual(check.free_disposable, 40000.0)
        self.assertAlmostEqual(check.allocated_capital, 40000.0)


class TestOrderBlocking(unittest.TestCase):
    """RULE B: Block ONLY when trade_cost > free_disposable (not enough real money)."""

    def test_trade_cost_exceeds_free_disposable_blocks(self):
        """Trade cost > free disposable = blocked (not enough REAL money)."""
        broker = MockBroker(balance=50000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=77.0,  # DECENT tier — advisory only
            brain_alignment=5,
            trade_cost_estimate=60000,  # > 50k free disposable
            open_positions_cost=0,
        )
        self.assertFalse(check.allowed)
        self.assertTrue(check.blocked)
        self.assertIn("Not enough real money", check.reason)

    def test_min_allocation_not_met_blocks(self):
        """When free_disposable < min_allocation → blocked (real money check)."""
        broker = MockBroker(balance=50000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=77.0,
            brain_alignment=5,
            trade_cost_estimate=10000,
            open_positions_cost=0,
            min_allocation=60000,  # > 50k available
        )
        self.assertFalse(check.allowed)
        self.assertTrue(check.blocked)
        self.assertIn("Not enough real money", check.reason)

    def test_low_conviction_does_not_block(self):
        """WEAK conviction is ADVISORY only — trade still allowed if real money OK."""
        broker = MockBroker(balance=100000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=70.0,  # WEAK tier
            brain_alignment=4,
            trade_cost_estimate=5000,
            open_positions_cost=0,
        )
        self.assertTrue(check.allowed)  # NOT blocked — advisory only
        self.assertEqual(check.conviction_tier, "WEAK")


class TestFullFlow(unittest.TestCase):
    """Full integration: 7-brain alignment → capital check → allocation."""

    def test_sure_shot_full_flow(self):
        """7 brains aligned, high score, enough capital = ALLOWED (full free disposable)."""
        broker = MockBroker(balance=150000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=5000,
            open_positions_cost=0,
        )
        self.assertTrue(check.allowed)
        self.assertEqual(check.conviction_tier, "SURE_SHOT")
        self.assertGreater(check.allocated_capital, 0)
        # allocated = free_disposable (no arbitrary cap)
        self.assertAlmostEqual(check.allocated_capital, 150000.0)

    def test_secondary_trade_with_primary_open(self):
        """Primary trade running, enough free margin for secondary."""
        broker = MockBroker(balance=100000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=5000,  # within the fixed slot
            open_positions_cost=50000.0,  # 50k deployed, 50k free
        )
        self.assertTrue(check.allowed)
        self.assertEqual(check.free_disposable, 50000.0)
        self.assertLessEqual(check.allocated_capital, 50000.0)

    def test_secondary_trade_blocked_no_free_margin(self):
        """Primary trade running, no free margin for secondary = BLOCKED."""
        broker = MockBroker(balance=100000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=5000,
            open_positions_cost=98000.0,  # 98k deployed, 2k free
        )
        # Only 2k free disposable, but SURE_SHOT wants 100k
        # Allocated will be capped to 2k, but min_allocation default is 0
        # so it passes if trade_cost (5k) <= allocated (2k)?
        # No — trade_cost > allocated → blocked
        self.assertFalse(check.allowed)

    def test_max_open_positions_blocks_third_trade(self):
        """CAPITAL FIX: MAX_OPEN_POSITIONS=2 blocks the 3rd concurrent trade."""
        broker = MockBroker(balance=100000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=5000,
            open_positions_cost=29000.0,
            open_position_count=2,  # already 2 open = max
        )
        self.assertFalse(check.allowed)
        self.assertTrue(check.blocked)
        self.assertIn("MAX_OPEN_POSITIONS", check.reason)

    def test_full_account_deployable_no_arbitrary_cap(self):
        """Tiger can deploy full account balance — no ALLOCATED_PER_TRADE cap.
        ₹29k account: allocated = free_disposable = full balance."""
        broker = MockBroker(balance=29453.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=85.0,
            brain_alignment=6,
            trade_cost_estimate=12000,  # one lot ~₹12,800
            open_positions_cost=0,
            min_allocation=12800,
            open_position_count=0,
        )
        self.assertTrue(check.allowed)
        # allocated = free_disposable = full balance (no arbitrary cap)
        self.assertAlmostEqual(check.allocated_capital, 29453.0)
        # must cover the minimum one-lot cost
        self.assertGreaterEqual(check.allocated_capital, 12800)


if __name__ == "__main__":
    unittest.main()
