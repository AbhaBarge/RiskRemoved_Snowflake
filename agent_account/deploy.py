"""Deploy the agent-account SQL (02_tables.sql onward) as RC_AGENT_SVC.

01_bootstrap.sql needs ACCOUNTADMIN and is run once by hand. Everything after
it is owned by RC_AGENT_ROLE, so it is deployed with the service user's key
pair; the ACCOUNTADMIN preamble in 02_tables.sql is skipped.

Usage (from RiskRemoved_Snowflake/):
    bridge/.venv/Scripts/python agent_account/deploy.py            # all files
    bridge/.venv/Scripts/python agent_account/deploy.py 03 04      # some files
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from risk_copilot.agentic.connections import agent_conn  # noqa: E402

SQL_DIR = Path(__file__).resolve().parent / "sql"
SKIP_PREFIX = ("USE ROLE ACCOUNTADMIN", "GRANT CREATE TABLE", "USE ROLE RC_AGENT_ROLE")


def _statements(text: str) -> str:
    """Drop the ACCOUNTADMIN-only preamble lines (already applied by hand)."""
    keep, skipping = [], False
    for line in text.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith(SKIP_PREFIX):
            skipping = not stripped.endswith(";")
            continue
        if skipping:
            skipping = not stripped.endswith(";")
            continue
        keep.append(line)
    return "\n".join(keep)


def _stage_code(conn) -> None:
    """Upload the tools module the 03_tools.sql procedures import."""
    src = (Path(__file__).resolve().parent / "python" / "str_tools.py").as_posix()
    cur = conn.cursor()
    cur.execute("CREATE STAGE IF NOT EXISTS RISK_COPILOT_AGENT.STR.CODE ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')")
    cur.execute(f"PUT 'file://{src}' @RISK_COPILOT_AGENT.STR.CODE AUTO_COMPRESS = FALSE OVERWRITE = TRUE")
    print("   staged str_tools.py:", cur.fetchone()[6])


def main(selected: list[str]) -> None:
    files = sorted(p for p in SQL_DIR.glob("*.sql") if not p.name.startswith("01_"))
    if selected:
        files = [p for p in files if any(p.name.startswith(s) for s in selected)]
    conn = agent_conn()
    for path in files:
        print(f"-- {path.name}")
        if path.name.startswith("03_"):
            _stage_code(conn)
        for cur in conn.execute_string(_statements(path.read_text(encoding="utf-8"))):
            first = cur.fetchone()
            print("  ", (first[0] if first else "ok"))
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
