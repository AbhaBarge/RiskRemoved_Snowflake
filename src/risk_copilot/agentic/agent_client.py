"""Cortex Agent REST client for RC_STR_AGENT in the agent account.

Auth is a key-pair JWT for the agent account's service user, the documented
non-interactive method for the Cortex REST APIs. agent:run streams Server-Sent
Events; this client reads them to the end and returns the assembled answer
plus every tool call and result, so callers can show what the agent did.

Threads keep follow-up questions in context: create_thread() once per Slack
thread, then pass thread_id and the last assistant message id each turn.

The investigator agent (sql/12_investigator) lives next to the data, not in the
agent account: run_investigator() and create_investigator_thread() pass its
AgentEndpoint. Calls without an endpoint are the STR agent's, unchanged.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from risk_copilot.agentic.connections import (
    AgentEndpoint,
    _key_path,
    agent_target,
    investigator_endpoint,
    load_private_key,
)

_TOKEN: dict[str, Any] = {"value": None, "exp": 0.0}
#: JWTs for explicit endpoints, one per account|user (the STR agent keeps _TOKEN).
_EP_TOKENS: dict[str, dict[str, Any]] = {}


def _account() -> str:
    return os.environ["AGENT_SNOWFLAKE_ACCOUNT"]


def _base_url() -> str:
    return f"https://{_account().lower()}.snowflakecomputing.com"


def _jwt() -> str:
    """A key-pair JWT, cached until five minutes before it expires."""
    if _TOKEN["value"] and time.time() < _TOKEN["exp"] - 300:
        return str(_TOKEN["value"])
    key = serialization.load_pem_private_key(
        _key_path("AGENT_PRIVATE_KEY_PATH", "keys/rc_agent_svc.p8").read_bytes(),
        password=(os.getenv("AGENT_PRIVATE_KEY_PASSPHRASE") or "").encode() or None,
    )
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("AGENT_PRIVATE_KEY_PATH must hold an RSA key (Snowflake key-pair auth)")
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()
    account = _account().upper()
    user = os.getenv("AGENT_SNOWFLAKE_USER", "RC_AGENT_SVC").upper()
    now = int(time.time())
    exp = now + 3600
    _TOKEN["value"] = jwt.encode(
        {"iss": f"{account}.{user}.{fp}", "sub": f"{account}.{user}", "iat": now, "exp": exp},
        key,
        algorithm="RS256",
    )
    _TOKEN["exp"] = float(exp)
    return str(_TOKEN["value"])


def _headers(stream: bool = False) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_jwt()}",
        "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }


def _ep_jwt(ep: AgentEndpoint) -> str:
    """A key-pair JWT for an explicit endpoint's user, cached like _jwt()."""
    cached = _EP_TOKENS.get(ep.cache_key)
    if cached and time.time() < cached["exp"] - 300:
        return str(cached["value"])
    key = load_private_key(ep.key_var, ep.passphrase_var, ep.key_default)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError(f"{ep.key_var} must hold an RSA key (Snowflake key-pair auth)")
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()
    account, user = ep.account.upper(), ep.user.upper()
    now = int(time.time())
    exp = now + 3600
    value = jwt.encode(
        {"iss": f"{account}.{user}.{fp}", "sub": f"{account}.{user}", "iat": now, "exp": exp},
        key,
        algorithm="RS256",
    )
    _EP_TOKENS[ep.cache_key] = {"value": value, "exp": float(exp)}
    return str(value)


def _url_for(endpoint: AgentEndpoint | None) -> str:
    if endpoint is None:
        return _base_url()
    return f"https://{endpoint.account.lower()}.snowflakecomputing.com"


def _headers_for(endpoint: AgentEndpoint | None, stream: bool = False) -> dict[str, str]:
    if endpoint is None:
        return _headers(stream)
    headers = {
        "Authorization": f"Bearer {_ep_jwt(endpoint)}",
        "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if endpoint.role:
        headers["X-Snowflake-Role"] = endpoint.role
    return headers


@dataclass
class AgentResult:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    request_id: str | None = None
    thread_id: int | None = None
    assistant_message_id: int | None = None
    error: str | None = None
    latency_ms: int = 0

    def result_of(self, tool_name: str) -> list[dict[str, Any]]:
        """Parsed JSON results of every call to `tool_name`, in order."""
        return [
            r["result"] for r in self.tool_results if r.get("name") == tool_name and r.get("result")
        ]

    @property
    def analyst_sql(self) -> list[str]:
        """Every SQL statement Cortex Analyst ran this turn, in order."""
        return [q["sql"] for q in self.analyst_queries]

    @property
    def analyst_queries(self) -> list[dict[str, Any]]:
        """Each SQL run by the agent's Analyst tool, with its rows.

        Agentic Analyst runs SQL through the built-in system_execute_sql tool,
        whose result carries the compiled SQL and the result_set, so callers can
        show the table without running the query a second time.
        """
        out: list[dict[str, Any]] = []
        for r in self.tool_results:
            res = r.get("result")
            if not isinstance(res, dict) or not isinstance(res.get("sql"), str):
                continue
            cols, rows = _result_set(res.get("result_set"))
            out.append(
                {"sql": res["sql"], "columns": cols, "rows": rows, "query_id": res.get("query_id")}
            )
        return out


def _result_set(rs: Any) -> tuple[list[dict[str, Any]], list[list[Any]]]:
    """(column specs, rows) from a jsonv2 result_set; values typed by rowType."""
    if not isinstance(rs, dict):
        return [], []
    cols = [
        {"name": c.get("name"), "type": c.get("type"), "scale": c.get("scale") or 0}
        for c in (rs.get("resultSetMetaData") or {}).get("rowType", [])
    ]
    rows = [
        [_typed(v, cols[i] if i < len(cols) else {}) for i, v in enumerate(raw)]
        for raw in rs.get("data") or []
    ]
    return cols, rows


def _typed(value: Any, col: dict[str, Any]) -> Any:
    """jsonv2 sends every value as text; numbers come back as numbers."""
    if value is None:
        return None
    kind = (col.get("type") or "").lower()
    try:
        if kind == "fixed":
            return int(value) if not col.get("scale") else float(value)
        if kind == "real":
            return float(value)
        if kind == "boolean":
            return str(value).lower() in ("1", "true")
    except (TypeError, ValueError):
        return value
    return value


def create_thread(
    origin: str = "risk_copilot", endpoint: AgentEndpoint | None = None
) -> int | None:
    r = requests.post(
        f"{_url_for(endpoint)}/api/v2/cortex/threads",
        headers=_headers_for(endpoint),
        json={"origin_application": origin},
        timeout=30,
    )
    if not r.ok:
        return None
    body = r.json() if r.text.strip().startswith(("{", "[")) else r.text.strip()
    if isinstance(body, dict):
        return body.get("thread_id") or body.get("id")
    try:
        return int(body)
    except (TypeError, ValueError):
        return None


def _tool_payload(content: Any) -> Any:
    """Tool results arrive as content blocks; the procedure's VARIANT is JSON text inside."""
    for block in content or []:
        if block.get("type") == "json":
            js = block.get("json")
            if isinstance(js, dict) and set(js) == {"result"}:
                js = js["result"]
            if isinstance(js, str):
                with contextlib.suppress(ValueError):
                    js = json.loads(js)
            return js
        if block.get("type") == "text":
            try:
                return json.loads(block.get("text", ""))
            except ValueError:
                return block.get("text")
    return None


def run_agent(
    message: str,
    *,
    thread_id: int | None = None,
    parent_message_id: int = 0,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    timeout: int = 300,
    endpoint: AgentEndpoint | None = None,
) -> AgentResult:
    """One agent:run turn. `on_event(kind, data)` sees each SSE event as it arrives.

    Without `endpoint` this is RC_STR_AGENT in the agent account.
    """
    t = endpoint.target if endpoint is not None else agent_target()
    url = (
        f"{_url_for(endpoint)}/api/v2/databases/{t.database}/schemas/{t.schema}"
        f"/agents/{t.agent}:run"
    )
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": message}]}]
    }
    if thread_id is not None:
        body.update(thread_id=thread_id, parent_message_id=parent_message_id)
    out = AgentResult(thread_id=thread_id)
    started = time.time()
    headers = _headers_for(endpoint, stream=True)
    try:
        with requests.post(url, headers=headers, json=body, stream=True, timeout=timeout) as r:
            out.request_id = r.headers.get("X-Snowflake-Request-Id")
            if not r.ok:
                out.error = f"agent:run returned {r.status_code}: {r.text[:500]}"
                return out
            _consume(r, out, on_event)
    except requests.RequestException as exc:
        out.error = f"agent:run failed: {exc}"
    out.latency_ms = int((time.time() - started) * 1000)
    return out


def run_investigator(
    question: str,
    *,
    thread_id: int | None = None,
    parent_message_id: int = 0,
    case_id: str | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    timeout: int = 180,
) -> AgentResult:
    """One turn with the investigator agent in the data account.

    `case_id` is the case the conversation is anchored to (a Slack STR thread),
    if any. It is stated as context, not as a filter: the agent still decides
    whether the question is about that case or about the whole bank.
    """
    message = question
    if case_id:
        message = f"(Conversation context: this thread is about case {case_id}.)\n{question}"
    return run_agent(
        message,
        thread_id=thread_id,
        parent_message_id=parent_message_id,
        on_event=on_event,
        timeout=timeout,
        endpoint=investigator_endpoint(),
    )


def create_investigator_thread(origin: str = "rc_investigator") -> int | None:
    """A thread in the data account, for the investigator's multi-turn memory.

    Keep `origin` short: a 25-character origin_application was rejected where a
    short one was accepted.
    """
    return create_thread(origin, endpoint=investigator_endpoint())


def _consume(
    r: requests.Response,
    out: AgentResult,
    on_event: Callable[[str, dict[str, Any]], None] | None,
) -> None:
    # Server-Sent Events are UTF-8 by definition. Without a charset on the
    # Content-Type, requests would decode as ISO-8859-1 and mangle the rupee sign.
    r.encoding = "utf-8"
    event: str | None = None
    data_lines: list[str] = []
    deltas: list[str] = []
    final_text: str | None = None
    names: dict[str, str] = {}

    def dispatch() -> None:
        nonlocal final_text
        if not data_lines:
            return
        raw = "\n".join(data_lines)
        try:
            data = json.loads(raw)
        except ValueError:
            return
        kind = event or data.get("event") or ""
        if on_event:
            on_event(kind, data)
        if kind == "response.text.delta":
            deltas.append(data.get("text", ""))
        elif kind == "response.text":
            final_text = (final_text or "") + data.get("text", "")
        elif kind == "response.tool_use":
            names[data.get("tool_use_id", "")] = data.get("name")
            out.tool_calls.append({"name": data.get("name"), "input": data.get("input")})
        elif kind == "response.tool_result":
            out.tool_results.append(
                {
                    "name": data.get("name") or names.get(data.get("tool_use_id", "")),
                    "status": data.get("status"),
                    "result": _tool_payload(data.get("content")),
                }
            )
        elif kind == "metadata":
            meta = data.get("metadata", data)
            if meta.get("role") == "assistant" and meta.get("message_id") is not None:
                out.assistant_message_id = meta["message_id"]
        elif kind == "error":
            out.error = data.get("message") or json.dumps(data)[:500]
        elif kind == "response" and not (final_text or deltas):
            for part in data.get("content", []):
                if part.get("type") == "text":
                    final_text = (final_text or "") + part.get("text", "")

    for line in r.iter_lines(decode_unicode=True):
        line = str(line)
        if line == "":
            dispatch()
            event, data_lines = None, []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    dispatch()
    out.text = final_text if final_text is not None else "".join(deltas)
