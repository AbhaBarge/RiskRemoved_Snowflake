"""Positional readers for the AMLworld source files.

Three facts, all measured against HI-Small on 2026-09-26, shape this module:

* **The ledger header repeats ``Account``** (columns 3 and 5). A header-keyed
  reader either errors or silently drops one of them, so columns are read by
  position and the header is *asserted* rather than trusted -- a reordered
  revision fails here instead of swapping sender and receiver.
* **Bank ids are zero-padded in the ledger and not in the accounts file**
  (build log M1). Every bank id is normalised through
  :func:`risk_copilot.ids.normalise_bank_id` on read -- the one implementation,
  applied to each distinct value -- so the files can only ever agree.
* **Amounts are exact decimals.** Non-Bitcoin amounts carry 2 decimal places,
  Bitcoin 6 (M14). They are parsed as ``decimal128`` at the configured scale;
  pyarrow refuses to rescale, so a value with more digits fails the read rather
  than being rounded. No amount ever passes through a binary float.

``src_row_number`` is the 1-based ordinal of the data row, header excluded. That
is what Snowflake's ``METADATA$FILE_ROW_NUMBER`` reports under ``SKIP_HEADER = 1``
(measured on ASHWINRAVI004, M16), so a generated label joins exactly onto the
verbatim ``RAW_AMLWORLD_TRANS`` row that ``load`` copies in.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pa_csv
import yaml

from risk_copilot.config import data_manifest_file
from risk_copilot.generation import GenerationError
from risk_copilot.ids import normalise_bank_id

#: The ledger's header, verbatim. Asserted, never used to name columns.
LEDGER_HEADER = (
    "Timestamp",
    "From Bank",
    "Account",
    "To Bank",
    "Account",
    "Amount Received",
    "Receiving Currency",
    "Amount Paid",
    "Payment Currency",
    "Payment Format",
    "Is Laundering",
)
#: Positional names, matching ``RAW.RAW_AMLWORLD_TRANS``. The Patterns file uses
#: the same eleven fields in the same order.
LEDGER_COLUMNS = (
    "txn_timestamp",
    "from_bank",
    "from_account",
    "to_bank",
    "to_account",
    "amount_received",
    "receiving_currency",
    "amount_paid",
    "payment_currency",
    "payment_format",
    "is_laundering",
)
ACCOUNTS_HEADER = ("Bank Name", "Bank ID", "Account Number", "Entity ID", "Entity Name")
ACCOUNTS_COLUMNS = ("bank_name", "bank_id", "account_number", "entity_id", "entity_name")

SRC_ROW_NUMBER = "src_row_number"
ENTITY_PREFIX = "entity_prefix"
BANK_COLUMNS = ("from_bank", "to_bank")

#: "Corporation #33520" -> "Corporation". Every one of 518,581 names matches.
_ENTITY_NAME_RE = re.compile(r"^(?P<prefix>\S.*?) #\d+$")
KEY_SEP = "\x1f"


class SourceError(GenerationError):
    """A source file is missing, malformed, or disagrees with the manifest."""


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Dataset:
    """One ``data/MANIFEST.yml`` entry, resolved against a data root."""

    key: str
    path: Path
    spec: Mapping[str, Any] = field(repr=False)

    def expected(self, name: str) -> int | None:
        """An integer the manifest declares for this dataset, e.g. ``rows``."""
        value = self.spec.get(name)
        return None if value is None else int(value)

    def require(self, name: str) -> int:
        """Like :meth:`expected`, but a missing declaration is an error.

        Used for the totals the generator asserts: an assertion against a value
        nobody declared would be vacuous.
        """
        value = self.expected(name)
        if value is None:
            raise SourceError(
                f"data/MANIFEST.yml: dataset {self.key!r} must declare {name!r}; the "
                "generator asserts it against the file rather than trusting a literal."
            )
        return value


def dataset(data_root: Path, key: str) -> Dataset:
    """Resolve manifest entry ``key`` under ``data_root`` and check it is present.

    Size is checked when the manifest declares it -- the cheap half of what
    ``doctor --checksums`` verifies -- so a truncated download fails before a
    five-million-row parse rather than halfway through one.
    """
    manifest_path = data_manifest_file(data_root)
    if not manifest_path.is_file():
        raise SourceError(f"Dataset manifest not found: {manifest_path}")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    datasets = manifest.get("datasets") or {}
    if key not in datasets:
        raise SourceError(f"{manifest_path}: no dataset {key!r}; declared: {sorted(datasets)}")

    spec: Mapping[str, Any] = datasets[key]
    path = data_root / str(spec.get("path", ""))
    if not path.is_file():
        raise SourceError(f"Dataset {key!r} not found at {path}. Run `riskcopilot doctor`.")
    declared = spec.get("bytes")
    if declared and path.stat().st_size != int(declared):
        raise SourceError(
            f"Dataset {key!r}: expected {int(declared):,} bytes, found "
            f"{path.stat().st_size:,}. The file is truncated or a different revision."
        )
    return Dataset(key=key, path=path, spec=spec)


def source_dataset(data_root: Path, generation: Mapping[str, Any], role: str) -> Dataset:
    """Resolve a source role (``ledger`` | ``accounts`` | ``patterns``) via config."""
    roles = (generation.get("source") or {}).get("datasets") or {}
    if role not in roles:
        raise SourceError(f"conf/generation.yml: source.datasets has no role {role!r}")
    return dataset(data_root, str(roles[role]))


@dataclass(frozen=True)
class LedgerFormat:
    """File-format parameters from ``source.ledger`` in ``conf/generation.yml``."""

    timestamp_format: str
    amount_precision: int
    amount_scale: int

    @classmethod
    def from_config(cls, generation: Mapping[str, Any]) -> LedgerFormat:
        try:
            spec = generation["source"]["ledger"]
            return cls(
                timestamp_format=str(spec["timestamp_format"]),
                amount_precision=int(spec["amount_precision"]),
                amount_scale=int(spec["amount_scale"]),
            )
        except (KeyError, TypeError) as exc:
            raise SourceError(f"conf/generation.yml: source.ledger is incomplete: {exc}") from exc

    @property
    def amount_type(self) -> pa.DataType:
        return pa.decimal128(self.amount_precision, self.amount_scale)


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #


def _check_header(path: Path, expected: tuple[str, ...]) -> None:
    with path.open("rb") as handle:
        first = handle.readline().decode("utf-8-sig").rstrip("\r\n")
    actual = tuple(first.split(","))
    if actual != expected:
        raise SourceError(
            f"{path.name}: unexpected header. Columns are read by position, so a "
            f"changed header means a changed layout.\n  expected {list(expected)}\n"
            f"  actual   {list(actual)}"
        )


def _read_positional(
    path: Path, columns: tuple[str, ...], types: Mapping[str, pa.DataType], **convert: Any
) -> pa.Table:
    try:
        return pa_csv.read_csv(
            path,
            read_options=pa_csv.ReadOptions(column_names=list(columns), skip_rows=1),
            convert_options=pa_csv.ConvertOptions(
                column_types=dict(types),
                strings_can_be_null=False,
                quoted_strings_can_be_null=False,
                **convert,
            ),
        )
    except pa.ArrowInvalid as exc:
        raise SourceError(f"{path.name}: {exc}") from exc


def _require_no_nulls(table: pa.Table, name: str) -> None:
    nulls = {col: table[col].null_count for col in table.column_names if table[col].null_count}
    if nulls:
        raise SourceError(f"{name}: empty or unparseable values per column: {nulls}")


def _check_rows(table: pa.Table, name: str, expected: int | None) -> None:
    if expected is not None and table.num_rows != expected:
        raise SourceError(f"{name}: {table.num_rows:,} data rows, manifest declares {expected:,}.")


def map_distinct(values: pa.ChunkedArray | pa.Array, fn: Callable[[str], str]) -> pa.Array:
    """Apply a scalar string function to each *distinct* value of ``values``.

    Lets the vectorised readers reuse the canonical scalar implementation
    (``ids.normalise_bank_id``) instead of re-implementing it in arrow kernels,
    where it could drift. Distinct bank ids number in the tens of thousands
    against five million rows, so the Python work is negligible.
    """
    array = values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values
    encoded = pc.dictionary_encode(array)
    mapped = pa.array([fn(v) for v in encoded.dictionary.to_pylist()], type=pa.string())
    return pc.take(mapped, encoded.indices)


def composite_key(bank: pa.ChunkedArray | pa.Array, account: pa.ChunkedArray | pa.Array) -> Any:
    """``bank \\x1f account`` per row, for vectorised uniqueness checks and joins."""
    return pc.binary_join_element_wise(bank, account, KEY_SEP)


def read_ledger(path: Path, fmt: LedgerFormat, *, expected_rows: int | None = None) -> pa.Table:
    """Read ``HI-Small_Trans.csv`` positionally.

    Returns ``src_row_number`` followed by :data:`LEDGER_COLUMNS`, with bank ids
    normalised, amounts as ``decimal128`` and ``is_laundering`` as ``int8`` in
    ``{0, 1}``. Row order is the file's order.
    """
    _check_header(path, LEDGER_HEADER)
    types: dict[str, pa.DataType] = {col: pa.string() for col in LEDGER_COLUMNS}
    types.update(
        txn_timestamp=pa.timestamp("s"),
        amount_received=fmt.amount_type,
        amount_paid=fmt.amount_type,
        is_laundering=pa.int8(),
    )
    table = _read_positional(path, LEDGER_COLUMNS, types, timestamp_parsers=[fmt.timestamp_format])
    _require_no_nulls(table, path.name)
    _check_rows(table, path.name, expected_rows)

    labels = pc.unique(table["is_laundering"]).to_pylist()
    if not set(labels) <= {0, 1}:
        raise SourceError(f"{path.name}: is_laundering must be 0 or 1, found {sorted(labels)}")

    for col in BANK_COLUMNS:
        table = table.set_column(
            table.schema.get_field_index(col), col, map_distinct(table[col], normalise_bank_id)
        )
    row_numbers = pa.array(np.arange(1, table.num_rows + 1, dtype=np.int64))
    return table.add_column(0, SRC_ROW_NUMBER, row_numbers)


def read_accounts(path: Path, *, expected_rows: int | None = None) -> pa.Table:
    """Read ``HI-Small_accounts.csv``, normalised and validated.

    Adds :data:`ENTITY_PREFIX` (``"Sole Proprietorship"`` from
    ``"Sole Proprietorship #50438"``). Fails if an entity name has another shape,
    if ``(bank_id, account_number)`` is not unique after normalisation, or if one
    entity carries two names.
    """
    _check_header(path, ACCOUNTS_HEADER)
    table = _read_positional(path, ACCOUNTS_COLUMNS, {col: pa.string() for col in ACCOUNTS_COLUMNS})
    _require_no_nulls(table, path.name)
    _check_rows(table, path.name, expected_rows)

    table = table.set_column(
        table.schema.get_field_index("bank_id"),
        "bank_id",
        map_distinct(table["bank_id"], normalise_bank_id),
    )

    names = table["entity_name"]
    shaped = pc.match_substring_regex(names, _ENTITY_NAME_RE.pattern)
    if not pc.all(shaped).as_py():
        bad = pc.filter(names, pc.invert(shaped))[:5].to_pylist()
        raise SourceError(f"{path.name}: entity names not of the form '<type> #<n>': {bad}")
    table = table.append_column(ENTITY_PREFIX, pc.replace_substring_regex(names, r" #\d+$", ""))

    keys = composite_key(table["bank_id"], table["account_number"])
    if len(pc.unique(keys)) != table.num_rows:
        counts = pc.value_counts(keys)
        dupes = pc.filter(counts.field("values"), pc.greater(counts.field("counts"), 1))
        raise SourceError(
            f"{path.name}: (bank_id, account_number) repeats after bank-id "
            f"normalisation, so ownership is ambiguous: "
            f"{[v.replace(KEY_SEP, '/') for v in dupes[:5].to_pylist()]}"
        )

    per_entity = table.group_by("entity_id").aggregate([("entity_name", "count_distinct")])
    if pc.max(per_entity["entity_name_count_distinct"]).as_py() != 1:
        raise SourceError(f"{path.name}: an entity_id carries more than one entity_name")
    return table


def load_ledger(data_root: Path, generation: Mapping[str, Any]) -> pa.Table:
    """Read the configured ledger, asserting its row count against the manifest."""
    ds = source_dataset(data_root, generation, "ledger")
    return read_ledger(
        ds.path, LedgerFormat.from_config(generation), expected_rows=ds.expected("rows")
    )


def load_accounts(data_root: Path, generation: Mapping[str, Any]) -> pa.Table:
    """Read the configured accounts file, asserting its row count against the manifest."""
    ds = source_dataset(data_root, generation, "accounts")
    return read_accounts(ds.path, expected_rows=ds.expected("rows"))


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #


def resolve_party_type(prefix: str, mapping: Mapping[str, str], default: str | None) -> str:
    """Map an entity-name prefix to a party type (``entities.party_type_from_entity_name``).

    With ``party_type_default: null`` (decision D10) an unmapped prefix fails:
    a guessed default is what once made ``Country #1`` an individual.
    """
    if prefix in mapping:
        return str(mapping[prefix])
    if default is None:
        raise SourceError(
            f"Entity prefix {prefix!r} is not mapped in "
            "entities.party_type_from_entity_name, and party_type_default is null. "
            "Map it explicitly rather than guessing."
        )
    return str(default)


class EntityOwnership:
    """The account -> entity backbone read from ``HI-Small_accounts.csv``.

    Lookups normalise the bank id, so a caller holding a padded ledger id and a
    caller holding an accounts-file id reach the same owner.
    """

    def __init__(self, accounts: pa.Table) -> None:
        self.table = accounts
        banks = accounts["bank_id"].to_pylist()
        numbers = accounts["account_number"].to_pylist()
        entities = accounts["entity_id"].to_pylist()
        self._owner: dict[tuple[str, str], str] = {
            (b, n): e for b, n, e in zip(banks, numbers, entities, strict=True)
        }
        self._name: dict[str, str] = dict(
            zip(entities, accounts["entity_name"].to_pylist(), strict=True)
        )
        self._prefix: dict[str, str] = dict(
            zip(entities, accounts[ENTITY_PREFIX].to_pylist(), strict=True)
        )
        grouped: dict[str, list[tuple[str, str]]] = {}
        for key, entity in self._owner.items():
            grouped.setdefault(entity, []).append(key)
        self._accounts = {entity: tuple(sorted(keys)) for entity, keys in grouped.items()}

    def __len__(self) -> int:
        return len(self._owner)

    @property
    def entity_count(self) -> int:
        return len(self._name)

    def owner_of(self, bank_id: str, account_number: str) -> str | None:
        """Owning entity of ``(bank_id, account_number)``, or ``None`` if unknown."""
        return self._owner.get((normalise_bank_id(bank_id), str(account_number).strip()))

    def accounts_of(self, entity_id: str) -> tuple[tuple[str, str], ...]:
        """Every ``(bank_id, account_number)`` the entity owns, sorted."""
        return self._accounts.get(entity_id, ())

    def entity_name(self, entity_id: str) -> str:
        return self._name[entity_id]

    def entity_prefix(self, entity_id: str) -> str:
        return self._prefix[entity_id]

    def party_type(self, entity_id: str, mapping: Mapping[str, str], default: str | None) -> str:
        return resolve_party_type(self.entity_prefix(entity_id), mapping, default)
