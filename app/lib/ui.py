"""Shared visual pieces: one palette, a pipeline strip, and the Visual / Table switch.

Every chart here is a second reading of a table the page also offers, so a
judge can browse visually and an analyst can still filter the rows.
"""

from __future__ import annotations

import html
from collections.abc import Sequence

import altair as alt
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
.rc-step {flex:1 1 7.5rem; border:1px solid #D6E4EC; border-radius:.6rem; padding:.6rem .75rem;
          background:linear-gradient(180deg,#FFFFFF 0%,#F2F7FA 100%)}
.rc-step .v {font-size:1.55rem; font-weight:700; color:#11567F; line-height:1.15}
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
</style>
"""


def style() -> None:
    """Inject the shared CSS once per page run."""
    st.markdown(_CSS, unsafe_allow_html=True)


def strip(
    steps: Sequence[tuple[str, str, str]], *, active: int | None = None, done: int | None = None
) -> None:
    """A row of connected tiles: (label, value, note). ``done`` tiles up to that index are green."""
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
    st.markdown(
        '<div class="rc-strip">' + '<div class="rc-arrow">&#10140;</div>'.join(parts) + "</div>",
        unsafe_allow_html=True,
    )


def callout(text_html: str) -> None:
    """A highlighted note. Callers escape any data they put in it."""
    st.markdown(f'<div class="rc-hero">{text_html}</div>', unsafe_allow_html=True)


def chip(text: str, colour: str) -> str:
    return f'<span class="rc-chip" style="background:{colour}">{html.escape(text)}</span>'


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
