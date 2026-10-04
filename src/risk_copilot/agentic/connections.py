"""Key-pair connections to the two accounts.

    data account   SNOWFLAKE_*        the teammate account: evidence, Analyst,
                                      DRAFT/REVIEW/ATTEST procedures
    agent account  AGENT_SNOWFLAKE_*  this project's agent account: the STR
                                      agent, its tools, the PDF stage

Values come from the environment, loaded from bridge/.env (override the path
with RC_BRIDGE_ENV). Relative key paths resolve against the .env's folder.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import snowflake.connector
from cryptography.hazmat.primitives import serialization

_REPO = Path(__file__).resolve().parents[3]
_ENV_FILE = Path(os.getenv("RC_BRIDGE_ENV", _REPO / "bridge" / ".env"))
_BASE = _ENV_FILE.parent

try:
    from dotenv import load_dotenv

    load_dotenv(_ENV_FILE, override=False)
except ImportError:  # the Streamlit app may run without python-dotenv
    pass


@dataclass(frozen=True)
class DataTarget:
    """Where the evidence lives, and the profile suffix on its object names."""

    database: str
    suffix: str
    semantic_view: str

    @property
    def evidence(self) -> str:
        return f"{self.database}.EVIDENCE_CHAIN"

    def proc(self, name: str) -> str:
        return f"{self.evidence}.{name}{self.suffix}"


@dataclass(frozen=True)
class AgentTarget:
    database: str
    schema: str
    agent: str

    @property
    def schema_fq(self) -> str:
        return f"{self.database}.{self.schema}"

    @property
    def agent_fq(self) -> str:
        return f"{self.schema_fq}.{self.agent}"


def data_target() -> DataTarget:
    db = os.getenv("DATA_DATABASE", "RISK_COPILOT_DEV")
    suffix = os.getenv("DATA_OBJECT_SUFFIX", "_DEV" if db.endswith("_DEV") else "")
    sv = os.getenv("SNOWFLAKE_SEMANTIC_VIEW", f"{db}.CURATED.SV_RISK_COPILOT{suffix}")
    return DataTarget(database=db, suffix=suffix, semantic_view=sv)


def agent_target() -> AgentTarget:
    return AgentTarget(
        database=os.getenv("AGENT_DATABASE", "RISK_COPILOT_AGENT"),
        schema=os.getenv("AGENT_SCHEMA", "STR"),
        agent=os.getenv("AGENT_NAME", "RC_STR_AGENT"),
    )


def agent_configured() -> bool:
    return _key_path("AGENT_PRIVATE_KEY_PATH", "keys/rc_agent_svc.p8").exists()


def _key_path(var: str, default: str | None = None) -> Path:
    raw = os.getenv(var, default or "")
    path = Path(raw)
    return path if path.is_absolute() else _BASE / path


def _private_key(var: str, passphrase_var: str, default: str | None = None) -> bytes:
    path = _key_path(var, default)
    passphrase = os.getenv(passphrase_var)
    key = serialization.load_pem_private_key(
        path.read_bytes(), password=passphrase.encode() if passphrase else None
    )
    return key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def data_conn() -> snowflake.connector.SnowflakeConnection:
    """The teammate account that holds the data and the evidence chain."""
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        role=os.getenv("SNOWFLAKE_ROLE"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        private_key=_private_key("SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"),
        application="risk_copilot_bridge",
        session_parameters={"QUERY_TAG": "risk_copilot|agentic|data"},
    )


def agent_conn() -> snowflake.connector.SnowflakeConnection:
    """The account that hosts the STR agent and its tools."""
    t = agent_target()
    return snowflake.connector.connect(
        account=os.getenv("AGENT_SNOWFLAKE_ACCOUNT", "HQRWRXK-UZ26728"),
        user=os.getenv("AGENT_SNOWFLAKE_USER", "RC_AGENT_SVC"),
        role=os.getenv("AGENT_SNOWFLAKE_ROLE", "RC_AGENT_ROLE"),
        warehouse=os.getenv("AGENT_SNOWFLAKE_WAREHOUSE", "RC_AGENT_WH"),
        database=t.database,
        schema=t.schema,
        private_key=_private_key(
            "AGENT_PRIVATE_KEY_PATH", "AGENT_PRIVATE_KEY_PASSPHRASE", "keys/rc_agent_svc.p8"
        ),
        application="risk_copilot_bridge",
        session_parameters={"QUERY_TAG": "risk_copilot|agentic|agent"},
    )
