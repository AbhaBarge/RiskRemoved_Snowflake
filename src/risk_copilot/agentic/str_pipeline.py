"""generate_ai_str(case_id): one STR, end to end, across the two accounts.

    data account (teammate)              agent account (RC_STR_AGENT)
    -----------------------              ----------------------------
 1  current draft, or DRAFT_STR    -->
 2  artifact, query logs, signals  -->
 3  Cortex Analyst context         -->   4  EVIDENCE_PACKAGE (hashed)
                                         5  agent: draft -> review (-> redraft)
                                         6  AI_DRAFT + AI_REVIEW (source of truth)
 7  SUBMIT_AI_DRAFT + REVIEW_STR   <--      only if the agent review approved
                                         8  RENDER_STR_PDF -> PDF bytes
                                         9  AGENT_RUN_LOG

Nothing is written back unless the agent account's checker approved, and the
data account's own deterministic REVIEW_STR then re-checks the written-back
version before it can reach the Principal Officer. Used by the Slack bot and
the Streamlit case page; also runnable as
    python -m risk_copilot.agentic.str_pipeline CASE-... [--out report.pdf]
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from risk_copilot.agentic.agent_client import AgentResult, run_agent
from risk_copilot.agentic.connections import agent_conn, agent_target, data_conn, data_target

Progress = Callable[[str, str], None]  # (step, message)

ANALYST_QUESTIONS = (
    "For customer {party_id}, show the total amount and number of transactions by channel and transaction type.",
    "How many cases and signals are there for customer {party_id}, by status and rule?",
)


def _q(cur, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    cur.execute(sql, params)
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _call(cur, sql: str, params: tuple | list = ()) -> Any:
    cur.execute(sql, params)
    v = cur.fetchone()[0]
    return json.loads(v) if isinstance(v, str) else v


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def _mask_pan(pan: str | None) -> str | None:
    return (pan[:4] + "****" + pan[-2:]) if pan and len(pan) >= 6 else pan


# ---------------------------------------------------------------------------
# Data account
# ---------------------------------------------------------------------------
def _current_artifact(cur, ev: str, case_id: str) -> dict | None:
    rows = _q(cur, f"""
        SELECT a.artifact_id, a.status, f.version, f.author_model
        FROM {ev}.ARTIFACT a JOIN {ev}.FINDING f ON f.finding_id = a.finding_id
        WHERE f.case_id = %s ORDER BY f.version DESC LIMIT 1""", (case_id,))
    return rows[0] if rows else None


def _baseline(cur, case_id: str) -> dict:
    """The newest DRAFT/REVIEWED artifact, or a fresh deterministic DRAFT_STR."""
    t = data_target()
    cur_art = _current_artifact(cur, t.evidence, case_id)
    if cur_art and cur_art["status"] == "FILED":
        raise RuntimeError(f"{case_id} already has a filed STR ({cur_art['artifact_id']})")
    if cur_art and cur_art["status"] in ("DRAFT", "REVIEWED"):
        return {**cur_art, "created": False}
    res = _call(cur, f"CALL {t.proc('DRAFT_STR')}(%s, FALSE)", (case_id,))
    return {"artifact_id": res["artifact_id"], "status": "DRAFT", "version": res["version"],
            "author_model": "TEMPLATE_V1", "created": True}


def _evidence(cur, case_id: str, artifact_id: str) -> dict:
    ev = data_target().evidence
    art = _q(cur, f"SELECT content, content_hash, finding_id FROM {ev}.ARTIFACT WHERE artifact_id = %s",
             (artifact_id,))[0]
    content = json.loads(art["content"])
    logs = _q(cur, f"""
        SELECT q.query_log_id, q.bind_values:name::VARCHAR AS name, q.result_hash, q.result_row_count AS row_count,
               q.executed_at, q.result_snapshot
        FROM {ev}.EVIDENCE_QUERY_LOG q JOIN {ev}.EVIDENCE e ON e.evidence_id = q.evidence_id
        WHERE e.case_id = %s ORDER BY q.query_log_id""", (case_id,))
    clauses = next((json.loads(lg["result_snapshot"]) for lg in logs if lg["name"] == "clauses_in_force"), [])
    signals = _q(cur, f"""
        SELECT s.signal_id, s.rule_id, s.rule_version, s.severity, s.window_bucket, s.fired_at,
               s.metric_value, s.threshold_value, s.detail
        FROM {ev}.SIGNAL s JOIN {ev}.CASE_SIGNAL cs ON cs.signal_id = s.signal_id
        WHERE cs.case_id = %s ORDER BY s.fired_at""", (case_id,))
    for s in signals:
        s["detail"] = json.loads(s["detail"]) if isinstance(s["detail"], str) else s["detail"]
    case = _q(cur, f"""SELECT case_id, party_id, case_type, status, branch_region, opened_at
                       FROM {ev}.CASE_RECORD WHERE case_id = %s""", (case_id,))[0]
    subject = dict(content.get("subject") or {})
    subject["PAN_NUMBER"] = _mask_pan(subject.get("PAN_NUMBER"))
    return {
        "case": {**case, "suspicion_established_at": content.get("suspicion_established_at"),
                 "filing_deadline": content.get("filing_deadline"), "typology_id": content.get("typology_id")},
        "subject": subject,
        "computed_values": content.get("computed_values", {}),
        "transactions": content.get("transactions", []),
        "signals": signals,
        "clauses": clauses,
        "cited_clause_ids": content.get("cited_clause_ids", []),
        "query_logs": [{k: lg[k] for k in ("query_log_id", "name", "result_hash", "row_count", "executed_at")}
                       for lg in logs],
        "baseline": {"artifact_id": artifact_id, "finding_id": art["finding_id"], "content_hash": art["content_hash"],
                     "narrative_author": content.get("narrative_author"),
                     "grounds_of_suspicion": content.get("grounds_of_suspicion")},
    }


def _analyst(conn, question: str) -> dict:
    """One Cortex Analyst turn in the data account, with its SQL run (20 rows max)."""
    from snowflake.connector.vendored import requests

    r = requests.post(
        f"https://{conn.host}/api/v2/cortex/analyst/message",
        json={"messages": [{"role": "user", "content": [{"type": "text", "text": question}]}],
              "semantic_view": data_target().semantic_view},
        headers={"Authorization": f'Snowflake Token="{conn.rest.token}"',
                 "Content-Type": "application/json", "Accept": "application/json"},
        timeout=90,
    )
    if not r.ok:
        return {"question": question, "error": f"{r.status_code}: {r.text[:200]}"}
    text, sql = "", None
    for part in (r.json().get("message") or {}).get("content", []):
        if part.get("type") == "text":
            text += part.get("text", "")
        elif part.get("type") == "sql":
            sql = part.get("statement")
    rows: list[dict] = []
    if sql:
        try:
            cur = conn.cursor()
            cur.execute(sql)
            cols = [d[0].lower() for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchmany(20)]
        except Exception as exc:  # context only: a failed query must not stop the STR
            text += f" (query failed: {str(exc)[:120]})"
    return {"question": question, "text": text, "sql": sql, "rows": rows, "source": "cortex_analyst"}


# ---------------------------------------------------------------------------
# Agent account
# ---------------------------------------------------------------------------
def _store_package(acur, payload: dict, case_id: str, base: dict) -> tuple[str, str]:
    t = data_target()
    canon = _canon(payload)
    digest = hashlib.sha256(canon.encode()).hexdigest()
    package_id = "PKG-" + hashlib.sha256(f"{case_id}|{digest}|{time.time()}".encode()).hexdigest()[:16]
    acur.execute(
        f"""INSERT INTO {agent_target().schema_fq}.EVIDENCE_PACKAGE
            (package_id, case_id, source_account, source_database, source_artifact_id,
             source_content_hash, payload, payload_hash)
            SELECT %s, %s, %s, %s, %s, %s, PARSE_JSON(%s), %s""",
        (package_id, case_id, payload["source"]["account"], t.database, base["artifact_id"],
         payload["baseline"]["content_hash"], canon, digest),
    )
    return package_id, digest


def _latest_draft(acur, package_id: str) -> dict | None:
    rows = _q(acur, f"""
        SELECT d.draft_id, d.attempt, d.status, d.model, d.prompt_version, d.sections, d.error,
               r.review_id, r.decision, r.defects, r.numeric_check, r.llm_verdict, r.reviewer_model
        FROM {agent_target().schema_fq}.AI_DRAFT d
        LEFT JOIN {agent_target().schema_fq}.AI_REVIEW r ON r.draft_id = d.draft_id
        WHERE d.package_id = %s ORDER BY d.attempt DESC, r.created_at DESC NULLS LAST LIMIT 1""",
        (package_id,))
    if not rows:
        return None
    row = rows[0]
    for k in ("sections", "defects", "numeric_check", "llm_verdict"):
        if isinstance(row.get(k), str):
            row[k] = json.loads(row[k])
    return row


def _direct_tools(acur, package_id: str) -> None:
    """Fallback when the agent run fails: the same tools, called in the agent's order."""
    sch = agent_target().schema_fq
    d = _call(acur, f"CALL {sch}.DRAFT_STR_NARRATIVE(%s)", (package_id,))
    if d.get("status") != "DRAFTED":
        return
    r = _call(acur, f"CALL {sch}.REVIEW_STR_DRAFT(%s)", (d["draft_id"],))
    if r.get("decision") == "REJECT":
        d2 = _call(acur, f"CALL {sch}.DRAFT_STR_NARRATIVE(%s, %s)", (package_id, "\n".join(r.get("defects", []))))
        if d2.get("status") == "DRAFTED":
            _call(acur, f"CALL {sch}.REVIEW_STR_DRAFT(%s)", (d2["draft_id"],))


def _fetch_pdf(acur, stage_path: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        acur.execute(f"GET '{stage_path}' 'file://{Path(tmp).as_posix()}/'")
        return next(Path(tmp).glob("*.pdf")).read_bytes()


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------
def generate_ai_str(
    case_id: str,
    *,
    progress: Progress | None = None,
    requested_by: str = "unknown",
    channel: str = "cli",
    include_analyst: bool = True,
) -> dict[str, Any]:
    say = progress or (lambda step, msg: None)
    run_id = "RUN-" + hashlib.sha256(f"{case_id}|{time.time()}".encode()).hexdigest()[:16]
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    steps: list[dict] = []
    out: dict[str, Any] = {"run_id": run_id, "case_id": case_id, "status": "ERROR"}

    def step(name: str, msg: str):
        steps.append({"step": name, "at": time.time()})
        say(name, msg)

    dconn = aconn = None
    try:
        dconn, aconn = data_conn(), agent_conn()
        dcur, acur = dconn.cursor(), aconn.cursor()

        step("baseline", "Locating the current STR draft in the data account")
        base = _baseline(dcur, case_id)
        out["base_artifact_id"] = base["artifact_id"]

        step("evidence", f"Collecting logged evidence for {base['artifact_id']}")
        payload = _evidence(dcur, case_id, base["artifact_id"])
        payload["source"] = {"account": os.getenv("SNOWFLAKE_ACCOUNT"), "database": data_target().database}
        payload["reporting_entity"] = {
            "bank_name": os.getenv("REPORTING_BANK_NAME", "Demo Bank Ltd."),
            "principal_officer": os.getenv("PRINCIPAL_OFFICER_NAME", "Rajesh Menon"),
        }

        payload["analyst_context"] = []
        if include_analyst:
            step("analyst", "Asking Cortex Analyst for supporting context")
            for q in ANALYST_QUESTIONS:
                payload["analyst_context"].append(_analyst(dconn, q.format(party_id=payload["case"]["party_id"])))

        step("package", "Sending the hashed evidence package to the agent account")
        package_id, digest = _store_package(acur, payload, case_id, base)
        out.update(package_id=package_id, payload_hash=digest)

        step("agent", "RC_STR_AGENT is drafting and cross-reviewing the STR")
        agent: AgentResult = run_agent(f"Prepare the STR for package {package_id} (case {case_id}).")
        out["agent_text"], out["agent_error"] = agent.text, agent.error
        out["agent_tools"] = [c.get("name") for c in agent.tool_calls]
        draft = _latest_draft(acur, package_id)
        if draft is None or draft.get("decision") is None:
            step("agent_fallback", "Agent run incomplete; calling the agent's tools directly")
            _direct_tools(acur, package_id)
            out["agent_fallback"] = True
            draft = _latest_draft(acur, package_id)
        if draft is None or draft.get("status") != "DRAFTED":
            raise RuntimeError(f"No usable draft: {(draft or {}).get('error') or agent.error}")

        sections = draft["sections"]
        ai_review = {
            "decision": draft["decision"], "reviewer_model": draft["reviewer_model"],
            "defects": draft.get("defects") or [], "numeric_check": draft.get("numeric_check") or {},
            "draft_id": draft["draft_id"], "attempt": draft["attempt"], "drafter_model": draft["model"],
            "package_id": package_id, "payload_hash": digest,
            "agent_account": os.getenv("AGENT_SNOWFLAKE_ACCOUNT", "HQRWRXK-UZ26728"),
            "agent": agent_target().agent_fq,
            "reason_codes": [r.get("code") for r in sections.get("reason_codes", [])],
        }
        out.update(draft_id=draft["draft_id"], attempt=draft["attempt"], sections=sections, ai_review=ai_review)

        target_aid = target_hash = None
        data_review = None
        if draft["decision"] == "APPROVE":
            t = data_target()
            step("write_back", "Writing the reviewed narrative back as a new STR version")
            sub = _call(dcur, f"CALL {t.proc('SUBMIT_AI_DRAFT')}(%s, %s, %s, PARSE_JSON(%s), %s)",
                        (base["artifact_id"], sections["grounds_of_suspicion"], draft["model"],
                         _canon(ai_review), draft["prompt_version"]))
            target_aid, target_hash = sub["artifact_id"], sub["content_hash"]
            step("data_review", f"Data account REVIEW_STR is re-checking {target_aid}")
            data_review = _call(dcur, f"CALL {t.proc('REVIEW_STR')}(%s)", (target_aid,))
            out.update(artifact_id=target_aid, content_hash=target_hash, data_review=data_review)
            out["status"] = "READY_FOR_ATTESTATION" if data_review.get("decision") == "APPROVE" else "REJECTED_BY_DATA_REVIEW"
        else:
            out["status"] = "REJECTED_BY_AGENT_REVIEW"

        step("pdf", "Rendering the STR PDF")
        rpt = _call(acur, f"CALL {agent_target().schema_fq}.RENDER_STR_PDF(%s, %s, %s, %s)",
                    (draft["draft_id"], target_aid, target_hash, _canon(data_review) if data_review else None))
        out.update(stage_path=rpt["stage_path"], pdf_sha256=rpt["pdf_sha256"])
        out["pdf_bytes"] = _fetch_pdf(acur, rpt["stage_path"])
        out["pdf_name"] = f"STR_{case_id}_{target_aid or draft['draft_id']}.pdf"
        step("done", out["status"])
    except Exception as exc:
        out["error"] = str(exc)[:1500]
        say("error", out["error"])
    finally:
        if aconn is not None:
            try:
                aconn.cursor().execute(
                    f"""INSERT INTO {agent_target().schema_fq}.AGENT_RUN_LOG
                        (run_id, case_id, channel, requested_by, status, steps, result, error, started_at, finished_at)
                        SELECT %s, %s, %s, %s, %s, PARSE_JSON(%s), PARSE_JSON(%s), %s, %s, SYSDATE()""",
                    (run_id, case_id, channel, requested_by, out["status"], _canon(steps),
                     _canon({k: v for k, v in out.items() if k not in ("pdf_bytes", "sections", "agent_text")}),
                     out.get("error"), started),
                )
            except Exception:
                pass
        for c in (dconn, aconn):
            if c is not None:
                c.close()
    return out


def latest_package(case_id: str) -> str | None:
    """The newest evidence package for a case in the agent account, if any."""
    conn = agent_conn()
    try:
        rows = _q(conn.cursor(), f"""SELECT package_id FROM {agent_target().schema_fq}.EVIDENCE_PACKAGE
                                     WHERE case_id = %s ORDER BY created_at DESC LIMIT 1""", (case_id,))
        return rows[0]["package_id"] if rows else None
    finally:
        conn.close()


def latest_report(case_id: str) -> dict[str, Any] | None:
    """The newest PDF whose STR artifact is still current in the data account.

    A report for a SUPERSEDED or REJECTED artifact is stale and is not returned,
    so the caller regenerates instead of sending an outdated STR.
    """
    aconn = dconn = None
    try:
        aconn = agent_conn()
        acur = aconn.cursor()
        rows = _q(acur, f"""
            SELECT r.stage_path, r.pdf_sha256, r.target_artifact_id, r.draft_id, r.created_at, d.package_id
            FROM {agent_target().schema_fq}.STR_REPORT r
            JOIN {agent_target().schema_fq}.AI_DRAFT d ON d.draft_id = r.draft_id
            WHERE r.case_id = %s AND r.target_artifact_id IS NOT NULL
            ORDER BY r.created_at DESC LIMIT 1""", (case_id,))
        if not rows:
            return None
        rep = rows[0]
        dconn = data_conn()
        art = _q(dconn.cursor(), f"SELECT status FROM {data_target().evidence}.ARTIFACT WHERE artifact_id = %s",
                 (rep["target_artifact_id"],))
        if not art or art[0]["status"] not in ("DRAFT", "REVIEWED", "FILED"):
            return None
        return {
            "case_id": case_id, "status": "EXISTING_REPORT", "artifact_id": rep["target_artifact_id"],
            "artifact_status": art[0]["status"], "draft_id": rep["draft_id"], "package_id": rep["package_id"],
            "stage_path": rep["stage_path"], "pdf_sha256": rep["pdf_sha256"], "created_at": rep["created_at"],
            "pdf_bytes": _fetch_pdf(acur, rep["stage_path"]),
            "pdf_name": f"STR_{case_id}_{rep['target_artifact_id']}.pdf",
        }
    finally:
        for c in (aconn, dconn):
            if c is not None:
                c.close()


def ask_agent(question: str, *, package_id: str | None = None, case_id: str | None = None,
              thread_id: int | None = None, parent_message_id: int = 0) -> AgentResult:
    """A follow-up question to RC_STR_AGENT, grounded in a package when one is known."""
    if case_id and not package_id:
        package_id = latest_package(case_id)
    ctx = f"Context: evidence package {package_id} for case {case_id}.\n" if package_id else ""
    return run_agent(ctx + question, thread_id=thread_id, parent_message_id=parent_message_id)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Generate an AI STR for one case across both accounts")
    ap.add_argument("case_id")
    ap.add_argument("--out", help="write the PDF here")
    ap.add_argument("--no-analyst", action="store_true")
    a = ap.parse_args()
    res = generate_ai_str(a.case_id, progress=lambda s, m: print(f"[{s}] {m}", flush=True),
                          channel="cli", include_analyst=not a.no_analyst)
    pdf = res.pop("pdf_bytes", None)
    if pdf and a.out:
        Path(a.out).write_bytes(pdf)
        print("PDF written:", a.out, len(pdf), "bytes")
    res.pop("sections", None)
    print(json.dumps(res, indent=1, default=str, ensure_ascii=False))
