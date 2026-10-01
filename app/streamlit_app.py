"""Risk & Compliance Copilot -- Streamlit entry point (local and Streamlit in Snowflake)."""

import streamlit as st
from lib.db import CFG, HERO

st.set_page_config(page_title=CFG["title"], page_icon=":material/shield:", layout="wide")

pages = [
    st.Page("app_pages/overview.py", title="Overview", icon=":material/monitoring:", default=True),
    st.Page("app_pages/case.py", title="Case investigation", icon=":material/manage_search:"),
    st.Page("app_pages/signoff.py", title="Officer sign-off", icon=":material/verified:"),
    st.Page("app_pages/ask.py", title="Ask the data", icon=":material/chat:"),
    st.Page("app_pages/governance.py", title="Governance & audit", icon=":material/policy:"),
]
nav = st.navigation(pages)

with st.sidebar:
    st.markdown(f"**{CFG['title']}**")
    st.caption(f"Database `{CFG['database']}` · profile `{CFG['profile']}`")
    if HERO:
        st.caption(f"Demo customer: `{HERO['party_id']}` (confirmed by detection)")
    st.caption(
        "Thresholds come from governed POLICY_CLAUSE rows. STR narratives are "
        "template-generated on this account; every figure comes from SQL."
    )

nav.run()
