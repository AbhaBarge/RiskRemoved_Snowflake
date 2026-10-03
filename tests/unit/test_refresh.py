"""``riskcopilot refresh``: which dynamic tables it refreshes, offline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from risk_copilot.config import load_settings
from risk_copilot.pipeline.refresh import refresh_all, targets

pytestmark = pytest.mark.unit


@pytest.fixture
def settings(repo_root: Path):  # type: ignore[no-untyped-def]
    return load_settings(repo_root=repo_root, profile="dev")


def test_only_deployed_configured_tables(settings) -> None:  # type: ignore[no-untyped-def]
    schema = settings.fq("curated")
    signals = settings.obj(settings.dt("signals_fast").name)
    daily = settings.obj(settings.dt("txn_daily_agg").name)
    live = {signals, daily.lower(), "SOMEONE_ELSES_DT"}
    assert targets(settings, live) == sorted([f"{schema}.{signals}", f"{schema}.{daily}"])


def test_nothing_deployed_refreshes_nothing(settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import risk_copilot.pipeline.refresh as module

    seen: list[str] = []
    monkeypatch.setattr(module, "execute", lambda session, sql, params=None: seen.append(sql) or [])
    assert refresh_all(settings, object()) == []
    assert len(seen) == 1 and seen[0].startswith("SHOW DYNAMIC TABLES")


def test_one_statement_refreshes_them_all(settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import risk_copilot.pipeline.refresh as module

    seen: list[str] = []
    live = [{"name": settings.obj(spec.name)} for spec in settings.dynamic_tables.values()][:3]

    def fake(session: Any, sql: str, params: Any = None) -> list[Any]:
        seen.append(sql)
        if sql.startswith("SHOW"):
            return live
        return [
            (t, "No new data") for t in sql.split("TABLE ", 1)[1].split(" REFRESH")[0].split(", ")
        ]

    monkeypatch.setattr(module, "execute", fake)
    done = refresh_all(settings, object())
    assert (
        len(seen) == 2
        and seen[1].startswith("ALTER DYNAMIC TABLE ")
        and seen[1].endswith(" REFRESH")
    )
    assert len(done) == 3
