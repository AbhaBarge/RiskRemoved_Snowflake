"""Reskin and leg-level labels (Phase 2, step 4).

Uses the ``laundering_patterns`` sample of the fixture, which captures FAN-OUT
attempt 1 (rows 1, 3, 4) and CYCLE attempt 3 (rows 9, 10), plus the cash pair
(row 8) and the unattributed illicit pair (row 11).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from risk_copilot.config import deep_merge, load_settings
from risk_copilot.generation.graph import Subgraph, sample_subgraph
from risk_copilot.generation.labels import (
    LabelError,
    confidence,
    label_source_legs,
    raw_laundering_patterns,
)
from risk_copilot.generation.patterns import load_patterns, locate_attempts
from risk_copilot.generation.reskin import (
    ReskinConfig,
    ReskinError,
    assign_pair_ids,
    reskin_pairs,
    source_pair_ref,
)
from risk_copilot.generation.source import SRC_ROW_NUMBER, load_accounts, load_ledger

pytestmark = pytest.mark.unit


@pytest.fixture
def settings_pair(repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    settings = load_settings(repo_root=repo_root, profile="dev")
    generation = deep_merge(
        settings.generation,
        {
            "subgraph": {
                "seed_source": "laundering_patterns",
                "target_parties": 50,
                "target_accounts": 50,
                "max_transactions": 1000,
            },
            "injection": {"structuring": {"cohort_parties": 1}},
        },
    )
    return generation, settings.detection


@pytest.fixture
def generation(settings_pair: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    return settings_pair[0]


@pytest.fixture
def detection(settings_pair: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    return settings_pair[1]


@pytest.fixture
def built(amlworld_mini: Path, generation: dict[str, Any]) -> dict[str, Any]:
    ledger = load_ledger(amlworld_mini, generation)
    patterns = load_patterns(amlworld_mini, generation)
    location = locate_attempts(patterns, ledger)
    subgraph = sample_subgraph(
        ledger, load_accounts(amlworld_mini, generation), patterns, location, generation
    )
    return {"subgraph": subgraph, "patterns": patterns, "location": location}


def legs_by_row(table: Any) -> dict[tuple[int, str], dict[str, Any]]:
    return {(r[SRC_ROW_NUMBER], r["leg_side"]): r for r in table.to_pylist()}


def reskin(built: dict[str, Any], generation: dict[str, Any], **fx: Any) -> Any:
    config = deep_merge(generation, {"fx": fx}) if fx else generation
    return reskin_pairs(built["subgraph"], config)


# --------------------------------------------------------------------------- #
# Reskin
# --------------------------------------------------------------------------- #


def test_every_pair_becomes_one_debit_and_one_credit(
    built: dict[str, Any], generation: dict[str, Any]
) -> None:
    sg: Subgraph = built["subgraph"]
    legs = reskin(built, generation)
    assert legs.num_rows == 2 * sg.pairs.num_rows
    sides: dict[str, list[str]] = {}
    for leg in legs.to_pylist():
        sides.setdefault(leg["pair_ref"], []).append(leg["leg_side"])
        sign = -1 if leg["leg_side"] == "D" else 1
        assert leg["amount"] * sign > 0
        assert leg["amount_inr"] * sign > 0
    assert all(sorted(v) == ["C", "D"] for v in sides.values())


def test_legs_reference_the_sampled_accounts(
    built: dict[str, Any], generation: dict[str, Any]
) -> None:
    sg: Subgraph = built["subgraph"]
    known = set(sg.account_ids.values())
    for leg in reskin(built, generation).to_pylist():
        assert {leg["account_id"], leg["counterparty_account_id"]} <= known
    row1 = legs_by_row(reskin(built, generation))[(1, "D")]
    assert row1["account_id"] == sg.account_ids[("10", "8000A0010")]
    assert row1["counterparty_account_id"] == sg.account_ids[("20", "8000A0030")]


def test_conversion_is_exact_and_per_currency(
    built: dict[str, Any], generation: dict[str, Any]
) -> None:
    legs = legs_by_row(reskin(built, generation, retain_foreign_pct=0))
    # 2848.96 USD x 82.50 = 235,039.20 exactly -- no float noise.
    assert legs[(1, "D")]["amount_inr"] == Decimal("-235039.20")
    assert legs[(1, "D")]["currency"] == "INR"
    assert legs[(1, "D")]["amount"] == legs[(1, "D")]["amount_inr"]


def test_cross_currency_pair_keeps_each_sides_amount(
    built: dict[str, Any], generation: dict[str, Any]
) -> None:
    legs = legs_by_row(reskin(built, generation, retain_foreign_pct=100))
    debit, credit = legs[(9, "D")], legs[(9, "C")]
    assert (debit["amount"], debit["currency"]) == (Decimal("-1000.00"), "USD")
    assert debit["amount_inr"] == Decimal("-82500.00")
    assert (credit["amount"], credit["currency"]) == (Decimal("82500.00"), "INR")
    assert credit["amount_inr"] == Decimal("82500.00")


def test_channels_and_cash_txn_types(built: dict[str, Any], generation: dict[str, Any]) -> None:
    legs = legs_by_row(reskin(built, generation))
    assert legs[(8, "C")]["channel"] == "CASH"
    assert (legs[(8, "D")]["txn_type"], legs[(8, "C")]["txn_type"]) == ("WITHDRAWAL", "DEPOSIT")
    assert legs[(9, "D")]["channel"] == "RTGS"  # Wire
    assert legs[(1, "D")]["txn_type"] == "TRANSFER"
    assert all(leg["reference_text"] is None for leg in legs.values())


def test_foreign_retention_is_per_pair_and_deterministic(
    built: dict[str, Any], generation: dict[str, Any]
) -> None:
    a, b = reskin(built, generation), reskin(built, generation)
    assert a.equals(b)
    for pct, expected in ((0, {"INR"}), (100, {"USD", "INR", "EUR"})):
        currencies = {
            leg["currency"] for leg in reskin(built, generation, retain_foreign_pct=pct).to_pylist()
        }
        assert currencies <= expected
    retained = legs_by_row(reskin(built, generation, retain_foreign_pct=100))
    assert retained[(1, "D")]["currency"] == retained[(1, "C")]["currency"] == "USD"


def test_rounding_follows_the_configured_mode(generation: dict[str, Any]) -> None:
    cfg = ReskinConfig.from_config(generation)
    assert cfg.money(Decimal("0.005")) == Decimal("0.01")  # ROUND_HALF_UP
    even = ReskinConfig.from_config(
        deep_merge(generation, {"fx": {"rounding_mode": "ROUND_HALF_EVEN"}})
    )
    assert even.money(Decimal("0.005")) == Decimal("0.00")


def test_unknown_currency_fails(built: dict[str, Any], generation: dict[str, Any]) -> None:
    rates = dict(generation["fx"]["rates_to_inr"])
    del rates["US Dollar"]
    config = deep_merge(generation, {})
    config["fx"]["rates_to_inr"] = rates
    with pytest.raises(ReskinError, match="US Dollar"):
        reskin_pairs(built["subgraph"], config)


def test_unmapped_format_fails_without_a_default(
    built: dict[str, Any], generation: dict[str, Any]
) -> None:
    config = deep_merge(generation, {"channel_default": None})
    del config["channel_map"]["Cash"]
    with pytest.raises(ReskinError, match="'Cash'"):
        reskin_pairs(built["subgraph"], config)


@pytest.mark.parametrize(
    "override",
    [
        {"fx": {"rounding_mode": "HALF_UP"}},
        {"legs": {"use_side_specific_amounts": False}},
        {"legs": {"debit_sign": 1}},
    ],
)
def test_invalid_reskin_settings_fail(generation: dict[str, Any], override: dict[str, Any]) -> None:
    with pytest.raises(ReskinError):
        ReskinConfig.from_config(deep_merge(generation, override))


# --------------------------------------------------------------------------- #
# Identifier allocation
# --------------------------------------------------------------------------- #


def test_pair_ids_are_dense_unique_and_order_independent(generation: dict[str, Any]) -> None:
    pairs = [(f"src:{n}", f"content-{n}") for n in range(1, 30)] + [("inj:1", "injected-a")]
    ids = assign_pair_ids(pairs, generation)
    assert ids == assign_pair_ids(list(reversed(pairs)), generation)
    numbers = sorted(int(p.txn_pair_id.removeprefix("TP-")) for p in ids.values())
    assert numbers == list(range(1, len(pairs) + 1))
    assert len({p.source_ref for p in ids.values()}) == len(pairs)


def test_injected_pairs_are_not_numbered_last(generation: dict[str, Any]) -> None:
    """Numbering by hash, not by arrival, is what hides the injected tail."""
    pairs = [(f"src:{n}", f"content-{n}") for n in range(1, 200)]
    injected = [(f"inj:{n}", f"injected-{n}") for n in range(1, 11)]
    ids = assign_pair_ids(pairs + injected, generation)
    positions = sorted(int(ids[ref].txn_pair_id.removeprefix("TP-")) for ref, _ in injected)
    assert positions[0] < len(pairs)


def test_source_ref_carries_no_provenance(generation: dict[str, Any]) -> None:
    ids = assign_pair_ids([("src:7", "a"), ("inj:1", "b")], generation)
    prefix = generation["legs"]["source_ref_prefix"]
    width = generation["legs"]["source_ref_hex_digits"]
    for pair in ids.values():
        assert pair.source_ref.startswith(prefix)
        body = pair.source_ref.removeprefix(prefix)
        assert len(body) == width and all(c in "0123456789ABCDEF" for c in body)
        assert "src" not in pair.source_ref.lower() and "inj" not in pair.source_ref.lower()
    assert ids["src:7"].leg_id("D").endswith("-D")


def test_a_ref_with_two_order_keys_fails(generation: dict[str, Any]) -> None:
    with pytest.raises(ReskinError, match="two different order keys"):
        assign_pair_ids([("src:1", "a"), ("src:1", "b")], generation)


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #


def test_captured_attempts_are_attributed(
    built: dict[str, Any], generation: dict[str, Any], detection: dict[str, Any]
) -> None:
    labels = legs_by_row(
        label_source_legs(
            built["subgraph"], built["patterns"], built["location"], generation, detection
        )
    )
    for n in (1, 3, 4):
        for side in ("D", "C"):
            row = labels[(n, side)]
            assert (row["attempt_id"], row["amlworld_pattern"]) == (1, "FAN-OUT")
            assert row["typology_id"] == "TYP_AML_FAN_OUT"
            assert row["match_confidence"] == Decimal("1.0000")
            assert row["is_illicit"] is True
    assert labels[(9, "C")]["typology_id"] == "TYP_AML_CYCLE"


def test_unattributed_illicit_keeps_its_label_without_a_typology(
    built: dict[str, Any], generation: dict[str, Any], detection: dict[str, Any]
) -> None:
    labels = legs_by_row(
        label_source_legs(
            built["subgraph"], built["patterns"], built["location"], generation, detection
        )
    )
    row = labels[(11, "D")]
    assert row["is_illicit"] is True
    assert row["typology_id"] is None and row["attempt_id"] is None
    assert row["match_confidence"] is None


def test_legit_legs_carry_no_attribution(
    built: dict[str, Any], generation: dict[str, Any], detection: dict[str, Any]
) -> None:
    labels = label_source_legs(
        built["subgraph"], built["patterns"], built["location"], generation, detection
    )
    assert labels.num_rows == 2 * built["subgraph"].pairs.num_rows
    for row in labels.to_pylist():
        assert row["injection_method"] == generation["labels"]["source_injection_method"]
        assert row["pair_ref"] == source_pair_ref(row[SRC_ROW_NUMBER])
        if not row["is_illicit"]:
            assert row["attempt_id"] is None and row["typology_id"] is None


def test_unknown_pattern_fails(
    built: dict[str, Any], generation: dict[str, Any], detection: dict[str, Any]
) -> None:
    pruned = {
        **detection,
        "typologies": [t for t in detection["typologies"] if t["amlworld_pattern"] != "CYCLE"],
    }
    with pytest.raises(LabelError, match="CYCLE"):
        label_source_legs(
            built["subgraph"], built["patterns"], built["location"], generation, pruned
        )


def test_confidence_is_one_over_the_match_count() -> None:
    assert confidence(1) == Decimal("1.0000")
    assert confidence(3) == Decimal("0.3333")
    with pytest.raises(LabelError):
        confidence(0)


def test_raw_patterns_keep_every_attempt_verbatim(built: dict[str, Any]) -> None:
    table = raw_laundering_patterns(built["patterns"]).to_pylist()
    assert [r["attempt_id"] for r in table] == [1, 2, 3]
    stack = table[1]
    assert (stack["typology"], stack["max_detail"], stack["txn_count"]) == ("STACK", None, 2)
    lines = json.loads(stack["txn_data"])
    assert lines[1].startswith("2022/09/02 12:29,020,8000A0022")  # padded, as in the file
