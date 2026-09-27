"""Structuring injection, evaluation grain, demo hero (Phase 2, step 6).

Uses the ``injection_hosts`` sample. Its one host, EA (``800A0000A``), owns two
SAVINGS accounts (``21174/8000A0100``, ``10/8000A0102``) and one LOAN account,
which must never receive a cash deposit.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from risk_copilot.config import deep_merge, load_settings
from risk_copilot.generation.demo_entity import DemoEntityError, select_demo_entity
from risk_copilot.generation.entities import build_entities
from risk_copilot.generation.eval_grain import build_eval_grain
from risk_copilot.generation.graph import sample_subgraph
from risk_copilot.generation.inject import InjectionError, cash_desk_ids, inject_structuring
from risk_copilot.generation.labels import label_source_legs
from risk_copilot.generation.patterns import load_patterns, locate_attempts
from risk_copilot.generation.reskin import reskin_pairs
from risk_copilot.generation.source import load_accounts, load_ledger
from risk_copilot.ids import account_id, party_id

pytestmark = pytest.mark.unit

HOST = "800A0000A"


@pytest.fixture
def config(repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    settings = load_settings(repo_root=repo_root, profile="dev")
    generation = deep_merge(
        settings.generation,
        {
            "subgraph": {"target_parties": 50, "target_accounts": 50, "max_transactions": 1000},
            "injection": {"structuring": {"cohort_parties": 1}},
        },
    )
    return generation, settings.detection


@pytest.fixture
def generation(config: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    return config[0]


@pytest.fixture
def detection(config: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    return config[1]


@pytest.fixture
def built(
    amlworld_mini: Path, generation: dict[str, Any], detection: dict[str, Any]
) -> dict[str, Any]:
    ledger = load_ledger(amlworld_mini, generation)
    patterns = load_patterns(amlworld_mini, generation)
    location = locate_attempts(patterns, ledger)
    sg = sample_subgraph(
        ledger, load_accounts(amlworld_mini, generation), patterns, location, generation
    )
    legs = reskin_pairs(sg, generation)
    labels = label_source_legs(sg, patterns, location, generation, detection)
    ents = build_entities(sg, legs, labels, generation)
    return {"subgraph": sg, "legs": legs, "labels": labels, "entities": ents}


def inject(built: dict[str, Any], generation: dict[str, Any], **structuring: Any) -> Any:
    config = (
        deep_merge(generation, {"injection": {"structuring": structuring}})
        if structuring
        else generation
    )
    return inject_structuring(built["subgraph"], built["entities"], config)


def host_accounts(built: dict[str, Any]) -> dict[str, str]:
    sg = built["subgraph"]
    types = dict(
        zip(
            built["entities"].accounts["account_id"].to_pylist(),
            built["entities"].accounts["account_type"].to_pylist(),
            strict=True,
        )
    )
    return {sg.account_ids[k]: types[sg.account_ids[k]] for k in sg.accounts_by_entity[HOST]}


# --------------------------------------------------------------------------- #
# Injection
# --------------------------------------------------------------------------- #


def test_deposits_land_only_in_deposit_accounts(
    built: dict[str, Any], generation: dict[str, Any]
) -> None:
    inj = inject(built, generation)
    cohort = generation["injection"]["structuring"]
    accounts = host_accounts(built)
    credited = {r["account_id"] for r in inj.pending.to_pylist() if r["leg_side"] == "C"}
    assert credited <= {a for a, t in accounts.items() if t in cohort["deposit_account_types"]}
    assert cohort["accounts_per_party_min"] <= len(credited) <= cohort["accounts_per_party_max"]
    assert "LOAN" in accounts.values()  # the fixture's trap is really there


def test_each_deposit_is_a_cash_desk_debit_and_a_customer_credit(
    built: dict[str, Any], generation: dict[str, Any]
) -> None:
    inj = inject(built, generation)
    desk_party, _, desk = cash_desk_ids(generation)
    region = built["entities"].branch_region[built["subgraph"].party_ids[HOST]]
    desk_account = desk[region][1]
    pairs: dict[str, list[dict[str, Any]]] = {}
    for row in inj.pending.to_pylist():
        pairs.setdefault(row["pair_ref"], []).append(row)
    cohort = generation["injection"]["structuring"]
    for legs in pairs.values():
        debit, credit = sorted(legs, key=lambda r: r["leg_side"], reverse=True)
        assert (debit["leg_side"], debit["amount_sign"], debit["account_id"]) == (
            "D",
            -1,
            desk_account,
        )
        assert (credit["leg_side"], credit["amount_sign"], credit["counterparty_account_id"]) == (
            "C",
            1,
            desk_account,
        )
        assert debit["counterparty_account_id"] == credit["account_id"]
        assert debit["amount_ctr_ratio"] == credit["amount_ctr_ratio"]
        for leg in legs:
            assert (leg["channel"], leg["txn_type"], leg["currency"]) == (
                cohort["channel"],
                cohort["txn_type"],
                generation["fx"]["base_currency"],
            )
            assert leg["cohort"] == cohort["label"]
    per_account: dict[str, int] = {}
    for legs in pairs.values():
        acct = next(r["account_id"] for r in legs if r["leg_side"] == "C")
        per_account[acct] = per_account.get(acct, 0) + 1
    assert all(
        cohort["deposits_per_account_min"] <= n <= cohort["deposits_per_account_max"]
        for n in per_account.values()
    )
    assert desk_party == party_id(
        generation["internal_accounts"]["owner_entity_key"], seed=generation["seed"]
    )


def test_ratios_lie_in_the_band_at_the_declared_scale(
    built: dict[str, Any], generation: dict[str, Any]
) -> None:
    cohort = generation["injection"]["structuring"]
    lo, hi = (Decimal(str(v)) for v in cohort["amount_band_pct_of_ctr"])
    quantum = Decimal(1).scaleb(-cohort["amount_ctr_ratio_decimals"])
    for ratio in inject(built, generation).pending["amount_ctr_ratio"].to_pylist():
        assert lo <= ratio <= hi
        assert ratio == ratio.quantize(quantum)


def test_deposits_fall_in_one_burst_inside_the_legit_window(
    built: dict[str, Any], generation: dict[str, Any]
) -> None:
    cohort = generation["injection"]["structuring"]
    window = built["entities"].window
    inj = inject(built, generation)
    (plan,) = inj.hosts
    assert (plan.window_end - plan.window_start).days == cohort["window_days"] - 1
    assert window.first_day <= plan.window_start and plan.window_end <= window.last_day
    start, end = cohort["deposit_hours"]
    for ts in inj.pending["txn_timestamp"].to_pylist():
        assert plan.window_start <= ts.date() <= plan.window_end
        assert start <= ts.hour < end and ts.second == 0


def test_pending_legs_carry_no_rupee_amount(
    built: dict[str, Any], generation: dict[str, Any]
) -> None:
    """D2: rupees are resolved at load from POLICY_CLAUSE, never here."""
    assert not {"amount", "amount_inr"} & set(inject(built, generation).pending.column_names)


def test_every_injected_leg_is_labelled(built: dict[str, Any], generation: dict[str, Any]) -> None:
    inj = inject(built, generation)
    cohort = generation["injection"]["structuring"]
    legs = {(r["pair_ref"], r["leg_side"]) for r in inj.pending.to_pylist()}
    labels = {(r["pair_ref"], r["leg_side"]): r for r in inj.labels.to_pylist()}
    assert set(labels) == legs
    for row in labels.values():
        assert row["is_illicit"] is True
        assert (row["typology_id"], row["injection_method"]) == (
            cohort["typology_id"],
            cohort["label"],
        )
        assert row["src_row_number"] is None and row["match_confidence"] is None


def test_cash_desk_lives_only_in_the_id_maps(
    built: dict[str, Any], generation: dict[str, Any]
) -> None:
    """D4: the desk is never a customer record."""
    inj = inject(built, generation)
    internal = generation["internal_accounts"]
    regions = [r["code"] for r in generation["entities"]["branch_regions"]]
    expected = {
        account_id(
            internal["src_bank_id"],
            f"{internal['cash_desk']['account_number_prefix']}:{r}",
            seed=generation["seed"],
        )
        for r in regions
    }
    assert set(inj.id_map_account["account_id"].to_pylist()) == expected
    assert set(inj.id_map_account["src_bank_id"].to_pylist()) == {internal["src_bank_id"]}
    ents = built["entities"]
    assert expected.isdisjoint(ents.accounts["account_id"].to_pylist())
    assert inj.id_map_party["party_id"].to_pylist()[0] not in ents.cbs["party_id"].to_pylist()


def test_window_longer_than_the_legit_span_fails(
    built: dict[str, Any], generation: dict[str, Any]
) -> None:
    span = built["entities"].window.span_days
    with pytest.raises(InjectionError, match="injection_window"):
        inject(built, generation, window_days=span + 1)


def test_injection_is_deterministic(built: dict[str, Any], generation: dict[str, Any]) -> None:
    a, b = inject(built, generation), inject(built, generation)
    assert a.pending.equals(b.pending) and a.labels.equals(b.labels)


# --------------------------------------------------------------------------- #
# Evaluation grain
# --------------------------------------------------------------------------- #


def grain(built: dict[str, Any], generation: dict[str, Any], detection: dict[str, Any]) -> pa.Table:
    inj = inject(built, generation)
    owners = dict(
        zip(
            built["entities"].accounts["account_id"].to_pylist(),
            built["entities"].accounts["party_id"].to_pylist(),
            strict=True,
        )
    )
    return build_eval_grain(
        [(built["legs"], built["labels"]), (inj.pending, inj.labels)], owners, detection
    )


def test_host_month_expects_the_structuring_rule(
    built: dict[str, Any], generation: dict[str, Any], detection: dict[str, Any]
) -> None:
    table = {r["party_id"]: r for r in grain(built, generation, detection).to_pylist()}
    host = table[built["subgraph"].party_ids[HOST]]
    deposits = inject(built, generation).stats["structuring_deposits"]
    assert host["is_illicit_actual"] is True
    assert host["typology_id"] == "TYP_STRUCTURING"
    assert host["expected_rule_ids"] == ["STRUCTURING_CROSS_ACCOUNT"]
    assert host["illicit_leg_count"] == deposits  # one customer credit per deposit
    assert host["month_bucket"] == "2022-09"


def test_unattributed_illicit_expects_no_rule(
    built: dict[str, Any], generation: dict[str, Any], detection: dict[str, Any]
) -> None:
    """E9's row 11 is illicit with no attempt (M5)."""
    table = {r["party_id"]: r for r in grain(built, generation, detection).to_pylist()}
    e9 = table[built["subgraph"].party_ids["800A00009"]]
    assert e9["is_illicit_actual"] is True
    assert e9["typology_id"] is None and e9["expected_rule_ids"] == []


def test_grain_counts_every_customer_leg_and_skips_the_desk(
    built: dict[str, Any], generation: dict[str, Any], detection: dict[str, Any]
) -> None:
    table = grain(built, generation, detection)
    inj = inject(built, generation)
    customer_legs = built["legs"].num_rows + inj.pending.num_rows // 2
    assert sum(table["total_leg_count"].to_pylist()) == customer_legs
    assert inj.id_map_party["party_id"].to_pylist()[0] not in table["party_id"].to_pylist()
    for row in table.to_pylist():
        if not row["is_illicit_actual"]:
            assert row["illicit_leg_count"] == 0 and row["expected_rule_ids"] == []


# --------------------------------------------------------------------------- #
# Demo hero
# --------------------------------------------------------------------------- #


def test_dev_hero_is_a_structuring_host(
    built: dict[str, Any], generation: dict[str, Any], detection: dict[str, Any]
) -> None:
    pin = select_demo_entity(
        grain(built, generation, detection), built["entities"].cbs, generation, profile="dev"
    )
    assert pin is not None
    assert pin["party_id"] == built["subgraph"].party_ids[HOST]
    assert pin["status"] == "PROVISIONAL_UNTIL_DETECTED"
    assert pin["must_trigger_rules"] == ["STRUCTURING_CROSS_ACCOUNT"]


def test_rules_with_no_constructed_cohort_fail(
    built: dict[str, Any], generation: dict[str, Any], detection: dict[str, Any]
) -> None:
    table = grain(built, generation, detection)
    demo_rules = {
        "demo_entity": {
            "must_trigger_rules": ["STRUCTURING_CROSS_ACCOUNT", "RAPID_CREDIT_VELOCITY"]
        }
    }
    with pytest.raises(DemoEntityError, match="RAPID_CREDIT_VELOCITY"):
        select_demo_entity(
            table, built["entities"].cbs, deep_merge(generation, demo_rules), profile="demo"
        )
    lenient = deep_merge(
        generation,
        {**demo_rules, "demo_entity": {**demo_rules["demo_entity"], "fail_if_not_found": False}},
    )
    assert select_demo_entity(table, built["entities"].cbs, lenient, profile="demo") is None


def test_hero_prefers_the_configured_region(generation: dict[str, Any]) -> None:
    grain_rows = pa.Table.from_pylist(
        [
            {"party_id": p, "expected_rule_ids": ["STRUCTURING_CROSS_ACCOUNT"]}
            for p in ("P-1", "P-2", "P-3")
        ]
    )
    cbs = pa.Table.from_pylist(
        [
            {
                "party_id": "P-1",
                "full_name": "A",
                "party_type": "INDIVIDUAL",
                "branch_region": "DELHI",
            },
            {
                "party_id": "P-2",
                "full_name": "B",
                "party_type": "LEGAL_PERSON",
                "branch_region": "MUMBAI",
            },
            {
                "party_id": "P-3",
                "full_name": "C",
                "party_type": "INDIVIDUAL",
                "branch_region": "MUMBAI",
            },
        ]
    )
    pin = select_demo_entity(grain_rows, cbs, generation, profile="dev")
    assert pin is not None and pin["party_id"] == "P-3"  # individual, then Mumbai


def test_window_dates_are_dates(built: dict[str, Any]) -> None:
    assert isinstance(built["entities"].window.first_day, dt.date)
