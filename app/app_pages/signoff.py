"""Principal Officer sign-off: the human gate. Nothing is filed without it."""

import html

import pandas as pd
import streamlit as st
from lib import auth, data, ui
from lib.db import PROC, call, reason
from lib.str_view import review_result, str_document

ui.style()
st.title("Principal Officer sign-off")
st.caption(
    "PML (Maintenance of Records) Rules, Rule 7: the Principal Officer bears the filing "
    "obligation. The system cannot file on its own -- ATTEST_STR refuses unless the caller holds "
    "the compliance officer role, the Reviewer approved, and this exact version is unchanged."
)


def decision_form(aid: str, a: dict) -> None:
    """The officer's signature: a name, a decision, and an explicit confirmation."""
    officer = auth.current() or {}
    with st.form("attest", clear_on_submit=False):
        attestor = st.text_input(
            "Your name",
            value=f"{officer.get('name', '')}, {officer.get('title', '')}",
            placeholder="Principal Officer",
        )
        decision = st.radio(
            "Decision",
            ["ATTESTED", "RETURNED"],
            format_func={"ATTESTED": "Attest and file", "RETURNED": "Return for redraft"}.get,
            horizontal=True,
        )
        comment = st.text_area("Comment (optional)")
        confirm = st.checkbox(
            f"I have read this exact version (hash {a['content_hash'][:12]}…) "
            "and take responsibility for this decision."
        )
        submitted = st.form_submit_button("Sign", type="primary")
    if submitted:
        if not confirm:
            st.error("Tick the confirmation to sign.")
        else:
            try:
                result = call(PROC["attest_str"], aid, decision, attestor, comment or None)
                st.session_state["signed"] = result
                st.rerun()
            except Exception as exc:  # the gate's reason, shown verbatim
                st.error(f"Refused: {reason(exc)}")


queue = data.signoff_queue()
if queue.empty:
    st.info("No STR is awaiting sign-off. Approved drafts appear here.")
else:
    labels = {
        r.artifact_id: f"{r.artifact_id} -- {r.full_name} ({r.branch_region}), file by "
        f"{str(r.sla_deadline)[:10]}"
        for r in queue.itertuples()
    }
    aid = st.selectbox("STR awaiting attestation", list(labels), format_func=labels.get)
    due = pd.to_datetime(queue.set_index("artifact_id").loc[aid, "sla_deadline"])
    days = (due.normalize() - pd.Timestamp.now(tz=due.tz).normalize()).days
    colour = ui.RED if days <= 2 else ui.AMBER if days <= 5 else ui.TEAL
    ui.callout(
        f"{ui.chip(f'{days} day(s) left' if days >= 0 else f'{-days} day(s) overdue', colour)}"
        f" Filing deadline <b>{html.escape(str(due)[:10])}</b>: the governed number of working "
        "days after suspicion was established, read from POLICY_CLAUSE when the STR was drafted."
    )
    a = data.artifact(aid)
    if a:
        left, right = st.columns([3, 2])
        with left:
            str_document(a)
        with right:
            st.markdown("##### Reviewer's result")
            arts = data.case_artifacts(a["case_id"])
            row = arts[arts["artifact_id"] == aid]
            if not row.empty:
                review_result(row.iloc[0].to_dict())
            st.markdown("##### Your decision")
            if auth.can("can_attest"):
                decision_form(aid, a)
            else:
                st.warning(
                    "Only the Principal Officer can sign. The analyst who prepared this STR "
                    "cannot approve their own work.",
                    icon=":material/lock:",
                )
                st.button(
                    "Switch to the Principal Officer",
                    on_click=auth.sign_out,
                    args=("officer",),
                    type="primary",
                )

signed = st.session_state.pop("signed", None)
if signed:
    st.success(
        f"{signed.get('decision')}: {signed.get('artifact_id')} is now "
        f"{signed.get('artifact_status')}. Attestation {signed.get('attestation_id')} records "
        f"hash {str(signed.get('attested_content_hash'))[:12]}…"
    )

st.subheader("Attestation register")
reg = data.attestations()
if reg.empty:
    st.caption("No attestations yet.")
else:
    st.dataframe(ui.pretty(reg), hide_index=True, width="stretch")
bad = data.filing_integrity()
st.metric(
    "Filed STRs without a matching, hash-bound attestation",
    bad,
    help="Must always be 0. Checked live against ARTIFACT and ATTESTATION.",
)
if bad:
    st.error("Integrity breach: an STR is FILED without a matching attestation.")
