"""
Tiger Brain V6+V7 — NSE Holiday Calendar (Section 34 ka gap-fix)
===================================================================
Weekday logic (Mon-Fri trading, Sat/Sun off) apne aap sahi hai, par
declared market holidays (Diwali, Holi, Republic Day, etc.) alag se
maintain karni padti hain — NSE har saal apna circular nikalta hai.

⚠️ IMPORTANT — Ye list MANUALLY update karni padegi har saal:
Source: NSE India ke official "Trading Holidays" circular (nseindia.com)
Last verified: 2026 list, cross-checked via Zerodha's holiday calendar
(zerodha.com/marketintel/holiday-calendar) as of Aug 2026.

NSE beech saal mein bhi circular nikal ke calendar revise kar sakta hai
(jaisa Bajaj AMC ki site pe likha tha) — isliye ye list "best available
at time of writing" hai, gospel truth nahi. Naye saal (2027+) ke liye ye
list dobara update karni hogi — NSE ki site ya apne broker ki holiday
calendar page check karke.
"""

from __future__ import annotations

from datetime import date

# 2026 NSE Equity/F&O trading holidays (jab exchange PURA band rehta hai)
# Format: date(year, month, day): "naam"
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 15): "Municipal Corporation Elections in Maharashtra",
    date(2026, 1, 26): "Republic Day",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 28): "Bakri Eid",
    date(2026, 6, 26): "Moharram",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali-Balipratipada",
    date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}

# ⚠️ NOTE: 8 Nov 2026 (Diwali Laxmi Pujan) Sunday ko already hai, isliye
# already OFF day hai — par us din SPECIAL "Muhurat Trading" session hota
# hai (thodi der ke liye market khulta hai, sirf symbolic trading ke liye).
# Ye system abhi Muhurat Trading handle NAHI karta — agar Muhurat mein
# trade karna ho to ye ek manual override hoga, automate nahi kiya hai.
MUHURAT_TRADING_DATES_2026 = {
    date(2026, 11, 8): "Diwali Laxmi Pujan — Muhurat Trading (special short session)",
}


# Jin saalon ki list upar maujood hai. Iske bahar ke saal ke liye holiday
# ka jawab "pata nahi" hai — unhe seedha trading day maan lena galat
# missing-session alerts paida karta hai.
CALENDAR_YEARS = {2026}


def has_holiday_calendar(year: int) -> bool:
    """Kya is saal ki NSE holiday list is repo mein maujood hai?"""
    return year in CALENDAR_YEARS


def is_market_holiday(check_date: date) -> tuple:
    """
    Args:
        check_date: datetime.date object (datetime.datetime bhi chalega,
                     .date() karke pass karo agar zarurat pade)

    Returns:
        (bool, str | None) — (True/False, holiday ka naam ya None)
    """
    if hasattr(check_date, "date") and callable(check_date.date):
        check_date = check_date.date()

    if check_date in NSE_HOLIDAYS_2026:
        return True, NSE_HOLIDAYS_2026[check_date]

    return False, None


# ============================================================
# QUICK MANUAL TEST
# Chalane ka tarika: repo ROOT se → python3 -m automation.holidays
# ============================================================
if __name__ == "__main__":
    from datetime import datetime

    print("=== NSE Holiday Check Test ===\n")

    test_dates = [
        date(2026, 1, 26),   # Republic Day — holiday
        date(2026, 3, 3),    # Holi — holiday
        date(2026, 6, 15),   # Random normal trading day
        datetime(2026, 12, 25, 10, 30),  # Christmas, as datetime not date
    ]

    for d in test_dates:
        is_holiday, name = is_market_holiday(d)
        print(f"{d}: {'HOLIDAY (' + name + ')' if is_holiday else 'normal trading day'}")

    print(f"\nTotal holidays loaded for 2026: {len(NSE_HOLIDAYS_2026)}")
    print("\n✅ Test complete — koi crash nahi hua.")
    print(
        "⚠️ REMINDER: Ye list 2026 ke liye hai. Har naye saal (2027 se) "
        "isko NSE ke official circular se update karna zaroori hai, warna "
        "system holiday ke din bhi 'trading day' samjhega."
)
  
