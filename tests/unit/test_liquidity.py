"""The liquidity slice: the illustrative ALM book and V_STRUCTURAL_LIQUIDITY.

The book is config, the limits are POLICY_CLAUSE rows, and the view joins the
two. These tests pin that split, refuse a book that names an unknown bucket,
side or clause, and recompute the statement in Python so the demo's claim (one
breach, in the 8-14 day bucket) is checked rather than asserted.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from risk_copilot.config import ConfigError, load_settings
from risk_copilot.pipeline.deploy import Deployer
from risk_copilot.policy import alm_book_rows

pytestmark = pytest.mark.unit


@pytest.fixture
def settings(repo_root: Path) -> Any:
    return load_settings(repo_root=repo_root, profile="dev")


def _confs(settings: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return copy.deepcopy(settings.generation), copy.deepcopy(settings.detection)


def test_one_row_per_line_and_bucket_in_rupees(settings: Any) -> None:
    generation, detection = _confs(settings)
    book = generation["alm_book"]
    rows = alm_book_rows(generation, detection)
    assert len(rows) == sum(len(line["amounts"]) for line in book["lines"])
    cp = next(
        r
        for r in rows
        if r["line_item"] == "'Commercial paper maturing'::VARCHAR"
        and r["bucket_id"] == "2::NUMBER(10,0)"
    )
    crore = Decimal(str(book["lines"][0]["amounts"][2]))
    assert cp["amount_inr"] == f"{crore * Decimal(str(book['unit_inr']))}::NUMBER(20,2)"
    assert cp["tolerance_clause_id"] == "'LIQ_MISMATCH_8_14D'::VARCHAR"


def test_every_tolerance_clause_is_a_governed_clause(settings: Any) -> None:
    generation, detection = _confs(settings)
    clauses = {c["clause_id"]: c for c in detection["clauses"]}
    named = [b["tolerance_clause"] for b in generation["alm_book"]["buckets"]]
    for clause_id in filter(None, named):
        assert clauses[clause_id]["provenance"]["source_document"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda g: g["alm_book"]["buckets"][0].update(tolerance_clause="NOPE"), "unknown clause"),
        (lambda g: g["alm_book"]["lines"][0].update(side="SIDEWAYS"), "side"),
        (lambda g: g["alm_book"]["lines"][0]["amounts"].update({99: 1}), "unknown bucket"),
        (lambda g: g["alm_book"]["buckets"].append(dict(g["alm_book"]["buckets"][0])), "duplicate"),
    ],
)
def test_a_malformed_book_is_refused(settings: Any, mutate: Any, message: str) -> None:
    generation, detection = _confs(settings)
    mutate(generation)
    with pytest.raises(ConfigError, match=message):
        alm_book_rows(generation, detection)


def test_no_book_renders_no_rows(settings: Any) -> None:
    _, detection = _confs(settings)
    assert alm_book_rows({}, detection) == []


def test_the_view_reads_its_limits_from_the_clause_view(settings: Any) -> None:
    deployer = Deployer(settings)
    (path,) = [p for p in deployer.stage_files("ddl") if p.name == "05_liquidity.sql"]
    sql = deployer.render(path)
    view = sql[sql.index("CREATE OR REPLACE VIEW") :]
    assert "V_POLICY_CLAUSE_CURRENT p ON p.clause_id = r.tolerance_clause_id" in view
    assert "p.threshold_lower AS tolerance_pct" in view
    assert settings.obj(settings.views["structural_liquidity"]) in view


def _statement(generation: dict[str, Any], detection: dict[str, Any]) -> dict[int, str]:
    """The view's arithmetic in Python: bucket -> WITHIN / BREACH for limited buckets."""
    book = generation["alm_book"]
    limits = {c["clause_id"]: Decimal(str(c["threshold_lower"])) for c in detection["clauses"]}
    flows: dict[int, Decimal] = {}
    outs: dict[int, Decimal] = {}
    for line in book["lines"]:
        sign = 1 if line["side"] == "INFLOW" else -1
        for bucket, crore in line["amounts"].items():
            flows[int(bucket)] = flows.get(int(bucket), Decimal(0)) + sign * Decimal(str(crore))
            if sign < 0:
                outs[int(bucket)] = outs.get(int(bucket), Decimal(0)) + Decimal(str(crore))
    status, cum, cum_out = {}, Decimal(0), Decimal(0)
    for b in sorted(book["buckets"], key=lambda b: b["bucket_id"]):
        cum += flows.get(b["bucket_id"], Decimal(0))
        cum_out += outs.get(b["bucket_id"], Decimal(0))
        if b["tolerance_clause"]:
            pct = 100 * cum / cum_out
            ok = pct >= 0 or -pct <= limits[b["tolerance_clause"]]
            status[b["bucket_id"]] = "WITHIN" if ok else "BREACH"
    return status


def test_the_book_breaches_only_the_8_to_14_day_limit(settings: Any) -> None:
    assert _statement(*_confs(settings)) == {1: "WITHIN", 2: "BREACH", 3: "WITHIN"}
