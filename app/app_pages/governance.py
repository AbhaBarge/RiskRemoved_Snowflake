"""Governance & audit: the governed thresholds, the rule registry, and every action logged."""

import altair as alt
import pandas as pd
import streamlit as st
from lib import data, ui
from lib.db import PROC, call, reason, variant

ui.style()
st.title("Governance & audit")
st.caption(
    "Changing a threshold is a governed data change with an audit trail, not a code change. "
    "Editing a rule's SQL without bumping its version blocks the deploy (rule_sql_hash)."
)

t1, t2, t3, t4 = st.tabs(
    ["Policy clauses in force", "Rule registry", "Action log", "Evaluation runs"]
)

with t1:
    st.dataframe(data.policy_clauses(), hide_index=True, width="stretch")
    st.caption("GOVERNANCE.V_POLICY_CLAUSE_CURRENT: the clauses effective today.")

with t2:
    r = data.rules()
    r["rule_sql_hash"] = r["rule_sql_hash"].str[:16] + "…"
    st.dataframe(r, hide_index=True, width="stretch")

with t3:
    log = data.audit_log()
    if log.empty:
        st.info(
            "No actions logged yet. Every workflow step writes a row to AUDIT.AGENT_ACTION_LOG."
        )
    elif ui.visual("gov_log"):
        tl = log.copy()
        tl["tool"] = tl["tools_invoked"].map(
            lambda v: ", ".join(variant(v) or []) if isinstance(variant(v), list) else str(v)
        )
        outcomes = {"SUCCESS": ui.TEAL, "REFUSED": ui.RED, "ERROR": ui.AMBER}
        present = [o for o in outcomes if o in set(tl["outcome"])]
        others = sorted(set(tl["outcome"]) - set(outcomes))
        chart = (
            alt.Chart(tl)
            .mark_point(filled=True, size=140, opacity=0.85)
            .encode(
                x=alt.X("logged_at:T", title=None),
                y=alt.Y("tool:N", title=None, sort="-x"),
                color=alt.Color(
                    "outcome:N",
                    title="Outcome",
                    scale=alt.Scale(
                        domain=present + others,
                        range=[outcomes[o] for o in present] + [ui.SLATE] * len(others),
                    ),
                    legend=alt.Legend(orient="bottom"),
                ),
                shape=alt.Shape("agent_mode:N", title="Actor", legend=alt.Legend(orient="bottom")),
                tooltip=[
                    alt.Tooltip("logged_at:T", format="%d %b %H:%M:%S"),
                    "tool:N",
                    "agent_mode:N",
                    "user_role:N",
                    "outcome:N",
                    "refusal_reason:N",
                    "latency_ms:Q",
                ],
            )
            .properties(height={"step": 34})
        )
        st.altair_chart(chart, width="stretch")
        st.caption(
            f"The last {len(tl)} actions: every tool call, question and reset, who ran it and "
            "how it ended. Refusals are logged too. Switch to Table for the raw rows."
        )
    else:
        st.dataframe(log, hide_index=True, width="stretch")

with t4:
    runs = data.eval_runs()
    if not runs.empty and ui.visual("gov_eval"):
        long = runs.melt(
            id_vars=["finished_at"], value_vars=["recall", "precision"], var_name="metric"
        )
        trend = (
            alt.Chart(long)
            .mark_line(point={"size": 90})
            .encode(
                x=alt.X("finished_at:T", title="Verify run"),
                y=alt.Y(
                    "value:Q", title=None, axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])
                ),
                color=alt.Color(
                    "metric:N",
                    title=None,
                    scale=alt.Scale(range=[ui.NAVY, ui.SKY]),
                    legend=alt.Legend(orient="bottom"),
                ),
                tooltip=[
                    alt.Tooltip("finished_at:T", format="%d %b %H:%M"),
                    "metric:N",
                    alt.Tooltip("value:Q", format=".0%"),
                ],
            )
            .properties(height=220)
        )
        st.altair_chart(trend, width="stretch")
    else:
        st.dataframe(runs, hide_index=True, width="stretch")
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
                width="stretch",
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
