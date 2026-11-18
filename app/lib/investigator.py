"""Plain-English questions via the investigator agent, with conversation memory.

The agent (sql/12_investigator) sits in front of Cortex Analyst: it keeps a
thread per conversation, looks a case up when one is mentioned, and rewrites a
follow-up ("and how much of that was cash?") into a standalone question with
the party, case and dates spelled out before Analyst writes the SQL.

Like lib/analyst.py, the REST call uses the session's own token and the HTTP
client bundled with the Snowflake connector, so it runs in every place the app
does. The agent runs SQL itself; its answer carries the SQL and the result
sets, so the page shows them without running the query a second time.
"""

from __future__ import annotations

import time
from typing import Any

import pandas as pd

try:  # connector 3.x bundles requests; 5.x does not (requirements.txt lists it)
    from snowflake.connector.vendored import requests
except ImportError:  # pragma: no cover - depends on the installed connector
    import requests  # type: ignore[no-redef]

from lib.db import CFG, session

THREADS = "/api/v2/cortex/threads"


def agent_name() -> str | None:
    """The investigator's fully qualified name, or None when the Ask page should not use it."""
    agent = CFG.get("agent") or {}
    if agent.get("ask_backend", "analyst") != "agent":
        return None
    return agent.get("investigator") or None


def _headers() -> dict[str, str]:
    conn = session().connection
    return {
        "Authorization": f'Snowflake Token="{conn.rest.token}"',
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _post(path: str, body: dict[str, Any], timeout: int) -> Any:
    """POST to the account's REST API, reconnecting once if the session token expired."""
    url = f"https://{session().connection.host}{path}"
    r = requests.post(url, json=body, headers=_headers(), timeout=timeout)
    if r.status_code == 401:
        session.clear()
        r = requests.post(
            f"https://{session().connection.host}{path}",
            json=body,
            headers=_headers(),
            timeout=timeout,
        )
    return r


def new_thread() -> int | None:
    """A thread for one Ask conversation; None if the account refuses (the turn still runs)."""
    try:
        r = _post(THREADS, {"origin_application": "rc_app"}, timeout=30)
    except Exception:
        return None
    if not r.ok:
        return None
    try:
        return int(r.json().get("thread_id"))
    except (TypeError, ValueError, AttributeError):
        return None


def frame(result_set: dict[str, Any] | None) -> pd.DataFrame | None:
    """A DataFrame from a jsonv2 result_set (values arrive as text; typed by rowType)."""
    if not isinstance(result_set, dict):
        return None
    cols = (result_set.get("resultSetMetaData") or {}).get("rowType") or []
    if not cols:
        return None
    df = pd.DataFrame(
        result_set.get("data") or [], columns=[c.get("name", "").lower() for c in cols]
    )
    for c in cols:
        name, kind = c.get("name", "").lower(), (c.get("type") or "").lower()
        if kind in ("fixed", "real"):
            df[name] = pd.to_numeric(df[name], errors="coerce")
        elif kind == "boolean":
            df[name] = df[name].map(
                lambda v: None if v is None else str(v).lower() in ("1", "true")
            )
        elif kind == "date":
            df[name] = pd.to_datetime(df[name], errors="coerce").dt.date
    return df


def parse(body: dict[str, Any]) -> dict[str, Any]:
    """The parts of a non-streaming agent:run response the page renders."""
    out: dict[str, Any] = {
        "text": "",
        "sql": None,
        "sqls": [],
        "tables": [],
        "suggestions": [],
        "tools": [],
        "result_set": None,
    }
    for part in body.get("content") or []:
        kind = part.get("type")
        if kind == "text":
            # An answer can arrive as several text parts (a caption, then the prose);
            # joined bare they run together ("...region.3 of the 10 cases").
            text = (part.get("text") or "").strip()
            if text:
                out["text"] = f"{out['text']}\n\n{text}" if out["text"] else text
        elif kind == "tool_use":
            name = (part.get("tool_use") or {}).get("name")
            if name and name not in out["tools"]:
                out["tools"].append(name)
        elif kind == "tool_result":
            for block in (part.get("tool_result") or {}).get("content") or []:
                js = block.get("json") if block.get("type") == "json" else None
                if isinstance(js, dict) and isinstance(js.get("sql"), str):
                    out["sqls"].append(js["sql"])
                    out["result_set"] = js.get("result_set") or out["result_set"]
        elif kind == "table":
            table = part.get("table") or {}
            out["tables"].append(
                {"title": table.get("title"), "result_set": table.get("result_set")}
            )
        elif kind == "suggested_queries":
            out["suggestions"] = [
                s.get("query") for s in part.get("suggested_queries") or [] if s.get("query")
            ]
    out["sql"] = out["sqls"][-1] if out["sqls"] else None
    meta = body.get("metadata") or {}
    out["assistant_message_id"] = meta.get("assistant_message_id")
    out["thread_id"] = meta.get("thread_id")
    out["model"] = meta.get("orchestration_model")
    return out


def ask(question: str, thread_id: int | None, parent_message_id: int) -> dict[str, Any]:
    """One investigator turn. Returns the parsed answer, or {error}."""
    name = agent_name()
    if not name:
        return {"error": "The investigator agent is not configured for the Ask page."}
    database, schema, agent = name.split(".")
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": question}]}],
        "stream": False,
    }
    if thread_id is not None:
        body.update(thread_id=thread_id, parent_message_id=parent_message_id)
    started = time.time()
    try:
        r = _post(
            f"/api/v2/databases/{database}/schemas/{schema}/agents/{agent}:run", body, timeout=180
        )
    except Exception as exc:  # network path unavailable in this runtime
        return {"error": f"The investigator agent could not be reached: {exc}"}
    if not r.ok:
        return {"error": f"The investigator agent returned {r.status_code}: {r.text[:300]}"}
    r.encoding = "utf-8"
    out = parse(r.json())
    out["request_id"] = r.headers.get("X-Snowflake-Request-Id")
    out["latency_ms"] = int((time.time() - started) * 1000)
    if not out["text"] and not out["sql"]:
        return {"error": "The investigator agent returned no answer.", **out}
    return out
