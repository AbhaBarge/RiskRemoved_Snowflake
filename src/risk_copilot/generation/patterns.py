"""Parse ``HI-Small_Patterns.txt`` into laundering attempts.

The file is not CSV. It is a sequence of blocks::

    BEGIN LAUNDERING ATTEMPT - FAN-OUT:  Max 16-degree Fan-Out
    2022/09/01 00:06,021174,800737690,012,80011F990,2848.96,Euro,...,ACH,1
    ...
    END LAUNDERING ATTEMPT - FAN-OUT

Measured facts this parser encodes (build log M3, M4):

* The ``:`` detail suffix is **optional**. BIPARTITE, SCATTER-GATHER and STACK
  headers carry none; a parser that required one found 234 of 370 attempts and
  silently dropped the rest.
* Rows use the ledger's eleven positional fields, so they are keyed exactly as
  the ledger reader keys its rows -- bank ids normalised, amounts as exact
  :class:`~decimal.Decimal` -- and :meth:`PatternRow.match_key` compares equal
  to the same ledger row.

The parser is strict on purpose. A BEGIN whose END names another typology, an
unclosed block, a row outside any block or a row that is not flagged illicit
all fail: each means the file is not what the labels are assumed to describe.
Totals are asserted against ``data/MANIFEST.yml``, never against literals.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from risk_copilot.generation.source import (
    LEDGER_COLUMNS,
    LedgerFormat,
    SourceError,
    source_dataset,
)
from risk_copilot.ids import normalise_bank_id

_TYPOLOGY = r"(?P<typology>[A-Z]+(?:-[A-Z]+)*)"
_BEGIN_RE = re.compile(rf"^BEGIN LAUNDERING ATTEMPT - {_TYPOLOGY}(?::\s*(?P<detail>\S.*?))?\s*$")
_END_RE = re.compile(rf"^END LAUNDERING ATTEMPT - {_TYPOLOGY}\s*$")
#: Unsigned plain decimal. Rejects ``NaN``, ``Infinity`` and exponent forms that
#: :class:`Decimal` would otherwise accept.
_AMOUNT_RE = re.compile(r"^\d+(?:\.\d+)?$")

#: The ten fields that identify a transfer. ``is_laundering`` is excluded: it is
#: the label being attributed, not part of the identity.
MatchKey = tuple[dt.datetime, str, str, str, str, Decimal, str, Decimal, str, str]


class PatternsError(SourceError):
    """The Patterns file breaks the block grammar or disagrees with the manifest."""


@dataclass(frozen=True)
class PatternRow:
    """One transaction line inside an attempt."""

    line_number: int
    raw: str
    txn_timestamp: dt.datetime
    from_bank: str
    from_account: str
    to_bank: str
    to_account: str
    amount_received: Decimal
    receiving_currency: str
    amount_paid: Decimal
    payment_currency: str
    payment_format: str
    is_laundering: int

    @property
    def from_key(self) -> tuple[str, str]:
        return (self.from_bank, self.from_account)

    @property
    def to_key(self) -> tuple[str, str]:
        return (self.to_bank, self.to_account)

    def match_key(self) -> MatchKey:
        """The full ten-field identity, comparable with a ledger row's.

        Measured (M4): every one of the 3,209 rows matches exactly one ledger
        row on this key, so attribution is 1:1. ``labels`` asserts it.
        """
        return (
            self.txn_timestamp,
            self.from_bank,
            self.from_account,
            self.to_bank,
            self.to_account,
            self.amount_received,
            self.receiving_currency,
            self.amount_paid,
            self.payment_currency,
            self.payment_format,
        )


@dataclass(frozen=True)
class Attempt:
    """One BEGIN/END block.

    ``attempt_id`` is the block's 1-based position in the file, which is stable
    for a given file revision and is what ``GROUND_TRUTH.TXN_LABELS.attempt_id``
    and ``RAW_LAUNDERING_PATTERNS.attempt_id`` carry.
    """

    attempt_id: int
    typology: str
    max_detail: str | None
    begin_line: int
    end_line: int
    rows: tuple[PatternRow, ...]

    @property
    def txn_count(self) -> int:
        return len(self.rows)

    @property
    def accounts(self) -> frozenset[tuple[str, str]]:
        """Every ``(bank_id, account_number)`` on either side of a row."""
        return frozenset(key for row in self.rows for key in (row.from_key, row.to_key))

    @property
    def payment_formats(self) -> frozenset[str]:
        return frozenset(row.payment_format for row in self.rows)

    @property
    def currencies(self) -> frozenset[str]:
        return frozenset(
            cur for row in self.rows for cur in (row.payment_currency, row.receiving_currency)
        )


@dataclass(frozen=True)
class Patterns:
    """All attempts in file order."""

    attempts: tuple[Attempt, ...]
    source: str

    @property
    def row_count(self) -> int:
        return sum(a.txn_count for a in self.attempts)

    def typology_counts(self) -> dict[str, int]:
        """Attempts per typology, sorted by typology."""
        return dict(sorted(Counter(a.typology for a in self.attempts).items()))

    def seed_accounts(self) -> tuple[tuple[str, str], ...]:
        """Distinct accounts named anywhere in the file, bank ids normalised, sorted.

        The ``laundering_patterns`` subgraph seed set.
        """
        return tuple(sorted(set().union(*(a.accounts for a in self.attempts))))

    def assert_totals(self, *, attempts: int, rows: int) -> None:
        """Fail unless the parse found exactly the declared totals."""
        if len(self.attempts) != attempts or self.row_count != rows:
            raise PatternsError(
                f"{self.source}: parsed {len(self.attempts)} attempts / {self.row_count} "
                f"rows; the manifest declares {attempts} / {rows}. A parser that "
                "under-counts is silently dropping typologies."
            )


def _parse_amount(text: str, *, line_number: int, source: str) -> Decimal:
    if not _AMOUNT_RE.match(text):
        raise PatternsError(f"{source}:{line_number}: amount {text!r} is not a plain decimal")
    try:
        return Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex
        raise PatternsError(f"{source}:{line_number}: amount {text!r}") from exc


def _parse_row(line: str, *, line_number: int, timestamp_format: str, source: str) -> PatternRow:
    fields = line.split(",")
    if len(fields) != len(LEDGER_COLUMNS):
        raise PatternsError(
            f"{source}:{line_number}: {len(fields)} fields, expected {len(LEDGER_COLUMNS)}"
        )
    values = dict(zip(LEDGER_COLUMNS, (f.strip() for f in fields), strict=True))
    try:
        timestamp = dt.datetime.strptime(values["txn_timestamp"], timestamp_format)
    except ValueError as exc:
        raise PatternsError(f"{source}:{line_number}: timestamp: {exc}") from exc
    if values["is_laundering"] != "1":
        raise PatternsError(
            f"{source}:{line_number}: is_laundering is {values['is_laundering']!r}; every "
            "row of a laundering attempt must be flagged illicit"
        )
    return PatternRow(
        line_number=line_number,
        raw=line,
        txn_timestamp=timestamp,
        from_bank=normalise_bank_id(values["from_bank"]),
        from_account=values["from_account"],
        to_bank=normalise_bank_id(values["to_bank"]),
        to_account=values["to_account"],
        amount_received=_parse_amount(
            values["amount_received"], line_number=line_number, source=source
        ),
        receiving_currency=values["receiving_currency"],
        amount_paid=_parse_amount(values["amount_paid"], line_number=line_number, source=source),
        payment_currency=values["payment_currency"],
        payment_format=values["payment_format"],
        is_laundering=1,
    )


def parse_patterns(
    lines: Iterable[str], *, timestamp_format: str, source: str = "<patterns>"
) -> Patterns:
    """Parse Patterns-file lines into :class:`Patterns`.

    Args:
        lines: File lines; trailing ``\\r`` / ``\\n`` are ignored, so LF and CRLF
            files parse identically.
        timestamp_format: ``source.ledger.timestamp_format``.
        source: Name used in error messages.

    Raises:
        PatternsError: On any grammar violation. Blank lines are allowed only
            between blocks.
    """
    attempts: list[Attempt] = []
    open_block: tuple[str, str | None, int] | None = None
    rows: list[PatternRow] = []

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\r\n")
        if begin := _BEGIN_RE.match(line):
            if open_block is not None:
                raise PatternsError(
                    f"{source}:{line_number}: BEGIN inside the {open_block[0]} attempt "
                    f"opened at line {open_block[2]}"
                )
            open_block = (begin["typology"], begin["detail"], line_number)
            rows = []
        elif end := _END_RE.match(line):
            if open_block is None:
                raise PatternsError(f"{source}:{line_number}: END with no open attempt")
            typology, detail, begin_line = open_block
            if end["typology"] != typology:
                raise PatternsError(
                    f"{source}:{line_number}: END {end['typology']} closes the {typology} "
                    f"attempt opened at line {begin_line}"
                )
            if not rows:
                raise PatternsError(
                    f"{source}:{begin_line}: {typology} attempt contains no transactions"
                )
            attempts.append(
                Attempt(
                    attempt_id=len(attempts) + 1,
                    typology=typology,
                    max_detail=detail,
                    begin_line=begin_line,
                    end_line=line_number,
                    rows=tuple(rows),
                )
            )
            open_block = None
        elif not line.strip():
            if open_block is not None:
                raise PatternsError(
                    f"{source}:{line_number}: blank line inside the {open_block[0]} attempt "
                    f"opened at line {open_block[2]}"
                )
        elif line.startswith(("BEGIN", "END")):
            raise PatternsError(f"{source}:{line_number}: unrecognised delimiter {line!r}")
        elif open_block is None:
            raise PatternsError(f"{source}:{line_number}: transaction outside any attempt")
        else:
            rows.append(
                _parse_row(
                    line, line_number=line_number, timestamp_format=timestamp_format, source=source
                )
            )

    if open_block is not None:
        raise PatternsError(
            f"{source}: the {open_block[0]} attempt opened at line {open_block[2]} is never closed"
        )
    return Patterns(attempts=tuple(attempts), source=source)


def read_patterns(path: Path, *, timestamp_format: str) -> Patterns:
    """Parse a Patterns file from disk."""
    text = path.read_text(encoding="utf-8-sig")
    return parse_patterns(text.splitlines(), timestamp_format=timestamp_format, source=path.name)


def load_patterns(data_root: Path, generation: Mapping[str, Any]) -> Patterns:
    """Parse the configured Patterns file and assert its totals against the manifest."""
    ds = source_dataset(data_root, generation, "patterns")
    patterns = read_patterns(
        ds.path, timestamp_format=LedgerFormat.from_config(generation).timestamp_format
    )
    patterns.assert_totals(
        attempts=ds.require("laundering_attempts"), rows=ds.require("illicit_transactions")
    )
    return patterns
