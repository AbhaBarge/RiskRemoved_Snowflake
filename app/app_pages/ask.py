"""Ask: plain-English questions about transactions, customers, cases and STRs.

Answered by the investigator agent when app_config.json says so: it keeps the
conversation's context (a thread per conversation), looks cases up, and has
Cortex Analyst write the SQL. Direct Cortex Analyst is the fallback, and the
whole page when agent.ask_backend is "analyst".
"""

import html

import pandas as pd
import streamlit as st
from lib import auth, chat, investigator, ui
from lib.analyst import ask, log_turn
from lib.db import CFG, query, reason

AGENT = investigator.agent_name()

ui.page_header(
    "Ask the data",
    (
        'The investigator agent remembers this conversation, so follow-ups like "and how much '
        'of that was cash?" keep their case and dates. Cortex Analyst writes the SQL from the '
        "governed semantic view; identity numbers are excluded, and every answer shows the SQL."
        if AGENT
        else "Cortex Analyst writes the SQL from the governed semantic view. Identity numbers "
        "are excluded, and every answer shows the SQL behind it."
    ),
    glyph="forum",
    eyebrow="Natural-language analytics",
    meta=[
        ui.pill("Investigator agent" if AGENT else "Cortex Analyst", "violet", glyph="auto_awesome"),
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
if "ask_agent_thread" not in st.session_state:
    # The investigator's thread for this conversation and its last assistant message
    # id. Separate from "ask_thread", the shared-conversation reference.
    st.session_state["ask_agent_thread"] = None
    st.session_state["ask_agent_parent"] = 0

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


def _ask_agent(q: str) -> dict | None:
    """One investigator turn as a rendered turn, or None to fall back (with the reason)."""
    with st.spinner("The investigator is resolving your question and querying the data..."):
        if st.session_state["ask_agent_thread"] is None:
            st.session_state["ask_agent_thread"] = investigator.new_thread()
        res = investigator.ask(
            q, st.session_state["ask_agent_thread"], st.session_state["ask_agent_parent"]
        )
    log_turn(
        q,
        res,
        tools=res.get("tools") or ["INVESTIGATOR_AGENT"],
        model_version=res.get("model") or AGENT or "INVESTIGATOR_AGENT",
    )
    if res.get("error"):
        st.session_state["ask_fallback_reason"] = res["error"]
        return None
    if res.get("assistant_message_id") is not None:
        st.session_state["ask_agent_parent"] = res["assistant_message_id"]
    turn = {"q": q, "backend": "agent", **res}
    frames = [
        (t.get("title"), investigator.frame(t.get("result_set"))) for t in res.get("tables", [])
    ]
    frames = [(title, df) for title, df in frames if df is not None]
    if not frames and (df := investigator.frame(res.get("result_set"))) is not None:
        frames = [(None, df)]
    if not frames and res.get("sql"):
        try:  # an answer whose result set did not come back: run the SQL it reported
            frames = [(None, query(res["sql"]))]
        except Exception as exc:
            turn["run_error"] = reason(exc)
    turn["frames"] = frames
    if st.session_state["ask_agent_thread"] is None:
        turn["notice"] = (
            "No conversation thread could be opened, so this answer has no memory of "
            "earlier turns."
        )
    return turn


def _ask_analyst(q: str, notice: str | None = None) -> dict:
    with st.spinner("Cortex Analyst is writing the SQL..."):
        result = ask(q, st.session_state["ask_history"])
    log_turn(q, result)
    turn = {"q": q, "backend": "analyst", "notice": notice, **result}
    if result.get("sql"):
        try:
            turn["frames"] = [(None, query(result["sql"]))]
        except Exception as exc:  # Analyst SQL that fails to run is shown, not hidden
            turn["run_error"] = reason(exc)
    if not result.get("error"):
        st.session_state["ask_history"] += [
            {"role": "user", "content": [{"type": "text", "text": q}]},
            result["message"],
        ]
    return turn


if question:
    turn = _ask_agent(question) if AGENT else None
    if turn is None:
        why = st.session_state.pop("ask_fallback_reason", None) or "no answer"
        turn = _ask_analyst(
            question,
            notice=(
                f"The investigator agent was unavailable ({why[:160]}); answered by Cortex "
                "Analyst directly, without the agent's case lookup."
            )
            if AGENT
            else None,
        )
    st.session_state["ask_turns"].append(turn)
    frames = turn.get("frames") or []
    user = auth.current()
    chat.record(
        question,
        turn,
        author=f"app:{user['username']}" if user else "app",
        thread_ref=st.session_state.setdefault("ask_thread", f"APP-{id(st.session_state):x}"),
        rows=len(frames[-1][1]) if frames else None,
        kind="AGENT" if turn.get("backend") == "agent" else "ANALYST",
    )

if not st.session_state["ask_turns"]:
    ui.empty(
        "Ask in plain English, or pick a question above. Answers are charted when the shape "
        "allows, with the table and SQL one click away.",
        glyph="tips_and_updates",
    )

for i, turn in enumerate(reversed(st.session_state["ask_turns"])):
    n = len(st.session_state["ask_turns"]) - i
    with st.chat_message("user", avatar=":material/person:"):
        st.write(turn["q"])
    with st.chat_message("assistant", avatar=":material/auto_awesome:"):
        if turn.get("notice"):
            st.info(turn["notice"], icon=":material/info:")
        if turn.get("error"):
            st.error(turn["error"])
            continue
        if turn.get("text"):
            if turn.get("backend") == "agent":
                st.markdown(turn["text"])  # the agent answers in markdown
            else:
                st.write(turn["text"])
        for j, (title, df) in enumerate(turn.get("frames") or []):
            if title:
                st.caption(title)
            charted = ui.visual(f"ask_view_{n}_{j}") and ui.auto_chart(df)
            if not charted:
                st.dataframe(ui.pretty(df), hide_index=True, width="stretch")
            st.caption(f"{len(df):,} row(s)")
        if turn.get("run_error"):
            st.error(f"The generated SQL did not run: {turn['run_error']}")
        sqls = turn.get("sqls") or ([turn["sql"]] if turn.get("sql") else [])
        if sqls:
            label = "SQL written by Cortex Analyst" + (
                f" ({len(sqls)} queries)" if len(sqls) > 1 else ""
            )
            with st.expander(label, icon=":material/code:"):
                for sql in sqls:
                    st.code(sql, language="sql")
        for s in turn.get("suggestions") or []:
            st.caption(f"Try: {s}")
        via = (
            f"investigator agent ({', '.join(turn.get('tools') or []) or 'no tools'})"
            if turn.get("backend") == "agent"
            else "Cortex Analyst"
        )
        meta = f"{turn.get('latency_ms', 0) / 1000:.1f}s · {via} · logged to AGENT_ACTION_LOG"
        st.caption(meta)

if st.session_state["ask_turns"] and st.button(
    "Clear conversation", icon=":material/delete_sweep:", type="tertiary"
):
    st.session_state["ask_history"], st.session_state["ask_turns"] = [], []
    st.session_state["ask_agent_thread"], st.session_state["ask_agent_parent"] = None, 0
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
