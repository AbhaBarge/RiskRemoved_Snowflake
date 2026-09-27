"""Coherence of the generation contract (Phase 2, step 1).

`conf/generation.yml`, `conf/detection.yml` and the DDL make claims about each
other: the injection band lies inside the detection band, the cohort's clause
ids exist, the typologies cover every AMLworld pattern, no label column lands in
a table that feeds the semantic view. Each claim was either wrong or unstated
before measurement, so each is pinned here against the real `conf/` and the real
templates -- cheap to check now, expensive to discover after a 400K-leg build.

The measured source facts themselves (18-day span, 1:1 label attribution, bank
id padding) are asserted by the generator at run time against the data, because
unit tests must not need the 950 MB source files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from risk_copilot.config import Settings, load_settings
from risk_copilot.generation.graph import reserved_injection_legs
from risk_copilot.ids import normalise_bank_id
from risk_copilot.pipeline.deploy import Deployer, split_statements

pytestmark = pytest.mark.unit

#: The eight labels measured in HI-Small_Patterns.txt. Three of them
#: (BIPARTITE, SCATTER-GATHER, STACK) carry no ":" detail suffix.
AMLWORLD_PATTERNS = {
    "FAN-OUT",
    "FAN-IN",
    "CYCLE",
    "GATHER-SCATTER",
    "SCATTER-GATHER",
    "BIPARTITE",
    "STACK",
    "RANDOM",
}

#: RAW tables `generate` writes. Every one feeds CURATED, so none may carry a label.
GENERATED_RAW_TABLES = {
    "ID_MAP_PARTY",
    "ID_MAP_ACCOUNT",
    "RAW_TRANSACTIONS",
    "RAW_PARTY_CBS",
    "RAW_PARTY_KYC",
    "RAW_ACCOUNTS",
    "RAW_PARTY_RELATIONSHIP_ATTR",
}

_CREATE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*)\)", re.DOTALL | re.IGNORECASE)
_COLUMN_RE = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s+[A-Z]", re.MULTILINE)


@pytest.fixture(params=["dev", "demo"])
def settings(request: pytest.FixtureRequest, repo_root: Path) -> Settings:
    return load_settings(repo_root=repo_root, profile=request.param)


def clauses(settings: Settings) -> dict[str, dict[str, Any]]:
    return {str(c["clause_id"]): c for c in settings.detection["clauses"]}


def structuring(settings: Settings) -> dict[str, Any]:
    cohort: dict[str, Any] = settings.generation["injection"]["structuring"]
    return cohort


def table_columns(settings: Settings, filename: str) -> dict[str, set[str]]:
    """Column names per table, parsed from a rendered DDL template."""
    deployer = Deployer(settings)
    path = next(p for p in deployer.stage_files("ddl") if p.name == filename)
    tables: dict[str, set[str]] = {}
    for statement in split_statements(deployer.render(path)):
        if match := _CREATE_RE.search(statement):
            body = re.sub(r"'[^']*'", "''", match.group(2))  # drop comment text
            tables[match.group(1).upper()] = {
                col for col in _COLUMN_RE.findall(body) if col != "constraint"
            }
    return tables


# --------------------------------------------------------------------------- #
# Injection <-> detection
# --------------------------------------------------------------------------- #


def test_structuring_references_only_clauses_that_exist(settings: Settings) -> None:
    known = clauses(settings)
    cohort = structuring(settings)
    for key in ("ctr_clause_id", "band_clause_id", "window_clause_id", "min_accounts_clause_id"):
        assert cohort[key] in known, f"injection.structuring.{key} = {cohort[key]!r}"


def test_injection_band_lies_within_the_detection_band(settings: Settings) -> None:
    """Otherwise the cohort is built to be undetectable -- or trivially detectable."""
    lower, upper = structuring(settings)["amount_band_pct_of_ctr"]
    band = clauses(settings)[structuring(settings)["band_clause_id"]]
    assert band["threshold_lower"] <= lower < upper <= band["threshold_upper"]


def test_injection_window_fits_the_detection_window(settings: Settings) -> None:
    window = clauses(settings)[structuring(settings)["window_clause_id"]]
    assert structuring(settings)["window_days"] <= window["threshold_lower"]


def test_injection_window_fits_the_measured_ledger_span(settings: Settings) -> None:
    """Legit activity spans 10 days, 09-01..09-10 (M21). Re-asserted against the data."""
    assert structuring(settings)["window_days"] <= 10


def test_every_host_can_satisfy_the_minimum_account_clause(settings: Settings) -> None:
    minimum = clauses(settings)[structuring(settings)["min_accounts_clause_id"]]
    assert structuring(settings)["accounts_per_party_min"] >= minimum["threshold_lower"]


def test_deferred_amounts_are_the_declared_resolution(settings: Settings) -> None:
    assert structuring(settings)["amount_resolution"] == "deferred_ctr_ratio"
    assert structuring(settings)["debit_side"] == "cash_desk"


def test_only_the_slice_cohort_is_enabled(settings: Settings) -> None:
    enabled = {k for k, v in settings.generation["injection"].items() if v.get("enabled")}
    assert enabled == {"structuring"}


# --------------------------------------------------------------------------- #
# Typologies
# --------------------------------------------------------------------------- #


def test_typologies_cover_every_measured_amlworld_pattern(settings: Settings) -> None:
    patterns = {t["amlworld_pattern"] for t in settings.detection["typologies"]}
    assert patterns >= AMLWORLD_PATTERNS


def test_typology_ids_are_unique(settings: Settings) -> None:
    ids = [t["typology_id"] for t in settings.detection["typologies"]]
    assert len(ids) == len(set(ids))


def test_injected_cohort_names_a_known_typology(settings: Settings) -> None:
    by_id = {t["typology_id"]: t for t in settings.detection["typologies"]}
    cohort = structuring(settings)
    assert cohort["typology_id"] in by_id
    assert by_id[cohort["typology_id"]]["injection_label"] == cohort["label"]


def test_expected_rules_are_rules_the_router_knows(settings: Settings) -> None:
    known = set(settings.detection["severity"])
    for typology in settings.detection["typologies"]:
        unknown = set(typology["expected_rule_ids"]) - known
        assert not unknown, f"{typology['typology_id']}: {sorted(unknown)}"


def test_graph_typologies_assert_no_rule_in_advance(settings: Settings) -> None:
    """Which rule catches a STACK is measured, not declared."""
    for typology in settings.detection["typologies"]:
        if typology["amlworld_pattern"]:
            assert typology["expected_rule_ids"] == [], typology["typology_id"]


def test_source_legs_are_labelled_with_the_amlworld_injection_label(settings: Settings) -> None:
    method = settings.generation["labels"]["source_injection_method"]
    for typology in settings.detection["typologies"]:
        if typology["amlworld_pattern"]:
            assert typology["injection_label"] == method, typology["typology_id"]


def test_every_fx_currency_has_an_iso_code(settings: Settings) -> None:
    fx = settings.generation["fx"]
    assert set(fx["rates_to_inr"]) == set(fx["iso_codes"])
    assert fx["iso_codes"]["Rupee"] == fx["base_currency"]


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #


def test_every_measured_entity_prefix_is_mapped(settings: Settings) -> None:
    mapping = settings.generation["entities"]["party_type_from_entity_name"]
    measured = {
        "Corporation",
        "Sole Proprietorship",
        "Partnership",
        "Individual",
        "Country",
        "Direct",
    }
    assert measured <= set(mapping)


def test_unmapped_prefixes_fail_rather_than_default(settings: Settings) -> None:
    assert settings.generation["entities"]["party_type_default"] is None


def test_sole_proprietors_are_individuals(settings: Settings) -> None:
    mapping = settings.generation["entities"]["party_type_from_entity_name"]
    assert mapping["Sole Proprietorship"] == "INDIVIDUAL"


def test_non_customer_entities_are_excluded(settings: Settings) -> None:
    entities = settings.generation["entities"]
    mapping = entities["party_type_from_entity_name"]
    assert {mapping["Country"], mapping["Direct"]} <= set(entities["excluded_party_types"])


def test_income_has_no_ceiling(settings: Settings) -> None:
    """A cap below real capacity manufactures a mismatch (9,755 legit entities)."""
    income = settings.generation["entities"]["income"]
    assert "ceiling_inr" not in income
    assert income["derive_from"] == "legit_inflow_window_annualised"


def test_internal_bank_id_cannot_normalise_into_a_real_bank(settings: Settings) -> None:
    src = str(settings.generation["internal_accounts"]["src_bank_id"])
    assert not src.isdigit()
    assert normalise_bank_id(src) == src.upper()


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


def test_dev_seeds_from_the_slice_hosts_and_demo_from_the_patterns(repo_root: Path) -> None:
    dev = load_settings(repo_root=repo_root, profile="dev").generation
    demo = load_settings(repo_root=repo_root, profile="demo").generation
    assert dev["subgraph"]["seed_source"] == "injection_hosts"
    assert demo["subgraph"]["seed_source"] == "laundering_patterns"


@pytest.mark.parametrize("profile", ["dev", "demo"])
def test_worst_case_reserve_fits_the_leg_budget(repo_root: Path, profile: str) -> None:
    """What the old cohort-sizing test's division really checked.

    It divided the worst-case injected legs by ``max_transactions`` and compared
    that with the illicit-rate ceiling -- optimistic, because induced samples are
    far sparser than the budget (M18). The rate itself is the blocking
    ``illicit_rate_pct`` gate, over the real legs; this checks only that the
    reserve leaves room for source legs at all.
    """
    generation = load_settings(repo_root=repo_root, profile=profile).generation
    assert reserved_injection_legs(generation) < generation["subgraph"]["max_transactions"]
    assert generation["validation"]["illicit_rate_pct"]["fail_build"] is True


def test_desk_routed_cash_posts_like_an_injected_deposit(settings: Settings) -> None:
    """D27: genuine and planted cash deposits must share one shape at the desk."""
    generation = settings.generation
    cohort = generation["injection"]["structuring"]
    routed = generation["internal_accounts"]["cash_desk"]["routes_payment_formats"]
    assert routed
    for fmt in routed:
        assert generation["channel_map"][fmt] == cohort["channel"]
        assert generation["txn_types"]["by_format"][fmt]["credit"] == cohort["txn_type"]
    assert cohort["debit_side"] == "cash_desk"


# --------------------------------------------------------------------------- #
# Label isolation in the DDL
# --------------------------------------------------------------------------- #


def test_no_generated_raw_table_declares_a_label_column(settings: Settings) -> None:
    forbidden = set(settings.generation["validation"]["label_leakage"]["forbidden_columns"])
    tables = table_columns(settings, "01_raw_landing.sql")
    assert tables.keys() >= GENERATED_RAW_TABLES
    for name in GENERATED_RAW_TABLES:
        leaked = tables[name] & forbidden
        assert not leaked, f"{name} declares label column(s) {sorted(leaked)}"


def test_label_column_parser_is_not_vacuous(settings: Settings) -> None:
    """Negative control for the test above.

    RAW_AMLWORLD_TRANS is the verbatim source and legitimately holds the label --
    it is not generated and never feeds CURATED. If the parser cannot see
    `is_laundering` there, the leakage test above proves nothing.
    """
    tables = table_columns(settings, "01_raw_landing.sql")
    assert "is_laundering" in tables["RAW_AMLWORLD_TRANS"]
    assert {"txn_id", "amount_inr", "counterparty_account_id"} <= tables["RAW_TRANSACTIONS"]


def test_deferred_amount_table_lives_in_ground_truth(settings: Settings) -> None:
    deployer = Deployer(settings)
    path = next(p for p in deployer.stage_files("ddl") if p.name == "04_audit_and_ground_truth.sql")
    rendered = deployer.render(path)
    gt_section = rendered.split(f"USE SCHEMA {settings.fq('ground_truth')};", 1)
    assert len(gt_section) == 2
    assert "CREATE TABLE IF NOT EXISTS INJECTED_TXN_PENDING" in gt_section[1]
    assert (
        "amount_ctr_ratio"
        in table_columns(settings, "04_audit_and_ground_truth.sql")["INJECTED_TXN_PENDING"]
    )
