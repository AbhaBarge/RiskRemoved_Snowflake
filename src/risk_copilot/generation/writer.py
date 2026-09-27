"""Write a generated dataset deterministically -- plan step 7.

Layout, under ``output.root/<profile>/``::

    raw/<TABLE>.parquet            loaded into RAW.*
    ground_truth/<TABLE>.parquet   loaded into GROUND_TRUTH.* (admin-only stage)
    manifest.json

Byte-identical for the same seed and source: rows are pre-sorted by primary key,
Parquet is written with fixed compression and row-group size, and nothing
records wall-clock time. The manifest carries a canonical content hash per table
(:func:`risk_copilot.hashing.result_hash`, row-order-insensitive), so two builds
can be compared without diffing Parquet bytes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from risk_copilot.hashing import result_hash


def table_hash(table: pa.Table) -> str:
    return result_hash(table.column_names, (tuple(r.values()) for r in table.to_pylist()))


def write_parquet(table: pa.Table, path: Path, output: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        path,
        compression=str(output["parquet_compression"]),
        row_group_size=int(output["parquet_row_group_size"]),
    )


def write_dataset(
    out_dir: Path,
    raw: Mapping[str, pa.Table],
    ground_truth: Mapping[str, pa.Table],
    manifest: dict[str, Any],
    output: Mapping[str, Any],
) -> dict[str, Any]:
    """Write every table plus ``manifest.json``; return the manifest as written."""
    tables: dict[str, Any] = {}
    for zone, group in (("raw", raw), ("ground_truth", ground_truth)):
        for name in sorted(group):
            table = group[name]
            write_parquet(table, out_dir / zone / f"{name}.parquet", output)
            tables[f"{zone}/{name}"] = {"rows": table.num_rows, "content_hash": table_hash(table)}
    manifest = {**manifest, "tables": tables}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return manifest


def write_demo_entity(path: Path, pin: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Written by `riskcopilot generate`. PROVISIONAL until the post-load\n"
        "# detection check confirms every listed rule fires for this party.\n"
    )
    path.write_text(header + yaml.safe_dump(dict(pin), sort_keys=True), encoding="utf-8")
