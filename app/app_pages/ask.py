"""Ask: plain-English questions about transactions, customers, cases and STRs."""

import streamlit as st
from lib.analyst import ask, log_turn
from lib.db import query, reason

st.title("Ask the data")
st.caption(
    "Cortex Analyst writes the SQL from a governed semantic view (SV_RISK_COPILOT). The view "
    "excludes the answer key and identity numbers. You see the SQL behind every answer."
)

EXAMPLES = [
    "Which branch region received the most cash deposits by value?",
    "How many cases are there by status?",
    "Show the 5 customers with the highest total cash deposits",
    "How many cash deposits were made each day?",
]

if "ask_history" not in st.session_state:
    st.session_state["ask_history"] = []  # Analyst message format
    st.session_state["ask_turns"] = []  # what we render

cols = st.columns(len(EXAMPLES))
picked = None
for col, q in zip(cols, EXAMPLES, strict=True):
    if col.button(q, use_container_width=True):
        picked = q
typed = st.chat_input("Ask a question about the bank's data")
question = typed or picked

if question:
    with st.spinner("Cortex Analyst is writing the SQL..."):
        result = ask(question, st.session_state["ask_history"])
    log_turn(question, result)
    turn = {"q": question, **result}
    if result.get("sql"):
        try:
            turn["df"] = query(result["sql"])
        except Exception as exc:  # Analyst SQL that fails to run is shown, not hidden
            turn["run_error"] = reason(exc)
    st.session_state["ask_turns"].append(turn)
    if not result.get("error"):
        st.session_state["ask_history"] += [
            {"role": "user", "content": [{"type": "text", "text": question}]},
            result["message"],
        ]

for turn in reversed(st.session_state["ask_turns"]):
    with st.chat_message("user"):
        st.write(turn["q"])
    with st.chat_message("assistant"):
        if turn.get("error"):
            st.error(turn["error"])
            continue
        if turn.get("text"):
            st.write(turn["text"])
        df = turn.get("df")
        if df is not None:
            st.dataframe(df, hide_index=True, use_container_width=True)
            numeric = df.select_dtypes("number").columns.tolist()
            if len(df.columns) == 2 and len(numeric) == 1 and 1 < len(df) <= 50:
                label = next(c for c in df.columns if c not in numeric)
                st.bar_chart(df.set_index(label)[numeric[0]])
        if turn.get("run_error"):
            st.error(f"The generated SQL did not run: {turn['run_error']}")
        if turn.get("sql"):
            with st.expander("SQL written by Cortex Analyst"):
                st.code(turn["sql"], language="sql")
        for s in turn.get("suggestions") or []:
            st.caption(f"Try: {s}")
        st.caption(f"{turn.get('latency_ms', 0) / 1000:.1f}s · logged to AGENT_ACTION_LOG")

if st.session_state["ask_turns"] and st.button("Clear conversation"):
    st.session_state["ask_history"], st.session_state["ask_turns"] = [], []
    st.rerun()
