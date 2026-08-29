"""
Tiger Brain V6+V7 — Angel One Broker Connection (Login/Session Management)
=============================================================================
Ye module Angel One SmartAPI se AUTOMATIC login karta hai — roz manual
login ki zarurat khatam karne ke liye. TOTP khud generate hota hai
(pyotp library se), isliye kisi insaan ko roz Authenticator app dekh
kar code type nahi karna padta.

⚠️ IMPORTANT — Ye module REAL Angel One API ke against abhi tak test
NAHI hua hai (sandbox mein internet disabled hai). Logic SmartAPI ki
official documentation aur smartapi-python package ke usage pattern
ke hisaab se likha gaya hai, par apne server pe pehli baar chalane ke
baad zaroor verify karna ki login successfully ho raha hai.

⚠️ CREDENTIALS: Ye module sirf .env file se credentials padhta hai —
kahin bhi hardcode nahi karta. .env file kabhi bhi GitHub pe commit
nahi honi chahiye (isliye .gitignore mein already honi chahiye).
"""

import logging
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv

logger = logging.getLogger("tiger_brain.broker.angel_connect")
logging.basicConfig(level=logging.INFO)

# .env file load karna (repo root mein honi chahiye)
load_dotenv()


class AngelConnectionError(Exception):
    """Jab login fail ho ya session invalid ho, ye exception raise hoga."""
    pass


class AngelBroker:
    """
    Angel One SmartAPI ke saath ek session manage karta hai.

    Usage:
        broker = AngelBroker()
        broker.login()
        # ab broker.smart_api use kar sakte ho data/orders ke liye
        # ya broker.get_session_token() se token nikal sakte ho
    """

    def __init__(self):
        self.client_id = os.getenv("ANGEL_CLIENT_ID")
        self.mpin = os.getenv("ANGEL_MPIN")
        self.totp_secret = os.getenv("ANGEL_TOTP_SECRET")
        self.api_key = os.getenv("ANGEL_API_KEY")

        self._validate_credentials()

        self.smart_api = None
        self.session_data = None
        self.login_time = None

    def _validate_credentials(self):
        """
        .env se saari zaroori credentials mil gayi ya nahi, ye check
        karta hai — agar koi missing hai, turant clear error dega
        (silent fail nahi hoga, taaki debug karna aasan ho).
        """
        missing = []
        if not self.client_id:
            missing.append("ANGEL_CLIENT_ID")
        if not self.mpin:
            missing.append("ANGEL_MPIN")
        if not self.totp_secret:
            missing.append("ANGEL_TOTP_SECRET")
        if not self.api_key:
            missing.append("ANGEL_API_KEY")

        if missing:
            raise AngelConnectionError(
                f".env file mein ye credentials missing hain: {', '.join(missing)}. "
                f"Repo ke ROOT folder mein .env file honi chahiye in saari "
                f"values ke saath."
            )

    def _generate_totp(self) -> str:
        """
        TOTP secret se abhi ka 6-digit code generate karta hai —
        wahi jo Google Authenticator app har 30 second pe dikhata hai.
        """
        try:
            import pyotp
        except ImportError:
            raise AngelConnectionError(
                "pyotp install nahi hai. Chalao: pip3 install pyotp --user"
            )

        totp = pyotp.TOTP(self.totp_secret)
        return totp.now()

    def login(self, max_retries: int = 3) -> bool:
        """
        Angel One SmartAPI ko login request bhejta hai — Client ID +
        MPIN + fresh TOTP code use karke.

        Args:
            max_retries: agar login fail ho (jaise network issue), kitni
                         baar retry karna hai

        Returns:
            True agar login successful, warna AngelConnectionError raise
            hota hai (max retries ke baad).
        """
        try:
            from SmartApi import SmartConnect
        except ImportError:
            raise AngelConnectionError(
                "smartapi-python install nahi hai. Chalao: "
                "pip3 install smartapi-python pyotp logzero websocket-client "
                "pycryptodome --user"
            )

        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                totp_code = self._generate_totp()

                self.smart_api = SmartConnect(api_key=self.api_key)
                session = self.smart_api.generateSession(
                    self.client_id, self.mpin, totp_code
                )

                if not session.get("status"):
                    raise AngelConnectionError(
                        f"Login fail hua: {session.get('message', 'Unknown error')}"
                    )

                self.session_data = session
                self.login_time = datetime.now()

                logger.info(
                    f"✅ Angel One login successful — {self.login_time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                return True

            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"Login attempt {attempt}/{max_retries} fail hua: {exc}"
                )

        raise AngelConnectionError(
            f"Login {max_retries} attempts ke baad bhi fail hua. "
            f"Last error: {last_error}"
        )

    def is_session_valid(self) -> bool:
        """
        Session abhi bhi valid hai ya nahi check karta hai. Angel One
        session usually ek trading-din ke liye valid rehta hai — agar
        login_time se 24 ghante se zyada ho gaye, session expire maan
        lete hain (safe default, exact expiry Angel One documentation
        se confirm karni chahiye).
        """
        if self.session_data is None or self.login_time is None:
            return False

        elapsed = datetime.now() - self.login_time
        return elapsed < timedelta(hours=20)  # conservative — 24 se pehle refresh

    def ensure_logged_in(self):
        """
        Session valid nahi hai to fresh login karta hai. Ye function
        automation/scheduler.py ke pre-market-wakeup job se call hoga
        har trading din.
        """
        if not self.is_session_valid():
            logger.info("Session invalid/expired — fresh login kar rahe hain...")
            self.login()
        else:
            logger.info("Session already valid hai, fresh login ki zarurat nahi.")

    def logout(self):
        """
        Session close karta hai — market close hone ke baad call hona
        chahiye (Section 34 ka "market close" step).
        """
        if self.smart_api is not None:
            try:
                self.smart_api.terminateSession(self.client_id)
                logger.info("Session successfully logout/terminate hua.")
            except Exception as exc:
                logger.warning(f"Logout ke waqt error (ignore kar sakte hain): {exc}")
            finally:
                self.session_data = None
                self.login_time = None


# ============================================================
# QUICK MANUAL TEST — ⚠️ Ye REAL Angel One account se connect
# karne ki koshish karega. Sirf apne server pe .env sahi se setup
# hone ke baad hi chalao. Sandbox mein ye test NAHI ho saka
# (internet disabled).
# Chalane ka tarika: repo ROOT se → python3 -m broker.angel_connect
# ============================================================
if __name__ == "__main__":
    print("=== Angel One Login Test ===\n")
    print("⚠️ Ye tumhare REAL Angel One account se connect karega.")
    print("Agar credentials galat hain, error aayega — ye normal hai,")
    print("isse pata chalega .env file mein kya theek karna hai.\n")

    try:
        broker = AngelBroker()
        broker.login()
        print("\n✅ LOGIN SUCCESSFUL!")
        print(f"Session valid hai: {broker.is_session_valid()}")
    except AngelConnectionError as exc:
        print(f"\n❌ LOGIN FAILED: {exc}")
        print(
            "\nCheck karo: .env file mein saari 4 values (CLIENT_ID, MPIN, "
            "TOTP_SECRET, API_KEY) sahi se hain, koi extra space/quote nahi hai."
          )
      
