"""Positional source readers (Phase 2, step 2).

Everything runs against ``tests/fixtures/amlworld_mini``; the real 950 MB
files are never needed. The measured defects these tests pin are the ones that
would silently empty every join: padded bank ids (M1), the duplicate
``Account`` header, and amounts that are not all 2 dp (M14).
"""

from __future__ import annotations

import datetime as dt
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from risk_copilot.config import load_settings
from risk_copilot.generation.source import (
    ENTITY_PREFIX,
    LEDGER_COLUMNS,
    SRC_ROW_NUMBER,
    EntityOwnership,
    LedgerFormat,
    SourceError,
    dataset,
    load_accounts,
    load_ledger,
    map_distinct,
    read_accounts,
    read_ledger,
    resolve_party_type,
)
from risk_copilot.ids import normalise_bank_id

pytestmark = pytest.mark.unit


@pytest.fixture
def generation(repo_root: Path) -> dict[str, Any]:
    """The real ``conf/generation.yml``: the fixture is read with production settings."""
    return load_settings(repo_root=repo_root, profile="dev").generation


@pytest.fixture
def fmt(generation: dict[str, Any]) -> LedgerFormat:
    return LedgerFormat.from_config(generation)


@pytest.fixture
def copy_root(amlworld_mini: Path, tmp_path: Path) -> Path:
    """A writable copy of the fixture, for tests that corrupt a file."""
    target = tmp_path / "amlworld_mini"
    shutil.copytree(amlworld_mini, target)
    return target


def _ledger_path(root: Path) -> Path:
    return root / "data" / "Kaggle" / "HI-Small_Trans.csv"


def _accounts_path(root: Path) -> Path:
    return root / "data" / "Kaggle" / "HI-Small_accounts.csv"


def _rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"fixture no longer contains {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="")


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


def test_ledger_reads_every_row_positionally(
    amlworld_mini: Path, generation: dict[str, Any]
) -> None:
    table = load_ledger(amlworld_mini, generation)
    assert table.num_rows == 16
    assert table.column_names == [SRC_ROW_NUMBER, *LEDGER_COLUMNS]


def test_duplicate_account_header_keeps_both_sides(amlworld_mini: Path, fmt: LedgerFormat) -> None:
    """Columns 3 and 5 share the header "Account"; sender and receiver must both survive."""
    row = read_ledger(_ledger_path(amlworld_mini), fmt).slice(0, 1).to_pylist()[0]
    assert (row["from_account"], row["to_account"]) == ("8000A0010", "8000A0030")


def test_src_row_number_is_the_one_based_data_row(amlworld_mini: Path, fmt: LedgerFormat) -> None:
    """Matches METADATA$FILE_ROW_NUMBER under SKIP_HEADER = 1 (measured, M16)."""
    table = read_ledger(_ledger_path(amlworld_mini), fmt)
    assert table[SRC_ROW_NUMBER].to_pylist() == list(range(1, 17))
    lines = _ledger_path(amlworld_mini).read_text(encoding="utf-8").splitlines()
    tenth = table.slice(9, 1).to_pylist()[0]
    assert lines[tenth[SRC_ROW_NUMBER]].startswith("2022/09/03 14:22,021174,8000A0060")


def test_padded_bank_ids_are_normalised(amlworld_mini: Path, fmt: LedgerFormat) -> None:
    table = read_ledger(_ledger_path(amlworld_mini), fmt)
    banks = set(table["from_bank"].to_pylist()) | set(table["to_bank"].to_pylist())
    assert banks == {"1", "10", "20", "70", "21174"}


def test_amounts_are_exact_decimals(amlworld_mini: Path, fmt: LedgerFormat) -> None:
    table = read_ledger(_ledger_path(amlworld_mini), fmt)
    assert pa.types.is_decimal(table.schema.field("amount_paid").type)
    rows = table.to_pylist()
    assert rows[0]["amount_paid"] == Decimal("2848.96")
    # Bitcoin's six places survive; a 2 dp read would have rounded to 0.03.
    assert rows[6]["amount_paid"] == Decimal("0.025852")
    assert all(isinstance(r["amount_received"], Decimal) for r in rows)


def test_cross_currency_pair_keeps_both_sides(amlworld_mini: Path, fmt: LedgerFormat) -> None:
    row = read_ledger(_ledger_path(amlworld_mini), fmt).slice(8, 1).to_pylist()[0]
    assert (row["amount_paid"], row["payment_currency"]) == (Decimal("1000.00"), "US Dollar")
    assert (row["amount_received"], row["receiving_currency"]) == (Decimal("82500.00"), "Rupee")


def test_timestamps_and_labels_are_typed(amlworld_mini: Path, fmt: LedgerFormat) -> None:
    table = read_ledger(_ledger_path(amlworld_mini), fmt)
    assert table.slice(0, 1).to_pylist()[0]["txn_timestamp"] == dt.datetime(2022, 9, 1, 0, 6)
    assert sum(table["is_laundering"].to_pylist()) == 8


def test_crlf_ledger_reads_identically(
    copy_root: Path, amlworld_mini: Path, fmt: LedgerFormat
) -> None:
    path = _ledger_path(copy_root)
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert read_ledger(path, fmt).equals(read_ledger(_ledger_path(amlworld_mini), fmt))


def test_reordered_header_fails(copy_root: Path, fmt: LedgerFormat) -> None:
    _rewrite(
        _ledger_path(copy_root),
        "Amount Received,Receiving Currency,Amount Paid,Payment Currency",
        "Amount Paid,Payment Currency,Amount Received,Receiving Currency",
    )
    with pytest.raises(SourceError, match="unexpected header"):
        read_ledger(_ledger_path(copy_root), fmt)


def test_amount_with_more_places_than_the_scale_fails(copy_root: Path, fmt: LedgerFormat) -> None:
    """pyarrow refuses to rescale, so extra digits fail rather than round."""
    _rewrite(_ledger_path(copy_root), "2848.96,US Dollar,2848.96", "2848.96,US Dollar,2848.9612345")
    with pytest.raises(SourceError, match="data loss"):
        read_ledger(_ledger_path(copy_root), fmt)


def test_label_outside_zero_one_fails(copy_root: Path, fmt: LedgerFormat) -> None:
    _rewrite(_ledger_path(copy_root), "Reinvestment,0", "Reinvestment,2")
    with pytest.raises(SourceError, match="is_laundering"):
        read_ledger(_ledger_path(copy_root), fmt)


def test_row_count_is_asserted_against_the_manifest(
    copy_root: Path, generation: dict[str, Any]
) -> None:
    _rewrite(copy_root / "data" / "MANIFEST.yml", "rows: 16", "rows: 99")
    with pytest.raises(SourceError, match="manifest declares 99"):
        load_ledger(copy_root, generation)


# --------------------------------------------------------------------------- #
# Manifest resolution
# --------------------------------------------------------------------------- #


def test_unknown_dataset_fails(amlworld_mini: Path) -> None:
    with pytest.raises(SourceError, match="no dataset"):
        dataset(amlworld_mini, "not_a_dataset")


def test_missing_file_fails(copy_root: Path) -> None:
    _ledger_path(copy_root).unlink()
    with pytest.raises(SourceError, match="not found"):
        dataset(copy_root, "amlworld_transactions")


def test_declared_size_is_checked(copy_root: Path) -> None:
    _rewrite(copy_root / "data" / "MANIFEST.yml", "rows: 16", "rows: 16\n    bytes: 1")
    with pytest.raises(SourceError, match="truncated"):
        dataset(copy_root, "amlworld_transactions")


def test_required_total_must_be_declared(copy_root: Path) -> None:
    ds = dataset(copy_root, "amlworld_transactions")
    with pytest.raises(SourceError, match="must declare"):
        ds.require("laundering_attempts")


# --------------------------------------------------------------------------- #
# Accounts + ownership
# --------------------------------------------------------------------------- #


def test_accounts_carry_the_entity_prefix(amlworld_mini: Path, generation: dict[str, Any]) -> None:
    table = load_accounts(amlworld_mini, generation)
    assert table.num_rows == 18
    assert set(table[ENTITY_PREFIX].to_pylist()) == {
        "Corporation",
        "Sole Proprietorship",
        "Partnership",
        "Individual",
        "Country",
        "Direct",
    }


def test_padded_ledger_ids_resolve_to_unpadded_owners(
    amlworld_mini: Path, generation: dict[str, Any], fmt: LedgerFormat
) -> None:
    """The M1 regression: every ledger leg must find its owner."""
    ownership = EntityOwnership(load_accounts(amlworld_mini, generation))
    for row in read_ledger(_ledger_path(amlworld_mini), fmt).to_pylist():
        assert ownership.owner_of(row["from_bank"], row["from_account"]) is not None
        assert ownership.owner_of(row["to_bank"], row["to_account"]) is not None
    assert ownership.owner_of("010", "8000A0010") == ownership.owner_of("10", "8000A0010")


def test_account_numbers_are_scoped_by_bank(
    amlworld_mini: Path, generation: dict[str, Any]
) -> None:
    ownership = EntityOwnership(load_accounts(amlworld_mini, generation))
    assert ownership.owner_of("10", "8000A0070") == "800A00007"
    assert ownership.owner_of("70", "8000A0070") == "800A00009"


def test_ownership_groups_accounts_by_entity(
    amlworld_mini: Path, generation: dict[str, Any]
) -> None:
    ownership = EntityOwnership(load_accounts(amlworld_mini, generation))
    assert len(ownership) == 18
    assert ownership.entity_count == 10
    assert ownership.accounts_of("800A00002") == (
        ("1", "8000A0021"),
        ("10", "8000A0020"),
        ("20", "8000A0022"),
    )
    assert ownership.entity_prefix("800A00005") == "Country"
    assert ownership.owner_of("10", "DOES_NOT_EXIST") is None


def test_party_types_follow_the_configured_map(
    amlworld_mini: Path, generation: dict[str, Any]
) -> None:
    entities = generation["entities"]
    mapping, default = entities["party_type_from_entity_name"], entities["party_type_default"]
    ownership = EntityOwnership(load_accounts(amlworld_mini, generation))
    assert ownership.party_type("800A00002", mapping, default) == "INDIVIDUAL"  # SoleProp (D5)
    assert ownership.party_type("800A00001", mapping, default) == "LEGAL_PERSON"
    assert ownership.party_type("800A00005", mapping, default) == "EXTERNAL"  # Country (M8)
    assert ownership.party_type("800A00008", mapping, default) == "EXTERNAL"  # Direct


def test_unmapped_prefix_fails_without_a_default() -> None:
    with pytest.raises(SourceError, match="not mapped"):
        resolve_party_type("Trust", {"Corporation": "LEGAL_PERSON"}, None)
    assert resolve_party_type("Trust", {}, "INDIVIDUAL") == "INDIVIDUAL"


def test_duplicate_account_after_normalisation_fails(copy_root: Path) -> None:
    """A padded duplicate would make ownership ambiguous."""
    path = _accounts_path(copy_root)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write("US Bank #10,010,8000A0010,800A00007,Corporation #7\n")
    with pytest.raises(SourceError, match="repeats after bank-id normalisation"):
        read_accounts(path)


def test_misshapen_entity_name_fails(copy_root: Path) -> None:
    _rewrite(_accounts_path(copy_root), "Partnership #3", "Partnership 3")
    with pytest.raises(SourceError, match="entity names"):
        read_accounts(_accounts_path(copy_root))


def test_entity_with_two_names_fails(copy_root: Path) -> None:
    _rewrite(
        _accounts_path(copy_root),
        "8000A0011,800A00001,Corporation #1",
        "8000A0011,800A00001,Corporation #99",
    )
    with pytest.raises(SourceError, match="more than one entity_name"):
        read_accounts(_accounts_path(copy_root))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def test_map_distinct_reuses_the_canonical_normaliser() -> None:
    values = pa.chunked_array([["010", "10", "0"], ["000", "021174", "INTERNAL"]])
    mapped = map_distinct(values, normalise_bank_id).to_pylist()
    assert mapped == [normalise_bank_id(v) for v in values.to_pylist()]
    assert mapped == ["10", "10", "0", "0", "21174", "INTERNAL"]
