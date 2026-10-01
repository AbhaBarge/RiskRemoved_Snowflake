"""Case investigation: who, what fired, and the transactions behind it."""

import json

import altair as alt
import pandas as pd
import streamlit as st
from lib import data
from lib.db import HERO, inr, lakh

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
