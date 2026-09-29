"""The ``policy`` seed (Phase 3, step 3).

``conf/detection.yml`` is the only place a threshold value may enter Snowflake,
and it enters as rows of GOVERNANCE tables. These tests pin the three things
that make that safe: every value is escaped by one function, an ungoverned row
is refused before anything renders, and no other template carries a threshold.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from risk_copilot.config import ConfigError, load_settings
from risk_copilot.pipeline.deploy import STAGE_ORDER, Deployer, split_statements
from risk_copilot.policy import policy_seed, rule_sql_hash, rule_sql_path, sql_literal

pytestmark = pytest.mark.unit

SQL_ROOT = Path(__file__).resolve().parents[2] / "sql"


@pytest.fixture
def detection(repo_root: Path) -> dict[str, Any]:
    return copy.deepcopy(load_settings(repo_root=repo_root, profile="dev").detection)


# --------------------------------------------------------------------------- #
# Literals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "sql_type", "expected"),
    [
        ("O'Brien", "VARCHAR", "'O''Brien'::VARCHAR"),
        ("C:\\path", "VARCHAR", "'C:\\\\path'::VARCHAR"),
        (None, "NUMBER(20,4)", "NULL::NUMBER(20,4)"),
        (0.999, "NUMBER(20,4)", "0.999::NUMBER(20,4)"),  # not 0.99899999...
        (1000000, "NUMBER(20,4)", "1000000::NUMBER(20,4)"),
        (True, "BOOLEAN", "TRUE"),
        ("2005-07-01", "DATE", "'2005-07-01'::DATE"),
        (dt.date(2005, 7, 1), "DATE", "'2005-07-01'::DATE"),
        (["A", "B'C"], "ARRAY", "ARRAY_CONSTRUCT('A'::VARCHAR, 'B''C'::VARCHAR)"),
    ],
)
def test_sql_literal(value: Any, sql_type: str, expected: str) -> None:
    assert sql_literal(value, sql_type) == expected


@pytest.mark.parametrize(
    ("value", "sql_type"),
    [("yes", "BOOLEAN"), (True, "NUMBER(10,0)"), ("abc", "NUMBER(10,0)"), ("A", "ARRAY")],
)
def test_sql_literal_rejects_a_value_of_the_wrong_type(value: Any, sql_type: str) -> None:
    with pytest.raises(ConfigError):
        sql_literal(value, sql_type)


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #


def test_one_row_per_config_entry(detection: dict[str, Any]) -> None:
    seed = policy_seed(detection, SQL_ROOT)
    for table, block in (
        ("clauses", "clauses"),
        ("typologies", "typologies"),
        ("rules", "rules"),
        ("channels", "channels"),
    ):
        assert len(seed[table]) == len(detection[block])
    assert len(seed["clauses"]) == 15


def test_clause_rows_keep_provenance_and_exact_thresholds(detection: dict[str, Any]) -> None:
    rows = {r["clause_id"]: r for r in policy_seed(detection, SQL_ROOT)["clauses"]}
    ctr = next(c for c in detection["clauses"] if c["clause_id"] == "CTR_CASH_THRESHOLD")
    row = rows["'CTR_CASH_THRESHOLD'::VARCHAR"]
    assert row["threshold_lower"] == f"{Decimal(str(ctr['threshold_lower']))}::NUMBER(20,4)"
    assert row["source_document"] == sql_literal(ctr["provenance"]["source_document"], "VARCHAR")
    assert row["clause_version"] == "1::NUMBER(10,0)"


def test_only_built_rules_are_registered(detection: dict[str, Any]) -> None:
    """A registry row for a rule with no SQL would let a finding cite logic nobody wrote."""
    assert [r["rule_id"] for r in detection["rules"]] == ["STRUCTURING_CROSS_ACCOUNT"]
    (rule,) = policy_seed(detection, SQL_ROOT)["rules"]
    assert rule["severity"] == sql_literal(
        detection["severity"]["STRUCTURING_CROSS_ACCOUNT"], "VARCHAR"
    )
    expr = rule["window_bucket_expr"]
    assert "1970-01-01" in expr and "STRUCTURING_WINDOW_DAYS" in expr
    assert not re.search(r"/ \d", expr)  # the window length is read, never written


def test_every_rule_is_governed(detection: dict[str, Any]) -> None:
    clause_ids = {c["clause_id"] for c in detection["clauses"]}
    typology_ids = {t["typology_id"] for t in detection["typologies"]}
    for rule in detection["rules"]:
        assert rule["clauses"] and set(rule["clauses"].values()) <= clause_ids
        assert rule["typology_id"] in typology_ids
        assert rule["rule_id"] in detection["severity"]


def test_rule_row_is_unchanged_by_the_role_keyed_clauses(detection: dict[str, Any]) -> None:
    """The live row was seeded from a flat clause_ids list; the drift check
    compares it with this one, so the order must be the one deployed."""
    (rule,) = policy_seed(detection, SQL_ROOT)["rules"]
    assert rule["clause_ids"] == (
        "ARRAY_CONSTRUCT('CTR_CASH_THRESHOLD'::VARCHAR, 'STRUCTURING_BAND'::VARCHAR, "
        "'STRUCTURING_MIN_ACCOUNTS'::VARCHAR, 'STRUCTURING_WINDOW_DAYS'::VARCHAR)"
    )


def test_rule_sql_hash_is_the_rule_files_hash(detection: dict[str, Any], tmp_path: Path) -> None:
    (rule,) = policy_seed(detection, SQL_ROOT)["rules"]
    path = rule_sql_path(SQL_ROOT, "STRUCTURING_CROSS_ACCOUNT")
    assert rule["rule_sql_hash"] == sql_literal(rule_sql_hash(path), "VARCHAR")
    # A CRLF checkout of the same file hashes identically.
    crlf = tmp_path / "rule.sql"
    crlf.write_bytes(path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
    assert rule_sql_hash(crlf) == rule_sql_hash(path)


def test_a_rule_with_no_sql_file_is_refused(detection: dict[str, Any], tmp_path: Path) -> None:
    """Only built rules are registered, mechanically."""
    with pytest.raises(ConfigError, match="has no detection SQL"):
        policy_seed(detection, tmp_path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["clauses"][0]["provenance"].update(source_document=" "), "provenance"),
        (lambda d: d["rules"][0].update(clauses={}), "implements no clause"),
        (lambda d: d["rules"][0]["clauses"].update(ctr="NO_SUCH"), "unknown clause"),
        (lambda d: d["rules"][0].update(clauses=["CTR_CASH_THRESHOLD"]), "role -> clause_id"),
        (lambda d: d["rules"][0].update(typology_id="TYP_NONE"), "unknown typology"),
        (lambda d: d["severity"].pop("STRUCTURING_CROSS_ACCOUNT"), "no severity"),
        (lambda d: d["rules"][0].update(detection_grain="DAILY"), "detection_grain"),
        (lambda d: d["rules"][0]["clauses"].pop("window"), "needs a `window` clause"),
        (lambda d: d["channels"].append(dict(d["channels"][0])), "duplicate channel_code"),
        (lambda d: d["clauses"].append(dict(d["clauses"][0])), "duplicate clause"),
        (lambda d: d.update(rules=[]), "`rules` is empty"),
    ],
)
def test_an_ungoverned_seed_is_refused(
    detection: dict[str, Any], mutate: Any, message: str
) -> None:
    mutate(detection)
    with pytest.raises(ConfigError, match=message):
        policy_seed(detection, SQL_ROOT)


def test_every_channel_generation_can_emit_is_catalogued(repo_root: Path) -> None:
    """REF_CHANNEL.is_cash decides CTR applicability; a leg on an uncatalogued
    channel would silently join to nothing."""
    settings = load_settings(repo_root=repo_root, profile="dev")
    generation = settings.generation
    emitted = set(generation["channel_map"].values())
    if generation.get("channel_default"):
        emitted.add(generation["channel_default"])
    emitted.add(generation["injection"]["structuring"]["channel"])
    catalogued = {c["channel_code"]: c for c in settings.detection["channels"]}
    assert emitted <= set(catalogued)
    assert catalogued[generation["injection"]["structuring"]["channel"]]["is_cash"] is True


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def test_seed_renders_one_select_per_row(repo_root: Path) -> None:
    deployer = Deployer(load_settings(repo_root=repo_root, profile="dev"))
    (path,) = deployer.stage_files("policy")
    sql = deployer.render(path)
    detection = deployer.settings.detection
    for block, key in (("clauses", "clause_id"), ("channels", "channel_code")):
        for entry in detection[block]:
            assert sql.count(f"SELECT '{entry[key]}'::VARCHAR AS {key}") == 1
    # Each SELECT is on its own line: a lost newline once fused rows together.
    assert "\nUNION ALL\nSELECT " in sql
    assert not re.search(r"\w(UNION ALL|SELECT )", sql.replace("\n", " \n"))


def test_no_other_template_carries_a_threshold(repo_root: Path) -> None:
    """Thresholds reach Snowflake only as seed rows; detection SQL joins to them."""
    deployer = Deployer(load_settings(repo_root=repo_root, profile="dev"))
    values = set()
    for clause in deployer.settings.detection["clauses"]:
        for bound in ("threshold_lower", "threshold_upper"):
            if clause.get(bound) is not None:
                text = str(Decimal(str(clause[bound])))
                if len(text.replace(".", "")) >= 4:  # "2" or "30" would match anything
                    values.add(text)
    assert values  # the CTR threshold at least
    # Every stage but policy, and the verify queries, which join the same view.
    paths = [p for key, _ in STAGE_ORDER if key != "policy" for p in deployer.stage_files(key)]
    assert deployer.verify_files()
    for path in paths + deployer.verify_files():
        for statement in split_statements(deployer.render(path)):
            code = " ".join(
                line for line in statement.splitlines() if not line.strip().startswith("--")
            )
            for value in values:
                assert not re.search(rf"(?<![\w.]){re.escape(value)}(?![\w.])", code), (
                    f"{path.name} carries threshold {value}"
                )
