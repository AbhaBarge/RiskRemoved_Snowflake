"""The shared Ask conversation: one AUDIT.CHAT_MESSAGE feed for the app and Slack.

The Ask page and the Slack bot used to keep separate histories, so a question
asked in one never appeared in the other. Both now append each turn here and
the Ask page reads the feed back, labelled by channel. Recording is best
effort: a failed write must never hide an answer from the person who asked.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

import pandas as pd

from lib.db import S, cached, run

_TABLE = f"{S['audit']}.CHAT_MESSAGE"


def record(
    question: str,
    result: dict[str, Any],
    *,
    author: str,
    thread_ref: str | None,
    rows: int | None = None,
    kind: str = "ANALYST",
) -> None:
    """Append one app turn. Values are bound; nothing the user typed reaches the SQL text.

    `kind` is the answer_kind of a successful turn: ANALYST, or AGENT for the
    investigator. A turn with an error is recorded as ERROR either way.
    """
    with contextlib.suppress(Exception):
        run(
            lambda s: s.sql(
                f"""
                INSERT INTO {_TABLE} (
                    message_id, channel, thread_ref, author, question, answer_text,
                    answer_kind, sql_text, row_count, latency_ms
                )
                SELECT ?, 'APP', ?, ?, ?, ?, ?, ?, ?, ?
                """,
                params=[
                    "MSG-" + uuid.uuid4().hex[:16],
                    thread_ref,
                    author,
                    question,
                    result.get("error") or result.get("text") or "",
                    "ERROR" if result.get("error") else kind,
                    result.get("sql"),
                    rows,
                    result.get("latency_ms"),
                ],
            ).collect()
        )


def feed(limit: int = 30) -> pd.DataFrame:
    """The latest turns from every channel, newest first."""
    try:
        return cached(
            f"""
            SELECT channel, author, question, answer_text, answer_kind, sql_text, row_count,
                   case_id, latency_ms, asked_at
            FROM {_TABLE} ORDER BY asked_at DESC LIMIT ?
            """,
            (limit,),
        )
    except Exception:  # the table is not deployed yet on this account
        return pd.DataFrame()
