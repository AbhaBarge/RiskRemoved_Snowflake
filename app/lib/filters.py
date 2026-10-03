"""Global filters: one sidebar panel that every page reads.

The values live in session state under fixed keys. Streamlit forgets a widget's
value when a page does not draw it, so the entry point re-saves them at the top
of every run (``keep``) and a page without a filter leaves it intact for the
pages that use it. An empty selection means "all".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd
import streamlit as st

from lib import ui

REGION, CHANNEL, WINDOW, STATUS = "f_region", "f_channel", "f_window", "f_status"
KEYS = (REGION, CHANNEL, WINDOW, STATUS)
_SPAN = "_f_span"
#: Case states in workflow order (CASE_RECORD.status).
CASE_STATES = ["OPEN", "INVESTIGATING", "DRAFTED", "REVIEWED", "CLOSED"]

#: Which filters each page uses, by the page's url path.
SCOPES: dict[str, tuple[str, ...]] = {
    "overview": (REGION, CHANNEL, WINDOW, STATUS),
    "case": (REGION, STATUS),
    "signoff": (REGION,),
}


@dataclass(frozen=True)
class Filters:
    regions: tuple[str, ...]
    channels: tuple[str, ...]
    window: tuple[date, date] | None
    statuses: tuple[str, ...]
    span: tuple[date, date] | None

    @property
    def narrowed(self) -> bool:
        return bool(self.regions or self.channels or self.statuses) or (
            self.window is not None and self.window != self.span
        )

    def pills(self, scope: tuple[str, ...]) -> list[str]:
        """Header pills naming what is applied on this page."""
        out = []
        if REGION in scope:
            out.append(ui.pill(_join(self.regions, "All branches"), "navy", glyph="location_on"))
        if CHANNEL in scope:
            out.append(ui.pill(_join(self.channels, "All channels"), "violet", glyph="hub"))
        if WINDOW in scope and self.window:
            lo, hi = self.window
            out.append(ui.pill(f"{lo:%d %b} \u2013 {hi:%d %b %Y}", "cyan", glyph="date_range"))
        if STATUS in scope and self.statuses:
            out.append(ui.pill(_join(self.statuses, ""), "amber", glyph="filter_alt"))
        return out


def _join(values: tuple[str, ...], everything: str) -> str:
    if not values:
        return everything
    shown = ", ".join(v.title() for v in values[:3])
    return shown + (f" +{len(values) - 3}" if len(values) > 3 else "")


def keep() -> None:
    """Re-save every filter value, so pages that do not draw a filter keep it."""
    for k in (*KEYS, _SPAN):
        if k in st.session_state:
            st.session_state[k] = st.session_state[k]


def _options() -> dict[str, Any]:
    from lib import insights  # imported late: the sign-in page never needs it

    try:
        return insights.filter_options()
    except Exception:  # the panel must not take the page down with it
        return {"regions": [], "channels": [], "first_day": None, "last_day": None}


def _sanitise(key: str, allowed: list[str]) -> None:
    if key in st.session_state:
        st.session_state[key] = [v for v in st.session_state[key] or [] if v in allowed]


def _window_state(first: date | None, last: date | None) -> None:
    """Clamp the stored window to the data; follow new days while it spans everything."""
    if first is None or last is None:
        st.session_state.pop(WINDOW, None)
        return
    span = (first, last)
    previous = st.session_state.get(_SPAN)
    current = st.session_state.get(WINDOW)
    if current is None or current == previous:
        st.session_state[WINDOW] = span
    else:
        lo = min(max(current[0], first), last)
        hi = max(min(current[1], last), lo)
        st.session_state[WINDOW] = (lo, hi)
    st.session_state[_SPAN] = span


def panel(scope: tuple[str, ...]) -> None:
    """Draw the filters this page uses, in the sidebar."""
    opts = _options()
    _sanitise(REGION, opts["regions"])
    _sanitise(CHANNEL, opts["channels"])
    _sanitise(STATUS, CASE_STATES)
    _window_state(opts["first_day"], opts["last_day"])
    ui.side_label("Filters")
    if not scope:
        st.caption("This view has no filters: it reads the whole governed record.")
        return
    if REGION in scope and opts["regions"]:
        st.pills(
            "Branch",
            opts["regions"],
            selection_mode="multi",
            key=REGION,
            format_func=str.title,
            help="Cases, signals and ledger flows for these branches. None selected means all.",
        )
    if CHANNEL in scope and opts["channels"]:
        st.pills(
            "Channel",
            opts["channels"],
            selection_mode="multi",
            key=CHANNEL,
            help="Ledger analytics for these payment channels. None selected means all.",
        )
    if (
        WINDOW in scope
        and opts["first_day"]
        and opts["last_day"]
        and opts["first_day"] < opts["last_day"]
    ):
        st.slider(
            "Ledger window",
            min_value=opts["first_day"],
            max_value=opts["last_day"],
            key=WINDOW,
            format="D MMM",
            help="Booking dates the ledger analytics cover.",
        )
    if STATUS in scope:
        st.pills(
            "Case status",
            CASE_STATES,
            selection_mode="multi",
            key=STATUS,
            format_func=str.title,
            help="Cases in these states. None selected means all.",
        )
    st.button(
        "Clear filters",
        icon=":material/filter_alt_off:",
        type="tertiary",
        on_click=clear,
        disabled=not current().narrowed,
    )


def clear() -> None:
    for k in (REGION, CHANNEL, STATUS):
        st.session_state[k] = []
    if _SPAN in st.session_state:
        st.session_state[WINDOW] = st.session_state[_SPAN]


def current() -> Filters:
    span = st.session_state.get(_SPAN)
    window = st.session_state.get(WINDOW)
    return Filters(
        regions=tuple(st.session_state.get(REGION) or ()),
        channels=tuple(st.session_state.get(CHANNEL) or ()),
        window=(window[0], window[1]) if window else None,
        statuses=tuple(st.session_state.get(STATUS) or ()),
        span=(span[0], span[1]) if span else None,
    )


# --------------------------------------------------------------------------- #
# Applying them
# --------------------------------------------------------------------------- #


def ledger(df: pd.DataFrame, f: Filters) -> pd.DataFrame:
    """The ledger rows inside the branch, channel and date filters."""
    if df.empty:
        return df
    keep_rows = pd.Series(True, index=df.index)
    if f.regions:
        keep_rows &= df["branch_region"].isin(f.regions)
    if f.channels:
        keep_rows &= df["channel"].isin(f.channels)
    if f.window:
        keep_rows &= df["txn_date"].between(f.window[0], f.window[1])
    return df[keep_rows]


def cases(df: pd.DataFrame, f: Filters) -> pd.DataFrame:
    """Case rows inside the branch and status filters."""
    if df.empty:
        return df
    keep_rows = pd.Series(True, index=df.index)
    if f.regions:
        keep_rows &= df["branch_region"].isin(f.regions)
    if f.statuses:
        keep_rows &= df["status"].isin(f.statuses)
    return df[keep_rows]
