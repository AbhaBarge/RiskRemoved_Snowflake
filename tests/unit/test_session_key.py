"""Key-pair loading: an empty passphrase must mean "unencrypted", not b"".

Regression: a blank ``RC_SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=`` line in ``.env``
was once passed to cryptography as ``b""``, which it rejects for an
unencrypted key, so a working login broke.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from risk_copilot.config import load_settings
from risk_copilot.session import SessionError, _load_private_key

pytestmark = pytest.mark.unit


def _write_key(path: Path, passphrase: bytes | None) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encryption = (
        serialization.BestAvailableEncryption(passphrase)
        if passphrase
        else serialization.NoEncryption()
    )
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
    )


@pytest.fixture
def settings(repo_root: Path):
    return load_settings(repo_root=repo_root, profile="dev")


def test_unencrypted_key_with_empty_passphrase_loads(tmp_path, monkeypatch, settings) -> None:
    key = tmp_path / "k.p8"
    _write_key(key, None)
    monkeypatch.setenv("RC_SNOWFLAKE_PRIVATE_KEY_PATH", str(key))
    monkeypatch.setenv("RC_SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "")
    assert _load_private_key(settings)


def test_encrypted_key_needs_the_right_passphrase(tmp_path, monkeypatch, settings) -> None:
    key = tmp_path / "k.p8"
    _write_key(key, b"s3cret")
    monkeypatch.setenv("RC_SNOWFLAKE_PRIVATE_KEY_PATH", str(key))
    monkeypatch.setenv("RC_SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "s3cret")
    assert _load_private_key(settings)
    monkeypatch.setenv("RC_SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "")
    with pytest.raises(SessionError, match="encrypted"):
        _load_private_key(settings)
