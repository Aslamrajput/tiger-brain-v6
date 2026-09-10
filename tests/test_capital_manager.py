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

    def test_weak_tier_blocks(self):
        """Score < 75 = WEAK (0% = blocked)."""
        tier, mult = CapitalManager.conviction_tier(
            setup_score=70.0, brain_alignment=4)
        self.assertEqual(tier, "WEAK")
        self.assertAlmostEqual(mult, 0.0)

    def test_sure_shot_allocation(self):
        """RULE A: Sure Shot allocates 100% of margin, capped by free."""
        broker = MockBroker(balance=100000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=50000,
            open_positions_cost=0,
        )
        self.assertTrue(check.allowed)
        self.assertEqual(check.conviction_tier, "SURE_SHOT")
        self.assertAlmostEqual(check.allocated_capital, 100000.0)
        self.assertAlmostEqual(check.allocation_pct, 100.0)

    def test_sure_shot_capped_by_free_disposable(self):
        """Sure Shot allocation capped when free disposable < 100%."""
        broker = MockBroker(balance=100000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=30000,
            open_positions_cost=60000.0,  # 60k deployed, 40k free
        )
        self.assertTrue(check.allowed)
        self.assertEqual(check.free_disposable, 40000.0)
        self.assertLessEqual(check.allocated_capital, 40000.0)


class TestOrderBlocking(unittest.TestCase):
    """RULE B: Order blocking when insufficient margin."""

    def test_trade_cost_exceeds_allocation_blocks(self):
        """Trade cost > allocated capital = blocked."""
        broker = MockBroker(balance=50000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=77.0,  # DECENT tier = 60% = 30k
            brain_alignment=5,
            trade_cost_estimate=40000,  # > 30k allocated
            open_positions_cost=0,
        )
        self.assertFalse(check.allowed)
        self.assertTrue(check.blocked)
        self.assertIn("Trade cost", check.reason)

    def test_min_allocation_not_met_blocks(self):
        """Allocated < minimum required = blocked."""
        broker = MockBroker(balance=50000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=77.0,  # DECENT = 60% = 30k
            brain_alignment=5,
            trade_cost_estimate=10000,
            open_positions_cost=0,
            min_allocation=35000,  # > 30k allocated
        )
        self.assertFalse(check.allowed)
        self.assertTrue(check.blocked)
        self.assertIn("minimum", check.reason)


class TestFullFlow(unittest.TestCase):
    """Full integration: 7-brain alignment → capital check → allocation."""

    def test_sure_shot_full_flow(self):
        """7 brains aligned, high score, enough capital = ALLOWED."""
        broker = MockBroker(balance=150000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=120000,
            open_positions_cost=0,
        )
        self.assertTrue(check.allowed)
        self.assertEqual(check.conviction_tier, "SURE_SHOT")
        self.assertGreater(check.allocated_capital, 0)
        self.assertLessEqual(check.allocated_capital, 150000.0)

    def test_secondary_trade_with_primary_open(self):
        """Primary trade running, enough free margin for secondary."""
        broker = MockBroker(balance=100000.0)
        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=95.0,
            brain_alignment=7,
            trade_cost_estimate=30000,
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


if __name__ == "__main__":
    unittest.main()
