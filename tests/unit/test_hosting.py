"""Hosting the app on Streamlit Community Cloud, and the demo sign-in.

The hosted app runs from the repository as committed, so what it reads there
(app_config.json, requirements.txt, the theme) must not drift from conf/ and
from what Streamlit in Snowflake installs. Its service role must be able to
run every case tool and nothing on GROUND_TRUTH.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from risk_copilot.config import load_settings
from risk_copilot.pipeline.app import AppError, app_config, secrets_toml
from risk_copilot.pipeline.deploy import Deployer, split_statements


def test_the_committed_app_config_matches_conf(repo_root: Path) -> None:
    # Community Cloud cannot run `riskcopilot app render`: it reads the file in git.
    committed = json.loads((repo_root / "app" / "app_config.json").read_text("utf-8"))
    assert committed == app_config(load_settings(repo_root=repo_root, profile="dev")), (
        "app/app_config.json is stale: run `riskcopilot -p dev app render` and commit it"
    )


def test_requirements_match_the_streamlit_in_snowflake_dependencies(repo_root: Path) -> None:
    project = tomllib.loads((repo_root / "app" / "pyproject.toml").read_text("utf-8"))
    lines = (repo_root / "app" / "requirements.txt").read_text("utf-8").splitlines()
    requirements = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    assert requirements == project["project"]["dependencies"]


def test_the_root_theme_is_the_app_theme(repo_root: Path) -> None:
    # Streamlit reads .streamlit/ from the working directory: the repository root on
    # Community Cloud, app/ for `riskcopilot app run` and in Snowflake.
    root = (repo_root / ".streamlit" / "config.toml").read_text("utf-8")
    app = (repo_root / "app" / ".streamlit" / "config.toml").read_text("utf-8")
    assert tomllib.loads(root) == tomllib.loads(app)


def test_secrets_are_never_committable(repo_root: Path) -> None:
    ignored = (repo_root / ".gitignore").read_text("utf-8").splitlines()
    assert {"secrets.toml", "*.streamlit-secrets.toml", "*.p8"} <= set(ignored)


def test_every_app_file_compiles(repo_root: Path) -> None:
    # The app is never imported by the suite (it needs a live session), so a
    # syntax error would otherwise first show up on the hosted URL.
    for path in (repo_root / "app").rglob("*.py"):
        compile(path.read_text("utf-8"), str(path), "exec")


# --------------------------------------------------------------------------- #
# The public app role
# --------------------------------------------------------------------------- #


def _signatures(sql: str) -> dict[str, tuple[str, ...]]:
    """Procedure name -> argument types, from CREATE PROCEDURE statements."""
    out: dict[str, tuple[str, ...]] = {}
    for m in re.finditer(
        r"CREATE OR REPLACE PROCEDURE\s+([\w.]+)\s*\((.*?)\)\s*RETURNS", sql, re.S
    ):
        args = [a.split()[1].upper() for a in m.group(2).split(",") if a.strip()]
        out[m.group(1).upper()] = tuple(args)
    return out


def test_the_public_role_can_run_every_case_tool(repo_root: Path) -> None:
    deployer = Deployer(load_settings(repo_root=repo_root, profile="dev"))
    procs: dict[str, tuple[str, ...]] = {}
    for path in deployer.stage_files("procedures"):
        procs |= _signatures(deployer.render(path))
    grants = {}
    for path in deployer.stage_files("app_access"):
        for m in re.finditer(
            r"GRANT USAGE ON PROCEDURE ([\w.]+)\(([^)]*)\)", deployer.render(path)
        ):
            grants[m.group(1).upper()] = tuple(t.strip().upper() for t in m.group(2).split(","))
    assert grants, "the app_access stage grants no procedure"
    # Each grant names a real signature; a mismatch fails only at deploy time otherwise.
    for name, types in grants.items():
        assert procs.get(name) == types, f"{name}{types} does not match {procs.get(name)}"
    tools = {
        deployer.settings.fq("evidence", t).upper() for t in deployer.settings.case_tools.values()
    }
    assert tools & set(procs) <= set(grants), "a case tool the app calls is not granted"


def test_the_public_role_gets_nothing_on_ground_truth(repo_root: Path) -> None:
    deployer = Deployer(load_settings(repo_root=repo_root, profile="dev"))
    gt = deployer.settings.fq("ground_truth").upper()
    for path in deployer.stage_files("app_access"):
        for statement in split_statements(deployer.render(path)):
            if statement.lstrip().upper().startswith("GRANT"):
                assert gt not in statement.upper(), statement


def test_secrets_refuse_values_that_would_break_the_file() -> None:
    good = {"account": "A", "user": "U", "role": "R", "warehouse": "W", "database": "D"}
    text = secrets_toml(**good, pem=b"-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----")
    parsed = tomllib.loads(text)["snowflake"]
    assert parsed["user"] == "U" and parsed["private_key_pem"].strip().startswith("-----BEGIN")
    with pytest.raises(AppError, match="bad role"):
        secrets_toml(**{**good, "role": 'R"\nx = "y'}, pem=b"k")


# --------------------------------------------------------------------------- #
# Demo sign-in (app/lib/auth.py)
# --------------------------------------------------------------------------- #


@pytest.fixture
def auth(repo_root: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.syspath_prepend(str(repo_root / "app"))
    from lib import auth as module

    monkeypatch.setattr(module, "st", SimpleNamespace(session_state={}))
    return module


def test_maker_and_checker_are_different_people(auth) -> None:  # type: ignore[no-untyped-def]
    makers = {k for k, p in auth.PERSONAS.items() if p["can_investigate"]}
    checkers = {k for k, p in auth.PERSONAS.items() if p["can_attest"]}
    assert makers and checkers and not makers & checkers


def test_sign_in_checks_the_password(auth) -> None:  # type: ignore[no-untyped-def]
    assert not auth.sign_in("analyst", "wrong")
    assert auth.current() is None
    assert auth.sign_in("analyst", auth.DEMO_PASSWORDS["analyst"])
    assert auth.can("can_investigate") and not auth.can("can_attest")
    auth.sign_out(prefill="officer")
    assert auth.current() is None
    assert auth.st.session_state["login_user"] == "officer"
    assert auth.sign_in("officer", auth.st.session_state["login_password"])
    assert auth.can("can_attest")
