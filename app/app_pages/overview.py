"""Overview: the executive command center -- what the system watches, what it caught, how well."""

import html
import itertools
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st
from lib import data, filters, insights, ui
from lib.db import CFG, HERO, PROC, call, inr, lakh, reason

SCOPE = filters.SCOPES["overview"]
F = filters.current()

if st.session_state.pop("ov_live_next", False):
    st.session_state["ov_live"] = True
live_on = bool(st.session_state.get("ov_live"))
tele = insights.telemetry()
through = pd.to_datetime(tele.get("data_through"))
ui.page_header(
    "Executive overview",
    "Suspicious patterns detected in SQL against governed thresholds. Every alert becomes a "
    "case, and every filed STR carries a human signature.",
    glyph="space_dashboard",
    eyebrow="Command center",
    meta=[
        ui.pill("Live stream", "emerald", live=True)
        if live_on
        else ui.pill("Snapshot", "slate", glyph="pause_circle"),
        ui.pill(
            f"Data through {through:%d %b %Y}" if pd.notna(through) else "No data yet",
            "cyan",
            glyph="schedule",
        ),
    ],
)
bar, toggle = st.columns([6, 1], vertical_alignment="center")
bar.markdown(" ".join(F.pills(SCOPE)), unsafe_allow_html=True)
live = toggle.toggle(
    "Live mode",
    key="ov_live",
    help="Re-read every 5 seconds -- use while the simulator below streams legs in.",
)


def _simulator() -> None:
    """Live simulator: SIM_CONTROL releases held-back legs inside Snowflake.

    Every leg is already loaded; a leg booked after the cut is held back until a
    tick releases it, and each call refreshes detection and routes new cases. The
    controls here and the one-a-minute task call the same procedure.
    """
    status = data.sim_status()
    if status is None:
        return
    done = st.session_state.pop("sim_done", None)
    held, arrived = int(status.get("held_back") or 0), int(status.get("arrived") or 0)
    running = status.get("task") == "started"
    with st.expander(
        "Live simulator: stream the ledger in" + (" -- running" if running else ""),
        expanded=bool(done) or running,
        icon=":material/play_circle:",
    ):
        st.caption(
            "Every transaction is already in Snowflake. Arm holds back the legs booked after "
            "the cut and clears the cases; each tick then releases the next six hours of "
            "bookings, refreshes detection and routes new cases, all inside Snowflake. Turn on "
            "Live mode to watch the tiles move. Inject releases the demo customer's deposits "
            "at once. A demo control: it clears and rebuilds the case queue."
        )
        if done:
            routed = done.get("routed") or {}
            st.success(
                f"{done['action'].replace('_', ' ').title()}: {int(done.get('released') or 0):,} "
                f"legs released, {int(routed.get('cases_opened') or 0)} new "
                f"case{'' if routed.get('cases_opened') == 1 else 's'}."
            )
        m1, m2, m3 = st.columns(3)
        m1.metric("Arrived", f"{arrived:,} legs")
        m2.metric("Held back", f"{held:,} legs")
        nxt = pd.to_datetime(status.get("next_booking"))
        m3.metric("Next booking", f"{nxt:%d %b %H:%M}" if pd.notna(nxt) else "all arrived")
        hero_party = HERO["party_id"] if HERO else None
        actions = [
            ("ARM", "Arm", ":material/restart_alt:", False),
            ("TICK", "Tick", ":material/skip_next:", held == 0),
            (
                "STOP" if running else "START",
                "Stop" if running else "Auto: 1 tick/min",
                ":material/pause:" if running else ":material/play_arrow:",
                held == 0 and not running,
            ),
            ("INJECT", "Inject demo deposits", ":material/bolt:", held == 0 or not hero_party),
            ("RELEASE_ALL", "Release all", ":material/fast_forward:", held == 0),
        ]
        cols = st.columns(len(actions))
        for col, (action, label, icon, disabled) in zip(cols, actions, strict=True):
            if col.button(
                label, icon=icon, disabled=disabled, width="stretch", key=f"sim_{action}"
            ):
                try:
                    with st.spinner(f"{label}: releasing, refreshing detection, routing..."):
                        result = call(
                            PROC["sim_control"],
                            action,
                            hero_party if action == "INJECT" else None,
                            "APP",
                        )
                    st.session_state["sim_done"] = result
                    if action in ("ARM", "START", "TICK", "INJECT"):
                        # Applied before the toggle exists on the next run: Streamlit
                        # refuses a widget's key once the widget is drawn.
                        st.session_state["ov_live_next"] = True
                    st.session_state.pop("case_id", None)
                    st.rerun()
                except Exception as exc:  # the procedure's own message, verbatim
                    st.error(f"{label} refused: {reason(exc)}")


_simulator()


# --------------------------------------------------------------------------- #
# Shared shaping
# --------------------------------------------------------------------------- #


def _days(f: filters.Filters) -> list[date]:
    if not f.window:
        return []
    return [d.date() for d in pd.date_range(f.window[0], f.window[1], freq="D")]


def _daily(df: pd.DataFrame, days: list[date], value: str, how: str = "sum") -> pd.Series:
    """One value per day across the window, zero-filled, in date order."""
    if df.empty:
        return pd.Series(0.0, index=days)
    grouped = (
        df.groupby("txn_date")[value].nunique()
        if how == "nunique"
        else df.groupby("txn_date")[value].agg(how)
    )
    return grouped.reindex(days, fill_value=0).astype(float)


def _period(series: pd.Series) -> tuple[str, int, int]:
    """Latest half of the window against the half before it: (text, trend, days)."""
    n = len(series) // 2
    if n == 0:
        return "", 0, 0
    current, previous = series.iloc[-n:].sum(), series.iloc[-2 * n : -n].sum()
    text, trend = ui.change(current, previous)
    return text, trend, n


def _region_of() -> dict[str, str]:
    p = insights.parties()
    return dict(zip(p["party_id"], p["branch_region"], strict=False)) if not p.empty else {}


def _names() -> dict[str, str]:
    p = insights.parties()
    return dict(zip(p["party_id"], p["full_name"], strict=False)) if not p.empty else {}


def _channel_scale(channels: list[str]) -> alt.Scale:
    pal = ui.palette()
    tones = {
        "CASH": pal["rose"],
        "CHEQUE": pal["navy"],
        "UPI": pal["emerald"],
        "IMPS": pal["sky"],
        "NEFT": pal["violet"],
        "RTGS": pal["amber"],
    }
    order = [c for c in tones if c in channels] + sorted(set(channels) - set(tones))
    rest = itertools.cycle(ui.series())
    return alt.Scale(domain=order, range=[tones.get(c) or next(rest) for c in order])


# --------------------------------------------------------------------------- #
# KPI tiles and the telemetry console (live)
# --------------------------------------------------------------------------- #


def _kpi_row() -> None:
    days = _days(F)
    led = filters.ledger(insights.ledger(), F)
    inflows = led[led["leg_side"] == "C"] if not led.empty else led
    cash = inflows[inflows["is_cash"]] if not inflows.empty else inflows
    th = insights.cash_thresholds()
    band = (
        cash[(cash["amount_inr"] >= th["band_lo"]) & (cash["amount_inr"] < th["ctr"])]
        if th["band_lo"] and th["ctr"] and not cash.empty
        else cash.iloc[0:0]
    )

    value_daily = _daily(inflows, days, "amount_inr")
    txn_daily = _daily(led, days, "txn_pair_id", "nunique")
    cash_daily = _daily(cash, days, "amount_inr", "count")
    v_delta, v_trend, n = _period(value_daily)
    t_delta, t_trend, _ = _period(txn_daily)
    c_delta, c_trend, _ = _period(cash_daily)
    span = f"vs prior {n} d" if n else ""

    queue = filters.cases(data.case_queue(), F)
    open_cases = int((queue["status"] != "CLOSED").sum()) if not queue.empty else 0
    pending = insights.sla_book()
    if F.regions and not pending.empty:
        pending = pending[pending["branch_region"].isin(F.regions)]
    due_s = float(pending["hours_left"].min()) * 3600 if not pending.empty else None
    k = data.kpis()
    ev = data.latest_detection_eval()
    breaches = data.filing_integrity()

    value, unit = ui.rupees(inflows["amount_inr"].sum() if not inflows.empty else 0)
    cash_share = (
        (cash["amount_inr"].sum() / inflows["amount_inr"].sum())
        if not inflows.empty and inflows["amount_inr"].sum()
        else 0
    )
    band_share = len(band) / len(cash) if len(cash) else 0
    ui.kpis(
        [
            ui.Kpi(
                "Inflows monitored",
                value,
                unit,
                note=span,
                delta=v_delta,
                trend=v_trend,
                spark=value_daily.tolist(),
                glyph="account_balance",
                tone="navy",
            ),
            ui.Kpi(
                "Transactions",
                f"{led['txn_pair_id'].nunique():,}" if not led.empty else "0",
                note=span,
                delta=t_delta,
                trend=t_trend,
                spark=txn_daily.tolist(),
                glyph="swap_horiz",
                tone="blue",
            ),
            ui.Kpi(
                "Cash deposits",
                f"{len(cash):,}",
                note=f"{cash_share:.1%} of inflow value",
                delta=c_delta,
                trend=c_trend,
                good=None,
                spark=cash_daily.tolist(),
                glyph="payments",
                tone="violet",
            ),
            ui.Kpi(
                "Near-threshold cash",
                f"{len(band):,}",
                note="just under the reporting line",
                delta=f"{band_share:.0%} of cash" if len(cash) else "",
                state="warn" if len(band) else "good",
                glyph="warning",
                tone="amber",
            ),
            ui.Kpi(
                "Open cases",
                f"{open_cases:,}",
                note=f"of {len(queue):,} in view",
                glyph="folder_open",
                tone="rose" if open_cases else "emerald",
            ),
            ui.Kpi(
                "Awaiting sign-off",
                f"{len(pending):,}",
                note="approved STRs, officer to sign",
                delta=f"next due in {ui.ago(due_s)}" if due_s is not None else "queue clear",
                state=("warn" if due_s < 3 * 86400 else "flat") if due_s is not None else "good",
                glyph="pending_actions",
                tone="violet",
            ),
            ui.Kpi(
                "STRs filed",
                f"{int(k['filed']):,}",
                note="hash-bound, officer-signed",
                delta="0 integrity breaches" if breaches == 0 else f"{breaches} integrity breach",
                state="good" if breaches == 0 else "bad",
                glyph="task_alt",
                tone="emerald",
            ),
            ui.Kpi(
                "Detection recall",
                f"{ev['recall']:.0%}" if ev else "-",
                note=f"precision {ev['precision']:.0%}" if ev else "no verify run yet",
                delta=f"{ev['tp']}/{ev['expected']} caught" if ev else "",
                state="good" if ev and ev["tp"] == ev["expected"] else "warn",
                glyph="radar",
                tone="cyan",
            ),
        ]
    )


def _console(live_mode: bool) -> None:
    t = insights.telemetry()
    ing = insights.ingest_series()
    log = insights.audit_all(limit=60)
    ev = data.latest_detection_eval()
    ctl = insights.eval_latest()
    breaches = data.filing_integrity()
    actions = int(t["actions"] or 0)
    ok_rate = (int(t["actions_ok"] or 0) / actions) if actions else 0.0
    live_sig, routed = int(t["live_signals"] or 0), int(t["routed_signals"] or 0)
    through_ts = pd.to_datetime(t["data_through"])
    latency = (
        log.sort_values("logged_at")["latency_ms"].dropna().tail(30).tolist()
        if not log.empty
        else []
    )
    readouts = [
        ui.Readout(
            "Ledger stream",
            f"{int(t['legs'] or 0):,} legs",
            f"{int(t['ingest_batches'] or 0)} batches · last {ui.ago(t['ingest_age_s'])} ago",
            spark=ing["legs"].tolist() if not ing.empty else (),
        ),
        ui.Readout(
            "Data horizon",
            f"{through_ts:%d %b %Y}" if pd.notna(through_ts) else "-",
            f"latest booking {through_ts:%H:%M}" if pd.notna(through_ts) else "no ledger yet",
        ),
        ui.Readout(
            "Detection engine",
            f"{live_sig} / {routed}",
            "live signals / routed · in sync"
            if live_sig <= routed
            else f"{live_sig - routed} awaiting routing",
            level=(routed / live_sig) if live_sig else 1.0,
            status="ok" if live_sig <= routed else "warn",
        ),
        ui.Readout(
            "Agent operations",
            f"{actions:,} actions",
            f"{ok_rate:.0%} ok · {int(t['refused'] or 0)} refused by policy",
            level=ok_rate,
            status="ok" if ok_rate >= 0.9 else "warn",
        ),
        ui.Readout(
            "Tool latency",
            f"p50 {float(t['p50_ms'] or 0) / 1000:.1f} s",
            f"p95 {float(t['p95_ms'] or 0) / 1000:.1f} s · last {len(latency)} calls",
            spark=latency,
        ),
        ui.Readout(
            "Evidence chain",
            f"{int(t['logged_queries'] or 0)} queries",
            f"hashed · {int(t['evidence'] or 0)} pointers · {int(t['attestations'] or 0)} signed",
        ),
    ]
    gauges = []
    if ev:
        gauges += [
            (
                "Recall",
                float(ev["recall"]),
                f"{ev['recall']:.0%}",
                f"{ev['tp']}/{ev['expected']} planted",
            ),
            (
                "Precision",
                float(ev["precision"]),
                f"{ev['precision']:.0%}",
                f"{ev['predicted']} raised",
            ),
        ]
    gauges.append(
        (
            "Filing integrity",
            1.0 if breaches == 0 else 0.0,
            "100%" if breaches == 0 else f"{breaches} open",
            "hash-bound attestations",
        )
    )
    # NaN, not 0, when no rule has a near-miss control defined: then there is no
    # false-positive figure to show, rather than a misleading 100%.
    controls = pd.to_numeric(ctl.get("controls"), errors="coerce") if ctl else float("nan")
    if pd.notna(controls) and controls > 0:
        fired_raw = pd.to_numeric(ctl.get("controls_fired"), errors="coerce")
        fired, controls = (int(fired_raw) if pd.notna(fired_raw) else 0), int(controls)
        gauges.append(
            (
                "False positives",
                1 - fired / controls,
                f"{fired} of {controls}",
                "legit control fired",
            )
        )
    ticker = []
    for r in log.head(16).itertuples():
        ok = str(r.outcome) == "SUCCESS"
        seconds = float(r.latency_ms) / 1000 if pd.notna(r.latency_ms) else 0.0
        ticker.append(
            f"<b>{html.escape(ui.ago(r.age_s))} ago</b> "
            f"{html.escape(str(r.agent_mode))} · {html.escape(str(r.tool))} "
            f'<span class="{"ok" if ok else "bad"}">{html.escape(str(r.outcome))}</span> '
            f'<span class="dim">{seconds:.1f} s</span>'
        )
    ui.console(
        "Surveillance telemetry",
        readouts,
        gauges=gauges,
        ticker=ticker,
        tag=f"{CFG['database']} · {CFG['profile']}".upper(),
        stamp=f"last action {ui.ago(t['action_age_s'])} ago",
        live=live_mode,
    )


@st.fragment(run_every=5 if live else None)
def headline() -> None:
    _kpi_row()
    _console(live)


headline()


# --------------------------------------------------------------------------- #
# Command center: pipeline, demo case, case queue, signals
# --------------------------------------------------------------------------- #


def _pipeline() -> None:
    f = data.funnel()
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


def _hero(q: pd.DataFrame) -> None:
    if not HERO or q.empty:
        return
    row = q[q["case_id"].isin(HERO["case_ids"])]
    if row.empty:
        return
    r = row.iloc[0]
    colour = ui.status_colours().get(r["status"], ui.palette()["slate"])
    ui.callout(
        f"<b>Demo case</b> · {html.escape(str(r['full_name']))} "
        f"(<code>{html.escape(str(r['case_id']))}</code>, {html.escape(str(r['branch_region']))}) "
        f"{ui.chip(str(r['status']), colour)}<br>"
        f"{int(r['signals'])} structuring signal(s) over cash deposits totalling "
        f"<b>{html.escape(lakh(r['flagged_inr']))}</b>, each kept just under the cash "
        "reporting line. Open it below and follow it to a filed STR."
    )


def _cases(q: pd.DataFrame) -> None:
    with ui.card(
        "cases",
        "Case queue",
        "Cash behind each case, and where every case stands. The outlined bar is the demo case.",
        glyph="folder_managed",
        tone="rose",
    ):
        show_visual = ui.visual("ov_cases")
        if q.empty:
            ui.empty("No cases in view. Signals are routed into cases by ROUTE_SIGNALS.")
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
        pal = ui.palette()
        left, right = st.columns([3, 2], gap="large")
        with left:
            bars = (
                alt.Chart(q)
                .mark_bar(cornerRadiusEnd=6, height={"band": 0.72})
                .encode(
                    y=alt.Y("full_name:N", sort="-x", title=None),
                    x=alt.X("flagged_lakh:Q", title="Cash flagged (₹ lakh)"),
                    color=alt.Color(
                        "branch_region:N",
                        title="Branch",
                        scale=alt.Scale(range=ui.series()),
                    ),
                    opacity=alt.condition("datum.demo", alt.value(1.0), alt.value(0.62)),
                    stroke=alt.condition("datum.demo", alt.value(pal["rose"]), alt.value(None)),
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
            labels = bars.mark_text(align="left", dx=5, color=pal["ink"], fontSize=11).encode(
                text="flagged:N", stroke=alt.value(None), opacity=alt.value(1.0)
            )
            ui.chart((bars + labels).properties(height=34 * len(q) + 40), axis={"labelLimit": 190})
        with right:
            status = (
                alt.Chart(q)
                .mark_bar(cornerRadiusEnd=6, height={"band": 0.6})
                .encode(
                    y=alt.Y("branch_region:N", title=None),
                    x=alt.X("count():Q", title="Cases", axis=alt.Axis(tickMinStep=1, format="d")),
                    color=alt.Color(
                        "status:N", title="Status", scale=ui.status_scale(q["status"].unique())
                    ),
                    tooltip=[
                        "branch_region:N",
                        "status:N",
                        alt.Tooltip("count():Q", title="Cases"),
                    ],
                )
                .properties(height=170)
            )
            ui.chart(status)
            sev = q.groupby("status").size().reset_index(name="cases")
            ring = (
                alt.Chart(sev)
                .mark_arc(innerRadius=46, outerRadius=72, cornerRadius=4, padAngle=0.02)
                .encode(
                    theta=alt.Theta("cases:Q", stack=True),
                    color=alt.Color("status:N", scale=ui.status_scale(sev["status"]), legend=None),
                    tooltip=["status:N", "cases:Q"],
                )
            )
            centre = (
                alt.Chart(pd.DataFrame({"t": [f"{len(q)}"]}))
                .mark_text(fontSize=22, fontWeight=700, color=pal["ink"])
                .encode(text="t:N")
            )
            ui.chart((ring + centre).properties(height=170))


def _signals() -> None:
    with ui.card(
        "signals",
        "Signals on the ledger timeline",
        "Each bubble is one signal, placed at the deposit that completed the pattern. "
        "Bigger means more deposits.",
        glyph="sensors",
        tone="amber",
    ):
        show_visual = ui.visual("ov_signals")
        sig = insights.signal_book()
        if not sig.empty and F.regions:
            sig = sig[sig["branch_region"].isin(F.regions)]
        if sig.empty:
            ui.empty("No signals in view yet.")
            return
        sig = sig.copy()
        sig["total"] = sig["total_inr"].map(inr)
        sig["total_lakh"] = sig["total_inr"].astype(float) / 100000
        if not show_visual:
            cols = ["fired_at", "full_name", "branch_region", "rule_id", "deposits", "accounts"]
            st.dataframe(
                ui.pretty(sig[[*cols, "total", "case_id"]]), hide_index=True, width="stretch"
            )
            return
        pal = ui.palette()
        bubbles = (
            alt.Chart(sig)
            .mark_circle(opacity=0.82, stroke=pal["ink"], strokeWidth=0.6)
            .encode(
                x=alt.X(
                    "fired_at:T",
                    title="Pattern completed (ledger time)",
                    axis=alt.Axis(format="%d %b", tickCount="day"),
                ),
                y=alt.Y(
                    "total_lakh:Q", title="Deposits in window (₹ lakh)", scale=alt.Scale(zero=False)
                ),
                size=alt.Size("deposits:Q", scale=alt.Scale(range=[120, 900]), legend=None),
                color=alt.Color(
                    "branch_region:N", title="Branch", scale=alt.Scale(range=ui.series())
                ),
                tooltip=[
                    alt.Tooltip("full_name:N", title="Customer"),
                    alt.Tooltip("branch_region:N", title="Branch"),
                    alt.Tooltip("deposits:Q", title="Deposits"),
                    alt.Tooltip("accounts:Q", title="Accounts"),
                    alt.Tooltip("total:N", title="Total"),
                    alt.Tooltip("case_id:N", title="Case"),
                    alt.Tooltip("fired_at:T", title="Completed", format="%d %b %H:%M"),
                ],
            )
            .properties(height=260)
        )
        ui.chart(bubbles)


@st.fragment(run_every=5 if live else None)
def command_center() -> None:
    ui.section("Signal-to-STR pipeline", "all branches · the evidence chain end to end")
    _pipeline()
    q = filters.cases(data.case_queue(), F)
    _hero(q)
    _cases(q)
    _signals()


# --------------------------------------------------------------------------- #
# Ledger analytics
# --------------------------------------------------------------------------- #


def _ledger_analytics() -> None:
    led = filters.ledger(insights.ledger(), F)
    if led.empty:
        ui.empty("No ledger rows inside these filters.")
        return
    pal = ui.palette()
    inflows = led[led["leg_side"] == "C"].copy()
    scale = _channel_scale(sorted(led["channel"].dropna().unique()))

    left, right = st.columns([3, 2], gap="medium")
    with (
        left,
        ui.card(
            "flows",
            "Daily inflows by channel",
            "Credits booked each day, stacked by payment channel. Count shows the busy rails "
            "that value hides.",
            glyph="stacked_line_chart",
        ),
    ):
        flow_measure = st.segmented_control(
            "Flow measure",
            ["Value", "Count"],
            default="Value",
            key="ov_flow",
            label_visibility="collapsed",
        )
        days = _days(F) or sorted(inflows["txn_date"].unique())
        channels = sorted(inflows["channel"].dropna().unique())
        cells = pd.MultiIndex.from_product([days, channels], names=["txn_date", "channel"])
        daily = (
            inflows.groupby(["txn_date", "channel"])
            .agg(value=("amount_inr", "sum"), count=("amount_inr", "size"))
            .reindex(cells, fill_value=0)
            .reset_index()
        )
        daily["crore"] = daily["value"] / 1e7
        daily["day"] = pd.to_datetime(daily["txn_date"])
        field, y_title, y_fmt = (
            ("count", "Credits per day", ",.0f")
            if flow_measure == "Count"
            else ("crore", "Inflows (₹ crore)", ",.1f")
        )
        area = (
            alt.Chart(daily)
            .mark_area(interpolate="monotone", opacity=0.86, line={"strokeWidth": 1.2})
            .encode(
                x=alt.X("day:T", title=None, axis=alt.Axis(format="%d %b", tickCount="day")),
                y=alt.Y(f"{field}:Q", stack=True, title=y_title),
                color=alt.Color("channel:N", title="Channel", scale=scale),
                order=alt.Order("channel:N"),
                tooltip=[
                    alt.Tooltip("day:T", title="Day", format="%a %d %b"),
                    "channel:N",
                    alt.Tooltip(f"{field}:Q", title=y_title, format=y_fmt),
                ],
            )
            .properties(height=262)
        )
        ui.chart(area)
    with (
        right,
        ui.card(
            "mix",
            "Channel mix",
            "Share of inflow value, and of the number of credits.",
            glyph="donut_large",
            tone="violet",
        ),
    ):
        measure = st.segmented_control(
            "Measure",
            ["Value", "Count"],
            default="Value",
            key="ov_mix",
            label_visibility="collapsed",
        )
        by = (
            inflows.groupby("channel")
            .agg(value=("amount_inr", "sum"), count=("amount_inr", "size"))
            .reset_index()
        )
        field = "count" if measure == "Count" else "value"
        total = by[field].sum()
        by["share"] = by[field] / total if total else 0
        centre_text = f"{int(total):,}" if field == "count" else "".join(ui.rupees(total))
        donut = (
            alt.Chart(by)
            .mark_arc(innerRadius=72, outerRadius=112, cornerRadius=5, padAngle=0.015)
            .encode(
                theta=alt.Theta(f"{field}:Q", stack=True),
                color=alt.Color(
                    "channel:N", title=None, scale=scale, legend=alt.Legend(orient="right")
                ),
                tooltip=[
                    "channel:N",
                    alt.Tooltip("share:Q", format=".1%", title="Share"),
                    alt.Tooltip(f"{field}:Q", format=",.0f"),
                ],
            )
        )
        centre = alt.Chart(
            pd.DataFrame({"t": [centre_text], "s": ["credits" if field == "count" else "inflows"]})
        )
        ui.chart(
            (
                donut
                + centre.mark_text(fontSize=19, fontWeight=700, color=pal["ink"], dy=-6).encode(
                    text="t:N"
                )
                + centre.mark_text(fontSize=11, color=pal["slate"], dy=14).encode(text="s:N")
            ).properties(height=262)
        )

    with ui.card(
        "heat",
        "Activity heatmap",
        "Transactions by booking hour and day: when money moves.",
        glyph="grid_on",
        tone="cyan",
    ):
        hm = led.assign(hour=led["txn_timestamp"].dt.hour)
        grid = hm.groupby(["txn_date", "hour"])["txn_pair_id"].nunique().reset_index(name="txns")
        grid["day"] = pd.to_datetime(grid["txn_date"]).dt.strftime("%a %d %b")
        order = [pd.Timestamp(d).strftime("%a %d %b") for d in sorted(grid["txn_date"].unique())]
        heat = (
            alt.Chart(grid)
            .mark_rect(cornerRadius=3, stroke=None)
            .encode(
                x=alt.X("hour:O", title="Hour of day", axis=alt.Axis(labelAngle=0)),
                y=alt.Y("day:N", sort=order, title=None),
                color=alt.Color(
                    "txns:Q",
                    title="Transactions",
                    scale=alt.Scale(type="sqrt"),
                    legend=alt.Legend(orient="right", gradientLength=160),
                ),
                tooltip=[
                    "day:N",
                    alt.Tooltip("hour:O", title="Hour"),
                    alt.Tooltip("txns:Q", title="Transactions"),
                ],
            )
            .properties(height=max(180, 28 * len(order)))
        )
        ui.chart(heat)

    left, right = st.columns(2, gap="medium")
    th = insights.cash_thresholds()
    with (
        left,
        ui.card(
            "band",
            "Cash deposits against the reporting line",
            "Structuring keeps each deposit just under the line; the amber band is the governed "
            "pattern.",
            glyph="price_check",
            tone="amber",
        ),
    ):
        cash = inflows[inflows["is_cash"]]
        if cash.empty or not th["ctr"]:
            ui.empty("No cash deposits inside these filters.")
        else:
            ctr_l = th["ctr"] / 1e5
            lo_l = (th["band_lo"] or th["ctr"]) / 1e5
            step = 0.5
            lak = cash["amount_inr"] / 1e5
            shown = lak[lak < 2 * ctr_l]
            bins = (shown // step * step).round(2).value_counts().sort_index().reset_index()
            bins.columns = ["lo", "deposits"]
            bins["hi"] = bins["lo"] + step
            bins["zone"] = bins["lo"].map(
                lambda v: (
                    "At or above the line"
                    if v >= ctr_l
                    else ("Structuring band" if v >= lo_l - 1e-9 else "Below the band")
                )
            )
            zones = ["Below the band", "Structuring band", "At or above the line"]
            hist = (
                alt.Chart(bins)
                .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3, binSpacing=1)
                .encode(
                    x=alt.X(
                        "lo:Q",
                        title="Cash deposit (₹ lakh)",
                        scale=alt.Scale(domain=[0, 2 * ctr_l]),
                    ),
                    x2="hi:Q",
                    y=alt.Y("deposits:Q", title="Deposits"),
                    y2=alt.datum(0),
                    color=alt.Color(
                        "zone:N",
                        title=None,
                        scale=alt.Scale(
                            domain=zones, range=[pal["slate"], pal["amber"], pal["rose"]]
                        ),
                    ),
                    tooltip=[
                        alt.Tooltip("lo:Q", title="From ₹ lakh", format=".1f"),
                        alt.Tooltip("hi:Q", title="To ₹ lakh", format=".1f"),
                        "deposits:Q",
                        "zone:N",
                    ],
                )
            )
            rule_df = pd.DataFrame({"x": [ctr_l], "t": [f"Reporting line {lakh(th['ctr'])}"]})
            rule = (
                alt.Chart(rule_df)
                .mark_rule(color=pal["rose"], strokeDash=[6, 4], strokeWidth=2)
                .encode(x="x:Q")
            )
            text = (
                alt.Chart(rule_df)
                .mark_text(
                    align="left", dx=6, dy=-6, color=pal["rose"], fontSize=11, fontWeight=600
                )
                .encode(x="x:Q", y=alt.value(12), text="t:N")
            )
            ui.chart((hist + rule + text).properties(height=270))
            over = int((lak >= 2 * ctr_l).sum())
            if over:
                st.caption(
                    f"{over:,} deposit(s) at or above {lakh(2 * th['ctr'])} sit beyond the axis."
                )
    with (
        right,
        ui.card(
            "tickets",
            "Ticket size by channel",
            "Spread of credit amounts on a log scale: the box is the middle half.",
            glyph="candlestick_chart",
            tone="blue",
        ),
    ):
        box = (
            alt.Chart(inflows[inflows["amount_inr"] > 0][["channel", "amount_inr"]])
            .mark_boxplot(
                extent="min-max",
                size=28,
                median={"color": pal["ink"], "strokeWidth": 2},
                ticks=True,
            )
            .encode(
                x=alt.X("channel:N", title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y(
                    "amount_inr:Q", title="Credit amount (₹, log)", scale=alt.Scale(type="log")
                ),
                color=alt.Color("channel:N", scale=scale, legend=None),
            )
            .properties(height=270)
        )
        ui.chart(box)

    left, right = st.columns([3, 2], gap="medium")
    with (
        left,
        ui.card(
            "pareto",
            "Customer concentration",
            "Top customers by inflow value, with the cumulative share they hold.",
            glyph="leaderboard",
            tone="emerald",
        ),
    ):
        names = _names()
        by_party = inflows.groupby("party_id")["amount_inr"].sum().sort_values(ascending=False)
        total = by_party.sum()
        top = by_party.head(12).reset_index()
        top["customer"] = top["party_id"].map(names).fillna(top["party_id"])
        top["crore"] = top["amount_inr"] / 1e7
        top["cum"] = top["amount_inr"].cumsum() / total if total else 0
        order = top["customer"].tolist()
        bars = (
            alt.Chart(top)
            .mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5, color=pal["navy"])
            .encode(
                x=alt.X(
                    "customer:N",
                    sort=order,
                    title=None,
                    axis=alt.Axis(labelAngle=-32, labelLimit=120),
                ),
                y=alt.Y("crore:Q", title="Inflows (₹ crore)"),
                tooltip=[
                    "customer:N",
                    alt.Tooltip("crore:Q", format=",.1f", title="₹ crore"),
                    alt.Tooltip("cum:Q", format=".1%", title="Cumulative"),
                ],
            )
        )
        line = (
            alt.Chart(top)
            .mark_line(
                color=pal["emerald"],
                strokeWidth=2.5,
                point={"filled": True, "size": 60, "color": pal["emerald"]},
            )
            .encode(
                x=alt.X("customer:N", sort=order),
                y=alt.Y(
                    "cum:Q",
                    title="Cumulative share",
                    axis=alt.Axis(format="%"),
                    scale=alt.Scale(domain=[0, 1]),
                ),
            )
        )
        ui.chart(alt.layer(bars, line).resolve_scale(y="independent").properties(height=300))
        if total:
            st.caption(
                f"The largest customer holds {top['amount_inr'].iloc[0] / total:.0%} of inflows "
                f"in view; the top {len(top)} hold {top['cum'].iloc[-1]:.0%} across "
                f"{len(by_party):,} customers."
            )
    with (
        right,
        ui.card(
            "products",
            "Product mix",
            "How each account type is used: share of transactions by type.",
            glyph="category",
            tone="slate",
        ),
    ):
        mix = (
            led.groupby(["account_type", "txn_type"])["txn_pair_id"]
            .nunique()
            .reset_index(name="txns")
        )
        stacked = (
            alt.Chart(mix)
            .mark_bar(height={"band": 0.66})
            .encode(
                y=alt.Y("account_type:N", title=None, sort="-x"),
                x=alt.X(
                    "txns:Q",
                    stack="normalize",
                    title="Share of transactions",
                    axis=alt.Axis(format="%"),
                ),
                color=alt.Color(
                    "txn_type:N",
                    title="Type",
                    scale=alt.Scale(
                        range=[pal["navy"], pal["emerald"], pal["amber"], pal["violet"]]
                    ),
                ),
                tooltip=[
                    "account_type:N",
                    "txn_type:N",
                    alt.Tooltip("txns:Q", title="Transactions"),
                ],
            )
            .properties(height=300)
        )
        ui.chart(stacked)


# --------------------------------------------------------------------------- #
# Customer and network risk
# --------------------------------------------------------------------------- #


def _customer_risk() -> None:
    pal = ui.palette()
    kyc = insights.kyc_profile()
    if F.regions and not kyc.empty:
        kyc = kyc[kyc["branch_region"].isin(F.regions)]
    if kyc.empty:
        ui.empty("No customers inside these filters.")
        return
    total = int(kyc["parties"].sum())
    high = int(kyc.loc[kyc["risk_rating"] == "HIGH", "parties"].sum())
    pending = int(kyc.loc[kyc["kyc_status"] != "VERIFIED", "parties"].sum())
    pep = int(kyc.loc[kyc["pep_flag"].astype(bool), "parties"].sum())
    edges = insights.network_edges()
    flow_edges = (
        int(edges.loc[edges["relationship_type"] == "FUND_FLOW", "edges"].sum())
        if not edges.empty
        else 0
    )
    ui.kpis(
        [
            ui.Kpi(
                "Customers", f"{total:,}", note="in the KYC register", glyph="groups", tone="navy"
            ),
            ui.Kpi(
                "High risk",
                f"{high:,}",
                note="enhanced due diligence",
                delta=f"{high / total:.1%} of base",
                state="bad" if high else "good",
                glyph="gpp_maybe",
                tone="rose",
            ),
            ui.Kpi(
                "KYC refresh due",
                f"{pending:,}",
                note="pending review or expired",
                delta=f"{pending / total:.1%} of base",
                state="warn" if pending else "good",
                glyph="assignment_late",
                tone="amber",
            ),
            ui.Kpi(
                "PEPs",
                f"{pep:,}",
                note="politically exposed persons",
                glyph="account_box",
                tone="violet",
            ),
            ui.Kpi(
                "Fund-flow links",
                f"{flow_edges:,}",
                note="customer-to-customer edges",
                glyph="share",
                tone="cyan",
            ),
        ]
    )
    risk_order = ["LOW", "MEDIUM", "HIGH"]
    risk_scale = alt.Scale(domain=risk_order, range=[pal["emerald"], pal["amber"], pal["rose"]])
    left, mid, right = st.columns([5, 4, 4], gap="medium")
    with (
        left,
        ui.card(
            "kyc-risk",
            "Risk rating by branch",
            "Customers per branch, by KYC risk rating.",
            glyph="shield_person",
            tone="rose",
        ),
    ):
        bars = (
            alt.Chart(kyc)
            .mark_bar(cornerRadiusEnd=5, height={"band": 0.62})
            .encode(
                y=alt.Y("branch_region:N", title=None),
                x=alt.X("sum(parties):Q", title="Customers"),
                color=alt.Color("risk_rating:N", title="Risk", scale=risk_scale, sort=risk_order),
                order=alt.Order("risk_order:Q"),
                tooltip=[
                    "branch_region:N",
                    "risk_rating:N",
                    alt.Tooltip("sum(parties):Q", title="Customers"),
                ],
            )
            .transform_calculate(risk_order="indexof(['LOW','MEDIUM','HIGH'], datum.risk_rating)")
            .properties(height=230)
        )
        ui.chart(bars)
    with (
        mid,
        ui.card(
            "kyc-status",
            "KYC status",
            "Verified, due for review, or lapsed.",
            glyph="fact_check",
            tone="emerald",
        ),
    ):
        status = kyc.groupby("kyc_status")["parties"].sum().reset_index()
        order = ["VERIFIED", "PENDING_REVIEW", "EXPIRED"]
        donut = (
            alt.Chart(status)
            .mark_arc(innerRadius=58, outerRadius=92, cornerRadius=4, padAngle=0.02)
            .encode(
                theta=alt.Theta("parties:Q", stack=True),
                color=alt.Color(
                    "kyc_status:N",
                    title=None,
                    scale=alt.Scale(
                        domain=order, range=[pal["emerald"], pal["amber"], pal["rose"]]
                    ),
                    legend=alt.Legend(orient="bottom", labelExpr="replace(datum.label, '_', ' ')"),
                ),
                tooltip=["kyc_status:N", "parties:Q"],
            )
        )
        verified = int(status.loc[status["kyc_status"] == "VERIFIED", "parties"].sum())
        centre = alt.Chart(pd.DataFrame({"t": [f"{verified / total:.0%}"], "s": ["verified"]}))
        ui.chart(
            (
                donut
                + centre.mark_text(fontSize=20, fontWeight=700, color=pal["ink"], dy=-6).encode(
                    text="t:N"
                )
                + centre.mark_text(fontSize=11, color=pal["slate"], dy=13).encode(text="s:N")
            ).properties(height=230)
        )
    with (
        right,
        ui.card(
            "graph",
            "Party graph",
            "Links the network rules traverse, by type.",
            glyph="hub",
            tone="cyan",
        ),
    ):
        if edges.empty:
            ui.empty("No relationships loaded.")
        else:
            e = edges.copy()
            e["label"] = e["relationship_type"].str.replace("_", " ").str.title()
            rose = (
                alt.Chart(e)
                .mark_arc(innerRadius=18, cornerRadius=4, padAngle=0.03, stroke=None)
                .encode(
                    theta=alt.Theta("edges:Q", stack=True),
                    radius=alt.Radius(
                        "edges:Q", scale=alt.Scale(type="sqrt", zero=True, rangeMin=24)
                    ),
                    color=alt.Color(
                        "label:N",
                        title=None,
                        scale=alt.Scale(
                            range=[
                                pal["cyan"],
                                pal["navy"],
                                pal["violet"],
                                pal["amber"],
                                pal["emerald"],
                            ]
                        ),
                    ),
                    tooltip=[
                        alt.Tooltip("label:N", title="Link"),
                        "edges:Q",
                        alt.Tooltip("parties:Q", title="Parties"),
                    ],
                )
                .properties(height=230)
            )
            ui.chart(rose)

    with ui.card(
        "flows-top",
        "Largest fund flows",
        "Customer-to-customer transfers by total value (square-root scale).",
        glyph="conversion_path",
        tone="violet",
    ):
        flows = insights.top_flows(12)
        if F.regions and not flows.empty:
            regions = _region_of()
            inside = flows["party_a_id"].map(regions).isin(F.regions) | flows["party_b_id"].map(
                regions
            ).isin(F.regions)
            flows = flows[inside]
        if flows.empty:
            ui.empty("No fund flows inside these filters.")
            return
        fl = flows.head(10).copy()
        fl["route"] = (
            fl["payer"].fillna(fl["party_a_id"]) + "  →  " + fl["payee"].fillna(fl["party_b_id"])
        )
        fl["crore"] = fl["total_amount_inr"].astype(float) / 1e7
        order = fl.sort_values("crore", ascending=False)["route"].tolist()
        base = alt.Chart(fl).encode(y=alt.Y("route:N", sort=order, title=None))
        stems = base.mark_rule(strokeWidth=3, color=pal["muted"], opacity=0.7).encode(
            x=alt.X("crore:Q", title="Total transferred (₹ crore)", scale=alt.Scale(type="sqrt")),
            x2=alt.datum(0),
        )
        heads = base.mark_circle(size=170, color=pal["violet"], opacity=1).encode(
            x="crore:Q",
            tooltip=[
                "route:N",
                alt.Tooltip("crore:Q", format=",.1f", title="₹ crore"),
                alt.Tooltip("txn_count:Q", title="Transfers"),
            ],
        )
        tags = base.mark_text(align="left", dx=12, fontSize=11, color=pal["ink"]).encode(
            x="crore:Q", text=alt.Text("crore:Q", format=",.0f")
        )
        ui.chart(
            (stems + heads + tags).properties(height=36 * len(fl) + 40), axis={"labelLimit": 300}
        )


t1, t2, t3 = st.tabs(
    [
        ":material/radar: Command center",
        ":material/analytics: Ledger analytics",
        ":material/hub: Customer & network risk",
    ]
)
with t1:
    command_center()
    # Outside the fragment: navigation from an auto-refreshing fragment is fragile.
    queue = filters.cases(data.case_queue(), F)
    if not queue.empty:
        ids = queue["case_id"].tolist()
        names = queue.set_index("case_id")["full_name"].to_dict()
        hero_first = next((i for i, c in enumerate(ids) if HERO and c in HERO["case_ids"]), 0)
        with ui.card(
            "open",
            "Investigate a case",
            "Open any case in the queue and follow it to a filed STR.",
            glyph="manage_search",
        ):
            o1, o2 = st.columns([4, 1], vertical_alignment="bottom")
            chosen = o1.selectbox(
                "Open a case", ids, index=hero_first, format_func=lambda c: f"{c} -- {names.get(c)}"
            )
            if o2.button(
                "Investigate", type="primary", width="stretch", icon=":material/arrow_forward:"
            ):
                st.session_state["case_id"] = chosen
                st.switch_page("app_pages/case.py")
with t2:
    _ledger_analytics()
with t3:
    _customer_risk()
