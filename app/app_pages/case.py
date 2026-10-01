"""Case investigation: who, what fired, and the transactions behind it."""

import json

import altair as alt
import pandas as pd
import streamlit as st
from lib import data
from lib.db import HERO, PROC, call, inr, lakh, reason
from lib.str_view import STATUS_HELP, review_result, str_document

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
h = st.columns(4)
h[0].metric("Status", case["status"])
h[1].metric("Case type", case["case_type"])
h[2].metric("Branch", case["branch_region"])
h[3].metric("Opened", str(case["opened_at"])[:16])

steps = [
    ("Signal", chain["signals"]),
    ("Evidence", chain["evidence"]),
    ("Finding", chain["findings"]),
    ("STR draft", chain["artifacts"]),
    ("Review", chain["reviews"]),
    ("Attested", chain["attestations"]),
]
st.markdown(
    "  →  ".join(f"**{name}** ✓" if n else f"{name} ·" for name, n in steps),
    help="The evidence chain: each step writes rows that point at the step before.",
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
    st.dataframe(
        pd.DataFrame(profile.items(), columns=["Field", "Value"]),
        hide_index=True,
        use_container_width=True,
    )
    if party.get("kyc_note"):
        st.caption("KYC note (free text, untrusted input)")
        st.text(party["kyc_note"])
    st.markdown("#### Accounts")
    st.dataframe(data.accounts(case["party_id"]), hide_index=True, use_container_width=True)

# ---- what fired ------------------------------------------------------------ #
with right:
    st.markdown("#### Why it was flagged")
    for _, s in signals.iterrows():
        detail = json.loads(s["detail"]) if isinstance(s["detail"], str) else (s["detail"] or {})
        with st.container(border=True):
            st.markdown(f"**{s['rule_id']}** v{s['rule_version']} · severity **{s['severity']}**")
            if s.get("rule_description"):
                st.caption(s["rule_description"])
            m = st.columns(3)
            m[0].metric("Cash deposits in band", detail.get("deposit_count", "-"))
            m[1].metric("Total deposited", lakh(detail.get("deposit_total_inr")))
            m[2].metric("Accounts used", len(detail.get("accounts", [])))
            band = (detail.get("band_lower_inr"), detail.get("band_upper_inr"))
            st.caption(
                f"Each deposit between {inr(band[0])} and {inr(band[1])}, just under the "
                f"{inr(detail.get('ctr_threshold_inr'))} cash reporting threshold, spread over "
                f"{len(detail.get('accounts', []))} accounts within {detail.get('window_days')} "
                f"days (window from {s['window_bucket']}). Thresholds read from POLICY_CLAUSE."
            )

    if not txns.empty:
        st.markdown("#### The deposits")
        ctr = None
        if not signals.empty:
            d0 = signals.iloc[0]["detail"]
            d0 = json.loads(d0) if isinstance(d0, str) else (d0 or {})
            ctr = d0.get("ctr_threshold_inr")
        pts = (
            alt.Chart(txns)
            .mark_circle(size=120)
            .encode(
                x=alt.X("txn_timestamp:T", title="Time"),
                y=alt.Y("amount_inr:Q", title="Amount (INR)", scale=alt.Scale(zero=False)),
                color=alt.Color("account_id:N", title="Account"),
                tooltip=["txn_id", "txn_timestamp", "account_id", "amount_inr", "channel"],
            )
        )
        layers = [pts]
        if ctr:
            rule = alt.Chart(pd.DataFrame({"y": [float(ctr)]}))
            layers.append(rule.mark_rule(strokeDash=[6, 4], color="red").encode(y="y:Q"))
        st.altair_chart(alt.layer(*layers), use_container_width=True)
        st.caption("Dashed line: the cash transaction reporting threshold.")

if not txns.empty:
    st.markdown("#### Evidence transactions")
    shown = txns.assign(amount=txns["amount_inr"].map(inr))
    st.dataframe(
        shown[
            [
                "txn_id",
                "txn_timestamp",
                "account_id",
                "amount",
                "channel",
                "txn_type",
                "branch_code",
                "source_ref",
            ]
        ],
        hide_index=True,
        use_container_width=True,
    )


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
plant = b[3].toggle(
    "Plant a defect in the next draft",
    help="Demo: inflates the deposit total by 10% and cites a clause that does not exist. "
    "Watch the Reviewer catch both.",
    disabled=filed,
)
if b[0].button("1 · Gather evidence", use_container_width=True, disabled=filed):
    run("Gathering evidence", lambda: call(PROC["gather_evidence"], case_id))
if b[1].button("2 · Draft STR", use_container_width=True, disabled=filed):
    run("Drafting the STR", lambda: call(PROC["draft_str"], case_id, bool(plant)))
can_review = latest is not None and latest["status"] == "DRAFT"
if b[2].button(
    "3 · Run Reviewer", type="primary", use_container_width=True, disabled=not can_review
):
    run("Reviewing", lambda: call(PROC["review_str"], latest["artifact_id"]))

last = st.session_state.pop("last_action", None)
if last:
    ok, label, result = last
    if ok:
        st.toast(f"{label}: done")
    else:
        st.error(f"{label} refused: {result}")

if latest is None:
    st.info("No STR drafted yet. Gather the evidence, then draft.")
else:
    if latest["status"] == "REVIEWED":
        s1, s2 = st.columns([3, 1], vertical_alignment="center")
        s1.success(
            "The Reviewer approved this STR. It now needs the Principal Officer's signature."
        )
        if s2.button("Go to sign-off", use_container_width=True):
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
        st.dataframe(hist, hide_index=True, use_container_width=True)
        for _, row in arts.iterrows():
            if row["review_decision"] == "REJECT":
                with st.expander(f"v{row['version']} -- why the Reviewer rejected it"):
                    review_result(row.to_dict())
    with t3:
        st.markdown("##### Evidence pointers")
        st.dataframe(data.evidence_rows(case_id), hide_index=True, use_container_width=True)
        st.markdown("##### Logged queries (the source of every figure)")
        st.dataframe(
            data.query_logs(case_id).drop(columns=["sql_text"]),
            hide_index=True,
            use_container_width=True,
        )
        st.caption(
            "Each query's exact text, bind value and canonical result hash are stored. "
            "The Reviewer re-runs them and compares."
        )
