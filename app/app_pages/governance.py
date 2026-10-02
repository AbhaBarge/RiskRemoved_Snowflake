"""Governance & audit: the governed thresholds, the rule registry, and every action logged."""

import pandas as pd
import streamlit as st
from lib import data
from lib.db import PROC, call, reason

st.title("Governance & audit")
st.caption(
    "Changing a threshold is a governed data change with an audit trail, not a code change. "
    "Editing a rule's SQL without bumping its version blocks the deploy (rule_sql_hash)."
)

t1, t2, t3, t4 = st.tabs(
    ["Policy clauses in force", "Rule registry", "Action log", "Evaluation runs"]
)

with t1:
    st.dataframe(data.policy_clauses(), hide_index=True, use_container_width=True)
    st.caption("GOVERNANCE.V_POLICY_CLAUSE_CURRENT: the clauses effective today.")

with t2:
    r = data.rules()
    r["rule_sql_hash"] = r["rule_sql_hash"].str[:16] + "…"
    st.dataframe(r, hide_index=True, use_container_width=True)

with t3:
    log = data.audit_log()
    if log.empty:
        st.info(
            "No actions logged yet. Every workflow step writes a row to AUDIT.AGENT_ACTION_LOG."
        )
    else:
        st.dataframe(log, hide_index=True, use_container_width=True)

with t4:
    st.dataframe(data.eval_runs(), hide_index=True, use_container_width=True)
    st.caption(
        "Detection scored against the hidden answer key, in SQL, by `riskcopilot verify`. "
        "The app reads only these aggregate results, never a label."
    )

# Demo reset: one CALL to RESET_CASES, the same procedure the CLI uses.
done = st.session_state.pop("reset_done", None)
with st.expander("Demo controls: reset every case", expanded=bool(done)):
    st.caption(
        "Puts all cases back to OPEN and deletes their evidence, drafts, reviews and sign-offs, "
        "so the demo can be run again. Signals are kept, and so is the action log: the reset "
        "adds its own row to it. A demo tool only -- a real evidence chain is never deleted."
    )
    if done:
        n = done["cases_reopened"]
        st.success(
            f"Reset: {sum(done['rows'].values()):,} workflow rows deleted, "
            f"{n} case{'' if n == 1 else 's'} reopened."
        )
    preview = data.reset_preview()
    to_delete = preview["rows"]
    if not sum(to_delete.values()) and not preview["cases_reopened"]:
        st.info("Nothing to reset: every case is OPEN, with no evidence, drafts or sign-offs.")
    else:
        left, right = st.columns([3, 2])
        with left:
            st.dataframe(
                pd.DataFrame(
                    {"table": list(to_delete), "rows to delete": list(to_delete.values())}
                ),
                hide_index=True,
                use_container_width=True,
            )
        with right:
            st.metric("Cases to reopen", preview["cases_reopened"])
            with st.form("reset_cases"):
                typed = st.text_input("Type RESET to confirm")
                go = st.form_submit_button("Reset all cases", type="primary")
        if go:
            if typed.strip() != "RESET":
                st.error("Type RESET to confirm.")
            else:
                try:
                    st.session_state["reset_done"] = call(PROC["reset_cases"], "APP", False)
                    st.session_state.pop("case_id", None)
                    st.rerun()
                except Exception as exc:  # the procedure's own message, verbatim
                    st.error(f"Reset refused: {reason(exc)}")
