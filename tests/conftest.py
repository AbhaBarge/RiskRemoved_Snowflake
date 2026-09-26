"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def repo_root() -> Path:
    """Repository root, so tests read the real ``conf/`` rather than a fixture copy.

    Testing against the actual configuration is the point: these tests are as
    much a check on ``conf/`` staying coherent as on the loader's logic.
    """
    return Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clear_rc_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ``RC_*`` variables so a developer's shell cannot alter results.

    Without this, someone with ``RC_PROFILE=demo`` exported would see different
    outcomes from CI for the same code.
    """
    for name in list(os.environ):
        if name.startswith("RC_"):
            monkeypatch.delenv(name, raising=False)
