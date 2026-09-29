"""``riskcopilot verify`` (Phase 3, step 7), offline.

The session is a recording fake that answers each verify query from a script,
so these tests pin what Python does with the answers: it trusts the SQL's own
judgement (``within_bounds``, ``detected``) rather than re-deriving it, writes the
hero result beside the pin and never over it, and never routes.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from risk_copilot.config import load_settings
from risk_copilot.generation import eval_grain
from risk_copilot.generation.demo_entity import STATUS as PROVISIONAL
from risk_copilot.generation.writer import write_demo_entity
from risk_copilot.pipeline import verify as verify_mod
from risk_copilot.pipeline.deploy import Deployer
from risk_copilot.pipeline.verify import (
    CONFIRMED,
    REJECTED,
    Verifier,
    VerifyError,
    read_pin,
)

pytestmark = pytest.mark.unit

RULE = "STRUCTURING_CROSS_ACCOUNT"
PARTY = "P-1"
KEY = f"{RULE}|{PARTY}|2022-08-26"


# --------------------------------------------------------------------------- #
# Settings with the pin and its result under tmp_path, and a fake session
# --------------------------------------------------------------------------- #


def _settings(repo_root: Path, tmp_path: Path, profile: str = "dev") -> Any:
    settings = load_settings(repo_root=repo_root, profile=profile)
    output = settings.generation["output"]
    # An absolute path wins over repo_root in pin_path's join: nothing touches conf/.
    output["demo_entity_file"] = str(tmp_path / "demo_entity.{profile}.yml")
    output["demo_entity_verified_file"] = str(tmp_path / "demo_entity.{profile}.verified.yml")
    return settings


def _pin(settings: Any, **changes: Any) -> dict[str, Any]:
    pin = {
        "status": PROVISIONAL,
        "profile": settings.profile,
        "generation_seed": int(settings.generation["seed"]),
        "party_id": PARTY,
        "full_name": "A Name",
        "must_trigger_rules": sorted(settings.generation["demo_entity"]["must_trigger_rules"]),
    }
    return {**pin, **changes}


@pytest.fixture
def settings(repo_root: Path, tmp_path: Path) -> Any:
    s = _settings(repo_root, tmp_path)
    write_demo_entity(verify_mod.pin_path(s), _pin(s))
    return s


class Row:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)

    def __getitem__(self, index: int) -> Any:
        return list(self.values.values())[index]


def _yield(signals: int = 10, within: bool = True, registered: bool = True) -> dict[str, Any]:
    return {
        "RULE_ID": RULE,
        "REGISTERED": registered,
        "SIGNALS": signals,
        "MIN_SIGNALS": 1,
        "MAX_SIGNALS": 2000,
        "WITHIN_BOUNDS": within,
    }


def _metrics() -> dict[str, Any]:
    return {
        "RULE_ID": RULE,
        "PREDICTED": 10,
        "EXPECTED": 10,
        "TRUE_POSITIVE": 10,
        "FALSE_POSITIVE": 0,
        "FP_LEGIT": 0,
        "FP_OTHER_ILLICIT": 0,
        "NOT_IN_GRAIN": 0,
        "MISSED": 0,
        "RECALL": "1.000000",
        "PRECISION": "1.000000",
        "EVIDENCE_UNRESOLVED": 0,
        "CONTROL_DEFINED": True,
        "CONTROLS": 1,
        "CONTROLS_FIRED": 0,
    }


def _hero(detected: bool = True, cases: list[str] | None = None) -> dict[str, Any]:
    return {
        "RULE_ID": RULE,
        "DETECTED": detected,
        "SIGNAL_BUSINESS_KEYS": json.dumps([KEY] if detected else []),
        "CASE_IDS": json.dumps(["CASE-1"] if cases is None else cases),
    }


class FakeSession:
    """Records every statement; answers each verify query by its header comment."""

    def __init__(self, seed: int, **answers: list[dict[str, Any]]) -> None:
        self.answers: dict[str, list[dict[str, Any]]] = {
            "01_refresh": [{"statistics": "No new data", "refreshed_dt_count": 0}],
            "02_signal_yield": [_yield()],
            "03_detection_metrics": [_metrics()],
            "04_loaded_seed": [{"SEEDS": json.dumps([seed]), "LEGS": 2}],
            "05_hero": [_hero()],
            "06_eval_run": [{"number of rows inserted": 1}],
            **answers,
        }
        self.statements: list[tuple[str, list[Any] | None]] = []

    def sql(self, sql: str, params: list[Any] | None = None) -> Any:
        self.statements.append((sql, params))
        match = re.search(r"verify / (\d\d_\w+)\.sql", sql)
        if match:
            rows = self.answers[match.group(1)]
        elif "CURRENT_TIMESTAMP" in sql:
            rows = [{"NOW": dt.datetime(2026, 9, 29, 8, 0)}]
        else:
            rows = []
        return type("Result", (), {"collect": lambda _self: [Row(r) for r in rows]})()

    def sent(self, query: str) -> list[tuple[str, list[Any] | None]]:
        return [(s, p) for s, p in self.statements if f"verify / {query}.sql" in s]


def _fake(settings: Any, **answers: list[dict[str, Any]]) -> FakeSession:
    return FakeSession(int(settings.generation["seed"]), **answers)


def _run(settings: Any, session: FakeSession) -> Any:
    return Verifier(settings, session).run(read_pin(settings))


# --------------------------------------------------------------------------- #
# The SQL: every value from config, no threshold, no literal
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("profile", ["dev", "demo"])
def test_every_verify_query_is_one_statement(repo_root: Path, profile: str) -> None:
    verifier = Verifier(load_settings(repo_root=repo_root, profile=profile), None)
    names = sorted(verifier.queries)
    assert names == sorted(
        [
            verify_mod.REFRESH,
            verify_mod.SIGNAL_YIELD,
            verify_mod.METRICS,
            verify_mod.LOADED_SEED,
            verify_mod.HERO,
            verify_mod.EVAL_RUN,
        ]
    )
    for name in names:
        assert verifier.statement(name)


@pytest.mark.parametrize("profile", ["dev", "demo"])
def test_rules_and_signal_tables_come_from_config(repo_root: Path, profile: str) -> None:
    settings = load_settings(repo_root=repo_root, profile=profile)
    verifier = Verifier(settings, None)
    ctx = settings.sql_context()
    rules = [r["rule_id"] for r in settings.detection["rules"]]
    tables = {
        f"{ctx['fq']['curated']}.{ctx['dt'][r['dynamic_table']]['name']}"
        for r in settings.detection["rules"]
    }
    refresh = verifier.statement(verify_mod.REFRESH)
    for table in tables:
        assert table in refresh
    assert re.search(r"ALTER DYNAMIC TABLE\s+\S+\s+REFRESH\s*$", refresh)
    for name in (verify_mod.SIGNAL_YIELD, verify_mod.METRICS):
        sql = verifier.statement(name)
        assert sorted(re.findall(r"^SELECT '(\w+)'::VARCHAR AS rule_id$", sql, re.M)) == sorted(
            rules
        )
        assert all(t in sql for t in tables)


def test_signal_tables_follow_each_rules_dynamic_table(repo_root: Path) -> None:
    settings = load_settings(repo_root=repo_root, profile="dev")
    settings.detection["rules"][0]["dynamic_table"] = "signals_aggregate"
    moved = settings.sql_context()["dt"]["signals_aggregate"]["name"]
    for name in (verify_mod.REFRESH, verify_mod.SIGNAL_YIELD, verify_mod.HERO):
        sql = Verifier(settings, None).statement(name)
        assert moved in sql
        assert settings.sql_context()["dt"]["signals_fast"]["name"] not in sql


@pytest.mark.parametrize("profile", ["dev", "demo"])
def test_yield_bounds_are_bound_from_config_never_rendered(
    repo_root: Path, tmp_path: Path, profile: str
) -> None:
    settings = _settings(repo_root, tmp_path, profile)
    write_demo_entity(verify_mod.pin_path(settings), _pin(settings))
    gate = settings.generation["validation"]["signal_yield"]
    bounds = [int(gate["min_detections_per_rule"]), int(gate["max_detections_per_rule"])]
    session = _fake(settings)
    _run(settings, session)
    ((sql, params),) = session.sent("02_signal_yield")
    assert params == bounds
    assert str(bounds[1]) not in sql
    assert "?::NUMBER(38,0) AS min_signals, ?::NUMBER(38,0) AS max_signals" in sql


def test_the_control_reads_band_and_ctr_through_the_rules_clause_roles(repo_root: Path) -> None:
    settings = load_settings(repo_root=repo_root, profile="dev")
    sql = Verifier(settings, None).statement(verify_mod.METRICS)
    (rule,) = [r for r in settings.detection["rules"] if r["rule_id"] == RULE]
    for role in ("ctr", "band"):
        assert f"clause_id = '{rule['clauses'][role]}'::VARCHAR" in sql
    assert "V_POLICY_CLAUSE_CURRENT" in sql and "NOT l.is_illicit" in sql


def test_a_rule_without_a_control_file_reports_none(repo_root: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    shutil.copytree(repo_root / "conf", root / "conf")
    shutil.copytree(repo_root / "sql", root / "sql")
    (root / "data").mkdir()
    (root / "sql" / "verify" / "controls" / f"{RULE}.sql").unlink()
    sql = Verifier(load_settings(repo_root=root, profile="dev"), None).statement(verify_mod.METRICS)
    assert "FALSE AS defined" in sql and "TXN_LABELS" not in sql


def test_signals_map_to_the_month_of_their_evidence(repo_root: Path) -> None:
    """D40: the evidence transaction's month, in the grain's own format."""
    settings = load_settings(repo_root=repo_root, profile="dev")
    sql = Verifier(settings, None).statement(verify_mod.METRICS)
    month = settings.sql_context()["eval_grain"]["month_format"]
    assert "LATERAL FLATTEN(input => s.evidence_txn_ids)" in sql
    assert f"TO_CHAR(t.txn_timestamp, {month}) AS month_bucket" in sql
    assert "window_start" not in sql and "window_bucket" not in sql
    python = eval_grain.MONTH_BUCKET_SQL_FORMAT.replace("YYYY", "%Y").replace("MM", "%m")
    assert python == eval_grain.MONTH_BUCKET_FORMAT
    assert dt.datetime(2022, 9, 5).strftime(eval_grain.MONTH_BUCKET_FORMAT) == "2022-09"


def test_changing_the_month_format_changes_the_sql(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings(repo_root=repo_root, profile="dev")
    monkeypatch.setattr(eval_grain, "MONTH_BUCKET_SQL_FORMAT", "YYYY/MM")
    assert "'YYYY/MM'::VARCHAR" in Verifier(settings, None).statement(verify_mod.METRICS)


def test_lint_covers_verify_queries_and_controls(repo_root: Path) -> None:
    deployer = Deployer(load_settings(repo_root=repo_root, profile="dev"))
    assert deployer.lint() == []
    assert [p.name for p in deployer.verify_files()] == sorted(
        p.name for p in (repo_root / "sql" / "verify").glob("[0-9]*.sql")
    )


# --------------------------------------------------------------------------- #
# Python decides nothing
# --------------------------------------------------------------------------- #


def _code_without_docstrings(path: Path) -> str:
    """The module's code, docstrings and comments removed (the prose may say 'threshold')."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)
    return ast.unparse(tree)


def test_the_module_holds_no_label_or_threshold_logic() -> None:
    code = _code_without_docstrings(Path(verify_mod.__file__))
    for word in (
        "is_illicit",
        "expected_rule_ids",
        "TXN_LABELS",
        "EVAL_GRAIN",
        "threshold",
        "POLICY_CLAUSE",
    ):
        assert word not in code, word
    # Bounds reach SQL as binds; no count is compared to them here.
    assert not re.search(r"signals\s*[<>]=?|[<>]=?\s*self\.bounds", code)


@pytest.mark.parametrize(
    ("signals", "within", "code"),
    [
        (10, True, 0),
        # In range by Python's arithmetic, but the SQL said no: the SQL wins.
        (10, False, 1),
        # A rule that fired nothing is listed, and the SQL fails it.
        (0, False, 1),
        # Out of range by arithmetic, but the SQL said yes: no second opinion.
        (5000, True, 0),
    ],
)
def test_the_yield_gate_exits_on_the_sqls_verdict(
    settings: Any, signals: int, within: bool, code: int
) -> None:
    report = _run(settings, _fake(settings, **{"02_signal_yield": [_yield(signals, within)]}))
    assert report.exit_code == code
    assert [y.signals for y in report.yields] == [signals]


def test_an_unregistered_rule_in_a_signal_table_fails(settings: Any) -> None:
    rows = [_yield(), {**_yield(3, False, registered=False), "RULE_ID": "GHOST"}]
    report = _run(settings, _fake(settings, **{"02_signal_yield": rows}))
    assert report.exit_code == 1 and not report.yield_ok


def test_a_non_blocking_yield_gate_reports_without_failing(settings: Any) -> None:
    settings.generation["validation"]["signal_yield"]["fail_build"] = False
    report = _run(settings, _fake(settings, **{"02_signal_yield": [_yield(0, False)]}))
    assert not report.yield_ok and report.exit_code == 0


def test_the_zero_signal_rule_is_listed_by_the_sql(repo_root: Path) -> None:
    sql = Verifier(load_settings(repo_root=repo_root, profile="dev"), None).statement(
        verify_mod.SIGNAL_YIELD
    )
    assert "FULL OUTER JOIN fired f ON f.rule_id = r.rule_id" in sql
    assert "COALESCE(f.signals, 0)" in sql


# --------------------------------------------------------------------------- #
# The hero
# --------------------------------------------------------------------------- #


def test_the_hero_is_confirmed_beside_the_pin(settings: Any) -> None:
    pin_file = verify_mod.pin_path(settings)
    before = pin_file.read_bytes()
    session = _fake(settings)
    report = _run(settings, session)

    assert report.status == CONFIRMED and report.exit_code == 0
    assert pin_file.read_bytes() == before  # generate's pin is never rewritten (D41)
    doc = yaml.safe_load(report.verified_path.read_text(encoding="utf-8"))
    assert doc["status"] == CONFIRMED and doc["party_id"] == PARTY
    assert doc["rules"][RULE] == {
        "detected": True,
        "signal_business_keys": [KEY],
        "case_ids": ["CASE-1"],
    }
    assert doc["missing_rules"] == []
    ((_, params),) = session.sent("05_hero")
    assert params == [json.dumps([RULE]), PARTY]


def test_the_hero_is_rejected_and_verify_exits_1(settings: Any) -> None:
    session = _fake(settings, **{"05_hero": [_hero(detected=False, cases=[])]})
    report = _run(settings, session)
    assert report.status == REJECTED and report.exit_code == 1
    doc = yaml.safe_load(report.verified_path.read_text(encoding="utf-8"))
    assert doc["status"] == REJECTED and doc["missing_rules"] == [RULE]
    ((_, params),) = session.sent("06_eval_run")
    assert params is not None and params[6] is False


def test_an_unrouted_hero_signal_is_confirmed_with_a_warning(settings: Any) -> None:
    report = _run(settings, _fake(settings, **{"05_hero": [_hero(cases=[])]}))
    assert report.status == CONFIRMED and report.unrouted == [RULE]


def test_the_result_file_is_byte_identical_for_the_same_data(settings: Any) -> None:
    first = _run(settings, _fake(settings)).verified_path.read_bytes()
    second = _run(settings, _fake(settings)).verified_path.read_bytes()
    assert first == second
    assert b"2026-09-29" not in first  # the run's clock (the fake's CURRENT_TIMESTAMP)


@pytest.mark.parametrize(
    "change",
    [
        {"generation_seed": 1},
        {"profile": "demo"},
        {"must_trigger_rules": ["OTHER_RULE"]},
        {"status": CONFIRMED},
        {"party_id": None},
    ],
)
def test_a_pin_that_disagrees_with_config_is_refused(settings: Any, change: dict[str, Any]) -> None:
    write_demo_entity(verify_mod.pin_path(settings), _pin(settings, **change))
    with pytest.raises(VerifyError, match="does not match the configuration"):
        read_pin(settings)


def test_a_missing_pin_is_refused(settings: Any) -> None:
    verify_mod.pin_path(settings).unlink()
    with pytest.raises(VerifyError, match="run `riskcopilot generate`"):
        read_pin(settings)


def test_data_from_another_seed_is_refused_before_anything_runs(settings: Any) -> None:
    session = _fake(settings, **{"04_loaded_seed": [{"SEEDS": json.dumps([7]), "LEGS": 2}]})
    with pytest.raises(VerifyError, match="different builds"):
        _run(settings, session)
    assert not session.sent("01_refresh") and not session.sent("06_eval_run")
    assert not verify_mod.pin_path(settings, "demo_entity_verified_file").exists()


def test_the_cli_exits_2_on_a_stale_pin_without_connecting(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from risk_copilot import cli, session

    write_demo_entity(verify_mod.pin_path(settings), _pin(settings, generation_seed=1))
    monkeypatch.setattr(cli, "_settings", lambda: settings)

    def no_session(*_: Any, **__: Any) -> Any:
        raise AssertionError("verify opened a session for a stale pin")

    monkeypatch.setattr(session, "open_session", no_session)
    result = CliRunner().invoke(cli.app, ["--profile", "dev", "verify"])
    assert result.exit_code == 2 and "Verify refused" in result.output


# --------------------------------------------------------------------------- #
# What verify writes
# --------------------------------------------------------------------------- #


def test_verify_never_routes_and_writes_only_refresh_and_eval_run(settings: Any) -> None:
    session = _fake(settings)
    _run(settings, session)
    sent = [s for s, _ in session.statements]
    ctx = settings.sql_context()
    assert sent[0] == f"USE ROLE {ctx['role']['admin']}"
    for sql in sent:
        code = " ".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
        assert not re.search(r"\b(CALL|EXECUTE TASK|RESUME|MERGE|DELETE|UPDATE)\b", code), sql
        assert ctx["stream_task"]["signals_stream"] not in code
        assert ctx["stream_task"]["route_signals_procedure"] not in code
    writes = [s for s in sent if re.search(r"^\s*(INSERT|ALTER)\b", s, re.M)]
    assert len(writes) == 2
    assert "REFRESH" in writes[0] and "EVAL_RUN" in writes[1]


def test_the_eval_run_row_carries_the_run(settings: Any) -> None:
    session = _fake(settings)
    report = _run(settings, session)
    ((sql, params),) = session.sent("06_eval_run")
    assert f"{settings.sql_context()['fq']['ground_truth']}.EVAL_RUN" in sql
    assert params is not None
    run_id, suite, profile, _commit, seed, metrics, passed, started = params
    assert (run_id, suite, profile, seed, passed) == (
        report.run_id,
        verify_mod.SUITE,
        "dev",
        int(settings.generation["seed"]),
        True,
    )
    body = json.loads(metrics)
    assert body["detection"][0]["true_positive"] == 10
    assert body["hero"]["status"] == CONFIRMED
    assert body["signal_yield"]["bounds"] == [1, 2000]
    assert started == "2026-09-29 08:00:00"
