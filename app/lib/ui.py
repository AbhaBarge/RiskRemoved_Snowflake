"""The design system: theme tokens, one stylesheet, and the components every page draws.

Light and dark are both Streamlit themes (.streamlit/config.toml), so tables,
widgets and charts are themed natively. The stylesheet below only adds depth
and polish; its colours come from CSS ``light-dark()``, which follows the
``color-scheme`` Streamlit sets on the app root, so custom cards always match
the native theme. The appearance toggle switches that theme in the browser
without a reload (``theme_hook``), so the signed-in session survives.

Every chart here is a second reading of a table the page also offers, so a
judge can browse visually and an analyst can still filter the rows.
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import math
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

#: Session key of the sidebar's appearance toggle (True = dark).
MODE_KEY = "rc_dark"

#: Mark colours per mode. Light uses executive navy; dark lifts every hue so it
#: reads on #0D1117. Axis, grid and label colours come from the Streamlit theme.
PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "navy": "#1E3A8A",
        "blue": "#2563EB",
        "sky": "#0284C7",
        "cyan": "#0891B2",
        "teal": "#0D9488",
        "emerald": "#059669",
        "amber": "#D97706",
        "rose": "#E11D48",
        "violet": "#7C3AED",
        "slate": "#64748B",
        "muted": "#94A3B8",
        "ink": "#0F172A",
        "band": "#F59E0B",
    },
    "dark": {
        "navy": "#60A5FA",
        "blue": "#3B82F6",
        "sky": "#38BDF8",
        "cyan": "#22D3EE",
        "teal": "#2DD4BF",
        "emerald": "#10B981",
        "amber": "#FBBF24",
        "rose": "#FB7185",
        "violet": "#A78BFA",
        "slate": "#94A3B8",
        "muted": "#64748B",
        "ink": "#E6EDF3",
        "band": "#FBBF24",
    },
}
_STATUS_TOKENS = {
    "OPEN": "slate",
    "INVESTIGATING": "sky",
    "DRAFTED": "amber",
    "REVIEWED": "emerald",
    "CLOSED": "navy",
    "DRAFT": "amber",
    "REJECTED": "rose",
    "RETURNED": "rose",
    "FILED": "navy",
    "SUPERSEDED": "muted",
}
_SERIES_TOKENS = ("navy", "emerald", "amber", "sky", "violet", "slate", "rose", "teal")
#: Old constant names, resolved per mode by the module ``__getattr__`` below.
_LEGACY = {"NAVY": "navy", "SKY": "sky", "TEAL": "teal", "AMBER": "amber", "RED": "rose"}
_LEGACY |= {"SLATE": "slate", "EMERALD": "emerald", "VIOLET": "violet", "CYAN": "cyan"}


def mode() -> str:
    """'light' or 'dark': the toggle once set, else the browser's reported theme."""
    try:
        chosen = st.session_state.get(MODE_KEY)
        if chosen is not None:
            return "dark" if chosen else "light"
        reported = st.context.theme.type
        return reported if reported in PALETTES else "light"
    except Exception:  # outside a script run (tests, imports)
        return "light"


def palette() -> dict[str, str]:
    return PALETTES[mode()]


def status_colours() -> dict[str, str]:
    """Case and artifact states, in workflow order, each with one colour everywhere."""
    p = palette()
    return {s: p[t] for s, t in _STATUS_TOKENS.items()}


def series() -> list[str]:
    """Categorical range for charts that name their own colours."""
    p = palette()
    return [p[t] for t in _SERIES_TOKENS]


def __getattr__(name: str) -> Any:
    # NAVY, STATUS_COLOURS, BRANCH_SCHEME ... predate the dark theme. Resolving
    # them on access keeps every page that reads ``ui.NAVY`` correct in both modes.
    if name in _LEGACY:
        return palette()[_LEGACY[name]]
    if name == "STATUS_COLOURS":
        return status_colours()
    if name == "BRANCH_SCHEME":
        return series()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --------------------------------------------------------------------------- #
# Stylesheet
# --------------------------------------------------------------------------- #

#: The stylesheet lives beside this module and ships with the app.
_CSS_FILE = Path(__file__).with_name("theme.css")

_STYLED = "_rc_styled_run"
_RUN = "_rc_run"


@st.cache_data(show_spinner=False)
def _stylesheet(stamp: float) -> str:
    """The stylesheet as a <style> block, re-read only when the file changes."""
    return "<style>" + _CSS_FILE.read_text("utf-8") + "</style>"


def begin_run() -> None:
    """Mark a new script run, so ``style`` injects the stylesheet once per run."""
    st.session_state[_RUN] = st.session_state.get(_RUN, 0) + 1


def style() -> None:
    """Inject the shared stylesheet (once per run; pages may call it again)."""
    run = st.session_state.get(_RUN)
    if run is not None and st.session_state.get(_STYLED) == run:
        return
    st.session_state[_STYLED] = run
    st.html(_stylesheet(_CSS_FILE.stat().st_mtime))


# The browser asks matchMedia('(prefers-color-scheme: dark)') to pick between the
# configured light and dark themes, and asks again on 'afterprint'. Answering
# with the viewer's choice and replaying that event switches Streamlit's own
# theme in place: no reload, so the signed-in session is kept. The choice is
# remembered in localStorage for the next visit. WANT is only ever '', 'light'
# or 'dark', set by Python below.
_THEME_JS = """
(function () {
  var want = "__WANT__", key = "rc-theme", w = window;
  try {
    if (want) { w.localStorage.setItem(key, want); }
    else { want = w.localStorage.getItem(key) || ""; }
  } catch (e) {}
  if (want !== "dark" && want !== "light") { return; }
  w.__rcScheme = want;
  if (!w.__rcNativeMatchMedia) {
    var native = w.matchMedia.bind(w);
    w.__rcNativeMatchMedia = native;
    w.matchMedia = function (query) {
      var list = native(query);
      if (!w.__rcScheme || String(query).replace(/\\s/g, "") !== "(prefers-color-scheme:dark)") {
        return list;
      }
      return new Proxy(list, {
        get: function (target, prop) {
          if (prop === "matches") { return w.__rcScheme === "dark"; }
          var value = target[prop];
          return typeof value === "function" ? value.bind(target) : value;
        }
      });
    };
  }
  var root = document.querySelector('[data-testid="stApp"]');
  var current = root ? w.getComputedStyle(root).colorScheme || "" : "";
  if (current.indexOf(want) === -1) { w.dispatchEvent(new Event("afterprint")); }
})();
"""


def theme_hook(dark: bool | None) -> None:
    """Apply the viewer's appearance choice; ``None`` restores the remembered one."""
    want = "" if dark is None else ("dark" if dark else "light")
    body = f'<div class="rc-hook"></div><script>{_THEME_JS.replace("__WANT__", want)}</script>'
    # TypeError: an older Streamlit without script support keeps the native theme only.
    with contextlib.suppress(TypeError):
        st.html(body, unsafe_allow_javascript=True)


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #

TONES = ("navy", "blue", "emerald", "amber", "rose", "violet", "cyan", "slate")


def _tone(tone: str) -> str:
    return f"rc-t-{tone if tone in TONES else 'navy'}"


def icon(name: str, *, size: int | None = None) -> str:
    """A Material Symbols glyph by name, e.g. icon('verified')."""
    safe = "".join(ch for ch in name if ch.isalnum() or ch == "_")
    style_attr = f' style="font-size:{int(size)}px"' if size else ""
    return f'<span class="rc-ms"{style_attr}>{safe}</span>'


def pill(text: str, tone: str = "navy", *, glyph: str = "", live: bool = False) -> str:
    """A rounded status pill. ``live`` adds a pulsing dot."""
    lead = '<span class="rc-dot live"></span>' if live else (icon(glyph) if glyph else "")
    return f'<span class="rc-pill {_tone(tone)}">{lead}{html.escape(text)}</span>'


def _md(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def page_header(
    title: str,
    subtitle: str = "",
    *,
    glyph: str = "monitoring",
    eyebrow: str = "Risk & Compliance Copilot",
    meta: Sequence[str] = (),
) -> None:
    """The branded banner at the top of every page. ``meta`` holds ready-made pills."""
    _md(
        '<div class="rc-head">'
        f'<div class="rc-head-icon">{icon(glyph)}</div>'
        '<div class="rc-head-text">'
        f'<div class="rc-eyebrow">{html.escape(eyebrow)}</div>'
        f'<div class="rc-title">{html.escape(title)}</div>'
        + (f'<div class="rc-subtitle">{html.escape(subtitle)}</div>' if subtitle else "")
        + "</div>"
        + (f'<div class="rc-head-meta">{"".join(meta)}</div>' if meta else "")
        + "</div>"
    )


def card_head(
    title: str, subtitle: str = "", *, glyph: str = "", tone: str = "navy", aside: str = ""
) -> None:
    _md(
        f'<div class="rc-card-head {_tone(tone)}">'
        + (f'<span class="rc-card-icon">{icon(glyph)}</span>' if glyph else "")
        + f'<div><div class="rc-card-title">{html.escape(title)}</div>'
        + (f'<div class="rc-card-sub">{html.escape(subtitle)}</div>' if subtitle else "")
        + "</div>"
        + (f'<div class="rc-card-aside">{aside}</div>' if aside else "")
        + "</div>"
    )


@contextmanager
def card(
    key: str,
    title: str = "",
    subtitle: str = "",
    *,
    glyph: str = "",
    tone: str = "navy",
    aside: str = "",
) -> Iterator[Any]:
    """A bordered, shadowed card: ``with ui.card("cases", "Case queue"): ...``."""
    box = st.container(border=True, key=f"rc-card-{key}")
    with box:
        if title:
            card_head(title, subtitle, glyph=glyph, tone=tone, aside=aside)
        yield box


def section(title: str, subtitle: str = "") -> None:
    _md(
        f'<div class="rc-section"><span class="rc-section-title">{html.escape(title)}</span>'
        + (f'<span class="rc-section-sub">{html.escape(subtitle)}</span>' if subtitle else "")
        + "</div>"
    )


def empty(text: str, *, glyph: str = "inbox") -> None:
    _md(f'<div class="rc-empty">{icon(glyph)}<span>{html.escape(text)}</span></div>')


def side_label(text: str) -> None:
    _md(f'<div class="rc-side-label">{html.escape(text)}</div>')


def user_card(name: str, title: str, badge: str, tone: str) -> None:
    initials = "".join(part[0] for part in name.split()[:2]).upper()
    _md(
        '<div class="rc-user">'
        f'<div class="rc-avatar">{html.escape(initials)}</div>'
        f'<div><div class="rc-user-name">{html.escape(name)}</div>'
        f'<div class="rc-user-role">{html.escape(title)}</div></div>'
        f"{pill(badge, tone)}</div>"
    )


def sparkline(
    values: Sequence[Any],
    *,
    width: int = 120,
    height: int = 34,
    css: str = "rc-spark",
    salt: str = "",
) -> str:
    """An inline SVG trend line with a soft area fill; '' when fewer than two points.

    ``salt`` keeps the gradient id unique when two series share the same points.
    """
    points: list[float] = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isnan(f):
            points.append(f)
    if len(points) < 2:
        return ""
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    step = width / (len(points) - 1)
    xy = [(i * step, height - 3 - (v - lo) / span * (height - 7)) for i, v in enumerate(points)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    area = f"M0,{height} L" + " L".join(f"{x:.1f},{y:.1f}" for x, y in xy) + f" L{width},{height} Z"
    gid = "rcg" + hashlib.sha1(f"{salt}|{line}".encode()).hexdigest()[:10]  # noqa: S324 - an element id
    return (
        f'<svg class="{css}" viewBox="0 0 {width} {height}" '
        'preserveAspectRatio="none" aria-hidden="true">'
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="currentColor" stop-opacity="0.30"/>'
        '<stop offset="1" stop-color="currentColor" stop-opacity="0"/></linearGradient></defs>'
        f'<path d="{area}" fill="url(#{gid})"/>'
        f'<polyline points="{line}" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/></svg>'
    )


@dataclass(frozen=True)
class Kpi:
    """One executive tile.

    ``trend`` (+1 / -1 / 0) draws an arrow on the delta chip; ``good`` says which
    direction is good and colours it. ``state`` ('good' / 'warn' / 'bad' /
    'flat') colours a chip that is a status rather than a trend.
    """

    label: str
    value: str
    unit: str = ""
    note: str = ""
    delta: str = ""
    trend: int = 0
    good: str | None = "up"
    state: str | None = None
    spark: Sequence[Any] = ()
    glyph: str = ""
    tone: str = "navy"


_STATE_GLYPH = {"good": "check_circle", "warn": "error", "bad": "error"}


def _delta(k: Kpi) -> str:
    if not k.delta:
        return ""
    if k.trend:
        glyph = "trending_up" if k.trend > 0 else "trending_down"
        cls = k.state or (
            "flat" if k.good is None else ("good" if (k.trend > 0) == (k.good == "up") else "bad")
        )
    else:
        cls = k.state or "flat"
        glyph = _STATE_GLYPH.get(cls, "")
    return (
        f'<span class="rc-delta {cls}">{icon(glyph) if glyph else ""}{html.escape(k.delta)}</span>'
    )


def kpis(items: Sequence[Kpi], *, columns: int | None = None) -> None:
    """A row of KPI tiles with trend chips and sparklines.

    Up to six tiles share one row; more wrap four to a row, so a grid never
    leaves a ragged gap. Narrow screens fall back to two columns, then one.
    """
    cols = columns or (len(items) if len(items) <= 6 else 4)
    tiles = []
    for k in items:
        tiles.append(
            f'<div class="rc-kpi {_tone(k.tone)}">'
            '<div class="rc-kpi-top">'
            f'<span class="rc-kpi-label">{html.escape(k.label)}</span>'
            + (f'<span class="rc-kpi-icon">{icon(k.glyph)}</span>' if k.glyph else "")
            + "</div>"
            f'<div class="rc-kpi-value">{html.escape(k.value)}'
            + (f'<span class="rc-kpi-unit">{html.escape(k.unit)}</span>' if k.unit else "")
            + "</div>"
            f'<div class="rc-kpi-foot">{_delta(k)}'
            + (f'<span class="rc-kpi-note">{html.escape(k.note)}</span>' if k.note else "")
            + "</div>"
            + sparkline(k.spark, salt=k.label)
            + "</div>"
        )
    _md(f'<div class="rc-kpis" style="--rc-cols:{int(cols)}">{"".join(tiles)}</div>')


def change(current: float, previous: float) -> tuple[str, int]:
    """('+12.4%', +1) for a period-over-period change; ('', 0) when not comparable."""
    if (
        previous is None
        or current is None
        or previous == 0
        or any(map(pd.isna, (current, previous)))
    ):
        return "", 0
    pct = (float(current) - float(previous)) / abs(float(previous)) * 100
    trend = 0 if abs(pct) < 0.05 else (1 if pct > 0 else -1)
    return f"{pct:+.1f}%", trend


def rupees(value: Any) -> tuple[str, str]:
    """A compact Indian-unit amount for tiles: (number, unit), e.g. ('₹2,593', 'cr')."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-", ""
    if math.isnan(v):
        return "-", ""
    if abs(v) >= 1e9:
        return f"₹{v / 1e7:,.0f}", "cr"
    if abs(v) >= 1e7:
        return f"₹{v / 1e7:,.1f}", "cr"
    if abs(v) >= 1e5:
        return f"₹{v / 1e5:,.1f}", "lakh"
    return f"₹{v:,.0f}", ""


def ago(seconds: Any) -> str:
    """'42 s', '27 min', '21 h', '3 d' for an age in seconds; '-' when unknown."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "-"
    if math.isnan(s):
        return "-"
    s = max(0.0, s)
    if s < 90:
        return f"{s:.0f} s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    if s < 172800:
        return f"{s / 3600:.0f} h"
    return f"{s / 86400:.0f} d"


@dataclass(frozen=True)
class Readout:
    """One instrument on the telemetry console."""

    label: str
    value: str
    sub: str = ""
    level: float | None = None
    status: str = "ok"
    spark: Sequence[Any] = ()


def _ring(fraction: float) -> str:
    f = max(0.0, min(1.0, float(fraction)))
    c = 2 * math.pi * 15.5
    return (
        '<svg viewBox="0 0 36 36" aria-hidden="true">'
        '<circle cx="18" cy="18" r="15.5" fill="none" '
        'stroke="rgba(148,163,184,.18)" stroke-width="3"/>'
        f'<circle cx="18" cy="18" r="15.5" fill="none" stroke="url(#rc-ring)" stroke-width="3" '
        f'stroke-linecap="round" stroke-dasharray="{c:.2f} {c:.2f}" '
        f'stroke-dashoffset="{c * (1 - f):.2f}" transform="rotate(-90 18 18)"/>'
        '<defs><linearGradient id="rc-ring" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#22D3EE"/><stop offset="1" stop-color="#10B981"/>'
        "</linearGradient></defs></svg>"
    )


def console(
    title: str,
    readouts: Sequence[Readout],
    *,
    gauges: Sequence[tuple[str, float, str, str]] = (),
    ticker: Sequence[str] = (),
    tag: str = "",
    stamp: str = "",
    live: bool = False,
) -> None:
    """The sci-fi telemetry panel: instruments, ring gauges and an event ticker.

    ``gauges`` are (label, fraction 0..1, value text, sub text). ``ticker`` items
    are HTML fragments the caller has already escaped.
    """
    cells = []
    for r in readouts:
        bar = ""
        if r.level is not None:
            width = max(0.0, min(1.0, float(r.level))) * 100
            bar = f'<div class="rc-ro-bar"><span style="width:{width:.1f}%"></span></div>'
        cells.append(
            '<div class="rc-readout">'
            f'<div class="rc-ro-label"><span class="rc-led {html.escape(r.status)}"></span>'
            f"{html.escape(r.label)}</div>"
            f'<div class="rc-ro-value">{html.escape(r.value)}</div>'
            + (f'<div class="rc-ro-sub">{html.escape(r.sub)}</div>' if r.sub else "")
            + bar
            + sparkline(r.spark, height=22, css="rc-ro-spark", salt=r.label)
            + "</div>"
        )
    rings = "".join(
        f'<div class="rc-gauge">{_ring(frac)}<div>'
        f'<div class="rc-gauge-val">{html.escape(val)}</div>'
        f'<div class="rc-gauge-lbl">{html.escape(lbl)}</div>'
        + (f'<div class="rc-gauge-sub">{html.escape(sub)}</div>' if sub else "")
        + "</div></div>"
        for lbl, frac, val, sub in gauges
    )
    feed = ""
    if ticker:
        items = "".join(f'<span class="rc-tk">{t}</span>' for t in ticker)
        feed = f'<div class="rc-ticker"><div class="rc-ticker-track">{items}{items}</div></div>'
    _md(
        '<div class="rc-console">'
        '<div class="rc-console-head">'
        f'<span class="rc-led{" pulse" if live else " idle"}"></span>'
        f'<span class="rc-console-title">{html.escape(title)}</span>'
        + (f'<span class="rc-console-tag">{html.escape(tag)}</span>' if tag else "")
        + (f'<span class="rc-console-clock">{html.escape(stamp)}</span>' if stamp else "")
        + "</div>"
        f'<div class="rc-console-grid"><div class="rc-readouts">{"".join(cells)}</div>'
        f'<div class="rc-gauges">{rings}</div></div>'
        f"{feed}</div>"
    )


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #


def dress(
    chart: Any, *, axis: dict[str, Any] | None = None, legend: dict[str, Any] | None = None
) -> Any:
    """House style on top of the Streamlit chart theme: quiet grid, no frame, bottom legend."""
    return (
        chart.configure_view(strokeWidth=0)
        .configure_axis(
            gridDash=[2, 4],
            gridOpacity=0.7,
            domain=False,
            tickSize=0,
            labelPadding=8,
            titlePadding=10,
            labelFontSize=11,
            titleFontSize=11,
            titleFontWeight=500,
            **(axis or {}),
        )
        .configure_legend(
            orient="bottom",
            labelFontSize=11,
            titleFontSize=11,
            titleFontWeight=600,
            symbolType="circle",
            **(legend or {}),
        )
    )


def chart(
    c: Any, *, axis: dict[str, Any] | None = None, legend: dict[str, Any] | None = None
) -> None:
    st.altair_chart(dress(c, axis=axis, legend=legend), width="stretch")


# --------------------------------------------------------------------------- #
# Kept from the first UI: pipeline strip, callout, chips, fact sheet, switch
# --------------------------------------------------------------------------- #


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
    for i, (lbl, value, note) in enumerate(steps):
        cls = (
            "rc-step"
            + (" on" if i == active else "")
            + (" done" if done is not None and i <= done else "")
        )
        parts.append(
            f'<div class="{cls}"><div class="l">{html.escape(lbl)}</div>'
            f'<div class="v">{html.escape(value)}</div>'
            f'<div class="n">{html.escape(note)}</div></div>'
        )
    joint = '<div class="rc-arrow">&#10140;</div>' if arrows else ""
    _md('<div class="rc-strip">' + joint.join(parts) + "</div>")


def callout(text_html: str) -> None:
    """A highlighted note. Callers escape any data they put in it."""
    _md(f'<div class="rc-hero">{text_html}</div>')


def chip(text: str, colour: str) -> str:
    return (
        f'<span class="rc-chip" style="background:{html.escape(colour)}">{html.escape(text)}</span>'
    )


def facts(rows: dict[str, str], *, flags: dict[str, str] | None = None) -> None:
    """A two-column fact sheet. ``flags`` maps a field to a warning shown beside it."""
    flags = flags or {}
    cells = "".join(
        f'<div class="k">{html.escape(k)}</div><div class="v">{html.escape(str(v))}'
        + (f' <span class="w">{html.escape(flags[k])}</span>' if k in flags else "")
        + "</div>"
        for k, v in rows.items()
    )
    _md(f'<div class="rc-facts">{cells}</div>')


def visual(key: str, *, label: str = "View") -> bool:
    """The Visual / Table switch. True when the visual is selected (the default)."""
    choice = st.segmented_control(
        label,
        [":material/bar_chart: Visual", ":material/table_rows: Table"],
        default=":material/bar_chart: Visual",
        key=key,
        label_visibility="collapsed",
    )
    return not str(choice or "").endswith("Table")


def status_scale(domain: Sequence[str]) -> alt.Scale:
    colours = status_colours()
    present = [s for s in colours if s in set(domain)]
    return alt.Scale(domain=present, range=[colours[s] for s in present])


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
    p = palette()
    if is_time:
        data[dim] = pd.to_datetime(data[dim], errors="coerce", format="ISO8601")
        chart_ = (
            alt.Chart(data)
            .mark_area(
                opacity=0.22,
                color=p["blue"],
                line={"color": p["navy"], "strokeWidth": 2},
                point={"color": p["navy"]},
                interpolate="monotone",
            )
            .encode(
                x=alt.X(f"{dim}:T", title=label(dim)),
                y=alt.Y(f"{measure}:Q", title=label(measure)),
                tooltip=[alt.Tooltip(f"{dim}:T"), alt.Tooltip(f"{measure}:Q", format=",")],
            )
            .properties(height=260)
        )
    else:
        data[dim] = data[dim].astype(str)
        chart_ = (
            alt.Chart(data)
            .mark_bar(cornerRadiusEnd=5, color=p["navy"])
            .encode(
                y=alt.Y(f"{dim}:N", sort="-x", title=None),
                x=alt.X(f"{measure}:Q", title=label(measure)),
                tooltip=[f"{dim}:N", alt.Tooltip(f"{measure}:Q", format=",")],
            )
            .properties(height={"step": 28})
        )
        chart_ = chart_ + chart_.mark_text(align="left", dx=4, color=p["ink"], fontSize=11).encode(
            text=alt.Text(f"{measure}:Q", format=",")
        )
    st.altair_chart(dress(chart_), width="stretch")
    return True
