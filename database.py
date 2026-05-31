"""
database.py — Supabase wrappers for Nexa.

Two clients:
  • `sb`        : anon key (RLS enforced as the signed-in user via JWT claim 'email')
  • `sb_admin`  : service-role key, bypasses RLS (admin / server-only operations)

RLS policies in the SQL migration restrict rows to:
    current_setting('request.jwt.claim.email', true) = user_email
So before any user-scoped call we set the Postgres GUC for that connection by
attaching the session JWT via PostgREST headers.
"""
from __future__ import annotations

import os
import time
import json
import hashlib
import hmac
import base64
from typing import Any, Optional

from supabase import create_client, Client
from postgrest.exceptions import APIError


# ─── Config ──────────────────────────────────────────────────────────────────
def _cfg(key: str, default: str = "") -> str:
    """Read from Streamlit secrets first, env var fallback."""
    try:
        import streamlit as st
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.getenv(key, default)


SUPABASE_URL = _cfg("SUPABASE_URL")
SUPABASE_ANON_KEY = _cfg("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = _cfg("SUPABASE_SERVICE_KEY")
SESSION_SECRET = _cfg("SESSION_SECRET", "dev-secret-change-me")


# ─── Clients ─────────────────────────────────────────────────────────────────
def get_anon_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise RuntimeError("Supabase URL/anon key missing. Check .env / secrets.toml")
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


def get_admin_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("Supabase service key missing.")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ─── Lightweight session JWT (so we can pass an email claim to PostgREST) ───
# This is a *minimal* HS256 token signed with SESSION_SECRET. It's not a
# Supabase auth token — it's used purely to carry the email claim through the
# request.jwt.claim.email GUC for RLS. We sign + verify it server-side here.
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def issue_session_jwt(email: str, ttl: int = 60 * 60 * 24 * 7) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps({"email": email, "exp": int(time.time()) + ttl}).encode()
    )
    msg = f"{header}.{payload}".encode()
    sig = _b64url(hmac.new(SESSION_SECRET.encode(), msg, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def verify_session_jwt(token: str) -> Optional[str]:
    try:
        header, payload, sig = token.split(".")
        msg = f"{header}.{payload}".encode()
        expected = _b64url(hmac.new(SESSION_SECRET.encode(), msg, hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=="))
        if data.get("exp", 0) < int(time.time()):
            return None
        return data.get("email")
    except Exception:
        return None


def user_client(email: str) -> Client:
    """Anon client with a session JWT attached so RLS sees the email claim."""
    client = get_anon_client()
    token = issue_session_jwt(email)
    client.postgrest.auth(token)
    return client


# ─── Profiles ────────────────────────────────────────────────────────────────
def get_profile(email: str) -> Optional[dict]:
    res = get_admin_client().table("profiles").select("*").eq("email", email).limit(1).execute()
    return res.data[0] if res.data else None


def upsert_profile(email: str, **fields) -> dict:
    fields["email"] = email
    res = (
        get_admin_client()
        .table("profiles")
        .upsert(fields, on_conflict="email")
        .execute()
    )
    return res.data[0]


def mark_email_verified(email: str) -> None:
    get_admin_client().table("profiles").update({"email_verified": True}).eq(
        "email", email
    ).execute()


def mark_onboarded(email: str) -> None:
    get_admin_client().table("profiles").update({"onboarded": True}).eq(
        "email", email
    ).execute()


def delete_user(email: str) -> None:
    """GDPR — wipe everything for this user."""
    admin = get_admin_client()
    # cascades handle sessions/messages
    admin.table("profiles").delete().eq("email", email).execute()
    admin.table("otp_codes").delete().eq("email", email).execute()


# ─── OTP ─────────────────────────────────────────────────────────────────────
def save_otp(email: str, code_hash: str, ttl_minutes: int = 10) -> None:
    expires = int(time.time()) + ttl_minutes * 60
    from datetime import datetime, timezone
    get_admin_client().table("otp_codes").upsert(
        {
            "email": email,
            "code_hash": code_hash,
            "expires_at": datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(),
            "attempts": 0,
        },
        on_conflict="email",
    ).execute()


def get_otp(email: str) -> Optional[dict]:
    res = get_admin_client().table("otp_codes").select("*").eq("email", email).limit(1).execute()
    return res.data[0] if res.data else None


def bump_otp_attempts(email: str) -> int:
    rec = get_otp(email)
    n = (rec.get("attempts", 0) if rec else 0) + 1
    get_admin_client().table("otp_codes").update({"attempts": n}).eq("email", email).execute()
    return n


def clear_otp(email: str) -> None:
    get_admin_client().table("otp_codes").delete().eq("email", email).execute()


# ─── Chat sessions ──────────────────────────────────────────────────────────
def create_chat_session(email: str, title: str = "New chat") -> dict:
    res = (
        user_client(email)
        .table("chat_sessions")
        .insert({"user_email": email, "title": title})
        .execute()
    )
    return res.data[0]


def list_chat_sessions(email: str) -> list[dict]:
    res = (
        user_client(email)
        .table("chat_sessions")
        .select("*")
        .eq("user_email", email)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return res.data or []


def rename_chat_session(email: str, session_id: str, title: str) -> None:
    user_client(email).table("chat_sessions").update({"title": title[:80]}).eq(
        "id", session_id
    ).execute()


def delete_chat_session(email: str, session_id: str) -> None:
    user_client(email).table("chat_sessions").delete().eq("id", session_id).execute()


def list_messages(email: str, session_id: str) -> list[dict]:
    res = (
        user_client(email)
        .table("chat_messages")
        .select("*")
        .eq("session_id", session_id)
        .order("created_at")
        .execute()
    )
    return res.data or []


def add_message(email: str, session_id: str, role: str, content: str) -> dict:
    res = (
        user_client(email)
        .table("chat_messages")
        .insert({"session_id": session_id, "role": role, "content": content})
        .execute()
    )
    return res.data[0]


def rate_message(email: str, message_id: str, rating: int) -> None:
    user_client(email).table("chat_messages").update({"rating": rating}).eq(
        "id", message_id
    ).execute()


# ─── Rate limiting (per-user, window-based, in-DB via message timestamps) ───
def recent_message_count(email: str, minutes: int = 10) -> int:
    from datetime import datetime, timezone, timedelta
    since = (datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)).isoformat()
    sessions = list_chat_sessions(email)
    if not sessions:
        return 0
    ids = [s["id"] for s in sessions]
    res = (
        user_client(email)
        .table("chat_messages")
        .select("id", count="exact")
        .in_("session_id", ids)
        .eq("role", "user")
        .gte("created_at", since)
        .execute()
    )
    return res.count or 0


# ─── Knowledge / RAG ────────────────────────────────────────────────────────
def insert_knowledge_chunk(
    kind: str, title: str, url: str, content: str, embedding: list[float], metadata: dict | None = None
) -> None:
    get_admin_client().table("knowledge_chunks").insert(
        {
            "kind": kind,
            "title": title[:300],
            "url": url,
            "content": content[:8000],
            "embedding": embedding,
            "metadata": metadata or {},
        }
    ).execute()


def match_chunks(query_embedding: list[float], k: int = 5, kind: str | None = None) -> list[dict]:
    try:
        res = get_admin_client().rpc(
            "match_chunks",
            {"query_embedding": query_embedding, "match_count": k, "filter_kind": kind},
        ).execute()
        return res.data or []
    except APIError:
        return []


# ─── Admin ──────────────────────────────────────────────────────────────────
def admin_list_users(limit: int = 500) -> list[dict]:
    res = get_admin_client().table("profiles").select("*").order("created_at", desc=True).limit(limit).execute()
    return res.data or []


def admin_user_chats(email: str) -> list[dict]:
    sessions = (
        get_admin_client().table("chat_sessions").select("*").eq("user_email", email).execute()
    ).data or []
    if not sessions:
        return []
    ids = [s["id"] for s in sessions]
    msgs = (
        get_admin_client()
        .table("chat_messages")
        .select("*")
        .in_("session_id", ids)
        .order("created_at")
        .execute()
    ).data or []
    by_session = {s["id"]: {**s, "messages": []} for s in sessions}
    for m in msgs:
        by_session[m["session_id"]]["messages"].append(m)
    return list(by_session.values())


def admin_stats() -> dict:
    admin = get_admin_client()
    from datetime import datetime, timezone, timedelta
    now = datetime.now(tz=timezone.utc)
    day_ago = (now - timedelta(days=1)).isoformat()
    month_ago = (now - timedelta(days=30)).isoformat()

    total_users = (admin.table("profiles").select("id", count="exact").execute()).count or 0
    dau = (
        admin.table("chat_messages")
        .select("id", count="exact")
        .gte("created_at", day_ago)
        .execute()
    ).count or 0
    mau = (
        admin.table("chat_messages")
        .select("id", count="exact")
        .gte("created_at", month_ago)
        .execute()
    ).count or 0
    return {"total_users": total_users, "messages_24h": dau, "messages_30d": mau}


# ─── Opportunities (admin CRUD) ─────────────────────────────────────────────
def list_opportunities(kind: str | None = None) -> list[dict]:
    q = get_admin_client().table("opportunities").select("*").order("created_at", desc=True)
    if kind:
        q = q.eq("kind", kind)
    return q.execute().data or []


def create_opportunity(**fields) -> dict:
    return get_admin_client().table("opportunities").insert(fields).execute().data[0]


def update_opportunity(opp_id: str, **fields) -> None:
    get_admin_client().table("opportunities").update(fields).eq("id", opp_id).execute()


def delete_opportunity(opp_id: str) -> None:
    get_admin_client().table("opportunities").delete().eq("id", opp_id).execute()


# ─── Broadcasts ─────────────────────────────────────────────────────────────
def create_broadcast(title: str, body: str, filter_: dict | None = None) -> dict:
    return (
        get_admin_client()
        .table("broadcasts")
        .insert({"title": title, "body": body, "filter": filter_ or {}})
        .execute()
        .data[0]
    )


def list_broadcasts(limit: int = 20) -> list[dict]:
    return (
        get_admin_client()
        .table("broadcasts")
        .select("*")
        .order("sent_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )
