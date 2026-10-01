"""Governance & audit: the governed thresholds, the rule registry, and every action logged."""

import streamlit as st
from lib import data

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
