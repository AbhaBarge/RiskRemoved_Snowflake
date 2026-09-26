"""Deterministic subgraph sampling (Phase 2, step 3).

The fixture's topology, traced by hand (entity letters as in the fixture MANIFEST):

    row  pair                         note
      1  E1a -> E3   ACH  illicit      FAN-OUT attempt 1
      2  E1a -> E1a  Reinvestment      self pair
      3  E1a -> E6a  ACH  illicit      attempt 1
      4  E1a -> E9a  ACH  illicit      attempt 1
      5  E5  -> E3   Cheque            Country: excluded (EXTERNAL)
      6  E7  -> E2b  ACH  illicit      STACK attempt 2 -- dropped with row 7
      7  E2c -> E4b  Bitcoin illicit   attempt 2: excluded format and currency
      8  E2a -> E3   Cash
      9  E4a -> E6a  Wire illicit      CYCLE attempt 3, cross-currency
     10  E6a -> E4a  ACH  illicit      attempt 3
     11  E9a -> E7   ACH  illicit      unattributed
     12  E8  -> E1b  Credit Card       Direct: excluded
     13  E6b -> E5   Bitcoin           excluded three ways
     14  E9b -> E6b  Cheque            bank 70's 8000A0070 (not E7's)
     15  EAa -> E3   Cheque            clean host
     16  EAb -> E7   Credit Card       clean host

EA (Sole Proprietorship #10, two accounts, legit only) is the only entity that
qualifies as a structuring host: E2, E4 and E6 are individuals with an illicit
history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from risk_copilot.config import deep_merge, load_settings
from risk_copilot.generation.graph import (
    SamplingError,
    Subgraph,
    pair_content_key,
    reserved_injection_legs,
    sample_subgraph,
)
from risk_copilot.generation.patterns import load_patterns, locate_attempts
from risk_copilot.generation.source import SRC_ROW_NUMBER, load_accounts, load_ledger
from risk_copilot.ids import account_id, party_id

pytestmark = pytest.mark.unit

HOST = "800A0000A"
E = {n: f"800A0000{n}" for n in range(1, 10)}


@pytest.fixture
def base(repo_root: Path) -> dict[str, Any]:
    """Production dev config, resized to the fixture."""
    generation = load_settings(repo_root=repo_root, profile="dev").generation
    return deep_merge(
        generation,
        {
            "subgraph": {"target_parties": 50, "target_accounts": 50, "max_transactions": 1000},
            "injection": {"structuring": {"cohort_parties": 1}},
        },
    )


@pytest.fixture
def source(amlworld_mini: Path, base: dict[str, Any]) -> dict[str, Any]:
    ledger = load_ledger(amlworld_mini, base)
    patterns = load_patterns(amlworld_mini, base)
    return {
        "ledger": ledger,
        "accounts": load_accounts(amlworld_mini, base),
        "patterns": patterns,
        "location": locate_attempts(patterns, ledger),
    }


def run(data: dict[str, Any], generation: dict[str, Any], /, **overrides: Any) -> Subgraph:
    """Sample with ``overrides`` deep-merged over ``generation``.

    Positional-only, because ``source`` is both a fixture name and a config key.
    """
    config = deep_merge(generation, overrides) if overrides else generation
    return sample_subgraph(
        data["ledger"], data["accounts"], data["patterns"], data["location"], config
    )


def rows(sg: Subgraph) -> list[int]:
    return list(sg.pairs[SRC_ROW_NUMBER].to_pylist())


# --------------------------------------------------------------------------- #
# Exclusions
# --------------------------------------------------------------------------- #


def test_exclusions_are_counted_per_reason(source: dict[str, Any], base: dict[str, Any]) -> None:
    stats = run(source, base).stats
    assert stats["unresolved_pairs"] == 0
    assert stats["excluded_pairs_format"] == 2  # rows 7, 13
    assert stats["excluded_pairs_currency"] == 2
    assert stats["excluded_pairs_party_type"] == 3  # rows 5, 12, 13
    assert stats["excluded_pairs"] == 4
    assert stats["eligible_pairs"] == 11  # 16 - 4 excluded - row 6 (attempt 2)


def test_attempt_with_an_excluded_pair_is_dropped_whole(
    source: dict[str, Any], base: dict[str, Any]
) -> None:
    """D9: the Bitcoin row drops STACK attempt 2, and its ACH row 6 with it."""
    for mode in ("injection_hosts", "laundering_patterns"):
        sg = run(source, base, subgraph={"seed_source": mode})
        assert sg.dropped_attempts[2] == "excluded_pair"
        assert 6 not in rows(sg)
        assert 7 not in rows(sg)


def test_no_excluded_party_type_is_ever_sampled(
    source: dict[str, Any], base: dict[str, Any]
) -> None:
    sg = run(source, base, subgraph={"seed_source": "laundering_patterns", "expansion_hops": 5})
    assert "EXTERNAL" not in set(sg.party_types.values())
    assert {E[5], E[8]}.isdisjoint(sg.entities)


# --------------------------------------------------------------------------- #
# Hosts and seeds
# --------------------------------------------------------------------------- #


def test_only_a_clean_multi_account_individual_hosts(
    source: dict[str, Any], base: dict[str, Any]
) -> None:
    sg = run(source, base)
    assert sg.hosts == (HOST,)
    assert sg.stats["qualifying_hosts"] == 1
    assert sg.party_types[HOST] == "INDIVIDUAL"  # Sole Proprietorship (D5)


def test_hosts_are_seeded_with_all_their_accounts(
    source: dict[str, Any], base: dict[str, Any]
) -> None:
    for mode in ("injection_hosts", "laundering_patterns"):
        sg = run(source, base, subgraph={"seed_source": mode})
        assert sg.accounts_by_entity[HOST] == (("20", "8000A0101"), ("21174", "8000A0100"))


def test_too_few_hosts_fails(source: dict[str, Any], base: dict[str, Any]) -> None:
    with pytest.raises(SamplingError, match="Only 1 entities qualify"):
        run(source, base, injection={"structuring": {"cohort_parties": 2}})


def test_injection_hosts_bfs_follows_the_hand_trace(
    source: dict[str, Any], base: dict[str, Any]
) -> None:
    """Hop 1 reaches E3 and E7; hop 2 reaches E1a, E2a and E9a. Attempt 1 is cut."""
    sg = run(source, base)
    assert sg.seed_entities == (HOST,)
    assert set(sg.entities) == {HOST, E[3], E[7], E[1], E[2], E[9]}
    assert sg.stats["hop1_admitted_accounts"] == 2
    assert sg.stats["hop2_admitted_accounts"] == 3
    # Account-level: E1 brings only the account BFS reached, not 21174/8000A0011.
    assert sg.accounts_by_entity[E[1]] == (("10", "8000A0010"),)
    assert sg.dropped_attempts[1] == "partial_capture"  # E6a was never reached
    assert sg.dropped_attempts[3] == "not_sampled"
    assert sg.captured_attempts == ()
    assert rows(sg) == [2, 8, 11, 15, 16]  # rows 1, 4 go with attempt 1


def test_unattributed_illicit_pairs_are_kept(source: dict[str, Any], base: dict[str, Any]) -> None:
    sg = run(source, base)
    by_row = dict(zip(rows(sg), sg.pairs["is_laundering"].to_pylist(), strict=True))
    assert by_row[11] == 1


def test_laundering_patterns_seed_every_surviving_attempt(
    source: dict[str, Any], base: dict[str, Any]
) -> None:
    sg = run(source, base, subgraph={"seed_source": "laundering_patterns", "expansion_hops": 0})
    assert sg.captured_attempts == (1, 3)
    assert set(sg.seed_entities) == {E[1], E[3], E[6], E[9], E[4], HOST}
    assert {1, 3, 4, 9, 10} <= set(rows(sg))


def test_seeds_over_budget_fail_rather_than_subsample(
    source: dict[str, Any], base: dict[str, Any]
) -> None:
    with pytest.raises(SamplingError, match="Seeds are never subsampled"):
        run(source, base, subgraph={"seed_source": "laundering_patterns", "target_parties": 3})
    with pytest.raises(SamplingError, match="Seeds are never subsampled"):
        run(source, base, subgraph={"seed_source": "laundering_patterns", "target_accounts": 4})


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #


def test_account_budget_is_hard(source: dict[str, Any], base: dict[str, Any]) -> None:
    sg = run(source, base, subgraph={"target_accounts": 4})
    assert sg.stats["accounts"] == 4
    assert sg.stats["bfs_skipped_over_budget"] > 0


def test_party_budget_is_hard(source: dict[str, Any], base: dict[str, Any]) -> None:
    sg = run(source, base, subgraph={"target_parties": 3})
    assert len(sg.entities) == 3


def test_neighbour_cap_limits_each_frontier_account(
    source: dict[str, Any], base: dict[str, Any]
) -> None:
    """Hop 2's frontier is E3's and E7's accounts; E3's has two new neighbours."""
    sg = run(source, base, subgraph={"max_neighbours_per_account": 1})
    assert sg.stats["hop2_admitted_accounts"] == 2


def test_leg_budget_reserves_the_worst_case_injection(base: dict[str, Any]) -> None:
    cohort = base["injection"]["structuring"]
    expected = (
        cohort["cohort_parties"]
        * cohort["accounts_per_party_max"]
        * cohort["deposits_per_account_max"]
        * 2
    )
    assert reserved_injection_legs(base) == expected


def test_leg_cut_keeps_attempts_and_host_traffic(
    source: dict[str, Any], base: dict[str, Any]
) -> None:
    reserve = reserved_injection_legs(base)
    full = run(source, base, subgraph={"seed_source": "laundering_patterns"})
    protected = {1, 3, 4, 9, 10, 15, 16}
    assert protected < set(rows(full))
    cut = run(
        source,
        base,
        subgraph={
            "seed_source": "laundering_patterns",
            "max_transactions": reserve + 2 * (len(protected) + 1),
        },
    )
    assert set(rows(cut)) >= protected
    assert len(rows(cut)) == len(protected) + 1
    assert cut.stats["legs"] + reserve <= reserve + 2 * (len(protected) + 1)


def test_protected_pairs_over_the_leg_budget_fail(
    source: dict[str, Any], base: dict[str, Any]
) -> None:
    reserve = reserved_injection_legs(base)
    with pytest.raises(SamplingError, match="Protected pairs"):
        run(
            source,
            base,
            subgraph={"seed_source": "laundering_patterns", "max_transactions": reserve + 2},
        )


def test_reserve_larger_than_the_budget_fails(source: dict[str, Any], base: dict[str, Any]) -> None:
    with pytest.raises(SamplingError, match="does not cover"):
        run(source, base, subgraph={"max_transactions": reserved_injection_legs(base)})


def test_unimplemented_cohort_fails_before_sampling(base: dict[str, Any]) -> None:
    config = deep_merge(base, {"injection": {"rapid_credit_velocity": {"enabled": True}}})
    with pytest.raises(SamplingError, match="not implemented"):
        reserved_injection_legs(config)


# --------------------------------------------------------------------------- #
# Configuration contract
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"subgraph": {"seed_source": "everything"}}, "seed_source"),
        ({"subgraph": {"partial_attempts": "keep"}}, "partial_attempts"),
        ({"source": {"exclusion_policy": "leg_only"}}, "exclusion_policy"),
        ({"subgraph": {"require_both_legs_resolvable": False}}, "only true is implemented"),
    ],
)
def test_unsupported_settings_fail(
    source: dict[str, Any], base: dict[str, Any], override: dict[str, Any], message: str
) -> None:
    with pytest.raises(SamplingError, match=message):
        run(source, base, **override)


# --------------------------------------------------------------------------- #
# Identity and determinism
# --------------------------------------------------------------------------- #


def test_id_maps_derive_through_ids(source: dict[str, Any], base: dict[str, Any]) -> None:
    sg = run(source, base)
    seed = base["seed"]
    assert sg.party_ids == {e: party_id(e, seed=seed) for e in sg.entities}
    assert sg.account_ids[("10", "8000A0010")] == account_id("010", "8000A0010", seed=seed)
    assert len(set(sg.account_ids.values())) == len(sg.account_ids)


def test_same_inputs_give_the_same_sample(source: dict[str, Any], base: dict[str, Any]) -> None:
    a, b = run(source, base), run(source, base)
    assert a.entities == b.entities
    assert a.accounts_by_entity == b.accounts_by_entity
    assert a.pairs.equals(b.pairs)
    assert a.stats == b.stats


def test_shuffled_ledger_samples_the_same_transfers(
    amlworld_mini: Path, source: dict[str, Any], base: dict[str, Any]
) -> None:
    """No decision depends on row order: src_row_number moves, content does not."""
    ledger = source["ledger"]
    order = np.random.default_rng(7).permutation(ledger.num_rows)
    shuffled = ledger.take(pa.array(order)).drop_columns([SRC_ROW_NUMBER])
    shuffled = shuffled.add_column(0, SRC_ROW_NUMBER, pa.array(range(1, ledger.num_rows + 1)))
    reordered = {
        **source,
        "ledger": shuffled,
        "location": locate_attempts(source["patterns"], shuffled),
    }
    for mode in ("injection_hosts", "laundering_patterns"):
        original = run(source, base, subgraph={"seed_source": mode})
        again = run(reordered, base, subgraph={"seed_source": mode})
        assert again.entities == original.entities
        assert again.accounts_by_entity == original.accounts_by_entity
        content = sorted(pair_content_key(r) for r in original.pairs.to_pylist())
        assert sorted(pair_content_key(r) for r in again.pairs.to_pylist()) == content


def test_a_different_seed_changes_the_ids(source: dict[str, Any], base: dict[str, Any]) -> None:
    a = run(source, base)
    b = run(source, deep_merge(base, {"seed": base["seed"] + 1}))
    assert a.entities == b.entities  # one qualifying host: the graph cannot move
    assert set(a.party_ids.values()).isdisjoint(b.party_ids.values())
