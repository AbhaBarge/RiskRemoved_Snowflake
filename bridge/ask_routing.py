"""Where a Slack question goes, and how an investigator answer is shown.

Kept free of Slack imports so it can be unit-tested: slack_bot.py builds the
Bolt app at import time, which needs live tokens.

Backends:
    pdf / str       the STR pipeline (unchanged)
    agent           RC_STR_AGENT: reasoning over a case's evidence package and draft
    investigator    the data-account agent: data questions with thread memory,
                    case lookup and Cortex Analyst (sql/12_investigator)
    analyst         direct Cortex Analyst, the fallback and the ASK_BACKEND=analyst mode
"""

from __future__ import annotations

import re
from typing import Any

from tabulate import tabulate

#: Slack section text limit is 3000 characters; stay under it.
_SECTION = 2900


def decide(
    kind: str, *, str_thread: bool, about_draft: bool, backend: str, investigator_ok: bool
) -> str:
    """The backend for one message.

    `kind` comes from slack_bot._route. A reply inside an STR thread that asks
    about the draft itself (reviewer, defects, grounds...) still goes to the STR
    agent, which holds the package; any other question goes to the investigator,
    with the thread's case as context, because it can query the data.

    Without the investigator (ASK_BACKEND=analyst, or it is not configured) this
    is main's routing exactly: every reply in an STR thread goes to the STR agent.
    """
    if kind in ("pdf", "str", "agent"):
        return kind
    use_investigator = backend == "agent" and investigator_ok
    if str_thread and (about_draft or not use_investigator):
        return "agent"
    if use_investigator:
        return "investigator"
    return "analyst"


_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_HEADING = re.compile(r"^#{1,6}\s*(.+)$", re.M)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def to_slack_mrkdwn(md: str) -> str:
    """The agent writes Markdown; Slack's mrkdwn uses *bold* and <url|text>."""
    text = _BOLD.sub(r"*\1*", md or "")
    text = _HEADING.sub(r"*\1*", text)
    return _LINK.sub(r"<\2|\1>", text)


def _table(columns: list[dict[str, Any]], rows: list[list[Any]], limit: int = 20) -> str:
    headers = [c.get("name") or "" for c in columns]
    body = rows[:limit]
    text = tabulate(body, headers=headers, tablefmt="simple", maxcolwidths=30)
    if len(rows) > limit:
        text += f"\n... {len(rows) - limit} more row(s)"
    if len(text) > _SECTION - 10:
        text = text[: _SECTION - 30] + "\n... (truncated)"
    return text


def investigator_blocks(result: Any, *, agent_name: str) -> list[dict[str, Any]]:
    """Block Kit for one investigator turn: answer, last query's table, its SQL, a footer."""
    answer = to_slack_mrkdwn(result.text) or (
        f":x: {result.error}" if result.error else "The agent returned no answer."
    )
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Risk Copilot", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": answer[:_SECTION]}},
    ]
    queries = result.analyst_queries
    if queries:
        last = queries[-1]
        if last["rows"]:
            table = _table(last["columns"], last["rows"])
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": f"```\n{table}\n```"}}
            )
        sql = last["sql"] if len(last["sql"]) < 2500 else last["sql"][:2500] + "\n-- truncated"
        label = "SQL run by Cortex Analyst"
        if len(queries) > 1:
            label += f" (last of {len(queries)})"
        blocks += [
            {"type": "divider"},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f":mag: *{label}:*"}]},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```\n{sql}\n```"}},
        ]
    names = [c.get("name") for c in result.tool_calls if c.get("name")]
    tools = ", ".join(dict.fromkeys(names)) or "none"
    footer = f"_{agent_name} | tools: {tools} | {result.latency_ms}ms | reply here to follow up_"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks
