"""Ask: plain-English questions about transactions, customers, cases and STRs."""

import html

import pandas as pd
import streamlit as st
from lib import auth, chat, ui
from lib.analyst import ask, log_turn
from lib.db import CFG, query, reason

ui.page_header(
    "Ask the data",
    "Cortex Analyst writes the SQL from the governed semantic view. Identity numbers are "
    "excluded, and every answer shows the SQL behind it.",
    glyph="forum",
    eyebrow="Natural-language analytics",
    meta=[
        ui.pill("Cortex Analyst", "violet", glyph="auto_awesome"),
        ui.pill(CFG["semantic_view"].split(".")[-1], "slate", glyph="schema"),
    ],
)

EXAMPLES = [
    ("Which branch region received the most cash deposits by value?", "location_on", "navy"),
    ("How many cases are there by status?", "folder_managed", "rose"),
    ("Show the 5 customers with the highest total cash deposits", "leaderboard", "emerald"),
    ("How many cash deposits were made each day?", "calendar_month", "amber"),
]

if "ask_history" not in st.session_state:
    st.session_state["ask_history"] = []  # Analyst message format
    st.session_state["ask_turns"] = []  # what we render

ui.section("Try one", "each question runs live against the semantic view")
cols = st.columns(len(EXAMPLES), gap="small")
picked = None
for i, (col, (q, glyph, tone)) in enumerate(zip(cols, EXAMPLES, strict=True)):
    with col, ui.card(f"ask-{i}", glyph=glyph, tone=tone):
        st.markdown(
            f'<div class="rc-qcard">{ui.icon(glyph)} &nbsp;{html.escape(q)}</div>',
            unsafe_allow_html=True,
        )
        if st.button("Ask", key=f"ex_{q}", icon=":material/arrow_forward:", width="stretch"):
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
    user = auth.current()
    chat.record(
        question,
        result,
        author=f"app:{user['username']}" if user else "app",
        thread_ref=st.session_state.setdefault("ask_thread", f"APP-{id(st.session_state):x}"),
        rows=len(turn["df"]) if turn.get("df") is not None else None,
    )
    if not result.get("error"):
        st.session_state["ask_history"] += [
            {"role": "user", "content": [{"type": "text", "text": question}]},
            result["message"],
        ]

if not st.session_state["ask_turns"]:
    ui.empty(
        "Ask in plain English, or pick a question above. Answers are charted when the shape "
        "allows, with the table and SQL one click away.",
        glyph="tips_and_updates",
    )

for i, turn in enumerate(reversed(st.session_state["ask_turns"])):
    with st.chat_message("user", avatar=":material/person:"):
        st.write(turn["q"])
    with st.chat_message("assistant", avatar=":material/auto_awesome:"):
        if turn.get("error"):
            st.error(turn["error"])
            continue
        if turn.get("text"):
            st.write(turn["text"])
        df = turn.get("df")
        if df is not None:
            n = len(st.session_state["ask_turns"]) - i
            charted = ui.visual(f"ask_view_{n}") and ui.auto_chart(df)
            if not charted:
                st.dataframe(ui.pretty(df), hide_index=True, width="stretch")
            st.caption(f"{len(df):,} row(s)")
        if turn.get("run_error"):
            st.error(f"The generated SQL did not run: {turn['run_error']}")
        if turn.get("sql"):
            with st.expander("SQL written by Cortex Analyst", icon=":material/code:"):
                st.code(turn["sql"], language="sql")
        for s in turn.get("suggestions") or []:
            st.caption(f"Try: {s}")
        st.caption(f"{turn.get('latency_ms', 0) / 1000:.1f}s · logged to AGENT_ACTION_LOG")

if st.session_state["ask_turns"] and st.button(
    "Clear conversation", icon=":material/delete_sweep:", type="tertiary"
):
    st.session_state["ask_history"], st.session_state["ask_turns"] = [], []
    st.rerun()

# One conversation across channels: every turn asked here or in Slack.
shared = chat.feed()
ui.section(
    "Shared conversation",
    "every question asked here or to the Slack bot, newest first (AUDIT.CHAT_MESSAGE)",
)
if shared.empty:
    ui.empty(
        "No questions yet. Ask one above, or mention the Risk Copilot bot in Slack: both "
        "channels write to the same feed.",
        glyph="forum",
    )
else:
    counts = shared["channel"].value_counts()
    labels = {"APP": "the app", "SLACK": "Slack"}
    st.caption(" · ".join(f"{n} from {labels.get(ch, ch)}" for ch, n in counts.items()))
    for row in shared.itertuples():
        when = pd.to_datetime(row.asked_at)
        badge = ":material/chat: Slack" if row.channel == "SLACK" else ":material/web: App"
        with st.container(border=True):
            st.markdown(
                f"**{html.escape(str(row.question))}**  \n"
                f"<span style='opacity:.7'>{badge} · {html.escape(str(row.author))} · "
                f"{when:%d %b %H:%M}</span>",
                unsafe_allow_html=True,
            )
            if row.answer_text:
                st.write(str(row.answer_text)[:600])
            if row.sql_text:
                with st.expander("SQL", icon=":material/code:"):
                    st.code(str(row.sql_text), language="sql")
