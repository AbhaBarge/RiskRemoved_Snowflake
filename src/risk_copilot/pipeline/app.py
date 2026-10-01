"""``riskcopilot app`` -- render, run and deploy the Streamlit-in-Snowflake app.

The app under ``app/`` is a thin client: it reads tables and views and calls
procedures. It cannot import this package (Streamlit in Snowflake has only the
files we upload, and the trial account has no external access to install
anything), so every object name reaches it through ``app/app_config.json``,
rendered here from the same Settings the SQL stages use.

Deploy is plain SQL, no Snowflake CLI: ``PUT`` the files to a stage, then
``CREATE OR REPLACE STREAMLIT ... FROM`` that stage on the container runtime.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from risk_copilot.config import ConfigError, Schemas, Settings, validate_identifier
from risk_copilot.pipeline.verify import pin_path
from risk_copilot.session import execute

log = logging.getLogger(__name__)

APP_DIR = "app"
MAIN_FILE = "streamlit_app.py"
CONFIG_FILE = "app_config.json"
#: What gets uploaded. Anything else under app/ (caches, notes) stays local.
UPLOAD_SUFFIXES = frozenset({".py", ".toml", ".json", ".css", ".ttf", ".png", ".svg"})
_SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})

CONFIRMED = "CONFIRMED_BY_DETECTION"


class AppError(RuntimeError):
    """The app could not be rendered or deployed."""


def app_root(settings: Settings) -> Path:
    return settings.repo_root / APP_DIR


def _hero(settings: Settings) -> dict[str, Any] | None:
    """The verified hero, or ``None`` unless detection confirmed it.

    Read from the file ``verify`` writes, never from the generation pin: the
    app must only call a party the hero once Snowflake has proven it.
    """
    try:
        path = pin_path(settings, "demo_entity_verified_file")
    except KeyError:
        return None
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if data.get("status") != CONFIRMED or data.get("profile") != settings.profile:
        return None
    case_ids = sorted(
        {str(c) for rule in (data.get("rules") or {}).values() for c in rule.get("case_ids", [])}
    )
    return {"party_id": str(data["party_id"]), "case_ids": case_ids}


def app_config(settings: Settings) -> dict[str, Any]:
    """Every name the app needs, resolved for this profile.

    Tables are not listed: they are unsuffixed in every profile (as in the DDL),
    so the app qualifies them with ``schema``. Dynamic tables, views and
    procedures carry the profile suffix and are listed fully qualified.
    """
    return {
        "profile": settings.profile,
        "title": settings.app.title,
        "database": settings.db(),
        "schema": {key: settings.fq(key) for key in Schemas.model_fields},
        "dt": {
            key: settings.fq("curated", spec.name) for key, spec in settings.dynamic_tables.items()
        },
        "view": {key: settings.fq("curated", name) for key, name in settings.views.items()},
        "procedure": {
            "route_signals": settings.fq(
                "evidence", settings.streams_tasks.route_signals_procedure
            ),
            **{
                key: settings.fq("evidence", name)
                for key, name in settings.case_tools.items()
                if key in ("gather_evidence", "draft_str", "review_str", "attest_str")
            },
        },
        "role": {"compliance_officer": settings.obj(settings.roles.compliance_officer)},
        "hero": _hero(settings),
    }


def render_app_config(settings: Settings) -> Path:
    """Write ``app/app_config.json``; deterministic for the same configuration."""
    root = app_root(settings)
    if not (root / MAIN_FILE).is_file():
        raise AppError(f"No app at {root / MAIN_FILE}")
    out = root / CONFIG_FILE
    out.write_text(json.dumps(app_config(settings), indent=2, sort_keys=True) + "\n", "utf-8")
    return out


def app_files(root: Path) -> list[Path]:
    """Files to upload, relative to ``root``, in a stable order."""
    files = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or _SKIP_DIRS.intersection(rel.parts):
            continue
        if path.suffix.lower() in UPLOAD_SUFFIXES:
            files.append(rel)
    if Path(MAIN_FILE) not in files:
        raise AppError(f"{MAIN_FILE} missing from {root}")
    return files


# --------------------------------------------------------------------------- #
# Deploy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AppDeployResult:
    fq_name: str
    stage: str
    compute_pool: str
    warehouse: str
    files: tuple[str, ...]
    url: str | None


def _first(rows: Sequence[Any], column: str) -> Any:
    return rows[0][column] if rows else None


class AppDeployer:
    """Upload ``app/`` and (re)create the STREAMLIT object, as the admin role."""

    def __init__(
        self, settings: Settings, session: Any, *, echo: Callable[[str], None] = log.info
    ) -> None:
        self.settings = settings
        self.session = session
        self.echo = echo
        spec = settings.app
        self.fq_name = settings.fq(spec.schema_key, spec.name)
        self.stage = settings.fq(spec.schema_key, spec.stage)
        self.warehouse = settings.obj(settings.warehouse(spec.warehouse).name)

    def sql(self, text: str, params: Sequence[Any] | None = None) -> list[Any]:
        return execute(self.session, text, params)

    def compute_pool(self) -> str:
        """The configured pool, else the account default; refused if not visible."""
        pool = self.settings.app.compute_pool
        if not pool:
            rows = self.sql("SHOW PARAMETERS LIKE 'DEFAULT_STREAMLIT_COMPUTE_POOL' IN ACCOUNT")
            pool = str(_first(rows, "value") or _first(rows, "default") or "")
        if not pool:
            raise AppError(
                "No compute pool: set conf/base.yml:app.compute_pool, or the account "
                "parameter DEFAULT_STREAMLIT_COMPUTE_POOL."
            )
        pool = validate_identifier(pool, what="Compute pool")
        visible = {str(r["name"]).upper() for r in self.sql("SHOW COMPUTE POOLS")}
        if pool not in visible:
            raise AppError(
                f"Compute pool {pool} is not visible to this role; visible: {sorted(visible)}"
            )
        return pool

    def upload(self, root: Path, files: Sequence[Path]) -> None:
        self.sql(
            f"CREATE STAGE IF NOT EXISTS {self.stage} "
            "COMMENT = 'Streamlit app source, replaced on every riskcopilot app deploy.'"
        )
        # A file deleted locally must not survive on the stage.
        self.sql(f"REMOVE @{self.stage}")
        for rel in files:
            target = f"@{self.stage}" + (f"/{rel.parent.as_posix()}" if rel.parent.parts else "")
            self.session.file.put(
                str((root / rel).resolve()), target, auto_compress=False, overwrite=True
            )
            self.echo(f"  put {rel.as_posix()}")

    def create(self, pool: str) -> None:
        spec = self.settings.app
        self.sql(
            f"CREATE OR REPLACE STREAMLIT {self.fq_name}\n"
            f"  FROM '@{self.stage}'\n"
            f"  MAIN_FILE = '{MAIN_FILE}'\n"
            f"  QUERY_WAREHOUSE = {self.warehouse}\n"
            f"  RUNTIME_NAME = '{spec.runtime_name}'\n"
            f"  COMPUTE_POOL = {pool}\n"
            f"  TITLE = '{spec.title}'\n"
            "  COMMENT = 'Risk & Compliance Copilot demo surface (riskcopilot app deploy).'"
        )
        # Without a live version only the owner, visiting in Snowsight, can open it.
        self.sql(f"ALTER STREAMLIT {self.fq_name} ADD LIVE VERSION FROM LAST")

    def confirm(self) -> str | None:
        """The object must exist after deploy; returns its Snowsight URL."""
        schema = self.settings.fq(self.settings.app.schema_key)
        name = self.settings.obj(self.settings.app.name)
        rows = self.sql(f"SHOW STREAMLITS LIKE '{name}' IN SCHEMA {schema}")
        if len(rows) != 1:
            raise AppError(f"Deploy reported success but SHOW STREAMLITS finds {len(rows)} rows")
        names = self.sql(
            "SELECT CURRENT_ORGANIZATION_NAME() AS org, CURRENT_ACCOUNT_NAME() AS acct"
        )
        if not names or not names[0]["ORG"]:
            return None
        org, acct = str(names[0]["ORG"]).lower(), str(names[0]["ACCT"]).lower()
        return f"https://app.snowflake.com/{org}/{acct}/#/streamlit-apps/{self.fq_name}"

    def run(self) -> AppDeployResult:
        root = app_root(self.settings)
        render_app_config(self.settings)
        files = app_files(root)
        pool = self.compute_pool()
        self.echo(f"Uploading {len(files)} files to @{self.stage}")
        self.upload(root, files)
        self.echo(f"Creating {self.fq_name} on {pool}, queries on {self.warehouse}")
        self.create(pool)
        url = self.confirm()
        return AppDeployResult(
            fq_name=self.fq_name,
            stage=self.stage,
            compute_pool=pool,
            warehouse=self.warehouse,
            files=tuple(f.as_posix() for f in files),
            url=url,
        )


def deploy_app(settings: Settings, session: Any, **kwargs: Any) -> AppDeployResult:
    try:
        return AppDeployer(settings, session, **kwargs).run()
    except ConfigError as exc:
        raise AppError(str(exc)) from exc
