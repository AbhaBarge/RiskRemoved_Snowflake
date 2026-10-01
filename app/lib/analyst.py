"""Plain-English questions via Cortex Analyst over the semantic view.

Analyst turns a question into SQL against SV_RISK_COPILOT; the app runs that
SQL and shows it. The REST endpoint is called with the session's own token,
using the HTTP client bundled with the Snowflake connector, so the app needs
no extra package (the trial account has no external access to install one).
Every question is written to AUDIT.AGENT_ACTION_LOG with the SQL it produced.
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from typing import Any

from snowflake.connector.vendored import requests

from lib.db import CFG, S, session

ENDPOINT = "/api/v2/cortex/analyst/message"


def ask(question: str, history: list[dict[str, Any]]) -> dict[str, Any]:
    """One Analyst turn. Returns {text, sql, suggestions, request_id} or {error}."""
    conn = session().connection
    messages = [*history, {"role": "user", "content": [{"type": "text", "text": question}]}]
    started = time.time()
    try:
        r = requests.post(
            f"https://{conn.host}{ENDPOINT}",
            json={"messages": messages, "semantic_view": CFG["semantic_view"]},
            headers={
                "Authorization": f'Snowflake Token="{conn.rest.token}"',
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=120,
        )
    except Exception as exc:  # network path unavailable in this runtime
        return {"error": f"Cortex Analyst could not be reached: {exc}"}
    if not r.ok:
        return {"error": f"Cortex Analyst returned {r.status_code}: {r.text[:300]}"}
    body = r.json()
    out: dict[str, Any] = {
        "text": "",
        "sql": None,
        "suggestions": [],
        "request_id": body.get("request_id"),
        "message": body.get("message"),
        "latency_ms": int((time.time() - started) * 1000),
    }
    for part in (body.get("message") or {}).get("content", []):
        if part.get("type") == "text":
            out["text"] += part.get("text", "")
        elif part.get("type") == "sql":
            out["sql"] = part.get("statement")
        elif part.get("type") == "suggestions":
            out["suggestions"] = part.get("suggestions", [])
    return out


def log_turn(question: str, result: dict[str, Any]) -> None:
    """Audit row for one question. Best effort: a logging failure must not hide the answer."""
    sql = result.get("sql")
    with contextlib.suppress(Exception):
        session().sql(
            f"""
            INSERT INTO {S["audit"]}.AGENT_ACTION_LOG (
                log_id, session_id, turn_number, agent_mode, invoked_by, snowflake_user,
                user_role, question_text, tools_invoked, sql_text, sql_hash, model_version,
                prompt_version, latency_ms, outcome, error_text
            )
            SELECT 'LOG-' || UUID_STRING(), CURRENT_SESSION()::VARCHAR,
                   (SELECT COUNT(*) + 1 FROM {S["audit"]}.AGENT_ACTION_LOG
                     WHERE session_id = CURRENT_SESSION()::VARCHAR),
                   'INVESTIGATOR', 'USER', CURRENT_USER(), CURRENT_ROLE(), ?,
                   ARRAY_CONSTRUCT('CORTEX_ANALYST'), ?, ?, 'CORTEX_ANALYST', ?, ?, ?, ?
            """,
            params=[
                question,
                sql,
                hashlib.sha256(sql.encode()).hexdigest() if sql else None,
                str(result.get("request_id") or "n/a"),
                result.get("latency_ms"),
                "ERROR" if result.get("error") else "SUCCESS",
                result.get("error"),
            ],
        ).collect()
