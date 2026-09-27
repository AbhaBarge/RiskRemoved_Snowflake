"""Leg-level ground truth -- GAP-08 / GAP-09, plan step 4.

AMLworld labels at pair grain; the pipeline stores legs, so one labelled pair
yields two labelled legs. Attribution rules:

* ``is_illicit`` is the ledger's own ``is_laundering``. It is never inferred
  from the Patterns file. 1,968 illicit rows have no attempt (M5), and they
  stay illicit with a NULL typology.
* A pair belonging to a **captured** attempt gets that attempt's
  ``attempt_id``, its AMLworld pattern, the ``typology_id`` the catalogue in
  ``conf/detection.yml`` assigns to that pattern, and
  ``match_confidence = 1 / n``, where ``n`` is how many ledger rows the Patterns
  row matched. That is 1.0 on HI-Small (M4), and the ``label_attribution`` gate
  fails the build if it ever is not.
* ``injection_method`` for every source leg is ``labels.source_injection_method``.
  Injected legs are labelled by ``inject``.

Labels are keyed by ``(pair_ref, leg_side)``; the writer swaps in the final
``txn_id``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pyarrow as pa

from risk_copilot.generation import GenerationError
from risk_copilot.generation.graph import Subgraph
from risk_copilot.generation.patterns import AttemptLocation, Patterns
from risk_copilot.generation.reskin import source_pair_ref
from risk_copilot.generation.source import SRC_ROW_NUMBER

CONFIDENCE = pa.decimal128(5, 4)

LABEL_SCHEMA = pa.schema(
    [
        ("pair_ref", pa.string()),
        ("leg_side", pa.string()),
        (SRC_ROW_NUMBER, pa.int64()),
        ("is_illicit", pa.bool_()),
        ("typology_id", pa.string()),
        ("amlworld_pattern", pa.string()),
        ("attempt_id", pa.int64()),
        ("injection_method", pa.string()),
        ("match_confidence", CONFIDENCE),
    ]
)

PATTERNS_SCHEMA = pa.schema(
    [
        ("attempt_id", pa.int64()),
        ("typology", pa.string()),
        ("max_detail", pa.string()),
        ("txn_count", pa.int64()),
        ("txn_data", pa.string()),
    ]
)


class LabelError(GenerationError):
    """Labels cannot be attributed consistently."""


def typology_ids(detection: Mapping[str, Any]) -> dict[str, str]:
    """AMLworld pattern -> ``typology_id``, from the ``typologies`` catalogue."""
    mapping: dict[str, str] = {}
    for entry in detection.get("typologies") or []:
        pattern = entry.get("amlworld_pattern")
        if pattern:
            if pattern in mapping:
                raise LabelError(f"conf/detection.yml: two typologies claim pattern {pattern!r}")
            mapping[str(pattern)] = str(entry["typology_id"])
    return mapping


def confidence(match_count: int) -> Decimal:
    """``1 / match_count`` at the column's scale, 4 dp."""
    if match_count < 1:
        raise LabelError("A located Patterns row cannot have zero matches")
    return (Decimal(1) / Decimal(match_count)).quantize(Decimal("0.0001"))


def label_source_legs(
    subgraph: Subgraph,
    patterns: Patterns,
    location: AttemptLocation,
    generation: Mapping[str, Any],
    detection: Mapping[str, Any],
) -> pa.Table:
    """One label row per sampled source leg, in :data:`LABEL_SCHEMA`."""
    method = str((generation.get("labels") or {}).get("source_injection_method") or "")
    if not method:
        raise LabelError("conf/generation.yml: labels.source_injection_method is required")
    typologies = typology_ids(detection)

    by_row: dict[int, tuple[int, str, Decimal]] = {}
    captured = set(subgraph.captured_attempts)
    for attempt in patterns.attempts:
        if attempt.attempt_id not in captured:
            continue
        if attempt.typology not in typologies:
            raise LabelError(
                f"Attempt {attempt.attempt_id}: pattern {attempt.typology!r} has no typology "
                "in conf/detection.yml"
            )
        for row in attempt.rows:
            matched = location.matches[row.line_number]
            for n in matched:
                if n in by_row and by_row[n][0] != attempt.attempt_id:
                    raise LabelError(
                        f"Ledger row {n} is claimed by attempts {by_row[n][0]} and "
                        f"{attempt.attempt_id}"
                    )
                by_row[n] = (attempt.attempt_id, attempt.typology, confidence(len(matched)))

    records: list[dict[str, Any]] = []
    for n, flag in zip(
        subgraph.pairs[SRC_ROW_NUMBER].to_pylist(),
        subgraph.pairs["is_laundering"].to_pylist(),
        strict=True,
    ):
        attempt_info = by_row.get(n)
        if attempt_info and flag != 1:
            raise LabelError(f"Ledger row {n} is in attempt {attempt_info[0]} but not illicit")
        for side in ("D", "C"):
            records.append(
                {
                    "pair_ref": source_pair_ref(n),
                    "leg_side": side,
                    SRC_ROW_NUMBER: n,
                    "is_illicit": flag == 1,
                    "typology_id": typologies[attempt_info[1]] if attempt_info else None,
                    "amlworld_pattern": attempt_info[1] if attempt_info else None,
                    "attempt_id": attempt_info[0] if attempt_info else None,
                    "injection_method": method,
                    "match_confidence": attempt_info[2] if attempt_info else None,
                }
            )
    return pa.Table.from_pylist(records, schema=LABEL_SCHEMA)


def raw_laundering_patterns(patterns: Patterns) -> pa.Table:
    """``GROUND_TRUTH.RAW_LAUNDERING_PATTERNS``: every attempt, verbatim.

    All attempts, not only captured ones: the table is the parsed source, kept
    so a label dispute can be traced to the file rather than to the parser.
    ``txn_data`` is a JSON array of the raw lines, loaded into the VARIANT column.
    """
    return pa.Table.from_pylist(
        [
            {
                "attempt_id": a.attempt_id,
                "typology": a.typology,
                "max_detail": a.max_detail,
                "txn_count": a.txn_count,
                "txn_data": json.dumps([r.raw for r in a.rows], separators=(",", ":")),
            }
            for a in patterns.attempts
        ],
        schema=PATTERNS_SCHEMA,
    )
