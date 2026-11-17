"""Deploy the Risk Copilot Slack bot to Snowpark Container Services.

Reads bridge/.env (the same file the local bot uses). Slack tokens and the two
.p8 keys become Snowflake SECRETs; plain settings become env vars in the
service spec. Nothing secret is printed.

    python bridge/spcs/deploy.py setup      # schema, repo, secrets, egress rule, EAI, pool, grants
    python bridge/spcs/deploy.py registry   # print the image registry URL (for docker push)
    python bridge/spcs/deploy.py service    # create the service, or ALTER it to the current spec
    python bridge/spcs/deploy.py restart    # suspend + resume: pulls a re-pushed :latest image
    python bridge/spcs/deploy.py status
    python bridge/spcs/deploy.py logs [N]
    python bridge/spcs/deploy.py suspend    # stop the bot and the compute pool (no credits)
    python bridge/spcs/deploy.py resume

The egress rule is outbound-only (MODE = EGRESS): it says where the container may
connect. It is not a network policy and does not affect who can log in.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE.parent / "src"))
os.chdir(BRIDGE)  # relative key paths in .env resolve from bridge/

import snowflake.connector  # noqa: E402

from risk_copilot.agentic.connections import _private_key  # noqa: E402
from risk_copilot.config import validate_identifier  # noqa: E402

ENV = {k: v for k, v in dotenv_values(BRIDGE / ".env").items() if v is not None}

DB = ENV.get("DATA_DATABASE", "RISK_COPILOT_DEV")
SUFFIX = ENV.get("DATA_OBJECT_SUFFIX", "_DEV")
SCHEMA = f"{DB}.SLACK_BOT"
REPO = f"{SCHEMA}.BOT_REPO"
IMAGE = "risk-copilot-slack-bot:latest"
POOL = f"RC_SLACK_BOT_POOL{SUFFIX}"
EAI = f"RC_SLACK_BOT_EAI{SUFFIX}"
RULE = f"{SCHEMA}.SLACK_BOT_EGRESS"
SERVICE = f"{SCHEMA}.RC_SLACK_BOT"
OWNER = ENV.get("SNOWFLAKE_ROLE", f"RC_ADMIN{SUFFIX}")
WAREHOUSE = ENV.get("SNOWFLAKE_WAREHOUSE", f"RC_DEMO_WH{SUFFIX}")

SECRETS = {  # secret name -> (value source, env var in the container)
    "SLACK_BOT_TOKEN": ("SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN"),
    "SLACK_APP_TOKEN": ("SLACK_APP_TOKEN", "SLACK_APP_TOKEN"),
    "SLACK_SIGNING_SECRET": ("SLACK_SIGNING_SECRET", "SLACK_SIGNING_SECRET"),
    "DATA_PRIVATE_KEY": ("@SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_PRIVATE_KEY_PEM"),
    "AGENT_PRIVATE_KEY": ("@AGENT_PRIVATE_KEY_PATH", "AGENT_PRIVATE_KEY_PEM"),
}
SECRET_ENV = {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_SIGNING_SECRET"}
PASS_THROUGH_SKIP = SECRET_ENV | {
    "SNOWFLAKE_PRIVATE_KEY_PATH",
    "AGENT_PRIVATE_KEY_PATH",
    "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
    "AGENT_PRIVATE_KEY_PASSPHRASE",
    "SNOWFLAKE_PASSWORD",
}


def _conn(role: str):
    return snowflake.connector.connect(
        account=ENV["SNOWFLAKE_ACCOUNT"],
        user=ENV["SNOWFLAKE_USER"],
        role=role,
        warehouse=WAREHOUSE,
        private_key=_private_key("SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"),
        application="risk_copilot_spcs_deploy",
    )


def _run(cur, sql: str, show: str | None = None) -> list:
    print(">", show or " ".join(sql.split())[:160])
    cur.execute(sql)
    return cur.fetchall()


def _pem(var: str) -> str:
    """The key as unencrypted PKCS8 PEM (the container has no passphrase)."""
    from cryptography.hazmat.primitives import serialization

    der = _private_key(var, var.replace("_PATH", "_PASSPHRASE"))
    key = serialization.load_der_private_key(der, password=None)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _secret_value(source: str) -> str:
    value = _pem(source[1:]) if source.startswith("@") else ENV.get(source, "")
    if not value or "REPLACE" in value:
        raise SystemExit(f"bridge/.env: {source.lstrip('@')} is empty or a placeholder")
    if "$$" in value:
        raise SystemExit(f"{source}: value contains '$$', cannot be dollar-quoted")
    return value


def _egress_hosts(cur) -> list[str]:
    """Slack, plus this account's Snowflake, stage (S3) and certificate-check hosts."""
    keep = {
        "SNOWFLAKE_DEPLOYMENT",
        "SNOWFLAKE_DEPLOYMENT_REGIONLESS",
        "STAGE",
        "OCSP_CACHE",
        "ACCOUNT_PUBLIC",
    }
    # Certificate-check hosts: SYSTEM$ALLOWLIST can list ones that no longer
    # resolve, which CREATE NETWORK RULE rejects; one wildcard level covers them.
    cert = {"OCSP_RESPONDER", "CRL_DISTRIBUTION_POINT"}
    hosts = set()
    cur.execute("SELECT SYSTEM$ALLOWLIST()")
    for e in json.loads(cur.fetchone()[0]):
        host = e["host"].lower()
        if e["type"] in keep:
            hosts.add(f"{host}:{e['port']}")
        elif e["type"] in cert:
            hosts.add(f"*.{'.'.join(host.split('.')[-2:])}:{e['port']}")
    hosts |= {"slack.com:443", "*.slack.com:443"}
    return sorted(hosts)


def setup() -> None:
    con = _conn("ACCOUNTADMIN")
    cur = con.cursor()
    _run(cur, f"USE ROLE {OWNER}")
    _run(cur, f"CREATE SCHEMA IF NOT EXISTS {SCHEMA} COMMENT = 'Risk Copilot Slack bot on SPCS'")
    _run(cur, f"CREATE IMAGE REPOSITORY IF NOT EXISTS {REPO}")
    for name, (source, _) in SECRETS.items():
        value = _secret_value(source)
        cur.execute(
            f"CREATE OR REPLACE SECRET {SCHEMA}.{name} TYPE = GENERIC_STRING "
            f"SECRET_STRING = $${value}$$"
        )
        print(f"> CREATE OR REPLACE SECRET {SCHEMA}.{name} (value from bridge/.env, not shown)")
    hosts = _egress_hosts(cur)
    values = ", ".join(f"'{h}'" for h in hosts)
    _run(
        cur,
        f"CREATE OR REPLACE NETWORK RULE {RULE} MODE = EGRESS TYPE = HOST_PORT "
        f"VALUE_LIST = ({values}) COMMENT = 'Outbound only: Slack + this Snowflake account'",
        show=f"CREATE OR REPLACE NETWORK RULE {RULE} MODE = EGRESS ({len(hosts)} hosts)",
    )

    _run(cur, "USE ROLE ACCOUNTADMIN")
    _run(
        cur,
        f"CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS {EAI} "
        f"ALLOWED_NETWORK_RULES = ({RULE}) ENABLED = TRUE",
    )
    _run(cur, f"ALTER EXTERNAL ACCESS INTEGRATION {EAI} SET ALLOWED_NETWORK_RULES = ({RULE})")
    _run(
        cur,
        f"CREATE COMPUTE POOL IF NOT EXISTS {POOL} MIN_NODES = 1 MAX_NODES = 1 "
        f"INSTANCE_FAMILY = CPU_X64_XS AUTO_RESUME = TRUE AUTO_SUSPEND_SECS = 300 "
        f"COMMENT = 'Risk Copilot Slack bot'",
    )
    _run(cur, f"GRANT USAGE ON INTEGRATION {EAI} TO ROLE {OWNER}")
    _run(cur, f"GRANT USAGE, MONITOR, OPERATE ON COMPUTE POOL {POOL} TO ROLE {OWNER}")
    con.close()
    print("setup done. Egress hosts:\n  " + "\n  ".join(hosts))


def registry() -> None:
    con = _conn(OWNER)
    rows = con.cursor().execute(f"SHOW IMAGE REPOSITORIES LIKE 'BOT_REPO' IN SCHEMA {SCHEMA}").fetchall()
    con.close()
    url = rows[0][4]
    print(url)
    print(f"docker tag {IMAGE} {url}/{IMAGE}\ndocker push {url}/{IMAGE}")


def _spec() -> str:
    env = {
        k: v
        for k, v in ENV.items()
        if k not in PASS_THROUGH_SKIP and v and validate_identifier(k, what="env key")
    }
    env["DEMO_MODE"] = ENV.get("DEMO_MODE", "false")
    db_l, sch = DB.lower(), "slack_bot"
    lines = [
        "spec:",
        "  containers:",
        "  - name: bot",
        f"    image: /{db_l}/{sch}/bot_repo/{IMAGE}",
        "    env:",
    ]
    lines += [f"      {k}: {json.dumps(v)}" for k, v in sorted(env.items())]
    lines.append("    secrets:")
    for name, (_, env_var) in SECRETS.items():
        lines += [
            f"    - snowflakeSecret: {SCHEMA}.{name}",
            "      secretKeyRef: secret_string",
            f"      envVarName: {env_var}",
        ]
    lines += [
        "    resources:",
        "      requests: {cpu: 0.5, memory: 1Gi}",
        "      limits: {cpu: 1, memory: 2Gi}",
    ]
    return "\n".join(lines) + "\n"


def service() -> None:
    con = _conn(OWNER)
    cur = con.cursor()
    exists = cur.execute(f"SHOW SERVICES LIKE 'RC_SLACK_BOT' IN SCHEMA {SCHEMA}").fetchall()
    spec = _spec()
    if exists:
        _run(cur, f"ALTER SERVICE {SERVICE} FROM SPECIFICATION $${spec}$$", "ALTER SERVICE ... FROM SPECIFICATION")
    else:
        _run(
            cur,
            f"CREATE SERVICE {SERVICE} IN COMPUTE POOL {POOL} "
            f"FROM SPECIFICATION $${spec}$$ "
            f"EXTERNAL_ACCESS_INTEGRATIONS = ({EAI}) QUERY_WAREHOUSE = {WAREHOUSE} "
            f"MIN_INSTANCES = 1 MAX_INSTANCES = 1 "
            f"COMMENT = 'Risk Copilot Slack bot (Socket Mode)'",
            f"CREATE SERVICE {SERVICE} IN COMPUTE POOL {POOL}",
        )
    con.close()
    status()


def status() -> None:
    con = _conn(OWNER)
    cur = con.cursor()
    for _ in range(30):
        raw = cur.execute(f"SELECT SYSTEM$GET_SERVICE_STATUS('{SERVICE}')").fetchone()[0]
        st = json.loads(raw)
        print(json.dumps(st, indent=1))
        if st and st[0].get("status") in ("READY", "FAILED", "SUSPENDED", "DONE"):
            break
        time.sleep(10)
    con.close()


def logs(lines: int = 100) -> None:
    con = _conn(OWNER)
    out = con.cursor().execute(
        f"SELECT SYSTEM$GET_SERVICE_LOGS('{SERVICE}', 0, 'bot', {int(lines)})"
    ).fetchone()[0]
    con.close()
    print(out)


def restart() -> None:
    con = _conn(OWNER)
    cur = con.cursor()
    _run(cur, f"ALTER SERVICE {SERVICE} SUSPEND")
    time.sleep(10)
    _run(cur, f"ALTER SERVICE {SERVICE} RESUME")
    con.close()
    status()


def suspend() -> None:
    con = _conn(OWNER)
    cur = con.cursor()
    _run(cur, f"ALTER SERVICE {SERVICE} SUSPEND")
    _run(cur, f"ALTER COMPUTE POOL {POOL} SUSPEND")
    con.close()


def resume() -> None:
    con = _conn(OWNER)
    cur = con.cursor()
    _run(cur, f"ALTER COMPUTE POOL {POOL} RESUME")
    _run(cur, f"ALTER SERVICE {SERVICE} RESUME")
    con.close()
    status()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    actions = {
        "setup": setup,
        "registry": registry,
        "service": service,
        "restart": restart,
        "status": status,
        "suspend": suspend,
        "resume": resume,
    }
    if cmd == "logs":
        logs(int(sys.argv[2]) if len(sys.argv) > 2 else 100)
    elif cmd in actions:
        actions[cmd]()
    else:
        print(__doc__)
        sys.exit(2)
