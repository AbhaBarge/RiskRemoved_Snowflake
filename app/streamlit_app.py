"""Risk & Compliance Copilot -- Streamlit entry point.

Runs locally, in Streamlit in Snowflake, and on Streamlit Community Cloud; only
the session differs (lib/db.py). This file draws what every page shares: the
stylesheet, the appearance toggle, the sidebar's identity card and the global
filters (lib/filters.py), then hands over to the page.
"""

from pathlib import Path

import streamlit as st
from lib import auth, filters, login, ui
from lib.db import CFG, HERO

st.set_page_config(
    page_title=CFG["title"],
    page_icon=":material/shield:",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.begin_run()
filters.keep()
if ui.MODE_KEY in st.session_state:  # kept across the sign-in screen, which has no toggle
    st.session_state[ui.MODE_KEY] = st.session_state[ui.MODE_KEY]
ui.style()
st.logo(str(Path(__file__).parent / "static" / "logo.svg"), size="large")

pages = [
    st.Page("app_pages/overview.py", title="Overview", icon=":material/monitoring:", default=True),
    st.Page("app_pages/case.py", title="Case investigation", icon=":material/manage_search:"),
    st.Page("app_pages/signoff.py", title="Officer sign-off", icon=":material/verified:"),
    st.Page("app_pages/ask.py", title="Ask the data", icon=":material/chat:"),
    st.Page("app_pages/liquidity.py", title="Liquidity (ALM)", icon=":material/water_drop:"),
    st.Page("app_pages/governance.py", title="Governance & audit", icon=":material/policy:"),
]
user = auth.current()
# Every page stays registered, so a deep link survives signing in; the menu is
# hidden and the sign-in screen is drawn instead of the page until then.
nav = st.navigation(pages, position="sidebar" if user else "hidden")
if user is None:
    ui.theme_hook(st.session_state.get(ui.MODE_KEY))
    login.render()
    st.stop()

st.session_state.setdefault(ui.MODE_KEY, st.context.theme.type == "dark")
ui.theme_hook(st.session_state[ui.MODE_KEY])

with st.sidebar:
    ui.user_card(
        user["name"],
        user["title"],
        "Maker" if user.get("can_investigate") else "Checker",
        "violet" if user.get("can_investigate") else "emerald",
    )
    st.button(
        "Sign out",
        on_click=auth.sign_out,
        icon=":material/logout:",
        type="tertiary",
        width="stretch",
    )
    ui.side_label("Appearance")
    st.toggle(
        "Dark mode",
        key=ui.MODE_KEY,
        help="Deep slate or studio white. Remembered on this browser.",
    )
    filters.panel(filters.SCOPES.get(nav.url_path or "overview", ()))
    ui.side_label("Workspace")
    st.markdown(
        ui.pill(CFG["database"], "slate", glyph="database")
        + " "
        + ui.pill(f"profile {CFG['profile']}", "slate", glyph="tune"),
        unsafe_allow_html=True,
    )
    if HERO:
        st.caption(f"Demo customer `{HERO['party_id']}`, confirmed by detection.")
    st.caption("Thresholds come from governed policy clauses; every figure is computed in SQL.")

nav.run()
