"""Case investigation: who, what fired, and the transactions behind it."""

import html
import json

import altair as alt
import pandas as pd
import streamlit as st
from lib import agent, auth, data, ui
from lib.db import HERO, PROC, call, inr, lakh, reason
from lib.str_view import STATUS_HELP, review_result, str_document

ui.style()
st.title("Case investigation")

queue = data.case_queue()
if queue.empty:
    st.info("No cases yet.")
    st.stop()

ids = queue["case_id"].tolist()
names = queue.set_index("case_id")["full_name"].to_dict()
wanted = st.session_state.get("case_id") or (HERO["case_ids"][0] if HERO else ids[0])
case_id = st.selectbox(
    "Case",
    ids,
    index=ids.index(wanted) if wanted in ids else 0,
    format_func=lambda c: (
        f"{c} -- {names.get(c) or 'unknown party'}"
        + ("  (demo hero)" if HERO and c in HERO["case_ids"] else "")
    ),
)
st.session_state["case_id"] = case_id

case = data.case(case_id)
party = data.party(case["party_id"]) if case else None
if not case or not party:
    st.error("Case or party not found.")
    st.stop()

signals = data.case_signals(case_id)
txns = data.evidence_transactions(case_id)
chain = data.chain_counts(case_id)

# ---- header and evidence-chain progress ---------------------------------- #
st.subheader(f"{party['full_name']}  ·  {case['party_id']}")
st.markdown(
    ui.chip(str(case["status"]), ui.STATUS_COLOURS.get(case["status"], ui.SLATE))
    + ui.chip(str(case["case_type"]), ui.NAVY)
    + ui.chip(str(case["branch_region"]), ui.SKY)
    + f" &nbsp;Opened {html.escape(str(case['opened_at'])[:16])}",
    unsafe_allow_html=True,
)

steps = [
    ("Signal", chain["signals"], "a rule fired"),
    ("Evidence", chain["evidence"], "pointers + logged queries"),
    ("Finding", chain["findings"], "facts the STR may state"),
    ("STR draft", chain["artifacts"], "governed template"),
    ("Review", chain["reviews"], "every figure re-run"),
    ("Attested", chain["attestations"], "officer's signature"),
]
reached = max((i for i, (_, n, _) in enumerate(steps) if n), default=-1)
ui.strip(
    [(name, f"{int(n):,}" if n else "--", note) for name, n, note in steps],
    active=reached + 1 if reached + 1 < len(steps) else None,
    done=reached,
)
st.caption(
    "The evidence chain: each step writes rows that point at the step before, so any "
    "sentence in the STR traces back to the transactions. The highlighted tile is next."
)

# ---- party profile -------------------------------------------------------- #
left, right = st.columns([2, 3])
with left:
    st.markdown("#### Customer profile (KYC)")
    profile = {
        "Party type": party["party_type"],
        "Occupation": party["occupation"],
        "Declared annual income": inr(party["declared_annual_income"]),
        "Source of funds": party["source_of_funds"],
        "Risk rating": party["risk_rating"],
        "PEP": "Yes" if party["pep_flag"] else "No",
        "KYC status": f"{party['kyc_status']} (reviewed {party['kyc_last_reviewed']})",
        "Customer since": str(party["customer_since"]),
        "Branch": f"{party['branch_region']} / {party['branch_code']}",
        "Aadhaar": f"XXXX-XXXX-{party['aadhaar_last4']}",
    }
    flags = {}
    if str(party["risk_rating"]).upper() == "HIGH":
        flags["Risk rating"] = "high risk"
    if party["pep_flag"]:
        flags["PEP"] = "enhanced due diligence"
    ui.facts(profile, flags=flags)
    if party.get("kyc_note"):
        st.caption("KYC note (free text, untrusted input)")
        st.text(party["kyc_note"])
    st.markdown("#### Accounts")
    st.dataframe(ui.pretty(data.accounts(case["party_id"])), hide_index=True, width="stretch")

# ---- what fired ------------------------------------------------------------ #
with right:
    st.markdown("#### Why it was flagged")
    for _, s in signals.iterrows():
        detail = json.loads(s["detail"]) if isinstance(s["detail"], str) else (s["detail"] or {})
        with st.container(border=True):
            st.markdown(f"**{s['rule_id']}** v{s['rule_version']} · severity **{s['severity']}**")
            if s.get("rule_description"):
                st.caption(s["rule_description"])
            ui.strip(
                [
                    ("Cash deposits in band", str(detail.get("deposit_count", "-")), ""),
                    ("Total deposited", lakh(detail.get("deposit_total_inr")), ""),
                    ("Accounts used", str(len(detail.get("accounts", []))), ""),
                ],
                arrows=False,
            )
            band = (detail.get("band_lower_inr"), detail.get("band_upper_inr"))
            st.caption(
                f"Each deposit between {inr(band[0])} and {inr(band[1])}, just under the "
                f"{inr(detail.get('ctr_threshold_inr'))} cash reporting threshold, spread over "
                f"{len(detail.get('accounts', []))} accounts within {detail.get('window_days')} "
                f"days (window from {s['window_bucket']}). Thresholds read from POLICY_CLAUSE."
            )

    if not txns.empty:
        st.markdown("#### The deposits")
        d0: dict = {}
        if not signals.empty:
            raw = signals.iloc[0]["detail"]
            d0 = json.loads(raw) if isinstance(raw, str) else (raw or {})
        ctr, lo, hi = (d0.get(k) for k in ("ctr_threshold_inr", "band_lower_inr", "band_upper_inr"))
        t = txns.sort_values("txn_timestamp").copy()
        t["lakh"] = t["amount_inr"].astype(float) / 100000
        t["amount"] = t["amount_inr"].map(inr)
        t["running_lakh"] = t["lakh"].cumsum()
        x = alt.X("txn_timestamp:T", title=None)
        layers = []
        if lo and hi:
            band = pd.DataFrame({"lo": [float(lo) / 100000], "hi": [float(hi) / 100000]})
            layers.append(
                alt.Chart(band).mark_rect(color=ui.AMBER, opacity=0.15).encode(y="lo:Q", y2="hi:Q")
            )
        if ctr:
            line = pd.DataFrame({"y": [float(ctr) / 100000]})
            rule = alt.Chart(line)
            layers.append(rule.mark_rule(strokeDash=[6, 4], color=ui.RED).encode(y="y:Q"))
            layers.append(
                rule.mark_text(align="left", dx=4, dy=-7, color=ui.RED, fontSize=11).encode(
                    y="y:Q", text=alt.value(f"Reporting line {lakh(ctr)}"), x=alt.value(0)
                )
            )
        layers.append(
            alt.Chart(t)
            .mark_circle(size=160, opacity=0.9, stroke="white", strokeWidth=1)
            .encode(
                x=x,
                y=alt.Y("lakh:Q", title="Deposit (₹ lakh)", scale=alt.Scale(zero=False)),
                color=alt.Color(
                    "account_id:N",
                    title="Account",
                    scale=alt.Scale(range=ui.BRANCH_SCHEME),
                    legend=alt.Legend(orient="bottom"),
                ),
                tooltip=[
                    "txn_id:N",
                    alt.Tooltip("txn_timestamp:T", format="%d %b %H:%M"),
                    "account_id:N",
                    "amount:N",
                    "channel:N",
                ],
            )
        )
        st.altair_chart(alt.layer(*layers).properties(height=230), width="stretch")
        cumulative = (
            alt.Chart(t)
            .mark_area(
                interpolate="step-after", color=ui.NAVY, opacity=0.18, line={"color": ui.NAVY}
            )
            .encode(
                x=x,
                y=alt.Y("running_lakh:Q", title="Running total (₹ lakh)"),
                tooltip=[
                    alt.Tooltip("txn_timestamp:T", format="%d %b %H:%M"),
                    alt.Tooltip("running_lakh:Q", title="Total (₹ lakh)", format=",.2f"),
                ],
            )
            .properties(height=150)
        )
        st.altair_chart(cumulative, width="stretch")
        st.caption(
            "Top: every deposit sits in the shaded band just under the dashed reporting line. "
            f"Bottom: together they add up to {lakh(t['amount_inr'].sum())}, none of it reported."
        )

if not txns.empty:
    head, switch = st.columns([4, 1], vertical_alignment="bottom")
    head.markdown("#### Evidence transactions")
    with switch:
        show_visual = ui.visual("case_txns")
    shown = txns.assign(amount=txns["amount_inr"].map(inr))
    if show_visual:
        per_account = (
            alt.Chart(shown.assign(lakh=shown["amount_inr"].astype(float) / 100000))
            .mark_bar(cornerRadiusEnd=4)
            .encode(
                y=alt.Y("account_id:N", title=None),
                x=alt.X("sum(lakh):Q", title="Deposited (₹ lakh)"),
                color=alt.Color(
                    "channel:N",
                    title="Channel",
                    scale=alt.Scale(range=ui.BRANCH_SCHEME),
                    legend=alt.Legend(orient="bottom"),
                ),
                tooltip=[
                    "account_id:N",
                    "channel:N",
                    alt.Tooltip("count():Q", title="Deposits"),
                    alt.Tooltip("sum(lakh):Q", title="₹ lakh", format=",.2f"),
                ],
            )
            .properties(height={"step": 38})
        )
        st.altair_chart(per_account, width="stretch")
        st.caption("How the cash was spread across accounts: splitting is part of the pattern.")
    else:
        cols = [
            "txn_id",
            "txn_timestamp",
            "account_id",
            "amount",
            "channel",
            "txn_type",
            "branch_code",
            "source_ref",
        ]
        st.dataframe(ui.pretty(shown[cols]), hide_index=True, width="stretch")


# ---- the STR workflow: gather -> draft -> review ---------------------------- #
st.divider()
st.header("Investigate, draft and review the STR")
st.caption(
    "Each button calls one stored procedure: the same tools a Cortex Agent will call "
    "once this account allows agents. Every step is written to the audit log."
)

arts = data.case_artifacts(case_id)
latest = arts.iloc[0].to_dict() if not arts.empty else None
filed = bool((arts["status"] == "FILED").any()) if not arts.empty else False


def run(label: str, fn) -> None:
    try:
        with st.spinner(label):
            result = fn()
        st.session_state["last_action"] = (True, label, result)
    except Exception as exc:  # shown to the user, verbatim
        st.session_state["last_action"] = (False, label, reason(exc))
    st.rerun()


b = st.columns([1, 1, 1, 2], vertical_alignment="center")
maker = auth.can("can_investigate")
locked = filed or not maker
gathered = chain["evidence"] > 0
can_review = maker and latest is not None and latest["status"] == "DRAFT"
# The next step is the primary button, so the page always says what to do next.
# An approved STR's next step is the officer's sign-off, so no button is primary.
redraft = latest is None or latest["status"] == "REJECTED"
next_step = (
    None
    if locked
    else "review"
    if can_review
    else ("draft" if gathered else "gather")
    if redraft
    else None
)
plant = b[3].toggle(
    "Plant a defect in the next draft",
    help="Demo: inflates the deposit total by 10% and cites a clause that does not exist. "
    "Watch the Reviewer catch both.",
    disabled=locked,
)
if b[0].button(
    "1 · Gather evidence",
    type="primary" if next_step == "gather" else "secondary",
    width="stretch",
    disabled=locked,
):
    run("Gathering evidence", lambda: call(PROC["gather_evidence"], case_id))
if b[1].button(
    "2 · Draft STR",
    type="primary" if next_step == "draft" else "secondary",
    width="stretch",
    disabled=locked,
):
    run("Drafting the STR", lambda: call(PROC["draft_str"], case_id, bool(plant)))
if b[2].button(
    "3 · Run Reviewer",
    type="primary" if next_step == "review" else "secondary",
    width="stretch",
    disabled=not can_review,
):
    run("Reviewing", lambda: call(PROC["review_str"], latest["artifact_id"]))
if not maker and not filed:
    st.info(
        "Maker-checker: the Principal Officer signs STRs but does not draft them. "
        "Sign in as the AML Analyst to investigate.",
        icon=":material/lock:",
    )

last = st.session_state.pop("last_action", None)
if last:
    ok, label, result = last
    if ok:
        st.toast(f"{label}: done")
    else:
        st.error(f"{label} refused: {result}")


# ---- the agentic path: Cortex Agent in the agent account ------------------- #
with st.container(border=True):
    a1, a2 = st.columns([3, 1], vertical_alignment="center")
    a1.markdown(
        "**AI STR (Cortex Agent)**: `RC_STR_AGENT`, in a separate Snowflake account, drafts the "
        "narrative with an Anthropic model, a non-Anthropic model cross-reviews it, and every "
        "amount, count and date is traced to this case's logged evidence. An approved draft is "
        "written back here as a new version and re-checked by the Reviewer."
    )
    ok_agent, why = agent.available()
    if a2.button(
        "4 · AI STR + PDF",
        type="secondary",
        width="stretch",
        disabled=locked or not ok_agent,
        help=why or "Runs the cross-account agent pipeline (a few minutes).",
    ):
        with st.status("Running the STR agent...", expanded=True) as box:
            res = agent.generate(
                case_id,
                progress=lambda step, msg: box.write(f"`{step}` {msg}"),
                user=(auth.current() or {}).get("username", "unknown"),
            )
            box.update(
                label=f"STR agent: {res.get('status')}",
                state="error" if res.get("error") else "complete",
            )
        st.session_state.setdefault("ai_str", {})[case_id] = res
        st.cache_data.clear()
    elif not ok_agent:
        a2.caption(why)
    res = st.session_state.get("ai_str", {}).get(case_id)
    if res:
        air, dr = res.get("ai_review") or {}, res.get("data_review") or {}
        if res.get("error") and not res.get("sections"):
            st.error(f"STR agent failed: {res['error']}")
        else:
            (st.success if res.get("status") == "READY_FOR_ATTESTATION" else st.warning)(
                f"**{res.get('status')}** · drafted by `{air.get('drafter_model')}` (attempt "
                f"{res.get('attempt')}) · agent reviewer `{air.get('reviewer_model')}`: "
                f"**{air.get('decision')}** · data-account Reviewer: **{dr.get('decision', 'not run')}**"
            )
            for d in (air.get("defects") or []):
                st.caption(f"- {d}")
            if res.get("pdf_bytes"):
                st.download_button(
                    "Download STR PDF",
                    data=res["pdf_bytes"],
                    file_name=res.get("pdf_name", f"STR_{case_id}.pdf"),
                    mime="application/pdf",
                    icon=":material/picture_as_pdf:",
                )
            st.caption(
                f"package `{res.get('package_id')}` · run `{res.get('run_id')}` · "
                f"PDF SHA-256 `{(res.get('pdf_sha256') or '')[:16]}...`"
            )
        if st.button("Refresh the STR below", key="ai_refresh"):
            st.rerun()


def evidence_chain() -> None:
    st.markdown("##### Evidence pointers")
    st.dataframe(ui.pretty(data.evidence_rows(case_id)), hide_index=True, width="stretch")
    st.markdown("##### Logged queries (the source of every figure)")
    st.dataframe(
        ui.pretty(data.query_logs(case_id).drop(columns=["sql_text"])),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "Each query's exact text, bind value and canonical result hash are stored. "
        "The Reviewer re-runs them and compares."
    )


if latest is None and not gathered:
    st.info("No evidence gathered yet. Start with **1 · Gather evidence**.")
elif latest is None:
    queries = len(data.query_logs(case_id))
    st.success(
        f"Evidence gathered: {chain['evidence']:,} pointer(s) and {queries:,} logged "
        "query(ies). No STR drafted yet. Next: **2 · Draft STR**."
        if maker
        else f"Evidence gathered: {chain['evidence']:,} pointer(s) and {queries:,} logged "
        "query(ies). The AML Analyst drafts the STR next."
    )
    with st.expander("What was gathered", expanded=True):
        evidence_chain()
else:
    if latest["status"] == "REVIEWED":
        s1, s2 = st.columns([3, 1], vertical_alignment="center")
        s1.success(
            "The Reviewer approved this STR. It now needs the Principal Officer's signature."
        )
        if s2.button("Go to sign-off", width="stretch"):
            st.switch_page("app_pages/signoff.py")
    elif filed:
        st.success("STR attested by the Principal Officer and filed. The case is closed.")
    t1, t2, t3 = st.tabs(["Latest STR", "Version history", "Evidence chain"])
    with t1:
        left, right = st.columns([3, 2])
        with left:
            a = data.artifact(latest["artifact_id"])
            if a:
                str_document(a)
        with right:
            st.markdown("##### Reviewer")
            review_result(latest)
    with t2:
        hist = arts[["version", "artifact_id", "status", "review_decision", "created_at"]].copy()
        hist["meaning"] = hist["status"].map(STATUS_HELP)
        st.dataframe(ui.pretty(hist), hide_index=True, width="stretch")
        for _, row in arts.iterrows():
            if row["review_decision"] == "REJECT":
                with st.expander(f"v{row['version']} -- why the Reviewer rejected it"):
                    review_result(row.to_dict())
    with t3:
        evidence_chain()
