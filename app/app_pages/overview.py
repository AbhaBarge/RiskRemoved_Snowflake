"""Overview: what the system is watching, what it caught, and how well."""

import html

import altair as alt
import pandas as pd
import streamlit as st
from lib import data, ui
from lib.db import HERO, inr, lakh

ui.style()
st.title("Overview")
st.caption(
    "Suspicious patterns detected in SQL against governed thresholds; every alert is a case, "
    "and every filed STR carries a human signature."
)

live = st.toggle(
    "Live mode", help="Re-read every 5 seconds -- use while `riskcopilot replay` streams data."
)


def _pipeline() -> None:
    f = data.funnel()
    ev = data.latest_detection_eval()
    ui.strip(
        [
            ("Transactions", f"{f['txns']:,}", "monitored"),
            ("Cash deposits", f"{f['cash']:,}", "where structuring hides"),
            ("Signals", f"{f['signals']:,}", "rules fired in SQL"),
            ("Cases", f"{f['cases']:,}", "one per customer"),
            ("Investigated", f"{f['investigated']:,}", "evidence gathered"),
            ("STRs drafted", f"{f['drafted']:,}", "from a governed template"),
            ("Approved", f"{f['approved']:,}", "by the Reviewer"),
            ("Filed", f"{f['filed']:,}", "signed by the officer"),
        ],
        done=7 if f["filed"] else None,
    )
    k = data.kpis()
    c = st.columns(4)
    c[0].metric(
        "Open cases", f"{int(k['open_cases']):,}", f"{int(k['cases'])} total", delta_color="off"
    )
    c[1].metric("STRs filed", f"{int(k['filed']):,}")
    if ev:
        c[2].metric(
            "Detection recall",
            f"{ev['recall']:.0%}",
            f"{ev['tp']}/{ev['expected']} planted patterns caught",
            delta_color="off",
        )
        c[3].metric(
            "Detection precision",
            f"{ev['precision']:.0%}",
            f"{ev['predicted']} signals raised",
            delta_color="off",
        )
    scope = (
        f"rule {ev['rule_id']}, scored against {ev['expected']} planted patterns, "
        "plus a legitimate near-threshold deposit as the false-positive control"
        if ev
        else "no `verify` run yet"
    )
    st.caption(f"Data through {k['data_through']} · detection scores: {scope}")


def _hero(q: pd.DataFrame) -> None:
    if not HERO or q.empty:
        return
    row = q[q["case_id"].isin(HERO["case_ids"])]
    if row.empty:
        return
    r = row.iloc[0]
    colour = ui.STATUS_COLOURS.get(r["status"], ui.SLATE)
    ui.callout(
        f"<b>Demo case</b> · {html.escape(str(r['full_name']))} "
        f"(<code>{html.escape(str(r['case_id']))}</code>, {html.escape(str(r['branch_region']))}) "
        f"{ui.chip(str(r['status']), colour)}<br>"
        f"{int(r['signals'])} structuring signal(s) over cash deposits totalling "
        f"<b>{html.escape(lakh(r['flagged_inr']))}</b>, each kept just under the cash "
        "reporting line. Open it below and follow it to a filed STR."
    )


def _cases(q: pd.DataFrame) -> None:
    head, switch = st.columns([4, 1], vertical_alignment="bottom")
    head.subheader("Case queue")
    with switch:
        show_visual = ui.visual("ov_cases")
    if q.empty:
        st.info("No cases yet. Signals are routed into cases by ROUTE_SIGNALS.")
        return
    q = q.copy()
    q["flagged_lakh"] = q["flagged_inr"].astype(float) / 100000
    q["flagged"] = q["flagged_inr"].map(lakh)
    q["demo"] = q["case_id"].isin(HERO["case_ids"]) if HERO else False
    if not show_visual:
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
        st.dataframe(ui.pretty(q[shown]), hide_index=True, width="stretch")
        return
    left, right = st.columns([3, 2], gap="large")
    with left:
        bars = (
            alt.Chart(q)
            .mark_bar(cornerRadiusEnd=4)
            .encode(
                y=alt.Y("full_name:N", sort="-x", title=None),
                x=alt.X("flagged_lakh:Q", title="Cash flagged (₹ lakh)"),
                color=alt.Color(
                    "branch_region:N",
                    title="Branch",
                    scale=alt.Scale(range=ui.BRANCH_SCHEME),
                    legend=alt.Legend(orient="bottom"),
                ),
                opacity=alt.condition("datum.demo", alt.value(1.0), alt.value(0.6)),
                stroke=alt.condition("datum.demo", alt.value(ui.RED), alt.value(None)),
                strokeWidth=alt.condition("datum.demo", alt.value(2.5), alt.value(0)),
                tooltip=[
                    alt.Tooltip("full_name:N", title="Customer"),
                    alt.Tooltip("case_id:N", title="Case"),
                    alt.Tooltip("status:N", title="Status"),
                    alt.Tooltip("signals:Q", title="Signals"),
                    alt.Tooltip("flagged:N", title="Flagged"),
                ],
            )
        )
        labels = bars.mark_text(align="left", dx=4, color=ui.NAVY, fontSize=11).encode(
            text="flagged:N", stroke=alt.value(None), opacity=alt.value(1.0)
        )
        chart = (bars + labels).properties(height=34 * len(q) + 30)
        st.altair_chart(chart.configure_axis(labelLimit=180), width="stretch")
        st.caption("Cash deposits behind each case. The outlined, solid bar is the demo case.")
    with right:
        status = (
            alt.Chart(q)
            .mark_bar(cornerRadiusEnd=4)
            .encode(
                y=alt.Y("branch_region:N", title=None),
                x=alt.X("count():Q", title="Cases"),
                color=alt.Color(
                    "status:N",
                    title="Status",
                    scale=ui.status_scale(q["status"].unique()),
                    legend=alt.Legend(orient="bottom"),
                ),
                tooltip=["branch_region:N", "status:N", alt.Tooltip("count():Q", title="Cases")],
            )
            .properties(height=180)
        )
        st.altair_chart(status, width="stretch")
        st.caption(
            "Cases by branch and status. Branch-scoped analyst roles exist; enforcing them "
            "row by row needs row access policies (Enterprise edition)."
        )


def _signals() -> None:
    head, switch = st.columns([4, 1], vertical_alignment="bottom")
    head.subheader("Latest signals")
    with switch:
        show_visual = ui.visual("ov_signals")
    f = data.signals_feed()
    if f.empty:
        st.caption("No signals yet.")
        return
    f = f.copy()
    f["total"] = f["total_inr"].map(inr)
    f["total_lakh"] = f["total_inr"].astype(float) / 100000
    if not show_visual:
        st.dataframe(
            ui.pretty(f[["last_seen_at", "party_id", "rule_id", "deposits", "total", "case_id"]]),
            hide_index=True,
            width="stretch",
        )
        return
    chart = (
        alt.Chart(f)
        .mark_circle(opacity=0.8, stroke=ui.NAVY, strokeWidth=0.5)
        .encode(
            x=alt.X("last_seen_at:T", title="Signal raised"),
            y=alt.Y("total_lakh:Q", title="Deposits in window (₹ lakh)"),
            size=alt.Size("deposits:Q", scale=alt.Scale(range=[60, 600]), legend=None),
            color=alt.Color(
                "rule_id:N",
                title="Rule",
                scale=alt.Scale(range=ui.BRANCH_SCHEME),
                legend=alt.Legend(orient="bottom", labelLimit=320),
            ),
            tooltip=[
                "party_id:N",
                "rule_id:N",
                "deposits:Q",
                "total:N",
                "case_id:N",
                alt.Tooltip("last_seen_at:T", title="Raised", format="%d %b %H:%M"),
            ],
        )
        .properties(height=240)
    )
    st.altair_chart(chart, width="stretch")
    st.caption("Each bubble is one signal: how much cash, and when. Bigger means more deposits.")


@st.fragment(run_every=5 if live else None)
def headline() -> None:
    _pipeline()
    q = data.case_queue()
    _hero(q)
    _cases(q)
    _signals()


headline()

# Outside the fragment: navigation from an auto-refreshing fragment is fragile.
queue = data.case_queue()
if not queue.empty:
    ids = queue["case_id"].tolist()
    names = queue.set_index("case_id")["full_name"].to_dict()
    hero_first = next((i for i, c in enumerate(ids) if HERO and c in HERO["case_ids"]), 0)
    with st.container(border=True):
        o1, o2 = st.columns([4, 1], vertical_alignment="bottom")
        chosen = o1.selectbox(
            "Open a case", ids, index=hero_first, format_func=lambda c: f"{c} -- {names.get(c)}"
        )
        if o2.button("Investigate", type="primary", width="stretch"):
            st.session_state["case_id"] = chosen
            st.switch_page("app_pages/case.py")

st.subheader("Daily credit activity")
d = data.daily_activity()
if not d.empty:
    base = alt.Chart(d).encode(x=alt.X("txn_date:T", title="Date"))
    area = base.mark_area(opacity=0.25, color=ui.SKY, line={"color": ui.SKY}).encode(
        y=alt.Y("credits:Q", title="All credits per day")
    )
    cash = base.mark_line(point=True, color=ui.NAVY).encode(
        y=alt.Y("cash_credits:Q", title="Cash deposits per day"),
        tooltip=[alt.Tooltip("txn_date:T", title="Date"), "credits:Q", "cash_credits:Q"],
    )
    st.altair_chart(alt.layer(area, cash).resolve_scale(y="independent"), width="stretch")
    st.caption("Area: all credits per day. Line: cash deposits, where structuring hides.")
