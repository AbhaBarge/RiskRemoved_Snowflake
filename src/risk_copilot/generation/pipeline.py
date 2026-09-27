"""End-to-end generation: source -> sample -> dress -> inject -> gates -> files.

``riskcopilot generate`` calls :func:`run_generation`. The order of the steps is
the one in ``generation/__init__.py``. Two things happen only here, because
they need every step's output:

* **Identifier allocation.** ``txn_pair_id`` / ``txn_id`` / ``source_ref`` are
  assigned once over source and injected pairs together (D16).
* **Gates.** They run over the final tables, exactly as they will be loaded.

A contract violation inside a step raises :class:`GenerationError`. A gate
failure does not raise: the files are still written, marked
``"passed": false`` in the manifest, so the failure can be inspected. The
exit code carries the verdict, and the demo-entity pin is written only on a
passing build.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

import pyarrow as pa

from risk_copilot.config import Settings
from risk_copilot.generation import GenerationError
from risk_copilot.generation.demo_entity import DemoEntityError, select_demo_entity
from risk_copilot.generation.desk import CashDesk
from risk_copilot.generation.entities import build_entities
from risk_copilot.generation.eval_grain import EVAL_SCHEMA, build_eval_grain
from risk_copilot.generation.graph import sample_subgraph
from risk_copilot.generation.inject import inject_structuring
from risk_copilot.generation.labels import label_source_legs, raw_laundering_patterns
from risk_copilot.generation.patterns import load_patterns, locate_attempts
from risk_copilot.generation.relationships import build_relationships
from risk_copilot.generation.reskin import MONEY, PairIds, assign_pair_ids, reskin_pairs
from risk_copilot.generation.source import (
    SRC_ROW_NUMBER,
    load_accounts,
    load_ledger,
    source_dataset,
)
from risk_copilot.generation.validate import Gate, GateInputs, run_gates
from risk_copilot.generation.writer import write_dataset, write_demo_entity

TXN_SCHEMA = pa.schema(
    [
        ("txn_id", pa.string()),
        ("txn_pair_id", pa.string()),
        ("leg_side", pa.string()),
        ("account_id", pa.string()),
        ("counterparty_account_id", pa.string()),
        ("txn_timestamp", pa.timestamp("s")),
        ("amount", MONEY),
        ("currency", pa.string()),
        ("amount_inr", MONEY),
        ("txn_type", pa.string()),
        ("channel", pa.string()),
        ("reference_text", pa.string()),
        ("source_ref", pa.string()),
    ]
)

LABELS_OUT_SCHEMA = pa.schema(
    [
        ("txn_id", pa.string()),
        ("txn_pair_id", pa.string()),
        (SRC_ROW_NUMBER, pa.int64()),
        ("is_illicit", pa.bool_()),
        ("typology_id", pa.string()),
        ("amlworld_pattern", pa.string()),
        ("attempt_id", pa.int64()),
        ("injection_method", pa.string()),
        ("match_confidence", pa.decimal128(5, 4)),
    ]
)


@dataclass(frozen=True)
class GenerationResult:
    out_dir: Path
    gates: tuple[Gate, ...]
    manifest: Mapping[str, Any]
    demo_entity: Mapping[str, Any] | None
    demo_entity_path: Path | None

    @property
    def passed(self) -> bool:
        return not any(g.blocks for g in self.gates)


def _with_ids(rows: list[dict[str, Any]], ids: Mapping[str, PairIds]) -> list[dict[str, Any]]:
    for row in rows:
        pair = ids[row.pop("pair_ref")]
        row.pop("order_key", None)
        row["txn_pair_id"] = pair.txn_pair_id
        row["txn_id"] = pair.leg_id(row["leg_side"])
        row["source_ref"] = pair.source_ref
    return rows


def _labels_with_ids(table: pa.Table, ids: Mapping[str, PairIds]) -> list[dict[str, Any]]:
    out = []
    for row in table.to_pylist():
        pair = ids[row.pop("pair_ref")]
        row["txn_pair_id"] = pair.txn_pair_id
        row["txn_id"] = pair.leg_id(row.pop("leg_side"))
        out.append(row)
    return out


def _label_values(generation: Mapping[str, Any], detection: Mapping[str, Any]) -> frozenset[str]:
    values = {str(generation["labels"]["source_injection_method"])}
    for t in detection.get("typologies") or []:
        values.update(str(v) for v in (t.get("typology_id"), t.get("injection_label")) if v)
    for spec in (generation.get("injection") or {}).values():
        if isinstance(spec, Mapping) and spec.get("label"):
            values.add(str(spec["label"]))
    return frozenset(values)


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def run_generation(
    settings: Settings, *, data_root: Path | None = None, out_dir: Path | None = None
) -> GenerationResult:
    """Build, gate and write the dataset for ``settings.profile``."""
    generation, detection = settings.generation, settings.detection
    root = data_root or settings.repo_root
    output = generation["output"]
    target = out_dir or settings.repo_root / str(output["root"]) / settings.profile
    seed = int(generation["seed"])

    # ---- source + sample --------------------------------------------------- #
    patterns = load_patterns(root, generation)
    ledger = load_ledger(root, generation)
    location = locate_attempts(patterns, ledger)
    subgraph = sample_subgraph(
        ledger, load_accounts(root, generation), patterns, location, generation
    )

    # ---- dress ------------------------------------------------------------- #
    legs = reskin_pairs(subgraph, generation)
    labels = label_source_legs(subgraph, patterns, location, generation, detection)
    entities = build_entities(subgraph, legs, labels, generation)
    relationships = build_relationships(entities, legs, generation)
    injection = inject_structuring(subgraph, entities, legs, generation)
    desk = CashDesk.from_config(generation)

    # ---- identifiers (once, over everything) ------------------------------- #
    pair_keys = {
        (r["pair_ref"], r["order_key"]) for r in legs.select(["pair_ref", "order_key"]).to_pylist()
    }
    pair_keys |= {
        (r["pair_ref"], r["order_key"])
        for r in injection.pending.select(["pair_ref", "order_key"]).to_pylist()
    }
    ids = assign_pair_ids(pair_keys, generation)

    txn_rows = _with_ids(
        [{k: v for k, v in r.items() if k != SRC_ROW_NUMBER} for r in legs.to_pylist()], ids
    )
    pending_rows = _with_ids(injection.pending.to_pylist(), ids)
    label_rows = _labels_with_ids(labels, ids) + _labels_with_ids(injection.labels, ids)
    txn_rows.sort(key=lambda r: r["txn_id"])
    pending_rows.sort(key=lambda r: r["txn_id"])
    label_rows.sort(key=lambda r: r["txn_id"])
    if not txn_rows:
        raise GenerationError("The sample produced no transactions")

    party_of_account = dict(
        zip(
            entities.accounts["account_id"].to_pylist(),
            entities.accounts["party_id"].to_pylist(),
            strict=True,
        )
    )
    eval_grain = build_eval_grain(
        [(legs, labels), (injection.pending, injection.labels)], party_of_account, detection
    )

    id_party = sorted(
        [
            {
                "party_id": subgraph.party_ids[e],
                "src_entity_id": e,
                "generation_seed": seed,
                "in_subgraph": True,
            }
            for e in subgraph.entities
        ]
        + [{**r, "in_subgraph": True} for r in desk.id_map_party().to_pylist()],
        key=lambda r: r["party_id"],
    )
    id_account = sorted(
        [
            {
                "account_id": subgraph.account_ids[k],
                "src_bank_id": k[0],
                "src_account_number": k[1],
                "party_id": subgraph.party_ids[e],
                "generation_seed": seed,
                "in_subgraph": True,
            }
            for e, keys in subgraph.accounts_by_entity.items()
            for k in keys
        ]
        + [{**r, "in_subgraph": True} for r in desk.id_map_account().to_pylist()],
        key=lambda r: r["account_id"],
    )

    raw = {
        "ID_MAP_PARTY": pa.Table.from_pylist(id_party),
        "ID_MAP_ACCOUNT": pa.Table.from_pylist(id_account),
        "RAW_TRANSACTIONS": pa.Table.from_pylist(txn_rows, schema=TXN_SCHEMA),
        "RAW_PARTY_CBS": entities.cbs,
        "RAW_PARTY_KYC": relationships.kyc,
        "RAW_ACCOUNTS": relationships.accounts,
        "RAW_PARTY_RELATIONSHIP_ATTR": relationships.edges,
    }
    pending_schema = pa.schema(
        [("txn_id", pa.string()), ("txn_pair_id", pa.string())]
        + [f for f in injection.pending.schema if f.name not in ("pair_ref", "order_key")]
        + [("source_ref", pa.string())]
    )
    ground_truth = {
        "TXN_LABELS": pa.Table.from_pylist(label_rows, schema=LABELS_OUT_SCHEMA),
        "EVAL_GRAIN_PARTY_MONTH": eval_grain.cast(EVAL_SCHEMA),
        "INJECTED_TXN_PENDING": pa.Table.from_pylist(pending_rows, schema=pending_schema),
        "RAW_LAUNDERING_PATTERNS": raw_laundering_patterns(patterns),
    }

    # ---- hero + gates ------------------------------------------------------ #
    pin: dict[str, Any] | None = None
    pin_error: str | None = None
    try:
        pin = select_demo_entity(eval_grain, entities.cbs, generation, profile=settings.profile)
    except DemoEntityError as exc:
        pin_error = str(exc)

    gates = tuple(
        run_gates(
            GateInputs(
                raw=raw,
                ground_truth=ground_truth,
                generation=generation,
                detection=detection,
                legit_span_days=entities.window.span_days,
                unresolved_source_pairs=int(subgraph.stats["unresolved_pairs"]),
                attribution_counts=location.match_counts(),
                attribution_legit_matches=len(location.legit_matches),
                hosts=injection.hosts,
                legit_inflow_inr=entities.legit_inflow_inr,
                label_values=_label_values(generation, detection),
                demo_entity_error=pin_error,
            )
        )
    )
    passed = not any(g.blocks for g in gates)

    # ---- manifest + files -------------------------------------------------- #
    structuring = generation["injection"]["structuring"]
    datasets = {
        role: {"key": ds.key, "sha256": ds.spec.get("sha256"), "bytes": ds.spec.get("bytes")}
        for role in ("ledger", "accounts", "patterns")
        for ds in (source_dataset(root, generation, role),)
    }
    manifest: dict[str, Any] = {
        "passed": passed,
        "profile": settings.profile,
        "generation_seed": seed,
        "source": datasets,
        "versions": {p: _version(p) for p in ("risk-copilot", "pyarrow", "Faker")},
        "legit_window": {
            "first_day": entities.window.first_day.isoformat(),
            "last_day": entities.window.last_day.isoformat(),
            "span_days": entities.window.span_days,
        },
        "referenced_clause_ids": sorted(
            {
                structuring[k]
                for k in (
                    "ctr_clause_id",
                    "band_clause_id",
                    "window_clause_id",
                    "min_accounts_clause_id",
                )
            }
        ),
        "sampling": dict(subgraph.stats),
        "dropped_attempts": {
            reason: sum(1 for v in subgraph.dropped_attempts.values() if v == reason)
            for reason in sorted(set(subgraph.dropped_attempts.values()))
        },
        "entities": dict(entities.stats),
        "relationships": dict(relationships.stats),
        "injection": dict(injection.stats),
        "cash_desk": {
            "accounts": len(desk.accounts),
            "routed_payment_formats": sorted(desk.routed_formats),
            "routed_source_pairs": sum(
                1 for f in subgraph.pairs["payment_format"].to_pylist() if f in desk.routed_formats
            ),
        },
        "gates": [
            {
                "name": g.name,
                "status": g.status.value,
                "fail_build": g.fail_build,
                "value": g.value,
                "detail": g.detail,
            }
            for g in gates
        ],
        "demo_entity": pin["party_id"] if pin else None,
    }
    written = write_dataset(target, raw, ground_truth, manifest, output)

    pin_path: Path | None = None
    if passed and pin is not None:
        pin_path = settings.repo_root / str(output["demo_entity_file"]).format(
            profile=settings.profile
        )
        write_demo_entity(pin_path, pin)
    return GenerationResult(target, gates, written, pin, pin_path)
