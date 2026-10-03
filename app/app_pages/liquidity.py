"""Liquidity: the Statement of Structural Liquidity against the RBI tolerance limits."""

import html

import altair as alt
import pandas as pd
import streamlit as st
from lib import data, ui


def crore(value: object) -> str:
    return f"₹{float(value) / 1e7:,.0f} cr"


s = data.structural_liquidity()
ui.page_header(
    "Liquidity (ALM)",
    "RBI Liquidity Risk Management Framework for NBFCs: the net cumulative negative mismatch "
    "in the 1-7, 8-14 and 15-30 day buckets may not exceed 10%, 10% and 20% of cumulative "
    "outflows. The limits are governed POLICY_CLAUSE rows; every figure here is SQL.",
    glyph="water_drop",
    eyebrow="Asset-liability management",
    meta=[ui.pill(f"As of {s['as_of_date'].iloc[0]}", "cyan", glyph="event")]
    if not s.empty
    else [],
)
if s.empty:
    ui.empty("No ALM book loaded. Deploy the ddl stage to seed CURATED.ALM_CASHFLOW.")
    st.stop()

pal = ui.palette()
s = s.copy()
for col in ("inflow_inr", "outflow_inr", "mismatch_inr", "cumulative_mismatch_inr"):
    s[col] = s[col].astype(float)
s["pct"] = s["cumulative_mismatch_pct"].astype(float)
limited = s[s["tolerance_clause_id"].notna()]
breaches = limited[limited["status"] == "BREACH"]

tiles = [
    ui.Kpi(
        str(r.bucket_label),
        f"{r.pct:+.2f}%",
        note=f"limit -{float(r.tolerance_pct):.0f}% of outflows"
        if pd.notna(r.tolerance_pct)
        else str(r.status),
        delta=str(r.status).replace("_", " ").title(),
        state="bad" if r.status == "BREACH" else ("good" if r.status == "WITHIN" else "warn"),
        glyph="error" if r.status == "BREACH" else "verified",
        tone="rose" if r.status == "BREACH" else "emerald",
    )
    for r in limited.itertuples()
]
total_in, unit_in = ui.rupees(s["inflow_inr"].sum())
total_out, unit_out = ui.rupees(s["outflow_inr"].sum())
tiles += [
    ui.Kpi(
        "Inflows, all buckets",
        total_in,
        unit_in,
        note="contractual, by residual maturity",
        glyph="south_west",
        tone="blue",
    ),
    ui.Kpi(
        "Outflows, all buckets",
        total_out,
        unit_out,
        note="contractual, by residual maturity",
        glyph="north_east",
        tone="violet",
    ),
]
ui.kpis(tiles)
st.caption(
    "Cumulative mismatch as a share of cumulative outflows. ALM book of the NBFC, seeded from "
    "governed configuration into CURATED.ALM_CASHFLOW."
)

lines = data.alm_lines()
for r in breaches.itertuples():
    out = lines[(lines["bucket_id"] == r.bucket_id) & (lines["side"] == "OUTFLOW")]
    driver = out.iloc[0] if not out.empty else None
    ui.callout(
        f"<b>Tolerance breached</b> {ui.pill('BREACH', 'rose', glyph='error')} "
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

order = s["bucket_label"].tolist()
colours = {
    "WITHIN": pal["emerald"],
    "BREACH": pal["rose"],
    "NO_LIMIT": pal["slate"],
    "LIMIT_MISSING": pal["amber"],
}

with ui.card(
    "statement",
    "Cumulative mismatch by bucket",
    "Bars: cumulative mismatch as a share of outflows. Red ticks: the tolerance floor for the "
    "limited buckets.",
    glyph="bar_chart",
    tone="navy",
):
    show_visual = ui.visual("liq_statement")
    if show_visual:
        present = [k for k in colours if k in set(s["status"])]
        bars = (
            alt.Chart(s)
            .mark_bar(cornerRadiusEnd=5)
            .encode(
                x=alt.X("bucket_label:N", sort=order, title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("pct:Q", title="Cumulative mismatch (% of outflows)"),
                color=alt.Color(
                    "status:N",
                    title="Status",
                    scale=alt.Scale(domain=present, range=[colours[k] for k in present]),
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
            .mark_tick(color=pal["rose"], thickness=3, size=46)
            .encode(x=alt.X("bucket_label:N", sort=order), y="floor:Q")
        )
        zero = (
            alt.Chart(pd.DataFrame({"y": [0]}))
            .mark_rule(color=pal["ink"], opacity=0.5)
            .encode(y="y:Q")
        )
        ui.chart((bars + ticks + zero).properties(height=320))
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

left, right = st.columns(2, gap="medium")
with (
    left,
    ui.card(
        "ladder",
        "Maturity ladder",
        "Inflows above the axis, outflows below, per residual-maturity bucket (₹ crore).",
        glyph="swap_vert",
        tone="blue",
    ),
):
    ladder = pd.concat(
        [
            s.assign(side="Inflows", crore=s["inflow_inr"] / 1e7),
            s.assign(side="Outflows", crore=-s["outflow_inr"] / 1e7),
        ]
    )
    mirror = (
        alt.Chart(ladder)
        .mark_bar(cornerRadius=4, size=22)
        .encode(
            x=alt.X("bucket_label:N", sort=order, title=None, axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("crore:Q", title="₹ crore"),
            color=alt.Color(
                "side:N",
                title=None,
                scale=alt.Scale(
                    domain=["Inflows", "Outflows"], range=[pal["emerald"], pal["violet"]]
                ),
            ),
            tooltip=[
                "bucket_label:N",
                "side:N",
                alt.Tooltip("crore:Q", format=",.0f", title="₹ crore"),
            ],
        )
    )
    net = (
        alt.Chart(s.assign(net=s["mismatch_inr"] / 1e7))
        .mark_line(
            color=pal["amber"],
            strokeWidth=2.5,
            point={"filled": True, "size": 55, "color": pal["amber"]},
        )
        .encode(
            x=alt.X("bucket_label:N", sort=order),
            y="net:Q",
            tooltip=["bucket_label:N", alt.Tooltip("net:Q", format=",.0f", title="Net ₹ crore")],
        )
    )
    ui.chart((mirror + net).properties(height=300))
    st.caption("The amber line is each bucket's net mismatch.")

with (
    right,
    ui.card(
        "waterfall",
        "Cumulative gap waterfall",
        "Each bucket's net mismatch building the cumulative position (₹ crore).",
        glyph="waterfall_chart",
        tone="emerald",
    ),
):
    w = s[["bucket_id", "bucket_label", "mismatch_inr"]].sort_values("bucket_id").copy()
    w["net"] = w["mismatch_inr"] / 1e7
    w["end"] = w["net"].cumsum()
    w["start"] = w["end"] - w["net"]
    w["move"] = w["net"].map(lambda v: "Adds liquidity" if v >= 0 else "Drains liquidity")
    steps = (
        alt.Chart(w)
        .mark_bar(cornerRadius=3, size=26)
        .encode(
            x=alt.X("bucket_label:N", sort=order, title=None, axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("start:Q", title="Cumulative gap (₹ crore)"),
            y2="end:Q",
            color=alt.Color(
                "move:N",
                title=None,
                scale=alt.Scale(
                    domain=["Adds liquidity", "Drains liquidity"],
                    range=[pal["emerald"], pal["rose"]],
                ),
            ),
            tooltip=[
                "bucket_label:N",
                alt.Tooltip("net:Q", format=",.0f", title="Net ₹ crore"),
                alt.Tooltip("end:Q", format=",.0f", title="Cumulative ₹ crore"),
            ],
        )
    )
    joins = (
        alt.Chart(w)
        .mark_text(dy=-9, fontSize=10, color=pal["ink"])
        .encode(
            x=alt.X("bucket_label:N", sort=order), y="end:Q", text=alt.Text("end:Q", format=",.0f")
        )
    )
    base = (
        alt.Chart(pd.DataFrame({"y": [0]}))
        .mark_rule(color=pal["ink"], opacity=0.45)
        .encode(y="y:Q")
    )
    ui.chart((steps + joins + base).properties(height=300))
    st.caption("Red steps drain liquidity; green steps add it.")

with ui.card(
    "items",
    "Where the cash comes from and goes",
    "Every ALM line item by bucket: the heat is the amount.",
    glyph="grid_view",
    tone="violet",
):
    heat_lines = lines.assign(crore=lines["amount_inr"].astype(float) / 1e7)
    heat_lines["item"] = heat_lines["side"].str.title() + " · " + heat_lines["line_item"]
    item_order = (
        heat_lines.sort_values(["side", "crore"], ascending=[True, False])["item"]
        .drop_duplicates()
        .tolist()
    )
    heat = (
        alt.Chart(heat_lines)
        .mark_rect(cornerRadius=3)
        .encode(
            x=alt.X("bucket_label:N", sort=order, title=None, axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("item:N", sort=item_order, title=None),
            color=alt.Color("crore:Q", title="₹ crore", scale=alt.Scale(type="sqrt")),
            tooltip=[
                "item:N",
                "bucket_label:N",
                alt.Tooltip("crore:Q", format=",.0f", title="₹ crore"),
            ],
        )
        .properties(height={"step": 30})
    )
    ui.chart(heat, axis={"labelLimit": 300})

with st.expander("ALM book line items", icon=":material/table_rows:"):
    st.dataframe(
        ui.pretty(lines.assign(amount=lines["amount_inr"].map(crore)).drop(columns="amount_inr")),
        hide_index=True,
        width="stretch",
    )
    st.caption("CURATED.ALM_CASHFLOW, seeded from conf/generation.yml:alm_book.")
