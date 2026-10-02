"""The sign-in screen. The demo credentials are on it on purpose: see lib/auth.py.

Rendered in place of whichever page was requested, so a deep link (or the
officer switching in from sign-off) lands back on that page after signing in.
"""

from __future__ import annotations

import streamlit as st

from lib import auth
from lib.db import CFG


def render() -> None:
    st.title(CFG["title"])
    st.markdown(
        "From a suspicious pattern to a regulator-ready **Suspicious Transaction Report**, "
        "with every figure proven in SQL and a human signature on every filing."
    )

    left, right = st.columns([5, 6], gap="large")
    with left, st.container(border=True):
        st.subheader("Sign in")
        with st.form("sign_in", border=False):
            username = st.text_input("Username", key="login_user")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)
        if submitted:
            if auth.sign_in(username, password):
                st.rerun()
            st.error("Those credentials do not match a demo user. Use one of the accounts shown.")

    with right:
        st.subheader("Demo accounts")
        st.caption(
            "Copy the credentials, or click **Use** to fill the form. Two roles, two duties."
        )
        for key, persona in auth.PERSONAS.items():
            with st.container(border=True):
                head, use = st.columns([4, 1], vertical_alignment="center")
                head.markdown(f"{persona['icon']} **{persona['title']}** · {persona['name']}")
                use.button(
                    "Use",
                    key=f"use_{key}",
                    on_click=auth.fill,
                    args=(key,),
                    use_container_width=True,
                )
                st.caption(persona["duty"])
                u, p = st.columns(2)
                with u:
                    st.code(key, language=None)
                with p:
                    st.code(auth.DEMO_PASSWORDS[key], language=None)

    with st.expander("Security model: what this page shows, and what a bank would use"):
        for note in auth.SECURITY_NOTES:
            st.markdown(f"- {note}")
