"""
app.py — Nexa main Streamlit entry.

Routes (via query params):
  ?page=landing       (default for signed-out users)
  ?page=verify        (OTP step)
  ?page=onboarding    (first-time profile setup)
  ?page=chat          (default for signed-in users)
  ?page=admin         (owner only)
  ?page=terms | privacy

OAuth callback: Google redirects back with ?code=... — we handle it on load.
"""
from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

import auth
import database as db
import chatbot
import admin as admin_page


# ─── Page setup ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Nexa — Your AI Career & News Companion",
    page_icon="✨",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _inject_css():
    css_path = Path(__file__).parent / "style.css"
    if css_path.exists():
        st.markdown(f"<style>{css_path.read_text()}</style>", unsafe_allow_html=True)


_inject_css()


# ─── Query-param helpers (Streamlit ≥ 1.30) ─────────────────────────────────
def get_page() -> str:
    return st.query_params.get("page", "")


def set_page(page: str, **extra):
    st.query_params.clear()
    st.query_params["page"] = page
    for k, v in extra.items():
        st.query_params[k] = v


# ─── OAuth callback handling ────────────────────────────────────────────────
def handle_oauth_callback():
    code = st.query_params.get("code")
    if not code or auth.current_email():
        return
    info = auth.exchange_code_for_email(code)
    if not info:
        st.error("Couldn't verify your Google account.")
        st.query_params.clear()
        return
    email = info["email"]
    auth.sign_in_email(email, info.get("name", ""), info.get("picture", ""))
    profile = db.get_profile(email)
    # If already verified + onboarded, jump straight in
    if profile and profile.get("email_verified") and profile.get("onboarded"):
        set_page("chat")
    else:
        # Send OTP and go to verify
        if auth.send_otp(email):
            st.session_state["otp_pending"] = True
        set_page("verify")
    st.rerun()


handle_oauth_callback()


# ─── Landing page ───────────────────────────────────────────────────────────
def render_landing():
    st.markdown(
        """
        <div class="nx-hero">
            <span class="nx-pill"><span class="dot"></span> Live · Real-time AI</span>
            <h1>Your AI career & news<br/>companion</h1>
            <p>Personalized jobs, internships, scholarships and news —
            streamed in real time. Powered by AI that actually knows what you're after.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        if st.button("Continue with Google", use_container_width=True, type="primary"):
            st.markdown(
                f'<meta http-equiv="refresh" content="0;url={auth.google_login_url()}">',
                unsafe_allow_html=True,
            )
            st.stop()
        st.caption(
            "By continuing you agree to our [Terms](?page=terms) and "
            "[Privacy](?page=privacy)."
        )

    # Features
    st.markdown("### Built for opportunity hunters", unsafe_allow_html=True)
    features = [
        ("🎯", "Personalized to you", "Tell us your field, city and interests once — every result is filtered for you."),
        ("⚡", "Real-time search", "Fresh jobs, scholarships and news pulled live from across the web."),
        ("🧠", "Memory + RAG", "Nexa remembers what you've seen and ranks new opportunities accordingly."),
        ("📬", "Daily digests", "Optional email summaries so you never miss a deadline."),
        ("🔐", "Your data, isolated", "Row-level security in Postgres. Only you see your data."),
        ("📱", "Anywhere, anytime", "Premium mobile UI. Use it on the bus, in class, in bed."),
    ]
    cols = st.columns(3)
    for i, (icon, title, body) in enumerate(features):
        with cols[i % 3]:
            st.markdown(
                f'<div class="nx-feat"><div class="nx-icon-wrap">{icon}</div>'
                f'<h3>{title}</h3><p>{body}</p></div>',
                unsafe_allow_html=True,
            )

    # Pricing
    st.markdown("### Simple pricing")
    p1, p2, p3 = st.columns(3)
    with p1:
        st.markdown(
            '<div class="nx-price"><h4>Free</h4>'
            '<div class="nx-amount">$0</div>'
            '<p>30 messages / 10 min · daily digest · core commands</p></div>',
            unsafe_allow_html=True,
        )
    with p2:
        st.markdown(
            '<div class="nx-price featured"><h4>Pro</h4>'
            '<div class="nx-amount">$9</div>'
            '<p>Unlimited chat · priority search · 3 saved alert profiles</p></div>',
            unsafe_allow_html=True,
        )
    with p3:
        st.markdown(
            '<div class="nx-price"><h4>Team</h4>'
            '<div class="nx-amount">$29</div>'
            '<p>For career centers · admin tools · bulk broadcasts</p></div>',
            unsafe_allow_html=True,
        )


# ─── OTP verify ─────────────────────────────────────────────────────────────
def render_verify():
    email = auth.current_email()
    if not email:
        set_page("landing")
        st.rerun()

    st.markdown('<div class="nx-hero"><h1>Check your inbox</h1></div>', unsafe_allow_html=True)
    st.markdown(
        f"<p style='text-align:center;color:#9A9AB0;'>We sent a 6-digit code to "
        f"<b>{email}</b>. It expires in 10 minutes.</p>",
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        code = st.text_input("Verification code", max_chars=6, placeholder="123456")
        col_a, col_b = st.columns(2)
        if col_a.button("Verify", use_container_width=True, type="primary"):
            ok, msg = auth.verify_otp(email, code)
            if ok:
                st.success("Email verified ✓")
                profile = db.get_profile(email)
                if profile and profile.get("onboarded"):
                    set_page("chat")
                else:
                    set_page("onboarding")
                st.rerun()
            else:
                st.error(msg)
        if col_b.button("Resend code", use_container_width=True):
            if auth.send_otp(email):
                st.toast("New code sent.")
        st.button("Sign out", on_click=lambda: (auth.sign_out(), set_page("landing")))


# ─── Onboarding ─────────────────────────────────────────────────────────────
INTEREST_OPTIONS = [
    "Internships", "Jobs", "Scholarships",
    "Current Affairs", "Tech News", "Govt Exams",
]
EDU_LEVELS = ["High School", "Undergraduate", "Graduate", "Postgraduate", "Working professional"]


def render_onboarding():
    email = auth.current_email()
    if not email:
        set_page("landing"); st.rerun()

    profile = db.get_profile(email) or {}
    st.markdown('<div class="nx-hero"><h1>Tell us about you</h1>'
                '<p>Takes 30 seconds. Powers everything that comes next.</p></div>',
                unsafe_allow_html=True)

    with st.form("onboarding"):
        full_name = st.text_input("Full name *", value=profile.get("full_name", ""))
        c1, c2 = st.columns(2)
        phone = c1.text_input("Phone number *", value=profile.get("phone", ""))
        location = c2.text_input("City / location *", value=profile.get("location", ""))
        edu = c1.selectbox(
            "Education level *", EDU_LEVELS,
            index=EDU_LEVELS.index(profile["education_level"])
            if profile.get("education_level") in EDU_LEVELS else 0,
        )
        field = c2.text_input("Field of study *", value=profile.get("field_of_study", ""),
                              placeholder="e.g. Computer Science")
        interests = st.multiselect(
            "Interested in *", INTEREST_OPTIONS,
            default=profile.get("interests") or ["Jobs", "Internships"],
        )
        companies_raw = st.text_input(
            "Preferred companies (comma-separated)",
            value=", ".join(profile.get("preferred_companies") or []),
            placeholder="Google, Systems Limited, Careem",
        )
        submitted = st.form_submit_button("Save & continue", type="primary",
                                          use_container_width=True)
        if submitted:
            errs = []
            if not full_name: errs.append("Full name")
            if not phone: errs.append("Phone")
            if not location: errs.append("Location")
            if not field: errs.append("Field of study")
            if not interests: errs.append("At least one interest")
            if errs:
                st.error("Please fill: " + ", ".join(errs))
            else:
                companies = [c.strip() for c in companies_raw.split(",") if c.strip()]
                db.upsert_profile(
                    email,
                    full_name=full_name, phone=phone, location=location,
                    education_level=edu, field_of_study=field,
                    interests=interests, preferred_companies=companies,
                )
                db.mark_onboarded(email)
                set_page("chat")
                st.rerun()


# ─── Chat ───────────────────────────────────────────────────────────────────
SUGGESTIONS = [
    "Latest HEC scholarships",
    "Remote CS internships",
    "Today's tech news in 3 bullets",
    "Govt exam dates this month",
]


def _render_message(role: str, content: str, mid: str | None = None):
    avatar_class = "user" if role == "user" else "bot"
    initial = "U" if role == "user" else "N"
    safe = content.replace("<", "&lt;").replace(">", "&gt;")
    # Re-allow basic markdown links by passing through st.markdown for body
    st.markdown(
        f'<div class="nx-msg"><div class="nx-avatar {avatar_class}">{initial}</div>'
        f'<div class="nx-msg-body" id="msg-{mid or ""}">',
        unsafe_allow_html=True,
    )
    st.markdown(content)
    st.markdown("</div></div>", unsafe_allow_html=True)


def _ensure_session(email: str) -> str:
    sid = st.session_state.get("current_session_id")
    if sid:
        return sid
    sess = db.create_chat_session(email, "New chat")
    st.session_state["current_session_id"] = sess["id"]
    st.session_state["messages"] = []
    return sess["id"]


def render_chat():
    email = auth.current_email()
    if not email:
        set_page("landing"); st.rerun()

    profile = db.get_profile(email)
    if not profile or not profile.get("email_verified"):
        set_page("verify"); st.rerun()
    if not profile.get("onboarded"):
        set_page("onboarding"); st.rerun()

    _render_sidebar(email, profile)

    sid = _ensure_session(email)
    messages = st.session_state.get("messages")
    if messages is None:
        messages = db.list_messages(email, sid)
        st.session_state["messages"] = messages

    # Header
    st.markdown(
        f"<h2 style='margin:0 0 4px;'>Hey {profile.get('full_name','').split(' ')[0] or 'there'} 👋</h2>"
        f"<p style='color:#9A9AB0;margin:0 0 24px;'>Ask me anything — try <code>/jobs</code>, "
        f"<code>/scholarships</code>, <code>/news</code>, <code>/internships</code>.</p>",
        unsafe_allow_html=True,
    )

    # Empty state — suggestion chips
    if not messages:
        cols = st.columns(2)
        for i, s in enumerate(SUGGESTIONS):
            if cols[i % 2].button(f"✨  {s}", key=f"sug-{i}", use_container_width=True):
                _submit(email, sid, s)
                st.rerun()

    # Render history
    for m in messages:
        _render_message(m["role"], m["content"], mid=m.get("id"))

    # Input
    user_input = st.chat_input("Message Nexa…")
    if user_input:
        _submit(email, sid, user_input)
        st.rerun()


def _submit(email: str, sid: str, text: str):
    ok, count = chatbot.check_rate_limit(email)
    if not ok:
        st.error(f"You've hit your rate limit ({count}/10 min). Wait a few minutes.")
        return

    text = chatbot.sanitize(text)
    # Persist user msg
    user_row = db.add_message(email, sid, "user", text)
    st.session_state["messages"].append(user_row)

    # Stream assistant
    profile = db.get_profile(email)
    history = st.session_state["messages"]

    placeholder = st.empty()
    full = ""
    with placeholder.container():
        _render_message("assistant", "▍")

    for chunk in chatbot.handle_turn(text, history, profile):
        full += chunk
        with placeholder.container():
            _render_message("assistant", full + "▍")

    with placeholder.container():
        _render_message("assistant", full or "*(no response)*")

    assistant_row = db.add_message(email, sid, "assistant", full)
    st.session_state["messages"].append(assistant_row)

    # Auto-title new chats
    sessions = db.list_chat_sessions(email)
    current = next((s for s in sessions if s["id"] == sid), None)
    if current and current["title"] == "New chat":
        title = text[:60] + ("…" if len(text) > 60 else "")
        db.rename_chat_session(email, sid, title)


# ─── Sidebar ────────────────────────────────────────────────────────────────
def _render_sidebar(email: str, profile: dict | None):
    with st.sidebar:
        st.markdown('<div class="nx-logo">✨ Nexa</div>', unsafe_allow_html=True)

        if st.button("➕  New chat", use_container_width=True, type="primary"):
            sess = db.create_chat_session(email)
            st.session_state["current_session_id"] = sess["id"]
            st.session_state["messages"] = []
            st.rerun()

        st.markdown("##### Recent chats")
        sessions = db.list_chat_sessions(email)
        cur = st.session_state.get("current_session_id")
        for s in sessions[:25]:
            label = s["title"] or "Untitled"
            is_cur = s["id"] == cur
            if st.button(
                ("●  " if is_cur else "○  ") + label[:30],
                key=f"sess-{s['id']}",
                use_container_width=True,
            ):
                st.session_state["current_session_id"] = s["id"]
                st.session_state["messages"] = None
                st.rerun()

        st.divider()
        st.markdown("##### Account")
        if profile:
            st.caption(f"📧 {email}")
            if profile.get("location"):
                st.caption(f"📍 {profile['location']}")

        if st.button("👤  Edit profile", use_container_width=True):
            set_page("onboarding"); st.rerun()

        if st.button("💎  Upgrade plan", use_container_width=True):
            st.toast("Upgrade flow coming soon ✨")

        if auth.is_owner():
            if st.button("🛠️  Admin", use_container_width=True):
                set_page("admin"); st.rerun()

        if st.button("📜  Terms", use_container_width=True):
            set_page("terms"); st.rerun()
        if st.button("🔒  Privacy", use_container_width=True):
            set_page("privacy"); st.rerun()

        with st.expander("⚠️ Danger zone"):
            st.caption("Delete your account and all chats permanently.")
            confirm = st.text_input("Type DELETE to confirm", key="del-confirm")
            if st.button("Delete my account", use_container_width=True):
                if confirm == "DELETE":
                    db.delete_user(email)
                    auth.sign_out()
                    set_page("landing"); st.rerun()
                else:
                    st.error("Type DELETE exactly.")

        if st.button("🚪  Sign out", use_container_width=True):
            auth.sign_out()
            set_page("landing"); st.rerun()


# ─── Static pages ───────────────────────────────────────────────────────────
def render_terms():
    st.markdown("# Terms of Service")
    st.markdown("""
Nexa is provided **as-is** without warranties. You agree to use it lawfully
and not to abuse our APIs. We may suspend accounts that violate these terms.
Opportunities surfaced by Nexa come from third-party sources — always verify
deadlines and authenticity before acting. Pricing and limits may change.
""")
    if st.button("← Back"): set_page("chat" if auth.current_email() else "landing"); st.rerun()


def render_privacy():
    st.markdown("# Privacy Policy")
    st.markdown("""
We collect only what you give us in your profile (name, contact, interests)
and your chat history — to personalize results. We **never sell your data**.
You can delete your account at any time from the sidebar's *Danger zone*.
All data is stored in a Postgres database with row-level security; only you
and the platform owner (for support) can access your records.
""")
    if st.button("← Back"): set_page("chat" if auth.current_email() else "landing"); st.rerun()


# ─── Router ─────────────────────────────────────────────────────────────────
def main():
    page = get_page()
    signed_in = bool(auth.current_email())

    if page == "terms":      return render_terms()
    if page == "privacy":    return render_privacy()
    if page == "admin":      return admin_page.render_admin()

    if not signed_in:
        return render_landing()

    if page == "verify":     return render_verify()
    if page == "onboarding": return render_onboarding()

    # Default for signed-in users → chat (but gate on verify/onboard)
    profile = db.get_profile(auth.current_email())
    if not profile or not profile.get("email_verified"):
        if not st.session_state.get("otp_pending"):
            auth.send_otp(auth.current_email())
            st.session_state["otp_pending"] = True
        return render_verify()
    if not profile.get("onboarded"):
        return render_onboarding()

    return render_chat()


if __name__ == "__main__":
    main()
