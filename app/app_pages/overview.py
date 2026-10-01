"""Overview: what the system is watching, what it caught, and how well."""

import altair as alt
import streamlit as st
from lib import data
from lib.db import HERO, inr, lakh

st.title("Overview")
st.caption(
    "Suspicious patterns detected in SQL against governed thresholds; every alert is a case."
)

live = st.toggle(
    "Live mode", help="Re-read every 5 seconds -- use while `riskcopilot replay` streams data."
)


@st.fragment(run_every=5 if live else None)
def headline() -> None:
    k = data.kpis()
    ev = data.latest_detection_eval()
    c = st.columns(5)
    c[0].metric("Transactions monitored", f"{int(k['txns']):,}")
    c[1].metric("Signals raised", f"{int(k['signals']):,}")
    c[2].metric(
        "Open cases", f"{int(k['open_cases']):,}", f"{int(k['cases'])} total", delta_color="off"
    )
    c[3].metric("STRs filed", f"{int(k['filed']):,}")
    if ev:
        c[4].metric(
            "Detection recall / precision",
            f"{ev['recall']:.0%} / {ev['precision']:.0%}",
            f"{ev['tp']}/{ev['expected']} planted caught",
            delta_color="off",
        )
    st.caption(f"Data through {k['data_through']}")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Case queue")
        q = data.case_queue()
        if q.empty:
            st.info("No cases yet. Signals are routed into cases by ROUTE_SIGNALS.")
        else:
            q["flagged"] = q["flagged_inr"].map(lakh)
            shown = [
                "case_id",
                "full_name",
                "branch_region",
                "severity",
                "status",
                "signals",
                "flagged",
                "opened_at",
            ]
            if HERO:
                q["demo"] = q["case_id"].isin(HERO["case_ids"]).map({True: "hero", False: ""})
                shown.append("demo")
            st.dataframe(q[shown], hide_index=True, use_container_width=True)
    with right:
        st.subheader("Latest signals")
        f = data.signals_feed()
        if not f.empty:
            f["total"] = f["total_inr"].map(inr)
            st.dataframe(
                f[["last_seen_at", "party_id", "rule_id", "deposits", "total", "case_id"]],
                hide_index=True,
                use_container_width=True,
            )


headline()

# Outside the fragment: navigation from an auto-refreshing fragment is fragile.
queue = data.case_queue()
if not queue.empty:
    ids = queue["case_id"].tolist()
    names = queue.set_index("case_id")["full_name"].to_dict()
    hero_first = next((i for i, c in enumerate(ids) if HERO and c in HERO["case_ids"]), 0)
    o1, o2 = st.columns([4, 1], vertical_alignment="bottom")
    chosen = o1.selectbox(
        "Open a case", ids, index=hero_first, format_func=lambda c: f"{c} -- {names.get(c)}"
    )
    if o2.button("Investigate", type="primary", use_container_width=True):
        st.session_state["case_id"] = chosen
        st.switch_page("app_pages/case.py")

st.subheader("Daily credit activity")
d = data.daily_activity()
if not d.empty:
    base = alt.Chart(d).encode(x=alt.X("txn_date:T", title="Date"))
    bars = base.mark_bar(opacity=0.35).encode(y=alt.Y("credits:Q", title="Credits"))
    cash = base.mark_line(point=True).encode(y=alt.Y("cash_credits:Q", title="Cash credits"))
    st.altair_chart(alt.layer(bars, cash).resolve_scale(y="independent"), use_container_width=True)
    st.caption("Bars: all credits per day. Line: cash deposits, where structuring hides.")
