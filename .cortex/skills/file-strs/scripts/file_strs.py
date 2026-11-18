"""Run the analyst's four STR steps for the next N unfiled cases, without the UI.

Same calls, same order and same Snowflake identity as the Case investigation page
(app/app_pages/case.py): the [snowflake] service user from app/.streamlit/secrets.toml,
i.e. what the app uses while the AML Analyst (maker) is signed in.

    1 GATHER_EVIDENCE(case)   2 DRAFT_STR(case, FALSE)   3 REVIEW_STR(artifact)
    4 GENERATE_AI_STR(case)   -> PDF written to --out

Usage (from the repo root):
    .venv\\Scripts\\python.exe .cortex\\skills\\file-strs\\scripts\\file_strs.py --count 3
    ... --list            # show eligible cases, call nothing
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
CFG = json.loads((REPO / "app" / "app_config.json").read_text("utf-8"))
PROC: dict[str, str] = CFG["procedure"]
EV: str = CFG["schema"]["evidence"]


def analyst_session(secrets: Path) -> Any:
    """The app's analyst session: key-pair JWT for the [snowflake] section (lib/db.py)."""
    from cryptography.hazmat.primitives import serialization
    from snowflake.snowpark import Session

    cfg = dict(tomllib.loads(secrets.read_text("utf-8"))["snowflake"])
    key = serialization.load_pem_private_key(cfg.pop("private_key_pem").encode(), password=None)
    cfg["private_key"] = key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    cfg["authenticator"] = "SNOWFLAKE_JWT"
    cfg["application"] = "risk_copilot_file_strs"
    return Session.builder.configs(cfg).create()


def call(s: Any, proc: str, *args: Any) -> Any:
    marks = ", ".join("?" for _ in args)
    rows = s.sql(f"CALL {proc}({marks})", params=list(args)).collect()
    value = rows[0][0] if rows else None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def reason(exc: Exception) -> str:
    text = str(exc)
    i = text.rfind(" : ")
    return (text[i + 3 :] if i != -1 else text).strip()


# One row per open case with a signal and no FILED STR, plus where it stands:
# evidence count and the latest STR version's status (NULL = none drafted yet).
# REVIEWED cases are already waiting for the officer, so they are left alone.
ELIGIBLE = f"""
WITH latest AS (
  SELECT f.case_id, a.artifact_id, a.status,
         MAX(IFF(a.status = 'FILED', 1, 0)) OVER (PARTITION BY f.case_id) AS filed
  FROM {EV}.ARTIFACT a JOIN {EV}.FINDING f ON f.finding_id = a.finding_id
  QUALIFY ROW_NUMBER() OVER (PARTITION BY f.case_id ORDER BY a.version DESC) = 1
)
SELECT c.case_id,
       (SELECT COUNT(*) FROM {EV}.EVIDENCE e WHERE e.case_id = c.case_id) AS evidence,
       l.artifact_id, l.status AS str_status
FROM {EV}.CASE_RECORD c
LEFT JOIN latest l ON l.case_id = c.case_id
WHERE c.status <> 'CLOSED'
  AND EXISTS (SELECT 1 FROM {EV}.CASE_SIGNAL cs WHERE cs.case_id = c.case_id)
  AND COALESCE(l.filed, 0) = 0
  AND COALESCE(l.status, 'NONE') IN ('NONE', 'DRAFT', 'REJECTED')
ORDER BY (l.status IS NULL AND evidence = 0) DESC, c.case_id
"""

LATEST_STR = f"""
SELECT a.artifact_id, a.status
FROM {EV}.ARTIFACT a JOIN {EV}.FINDING f ON f.finding_id = a.finding_id
WHERE f.case_id = ? ORDER BY a.version DESC LIMIT 1
"""


def latest_str(s: Any, case_id: str) -> tuple[str | None, str | None]:
    rows = s.sql(LATEST_STR, params=[case_id]).collect()
    return (rows[0]["ARTIFACT_ID"], rows[0]["STATUS"]) if rows else (None, None)


def work_case(s: Any, row: Any, out: Path) -> dict[str, Any]:
    case_id = row["CASE_ID"]
    rec: dict[str, Any] = {"case_id": case_id, "steps": []}

    def step(name: str, fn) -> Any:
        t0 = time.time()
        print(f"  {name} ...", end="", flush=True)
        res = fn()
        print(f" ok ({time.time() - t0:.0f}s)")
        rec["steps"].append(name)
        return res

    status = row["STR_STATUS"]
    if status in (None, "REJECTED"):
        if not row["EVIDENCE"]:
            step("1 gather evidence", lambda: call(s, PROC["gather_evidence"], case_id))
        step("2 draft STR", lambda: call(s, PROC["draft_str"], case_id, False))
    artifact_id, status = latest_str(s, case_id)
    if status != "DRAFT":
        raise RuntimeError(f"expected a DRAFT STR before review, found {status}")
    step("3 run Reviewer", lambda: call(s, PROC["review_str"], artifact_id))
    _, status = latest_str(s, case_id)
    if status != "REVIEWED":
        raise RuntimeError(f"Reviewer did not approve (STR status {status})")
    res = step("4 AI STR + PDF", lambda: call(s, PROC["generate_ai_str"], case_id))
    res = res if isinstance(res, dict) else {"status": "ERROR", "error": str(res)}
    rec.update(
        ai_status=res.get("status"),
        package_id=res.get("package_id"),
        drafter=(res.get("ai_review") or {}).get("drafter_model"),
        ai_reviewer=(res.get("ai_review") or {}).get("decision"),
        data_reviewer=(res.get("data_review") or {}).get("decision"),
    )
    if res.get("pdf_b64"):
        pdf = out / res.get("pdf_name", f"STR_{case_id}.pdf")
        pdf.write_bytes(base64.b64decode(res["pdf_b64"]))
        rec["pdf"] = str(pdf)
    if res.get("status") != "READY_FOR_ATTESTATION":
        raise RuntimeError(f"AI STR ended {res.get('status')}: {res.get('error') or ''}".strip())
    return rec


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--count", type=int, default=1, help="how many STRs to take to sign-off")
    p.add_argument("--list", action="store_true", help="only list eligible cases")
    p.add_argument("--secrets", type=Path, default=REPO / "app" / ".streamlit" / "secrets.toml")
    p.add_argument("--out", type=Path, default=REPO / "out" / "str_pdfs")
    a = p.parse_args()

    s = analyst_session(a.secrets)
    try:
        who = s.sql("SELECT CURRENT_USER() u, CURRENT_ROLE() r").collect()[0]
        print(f"Snowflake identity: user {who['U']}, role {who['R']}")
        rows = s.sql(ELIGIBLE).collect()
        print(f"Eligible unfiled cases: {len(rows)}")
        for r in rows:
            print(f"  {r['CASE_ID']}  evidence={r['EVIDENCE']}  str={r['STR_STATUS'] or '-'}")
        if a.list:
            return 0
        a.out.mkdir(parents=True, exist_ok=True)
        done, failed = [], []
        for r in rows:
            if len(done) >= a.count:
                break
            print(f"\n{r['CASE_ID']}")
            try:
                done.append(work_case(s, r, a.out))
            except Exception as exc:  # skip this case, keep going
                msg = reason(exc)
                print(f"\n  FAILED: {msg}")
                failed.append({"case_id": r["CASE_ID"], "error": msg})
        print("\nRESULT " + json.dumps({"requested": a.count, "ready": done, "failed": failed}))
        return 0 if len(done) == a.count else 2
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
