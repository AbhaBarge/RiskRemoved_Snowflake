"""A model's triage summary of a case's evidence: an analyst aid, never evidence.

One ``AI_AGG`` call over the legs the case's signals cite (``AI_COMPLETE`` is
blocked on this trial account; ``AI_AGG`` is not). The text is shown beside the
evidence, never written into a FINDING or an STR, and the prompt forbids stating
a total: every figure in a filing still comes from SQL. Each model call writes a
row to AUDIT.AGENT_ACTION_LOG.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

import streamlit as st

from lib.db import DT, S, run

PROMPT_VERSION = "TRIAGE_V1"
MODEL = "AI_AGG"
PROMPT = (
    "You are assisting an AML analyst. In at most three sentences, describe the pattern "
    "in these deposits: timing, channel, how many accounts, and how close each amount "
    "sits to a round reporting threshold. Do not state a total."
)


@st.cache_data(ttl=3600, show_spinner=False)
def summarise(case_id: str) -> dict[str, Any]:
    """{text, latency_ms} or {error}. Cached per case, so the model runs once an hour."""
    ev = S["evidence"]
    sql = f"""
        WITH ids AS (
          SELECT DISTINCT f.value::STRING AS txn_id
          FROM {ev}.CASE_SIGNAL cs
          JOIN {ev}.SIGNAL s ON s.signal_id = cs.signal_id,
          LATERAL FLATTEN(input => s.evidence_txn_ids) f
          WHERE cs.case_id = ?
        )
        SELECT AI_AGG(
                 TO_VARCHAR(t.txn_timestamp, 'YYYY-MM-DD') || ': ' || t.channel || ' '
                 || t.txn_type || ' of INR ' || TO_VARCHAR(t.amount_inr, '999,999,990')
                 || ' into account ' || t.account_id || ' at branch ' || t.branch_code,
                 ?) AS summary,
               COUNT(*) AS legs
        FROM ids JOIN {DT["transactions_enriched"]} t ON t.txn_id = ids.txn_id
    """
    started = time.time()
    try:
        row = run(lambda s: s.sql(sql, params=[case_id, PROMPT]).collect())[0]
        out: dict[str, Any] = {"text": row[0], "legs": int(row[1])}
    except Exception as exc:  # shown as a note; the evidence above stands on its own
        out = {"error": str(exc)}
    out["latency_ms"] = int((time.time() - started) * 1000)
    _log(case_id, out)
    return out


def _log(case_id: str, result: dict[str, Any]) -> None:
    """Audit row for one model call. Best effort: a logging failure must not hide the text."""
    with contextlib.suppress(Exception):
        run(
            lambda s: s.sql(
                f"""
            INSERT INTO {S["audit"]}.AGENT_ACTION_LOG (
                log_id, session_id, turn_number, agent_mode, invoked_by, snowflake_user,
                user_role, question_text, tools_invoked, case_ids_touched, model_version,
                prompt_version, latency_ms, outcome, error_text
            )
            SELECT 'LOG-' || UUID_STRING(), CURRENT_SESSION()::VARCHAR,
                   (SELECT COUNT(*) + 1 FROM {S["audit"]}.AGENT_ACTION_LOG
                     WHERE session_id = CURRENT_SESSION()::VARCHAR),
                   'SENTINEL', 'USER', CURRENT_USER(), CURRENT_ROLE(), ?,
                   ARRAY_CONSTRUCT('AI_AGG'), ARRAY_CONSTRUCT(?), ?, ?, ?, ?, ?
            """,
                params=[
                    PROMPT,
                    case_id,
                    MODEL,
                    PROMPT_VERSION,
                    result.get("latency_ms"),
                    "ERROR" if result.get("error") else "SUCCESS",
                    result.get("error"),
                ],
            ).collect()
        )
