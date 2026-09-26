"""Tests for canonical result hashing.

The property that matters: two runs of the same query over the same data must
produce the same hash despite incidental differences in row order, numeric
formatting or column-name case. Without that, the Reviewer rejects sound
findings and the evidence chain becomes noise -- which is exactly what the
original design left undefined.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from risk_copilot.hashing import (
    CANONICAL_FORM_VERSION,
    HashingError,
    canonicalise,
    content_hash,
    hashes_match,
    result_hash,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Stability
# --------------------------------------------------------------------------- #


def test_hash_is_stable_across_calls() -> None:
    columns = ["PARTY_ID", "AMOUNT"]
    rows = [("P-0000001", Decimal("950000.00"))]
    assert result_hash(columns, rows) == result_hash(columns, rows)


def test_row_order_does_not_change_the_hash() -> None:
    # Result-set order is not guaranteed without ORDER BY, so a hash sensitive to
    # it would be flaky. The hash attests to the SET of rows.
    columns = ["PARTY_ID", "AMOUNT"]
    forward = [("P-1", Decimal("1")), ("P-2", Decimal("2"))]
    reverse = list(reversed(forward))
    assert result_hash(columns, forward) == result_hash(columns, reverse)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (Decimal("1000"), Decimal("1000.00")),
        (Decimal("1000"), Decimal("1E+3")),
        (Decimal("0.50"), Decimal("0.5")),
        (Decimal("-0"), Decimal("0")),
    ],
)
def test_equivalent_decimals_hash_identically(left: Decimal, right: Decimal) -> None:
    # The single most common source of spurious mismatch.
    assert result_hash(["A"], [(left,)]) == result_hash(["A"], [(right,)])


def test_column_name_case_does_not_change_the_hash() -> None:
    assert result_hash(["party_id"], [("P-1",)]) == result_hash(["PARTY_ID"], [("P-1",)])


def test_naive_timestamps_are_treated_as_utc() -> None:
    # Snowflake TIMESTAMP_NTZ carries no zone; inventing one would make the hash
    # depend on the client's locale.
    naive = dt.datetime(2022, 9, 14, 10, 30, 0)
    aware = dt.datetime(2022, 9, 14, 10, 30, 0, tzinfo=dt.UTC)
    assert result_hash(["TS"], [(naive,)]) == result_hash(["TS"], [(aware,)])


def test_variant_object_key_order_does_not_change_the_hash() -> None:
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    assert result_hash(["V"], [(a,)]) == result_hash(["V"], [(b,)])


# --------------------------------------------------------------------------- #
# Sensitivity -- the hash must still detect real differences
# --------------------------------------------------------------------------- #


def test_different_values_hash_differently() -> None:
    assert result_hash(["A"], [(Decimal("1"),)]) != result_hash(["A"], [(Decimal("2"),)])


def test_changing_the_column_set_changes_the_hash() -> None:
    # A query that starts returning different columns must not pass review just
    # because the values coincide.
    assert result_hash(["A"], [("x",)]) != result_hash(["B"], [("x",)])


def test_null_is_distinguishable_from_the_string_null() -> None:
    assert result_hash(["A"], [(None,)]) != result_hash(["A"], [("NULL",)])


def test_boolean_is_distinguishable_from_integer() -> None:
    # bool subclasses int, so an unguarded implementation collapses True and 1.
    assert result_hash(["A"], [(True,)]) != result_hash(["A"], [(1,)])


def test_field_boundaries_cannot_be_forged_by_cell_content() -> None:
    # Length-prefixing is what prevents ("ab", "c") colliding with ("a", "bc").
    assert result_hash(["A", "B"], [("ab", "c")]) != result_hash(["A", "B"], [("a", "bc")])


def test_row_count_affects_the_hash() -> None:
    single = [("P-1",)]
    duplicated = [("P-1",), ("P-1",)]
    assert result_hash(["A"], single) != result_hash(["A"], duplicated)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_empty_column_list_is_rejected() -> None:
    with pytest.raises(HashingError, match="at least one column"):
        canonicalise([], [])


def test_row_width_mismatch_is_reported_with_its_index() -> None:
    with pytest.raises(HashingError, match="Row 1"):
        canonicalise(["A", "B"], [("x", "y"), ("z",)])


def test_non_finite_numbers_are_rejected() -> None:
    # NaN and infinity have no defensible place in a financial result set.
    with pytest.raises(HashingError, match="Non-finite"):
        result_hash(["A"], [(Decimal("NaN"),)])


# --------------------------------------------------------------------------- #
# Versioning and helpers
# --------------------------------------------------------------------------- #


def test_canonical_form_is_version_tagged() -> None:
    # Stored beside every hash, so an old hash is never compared against a new
    # algorithm -- that mismatch would look like tampered evidence.
    assert canonicalise(["A"], [("x",)]).startswith(f"v{CANONICAL_FORM_VERSION}")


def test_content_hash_is_insensitive_to_key_order() -> None:
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


def test_content_hash_detects_a_changed_payload() -> None:
    # This is what binds an attestation to one exact artifact version.
    assert content_hash({"amount": 950000}) != content_hash({"amount": 950001})


@pytest.mark.parametrize(
    ("expected", "actual", "result"),
    [
        ("ABC123", "abc123", True),
        ("abc123", " abc123 ", True),
        ("abc123", "def456", False),
        ("", "abc123", False),
        ("abc123", "", False),
    ],
)
def test_hashes_match_is_case_and_whitespace_tolerant(
    expected: str, actual: str, result: bool
) -> None:
    assert hashes_match(expected, actual) is result
