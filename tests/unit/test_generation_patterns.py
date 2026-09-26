"""Patterns-file parser (Phase 2, step 2).

The parser's failure modes are the dangerous ones: a parser that requires a ``:``
detail suffix found 234 of 370 attempts and dropped three typologies without an
error (M3). So the grammar is pinned both ways -- every accepted shape and every
rejected one -- and the totals are asserted against the manifest.
"""

from __future__ import annotations

import datetime as dt
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from risk_copilot.config import load_settings
from risk_copilot.generation.patterns import (
    PatternsError,
    load_patterns,
    parse_patterns,
    read_patterns,
)
from risk_copilot.generation.source import LedgerFormat, SourceError, read_ledger

pytestmark = pytest.mark.unit

ROW = "2022/09/01 00:06,010,8000A0010,020,8000A0030,2848.96,US Dollar,2848.96,US Dollar,ACH,1"


@pytest.fixture
def generation(repo_root: Path) -> dict[str, Any]:
    return load_settings(repo_root=repo_root, profile="dev").generation


@pytest.fixture
def ts_format(generation: dict[str, Any]) -> str:
    return LedgerFormat.from_config(generation).timestamp_format


def _patterns_path(root: Path) -> Path:
    return root / "data" / "Kaggle" / "HI-Small_Patterns.txt"


def parse(text: str, ts_format: str) -> Any:
    return parse_patterns(text.splitlines(), timestamp_format=ts_format)


# --------------------------------------------------------------------------- #
# The fixture file
# --------------------------------------------------------------------------- #


def test_fixture_parses_with_manifest_totals(
    amlworld_mini: Path, generation: dict[str, Any]
) -> None:
    patterns = load_patterns(amlworld_mini, generation)
    assert len(patterns.attempts) == 3
    assert patterns.row_count == 7
    assert patterns.typology_counts() == {"CYCLE": 1, "FAN-OUT": 1, "STACK": 1}


def test_attempt_ids_are_file_positions(amlworld_mini: Path, ts_format: str) -> None:
    patterns = read_patterns(_patterns_path(amlworld_mini), timestamp_format=ts_format)
    assert [(a.attempt_id, a.typology) for a in patterns.attempts] == [
        (1, "FAN-OUT"),
        (2, "STACK"),
        (3, "CYCLE"),
    ]
    assert [(a.begin_line, a.end_line) for a in patterns.attempts] == [(1, 5), (7, 10), (12, 15)]


def test_detail_suffix_is_optional(amlworld_mini: Path, ts_format: str) -> None:
    attempts = read_patterns(_patterns_path(amlworld_mini), timestamp_format=ts_format).attempts
    assert attempts[0].max_detail == "Max 3-degree Fan-Out"
    assert attempts[1].max_detail is None  # STACK: no ":" (M3)
    assert attempts[2].max_detail == "Max 2 hops"


def test_rows_are_normalised_like_the_ledger(amlworld_mini: Path, ts_format: str) -> None:
    row = (
        read_patterns(_patterns_path(amlworld_mini), timestamp_format=ts_format).attempts[0].rows[0]
    )
    assert (row.from_bank, row.to_bank) == ("10", "20")
    assert row.amount_paid == Decimal("2848.96")
    assert row.txn_timestamp == dt.datetime(2022, 9, 1, 0, 6)
    assert row.line_number == 2
    assert row.raw.startswith("2022/09/01 00:06,010,")


def test_bitcoin_row_is_kept_for_the_exclusion_step(amlworld_mini: Path, ts_format: str) -> None:
    """Parsing does not exclude; graph drops the whole attempt (D9) and must see it."""
    stack = read_patterns(_patterns_path(amlworld_mini), timestamp_format=ts_format).attempts[1]
    assert "Bitcoin" in stack.payment_formats
    assert "Bitcoin" in stack.currencies
    assert stack.rows[1].amount_paid == Decimal("0.025852")


def test_cross_currency_row_keeps_both_amounts(amlworld_mini: Path, ts_format: str) -> None:
    row = (
        read_patterns(_patterns_path(amlworld_mini), timestamp_format=ts_format).attempts[2].rows[0]
    )
    assert (row.amount_paid, row.payment_currency) == (Decimal("1000.00"), "US Dollar")
    assert (row.amount_received, row.receiving_currency) == (Decimal("82500.00"), "Rupee")


def test_seed_accounts_are_distinct_normalised_and_sorted(
    amlworld_mini: Path, ts_format: str
) -> None:
    seeds = read_patterns(_patterns_path(amlworld_mini), timestamp_format=ts_format).seed_accounts()
    assert seeds == tuple(sorted(set(seeds)))
    assert ("10", "8000A0010") in seeds  # padded "010" in the file
    assert len(seeds) == 9
    assert all(not bank.startswith("0") for bank, _ in seeds)


def test_every_pattern_row_matches_exactly_one_ledger_row(
    amlworld_mini: Path, ts_format: str, generation: dict[str, Any]
) -> None:
    """M4 on the fixture: the match key built here equals the ledger's for the same row."""
    fmt = LedgerFormat.from_config(generation)
    ledger = read_ledger(amlworld_mini / "data" / "Kaggle" / "HI-Small_Trans.csv", fmt)
    keys: dict[tuple[Any, ...], int] = {}
    for r in ledger.to_pylist():
        key = (
            r["txn_timestamp"],
            r["from_bank"],
            r["from_account"],
            r["to_bank"],
            r["to_account"],
            r["amount_received"],
            r["receiving_currency"],
            r["amount_paid"],
            r["payment_currency"],
            r["payment_format"],
        )
        keys[key] = keys.get(key, 0) + 1
    patterns = read_patterns(_patterns_path(amlworld_mini), timestamp_format=ts_format)
    for attempt in patterns.attempts:
        for row in attempt.rows:
            assert keys.get(row.match_key()) == 1, row.raw


def test_crlf_file_parses_identically(amlworld_mini: Path, tmp_path: Path, ts_format: str) -> None:
    source = _patterns_path(amlworld_mini)
    crlf = tmp_path / source.name
    crlf.write_bytes(source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    lf = read_patterns(source, timestamp_format=ts_format)
    assert read_patterns(crlf, timestamp_format=ts_format).attempts == lf.attempts


# --------------------------------------------------------------------------- #
# Totals against the manifest
# --------------------------------------------------------------------------- #


def test_totals_mismatch_fails(
    amlworld_mini: Path, tmp_path: Path, generation: dict[str, Any]
) -> None:
    root = tmp_path / "root"
    shutil.copytree(amlworld_mini, root)
    manifest = root / "data" / "MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "laundering_attempts: 3", "laundering_attempts: 4"
        ),
        encoding="utf-8",
    )
    with pytest.raises(PatternsError, match="manifest declares 4 / 7"):
        load_patterns(root, generation)


def test_totals_must_be_declared(
    amlworld_mini: Path, tmp_path: Path, generation: dict[str, Any]
) -> None:
    root = tmp_path / "root"
    shutil.copytree(amlworld_mini, root)
    manifest = root / "data" / "MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("    illicit_transactions: 7\n", ""),
        encoding="utf-8",
    )
    with pytest.raises(SourceError, match="must declare 'illicit_transactions'"):
        load_patterns(root, generation)


# --------------------------------------------------------------------------- #
# Grammar
# --------------------------------------------------------------------------- #


def test_all_measured_header_shapes_parse(ts_format: str) -> None:
    headers = {
        "BIPARTITE": "BEGIN LAUNDERING ATTEMPT - BIPARTITE",
        "CYCLE": "BEGIN LAUNDERING ATTEMPT - CYCLE:  Max 10 hops",
        "FAN-IN": "BEGIN LAUNDERING ATTEMPT - FAN-IN:  Max 7-degree Fan-In",
        "FAN-OUT": "BEGIN LAUNDERING ATTEMPT - FAN-OUT:  Max 16-degree Fan-Out",
        "GATHER-SCATTER": "BEGIN LAUNDERING ATTEMPT - GATHER-SCATTER:  Max 3-degree Fan-In",
        "RANDOM": "BEGIN LAUNDERING ATTEMPT - RANDOM:  Max 5 hops",
        "SCATTER-GATHER": "BEGIN LAUNDERING ATTEMPT - SCATTER-GATHER",
        "STACK": "BEGIN LAUNDERING ATTEMPT - STACK",
    }
    text = "\n".join(
        f"{begin}\n{ROW}\nEND LAUNDERING ATTEMPT - {typ}\n" for typ, begin in headers.items()
    )
    patterns = parse(text, ts_format)
    assert [a.typology for a in patterns.attempts] == list(headers)
    no_detail = {a.typology for a in patterns.attempts if a.max_detail is None}
    assert no_detail == {"BIPARTITE", "SCATTER-GATHER", "STACK"}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (f"BEGIN LAUNDERING ATTEMPT - CYCLE\n{ROW}\n", "never closed"),
        (
            f"BEGIN LAUNDERING ATTEMPT - CYCLE\n{ROW}\nEND LAUNDERING ATTEMPT - STACK\n",
            "closes the CYCLE",
        ),
        ("END LAUNDERING ATTEMPT - CYCLE\n", "no open attempt"),
        (
            f"BEGIN LAUNDERING ATTEMPT - CYCLE\n{ROW}\nBEGIN LAUNDERING ATTEMPT - STACK\n",
            "BEGIN inside",
        ),
        ("BEGIN LAUNDERING ATTEMPT - CYCLE\nEND LAUNDERING ATTEMPT - CYCLE\n", "no transactions"),
        (f"{ROW}\n", "outside any attempt"),
        (
            f"BEGIN LAUNDERING ATTEMPT - CYCLE\n{ROW}\n\n{ROW}\nEND LAUNDERING ATTEMPT - CYCLE\n",
            "blank line inside",
        ),
        (f"BEGIN LAUNDERING ATTEMPT - Cycle\n{ROW}\n", "unrecognised delimiter"),
        (
            f"BEGIN LAUNDERING ATTEMPT - CYCLE\n{ROW},extra\nEND LAUNDERING ATTEMPT - CYCLE\n",
            "12 fields",
        ),
        (
            f"BEGIN LAUNDERING ATTEMPT - CYCLE\n{ROW[:-1]}0\nEND LAUNDERING ATTEMPT - CYCLE\n",
            "flagged illicit",
        ),
        (
            f"BEGIN LAUNDERING ATTEMPT - CYCLE\n{ROW.replace('2848.96,US', 'NaN,US', 1)}\n"
            "END LAUNDERING ATTEMPT - CYCLE\n",
            "not a plain decimal",
        ),
        (
            f"BEGIN LAUNDERING ATTEMPT - CYCLE\n{ROW.replace('2022/09/01', '2022-09-01')}\n"
            "END LAUNDERING ATTEMPT - CYCLE\n",
            "timestamp",
        ),
    ],
)
def test_grammar_violations_fail(text: str, message: str, ts_format: str) -> None:
    with pytest.raises(PatternsError, match=message):
        parse(text, ts_format)


def test_error_names_the_line(ts_format: str) -> None:
    text = f"BEGIN LAUNDERING ATTEMPT - CYCLE\n{ROW}\nEND LAUNDERING ATTEMPT - STACK\n"
    with pytest.raises(PatternsError, match=r"<patterns>:3:"):
        parse(text, ts_format)


def test_blank_lines_between_blocks_are_allowed(ts_format: str) -> None:
    block = f"BEGIN LAUNDERING ATTEMPT - STACK\n{ROW}\nEND LAUNDERING ATTEMPT - STACK\n"
    patterns = parse(f"\n{block}\n\n{block}\n", ts_format)
    assert [a.attempt_id for a in patterns.attempts] == [1, 2]
