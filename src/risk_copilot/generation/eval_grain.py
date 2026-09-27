"""The evaluation grain -- GAP-08, plan step 6.

Labels are leg-grain; signals fire at party-window grain. Precision and recall
are only defined once a denominator is fixed, and this module fixes it as an
artefact: ``GROUND_TRUTH.EVAL_GRAIN_PARTY_MONTH``, one row per customer party
per calendar month in which it has a leg.

* ``is_illicit_actual``: any illicit leg of the party in the month.
* ``typology_id``: the party-month's dominant typology, i.e. the most illicit
  legs, with the smallest id breaking ties. NULL when every illicit leg is
  unattributed (M5).
* ``expected_rule_ids``: the union of ``expected_rule_ids`` over **every**
  typology present, from the catalogue in ``conf/detection.yml``. Unattributed
  illicit legs contribute none, so they count against no rule's recall. A party
  both structuring and in a FAN-OUT is expected by both rules, not only by
  whichever dominates.

Internal (cash desk) legs are not a customer's and are excluded.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow as pa

from risk_copilot.generation import GenerationError

EVAL_SCHEMA = pa.schema(
    [
        ("party_id", pa.string()),
        ("month_bucket", pa.string()),
        ("typology_id", pa.string()),
        ("is_illicit_actual", pa.bool_()),
        ("illicit_leg_count", pa.int64()),
        ("total_leg_count", pa.int64()),
        ("expected_rule_ids", pa.list_(pa.string())),
    ]
)


def expected_rules(detection: Mapping[str, Any]) -> dict[str, list[str]]:
    """``typology_id -> expected_rule_ids`` from the catalogue."""
    return {
        str(t["typology_id"]): [str(r) for r in t.get("expected_rule_ids") or []]
        for t in detection.get("typologies") or []
    }


def build_eval_grain(
    legs: Sequence[tuple[pa.Table, pa.Table]],
    party_of_account: Mapping[str, str],
    detection: Mapping[str, Any],
) -> pa.Table:
    """Aggregate labelled legs to party-month.

    Args:
        legs: ``(legs, labels)`` table pairs. Each legs table has ``pair_ref``,
            ``leg_side``, ``account_id`` and ``txn_timestamp``; each labels table
            is in ``labels.LABEL_SCHEMA``.
        party_of_account: Customer accounts only. A leg on any other account
            (the cash desk) is skipped.
    """
    rules = expected_rules(detection)
    total: Counter[tuple[str, str]] = Counter()
    illicit: Counter[tuple[str, str]] = Counter()
    typologies: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)

    for leg_table, label_table in legs:
        label = {
            (r["pair_ref"], r["leg_side"]): r
            for r in label_table.select(
                ["pair_ref", "leg_side", "is_illicit", "typology_id"]
            ).to_pylist()
        }
        for leg in leg_table.select(
            ["pair_ref", "leg_side", "account_id", "txn_timestamp"]
        ).to_pylist():
            party = party_of_account.get(leg["account_id"])
            if party is None:
                continue
            key = (party, leg["txn_timestamp"].strftime("%Y-%m"))
            row = label.get((leg["pair_ref"], leg["leg_side"]))
            if row is None:
                raise GenerationError(f"Leg {leg['pair_ref']}/{leg['leg_side']} has no label")
            total[key] += 1
            if row["is_illicit"]:
                illicit[key] += 1
                if row["typology_id"]:
                    if row["typology_id"] not in rules:
                        raise GenerationError(f"Typology {row['typology_id']!r} is not catalogued")
                    typologies[key][row["typology_id"]] += 1

    records = []
    for key in sorted(total):
        present = typologies.get(key, Counter())
        dominant = min(present, key=lambda t: (-present[t], t)) if present else None
        records.append(
            {
                "party_id": key[0],
                "month_bucket": key[1],
                "typology_id": dominant,
                "is_illicit_actual": illicit[key] > 0,
                "illicit_leg_count": illicit[key],
                "total_leg_count": total[key],
                "expected_rule_ids": sorted({r for t in present for r in rules[t]}),
            }
        )
    return pa.Table.from_pylist(records, schema=EVAL_SCHEMA)
