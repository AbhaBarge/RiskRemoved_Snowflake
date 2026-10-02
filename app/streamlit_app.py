"""Risk & Compliance Copilot -- Streamlit entry point.

Runs locally, in Streamlit in Snowflake, and on Streamlit Community Cloud; only
the session differs (lib/db.py).
"""

import streamlit as st
from lib import auth, login
from lib.db import CFG, HERO

st.set_page_config(page_title=CFG["title"], page_icon=":material/shield:", layout="wide")

pages = [
    st.Page("app_pages/overview.py", title="Overview", icon=":material/monitoring:", default=True),
    st.Page("app_pages/case.py", title="Case investigation", icon=":material/manage_search:"),
    st.Page("app_pages/signoff.py", title="Officer sign-off", icon=":material/verified:"),
    st.Page("app_pages/ask.py", title="Ask the data", icon=":material/chat:"),
    st.Page("app_pages/governance.py", title="Governance & audit", icon=":material/policy:"),
]
user = auth.current()
# Every page stays registered, so a deep link survives signing in; the menu is
# hidden and the sign-in screen is drawn instead of the page until then.
nav = st.navigation(pages, position="sidebar" if user else "hidden")
if user is None:
    login.render()
    st.stop()

with st.sidebar:
    with st.container(border=True):
        st.markdown(f"{user['icon']} **{user['name']}**")
        st.caption(user["title"])
        st.button("Sign out", on_click=auth.sign_out, use_container_width=True)
    st.markdown(f"**{CFG['title']}**")
    st.caption(f"Database `{CFG['database']}` · profile `{CFG['profile']}`")
    if HERO:
        st.caption(f"Demo customer: `{HERO['party_id']}` (confirmed by detection)")
    st.caption(
        "Thresholds come from governed POLICY_CLAUSE rows. STR narratives are "
        "template-generated on this account; every figure comes from SQL."
    )

nav.run()
