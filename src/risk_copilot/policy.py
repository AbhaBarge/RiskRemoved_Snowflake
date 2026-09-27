"""The ``policy`` seed -- ``conf/detection.yml`` as rows for the GOVERNANCE tables.

``conf/detection.yml`` is a seed, not a runtime source (see :mod:`risk_copilot.config`).
This module turns it into the rows ``sql/05_policy`` writes, and refuses a seed
that would put an ungoverned row into a governed table:

* a clause without a ``provenance.source_document``;
* a rule with no clauses, an unknown clause or typology, or no severity;
* a duplicate key.

Every value leaves here as a typed SQL literal, so the template interpolates
nothing but these strings. Quoting lives in one tested function
(:func:`sql_literal`) rather than being repeated by each template.

Seed semantics, enforced by the template:

* ``POLICY_CLAUSE`` and ``RULE_DEFINITION`` are versioned and **insert-only**. A
  row that already exists must match the seed exactly, or the deploy fails:
  a threshold changes through a new clause version, never by editing a live row.
* ``RULE_DEFINITION.rule_sql_hash`` is the SHA-256 of the rule's detection SQL
  (``sql/rules/<rule_id>.sql``). It is bound once, while NULL; after that a
  changed rule file fails the deploy until the rule gets a new version. A
  registered rule with no SQL file is refused, so the registry cannot claim a
  rule nobody wrote.
* ``TYPOLOGY`` and ``REF_CHANNEL`` are unversioned reference data and are
  **upserted**.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from risk_copilot.config import ConfigError

#: Where each rule's detection SELECT lives, relative to ``sql/``.
RULE_SQL_DIR = "rules"

#: Column -> Snowflake type, in table order. The types are the DDL's
#: (``sql/01_ddl/02_governance.sql``); every literal is cast, so a column whose
#: seed values are all NULL still has its real type in the UNION ALL.
CLAUSE_COLUMNS: dict[str, str] = {
    "clause_id": "VARCHAR",
    "clause_version": "NUMBER(10,0)",
    "source_document": "VARCHAR",
    "source_page": "NUMBER(10,0)",
    "section_heading": "VARCHAR",
    "clause_text": "VARCHAR",
    "metric": "VARCHAR",
    "operator": "VARCHAR",
    "threshold_lower": "NUMBER(20,4)",
    "threshold_upper": "NUMBER(20,4)",
    "action_required": "VARCHAR",
    "effective_from": "DATE",
    "effective_to": "DATE",
    "supersedes_clause_id": "VARCHAR",
}
TYPOLOGY_COLUMNS: dict[str, str] = {
    "typology_id": "VARCHAR",
    "name": "VARCHAR",
    "fatf_reference": "VARCHAR",
    "rbi_reference": "VARCHAR",
    "description": "VARCHAR",
    "detection_approach": "VARCHAR",
    "amlworld_pattern": "VARCHAR",
}
RULE_COLUMNS: dict[str, str] = {
    "rule_id": "VARCHAR",
    "rule_version": "NUMBER(10,0)",
    "typology_id": "VARCHAR",
    "clause_ids": "ARRAY",
    "description": "VARCHAR",
    "detection_grain": "VARCHAR",
    "window_bucket_expr": "VARCHAR",
    "rule_sql_hash": "VARCHAR",
    "severity": "VARCHAR",
    "is_active": "BOOLEAN",
    "admissible_as_primary_evidence": "BOOLEAN",
}
CHANNEL_COLUMNS: dict[str, str] = {
    "channel_code": "VARCHAR",
    "channel_name": "VARCHAR",
    "is_cash": "BOOLEAN",
    "is_instant": "BOOLEAN",
    "typical_max_inr": "NUMBER(20,2)",
}

#: ``RULE_DEFINITION.detection_grain`` values (the DDL's comment).
DETECTION_GRAINS = ("TRANSACTION", "ACCOUNT_WINDOW", "PARTY_WINDOW")


def sql_literal(value: Any, sql_type: str) -> str:
    """``value`` as a Snowflake literal cast to ``sql_type``.

    Strings escape backslashes as well as quotes: Snowflake reads ``\\`` inside a
    single-quoted literal as an escape, so ``'a\\b'`` would not round-trip.
    """
    if value is None:
        return f"NULL::{sql_type}"
    if sql_type == "ARRAY":
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise ConfigError(f"Expected a list for an ARRAY column, got {value!r}")
        return "ARRAY_CONSTRUCT(" + ", ".join(sql_literal(v, "VARCHAR") for v in value) + ")"
    if sql_type == "BOOLEAN":
        if not isinstance(value, bool):
            raise ConfigError(f"Expected true/false, got {value!r}")
        return "TRUE" if value else "FALSE"
    if sql_type.startswith("NUMBER"):
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
            raise ConfigError(f"Expected a number, got {value!r}")
        try:
            number = Decimal(str(value))  # str(): a YAML float must not become its binary expansion
        except ArithmeticError as exc:
            raise ConfigError(f"Expected a number, got {value!r}") from exc
        if not number.is_finite():
            raise ConfigError(f"Expected a finite number, got {value!r}")
        return f"{number}::{sql_type}"
    if sql_type == "DATE":
        date = value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))
        return f"'{date.isoformat()}'::DATE"
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return f"'{text}'::{sql_type}"


def _row(values: Mapping[str, Any], columns: Mapping[str, str]) -> dict[str, str]:
    return {col: sql_literal(values.get(col), typ) for col, typ in columns.items()}


def _unique(ids: Sequence[Any], what: str) -> None:
    seen: set[Any] = set()
    for key in ids:
        if key in seen:
            raise ConfigError(f"conf/detection.yml: duplicate {what} {key!r}")
        seen.add(key)


def _text(value: Any) -> str | None:
    """Folded YAML text, whitespace-normalised; ``None`` stays ``None``."""
    return " ".join(str(value).split()) if value is not None else None


def rule_sql_path(sql_root: Path, rule_id: str) -> Path:
    return sql_root / RULE_SQL_DIR / f"{rule_id}.sql"


def rule_sql_hash(path: Path) -> str:
    """SHA-256 of a rule's SQL source, line endings normalised.

    The template source, not its rendering: the rendered text carries the
    profile's object names, and one rule must hash the same on dev and demo, on
    Windows and Linux checkouts alike.
    """
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def policy_seed(detection: Mapping[str, Any], sql_root: Path) -> dict[str, list[dict[str, str]]]:
    """Rows for the four seeded tables, as ``{table: [{column: literal}]}``."""
    clauses = list(detection.get("clauses") or [])
    typologies = list(detection.get("typologies") or [])
    rules = list(detection.get("rules") or [])
    channels = list(detection.get("channels") or [])
    severity = dict(detection.get("severity") or {})
    for name, block in (
        ("clauses", clauses),
        ("typologies", typologies),
        ("rules", rules),
        ("channels", channels),
    ):
        if not block:
            # An empty block would render "CREATE ... AS ;" and fail mid-stage.
            raise ConfigError(f"conf/detection.yml: `{name}` is empty; nothing to seed")

    clause_rows = []
    for c in clauses:
        provenance = c.get("provenance") or {}
        if not str(provenance.get("source_document") or "").strip():
            raise ConfigError(
                f"conf/detection.yml: clause {c.get('clause_id')!r} has no "
                "provenance.source_document; a governed threshold must cite its source"
            )
        clause_rows.append(
            _row(
                {
                    **c,
                    "clause_version": c.get("clause_version", 1),
                    "source_document": provenance["source_document"],
                    "source_page": provenance.get("source_page"),
                    "section_heading": provenance.get("section_heading"),
                    "clause_text": _text(c.get("notes")),
                },
                CLAUSE_COLUMNS,
            )
        )
    _unique([(c["clause_id"], c.get("clause_version", 1)) for c in clauses], "clause")
    clause_by_id = {c["clause_id"]: c for c in clauses}

    _unique([t["typology_id"] for t in typologies], "typology_id")
    typology_ids = {t["typology_id"] for t in typologies}
    typology_rows = [
        _row({**t, "description": _text(t.get("description"))}, TYPOLOGY_COLUMNS)
        for t in typologies
    ]

    rule_rows = []
    for r in rules:
        rid = r["rule_id"]
        roles = r.get("clauses") or {}
        if not isinstance(roles, Mapping):
            raise ConfigError(
                f"conf/detection.yml: rule {rid!r} clauses must map role -> clause_id"
            )
        ids = list(roles.values())
        if not ids:
            raise ConfigError(f"conf/detection.yml: rule {rid!r} implements no clause")
        unknown = [i for i in ids if i not in clause_by_id]
        if unknown:
            raise ConfigError(f"conf/detection.yml: rule {rid!r} names unknown clause(s) {unknown}")
        if r.get("typology_id") not in typology_ids:
            raise ConfigError(f"conf/detection.yml: rule {rid!r} has unknown typology_id")
        if rid not in severity:
            raise ConfigError(f"conf/detection.yml: rule {rid!r} has no severity entry")
        if r.get("detection_grain") not in DETECTION_GRAINS:
            raise ConfigError(
                f"conf/detection.yml: rule {rid!r} detection_grain must be one of "
                f"{DETECTION_GRAINS}"
            )
        sql_path = rule_sql_path(sql_root, rid)
        if not sql_path.is_file():
            raise ConfigError(
                f"conf/detection.yml: rule {rid!r} has no detection SQL at "
                f"sql/{RULE_SQL_DIR}/{rid}.sql; only built rules are registered"
            )
        rule_rows.append(
            _row(
                {
                    **r,
                    "clause_ids": ids,
                    "description": _text(r.get("description")),
                    "window_bucket_expr": window_bucket_expr(r, clause_by_id),
                    "rule_sql_hash": rule_sql_hash(sql_path),
                    "severity": severity[rid],
                },
                RULE_COLUMNS,
            )
        )
    _unique([(r["rule_id"], r["rule_version"]) for r in rules], "rule")

    _unique([ch["channel_code"] for ch in channels], "channel_code")
    channel_rows = [_row(ch, CHANNEL_COLUMNS) for ch in channels]

    return {
        "clauses": clause_rows,
        "typologies": typology_rows,
        "rules": rule_rows,
        "channels": channel_rows,
    }


def window_bucket_expr(rule: Mapping[str, Any], clauses: Mapping[str, Any]) -> str | None:
    """The documented bucket expression of a windowed rule (GAP-11), or ``None``.

    Text for the registry, naming the anchor and the clause the window length is
    read from (the rule's ``window`` clause) -- never the length itself, which
    lives only in POLICY_CLAUSE.
    """
    if rule.get("detection_grain") == "TRANSACTION":
        return None
    clause_id = (rule.get("clauses") or {}).get("window")
    anchor = rule.get("window_anchor")
    if clause_id not in clauses or not anchor:
        raise ConfigError(
            f"conf/detection.yml: windowed rule {rule.get('rule_id')!r} needs a `window` clause "
            "naming a known clause and a window_anchor"
        )
    anchor_date = dt.date.fromisoformat(str(anchor)).isoformat()
    return (
        f"FLOOR(DATEDIFF('day', '{anchor_date}'::DATE, TO_DATE(txn_timestamp)) / "
        f"{clause_id}.threshold_lower)"
    )


def routing_context(detection: Mapping[str, Any]) -> dict[str, str]:
    """``conf/detection.yml`` ``routing`` as SQL literals for ROUTE_SIGNALS.

    Validated by :func:`risk_copilot.config._check_routing` when the settings
    load; this only renders. ``case_type_by_rule`` becomes an OBJECT keyed by
    rule id, so the procedure looks a rule's case type up rather than listing it.
    """
    routing = detection.get("routing") or {}
    case_types = routing.get("case_type_by_rule") or {}
    pairs = ", ".join(
        f"{sql_literal(str(rule), 'VARCHAR')}, {sql_literal(str(kind).upper(), 'VARCHAR')}"
        for rule, kind in sorted(case_types.items())
    )
    return {
        "severity_order": sql_literal(
            [str(s).upper() for s in routing.get("severity_order") or []], "ARRAY"
        ),
        "min_severity_for_case": sql_literal(
            str(routing.get("min_severity_for_case") or "").upper(), "VARCHAR"
        ),
        "open_case_statuses": sql_literal(
            [str(s).upper() for s in routing.get("open_case_statuses") or []], "ARRAY"
        ),
        "cross_domain_min_distinct_rules": sql_literal(
            routing.get("cross_domain_min_distinct_rules"), "NUMBER(10,0)"
        ),
        "case_type_by_rule": f"OBJECT_CONSTRUCT({pairs})",
    }


def rule_context(detection: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """What a rule's detection SQL may reference, as SQL literals.

    ``{rule_id: {id, version, clause: {role: 'CLAUSE_ID'}, anchor, dynamic_table}}``.
    The clause ids are names, not thresholds: the SQL joins each to
    V_POLICY_CLAUSE_CURRENT for its value at refresh time.
    """
    out: dict[str, dict[str, Any]] = {}
    for r in detection.get("rules") or []:
        anchor = r.get("window_anchor")
        clauses = r.get("clauses") or {}
        out[str(r["rule_id"])] = {
            "id": sql_literal(r["rule_id"], "VARCHAR"),
            "version": int(r["rule_version"]),
            "clause": {str(role): sql_literal(cid, "VARCHAR") for role, cid in clauses.items()},
            "anchor": sql_literal(anchor, "DATE") if anchor else None,
            "dynamic_table": str(r.get("dynamic_table") or ""),
        }
    return out
