"""Liquidity: the Statement of Structural Liquidity against the RBI tolerance limits."""

import html

import altair as alt
import pandas as pd
import streamlit as st
from lib import data, ui

ui.style()
st.title("Liquidity (ALM)")
st.caption(
    "RBI Liquidity Risk Management Framework for NBFCs: the net cumulative negative mismatch "
    "in the 1-7, 8-14 and 15-30 day buckets may not exceed 10%, 10% and 20% of cumulative "
    "outflows. The limits are governed POLICY_CLAUSE rows; every figure here is SQL."
)


def crore(value: object) -> str:
    return f"₹{float(value) / 1e7:,.0f} cr"


s = data.structural_liquidity()
if s.empty:
    st.info("No ALM book loaded. Deploy the ddl stage to seed CURATED.ALM_CASHFLOW.")
    st.stop()

s = s.copy()
for col in ("inflow_inr", "outflow_inr", "mismatch_inr", "cumulative_mismatch_inr"):
    s[col] = s[col].astype(float)
s["pct"] = s["cumulative_mismatch_pct"].astype(float)
limited = s[s["tolerance_clause_id"].notna()]

ui.strip(
    [
        (
            str(r.bucket_label),
            f"{r.pct:+.2f}%",
            f"limit -{float(r.tolerance_pct):.0f}% · {r.status}"
            if pd.notna(r.tolerance_pct)
            else str(r.status),
        )
        for r in limited.itertuples()
    ],
    arrows=False,
)
st.caption(
    f"As of {s['as_of_date'].iloc[0]} · cumulative mismatch as a share of cumulative outflows · "
    "illustrative ALM book of the fictional NBFC, not derived from the transaction ledger"
)

lines = data.alm_lines()
for r in limited[limited["status"] == "BREACH"].itertuples():
    out = lines[(lines["bucket_id"] == r.bucket_id) & (lines["side"] == "OUTFLOW")]
    driver = out.iloc[0] if not out.empty else None
    ui.callout(
        f"<b>Tolerance breached</b> {ui.chip('BREACH', ui.RED)} "
        f"{html.escape(str(r.bucket_label))}: cumulative negative mismatch "
        f"<b>{-r.pct:.2f}%</b> of outflows against a {float(r.tolerance_pct):.0f}% limit "
        f"(<code>{html.escape(str(r.tolerance_clause))}</code>, "
        f"{html.escape(str(r.tolerance_source))}). "
        + (
            f"Largest outflow in the bucket: {html.escape(str(driver['line_item']))}, "
            f"{html.escape(crore(driver['amount_inr']))}. "
            if driver is not None
            else ""
        )
        + f"Required action: <b>{html.escape(str(r.tolerance_action))}</b>."
    )
for r in limited[limited["status"] == "LIMIT_MISSING"].itertuples():
    st.warning(f"{r.bucket_label}: clause {r.tolerance_clause_id} is not in force; deploy policy.")

head, switch = st.columns([4, 1], vertical_alignment="bottom")
head.subheader("Cumulative mismatch by bucket")
with switch:
    show_visual = ui.visual("liq_statement")
if show_visual:
    colours = {"WITHIN": ui.TEAL, "BREACH": ui.RED, "NO_LIMIT": ui.SLATE, "LIMIT_MISSING": ui.AMBER}
    present = [k for k in colours if k in set(s["status"])]
    order = s["bucket_label"].tolist()
    bars = (
        alt.Chart(s)
        .mark_bar(cornerRadiusEnd=4)
        .encode(
            x=alt.X("bucket_label:N", sort=order, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("pct:Q", title="Cumulative mismatch (% of outflows)"),
            color=alt.Color(
                "status:N",
                title="Status",
                scale=alt.Scale(domain=present, range=[colours[k] for k in present]),
                legend=alt.Legend(orient="bottom"),
            ),
            tooltip=[
                alt.Tooltip("bucket_label:N", title="Bucket"),
                alt.Tooltip("pct:Q", title="Cumulative mismatch %", format="+.2f"),
                alt.Tooltip("tolerance_pct:Q", title="Limit %"),
                alt.Tooltip("status:N", title="Status"),
            ],
        )
    )
    floor = limited.assign(floor=-limited["tolerance_pct"].astype(float))
    ticks = (
        alt.Chart(floor)
        .mark_tick(color=ui.RED, thickness=3, size=46)
        .encode(x=alt.X("bucket_label:N", sort=order), y="floor:Q")
    )
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=ui.NAVY).encode(y="y:Q")
    st.altair_chart((bars + ticks + zero).properties(height=320), width="stretch")
    st.caption("Bars: cumulative mismatch. Red ticks: the tolerance floor for the limited buckets.")
else:
    shown = s.assign(
        inflow=s["inflow_inr"].map(crore),
        outflow=s["outflow_inr"].map(crore),
        mismatch=s["mismatch_inr"].map(crore),
        cumulative_mismatch=s["cumulative_mismatch_inr"].map(crore),
        cumulative_mismatch_pct=s["pct"].round(2),
    )[
        [
            "bucket_label",
            "inflow",
            "outflow",
            "mismatch",
            "cumulative_mismatch",
            "cumulative_mismatch_pct",
            "tolerance_pct",
            "tolerance_clause",
            "status",
        ]
    ]
    st.dataframe(ui.pretty(shown), hide_index=True, width="stretch")

with st.expander("ALM book line items"):
    st.dataframe(
        ui.pretty(lines.assign(amount=lines["amount_inr"].map(crore)).drop(columns="amount_inr")),
        hide_index=True,
        width="stretch",
    )
    st.caption("CURATED.ALM_CASHFLOW, seeded from conf/generation.yml:alm_book.")
