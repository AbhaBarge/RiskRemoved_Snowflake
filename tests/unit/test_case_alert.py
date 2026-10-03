"""The case-event alert (sql/10_automations): rendered only with a recipient.

The recipient comes from the environment, so these tests set it and check the
stage renders the integration, the procedure and a suspended alert; unset, the
stage renders nothing; a malformed address is refused before anything renders.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from risk_copilot.config import ConfigError, load_settings
from risk_copilot.pipeline.deploy import Deployer, split_statements

pytestmark = pytest.mark.unit

ENV = "RC_ALERT_EMAIL"


def _render(repo_root: Path) -> str:
    deployer = Deployer(load_settings(repo_root=repo_root, profile="dev"))
    (path,) = deployer.stage_files("automations")
    return deployer.render(path)


def test_without_a_recipient_the_stage_deploys_nothing(repo_root: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert split_statements(_render(repo_root)) == []


def test_with_a_recipient_it_creates_a_suspended_serverless_alert(
    repo_root: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv(ENV, "desk@example.com")
    sql = _render(repo_root)
    assert "ALLOWED_RECIPIENTS = ('desk@example.com')" in sql
    assert "CALL SYSTEM$SEND_EMAIL('RC_NOTIFICATION_INT_DEV', 'desk@example.com'" in sql
    alert = sql[sql.index("CREATE OR REPLACE ALERT") :]
    assert "SCHEDULE = '1 MINUTE'" in alert
    assert "WAREHOUSE" not in alert  # serverless
    assert "LAST_SUCCESSFUL_SCHEDULED_TIME" in alert and "SCHEDULED_TIME()" in alert
    assert "RESUME" not in sql  # spends nothing until someone resumes it


def test_the_email_carries_ids_not_customer_data(repo_root: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv(ENV, "desk@example.com")
    sql = _render(repo_root)
    body = sql[sql.index("CREATE OR REPLACE PROCEDURE") : sql.index("CREATE OR REPLACE ALERT")]
    for column in ("full_name", "amount_inr", "pan", "dim_party"):
        assert column not in body.lower()


@pytest.mark.parametrize("bad", ["not-an-email", "a@b", "x'@y.com", "a@b.com, c@d.com"])
def test_a_malformed_recipient_is_refused(repo_root: Path, monkeypatch: Any, bad: str) -> None:
    monkeypatch.setenv(ENV, bad)
    with pytest.raises(ConfigError, match=ENV):
        _render(repo_root)
