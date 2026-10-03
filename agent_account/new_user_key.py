"""Create a key pair for one teammate's bridge, and print the SQL to register it.

Each person who runs the Slack bot or the Streamlit "AI STR" button gets their
own SERVICE user in the agent account, so no private key is ever shared and
AGENT_RUN_LOG shows who ran what. The private key stays on this machine
(bridge/keys/ is gitignored); only the printed public key is sent to the
agent-account admin.

Usage (teammate, from RiskRemoved_Snowflake/):
    bridge/.venv/Scripts/python agent_account/new_user_key.py ASHWIN
Then send the printed SQL to the agent-account admin, who runs it as ACCOUNTADMIN.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEYS = Path(__file__).resolve().parents[1] / "bridge" / "keys"


def main(who: str) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,30}", who):
        sys.exit("Name must be letters, digits or _ (e.g. ASHWIN)")
    user = f"RC_AGENT_SVC_{who.upper()}"
    KEYS.mkdir(parents=True, exist_ok=True)
    p8 = KEYS / f"{user.lower()}.p8"
    if p8.exists():
        key = serialization.load_pem_private_key(p8.read_bytes(), None)
        print(f"-- reusing existing key {p8}")
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        p8.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
        print(f"-- private key written to {p8} (keep it; never commit or share it)")
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    body = "".join(line for line in pub.splitlines() if "-----" not in line)
    print(f"""
-- ===== send everything below to the agent-account admin (run as ACCOUNTADMIN) =====
CREATE USER IF NOT EXISTS {user}
    TYPE = SERVICE
    DEFAULT_ROLE = RC_AGENT_ROLE
    DEFAULT_WAREHOUSE = RC_AGENT_WH
    DEFAULT_NAMESPACE = RISK_COPILOT_AGENT.STR
    COMMENT = 'Risk Copilot bridge for {who}';
GRANT ROLE RC_AGENT_ROLE TO USER {user};
ALTER USER {user} SET RSA_PUBLIC_KEY = '{body}';
-- ===================================================================================

Then add these two lines to bridge/.env:
AGENT_SNOWFLAKE_USER={user}
AGENT_PRIVATE_KEY_PATH=keys/{p8.name}
""")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
