"""
admin.py — Owner-only dashboard for Nexa.

Access: only the email matching APP_OWNER_EMAIL.
Routes inside: Overview · Users · Chats · Opportunities · Broadcasts
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

import auth
import database as db


def render_admin():
    if not auth.is_owner():
        st.error("🚫 Access denied. This page is for the app owner only.")
        st.stop()

    st.markdown("## 🛠️ Admin Dashboard")
    st.caption(f"Signed in as owner: `{auth.current_email()}`")

    tab_overview, tab_users, tab_chats, tab_opps, tab_broadcast = st.tabs(
        ["📊 Overview", "👥 Users", "💬 Chats", "🎯 Opportunities", "📣 Broadcasts"]
    )

    # ─── Overview ───────────────────────────────────────────────────────────
    with tab_overview:
        s = db.admin_stats()
        c1, c2, c3 = st.columns(3)
        c1.metric("Total users", s["total_users"])
        c2.metric("Messages (24h)", s["messages_24h"])
        c3.metric("Messages (30d)", s["messages_30d"])

        users = db.admin_list_users()
        if users:
            df = pd.DataFrame(users)
            # Top interests
            all_int = []
            for u in users:
                all_int.extend(u.get("interests") or [])
            if all_int:
                ic = pd.Series(all_int).value_counts().reset_index()
                ic.columns = ["interest", "users"]
                fig = px.bar(ic, x="interest", y="users", title="Top user interests",
                             color="users", color_continuous_scale="Purples")
                fig.update_layout(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                                  font_color="#EAEAF2")
                st.plotly_chart(fig, use_container_width=True)

            df["created_at"] = pd.to_datetime(df["created_at"])
            sign_ups = df.groupby(df["created_at"].dt.date).size().reset_index(name="signups")
            fig2 = px.line(sign_ups, x="created_at", y="signups", title="Sign-ups over time",
                           markers=True)
            fig2.update_traces(line_color="#7C5CFF")
            fig2.update_layout(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                               font_color="#EAEAF2")
            st.plotly_chart(fig2, use_container_width=True)

    # ─── Users ──────────────────────────────────────────────────────────────
    with tab_users:
        users = db.admin_list_users()
        if not users:
            st.info("No users yet.")
        else:
            df = pd.DataFrame(users)[
                ["email", "full_name", "field_of_study", "location",
                 "interests", "email_verified", "onboarded", "created_at"]
            ]
            st.dataframe(df, use_container_width=True, hide_index=True)

            with st.expander("⚠️ Danger zone — delete a user"):
                email = st.text_input("User email to delete")
                if st.button("Delete user permanently", type="primary"):
                    if email:
                        db.delete_user(email.strip().lower())
                        st.success(f"Deleted {email}")
                        st.rerun()

    # ─── Chats ──────────────────────────────────────────────────────────────
    with tab_chats:
        users = db.admin_list_users()
        if not users:
            st.info("No users yet.")
        else:
            emails = [u["email"] for u in users]
            pick = st.selectbox("Pick a user", emails)
            if pick:
                chats = db.admin_user_chats(pick)
                if not chats:
                    st.caption("No chats for this user.")
                for c in chats:
                    with st.expander(f"💬 {c['title']} — {c['created_at']}"):
                        for m in c["messages"]:
                            role = "🧑" if m["role"] == "user" else "🤖"
                            st.markdown(f"**{role} {m['role']}**")
                            st.markdown(m["content"])
                            st.divider()

    # ─── Opportunities CRUD ─────────────────────────────────────────────────
    with tab_opps:
        st.markdown("#### Add a curated opportunity")
        with st.form("new_opp", clear_on_submit=True):
            c1, c2 = st.columns(2)
            kind = c1.selectbox("Kind", ["job", "internship", "scholarship", "news"])
            title = c2.text_input("Title")
            org = c1.text_input("Organization")
            url = c2.text_input("URL")
            location = c1.text_input("Location")
            deadline = c2.date_input("Deadline (optional)", value=None)
            desc = st.text_area("Description")
            if st.form_submit_button("Create"):
                if title:
                    db.create_opportunity(
                        kind=kind, title=title, org=org, url=url, location=location,
                        deadline=deadline.isoformat() if deadline else None,
                        description=desc,
                    )
                    st.success("Created.")
                    st.rerun()

        st.markdown("#### Existing opportunities")
        opps = db.list_opportunities()
        if not opps:
            st.caption("None yet.")
        for o in opps:
            with st.expander(f"[{o['kind']}] {o['title']} — {o.get('org','')}"):
                st.write(o.get("description", ""))
                if o.get("url"):
                    st.markdown(f"[Open link →]({o['url']})")
                if st.button("Delete", key=f"del-{o['id']}"):
                    db.delete_opportunity(o["id"])
                    st.rerun()

    # ─── Broadcasts ─────────────────────────────────────────────────────────
    with tab_broadcast:
        st.markdown("#### Send a broadcast")
        st.caption("Stored in DB and shown to users in the announcements feed next visit.")
        with st.form("broadcast", clear_on_submit=True):
            title = st.text_input("Title")
            body = st.text_area("Body (markdown supported)")
            interest_filter = st.multiselect(
                "Filter by interest (empty = all users)",
                ["Internships", "Jobs", "Scholarships", "Current Affairs",
                 "Tech News", "Govt Exams"],
            )
            if st.form_submit_button("Send"):
                db.create_broadcast(title, body, {"interests": interest_filter})
                st.success("Broadcast queued.")
                st.rerun()

        st.markdown("#### History")
        for b in db.list_broadcasts():
            with st.expander(f"{b['title']} — {b['sent_at']}"):
                st.markdown(b["body"])
                if b.get("filter"):
                    st.caption(f"Filter: {b['filter']}")
