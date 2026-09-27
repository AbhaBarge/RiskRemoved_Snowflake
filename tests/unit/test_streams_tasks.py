"""Signal routing (Phase 3, step 6), rendered offline.

The live proof (M31) shows ROUTE_SIGNALS is idempotent. These tests pin what
the proof cannot see: the stream is never replaced (which would drop unrouted
changes), the task is triggered by it and resumed only where config says so,
and every routing value comes from conf/detection.yml.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from risk_copilot import ids
from risk_copilot.config import ConfigError, _check_routing, load_settings
from risk_copilot.pipeline.deploy import Deployer, split_statements

pytestmark = pytest.mark.unit


@pytest.fixture(params=["dev", "demo"])
def deployer(request: pytest.FixtureRequest, repo_root: Path) -> Deployer:
    return Deployer(load_settings(repo_root=repo_root, profile=request.param))


def _code(statement: str) -> str:
    """A statement without its comment lines, whitespace-normalised."""
    lines = [line for line in statement.splitlines() if not line.strip().startswith("--")]
    return " ".join(" ".join(lines).split())


def _statements(deployer: Deployer) -> list[str]:
    return [
        _code(s)
        for p in deployer.stage_files("streams_tasks")
        for s in split_statements(deployer.render(p))
    ]


def _names(deployer: Deployer) -> dict[str, str]:
    ctx = deployer.settings.sql_context()
    st = ctx["stream_task"]
    return {
        "stream": f"{ctx['fq']['curated']}.{st['signals_stream']}",
        "source": f"{ctx['fq']['curated']}.{ctx['dt']['signals_fast']['name']}",
        "proc": f"{ctx['fq']['evidence']}.{st['route_signals_procedure']}",
        "task": f"{ctx['fq']['evidence']}.{st['route_signals_task']}",
        "wh": ctx["wh"]["pipeline"],
    }


def _one(statements: list[str], prefix: str) -> str:
    found = [s for s in statements if s.startswith(prefix)]
    assert len(found) == 1, f"expected one statement starting {prefix!r}, got {len(found)}"
    return found[0]


def test_the_stream_is_created_once_on_the_signal_table(deployer: Deployer) -> None:
    n = _names(deployer)
    stmts = _statements(deployer)
    stream = _one(stmts, f"CREATE STREAM IF NOT EXISTS {n['stream']} ")
    assert f"ON DYNAMIC TABLE {n['source']} " in stream
    # OR REPLACE would reset the offset on every deploy and drop unrouted changes.
    assert not any("OR REPLACE STREAM" in s or "OR ALTER STREAM" in s for s in stmts)


def test_the_task_is_triggered_by_the_stream(deployer: Deployer) -> None:
    n = _names(deployer)
    task = _one(_statements(deployer), f"CREATE OR ALTER TASK {n['task']} ")
    assert f"WAREHOUSE = {n['wh']} " in task
    assert f"WHEN SYSTEM$STREAM_HAS_DATA('{n['stream']}')" in task
    assert "SCHEDULE" not in task  # D38: triggered, not polled
    assert f"CALL {n['proc']}(FALSE);" in task


def test_the_task_is_suspended_first_and_resumed_only_when_configured(
    deployer: Deployer,
) -> None:
    n = _names(deployer)
    stmts = _statements(deployer)
    suspend = stmts.index(f"ALTER TASK IF EXISTS {n['task']} SUSPEND")
    create = stmts.index(_one(stmts, f"CREATE OR ALTER TASK {n['task']} "))
    assert suspend < create
    resumes = [i for i, s in enumerate(stmts) if s == f"ALTER TASK {n['task']} RESUME"]
    wanted = deployer.settings.streams_tasks.resume_route_task
    assert resumes == ([len(stmts) - 1] if wanted else [])
    assert wanted is (deployer.settings.profile == "demo")


def test_there_is_no_schedule_to_configure(repo_root: Path) -> None:
    # D38 removed route_signals_schedule; a leftover key must fail the load
    # rather than suggest the task is polled.
    with pytest.raises((ConfigError, ValueError)):
        load_settings(
            repo_root=repo_root, profile="dev", streams_tasks={"route_signals_schedule": "1 minute"}
        )


def test_the_router_takes_version_and_severity_from_the_registry(deployer: Deployer) -> None:
    ctx = deployer.settings.sql_context()
    proc = _one(_statements(deployer), f"CREATE OR REPLACE PROCEDURE {_names(deployer)['proc']}(")
    registry = (
        "SELECT rule_id, rule_version, severity "
        f"FROM {ctx['fq']['governance']}.RULE_DEFINITION WHERE is_active"
    )
    assert registry in proc
    # The rule id appears only as a case_type_by_rule key, never as a severity.
    for rule_id in ctx["rule"]:
        assert proc.count(f"'{rule_id}'::VARCHAR") == proc.count(
            ctx["routing"]["case_type_by_rule"]
        )


def test_the_router_derives_ids_as_python_does(deployer: Deployer) -> None:
    proc = _one(_statements(deployer), f"CREATE OR REPLACE PROCEDURE {_names(deployer)['proc']}(")
    hex_ = ids.ROUTED_ID_HEX
    assert (
        f"'{ids.SIGNAL_ID_PREFIX}'::VARCHAR || LEFT(SHA2(b.signal_business_key, 256), {hex_})"
        in proc
    )
    assert (
        f"'{ids.CASE_ID_PREFIX}'::VARCHAR || "
        f"LEFT(SHA2(c.party_id || '|' || MIN(c.signal_business_key), 256), {hex_})" in proc
    )


def _render_with(repo_root: Path, **routing: Any) -> str:
    settings = load_settings(repo_root=repo_root, profile="dev")
    detection = copy.deepcopy(settings.detection)
    detection["routing"].update(routing)
    deployer = Deployer(settings.model_copy(update={"detection": detection}))
    return " ".join(_statements(deployer))


def test_every_routing_value_comes_from_config(repo_root: Path) -> None:
    base = _render_with(repo_root)
    changed = _render_with(
        repo_root,
        min_severity_for_case="HIGH",
        open_case_statuses=["OPEN", "ON_HOLD"],
        cross_domain_min_distinct_rules=3,
        case_type_by_rule={"STRUCTURING_CROSS_ACCOUNT": "FRAUD"},
    )
    for old, new in (
        ("ARRAY_POSITION('MEDIUM'::VARCHAR::VARIANT", "ARRAY_POSITION('HIGH'::VARCHAR::VARIANT"),
        ("'INVESTIGATING'::VARCHAR", "'ON_HOLD'::VARCHAR"),
        (">= 2::NUMBER(10,0)", ">= 3::NUMBER(10,0)"),
        ("'AML'::VARCHAR", "'FRAUD'::VARCHAR"),
    ):
        assert old in base and old not in changed, old
        assert new in changed, new


@pytest.fixture
def detection(repo_root: Path) -> dict[str, Any]:
    return copy.deepcopy(load_settings(repo_root=repo_root, profile="dev").detection)


def test_the_configured_routing_is_valid(detection: dict[str, Any]) -> None:
    _check_routing(detection)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("min_severity_for_case", "SEVERE", "min_severity_for_case"),
        ("severity_order", ["LOW", "LOW", "HIGH"], "distinct"),
        ("severity_order", [], "distinct"),
        ("open_case_statuses", [], "open_case_statuses"),
        ("cross_domain_min_distinct_rules", 1, ">= 2"),
        ("cross_domain_min_distinct_rules", True, ">= 2"),
        ("cross_domain_min_distinct_rules", "2", ">= 2"),
        ("case_type_by_rule", {}, "STRUCTURING_CROSS_ACCOUNT"),
        ("case_type_by_rule", {"STRUCTURING_CROSS_ACCOUNT": "CROSS_DOMAIN"}, "one of"),
    ],
)
def test_bad_routing_is_refused(
    detection: dict[str, Any], key: str, value: Any, message: str
) -> None:
    detection["routing"][key] = value
    with pytest.raises(ConfigError, match=message):
        _check_routing(detection)
