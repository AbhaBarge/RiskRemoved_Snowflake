"""The dynamic tables of the structuring slice (Phase 3, step 5), rendered offline.

The config lag check (D32) judges the DESIGN graph. What Snowflake schedules is
the DEPLOYED graph, so the properties that decide whether anything refreshes at
all -- and whether a label or the cash desk can leak into CURATED -- are pinned
here against the rendered SQL.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from risk_copilot.config import load_settings
from risk_copilot.ids import signal_business_key
from risk_copilot.pipeline.deploy import Deployer, split_statements

pytestmark = pytest.mark.unit

_CREATE_DT = re.compile(
    r"CREATE OR ALTER DYNAMIC TABLE (\S+)\s+TARGET_LAG = ('[^']+'|DOWNSTREAM)", re.IGNORECASE
)


@pytest.fixture(params=["dev", "demo"])
def deployer(request: pytest.FixtureRequest, repo_root: Path) -> Deployer:
    return Deployer(load_settings(repo_root=repo_root, profile=request.param))


def _statements(deployer: Deployer) -> list[str]:
    return [
        s
        for p in deployer.stage_files("dynamic_tables")
        for s in split_statements(deployer.render(p))
    ]


def _code(statement: str) -> str:
    return "\n".join(line for line in statement.splitlines() if not line.strip().startswith("--"))


def _deployed(deployer: Deployer) -> dict[str, tuple[str, str]]:
    """Deployed dynamic table -> (lag, its SQL)."""
    out = {}
    for s in _statements(deployer):
        m = _CREATE_DT.search(_code(s))
        if m:
            out[m.group(1)] = (m.group(2), _code(s))
    return out


def test_the_slice_deploys_exactly_the_built_tables(deployer: Deployer) -> None:
    ctx = deployer.settings.sql_context()
    names = {
        f"{ctx['fq']['curated']}.{ctx['dt'][k]['name']}"
        for k in (
            "dim_party",
            "transactions_enriched",
            "txn_daily_agg",
            "party_relationship",
            "signals_fast",
        )
    }
    assert set(_deployed(deployer)) == names


def test_every_deployed_downstream_table_has_a_deployed_consumer(deployer: Deployer) -> None:
    """A DOWNSTREAM table nobody deployed reads never refreshes (P3, D34)."""
    deployed = _deployed(deployer)
    for name, (lag, _) in deployed.items():
        if lag.upper() != "DOWNSTREAM":
            continue
        readers = [
            n
            for n, (_, sql) in deployed.items()
            if n != name and re.search(rf"\b{re.escape(name)}\b", sql.split(" AS\n", 1)[-1])
        ]
        assert readers, f"{name} is DOWNSTREAM but no deployed dynamic table reads it"


def test_deployed_lags_are_the_configured_ones(deployer: Deployer) -> None:
    ctx = deployer.settings.sql_context()
    deployed = _deployed(deployer)
    for key, spec in ctx["dt"].items():
        name = f"{ctx['fq']['curated']}.{spec['name']}"
        if name in deployed:
            lag = spec["target_lag"]
            assert deployed[name][0] == (lag if lag == "DOWNSTREAM" else f"'{lag}'"), key


def test_every_table_is_incremental_on_the_pipeline_warehouse(deployer: Deployer) -> None:
    """INCREMENTAL, not AUTO: a construct that forces FULL must fail the deploy."""
    wh = deployer.settings.sql_context()["wh"]["pipeline"]
    for name, (_, sql) in _deployed(deployer).items():
        assert f"WAREHOUSE = {wh}" in sql and "REFRESH_MODE = INCREMENTAL" in sql, name


def test_nothing_in_curated_reads_ground_truth(deployer: Deployer) -> None:
    gt = deployer.settings.sql_context()["fq"]["ground_truth"]
    for s in _statements(deployer):
        assert gt not in _code(s)


def test_fund_flow_excludes_the_cash_desk(deployer: Deployer) -> None:
    """Obligation 6: both sides of a FUND_FLOW edge are customers."""
    ctx = deployer.settings.sql_context()
    (sql,) = [
        s
        for n, (_, s) in _deployed(deployer).items()
        if n.endswith(ctx["dt"]["party_relationship"]["name"])
    ]
    branch = sql.split("'FUND_FLOW'", 1)[1].split("UNION ALL", 1)[0]
    assert "NOT is_internal_account" in branch and "NOT is_internal_counterparty" in branch
    assert "party_id <> counterparty_party_id" in branch


def test_the_desk_is_flagged_from_the_configured_bank_id(deployer: Deployer) -> None:
    bank = deployer.settings.generation["internal_accounts"]["src_bank_id"]
    ctx = deployer.settings.sql_context()
    (sql,) = [
        s
        for n, (_, s) in _deployed(deployer).items()
        if n.endswith(ctx["dt"]["transactions_enriched"]["name"])
    ]
    assert f"a.src_bank_id = '{bank}'::VARCHAR      AS is_internal_account" in sql


def test_every_rule_is_in_its_dynamic_table(deployer: Deployer) -> None:
    detection = deployer.settings.detection
    ctx = deployer.settings.sql_context()
    deployed = _deployed(deployer)
    for rule in detection["rules"]:
        key = rule["dynamic_table"]
        assert key in ctx["dt"], f"{rule['rule_id']}: unknown dynamic_table {key!r}"
        sql = deployed[f"{ctx['fq']['curated']}.{ctx['dt'][key]['name']}"][1]
        assert sql.count(f"'{rule['rule_id']}'::VARCHAR") == 2  # rule_id and its key


def test_rule_sql_reads_every_threshold_from_the_clause_view(repo_root: Path) -> None:
    """No number in the rule; each role's clause id joined, not written."""
    deployer = Deployer(load_settings(repo_root=repo_root, profile="dev"))
    source = (deployer.sql_root / "rules" / "STRUCTURING_CROSS_ACCOUNT.sql").read_text("utf-8")
    code = re.sub(r"\{#.*?#\}", "", source, flags=re.S)
    code = "\n".join(line for line in code.splitlines() if not line.strip().startswith("--"))
    # The only numbers allowed are the output cast's precision and the +/- 1 of
    # the inclusive window end; every threshold is a clause column.
    numbers = set(re.findall(r"(?<![\w.])\d+(?:\.\d+)?", code.replace("NUMBER(20,4)", "")))
    assert numbers <= {"1"}, numbers
    rule = next(
        r
        for r in deployer.settings.detection["rules"]
        if r["rule_id"] == "STRUCTURING_CROSS_ACCOUNT"
    )
    for role in rule["clauses"]:
        assert f"r.clause.{role}" in code
    assert "V_POLICY_CLAUSE_CURRENT" in code and ";" not in source


def test_the_business_key_matches_the_python_contract(deployer: Deployer) -> None:
    """The router MERGEs on this key; SQL and ids.signal_business_key must agree."""
    ctx = deployer.settings.sql_context()
    (sql,) = [
        s
        for n, (_, s) in _deployed(deployer).items()
        if n.endswith(ctx["dt"]["signals_fast"]["name"])
    ]
    assert "'STRUCTURING_CROSS_ACCOUNT'::VARCHAR || '|' || UPPER(d.party_id) || '|'" in sql
    assert signal_business_key("STRUCTURING_CROSS_ACCOUNT", "P-1", "2022-08-26") == (
        "STRUCTURING_CROSS_ACCOUNT|P-1|2022-08-26"
    )


def test_lint_covers_included_rule_files(repo_root: Path, tmp_path: Path) -> None:
    import shutil

    root = tmp_path / "repo"
    shutil.copytree(repo_root / "conf", root / "conf")
    shutil.copytree(repo_root / "sql", root / "sql")
    (root / "data").mkdir()
    (root / "sql" / "rules" / "STRUCTURING_CROSS_ACCOUNT.sql").write_text(
        "SELECT 1 FROM RISK_COPILOT.RAW.X;\n", encoding="utf-8"
    )
    errors = Deployer(load_settings(repo_root=root, profile="dev")).lint()
    assert any("literal object name" in e for e in errors)
    assert any("a ';'" in e for e in errors)
