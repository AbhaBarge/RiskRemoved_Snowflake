"""Canonical result-set hashing for the evidence chain.

GAP-12. The architecture specified a SHA-256 ``result_hash`` on every evidence
query but never defined *what* gets hashed. Without a canonicalisation rule the
hash is worthless: two runs of the same query differ whenever column order,
row order, numeric formatting or the null representation differ, so the
Reviewer would reject sound findings and the audit trail would be noise.

This module fixes the rule. The canonical form is:

* **Column order** -- as declared by the query, preserved. Column *names* are
  included in the digest, so a query that starts returning different columns
  produces a different hash even if the values coincide.
* **Row order** -- rows are sorted by their canonical serialisation. Result-set
  order is not guaranteed without ``ORDER BY``, so relying on it would make the
  hash flaky; sorting means the hash attests to the *set* of rows.
* **Numbers** -- ``Decimal`` normalised to a fixed exponent, so ``1000``,
  ``1000.00`` and ``1E+3`` collapse to one form. The most common source of
  spurious mismatch.
* **Nulls** -- one reserved sentinel that no legitimate string can produce.
* **Timestamps** -- ISO-8601 with explicit microseconds.

The digest is over a length-prefixed encoding, so no value's content can be
confused with a field boundary.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

#: Bumped whenever the canonical form changes. Stored beside every hash so an
#: old hash is never compared against a new algorithm -- the mismatch would
#: otherwise look like tampered evidence.
CANONICAL_FORM_VERSION = "1"

#: Sentinels use C0 control characters, which cannot appear in a Snowflake
#: string value, so a crafted cell value cannot forge one.
_NULL = "\x00NULL\x00"
_FIELD_SEP = "\x1f"
_ROW_SEP = "\x1e"

#: Fixed scale for numeric normalisation. Covers INR paise (2dp) with headroom
#: for computed ratios; beyond this, values are rounded, which is intended --
#: floating-point noise in the 15th decimal place must not invalidate evidence.
_DECIMAL_SCALE = 8


class HashingError(ValueError):
    """Raised when a value cannot be canonicalised."""


def _canonical_scalar(value: Any) -> str:
    """Render one cell in canonical form."""
    if value is None:
        return _NULL

    if isinstance(value, bool):
        # Checked before int: bool is a subclass of int, and "true"/"1" must not
        # collide.
        return "true" if value else "false"

    if isinstance(value, Decimal):
        return _canonical_decimal(value)

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        # Floats are converted via str() rather than Decimal(float) so the value
        # reflects the repr a human would see, not the binary expansion.
        return _canonical_decimal(Decimal(str(value)))

    if isinstance(value, dt.datetime):
        # Naive timestamps are assumed UTC. Snowflake TIMESTAMP_NTZ carries no
        # zone, and inventing one here would make the hash depend on the
        # client's locale.
        aware = value if value.tzinfo else value.replace(tzinfo=dt.UTC)
        return aware.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")

    if isinstance(value, dt.date):
        return value.isoformat()

    if isinstance(value, bytes | bytearray):
        return value.hex()

    if isinstance(value, Mapping | list | tuple):
        # VARIANT/ARRAY/OBJECT columns. sort_keys makes object key order
        # irrelevant; separators strip incidental whitespace.
        try:
            return json.dumps(
                value, sort_keys=True, separators=(",", ":"), default=_canonical_scalar
            )
        except (TypeError, ValueError) as exc:
            raise HashingError(f"Cannot canonicalise structured value: {value!r}") from exc

    return str(value)


def _canonical_decimal(value: Decimal) -> str:
    """Normalise a decimal to a fixed scale with no exponent notation."""
    if not value.is_finite():
        # NaN and infinity have no defensible place in a financial result set;
        # surfacing them is more useful than hashing them.
        raise HashingError(f"Non-finite numeric value in result set: {value!r}")
    try:
        quantised = value.quantize(Decimal(1).scaleb(-_DECIMAL_SCALE))
    except InvalidOperation as exc:
        raise HashingError(f"Numeric value exceeds supported precision: {value!r}") from exc
    text = format(quantised, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    # Negative zero must not hash differently from zero: quantising a value like
    # Decimal("-0.000000001") yields "-0", and treating that as distinct from "0"
    # would fail a re-verification for no substantive reason.
    if text in ("", "-0", "-"):
        return "0"
    return text


def canonicalise(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
) -> str:
    """Return the canonical text form of a result set.

    Args:
        columns: Column names, in query order.
        rows: Row tuples, each the same width as ``columns``.

    Raises:
        HashingError: If ``columns`` is empty or a row's width does not match.
    """
    if not columns:
        raise HashingError("A result set must declare at least one column")

    width = len(columns)
    header = _FIELD_SEP.join(str(c).strip().upper() for c in columns)

    encoded: list[str] = []
    for index, row in enumerate(rows):
        if len(row) != width:
            raise HashingError(
                f"Row {index} has {len(row)} values but {width} columns were declared"
            )
        # Length-prefix each field so a value containing a separator-like
        # sequence cannot shift the field boundaries.
        cells = []
        for cell in row:
            text = _canonical_scalar(cell)
            cells.append(f"{len(text)}:{text}")
        encoded.append(_FIELD_SEP.join(cells))

    # Sort the *encoded* rows: hashing attests to the set of rows returned, not
    # to an ordering the query never guaranteed.
    encoded.sort()
    return _ROW_SEP.join([f"v{CANONICAL_FORM_VERSION}", header, *encoded])


def result_hash(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Return the SHA-256 hex digest of a canonicalised result set.

    This is the value stored in ``EVIDENCE_CHAIN.EVIDENCE_QUERY_LOG.result_hash``
    and recomputed by the Reviewer.
    """
    canonical = canonicalise(columns, rows)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_hash(payload: Any) -> str:
    """Return the SHA-256 hex digest of an artifact payload.

    Used for ``ARTIFACT.content_hash``, which is what makes an attestation bind
    to one exact document version: if the content changes, the attestation no
    longer matches and must be re-obtained.
    """
    if isinstance(payload, str):
        canonical = payload
    else:
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=_canonical_scalar
        )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def hashes_match(expected: str, actual: str) -> bool:
    """Compare two digests case-insensitively."""
    return bool(expected) and bool(actual) and expected.strip().lower() == actual.strip().lower()
