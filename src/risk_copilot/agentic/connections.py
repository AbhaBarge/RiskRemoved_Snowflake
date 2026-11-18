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
from typing import Any

import snowflake.connector
from cryptography.hazmat.primitives import serialization

from risk_copilot.config import validate_identifier

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


@dataclass(frozen=True)
class AgentEndpoint:
    """Where an agent:run goes, and the key-pair identity that calls it.

    The investigator lives next to the data, not in the agent account, so a call
    to it names its endpoint. Calls without one keep the STR agent's defaults.
    """

    account: str
    user: str
    key_var: str
    passphrase_var: str
    target: AgentTarget
    key_default: str | None = None
    role: str | None = None  # sent as X-Snowflake-Role; None = the user's default role

    @property
    def cache_key(self) -> str:
        return f"{self.account.upper()}|{self.user.upper()}"


def _ident(var: str, default: str) -> str:
    """An object name from the environment, validated: these are interpolated into SQL."""
    return validate_identifier(os.getenv(var, default), what=var)


def _fqn(var: str, default: str) -> str:
    """A dotted name from the environment, each part validated."""
    raw = os.getenv(var, default)
    return ".".join(validate_identifier(part, what=var) for part in raw.split("."))


def data_target() -> DataTarget:
    db = _ident("DATA_DATABASE", "RISK_COPILOT_DEV")
    suffix = os.getenv("DATA_OBJECT_SUFFIX", "_DEV" if db.endswith("_DEV") else "")
    if suffix:
        suffix = "_" + validate_identifier(suffix.lstrip("_"), what="DATA_OBJECT_SUFFIX")
    sv = _fqn("SNOWFLAKE_SEMANTIC_VIEW", f"{db}.CURATED.SV_RISK_COPILOT{suffix}")
    return DataTarget(database=db, suffix=suffix, semantic_view=sv)


def agent_target() -> AgentTarget:
    return AgentTarget(
        database=_ident("AGENT_DATABASE", "RISK_COPILOT_AGENT"),
        schema=_ident("AGENT_SCHEMA", "STR"),
        agent=_ident("AGENT_NAME", "RC_STR_AGENT"),
    )


def agent_configured() -> bool:
    return _key_path("AGENT_PRIVATE_KEY_PATH", "keys/rc_agent_svc.p8").exists()


def investigator_target() -> AgentTarget:
    """The investigator agent (sql/12_investigator), next to the data it queries."""
    t = data_target()
    return AgentTarget(
        database=t.database,
        schema=_ident("INVESTIGATOR_AGENT_SCHEMA", "EVIDENCE_CHAIN"),
        agent=_ident("INVESTIGATOR_AGENT_NAME", f"RC_INVESTIGATOR_AGENT{t.suffix}"),
    )


def investigator_endpoint() -> AgentEndpoint:
    """The investigator, called with the data account's key pair and role."""
    return AgentEndpoint(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        key_var="SNOWFLAKE_PRIVATE_KEY_PATH",
        passphrase_var="SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",  # noqa: S106 - an env var name
        target=investigator_target(),
        role=os.getenv("SNOWFLAKE_ROLE") or None,
    )


def investigator_configured() -> bool:
    """True when the data account's key pair is available to call the investigator."""
    if not (os.getenv("SNOWFLAKE_ACCOUNT") and os.getenv("SNOWFLAKE_USER")):
        return False
    raw = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH")
    return bool(raw) and _key_path("SNOWFLAKE_PRIVATE_KEY_PATH").is_file()


def _key_path(var: str, default: str | None = None) -> Path:
    raw = os.getenv(var, default or "")
    path = Path(raw)
    return path if path.is_absolute() else _BASE / path


def load_private_key(var: str, passphrase_var: str, default: str | None = None) -> Any:
    """The private key object at the path in `var`, for the investigator's JWT.

    The passphrase is used only if the key is encrypted: a stale passphrase left
    in .env next to an unencrypted key must not make loading fail.
    """
    raw = _key_path(var, default).read_bytes()
    try:
        return serialization.load_pem_private_key(raw, password=None)
    except TypeError:  # encrypted: the passphrase is required
        passphrase = os.getenv(passphrase_var) or ""
        return serialization.load_pem_private_key(raw, password=passphrase.encode())


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
        account=os.environ["AGENT_SNOWFLAKE_ACCOUNT"],
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
