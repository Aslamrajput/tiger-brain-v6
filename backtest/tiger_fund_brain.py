"""
Tiger Brain V16 — FUND ANNOUNCEMENT BRAIN (Brain 0: Capital Commander)
=========================================================================
Ye Tiger ki sabse pehli Brain hai — market open hone se PHELE decide
karti hai ki aaj kitna fund use karna hai, kis segment me, kitne trades
lene hai, aur small capital (₹10k) ko fastest kaise grow karna hai.

CORE PHILOSOPHY:
  - Trading LOT SIZE se nahi, CAPITAL se decide hogi.
  - ₹10,000 account → aggressive compounding (high risk %, small lots)
  - ₹1,00,000 account → balanced (medium risk, steady growth)
  - ₹10,00,000+ account → conservative (low risk %, large lots, capital preservation)

PRE-MARKET ANNOUNCEMENT (before 09:15 IST):
  1. Read account fund (broker API or config)
  2. Classify capital tier (micro / small / mid / large / whale)
  3. Announce: max trades today, per-trade allocation %, risk per trade
  4. Growth strategy: for small accounts, aim for 20-30% monthly compounding
  5. Segment allocation: intraday vs delivery split

This brain runs BEFORE Brain 1 (scanner). It sets the budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger("tiger_brain.fund_brain")


# ============================================================
# CAPITAL TIERS — fund ke hisab se strategy change hoti hai
# ============================================================
TIER_MICRO = "MICRO"      # ₹0      - ₹25,000    — survival + aggressive growth
TIER_SMALL = "SMALL"      # ₹25,001 - ₹1,00,000   — balanced compounding
TIER_MID = "MID"          # ₹1,00,001 - ₹5,00,000 — steady growth
TIER_LARGE = "LARGE"      # ₹5,00,001 - ₹50,00,000 — capital preservation
TIER_WHALE = "WHALE"      # ₹50,00,001+            — institutional scale


@dataclass
class FundPlan:
    """Pre-market fund announcement — Tiger ka aaj ka battle plan."""
    account_capital: float
    tier: str
    available_capital: float
    max_trades_today: int
    max_trades_intraday: int
    max_trades_delivery: int
    risk_per_trade_pct: float
    risk_per_trade_rupees: float
    max_capital_per_trade_pct: float
    max_capital_per_trade_rupees: float
    max_total_exposure_pct: float
    max_total_exposure_rupees: float
    intraday_allocation_pct: float
    delivery_allocation_pct: float
    growth_target_monthly_pct: float
    growth_strategy: str
    lot_scaling: str
    announcement_date: date
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"  Account Capital:    ₹{self.account_capital:,.0f}",
            f"  Tier:               {self.tier}",
            f"  Available:          ₹{self.available_capital:,.0f}",
            f"  Max Trades Today:   {self.max_trades_today} "
            f"(intraday:{self.max_trades_intraday}, delivery:{self.max_trades_delivery})",
            f"  Risk/Trade:         {self.risk_per_trade_pct:.1f}% = ₹{self.risk_per_trade_rupees:,.0f}",
            f"  Max Capital/Trade:  {self.max_capital_per_trade_pct:.1f}% = ₹{self.max_capital_per_trade_rupees:,.0f}",
            f"  Max Exposure:       {self.max_total_exposure_pct:.1f}% = ₹{self.max_total_exposure_rupees:,.0f}",
            f"  Intraday Split:     {self.intraday_allocation_pct:.0f}% | "
            f"Delivery: {self.delivery_allocation_pct:.0f}%",
            f"  Monthly Target:     {self.growth_target_monthly_pct:.0f}%",
            f"  Strategy:           {self.growth_strategy}",
        ]
        for note in self.notes:
            lines.append(f"  ⚡ {note}")
        return "\n".join(lines)


# ============================================================
# TIER CONFIGURATIONS — har tier ka risk/reward profile
# ============================================================
TIER_CONFIG = {
    TIER_MICRO: {
        "risk_per_trade_pct": 3.0,       # aggressive — ₹10k me ₹300 risk
        # FULL capital per trade (user rule: "pura fund use karo").
        # ⚠️ Micro tier: single option can lose 100% premium → full account
        # at risk. Risk accepted per user mandate — high-conviction
        # sniper trades only. Stop-loss (OB -12%) caps actual loss.
        "max_capital_per_trade_pct": 100.0, # FULL capital deployable
        "max_total_exposure_pct": 100.0,   # full capital deployable
        "max_trades_today": 2,             # few trades, high conviction
        "max_trades_intraday": 1,
        "max_trades_delivery": 1,
        "intraday_allocation_pct": 100.0,  # no split — micro needs full fund per trade
        "delivery_allocation_pct": 50.0,
        "growth_target_monthly_pct": 30.0, # 30% monthly = aggressive compounding
        "growth_strategy": "SURVIVAL+AGGRESSIVE — ek-ek rupee ka jawab",
        "lot_scaling": "MINIMUM_LOT — sirf 1 lot, premium cheap hona chahiye",
    },
    TIER_SMALL: {
        "risk_per_trade_pct": 2.0,       # ₹1L me ₹2000 risk
        "max_capital_per_trade_pct": 100.0, # FULL capital deployable (user: "pura fund use karo")
        "max_total_exposure_pct": 100.0,   # full capital deployable
        "max_trades_today": 3,
        "max_trades_intraday": 2,
        "max_trades_delivery": 1,
        "intraday_allocation_pct": 100.0,  # no intraday/delivery split — full fund for the active trade
        "delivery_allocation_pct": 40.0,
        "growth_target_monthly_pct": 20.0,
        "growth_strategy": "BALANCED COMPOUNDING — steady growth, controlled risk",
        "lot_scaling": "SCALED — 1-2 lots based on premium",
    },
    TIER_MID: {
        "risk_per_trade_pct": 1.5,       # ₹5L me ₹7500 risk
        "max_capital_per_trade_pct": 80.0, # 80% per trade when fully sure
        "max_total_exposure_pct": 100.0,   # full capital deployable
        "max_trades_today": 4,
        "max_trades_intraday": 3,
        "max_trades_delivery": 1,
        "intraday_allocation_pct": 70.0,
        "delivery_allocation_pct": 30.0,
        "growth_target_monthly_pct": 15.0,
        "growth_strategy": "STEADY GROWTH — diversified, medium risk",
        "lot_scaling": "MULTI-LOT — 2-4 lots, proper sizing",
    },
    TIER_LARGE: {
        "risk_per_trade_pct": 1.0,       # ₹50L me ₹50,000 risk
        "max_capital_per_trade_pct": 80.0, # 80% per trade when fully sure
        "max_total_exposure_pct": 100.0,   # full capital deployable
        "max_trades_today": 5,
        "max_trades_intraday": 4,
        "max_trades_delivery": 1,
        "intraday_allocation_pct": 75.0,
        "delivery_allocation_pct": 25.0,
        "growth_target_monthly_pct": 10.0,
        "growth_strategy": "CAPITAL PRESERVATION — low risk, consistent returns",
        "lot_scaling": "INSTITUTIONAL — 5-10 lots, proper risk distribution",
    },
    TIER_WHALE: {
        "risk_per_trade_pct": 0.5,       # ₹10cr me ₹50,000 risk
        "max_capital_per_trade_pct": 80.0, # 80% per trade when fully sure
        "max_total_exposure_pct": 100.0,   # full capital deployable
        "max_trades_today": 6,
        "max_trades_intraday": 5,
        "max_trades_delivery": 1,
        "intraday_allocation_pct": 80.0,
        "delivery_allocation_pct": 20.0,
        "growth_target_monthly_pct": 8.0,
        "growth_strategy": "INSTITUTIONAL SCALE — minimal risk, max diversification",
        "lot_scaling": "WHALE — 10-50 lots, full position management",
    },
}


def classify_capital(capital: float) -> str:
    """Account capital ko tier me classify karo."""
    if capital <= 25000:
        return TIER_MICRO
    elif capital <= 100000:
        return TIER_SMALL
    elif capital <= 500000:
        return TIER_MID
    elif capital <= 5000000:
        return TIER_LARGE
    else:
        return TIER_WHALE


def announce_fund_plan(
    account_capital: float,
    available_capital: float | None = None,
    today: date | None = None,
) -> FundPlan:
    """
    PRE-MARKET FUND ANNOUNCEMENT — Tiger ka aaj ka battle plan.

    Ye function market open hone se PHELE call hota hai. It reads the
    account capital, classifies the tier, and announces:
      - How many trades today (intraday vs delivery)
      - Risk per trade (₹ and %)
      - Max capital per trade
      - Max total exposure
      - Growth strategy

    Args:
        account_capital: total account value (broker RMS or config)
        available_capital: cash available for trading (defaults to account_capital)
        today: date for the announcement (defaults to today)

    Returns:
        FundPlan: the complete pre-market battle plan
    """
    if account_capital <= 0:
        raise ValueError(f"Account capital must be positive, got {account_capital}")

    if available_capital is None:
        available_capital = account_capital
    if today is None:
        today = date.today()

    tier = classify_capital(account_capital)
    cfg = TIER_CONFIG[tier]

    risk_rupees = account_capital * cfg["risk_per_trade_pct"] / 100
    max_per_trade_rupees = available_capital * cfg["max_capital_per_trade_pct"] / 100
    max_exposure_rupees = available_capital * cfg["max_total_exposure_pct"] / 100

    notes = []
    if tier == TIER_MICRO:
        notes.append("MICRO ACCOUNT: sirf A+ rocket setups — 1 trade din me, "
                     "premium ₹10-50 range, 1 lot only")
        notes.append(f"Growth target: ₹{account_capital:,.0f} → "
                     f"₹{account_capital * 2:,.0f} in 3 months (100% doubling)")
        notes.append("Strategy: survive first, then thrive. NO REVENGE TRADING.")
    elif tier == TIER_SMALL:
        notes.append("SMALL ACCOUNT: 2-3 high-conviction trades, "
                     "proper lot sizing, ₹50-150 premium range")
        notes.append(f"Growth target: ₹{account_capital:,.0f} → "
                     f"₹{account_capital * 1.7:,.0f} in 3 months (70% growth)")
    elif tier == TIER_MID:
        notes.append("MID ACCOUNT: diversified 3-4 trades, "
                     "delivery mode active for rocket setups")
        notes.append(f"Growth target: ₹{account_capital:,.0f} → "
                     f"₹{account_capital * 1.5:,.0f} in 3 months (50% growth)")
    elif tier == TIER_LARGE:
        notes.append("LARGE ACCOUNT: capital preservation priority, "
                     "5 trades max, proper risk distribution")
        notes.append(f"Growth target: ₹{account_capital:,.0f} → "
                     f"₹{account_capital * 1.3:,.0f} in 3 months (30% growth)")
    else:
        notes.append("WHALE ACCOUNT: institutional scale, minimal risk %, "
                     "maximum diversification across segments")
        notes.append(f"Growth target: ₹{account_capital:,.0f} → "
                     f"₹{account_capital * 1.25:,.0f} in 3 months (25% growth)")

    plan = FundPlan(
        account_capital=account_capital,
        tier=tier,
        available_capital=available_capital,
        max_trades_today=cfg["max_trades_today"],
        max_trades_intraday=cfg["max_trades_intraday"],
        max_trades_delivery=cfg["max_trades_delivery"],
        risk_per_trade_pct=cfg["risk_per_trade_pct"],
        risk_per_trade_rupees=round(risk_rupees, 2),
        max_capital_per_trade_pct=cfg["max_capital_per_trade_pct"],
        max_capital_per_trade_rupees=round(max_per_trade_rupees, 2),
        max_total_exposure_pct=cfg["max_total_exposure_pct"],
        max_total_exposure_rupees=round(max_exposure_rupees, 2),
        intraday_allocation_pct=cfg["intraday_allocation_pct"],
        delivery_allocation_pct=cfg["delivery_allocation_pct"],
        growth_target_monthly_pct=cfg["growth_target_monthly_pct"],
        growth_strategy=cfg["growth_strategy"],
        lot_scaling=cfg["lot_scaling"],
        announcement_date=today,
        notes=notes,
    )

    logger.info(f"Fund Plan announced: {tier} tier, ₹{account_capital:,.0f}, "
                f"{plan.max_trades_today} trades today")
    return plan


def size_trade_with_fund_brain(
    plan: FundPlan,
    entry_premium: float,
    stop_premium: float,
    lot_sz: int,
    current_exposure: float = 0.0,
    is_delivery: bool = False,
) -> dict:
    """
    ADVISORY position sizing — Fund Brain advises, Tiger decides.

    The money is Tiger's.  Fund Brain never blocks — it calculates the
    optimal lot count and logs a risk advisory if the stop loss exceeds
    the risk guideline.  Tiger can override and take the trade anyway.

    Returns:
        dict with quantity, lots, allocated_capital, max_loss, advisory, etc.
    """
    if plan is None:
        return {"quantity": 0, "lots": 0, "allocated_capital": 0.0,
                "max_loss": 0.0, "reason": "no fund plan"}

    lot = int(lot_sz) if lot_sz and int(lot_sz) > 0 else 1

    if entry_premium <= 0 or stop_premium <= 0:
        return {"quantity": 0, "lots": 0, "allocated_capital": 0.0,
                "max_loss": 0.0, "reason": "invalid premium"}

    stop_per_unit = abs(entry_premium - stop_premium)
    if stop_per_unit <= 0:
        return {"quantity": 0, "lots": 0, "allocated_capital": 0.0,
                "max_loss": 0.0, "reason": "no stop distance"}

    # Risk guideline from plan (₹) — ADVISORY, not a hard cap
    risk_guideline = plan.risk_per_trade_rupees
    if is_delivery:
        risk_guideline = risk_guideline * 0.7

    # Capital available for this trade — Tiger deploys what it needs
    max_exposure = plan.max_total_exposure_rupees
    headroom = max_exposure - current_exposure
    if headroom <= 0:
        return {"quantity": 0, "lots": 0, "allocated_capital": 0.0,
                "max_loss": 0.0, "reason": "exposure limit reached"}

    capital_available = min(plan.max_capital_per_trade_rupees, headroom)

    # Lots based on CAPITAL (Tiger takes what it needs — full lot if affordable)
    loss_per_lot = stop_per_unit * lot
    lots = max(1, int(capital_available // (entry_premium * lot)))

    quantity = lots * lot
    allocated_capital = quantity * entry_premium
    max_loss = stop_per_unit * quantity

    # Advisory: is the stop loss exceeding the risk guideline?
    advisory = None
    if max_loss > risk_guideline:
        advisory = (
            f"⚠️ Risk advisory: max loss ₹{max_loss:,.0f} exceeds guideline "
            f"₹{risk_guideline:,.0f} ({max_loss / plan.account_capital * 100:.1f}% "
            f"vs {risk_guideline / plan.account_capital * 100:.1f}%) — Tiger decides"
        )
        logger.info(advisory)

    return {
        "quantity": quantity,
        "lots": lots,
        "allocated_capital": round(allocated_capital, 2),
        "max_loss": round(max_loss, 2),
        "risk_used_pct": round(max_loss / plan.account_capital * 100, 2),
        "capital_used_pct": round(allocated_capital / plan.available_capital * 100, 2),
        "stop_per_unit": round(stop_per_unit, 2),
        "tier": plan.tier,
        "is_delivery": is_delivery,
        "advisory": advisory,
        "reason": "sized_by_fund_brain",
    }


def project_growth(
    starting_capital: float,
    months: int = 3,
    monthly_return_pct: float | None = None,
) -> list[dict]:
    """
    Small capital growth projection — ₹10k se kitna growth possible.

    Compounding calculation: har month ka return add hoke capital grow
    karta hai. This shows the user EXACTLY how ₹10k can grow.

    Args:
        starting_capital: ₹ amount (e.g., 10000)
        months: projection period (default 3)
        monthly_return_pct: override tier default (None = use tier config)

    Returns:
        list of monthly projections
    """
    if monthly_return_pct is None:
        tier = classify_capital(starting_capital)
        monthly_return_pct = TIER_CONFIG[tier]["growth_target_monthly_pct"]

    capital = starting_capital
    projections = []
    for m in range(1, months + 1):
        growth = capital * monthly_return_pct / 100
        capital += growth
        projections.append({
            "month": m,
            "starting_capital": round(starting_capital + sum(
                p["growth"] for p in projections), 2),
            "growth": round(growth, 2),
            "ending_capital": round(capital, 2),
            "return_pct": monthly_return_pct,
        })
    return projections
