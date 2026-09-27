"""``riskcopilot load`` -- a generated dataset into Snowflake (Phase 3, step 4).

Order of operations, each step refusing rather than guessing:

1. **Bundle checks, offline.** ``manifest.json`` must say ``passed: true`` for
   this profile, and every Parquet file must hash to its manifest entry. A demo
   build that failed a gate, or a file edited after generation, never reaches
   Snowflake.
2. **Pre-flight, live.** ``RAW_AMLWORLD_TRANS`` amounts carry the ledger's scale
   (M14); every CTR clause the injected legs name is in force in
   ``V_POLICY_CLAUSE_CURRENT`` (the ``policy`` stage ran first); the ground-truth
   stage carries no grant but its ownership; and every target table is empty,
   unless ``replace`` is set.
3. **Upload.** RAW files to the generated stage, ground-truth files to the
   ground-truth stage and nowhere else, the AMLworld CSVs to the data stage.
   Paths are content-addressed (manifest hash, source SHA-256), so a re-upload
   of the same content is skipped by name.
4. **One transaction:** ``replace`` deletes; COPY every table; promote the
   injected legs; verify. Any failure rolls the whole load back.

COPY never interpolates a value taken from a data file. Column lists come from
the live ``INFORMATION_SCHEMA`` and are validated as identifiers; casts come
from the target column types; timestamps are read from the file's own time
unit, because Snowflake's Parquet reader returns ``timestamp[ms]`` as epoch
milliseconds and a plain ``::TIMESTAMP_NTZ`` reads those as seconds (M29).

Injected legs carry a fraction of the CTR threshold, never rupees (D2). Promotion
resolves them against ``V_POLICY_CLAUSE_CURRENT`` in SQL, so the threshold value
is read from the governed table at load time and appears in no file and no
statement.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from risk_copilot.config import ConfigError, Settings, validate_identifier
from risk_copilot.generation.source import (
    ACCOUNTS_COLUMNS,
    LEDGER_COLUMNS,
    SRC_ROW_NUMBER,
    Dataset,
    source_dataset,
)
from risk_copilot.generation.writer import table_hash
from risk_copilot.session import execute

#: Snowflake ``TO_TIMESTAMP_NTZ(<n>, <scale>)`` scale for each Arrow time unit.
_TIMESTAMP_SCALE = {"s": 0, "ms": 3, "us": 6, "ns": 9}

#: Where each zone of the bundle lands: (schema key, stage key).
ZONES: dict[str, tuple[str, str]] = {
    "raw": ("raw", "generated"),
    "ground_truth": ("ground_truth", "ground_truth"),
}

AMLWORLD_TRANS = "RAW_AMLWORLD_TRANS"
AMLWORLD_ACCOUNTS = "RAW_AMLWORLD_ACCOUNTS"
TRANSACTIONS = "RAW_TRANSACTIONS"
PENDING = "INJECTED_TXN_PENDING"
LABELS = "TXN_LABELS"

#: Stage-path segment for the AMLworld source files.
SOURCE_PREFIX = "amlworld"


class LoadError(RuntimeError):
    """The load was refused or failed; nothing was committed."""


def _content_key(ds: Dataset) -> str:
    """16 hex digits of the source file's SHA-256: the declared one, else computed."""
    declared = str(ds.spec.get("sha256") or "")
    if declared:
        return declared[:16]
    digest = hashlib.sha256()
    with ds.path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# The bundle
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Artifact:
    zone: str
    table: str
    path: Path
    rows: int


@dataclass(frozen=True)
class Bundle:
    out_dir: Path
    manifest: Mapping[str, Any]
    #: First 16 hex digits of the manifest's SHA-256: the stage folder, so two
    #: builds never share one and the same build always reuses its own.
    run_key: str
    artifacts: tuple[Artifact, ...]

    def artifact(self, zone: str, table: str) -> Artifact:
        for a in self.artifacts:
            if (a.zone, a.table) == (zone, table):
                return a
        raise LoadError(f"The bundle has no {zone}/{table}")


def read_bundle(out_dir: Path) -> Bundle:
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        raise LoadError(f"No manifest.json in {out_dir}; run `riskcopilot generate` first")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    artifacts = []
    for key, entry in sorted((manifest.get("tables") or {}).items()):
        zone, _, table = key.partition("/")
        if zone not in ZONES:
            raise LoadError(f"manifest.json: unknown zone in {key!r}")
        artifacts.append(
            Artifact(
                zone=zone,
                table=validate_identifier(table, what="Manifest table name"),
                path=out_dir / zone / f"{table}.parquet",
                rows=int(entry["rows"]),
            )
        )
    return Bundle(out_dir, manifest, hashlib.sha256(raw).hexdigest()[:16], tuple(artifacts))


def check_bundle(bundle: Bundle, settings: Settings) -> None:
    """Refusals that need no connection: failed build, wrong profile, altered file."""
    m = bundle.manifest
    if m.get("passed") is not True:
        failed = [g["name"] for g in m.get("gates") or [] if g.get("status") == "FAIL"]
        raise LoadError(
            f"{bundle.out_dir}: manifest says passed: {m.get('passed')!r} (failed gates: "
            f"{failed}). A dataset that failed a blocking gate is never loaded."
        )
    if m.get("profile") != settings.profile:
        raise LoadError(
            f"{bundle.out_dir} was generated for profile {m.get('profile')!r}, not "
            f"{settings.profile!r}"
        )
    if not bundle.artifacts:
        raise LoadError("manifest.json lists no tables")
    entries = m["tables"]
    for a in bundle.artifacts:
        if not a.path.is_file():
            raise LoadError(f"Missing {a.path}")
        table = pq.read_table(a.path)
        expected = entries[f"{a.zone}/{a.table}"]["content_hash"]
        if table.num_rows != a.rows or table_hash(table) != expected:
            raise LoadError(
                f"{a.path}: content differs from manifest.json (rows {table.num_rows} vs "
                f"{a.rows}, or hash). The file changed after generation; regenerate."
            )


# --------------------------------------------------------------------------- #
# SQL builders (pure, so they are tested without a connection)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str


def column_type(data_type: str, precision: Any, scale: Any) -> str:
    """An ``INFORMATION_SCHEMA.COLUMNS`` type as a castable SQL type."""
    kind = data_type.upper()
    if kind == "NUMBER":
        return f"NUMBER({int(precision)},{int(scale)})"
    if kind == "TEXT":
        return "VARCHAR"
    if kind in {"BOOLEAN", "DATE", "TIMESTAMP_NTZ", "ARRAY", "VARIANT", "OBJECT", "FLOAT"}:
        return kind
    raise LoadError(f"Unsupported target column type {data_type!r}")


def select_expressions(schema: pa.Schema, target: Mapping[str, Column], table: str) -> list[str]:
    """``$1:"col"`` cast to each target column, in the file's column order.

    A file column the table does not have is refused: it would otherwise be
    dropped silently, and in RAW an unexpected column is the shape of a leak.
    """
    exprs = []
    for f in schema:
        name = validate_identifier(f.name, what=f"{table} file column")
        col = target.get(name)
        if col is None:
            raise LoadError(f"{table}: file column {f.name!r} has no column in the table")
        path = f'$1:"{f.name}"'
        if pa.types.is_timestamp(f.type):
            if col.sql_type != "TIMESTAMP_NTZ":
                raise LoadError(f"{table}.{f.name}: timestamp file column into {col.sql_type}")
            if f.type.tz is not None:
                raise LoadError(f"{table}.{f.name}: zoned timestamps are not expected")
            expr = f"TO_TIMESTAMP_NTZ({path}::NUMBER, {_TIMESTAMP_SCALE[f.type.unit]})"
        elif col.sql_type == "VARIANT" and pa.types.is_string(f.type):
            expr = f"PARSE_JSON({path}::VARCHAR)"
        else:
            expr = f"{path}::{col.sql_type}"
        exprs.append(f"{expr} AS {col.name}")
    return exprs


def copy_parquet_sql(
    fq_table: str, columns: Sequence[str], stage_file: str, file_format: str, exprs: Sequence[str]
) -> str:
    return (
        f"COPY INTO {fq_table} ({', '.join(columns)})\n"
        f"FROM (SELECT {', '.join(exprs)}\n      FROM {stage_file})\n"
        f"FILE_FORMAT = (FORMAT_NAME = '{file_format}')\n"
        # FORCE: an empty target is checked first, and a re-load after
        # replace must not be skipped by COPY's 64-day load history.
        "ON_ERROR = ABORT_STATEMENT\nFORCE = TRUE"
    )


def amlworld_trans_copy_sql(
    fq_table: str, stage_file: str, file_format: str, timestamp_format: str, scale: int
) -> str:
    """Positional ``$1..$11`` (columns 3 and 5 share a header) plus M16's row number.

    ``METADATA$FILE_ROW_NUMBER`` under ``SKIP_HEADER = 1`` is the 1-based data-row
    ordinal (measured, M16) -- the same number the generator writes to
    ``TXN_LABELS.src_row_number``.
    """
    casts = {
        "txn_timestamp": f"TO_TIMESTAMP_NTZ($1, '{timestamp_format}')",
        "amount_received": f"$6::NUMBER(20,{scale})",
        "amount_paid": f"$8::NUMBER(20,{scale})",
        "is_laundering": "$11::NUMBER(1,0)",
    }
    exprs = [casts.get(col, f"${i}") for i, col in enumerate(LEDGER_COLUMNS, start=1)]
    columns = [SRC_ROW_NUMBER, *LEDGER_COLUMNS]
    return (
        f"COPY INTO {fq_table} ({', '.join(columns)})\n"
        f"FROM (SELECT METADATA$FILE_ROW_NUMBER, {', '.join(exprs)}\n      FROM {stage_file})\n"
        f"FILE_FORMAT = (FORMAT_NAME = '{file_format}')\n"
        "ON_ERROR = ABORT_STATEMENT\nFORCE = TRUE"
    )


def amlworld_accounts_copy_sql(fq_table: str, stage_file: str, file_format: str) -> str:
    exprs = [f"${i}" for i in range(1, len(ACCOUNTS_COLUMNS) + 1)]
    return (
        f"COPY INTO {fq_table} ({', '.join(ACCOUNTS_COLUMNS)})\n"
        f"FROM (SELECT {', '.join(exprs)} FROM {stage_file})\n"
        f"FILE_FORMAT = (FORMAT_NAME = '{file_format}')\n"
        "ON_ERROR = ABORT_STATEMENT\nFORCE = TRUE"
    )


#: The RAW_TRANSACTIONS columns a promoted leg fills. amount and amount_inr are
#: the same rupee value: injected deposits are in the base currency by
#: construction, and promotion refuses any that is not.
_PROMOTED = (
    "txn_id",
    "txn_pair_id",
    "leg_side",
    "account_id",
    "counterparty_account_id",
    "txn_timestamp",
    "amount",
    "currency",
    "amount_inr",
    "txn_type",
    "channel",
    "reference_text",
    "source_ref",
)


def promotion_sql(fq_transactions: str, fq_pending: str, fq_clauses: str) -> str:
    """Pending legs -> RAW_TRANSACTIONS, rupees resolved from the clause in force.

    Binds: rounding decimals (twice). The threshold is joined, never written.
    """
    amount = "ROUND(p.amount_sign * p.amount_ctr_ratio * c.threshold_lower, ?)"
    select = [
        f"{amount} AS {col}" if col in ("amount", "amount_inr") else f"p.{col}" for col in _PROMOTED
    ]
    return (
        f"INSERT INTO {fq_transactions} ({', '.join(_PROMOTED)})\n"
        f"SELECT {', '.join(select)}\n"
        f"FROM {fq_pending} p\n"
        f"JOIN {fq_clauses} c ON c.clause_id = p.ctr_clause_id\n"
        "WHERE p.resolved_at IS NULL"
    )


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #


@dataclass
class Check:
    name: str
    ok: bool
    value: str
    detail: str = ""


@dataclass
class LoadReport:
    run_key: str
    rows: dict[str, int] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


class Loader:
    """Drive one load over an open session. See the module docstring for order."""

    def __init__(
        self,
        settings: Settings,
        session: Any,
        *,
        data_root: Path | None = None,
        replace: bool = False,
        source: bool = True,
    ) -> None:
        self.settings = settings
        self.session = session
        self.data_root = data_root or settings.repo_root
        self.replace = replace
        self.source = source
        ctx = settings.sql_context()
        self.fq: Mapping[str, str] = ctx["fq"]
        self.stage: Mapping[str, str] = ctx["stage"]
        self.parquet_format = f"{self.fq['raw']}.FF_PARQUET"
        self.csv_format = f"{self.fq['raw']}.FF_CSV_HEADER"
        generation = settings.generation
        self.base_currency = str(generation["fx"]["base_currency"])
        self.decimals = int(generation["fx"]["rounding_decimals"])
        ledger = generation["source"]["ledger"]
        self.amount_scale = int(ledger["amount_scale"])
        self.timestamp_format = str(ledger["snowflake_timestamp_format"])
        self.internal_bank = str(generation["internal_accounts"]["src_bank_id"])

    # ---- helpers ---------------------------------------------------------- #

    def _sql(self, sql: str, params: Sequence[Any] | None = None) -> list[Any]:
        return execute(self.session, sql, params)

    def _scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        rows = self._sql(sql, params)
        return rows[0][0] if rows else None

    def _table(self, zone: str, table: str) -> str:
        return f"{self.fq[ZONES[zone][0]]}.{table}"

    def _stage(self, zone: str) -> str:
        schema_key, stage_key = ZONES[zone]
        return f"@{self.fq[schema_key]}.{self.stage[stage_key]}"

    def _sources(self) -> list[tuple[Dataset, str]]:
        generation = self.settings.generation
        return [
            (source_dataset(self.data_root, generation, "ledger"), AMLWORLD_TRANS),
            (source_dataset(self.data_root, generation, "accounts"), AMLWORLD_ACCOUNTS),
        ]

    def target_tables(self, bundle: Bundle) -> list[str]:
        """Every table this load writes, fully qualified."""
        tables = [self._table(a.zone, a.table) for a in bundle.artifacts]
        if self.source:
            tables += [self._table("raw", AMLWORLD_TRANS), self._table("raw", AMLWORLD_ACCOUNTS)]
        return tables

    def columns(self, schema_key: str) -> dict[str, dict[str, Column]]:
        """Live column types per table of one schema, from INFORMATION_SCHEMA."""
        db, schema = self.fq[schema_key].split(".")
        rows = self._sql(
            f"SELECT table_name, column_name, data_type, numeric_precision, numeric_scale "
            f"FROM {db}.INFORMATION_SCHEMA.COLUMNS WHERE table_schema = ?",
            [schema],
        )
        out: dict[str, dict[str, Column]] = {}
        for table, column, data_type, precision, scale in (tuple(r) for r in rows):
            name = validate_identifier(column, what="Live column name")
            out.setdefault(table.upper(), {})[name] = Column(
                name, column_type(data_type, precision, scale)
            )
        return out

    # ---- pre-flight --------------------------------------------------------- #

    def preflight(self, bundle: Bundle) -> None:
        raw_cols = self.columns("raw")
        gt_cols = self.columns("ground_truth")
        live = {"raw": raw_cols, "ground_truth": gt_cols}
        for a in bundle.artifacts:
            if a.table not in live[a.zone]:
                raise LoadError(f"{self._table(a.zone, a.table)} does not exist; deploy ddl first")

        if self.source:
            scales = {
                c.name: c.sql_type
                for c in raw_cols.get(AMLWORLD_TRANS, {}).values()
                if c.name.startswith("AMOUNT_")
            }
            want = f"NUMBER(20,{self.amount_scale})"
            if not scales or any(t != want for t in scales.values()):
                raise LoadError(
                    f"{AMLWORLD_TRANS} amounts are {scales}, not {want}: COPY would round the "
                    "ledger's 6 dp values (M14). Re-create the table from sql/01_ddl."
                )

        pending = pq.read_table(bundle.artifact("ground_truth", PENDING).path)
        clause_ids = sorted(set(pending["ctr_clause_id"].to_pylist()))
        currencies = set(pending["currency"].to_pylist())
        if currencies - {self.base_currency}:
            raise LoadError(f"{PENDING}: currencies {currencies}; only {self.base_currency}")
        clauses = f"{self.fq['governance']}.V_POLICY_CLAUSE_CURRENT"
        for clause_id in clause_ids:
            found = self._scalar(f"SELECT COUNT(*) FROM {clauses} WHERE clause_id = ?", [clause_id])
            if found != 1:
                raise LoadError(
                    f"Clause {clause_id!r} is not in force in V_POLICY_CLAUSE_CURRENT ({found} "
                    "rows). Deploy the policy stage before load: injected amounts resolve from it."
                )

        gt_stage = self._stage("ground_truth").removeprefix("@")
        grants = [r.as_dict() for r in self._sql(f"SHOW GRANTS ON STAGE {gt_stage}")]
        other = [
            (g["privilege"], g["grantee_name"]) for g in grants if g["privilege"] != "OWNERSHIP"
        ]
        if other:
            raise LoadError(f"{gt_stage} is granted beyond ownership: {other}. Labels would leak.")

        occupied = [
            (t, n)
            for t in self.target_tables(bundle)
            if (n := self._scalar(f"SELECT COUNT(*) FROM {t}"))
        ]
        if occupied and not self.replace:
            raise LoadError(
                f"Target tables are not empty: {occupied}. Nothing was written. Re-run with "
                "--replace to delete and reload exactly these tables."
            )

    # ---- upload ------------------------------------------------------------- #

    def _put(self, local: Path, stage_dir: str, *, compress: bool) -> str:
        """PUT one file; return its staged path. Content-addressed dirs make a skip safe."""
        results = self.session.file.put(
            str(local.resolve()), stage_dir, auto_compress=compress, overwrite=False
        )
        (result,) = results
        return f"{stage_dir}/{result.target}"

    def upload(self, bundle: Bundle) -> dict[str, str]:
        staged: dict[str, str] = {}
        for a in bundle.artifacts:
            stage_dir = f"{self._stage(a.zone)}/{self.settings.profile}/{bundle.run_key}/{a.zone}"
            staged[f"{a.zone}/{a.table}"] = self._put(a.path, stage_dir, compress=False)
        if self.source:
            for ds, table in self._sources():
                stage_dir = (
                    f"@{self.fq['raw']}.{self.stage['data']}/{SOURCE_PREFIX}/{_content_key(ds)}"
                )
                staged[f"source/{table}"] = self._put(ds.path, stage_dir, compress=True)
        return staged

    # ---- the transaction ---------------------------------------------------- #

    def _copy_all(self, bundle: Bundle, staged: Mapping[str, str]) -> None:
        live = {"raw": self.columns("raw"), "ground_truth": self.columns("ground_truth")}
        for a in bundle.artifacts:
            target = live[a.zone][a.table]
            schema = pq.read_schema(a.path)
            exprs = select_expressions(schema, target, a.table)
            names = [target[validate_identifier(f.name, what="File column")].name for f in schema]
            self._sql(
                copy_parquet_sql(
                    self._table(a.zone, a.table),
                    names,
                    staged[f"{a.zone}/{a.table}"],
                    self.parquet_format,
                    exprs,
                )
            )
        if self.source:
            self._sql(
                amlworld_trans_copy_sql(
                    self._table("raw", AMLWORLD_TRANS),
                    staged[f"source/{AMLWORLD_TRANS}"],
                    self.csv_format,
                    self.timestamp_format,
                    self.amount_scale,
                )
            )
            self._sql(
                amlworld_accounts_copy_sql(
                    self._table("raw", AMLWORLD_ACCOUNTS),
                    staged[f"source/{AMLWORLD_ACCOUNTS}"],
                    self.csv_format,
                )
            )

    def _promote(self) -> int:
        pending = self._table("ground_truth", PENDING)
        clauses = f"{self.fq['governance']}.V_POLICY_CLAUSE_CURRENT"
        waiting = self._scalar(f"SELECT COUNT(*) FROM {pending} WHERE resolved_at IS NULL")
        rows = self._sql(
            promotion_sql(self._table("raw", TRANSACTIONS), pending, clauses),
            [self.decimals, self.decimals],
        )
        inserted = int(rows[0][0]) if rows else 0
        if inserted != waiting:
            raise LoadError(
                f"Promoted {inserted} of {waiting} pending legs: some name a clause that is not "
                "in force. Nothing is committed."
            )
        self._sql(
            f"UPDATE {pending} SET resolved_at = CURRENT_TIMESTAMP() WHERE resolved_at IS NULL"
        )
        return inserted

    def run(self, bundle: Bundle) -> LoadReport:
        check_bundle(bundle, self.settings)
        self.preflight(bundle)
        staged = self.upload(bundle)
        report = LoadReport(run_key=bundle.run_key)
        self._sql("BEGIN")
        try:
            if self.replace:
                for table in self.target_tables(bundle):
                    self._sql(f"DELETE FROM {table}")
            self._copy_all(bundle, staged)
            promoted = self._promote()
            report.rows = {
                t: int(self._scalar(f"SELECT COUNT(*) FROM {t}"))
                for t in self.target_tables(bundle)
            }
            report.checks = self.verify(bundle, promoted, report.rows)
            if not report.ok:
                failed = [c.name for c in report.checks if not c.ok]
                raise LoadError(f"Post-load checks failed: {failed}")
        except BaseException:
            self._sql("ROLLBACK")
            raise
        self._sql("COMMIT")
        return report

    # ---- verification ------------------------------------------------------- #

    def verify(self, bundle: Bundle, promoted: int, rows: Mapping[str, int]) -> list[Check]:
        checks: list[Check] = []
        for a in bundle.artifacts:
            table = self._table(a.zone, a.table)
            expected = a.rows + (promoted if a.table == TRANSACTIONS else 0)
            checks.append(
                Check(
                    f"rows {a.zone}.{a.table}",
                    rows[table] == expected,
                    f"{rows[table]:,}",
                    f"expected {expected:,}",
                )
            )
        pending_rows = bundle.artifact("ground_truth", PENDING).rows
        checks.append(
            Check(
                "injected legs promoted",
                promoted == pending_rows,
                f"{promoted}",
                f"pending {pending_rows}",
            )
        )

        tx = self._table("raw", TRANSACTIONS)
        labels = self._table("ground_truth", LABELS)
        unlabelled = self._scalar(
            f"SELECT COUNT(*) FROM {tx} t LEFT JOIN {labels} l ON l.txn_id = t.txn_id "
            "WHERE l.txn_id IS NULL"
        )
        checks.append(Check("every leg labelled", unlabelled == 0, f"{unlabelled} unlabelled"))

        if self.source:
            ledger, accounts = self._sources()
            trans = self._table("raw", AMLWORLD_TRANS)
            n, illicit = self._sql(f"SELECT COUNT(*), SUM(is_laundering) FROM {trans}")[0]
            checks.append(
                Check(
                    "ledger rows (M5)",
                    n == ledger[0].require("rows")
                    and illicit == ledger[0].require("laundering_rows"),
                    f"{n:,} rows / {illicit:,} laundering",
                    f"manifest {ledger[0].require('rows'):,} / "
                    f"{ledger[0].require('laundering_rows'):,}",
                )
            )
            n_acc = self._scalar(f"SELECT COUNT(*) FROM {self._table('raw', AMLWORLD_ACCOUNTS)}")
            checks.append(
                Check(
                    "accounts rows",
                    n_acc == accounts[0].require("rows"),
                    f"{n_acc:,}",
                    f"manifest {accounts[0].require('rows'):,}",
                )
            )
            checks.extend(self._verify_source_join(tx, labels, trans))
        return checks

    def _verify_source_join(self, tx: str, labels: str, trans: str) -> Iterable[Check]:
        """M16 in practice, and pair balance explained by the source (M28)."""
        id_map = self._table("raw", "ID_MAP_ACCOUNT")

        # Same rule as ids.normalise_bank_id: numeric ids lose their zero padding.
        def side(bank: str, account: str) -> str:
            return (
                f"(a.src_bank_id = COALESCE(NULLIF(LTRIM({bank}, '0'), ''), '0') "
                f"AND a.src_account_number = {account})"
            )

        rows = self._sql(
            f"""SELECT COUNT(*),
                   COUNT_IF(s.src_row_number IS NULL),
                   COUNT_IF(s.txn_timestamp <> t.txn_timestamp),
                   COUNT_IF((s.is_laundering = 1) <> l.is_illicit),
                   COUNT_IF(a.src_bank_id <> ? AND NOT (
                       {side("s.from_bank", "s.from_account")}
                    OR {side("s.to_bank", "s.to_account")}))
                FROM {labels} l
                JOIN {tx} t ON t.txn_id = l.txn_id
                JOIN {id_map} a ON a.account_id = t.account_id
                LEFT JOIN {trans} s ON s.src_row_number = l.src_row_number
                WHERE l.src_row_number IS NOT NULL""",
            [self.internal_bank],
        )
        total, orphan, ts, flag, account = (int(v) for v in rows[0])
        yield Check(
            "labels join the ledger (M16)",
            total > 0 and orphan == ts == flag == account == 0,
            f"{total:,} source legs",
            f"no row {orphan}, timestamp {ts}, is_laundering {flag}, account {account}",
        )
        rows = self._sql(
            f"""WITH pair AS (
                    SELECT t.txn_pair_id, SUM(t.amount_inr) AS net, MIN(l.src_row_number) AS src
                    FROM {tx} t JOIN {labels} l ON l.txn_id = t.txn_id
                    GROUP BY t.txn_pair_id)
                SELECT COUNT_IF(net <> 0),
                       COUNT_IF(net <> 0
                                AND (src IS NULL OR s.payment_currency = s.receiving_currency))
                FROM pair p LEFT JOIN {trans} s ON s.src_row_number = p.src"""
        )
        unbalanced, unexplained = (int(v) for v in rows[0])
        yield Check(
            "pairs balance in INR (M28)",
            unexplained == 0,
            f"{unbalanced} unbalanced",
            f"{unexplained} not a cross-currency source pair",
        )


def open_bundle(settings: Settings, out_dir: Path | None = None) -> Bundle:
    """Read and check ``out_dir`` (default ``output.root/<profile>``) with no connection.

    Called before a session is opened, so a failed or foreign build is refused
    even when the target account or database is unreachable.
    """
    output = settings.generation["output"]
    bundle = read_bundle(out_dir or settings.repo_root / str(output["root"]) / settings.profile)
    check_bundle(bundle, settings)
    return bundle


def load(
    settings: Settings,
    session: Any,
    *,
    bundle: Bundle,
    data_root: Path | None = None,
    replace: bool = False,
    source: bool = True,
) -> LoadReport:
    """Load a checked ``bundle`` (:func:`open_bundle`) through ``session``."""
    try:
        return Loader(settings, session, data_root=data_root, replace=replace, source=source).run(
            bundle
        )
    except ConfigError as exc:
        raise LoadError(str(exc)) from exc
