"""The Slack side of the shared Ask conversation (AUDIT.CHAT_MESSAGE).

The dashboard's Ask page reads the same table (app/lib/chat.py), so a question
asked in Slack shows up there, and the other way round. Writes go through the
data-account connection; values are bound. Best effort: a failed write is
logged and never stops the answer reaching Slack.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from risk_copilot.agentic.connections import data_conn, data_target

log = logging.getLogger(__name__)


def record_turn(
    question: str,
    answer: str,
    *,
    kind: str,
    author: str,
    thread_ref: str | None = None,
    sql: str | None = None,
    rows: int | None = None,
    case_id: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """Append one Slack turn to the shared conversation."""
    table = f"{data_target().database}.AUDIT.CHAT_MESSAGE"
    params: list[Any] = [
        "MSG-" + uuid.uuid4().hex[:16],
        thread_ref,
        author,
        question,
        answer[:16000],
        kind,
        sql,
        rows,
        case_id,
        latency_ms,
    ]
    try:
        conn = data_conn()
        try:
            conn.cursor().execute(
                f"""INSERT INTO {table} (
                        message_id, channel, thread_ref, author, question, answer_text,
                        answer_kind, sql_text, row_count, case_id, latency_ms
                    ) SELECT %s, 'SLACK', %s, %s, %s, %s, %s, %s, %s, %s, %s""",
                params,
            )
        finally:
            conn.close()
    except Exception:
        log.warning("CHAT_MESSAGE insert failed for a Slack turn", exc_info=True)
