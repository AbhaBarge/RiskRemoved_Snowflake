"""Shared visual pieces: one palette, a pipeline strip, and the Visual / Table switch.

Every chart here is a second reading of a table the page also offers, so a
judge can browse visually and an analyst can still filter the rows.
"""

from __future__ import annotations

import html
from collections.abc import Sequence

import altair as alt
import pandas as pd
import streamlit as st

NAVY = "#11567F"
SKY = "#29B5E8"
TEAL = "#1A9E8F"
AMBER = "#E8A33D"
RED = "#D64545"
SLATE = "#8A9BA8"
#: Case and artifact states, in workflow order, each with one colour everywhere.
STATUS_COLOURS = {
    "OPEN": SLATE,
    "INVESTIGATING": SKY,
    "DRAFTED": AMBER,
    "REVIEWED": TEAL,
    "CLOSED": NAVY,
    "DRAFT": AMBER,
    "REJECTED": RED,
    "RETURNED": RED,
    "FILED": NAVY,
}
BRANCH_SCHEME = [NAVY, SKY, TEAL, AMBER, "#7E6BC4", SLATE]

_CSS = """
<style>
.rc-strip {display:flex; align-items:stretch; gap:.35rem; flex-wrap:wrap; margin:.25rem 0 1rem}
.rc-step {flex:1 1 7.5rem; min-width:0; border:1px solid #D6E4EC; border-radius:.6rem;
          padding:.6rem .75rem; background:linear-gradient(180deg,#FFFFFF 0%,#F2F7FA 100%)}
.rc-step .v {font-size:1.55rem; font-weight:700; color:#11567F; line-height:1.15;
             overflow-wrap:anywhere}
.rc-step .l {font-size:.78rem; color:#4F6B7D; text-transform:uppercase; letter-spacing:.04em}
.rc-step .n {font-size:.75rem; color:#6B8292; margin-top:.15rem}
.rc-step.on {border-color:#29B5E8; box-shadow:0 0 0 2px rgba(41,181,232,.18)}
.rc-step.done .v {color:#1A9E8F}
.rc-arrow {align-self:center; color:#29B5E8; font-size:1.1rem}
.rc-hero {border-left:4px solid #29B5E8; background:#F2F7FA; border-radius:.5rem;
          padding:.75rem 1rem; margin:.25rem 0 .75rem}
.rc-hero b {color:#11567F}
.rc-chip {display:inline-block; border-radius:1rem; padding:.05rem .55rem; font-size:.75rem;
          font-weight:600; color:#fff; margin-right:.25rem}
.rc-facts {display:grid; grid-template-columns:fit-content(45%) minmax(55%,1fr);
           border:1px solid #D6E4EC; border-radius:.6rem; overflow:hidden; margin-bottom:.75rem}
.rc-facts .k, .rc-facts .v {padding:.4rem .7rem; border-bottom:1px solid #EAF1F5; font-size:.88rem}
.rc-facts .k {background:#F2F7FA; color:#4F6B7D; font-weight:600}
.rc-facts .v {color:#11567F; overflow-wrap:anywhere; min-width:0}
.rc-facts .w {color:#D64545; font-weight:600; font-size:.78rem}
</style>
"""


def style() -> None:
    """Inject the shared CSS once per page run."""
    st.markdown(_CSS, unsafe_allow_html=True)


def strip(
    steps: Sequence[tuple[str, str, str]],
    *,
    active: int | None = None,
    done: int | None = None,
    arrows: bool = True,
) -> None:
    """A row of tiles: (label, value, note). ``done`` tiles up to that index are green.

    With ``arrows`` the tiles read as a flow; without, as independent figures.
    """
    parts = []
    for i, (label, value, note) in enumerate(steps):
        cls = (
            "rc-step"
            + (" on" if i == active else "")
            + (" done" if done is not None and i <= done else "")
        )
        parts.append(
            f'<div class="{cls}"><div class="l">{html.escape(label)}</div>'
            f'<div class="v">{html.escape(value)}</div>'
            f'<div class="n">{html.escape(note)}</div></div>'
        )
    joint = '<div class="rc-arrow">&#10140;</div>' if arrows else ""
    st.markdown(
        '<div class="rc-strip">' + joint.join(parts) + "</div>",
        unsafe_allow_html=True,
    )


def callout(text_html: str) -> None:
    """A highlighted note. Callers escape any data they put in it."""
    st.markdown(f'<div class="rc-hero">{text_html}</div>', unsafe_allow_html=True)


def chip(text: str, colour: str) -> str:
    return f'<span class="rc-chip" style="background:{colour}">{html.escape(text)}</span>'


def facts(rows: dict[str, str], *, flags: dict[str, str] | None = None) -> None:
    """A two-column fact sheet. ``flags`` maps a field to a warning shown beside it."""
    flags = flags or {}
    cells = "".join(
        f'<div class="k">{html.escape(k)}</div><div class="v">{html.escape(str(v))}'
        + (f' <span class="w">{html.escape(flags[k])}</span>' if k in flags else "")
        + "</div>"
        for k, v in rows.items()
    )
    st.markdown(f'<div class="rc-facts">{cells}</div>', unsafe_allow_html=True)


def visual(key: str, *, label: str = "View") -> bool:
    """The Visual / Table switch. True when the visual is selected (the default)."""
    choice = st.segmented_control(
        label,
        ["Visual", "Table"],
        default="Visual",
        key=key,
        label_visibility="collapsed",
    )
    return choice != "Table"


def status_scale(domain: Sequence[str]) -> alt.Scale:
    present = [s for s in STATUS_COLOURS if s in set(domain)]
    return alt.Scale(domain=present, range=[STATUS_COLOURS[s] for s in present])


_ACRONYMS = {
    "id": "ID",
    "inr": "(₹)",
    "kyc": "KYC",
    "pep": "PEP",
    "str": "STR",
    "sql": "SQL",
    "ms": "(ms)",
    "txn": "Txn",
}


def label(column: str) -> str:
    """snake_case -> a heading: 'flagged_inr' -> 'Flagged (₹)', 'case_id' -> 'Case ID'."""
    words = [_ACRONYMS.get(w.lower(), w.lower()) for w in str(column).split("_") if w]
    if not words:
        return str(column)
    first = words[0]
    words[0] = first if first.isupper() or first.startswith("(") else first.capitalize()
    return " ".join(words)


def pretty(df: pd.DataFrame) -> pd.DataFrame:
    """The same rows with human column headings, for the Table views."""
    return df.rename(columns={c: label(c) for c in df.columns})


def auto_chart(df: pd.DataFrame) -> bool:
    """Draw the obvious chart for a query result. False when there is none to draw.

    One number is a figure; a date column with a measure is a line; one category
    with a measure is a ranked bar. Anything else is left to the table.
    """
    if df is None or df.empty:
        return False
    numeric = df.select_dtypes("number").columns.tolist()
    if df.shape == (1, 1) and numeric:
        st.metric(
            label(df.columns[0]),
            f"{df.iloc[0, 0]:,.0f}"
            if float(df.iloc[0, 0]).is_integer()
            else f"{df.iloc[0, 0]:,.2f}",
        )
        return True
    if len(numeric) != 1 or len(df) < 2 or len(df) > 200:
        return False
    measure = numeric[0]
    others = [c for c in df.columns if c != measure]
    if len(others) != 1:
        return False
    dim = others[0]
    is_time = pd.api.types.is_datetime64_any_dtype(df[dim]) or (
        df[dim].dtype == object
        and pd.to_datetime(df[dim], errors="coerce", format="ISO8601").notna().all()
    )
    data = df.copy()
    if is_time:
        data[dim] = pd.to_datetime(data[dim], errors="coerce", format="ISO8601")
        chart = (
            alt.Chart(data)
            .mark_area(opacity=0.2, color=SKY, line={"color": NAVY}, point={"color": NAVY})
            .encode(
                x=alt.X(f"{dim}:T", title=label(dim)),
                y=alt.Y(f"{measure}:Q", title=label(measure)),
                tooltip=[alt.Tooltip(f"{dim}:T"), alt.Tooltip(f"{measure}:Q", format=",")],
            )
            .properties(height=260)
        )
    else:
        data[dim] = data[dim].astype(str)
        chart = (
            alt.Chart(data)
            .mark_bar(cornerRadiusEnd=4, color=NAVY)
            .encode(
                y=alt.Y(f"{dim}:N", sort="-x", title=None),
                x=alt.X(f"{measure}:Q", title=label(measure)),
                tooltip=[f"{dim}:N", alt.Tooltip(f"{measure}:Q", format=",")],
            )
            .properties(height={"step": 28})
        )
        chart = chart + chart.mark_text(align="left", dx=4, color=NAVY, fontSize=11).encode(
            text=alt.Text(f"{measure}:Q", format=",")
        )
    st.altair_chart(chart, width="stretch")
    return True
