"""
auth.py — Gmail OAuth + 6-digit email OTP for Nexa.

Flow:
  1. User clicks "Continue with Google" → Authlib redirects to Google.
  2. Google returns code → we exchange for id_token, extract verified email.
  3. We generate a 6-digit OTP, bcrypt-hash it, store in `otp_codes`, SMTP it.
  4. User types OTP → we verify, mark profile.email_verified = true.
  5. Session cookie holds a signed JWT carrying the email claim for RLS.
"""
from __future__ import annotations

import os
import smtplib
import secrets as pysecrets
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
from urllib.parse import urlencode

import bcrypt
import requests
import streamlit as st
from email_validator import EmailNotValidError, validate_email

import database as db


def _cfg(key: str, default: str = "") -> str:
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.getenv(key, default)


GOOGLE_CLIENT_ID = _cfg("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = _cfg("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = _cfg("GOOGLE_REDIRECT_URI", "http://localhost:8501")
APP_OWNER_EMAIL = _cfg("APP_OWNER_EMAIL", "").lower().strip()

SMTP_HOST = _cfg("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(_cfg("SMTP_PORT", "587"))
SMTP_USER = _cfg("SMTP_USER")
SMTP_PASS = _cfg("SMTP_PASS")
SMTP_FROM = _cfg("SMTP_FROM", SMTP_USER)


# ─── Google OAuth ────────────────────────────────────────────────────────────
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def google_login_url() -> str:
    state = pysecrets.token_urlsafe(24)
    st.session_state["_oauth_state"] = state
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_email(code: str) -> Optional[dict]:
    """Returns dict {email, name, picture} on success."""
    try:
        r = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        r.raise_for_status()
        access_token = r.json()["access_token"]
        ui = requests.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        ui.raise_for_status()
        data = ui.json()
        if not data.get("email_verified", False):
            return None
        return {
            "email": data["email"].lower().strip(),
            "name": data.get("name", ""),
            "picture": data.get("picture", ""),
        }
    except Exception as e:
        st.error(f"Google sign-in failed: {e}")
        return None


# ─── OTP ─────────────────────────────────────────────────────────────────────
def _generate_code() -> str:
    return f"{pysecrets.randbelow(1_000_000):06d}"


def _hash_code(code: str) -> str:
    return bcrypt.hashpw(code.encode(), bcrypt.gensalt()).decode()


def _verify_hash(code: str, code_hash: str) -> bool:
    try:
        return bcrypt.checkpw(code.encode(), code_hash.encode())
    except Exception:
        return False


def send_otp(email: str) -> bool:
    try:
        validate_email(email)
    except EmailNotValidError:
        return False

    code = _generate_code()
    db.save_otp(email, _hash_code(code), ttl_minutes=10)

    if not SMTP_USER or not SMTP_PASS:
        st.warning(f"⚠️ SMTP not configured. Dev OTP for {email}: **{code}**")
        return True

    subject = "Your Nexa verification code"
    html = f"""
    <div style="font-family:Inter,system-ui,sans-serif;background:#0B0B12;padding:40px;color:#EAEAF2;">
      <div style="max-width:480px;margin:auto;background:#15151F;border-radius:20px;padding:32px;border:1px solid rgba(255,255,255,0.08);">
        <h1 style="margin:0 0 8px;font-weight:800;font-size:24px;
                   background:linear-gradient(135deg,#7C5CFF,#00D4FF);
                   -webkit-background-clip:text;color:transparent;">Nexa</h1>
        <p style="color:#9A9AB0;margin:0 0 28px;">Your AI career & news companion</p>
        <p>Use this 6-digit code to verify your email. It expires in 10 minutes.</p>
        <div style="font-size:38px;letter-spacing:12px;font-weight:800;text-align:center;
                    padding:20px;background:#0B0B12;border-radius:14px;margin:24px 0;
                    border:1px solid rgba(124,92,255,0.3);">{code}</div>
        <p style="color:#9A9AB0;font-size:13px;">If you didn't request this, ignore this email.</p>
      </div>
    </div>
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = email
    msg.attach(MIMEText(f"Your Nexa code is {code}. Expires in 10 minutes.", "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls(context=ctx)
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_FROM, [email], msg.as_string())
        return True
    except Exception as e:
        st.error(f"Couldn't send email: {e}")
        return False


def verify_otp(email: str, code: str) -> tuple[bool, str]:
    rec = db.get_otp(email)
    if not rec:
        return False, "No code on file. Request a new one."
    if rec.get("attempts", 0) >= 5:
        return False, "Too many attempts. Request a new code."

    import datetime as dt
    expires_at = dt.datetime.fromisoformat(rec["expires_at"].replace("Z", "+00:00"))
    if dt.datetime.now(tz=dt.timezone.utc) > expires_at:
        return False, "Code expired. Request a new one."

    if not _verify_hash(code.strip(), rec["code_hash"]):
        db.bump_otp_attempts(email)
        return False, "Incorrect code."

    db.clear_otp(email)
    db.mark_email_verified(email)
    return True, "Verified."


# ─── Session helpers ─────────────────────────────────────────────────────────
def current_email() -> Optional[str]:
    return st.session_state.get("email")


def sign_in_email(email: str, name: str = "", picture: str = "") -> None:
    """Create/refresh profile row, set session."""
    db.upsert_profile(email, full_name=name or None)
    st.session_state["email"] = email
    st.session_state["name"] = name
    st.session_state["picture"] = picture
    st.session_state["session_token"] = db.issue_session_jwt(email)


def sign_out() -> None:
    for k in ("email", "name", "picture", "session_token", "current_session_id",
              "messages", "otp_pending"):
        st.session_state.pop(k, None)


def is_owner(email: Optional[str] = None) -> bool:
    email = (email or current_email() or "").lower().strip()
    return bool(email) and email == APP_OWNER_EMAIL
