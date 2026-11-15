"""Snowflake session management.

One place resolves credentials, and it resolves them from
``~/.snowflake/connections.toml`` rather than from this repository. No account
identifier, user, password, key or token appears in source or in ``conf/``;
``conf/external.yml`` holds only the *name* of a connection and the *names* of
secrets.

Key-pair (``SNOWFLAKE_JWT``) is required for unattended execution -- automations
and CI cannot answer an SSO prompt -- while interactive authentication remains
fine for local development. :func:`open_session` warns rather than fails when an
interactive method is used, because failing would block the ordinary
development loop.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from risk_copilot.config import ConfigError, Settings

logger = logging.getLogger(__name__)

#: Methods that require a human. Unattended contexts must not use these.
_INTERACTIVE_AUTHENTICATORS = {"externalbrowser", "username_password_mfa", "oauth"}


class SessionError(RuntimeError):
    """Raised when a Snowflake session cannot be established or configured."""


def _connections_toml_path() -> Path:
    """Return the location Snowflake reads connection definitions from."""
    if override := os.environ.get("SNOWFLAKE_HOME"):
        return Path(override).expanduser() / "connections.toml"
    return Path.home() / ".snowflake" / "connections.toml"


def _load_private_key(settings: Settings) -> bytes | None:
    """Return DER-encoded private key bytes for key-pair auth, if configured.

    The key is read from the path in ``RC_SNOWFLAKE_PRIVATE_KEY_PATH``. It is
    never read from ``conf/``, and its contents are never logged. An encrypted
    key needs a passphrase, which is fetched from the environment so the value
    does not pass through a config file.
    """
    sf = settings.external.get("snowflake") or {}
    env_var = sf.get("private_key_path_env", "RC_SNOWFLAKE_PRIVATE_KEY_PATH")
    raw_path = os.environ.get(str(env_var))
    if not raw_path:
        return None

    key_path = Path(raw_path).expanduser()
    if not key_path.is_file():
        raise SessionError(
            f"{env_var} points at {key_path}, which does not exist. "
            "Unset it to fall back to the connections.toml authenticator."
        )

    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:  # pragma: no cover
        raise SessionError(
            "Key-pair authentication requires the 'cryptography' package, which "
            "ships with snowflake-connector-python. Reinstall the project "
            "dependencies."
        ) from exc

    passphrase = os.environ.get("RC_SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    try:
        private_key = serialization.load_pem_private_key(
            key_path.read_bytes(),
            # An empty value (a blank line in .env) means "unencrypted key":
            # cryptography rejects any password, even b"", for such a key.
            password=passphrase.encode("utf-8") if passphrase else None,
        )
    except (ValueError, TypeError) as exc:
        hint = (
            "The key appears to be encrypted; set RC_SNOWFLAKE_PRIVATE_KEY_PASSPHRASE."
            if not passphrase
            else "Check that RC_SNOWFLAKE_PRIVATE_KEY_PASSPHRASE is correct."
        )
        raise SessionError(f"Could not load private key from {key_path}. {hint}") from exc

    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def build_connect_params(settings: Settings, *, unattended: bool = False) -> dict[str, Any]:
    """Assemble connector parameters for the configured connection.

    Args:
        settings: Resolved configuration.
        unattended: When true, an interactive authenticator is an error rather
            than a warning. Set this for automations and CI.

    Raises:
        SessionError: If ``connections.toml`` is missing, the named connection is
            absent, or an interactive method is requested while unattended.
    """
    toml_path = _connections_toml_path()
    if not toml_path.is_file():
        raise SessionError(
            f"No Snowflake connection file at {toml_path}.\n"
            "Create one with:  snow connection add\n"
            "Credentials are deliberately not stored in this repository."
        )

    import tomllib

    try:
        connections = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise SessionError(f"Could not read {toml_path}: {exc}") from exc

    name = settings.connection_name
    if name not in connections:
        raise SessionError(
            f"Connection {name!r} is not defined in {toml_path}. "
            f"Available: {sorted(connections)}. "
            "Set conf/external.yml:snowflake.connection_name or RC_SNOWFLAKE_CONNECTION."
        )

    params: dict[str, Any] = {
        k: v for k, v in dict(connections[name]).items() if v not in (None, "")
    }

    # Topology always comes from config, never from connections.toml, so the
    # active profile -- not a developer's saved default -- decides which
    # database and warehouse are used.
    params["database"] = settings.db()
    params["warehouse"] = settings.obj(settings.warehouse("dev").name)
    params["role"] = settings.obj(settings.roles.admin)

    if key_bytes := _load_private_key(settings):
        params["private_key"] = key_bytes
        params["authenticator"] = "SNOWFLAKE_JWT"
        params.pop("password", None)

    authenticator = str(params.get("authenticator", "snowflake")).lower()
    if authenticator in _INTERACTIVE_AUTHENTICATORS:
        message = (
            f"Connection {name!r} uses the interactive authenticator "
            f"{authenticator!r}. Unattended runs (automations, CI) cannot "
            "complete it; configure key-pair auth and set "
            "RC_SNOWFLAKE_PRIVATE_KEY_PATH."
        )
        if unattended:
            raise SessionError(message)
        logger.warning(message)

    params.setdefault("client_session_keep_alive", True)
    # Tags every query this project runs, so usage is attributable in
    # ACCOUNT_USAGE without relying on QUERY_TAG discipline at the call site.
    params.setdefault("application", "risk_copilot")
    params.setdefault("session_parameters", {})["QUERY_TAG"] = (
        f"risk_copilot|profile={settings.profile}"
    )
    return params


def create_session(settings: Settings, *, unattended: bool = False) -> Any:
    """Return an open Snowpark ``Session``. The caller owns closing it.

    Prefer :func:`open_session`, which closes the session for you; this exists
    for ``doctor``, which hands one session to a sequence of later checks.
    """
    try:
        from snowflake.snowpark import Session
    except ImportError as exc:  # pragma: no cover
        raise SessionError(
            "snowflake-snowpark-python is not installed. Run: pip install -e .[dev]"
        ) from exc

    params = build_connect_params(settings, unattended=unattended)
    try:
        return Session.builder.configs(params).create()
    except Exception as exc:
        # The role, database and warehouse are pinned into the connect string so
        # the active profile decides the topology. Before the bootstrap stage has
        # run none of them exist yet, and Snowflake rejects the *login* rather
        # than the later USE statements -- which would make bootstrap
        # unreachable. Retry once on a bare connection so `deploy --stage
        # bootstrap` can create them; _use_context sets them afterwards.
        if not _is_missing_topology(exc):
            raise SessionError(_explain_connection_failure(exc, settings)) from exc
        logger.warning(
            "Connecting without role/database/warehouse: %s. This is expected "
            "before 'riskcopilot deploy --stage bootstrap' has created them.",
            str(exc).strip().splitlines()[0] if str(exc).strip() else exc,
        )
        bare = {k: v for k, v in params.items() if k not in ("role", "database", "warehouse")}
        try:
            return Session.builder.configs(bare).create()
        except Exception as retry_exc:
            raise SessionError(_explain_connection_failure(retry_exc, settings)) from retry_exc


@contextmanager
def open_session(settings: Settings, *, unattended: bool = False) -> Iterator[Any]:
    """Yield a Snowpark ``Session``, closing it on exit.

    Snowpark is preferred over the raw connector because DataFrame operations
    push down to Snowflake rather than pulling rows to the client -- which
    matters when the transaction table has hundreds of thousands of rows.
    """
    session = create_session(settings, unattended=unattended)

    try:
        yield session
    finally:
        try:
            session.close()
        except Exception:  # pragma: no cover - close failures must not mask errors
            logger.debug("Session close failed", exc_info=True)


def _is_missing_topology(exc: Exception) -> bool:
    """True when a login failed only because the role/database/warehouse is absent.

    Distinguished from a credential failure so that a genuinely bad key is never
    retried and silently reported as a bootstrap condition. 390189 is the role
    form; 390201 covers a missing database or warehouse.
    """
    text = str(exc)
    if "390189" in text or "390201" in text:
        return True
    lowered = text.lower()
    return "does not exist or not authorized" in lowered and (
        "role" in lowered or "database" in lowered or "warehouse" in lowered
    )


def _explain_connection_failure(exc: Exception, settings: Settings) -> str:
    """Turn a connector error into something actionable.

    Snowflake's authentication errors are numeric and opaque; mapping the ones
    we have actually hit saves a support round-trip.
    """
    text = str(exc)
    base = f"Could not connect using connection {settings.connection_name!r}."

    if "390190" in text or "SAML Identity Provider" in text:
        return (
            f"{base}\n"
            "Snowflake error 390190: the account's SAML identity provider parameter "
            "is misconfigured, so browser/SSO authentication cannot complete. This "
            "is an account-level setting, not a local one.\n"
            "Options:\n"
            "  1. Configure key-pair auth and set RC_SNOWFLAKE_PRIVATE_KEY_PATH "
            "(also required for unattended runs).\n"
            "  2. Have an ACCOUNTADMIN review SAML_IDENTITY_PROVIDER / the SSO "
            "security integration.\n"
            "  3. Use a connection that authenticates with a password or key pair.\n"
            f"Original error: {text}"
        )
    if "250001" in text or "Incorrect username or password" in text:
        return f"{base}\nCredentials were rejected. Re-run: snow connection add\n{text}"
    if "not authorized" in text.lower() or "does not exist" in text.lower():
        return (
            f"{base}\nThe role {settings.obj(settings.roles.admin)!r} may not exist yet, "
            "or may lack access to the configured database/warehouse. Run the "
            "bootstrap stage first: riskcopilot deploy --stage bootstrap\n"
            f"{text}"
        )
    return f"{base}\n{text}"


def execute(session: Any, sql: str, params: Sequence[Any] | None = None) -> list[Any]:
    """Run one statement and return its rows.

    Args:
        session: Snowpark session.
        sql: A single statement.
        params: Bind values. Always prefer these to string interpolation for
            *data*; identifiers cannot be bound and are instead validated by
            :func:`risk_copilot.config.validate_identifier`.
    """
    try:
        rows = session.sql(sql, params=list(params) if params else None).collect()
    except Exception as exc:
        snippet = " ".join(sql.split())[:300]
        raise SessionError(f"Statement failed: {snippet}\n{exc}") from exc
    return list(rows)


def fetch_scalar(session: Any, sql: str, params: Sequence[Any] | None = None) -> Any:
    """Run a statement and return the first column of the first row, or ``None``."""
    rows = execute(session, sql, params)
    if not rows:
        return None
    return rows[0][0]


def require_settings(settings: Settings | None) -> Settings:
    """Return ``settings``, or raise a clear error if it was never loaded."""
    if settings is None:
        raise ConfigError("Configuration was not loaded; call Settings.load() first")
    return settings
