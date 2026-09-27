"""Tests for deterministic identity derivation.

These pin down the fix for GAP-02. The properties that make referential
integrity hold by construction rather than by convention:

* the same source key always yields the same surrogate id, across processes and
  across sample sizes;
* distinct source keys yield distinct ids -- in particular, an account number
  that recurs across banks must not collapse into one account, which would
  corrupt the counterparty graph;
* derived attribute streams are independent, so risk rating and branch region do
  not move together despite descending from one seed.
"""

from __future__ import annotations

import hashlib

import pytest

from risk_copilot.ids import (
    ACCOUNT_ID_WIDTH,
    PARTY_ID_WIDTH,
    account_id,
    case_id,
    derived_rng,
    is_account_id,
    is_party_id,
    normalise_bank_id,
    party_id,
    signal_business_key,
    signal_id,
    stable_hash,
    txn_leg_id,
    txn_pair_id,
    weighted_choice,
)

pytestmark = pytest.mark.unit

SEED = 20260922


# --------------------------------------------------------------------------- #
# stable_hash
# --------------------------------------------------------------------------- #


def test_stable_hash_is_reproducible() -> None:
    # Not Python's hash(), which is salted per process and therefore useless here.
    assert stable_hash("abc", 1) == stable_hash("abc", 1)


def test_namespaces_give_uncorrelated_streams() -> None:
    assert stable_hash("abc", namespace="party") != stable_hash("abc", namespace="account")


def test_part_boundaries_are_unambiguous() -> None:
    # Joining on a separator that cannot occur in the source keys prevents
    # ("a","bc") colliding with ("ab","c").
    assert stable_hash("a", "bc") != stable_hash("ab", "c")


# --------------------------------------------------------------------------- #
# party_id
# --------------------------------------------------------------------------- #


def test_party_id_is_deterministic_and_well_formed() -> None:
    first = party_id("80062E240", seed=SEED)
    assert first == party_id("80062E240", seed=SEED)
    assert is_party_id(first)


def test_party_id_ignores_case_and_surrounding_whitespace() -> None:
    # Source CSVs are inconsistent about both; treating them as distinct entities
    # would split one party in two.
    assert party_id("80062e240", seed=SEED) == party_id("  80062E240  ", seed=SEED)


def test_party_id_changes_with_the_seed() -> None:
    assert party_id("80062E240", seed=SEED) != party_id("80062E240", seed=SEED + 1)


def test_distinct_entities_get_distinct_party_ids() -> None:
    # The demo-scale sample must be collision-free outright. A collision merges
    # two real entities and corrupts every edge between them, so this is a
    # correctness property rather than a quality-of-implementation nicety.
    ids = {party_id(f"80{index:07X}", seed=SEED) for index in range(5000)}
    assert len(ids) == 5000


def test_id_width_has_headroom_for_the_full_amlworld_population() -> None:
    # Regression guard on PARTY_ID_WIDTH. Expected collisions are ~n^2/2m, so a
    # narrower width silently reintroduces merging at full scale: width 7 would
    # produce ~24,500 collisions across AMLworld's ~700K entities.
    population = 700_000
    modulus = 10**PARTY_ID_WIDTH
    expected_collisions = population**2 / (2 * modulus)
    assert expected_collisions < 1.0, (
        f"PARTY_ID_WIDTH={PARTY_ID_WIDTH} gives ~{expected_collisions:.1f} expected "
        f"collisions at {population:,} entities"
    )


def test_account_id_width_has_headroom_for_the_account_population() -> None:
    modulus = 10**ACCOUNT_ID_WIDTH
    assert 518_581**2 / (2 * modulus) < 1.0


@pytest.mark.parametrize("value", ["", "   "])
def test_party_id_rejects_an_empty_entity_id(value: str) -> None:
    with pytest.raises(ValueError, match="entity_id"):
        party_id(value, seed=SEED)


# --------------------------------------------------------------------------- #
# account_id
# --------------------------------------------------------------------------- #


def test_account_id_is_deterministic_and_well_formed() -> None:
    first = account_id("331579", "80B779D80", seed=SEED)
    assert first == account_id("331579", "80B779D80", seed=SEED)
    assert is_account_id(first)


def test_same_account_number_at_different_banks_stays_distinct() -> None:
    # The whole reason the derivation is composite. AMLworld account numbers are
    # unique only within a bank; deriving from the number alone would merge two
    # real accounts and silently corrupt every counterparty edge between them.
    assert account_id("331579", "80B779D80", seed=SEED) != account_id("210", "80B779D80", seed=SEED)


def test_account_id_rejects_an_empty_account_number() -> None:
    with pytest.raises(ValueError, match="account_number"):
        account_id("331579", "", seed=SEED)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("010", "10"), ("021174", "21174"), ("10", "10"), ("0", "0"), ("000", "0"), (" 07 ", "7")],
)
def test_normalise_bank_id_strips_zero_padding(raw: str, expected: str) -> None:
    assert normalise_bank_id(raw) == expected


def test_normalise_bank_id_leaves_non_numeric_keys_intact() -> None:
    # The internal cash-desk bank id must never normalise into a real bank.
    assert normalise_bank_id("internal") == "INTERNAL"
    assert normalise_bank_id("0INTERNAL") == "0INTERNAL"


def test_padded_and_unpadded_bank_ids_derive_the_same_account() -> None:
    # The measured defect: the ledger writes "010", the accounts file writes
    # "10". Unnormalised, 0 of 300,000 ledger rows joined to an account.
    assert account_id("010", "8000EBD30", seed=SEED) == account_id("10", "8000EBD30", seed=SEED)
    assert account_id("021174", "800737690", seed=SEED) == account_id(
        "21174", "800737690", seed=SEED
    )


# --------------------------------------------------------------------------- #
# Transaction ids
# --------------------------------------------------------------------------- #


def test_pair_id_is_zero_padded_and_ordered() -> None:
    assert txn_pair_id(1) < txn_pair_id(2) < txn_pair_id(10)


def test_legs_of_one_pair_share_the_pair_sequence() -> None:
    pair = txn_pair_id(42)
    debit, credit = txn_leg_id(pair, "D"), txn_leg_id(pair, "C")
    assert debit != credit
    assert debit.endswith("-D")
    assert credit.endswith("-C")
    assert "000000000042" in debit


def test_leg_side_is_normalised() -> None:
    pair = txn_pair_id(7)
    assert txn_leg_id(pair, "d") == txn_leg_id(pair, " D ")


def test_invalid_leg_side_is_rejected() -> None:
    with pytest.raises(ValueError, match="side must be"):
        txn_leg_id(txn_pair_id(1), "X")


def test_negative_pair_sequence_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        txn_pair_id(-1)


# --------------------------------------------------------------------------- #
# signal_business_key -- the idempotency mechanism (GAP-11)
# --------------------------------------------------------------------------- #


def test_business_key_is_stable_for_the_same_logical_signal() -> None:
    # A dynamic table recomputes its full result set every refresh, so the same
    # signal reappears. This key is what lets the router MERGE instead of
    # inserting a duplicate case every five minutes.
    args = ("STRUCTURING_CROSS_ACCOUNT", "P-0000001", "2022-09")
    assert signal_business_key(*args) == signal_business_key(*args)


def test_business_key_separates_grains() -> None:
    party_grain = signal_business_key("RULE", "P-1", "2022-09")
    account_grain = signal_business_key("RULE", "P-1", "2022-09", account_id_value="A-1")
    assert party_grain != account_grain


def test_business_key_separates_windows() -> None:
    # Keyed on the observation window rather than wall-clock time, which is what
    # keeps it stable across refreshes.
    assert signal_business_key("R", "P-1", "2022-09") != signal_business_key("R", "P-1", "2022-10")


@pytest.mark.parametrize(
    ("rule", "party", "window"),
    [("", "P-1", "2022-09"), ("R", "", "2022-09"), ("R", "P-1", "")],
)
def test_business_key_requires_every_component(rule: str, party: str, window: str) -> None:
    with pytest.raises(ValueError):
        signal_business_key(rule, party, window)


def test_routed_ids_are_the_sha256_prefix_sql_computes() -> None:
    # ROUTE_SIGNALS derives the same strings in SQL; the live proof (M31)
    # compares the two on every routed row. These pin the Python side.
    key = "STRUCTURING_CROSS_ACCOUNT|P-000000000001|2022-08-26"
    assert signal_id(key) == "SIG-" + hashlib.sha256(key.encode()).hexdigest()[:16]
    assert case_id("P-000000000001", key) == (
        "CASE-" + hashlib.sha256(f"P-000000000001|{key}".encode()).hexdigest()[:16]
    )
    assert signal_id(key) != signal_id(key.replace("08-26", "09-25"))
    assert case_id("P-1", key) != case_id("P-2", key)


@pytest.mark.parametrize("bad", ["", "  "])
def test_routed_ids_require_their_inputs(bad: str) -> None:
    with pytest.raises(ValueError):
        signal_id(bad)
    with pytest.raises(ValueError):
        case_id(bad, "K")
    with pytest.raises(ValueError):
        case_id("P-1", bad)


# --------------------------------------------------------------------------- #
# Derived randomness
# --------------------------------------------------------------------------- #


def test_derived_rng_is_reproducible_per_entity() -> None:
    a = derived_rng("P-1", seed=SEED, namespace="income")
    b = derived_rng("P-1", seed=SEED, namespace="income")
    assert [a.random() for _ in range(5)] == [b.random() for _ in range(5)]


def test_derived_rng_streams_are_independent_across_namespaces() -> None:
    # Why attributes are drawn per namespace: otherwise risk rating and branch
    # region would be correlated purely as an artefact of generation.
    income = derived_rng("P-1", seed=SEED, namespace="income")
    region = derived_rng("P-1", seed=SEED, namespace="region")
    assert income.random() != region.random()


def test_entity_attributes_do_not_depend_on_generation_order() -> None:
    # The per-entity generator pattern: a party's attributes must be identical
    # whether 500 or 5,000 parties are generated.
    def draw(entity: str) -> float:
        return derived_rng(entity, seed=SEED, namespace="income").random()

    forward = {e: draw(e) for e in ["P-1", "P-2", "P-3"]}
    backward = {e: draw(e) for e in ["P-3", "P-2", "P-1"]}
    assert forward == backward


def test_weighted_choice_respects_the_configured_distribution() -> None:
    rng = derived_rng("test", seed=SEED, namespace="choice")
    options = [("A", 0.9), ("B", 0.1)]
    counts = {"A": 0, "B": 0}
    for _ in range(2000):
        counts[weighted_choice(rng, options)] += 1
    assert 0.85 < counts["A"] / 2000 < 0.95


def test_weighted_choice_rejects_degenerate_input() -> None:
    rng = derived_rng("t", seed=SEED)
    with pytest.raises(ValueError, match="must not be empty"):
        weighted_choice(rng, [])
    with pytest.raises(ValueError, match="positive"):
        weighted_choice(rng, [("A", 0.0)])
