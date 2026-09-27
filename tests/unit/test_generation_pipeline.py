"""End-to-end generation, gates, writer and CLI (Phase 2, step 7).

The pipeline runs on the fixture with the production dev config resized to it.
``GateInputs`` is captured from that run, so each gate test starts from a real,
passing dataset and breaks exactly one contract.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from risk_copilot import cli
from risk_copilot.config import Settings, deep_merge, load_settings
from risk_copilot.generation import pipeline as pipeline_mod
from risk_copilot.generation import validate
from risk_copilot.generation.draws import account_type
from risk_copilot.generation.pipeline import GenerationResult, run_generation
from risk_copilot.generation.validate import Gate, GateInputs, GateStatus
from risk_copilot.ids import account_id
from risk_copilot.pipeline.deploy import Deployer, split_statements

pytestmark = pytest.mark.unit

#: The fixture's single host generates a few dozen illicit legs among a few
#: dozen source legs, so the real illicit-rate band cannot hold on it.
WIDE_BAND = {"validation": {"illicit_rate_pct": {"min": 0.0, "max": 100.0}}}


def fixture_settings(repo_root: Path, tmp_path: Path, **extra: Any) -> Settings:
    base = load_settings(repo_root=repo_root, profile="dev")
    generation = deep_merge(
        base.generation,
        {
            "subgraph": {"target_parties": 50, "target_accounts": 50, "max_transactions": 1000},
            "injection": {"structuring": {"cohort_parties": 1}},
            # Absolute, so the real conf/generated pin is never touched.
            "output": {"demo_entity_file": str(tmp_path / "demo_entity.{profile}.yml")},
        },
    )
    return base.model_copy(update={"generation": deep_merge(generation, extra)})


@pytest.fixture
def run(repo_root: Path, tmp_path: Path, amlworld_mini: Path) -> Callable[..., GenerationResult]:
    def _run(out: str = "out", **extra: Any) -> GenerationResult:
        settings = fixture_settings(repo_root, tmp_path, **extra)
        return run_generation(settings, data_root=amlworld_mini, out_dir=tmp_path / out)

    return _run


@pytest.fixture
def captured(run: Callable[..., GenerationResult], monkeypatch: pytest.MonkeyPatch) -> GateInputs:
    """The GateInputs of a passing fixture run."""
    seen: list[GateInputs] = []
    real = pipeline_mod.run_gates

    def spy(inputs: GateInputs) -> list[Gate]:
        seen.append(inputs)
        return real(inputs)

    monkeypatch.setattr(pipeline_mod, "run_gates", spy)
    result = run(**WIDE_BAND)
    assert result.passed, [g for g in result.gates if g.blocks]
    return seen[0]


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


def test_fixture_build_passes_with_a_band_that_fits_it(
    run: Callable[..., GenerationResult],
) -> None:
    result = run(**WIDE_BAND)
    assert result.passed
    assert result.manifest["passed"] is True
    assert result.demo_entity_path is not None and result.demo_entity_path.is_file()
    statuses = {g.name: g.status for g in result.gates}
    assert statuses["signal_yield"] is GateStatus.DEFERRED
    assert statuses["income_calibration"] is GateStatus.REPORT


def test_real_band_fails_the_fixture_and_blocks(run: Callable[..., GenerationResult]) -> None:
    """The production 2.50% ceiling cannot hold on 24 legs -- the gate must say so."""
    result = run()
    assert not result.passed
    assert [g.name for g in result.gates if g.blocks] == ["illicit_rate_pct"]
    assert result.manifest["passed"] is False
    assert result.demo_entity_path is None  # no pin from a failed build


def test_files_are_byte_identical_across_runs(run: Callable[..., GenerationResult]) -> None:
    a, b = run("a", **WIDE_BAND), run("b", **WIDE_BAND)
    files_a = sorted(p.relative_to(a.out_dir) for p in a.out_dir.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(b.out_dir) for p in b.out_dir.rglob("*") if p.is_file())
    assert files_a == files_b and len(files_a) == 12
    for rel in files_a:
        assert (a.out_dir / rel).read_bytes() == (b.out_dir / rel).read_bytes(), rel


def test_a_different_seed_changes_the_output(
    run: Callable[..., GenerationResult], repo_root: Path
) -> None:
    """Account types hash from seeded ids, so the fixture host qualifies only under
    some seeds. Use the first other seed under which it still has two deposit accounts."""
    generation = load_settings(repo_root=repo_root, profile="dev").generation
    deposit = set(generation["injection"]["structuring"]["deposit_account_types"])
    host_accounts = [("21174", "8000A0100"), ("20", "8000A0101"), ("10", "8000A0102")]
    other = next(
        s
        for s in range(1, 200)
        if s != generation["seed"]
        and sum(
            account_type(account_id(b, n, seed=s), {**generation, "seed": s}) in deposit
            for b, n in host_accounts
        )
        >= 2
    )
    a = run("a", **WIDE_BAND)
    b = run("b", **WIDE_BAND, seed=other)
    assert a.manifest["tables"]["raw/ID_MAP_PARTY"] != b.manifest["tables"]["raw/ID_MAP_PARTY"]


def test_manifest_records_what_a_reviewer_needs(run: Callable[..., GenerationResult]) -> None:
    result = run(**WIDE_BAND)
    manifest = json.loads((result.out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["generation_seed"] == result.manifest["generation_seed"]
    assert manifest["legit_window"]["span_days"] == 10
    assert set(manifest["referenced_clause_ids"]) == {
        "CTR_CASH_THRESHOLD",
        "STRUCTURING_BAND",
        "STRUCTURING_WINDOW_DAYS",
        "STRUCTURING_MIN_ACCOUNTS",
    }
    assert {g["name"] for g in manifest["gates"]} == {
        g.__name__ if g.__name__ != "illicit_rate" else "illicit_rate_pct" for g in validate.GATES
    }
    assert manifest["versions"]["Faker"] != "unknown"
    assert "raw/RAW_TRANSACTIONS" in manifest["tables"]
    assert not re.search(r"20\d\d-\d\d-\d\dT\d\d:\d\d", json.dumps(manifest["tables"]))


# --------------------------------------------------------------------------- #
# Output shape against the DDL
# --------------------------------------------------------------------------- #

_CREATE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*)\)", re.DOTALL | re.IGNORECASE)
_COLUMN_RE = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s+[A-Z]", re.MULTILINE)
_REQUIRED_RE = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s+[A-Z][^\n]*NOT NULL", re.MULTILINE)


def ddl_tables(settings: Settings) -> dict[str, tuple[set[str], set[str]]]:
    """``table -> (columns, NOT NULL columns without a DEFAULT)`` from the DDL."""
    deployer = Deployer(settings)
    tables: dict[str, tuple[set[str], set[str]]] = {}
    for path in deployer.stage_files("ddl"):
        for statement in split_statements(deployer.render(path)):
            if match := _CREATE_RE.search(statement):
                body = re.sub(r"'(?:[^']|'')*'", "''", match.group(2))
                cols = {c for c in _COLUMN_RE.findall(body) if c != "constraint"}
                required = {
                    line.split()[0]
                    for line in body.splitlines()
                    if _REQUIRED_RE.match(line) and "DEFAULT" not in line
                }
                tables[match.group(1).upper()] = (cols, required)
    return tables


def test_every_output_matches_its_ddl_table(
    run: Callable[..., GenerationResult], repo_root: Path, tmp_path: Path
) -> None:
    result = run(**WIDE_BAND)
    ddl = ddl_tables(fixture_settings(repo_root, tmp_path))
    for path in sorted(result.out_dir.rglob("*.parquet")):
        name = path.stem
        assert name in ddl, f"{name} has no DDL table"
        columns, required = ddl[name]
        written = set(pq.read_schema(path).names)
        assert written <= columns, f"{name}: columns not in DDL {sorted(written - columns)}"
        assert required <= written, f"{name}: missing NOT NULL columns {sorted(required - written)}"


def test_raw_files_carry_no_label_column(
    run: Callable[..., GenerationResult], repo_root: Path
) -> None:
    result = run(**WIDE_BAND)
    forbidden = set(
        load_settings(repo_root=repo_root, profile="dev").generation["validation"]["label_leakage"][
            "forbidden_columns"
        ]
    )
    for path in (result.out_dir / "raw").glob("*.parquet"):
        assert not forbidden & set(pq.read_schema(path).names), path.name


def test_injected_legs_are_not_in_raw_transactions(run: Callable[..., GenerationResult]) -> None:
    """D2: they reach RAW only at load, once the threshold is resolved."""
    result = run(**WIDE_BAND)
    raw = pq.read_table(result.out_dir / "raw" / "RAW_TRANSACTIONS.parquet")
    pending = pq.read_table(result.out_dir / "ground_truth" / "INJECTED_TXN_PENDING.parquet")
    assert pending.num_rows > 0
    assert set(raw["txn_id"].to_pylist()).isdisjoint(pending["txn_id"].to_pylist())
    labels = pq.read_table(result.out_dir / "ground_truth" / "TXN_LABELS.parquet")
    assert set(labels["txn_id"].to_pylist()) == set(raw["txn_id"].to_pylist()) | set(
        pending["txn_id"].to_pylist()
    )


# --------------------------------------------------------------------------- #
# Every blocking gate blocks when violated
# --------------------------------------------------------------------------- #


def _replace_raw(g: GateInputs, name: str, table: pa.Table) -> GateInputs:
    return dataclasses.replace(g, raw={**g.raw, name: table})


def _append_row(table: pa.Table, **changes: Any) -> pa.Table:
    row = {**table.slice(0, 1).to_pylist()[0], **changes}
    return pa.concat_tables([table, pa.Table.from_pylist([row], schema=table.schema)])


VIOLATIONS: dict[str, Callable[[GateInputs], GateInputs]] = {
    "illicit_rate_pct": lambda g: dataclasses.replace(
        g, generation=deep_merge(g.generation, {"validation": {"illicit_rate_pct": {"max": 0.0}}})
    ),
    "bank_id_resolution": lambda g: dataclasses.replace(g, unresolved_source_pairs=1),
    "label_attribution": lambda g: dataclasses.replace(g, attribution_counts={1: 5, 2: 1}),
    "id_map_one_to_one": lambda g: _replace_raw(
        g, "ID_MAP_PARTY", _append_row(g.raw["ID_MAP_PARTY"])
    ),
    "injection_window": lambda g: dataclasses.replace(g, legit_span_days=3),
    "injection_band": lambda g: dataclasses.replace(
        g,
        generation=deep_merge(
            g.generation, {"injection": {"structuring": {"amount_band_pct_of_ctr": [0.5, 0.985]}}}
        ),
    ),
    "money_conservation": lambda g: _replace_raw(
        g, "RAW_TRANSACTIONS", _append_row(g.raw["RAW_TRANSACTIONS"], txn_id="T-X-D")
    ),
    "referential_integrity": lambda g: _replace_raw(
        g, "RAW_ACCOUNTS", _append_row(g.raw["RAW_ACCOUNTS"], account_id="A-000000000000")
    ),
    "label_leakage": lambda g: _replace_raw(
        g,
        "RAW_PARTY_CBS",
        g.raw["RAW_PARTY_CBS"].append_column(
            "cohort", pa.array(["x"] * g.raw["RAW_PARTY_CBS"].num_rows)
        ),
    ),
    "budget": lambda g: dataclasses.replace(
        g, generation=deep_merge(g.generation, {"subgraph": {"target_parties": 1}})
    ),
    "signal_yield": lambda g: dataclasses.replace(
        g, hosts=[dataclasses.replace(h, accounts=h.accounts[:1]) for h in g.hosts]
    ),
    "demo_entity": lambda g: dataclasses.replace(g, demo_entity_error="no hero"),
}


def test_every_blocking_gate_has_a_violation_test(captured: GateInputs) -> None:
    blocking = {g.name for g in validate.run_gates(captured) if g.fail_build}
    assert blocking == set(VIOLATIONS)


@pytest.mark.parametrize("name", sorted(VIOLATIONS))
def test_violating_a_gate_blocks_the_build(captured: GateInputs, name: str) -> None:
    before = {g.name: g for g in validate.run_gates(captured)}
    assert not before[name].blocks
    after = {g.name: g for g in validate.run_gates(VIOLATIONS[name](captured))}
    assert after[name].status is GateStatus.FAIL
    assert after[name].blocks


@pytest.mark.parametrize(
    "value", ["reskin_structuring", "TYP_AML_CYCLE", "amlworld_original", "src:12", "inj:x"]
)
def test_label_values_are_caught_inside_string_columns(captured: GateInputs, value: str) -> None:
    kyc = captured.raw["RAW_PARTY_KYC"]
    index = kyc.schema.get_field_index("source_of_funds")
    leaked = kyc.set_column(index, "source_of_funds", pa.array([f"note {value}"] * kyc.num_rows))
    gate = validate.label_leakage(_replace_raw(captured, "RAW_PARTY_KYC", leaked))
    assert gate.blocks


def test_reporting_gates_never_block(captured: GateInputs) -> None:
    broken = dataclasses.replace(
        captured,
        generation=deep_merge(
            captured.generation,
            {
                "validation": {
                    "benford": {"max_chi_square": 0.0},
                    "degree_distribution": {"max_power_law_alpha": 0.0},
                }
            },
        ),
    )
    gates = {g.name: g for g in validate.run_gates(broken)}
    for name in ("benford", "degree_distribution", "income_calibration"):
        assert not gates[name].blocks


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _fake_result(tmp_path: Path, status: GateStatus, fail_build: bool) -> GenerationResult:
    gate = Gate("illicit_rate_pct", status, fail_build, "9.9%")
    return GenerationResult(tmp_path, (gate,), {"passed": not gate.blocks}, None, None)


@pytest.mark.parametrize(
    ("status", "fail_build", "code"),
    [
        (GateStatus.PASS, True, 0),
        (GateStatus.FAIL, True, 1),
        (GateStatus.FAIL, False, 0),
        (GateStatus.DEFERRED, True, 0),
    ],
)
def test_cli_exit_code_follows_blocking_gates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_root: Path,
    status: GateStatus,
    fail_build: bool,
    code: int,
) -> None:
    monkeypatch.setattr(
        pipeline_mod, "run_generation", lambda *a, **k: _fake_result(tmp_path, status, fail_build)
    )
    result = CliRunner().invoke(
        cli.app, ["--repo-root", str(repo_root), "--profile", "dev", "generate"]
    )
    assert result.exit_code == code, result.output


def test_cli_turns_a_contract_error_into_exit_1(
    repo_root: Path, amlworld_mini: Path, tmp_path: Path
) -> None:
    """The production dev cohort (10 hosts) cannot be found in the fixture."""
    result = CliRunner().invoke(
        cli.app,
        [
            "--repo-root",
            str(repo_root),
            "--profile",
            "dev",
            "generate",
            "--data-root",
            str(amlworld_mini),
            "--out-dir",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 1
    assert "Generation failed" in result.output
