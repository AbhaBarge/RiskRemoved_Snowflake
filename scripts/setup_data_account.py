"""One-shot setup for a new data account: connection, key pair, .env.

    .venv\\Scripts\\python scripts\\setup_data_account.py

It asks for the account identifier and the user, then:
  1. writes a key pair to ~/.snowflake/keys/<name>.p8 / .pub (never the repo);
  2. signs in once in your browser and runs ALTER USER ... SET RSA_PUBLIC_KEY;
  3. adds a [<name>] section to ~/.snowflake/connections.toml (others untouched);
  4. points .env at it (RC_SNOWFLAKE_CONNECTION, RC_SNOWFLAKE_PRIVATE_KEY_PATH);
  5. connects with the key, as the CLI will, and prints who it is.

The private key is unencrypted and readable only by your Windows user, like the
snow CLI's default, so unattended runs need no passphrase in any file. Re-running
reuses an existing key and connection section.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPO = Path(__file__).resolve().parents[1]
SF_HOME = Path(os.environ.get("SNOWFLAKE_HOME", Path.home() / ".snowflake")).expanduser()
TOML = SF_HOME / "connections.toml"
KEYS = SF_HOME / "keys"
ENV = REPO / ".env"


def ask(prompt: str, default: str = "") -> str:
    value = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return value or default


def restrict_to_me(path: Path) -> None:
    """Owner-only access: chmod on POSIX, an ACL with only this user on Windows."""
    if os.name != "nt":
        path.chmod(0o600)
        return
    subprocess.run(  # noqa: S603 - fixed argv, our own key path
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],  # noqa: S607
        check=True,
        capture_output=True,
    )


def key_pair(name: str) -> tuple[Path, str]:
    """(private key path, public key body for ALTER USER); reused if present."""
    KEYS.mkdir(parents=True, exist_ok=True)
    p8, pub = KEYS / f"{name.lower()}.p8", KEYS / f"{name.lower()}.pub"
    if p8.is_file():
        print(f"-- reusing the key at {p8}")
        key = serialization.load_pem_private_key(p8.read_bytes(), password=None)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        p8.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        restrict_to_me(p8)
        print(f"-- private key written to {p8}")
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    pub.write_bytes(pem)
    body = "".join(line for line in pem.decode().splitlines() if "-----" not in line)
    return p8, body


def register_key(account: str, user: str, body: str) -> None:
    import snowflake.connector

    print("-- a browser window opens: sign in to the new account (one time only)")
    conn = snowflake.connector.connect(
        account=account, user=user, authenticator="externalbrowser", role="ACCOUNTADMIN"
    )
    try:
        cur = conn.cursor()
        # Identifiers cannot be bound; the user name is validated in main().
        cur.execute(f'ALTER USER "{user.upper()}" SET RSA_PUBLIC_KEY = %s', (body,))
        print(f"-- public key registered on user {user.upper()}")
    finally:
        conn.close()


def write_connection(name: str, account: str, user: str) -> None:
    TOML.parent.mkdir(parents=True, exist_ok=True)
    text = TOML.read_text(encoding="utf-8") if TOML.is_file() else ""
    existing = tomllib.loads(text) if text else {}
    if name in existing:
        print(f"-- [{name}] already in {TOML}; left as it is")
        return
    block = f'\n[{name}]\naccount = "{account}"\nuser = "{user}"\n'
    TOML.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")
    print(f"-- added [{name}] to {TOML} (other connections untouched)")


def write_env(name: str, p8: Path) -> None:
    if not ENV.is_file():
        ENV.write_text((REPO / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    text = ENV.read_text(encoding="utf-8")
    values = {
        "RC_SNOWFLAKE_CONNECTION": name,
        "RC_SNOWFLAKE_PRIVATE_KEY_PATH": str(p8),
        "RC_SNOWFLAKE_PRIVATE_KEY_PASSPHRASE": "",
    }
    for var, value in values.items():
        line = f"{var}={value}"
        pattern = re.compile(rf"^#?\s*{var}=.*$", re.M)
        if pattern.search(text):
            text = pattern.sub(lambda _m, line=line: line, text, count=1)
        else:
            text += f"\n{line}\n"
    ENV.write_text(text, encoding="utf-8")
    print(f"-- {ENV.name} now uses connection {name}")


def check(account: str, user: str, p8: Path) -> None:
    import snowflake.connector

    der = serialization.load_pem_private_key(p8.read_bytes(), password=None).private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    conn = snowflake.connector.connect(
        account=account, user=user, private_key=der, authenticator="SNOWFLAKE_JWT"
    )
    try:
        row = (
            conn.cursor()
            .execute("SELECT CURRENT_ACCOUNT(), CURRENT_USER(), CURRENT_REGION(), CURRENT_ROLE()")
            .fetchone()
        )
        print(f"-- key-pair sign-in OK: account {row[0]}, user {row[1]}, region {row[2]}")
        print(f"   default role {row[3]}")
    finally:
        conn.close()


def main() -> int:
    print("Risk Copilot -- new data account setup\n")
    account = ask("Account identifier (Snowsight > account menu > Account details, e.g. ORG-ACCT)")
    user = ask("Snowflake user name")
    name = ask("Connection name to create", "RC_DATA").upper()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", account) or not re.fullmatch(r"[A-Za-z0-9_]+", user):
        print("Account may hold letters, digits, - _ . ; the user letters, digits and _.")
        return 2
    if not re.fullmatch(r"[A-Z0-9_]+", name):
        print("Connection name: letters, digits and _ only.")
        return 2

    p8, body = key_pair(name)
    try:
        register_key(account, user, body)
    except Exception as exc:
        print(f"\n!! Could not register the key automatically: {exc}")
        print("   Run this in a Snowsight worksheet as ACCOUNTADMIN, then re-run this script:\n")
        print(f"   ALTER USER \"{user.upper()}\" SET RSA_PUBLIC_KEY = '{body}';\n")
    write_connection(name, account, user)
    write_env(name, p8)
    try:
        check(account, user, p8)
    except Exception as exc:
        print(f"\n!! Key-pair sign-in failed: {exc}")
        return 1
    print("\nDone. Tell the assistant the account is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
