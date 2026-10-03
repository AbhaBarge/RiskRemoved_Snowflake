"""``riskcopilot refresh`` -- every deployed dynamic table, at one data timestamp.

The operator's refresh during a live demo. ``verify`` refreshes only the signal
tables and what is upstream of them, so a table the app reads that is not
upstream of a signal (TXN_DAILY_AGG: the cash-deposit counts) kept its old rows
for the whole rehearsal (build log M49). One ``ALTER DYNAMIC TABLE a, b, ...
REFRESH`` refreshes them all, and their upstream tables, as one pipeline.

Only configured tables that are deployed are refreshed: a configured but unbuilt
table is skipped, and nothing outside ``dynamic_tables`` is touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from risk_copilot.config import Settings, validate_identifier
from risk_copilot.session import execute


@dataclass(frozen=True)
class Refreshed:
    name: str
    statistics: str


def targets(settings: Settings, live: set[str]) -> list[str]:
    """Fully qualified names of the configured dynamic tables that are deployed, sorted."""
    schema = settings.fq("curated")
    names = {settings.obj(spec.name) for spec in settings.dynamic_tables.values()}
    return [
        f"{schema}.{validate_identifier(n, what='Dynamic table name')}"
        for n in sorted(names & {n.upper() for n in live})
    ]


def refresh_all(settings: Settings, session: Any) -> list[Refreshed]:
    """Refresh every deployed dynamic table in one statement. Cannot run in a transaction."""
    live = {
        str(r["name"])
        for r in execute(session, f"SHOW DYNAMIC TABLES IN SCHEMA {settings.fq('curated')}")
    }
    tables = targets(settings, live)
    if not tables:
        return []
    rows = execute(session, f"ALTER DYNAMIC TABLE {', '.join(tables)} REFRESH")
    return [Refreshed(str(r[0]), str(r[1])) for r in rows]
