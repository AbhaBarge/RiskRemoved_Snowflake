"""``riskcopilot load`` (Phase 3, step 4), offline.

A real bundle is written with the generator's own writer, so the manifest hashes
are the ones ``load`` must verify. The session is a recording fake that answers
the live questions (column types, clause in force, stage grants, row counts)
from a script; nothing here needs a connection.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from risk_copilot.config import load_settings
from risk_copilot.generation.writer import write_dataset
from risk_copilot.pipeline.load import (
    Column,
    Loader,
    LoadError,
    amlworld_trans_copy_sql,
    check_bundle,
    promotion_sql,
    read_bundle,
    select_expressions,
)

pytestmark = pytest.mark.unit

TS = dt.datetime(2022, 9, 5, 11, 30)


# --------------------------------------------------------------------------- #
# A bundle and a fake session
# --------------------------------------------------------------------------- #


def _tables() -> tuple[dict[str, pa.Table], dict[str, pa.Table]]:
    tx = pa.table(
        {
            "txn_id": ["T-1-D", "T-1-C"],
            "txn_pair_id": ["T-1", "T-1"],
            "leg_side": ["D", "C"],
            "account_id": ["A-1", "A-2"],
            "counterparty_account_id": ["A-2", "A-1"],
            "txn_timestamp": pa.array([TS, TS], pa.timestamp("ms")),
            "amount": pa.array([Decimal("-5.00"), Decimal("5.00")], pa.decimal128(20, 2)),
        }
    )
    pending = pa.table(
        {
            "txn_id": ["T-2-D", "T-2-C"],
            "txn_pair_id": ["T-2", "T-2"],
            "ctr_clause_id": ["CTR_CASH_THRESHOLD"] * 2,
            "currency": ["INR", "INR"],
            "amount_ctr_ratio": pa.array([Decimal("0.95")] * 2, pa.decimal128(12, 10)),
        }
    )
    labels = pa.table({"txn_id": ["T-1-D", "T-1-C", "T-2-D", "T-2-C"]})
    return {"RAW_TRANSACTIONS": tx}, {"INJECTED_TXN_PENDING": pending, "TXN_LABELS": labels}


@pytest.fixture
def settings(repo_root: Path) -> Any:
    return load_settings(repo_root=repo_root, profile="dev")


@pytest.fixture
def bundle_dir(tmp_path: Path, settings: Any) -> Path:
    raw, gt = _tables()
    write_dataset(
        tmp_path,
        raw,
        gt,
        {"passed": True, "profile": "dev", "gates": []},
        settings.generation["output"],
    )
    return tmp_path


class Row(tuple):  # type: ignore[type-arg]
    def as_dict(self) -> dict[str, Any]:
        return dict(self[0]) if self and isinstance(self[0], dict) else {}


def _cols(table: str, *specs: tuple[str, str, Any, Any]) -> list[tuple[Any, ...]]:
    return [(table, c, t, p, s) for c, t, p, s in specs]


_TEXT = ("TEXT", None, None)
RAW_COLUMNS = [
    *_cols(
        "RAW_TRANSACTIONS",
        *[(c, *_TEXT) for c in ("TXN_ID", "TXN_PAIR_ID", "LEG_SIDE", "ACCOUNT_ID")],
        ("COUNTERPARTY_ACCOUNT_ID", *_TEXT),
        ("TXN_TIMESTAMP", "TIMESTAMP_NTZ", None, None),
        ("AMOUNT", "NUMBER", 20, 2),
    ),
    *_cols(
        "RAW_AMLWORLD_TRANS", ("AMOUNT_RECEIVED", "NUMBER", 20, 6), ("AMOUNT_PAID", "NUMBER", 20, 6)
    ),
]
GT_COLUMNS = [
    *_cols(
        "INJECTED_TXN_PENDING",
        *[(c, *_TEXT) for c in ("TXN_ID", "TXN_PAIR_ID", "CTR_CLAUSE_ID", "CURRENCY")],
        ("AMOUNT_CTR_RATIO", "NUMBER", 12, 10),
    ),
    *_cols("TXN_LABELS", ("TXN_ID", *_TEXT)),
]

#: What a ready account answers: the CTR clause in force, a stage owned and
#: granted to no one, and empty target tables.
READY: dict[str, list[Any]] = {
    r"V_POLICY_CLAUSE_CURRENT WHERE clause_id = \?": [(1,)],
    r"SHOW GRANTS ON STAGE": [({"privilege": "OWNERSHIP", "grantee_name": "RC_ADMIN_DEV"},)],
    r"^SELECT COUNT\(\*\) FROM \S+$": [(0,)],
}


class FakeSession:
    """Records every statement and PUT; answers from regex -> rows, first match wins."""

    def __init__(
        self, answers: dict[str, list[Any]], columns: dict[str, list[Any]] | None = None
    ) -> None:
        self.answers = answers
        self.columns = columns or {"RAW": RAW_COLUMNS, "GROUND_TRUTH": GT_COLUMNS}
        self.statements: list[tuple[str, list[Any] | None]] = []
        self.puts: list[tuple[str, str, bool]] = []
        self.file = SimpleNamespace(put=self._put)

    def _put(self, local: str, stage: str, *, auto_compress: bool, overwrite: bool) -> Any:
        self.puts.append((Path(local).name, stage, auto_compress))
        return [SimpleNamespace(target=Path(local).name + (".gz" if auto_compress else ""))]

    def sql(self, sql: str, params: list[Any] | None = None) -> Any:
        self.statements.append((sql, params))
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            rows = self.columns[params[0]] if params else []
        else:
            rows = next((r for p, r in self.answers.items() if re.search(p, sql, re.S)), [(0,)])
        return SimpleNamespace(collect=lambda: [Row(r) for r in rows])

    @property
    def sent(self) -> list[str]:
        return [s for s, _ in self.statements]


def fake(columns: dict[str, list[Any]] | None = None, **answers: list[Any]) -> FakeSession:
    return FakeSession({**READY, **answers}, columns)


def loader(settings: Any, session: FakeSession, **kw: Any) -> Loader:
    return Loader(settings, session, source=kw.pop("source", False), **kw)


# --------------------------------------------------------------------------- #
# Bundle refusals
# --------------------------------------------------------------------------- #


def test_a_failed_build_is_refused(bundle_dir: Path, settings: Any) -> None:
    """Obligation 1."""
    bundle = read_bundle(bundle_dir)
    failed = {
        **bundle.manifest,
        "passed": False,
        "gates": [{"name": "demo_entity", "status": "FAIL"}],
    }
    with pytest.raises(LoadError, match=r"passed: False.*demo_entity"):
        check_bundle(
            bundle.__class__(bundle.out_dir, failed, bundle.run_key, bundle.artifacts), settings
        )


def test_another_profiles_bundle_is_refused(bundle_dir: Path, repo_root: Path) -> None:
    with pytest.raises(LoadError, match="generated for profile 'dev'"):
        check_bundle(read_bundle(bundle_dir), load_settings(repo_root=repo_root, profile="demo"))


def test_a_file_changed_after_generation_is_refused(bundle_dir: Path, settings: Any) -> None:
    path = bundle_dir / "raw" / "RAW_TRANSACTIONS.parquet"
    table = pq.read_table(path)
    pq.write_table(table.set_column(0, "txn_id", pa.array(["T-9-D", "T-9-C"])), path)
    with pytest.raises(LoadError, match="content differs"):
        check_bundle(read_bundle(bundle_dir), settings)


def test_run_key_is_the_manifest_hash_prefix(bundle_dir: Path) -> None:
    a, b = read_bundle(bundle_dir), read_bundle(bundle_dir)
    assert a.run_key == b.run_key and re.fullmatch(r"[0-9a-f]{16}", a.run_key)


def test_the_cli_refuses_a_failed_build_before_connecting(
    tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed demo build must be refused even when the demo database does not exist."""
    from typer.testing import CliRunner

    from risk_copilot import cli, session

    raw, gt = _tables()
    write_dataset(
        tmp_path,
        raw,
        gt,
        {"passed": False, "profile": "dev", "gates": []},
        settings.generation["output"],
    )

    def no_session(*_: Any, **__: Any) -> Any:
        raise AssertionError("load opened a session for a build that failed its gates")

    monkeypatch.setattr(session, "open_session", no_session)
    result = CliRunner().invoke(cli.app, ["--profile", "dev", "load", "--out-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "passed: False" in result.output and "nothing was committed" in result.output


# --------------------------------------------------------------------------- #
# SQL builders
# --------------------------------------------------------------------------- #


def test_millisecond_timestamps_are_read_at_their_unit() -> None:
    """M29: `::TIMESTAMP_NTZ` on Parquet epoch-ms lands in the year 54657."""
    schema = pa.schema([("txn_timestamp", pa.timestamp("ms")), ("opened", pa.date32())])
    target = {
        "TXN_TIMESTAMP": Column("TXN_TIMESTAMP", "TIMESTAMP_NTZ"),
        "OPENED": Column("OPENED", "DATE"),
    }
    exprs = select_expressions(schema, target, "T")
    assert exprs[0] == 'TO_TIMESTAMP_NTZ($1:"txn_timestamp"::NUMBER, 3) AS TXN_TIMESTAMP'
    assert exprs[1] == '$1:"opened"::DATE AS OPENED'


def test_json_text_into_variant_is_parsed() -> None:
    schema = pa.schema([("txn_data", pa.string())])
    exprs = select_expressions(schema, {"TXN_DATA": Column("TXN_DATA", "VARIANT")}, "T")
    assert exprs == ['PARSE_JSON($1:"txn_data"::VARCHAR) AS TXN_DATA']


def test_a_file_column_the_table_lacks_is_refused() -> None:
    """An extra RAW column is the shape of a label leak; never drop it silently."""
    schema = pa.schema([("is_illicit", pa.bool_())])
    with pytest.raises(LoadError, match="is_illicit"):
        select_expressions(schema, {}, "RAW_TRANSACTIONS")


def test_ledger_copy_is_positional_with_the_file_row_number() -> None:
    """Obligation 5 / M16."""
    sql = amlworld_trans_copy_sql("DB.RAW.T", "@S/f.csv.gz", "DB.RAW.FF", "YYYY/MM/DD HH24:MI", 6)
    assert "SELECT METADATA$FILE_ROW_NUMBER, TO_TIMESTAMP_NTZ($1, 'YYYY/MM/DD HH24:MI')" in sql
    assert "$6::NUMBER(20,6)" in sql and "$8::NUMBER(20,6)" in sql and "$11::NUMBER(1,0)" in sql
    assert re.search(r"\(src_row_number, txn_timestamp, from_bank, from_account", sql)
    assert "FORCE = TRUE" in sql


def test_promotion_joins_the_clause_and_writes_no_threshold(settings: Any) -> None:
    """Obligation 3: rupees come from V_POLICY_CLAUSE_CURRENT at load, never a literal."""
    sql = promotion_sql(
        "DB.RAW.RAW_TRANSACTIONS", "DB.GT.INJECTED_TXN_PENDING", "DB.GOV.V_POLICY_CLAUSE_CURRENT"
    )
    assert "JOIN DB.GOV.V_POLICY_CLAUSE_CURRENT c ON c.clause_id = p.ctr_clause_id" in sql
    assert "p.amount_sign * p.amount_ctr_ratio * c.threshold_lower, ?" in sql
    ctr = next(c for c in settings.detection["clauses"] if c["clause_id"] == "CTR_CASH_THRESHOLD")
    assert str(ctr["threshold_lower"]) not in sql
    assert not re.search(r"\d{4,}", sql)


# --------------------------------------------------------------------------- #
# Pre-flight refusals (live answers faked)
# --------------------------------------------------------------------------- #


def test_preflight_passes_on_a_ready_account(bundle_dir: Path, settings: Any) -> None:
    loader(settings, fake(), source=True).preflight(read_bundle(bundle_dir))


def test_a_ledger_table_at_scale_2_is_refused(bundle_dir: Path, settings: Any) -> None:
    raw = [r for r in RAW_COLUMNS if r[0] != "RAW_AMLWORLD_TRANS"]
    raw += _cols("RAW_AMLWORLD_TRANS", ("AMOUNT_PAID", "NUMBER", 20, 2))
    session = fake(columns={"RAW": raw, "GROUND_TRUTH": GT_COLUMNS})
    with pytest.raises(LoadError, match="M14"):
        loader(settings, session, source=True).preflight(read_bundle(bundle_dir))


def test_load_before_the_policy_seed_is_refused(bundle_dir: Path, settings: Any) -> None:
    session = fake(**{r"V_POLICY_CLAUSE_CURRENT WHERE clause_id = \?": [(0,)]})
    with pytest.raises(LoadError, match="Deploy the policy stage before load"):
        loader(settings, session).preflight(read_bundle(bundle_dir))


def test_a_granted_ground_truth_stage_is_refused(bundle_dir: Path, settings: Any) -> None:
    """Obligation 4."""
    grants = [
        ({"privilege": "OWNERSHIP", "grantee_name": "RC_ADMIN_DEV"},),
        ({"privilege": "READ", "grantee_name": "RC_SERVICE_ROLE_DEV"},),
    ]
    session = fake(**{"SHOW GRANTS ON STAGE": grants})
    with pytest.raises(LoadError, match="Labels would leak"):
        loader(settings, session).preflight(read_bundle(bundle_dir))


def test_a_non_empty_target_is_refused_without_replace(bundle_dir: Path, settings: Any) -> None:
    occupied = {r"^SELECT COUNT\(\*\) FROM \S+$": [(7,)]}
    with pytest.raises(LoadError, match=r"not empty.*--replace"):
        loader(settings, fake(**occupied)).preflight(read_bundle(bundle_dir))
    loader(settings, fake(**occupied), replace=True).preflight(read_bundle(bundle_dir))


# --------------------------------------------------------------------------- #
# Upload routing and the transaction
# --------------------------------------------------------------------------- #


def test_ground_truth_files_go_only_to_the_ground_truth_stage(
    bundle_dir: Path, settings: Any, amlworld_mini: Path
) -> None:
    session = fake()
    ctx = settings.sql_context()
    staged = loader(settings, session, source=True, data_root=amlworld_mini).upload(
        read_bundle(bundle_dir)
    )
    gt_stage = f"@{ctx['fq']['ground_truth']}.{ctx['stage']['ground_truth']}/"
    raw_stage = f"@{ctx['fq']['raw']}.{ctx['stage']['generated']}/"
    data_stage = f"@{ctx['fq']['raw']}.{ctx['stage']['data']}/"
    for name, stage, compress in session.puts:
        if name in ("INJECTED_TXN_PENDING.parquet", "TXN_LABELS.parquet"):
            assert stage.startswith(gt_stage)
        elif name == "RAW_TRANSACTIONS.parquet":
            assert stage.startswith(raw_stage) and not compress
        else:
            assert stage.startswith(data_stage) and compress  # the AMLworld CSVs
    assert len(session.puts) == 5
    assert staged["source/RAW_AMLWORLD_TRANS"].endswith("HI-Small_Trans.csv.gz")


def test_a_failed_check_rolls_the_whole_load_back(bundle_dir: Path, settings: Any) -> None:
    # INSERT reports 2 promoted legs; the row counts then disagree with the manifest.
    session = fake(**{r"^INSERT INTO": [(2,)], r"WHERE resolved_at IS NULL$": [(2,)]})
    with pytest.raises(LoadError, match="Post-load checks failed"):
        loader(settings, session).run(read_bundle(bundle_dir))
    sent = session.sent
    assert sent.index("BEGIN") < sent.index("ROLLBACK") and "COMMIT" not in sent
    copies = [s for s in sent if s.startswith("COPY INTO")]
    assert len(copies) == 3 and all("FORCE = TRUE" in s for s in copies)
    assert sent.index("BEGIN") < sent.index(copies[0])


def test_promotion_that_misses_a_leg_is_refused(bundle_dir: Path, settings: Any) -> None:
    session = fake(**{r"^INSERT INTO": [(1,)], r"WHERE resolved_at IS NULL$": [(2,)]})
    with pytest.raises(LoadError, match="Promoted 1 of 2"):
        loader(settings, session).run(read_bundle(bundle_dir))
    assert "ROLLBACK" in session.sent


def test_promotion_binds_the_rounding_decimals(bundle_dir: Path, settings: Any) -> None:
    session = fake(**{r"^INSERT INTO": [(2,)], r"WHERE resolved_at IS NULL$": [(2,)]})
    with pytest.raises(LoadError):
        loader(settings, session).run(read_bundle(bundle_dir))
    (params,) = [p for s, p in session.statements if s.startswith("INSERT INTO")]
    decimals = settings.generation["fx"]["rounding_decimals"]
    assert params == [decimals, decimals]
