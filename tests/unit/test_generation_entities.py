"""Entity enrichment and relationship edges (Phase 2, step 5)."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from risk_copilot.config import deep_merge, load_settings
from risk_copilot.generation.entities import (
    EntityError,
    build_entities,
    observed_window,
)
from risk_copilot.generation.graph import Subgraph, sample_subgraph
from risk_copilot.generation.labels import LABEL_SCHEMA, label_source_legs
from risk_copilot.generation.patterns import load_patterns, locate_attempts
from risk_copilot.generation.relationships import RelationshipError, build_relationships
from risk_copilot.generation.reskin import LEG_SCHEMA, reskin_pairs
from risk_copilot.generation.source import load_accounts, load_ledger

pytestmark = pytest.mark.unit

_PHONE_RE = re.compile(r"^\+91[6-9]\d{9}$")
_PAN_RE = re.compile(r"^[A-Z]{3}(?P<holder>[A-Z])[A-Z]\d{4}[A-Z]$")


@pytest.fixture
def config(repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
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
def generation(config: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    return config[0]


@pytest.fixture
def pipeline(amlworld_mini: Path, config: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    generation, detection = config
    ledger = load_ledger(amlworld_mini, generation)
    patterns = load_patterns(amlworld_mini, generation)
    location = locate_attempts(patterns, ledger)
    accounts = load_accounts(amlworld_mini, generation)
    subgraph = sample_subgraph(ledger, accounts, patterns, location, generation)
    legs = reskin_pairs(subgraph, generation)
    labels = label_source_legs(subgraph, patterns, location, generation, detection)
    return {
        "subgraph": subgraph,
        "legs": legs,
        "labels": labels,
        "raw": (ledger, accounts, patterns, location),
    }


def enrich(pipeline: dict[str, Any], generation: dict[str, Any], **overrides: Any) -> Any:
    config = deep_merge(generation, overrides) if overrides else generation
    return build_entities(pipeline["subgraph"], pipeline["legs"], pipeline["labels"], config)


# --------------------------------------------------------------------------- #
# Coverage and identity
# --------------------------------------------------------------------------- #


def test_one_record_per_sampled_party_and_account(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    sg: Subgraph = pipeline["subgraph"]
    ents = enrich(pipeline, generation)
    assert ents.cbs["party_id"].to_pylist() == sorted(sg.party_ids.values())
    assert ents.kyc["party_id"].to_pylist() == sorted(sg.party_ids.values())
    assert ents.accounts["account_id"].to_pylist() == sorted(sg.account_ids.values())
    owner = {
        sg.account_ids[k]: sg.party_ids[e] for e, ks in sg.accounts_by_entity.items() for k in ks
    }
    for row in ents.accounts.to_pylist():
        assert row["party_id"] == owner[row["account_id"]]


def test_no_output_carries_a_label_column(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    forbidden = set(generation["validation"]["label_leakage"]["forbidden_columns"])
    ents = enrich(pipeline, generation)
    rel = build_relationships(ents, pipeline["legs"], generation)
    for table in (ents.cbs, ents.kyc, ents.accounts, rel.kyc, rel.accounts, rel.edges):
        assert not set(table.column_names) & forbidden


def test_attributes_are_stable_across_sample_sizes(
    pipeline: dict[str, Any],
    generation: dict[str, Any],
    config: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """A party's identity must not depend on who else was sampled."""
    ledger, accounts, patterns, location = pipeline["raw"]
    small_cfg = deep_merge(generation, {"subgraph": {"seed_source": "injection_hosts"}})
    small = sample_subgraph(ledger, accounts, patterns, location, small_cfg)
    small_legs = reskin_pairs(small, small_cfg)
    small_labels = label_source_legs(small, patterns, location, small_cfg, config[1])
    a = {r["party_id"]: r for r in enrich(pipeline, generation).cbs.to_pylist()}
    b = {
        r["party_id"]: r
        for r in build_entities(small, small_legs, small_labels, small_cfg).cbs.to_pylist()
    }
    common = set(a) & set(b)
    assert len(common) >= 3
    for pid in common:
        for field in ("full_name", "party_type", "branch_region", "branch_code"):
            assert a[pid][field] == b[pid][field], (pid, field)


def test_same_inputs_give_identical_tables(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    a, b = enrich(pipeline, generation), enrich(pipeline, generation)
    assert a.cbs.equals(b.cbs) and a.kyc.equals(b.kyc) and a.accounts.equals(b.accounts)


# --------------------------------------------------------------------------- #
# Attribute shapes
# --------------------------------------------------------------------------- #


def test_type_specific_fields(pipeline: dict[str, Any], generation: dict[str, Any]) -> None:
    ents = enrich(pipeline, generation)
    kyc = {r["party_id"]: r for r in ents.kyc.to_pylist()}
    holder = generation["entities"]["pan_holder_code"]
    for row in ents.cbs.to_pylist():
        k = kyc[row["party_id"]]
        match = _PAN_RE.match(k["pan_number"])
        assert match and match["holder"] == holder[row["party_type"]]
        if row["party_type"] == "INDIVIDUAL":
            assert row["date_of_birth"] and row["incorporation_date"] is None
            assert k["occupation"] and re.fullmatch(r"\d{4}", k["aadhaar_last4"])
        else:
            assert row["incorporation_date"] and row["date_of_birth"] is None
            assert k["occupation"] is None and k["aadhaar_last4"] is None
        assert _PHONE_RE.match(k["phone_number"])
        assert k["kyc_note"] is None


def test_names_are_latin_script(pipeline: dict[str, Any], generation: dict[str, Any]) -> None:
    """M20: hi_IN produced Devanagari, which breaks screening against a Latin list."""
    for name in enrich(pipeline, generation).cbs["full_name"].to_pylist():
        assert name.isascii(), name


def test_every_record_predates_the_window(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    ents = enrich(pipeline, generation)
    start = ents.window.first_day
    min_age = generation["entities"]["dates"]["min_customer_age_years"]
    since = {}
    for row in ents.cbs.to_pylist():
        assert row["customer_since"] < start
        born = row["date_of_birth"] or row["incorporation_date"]
        assert born < row["customer_since"]
        if row["date_of_birth"]:
            assert (row["customer_since"] - row["date_of_birth"]).days >= min_age * 365
        since[row["party_id"]] = row["customer_since"]
    for row in ents.accounts.to_pylist():
        assert since[row["party_id"]] <= row["opened_date"] < start
    for row in ents.kyc.to_pylist():
        assert since[row["party_id"]] <= row["kyc_last_reviewed"] <= start


def test_accounts_denormalise_the_party_branch(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    ents = enrich(pipeline, generation)
    regions = {r["code"] for r in generation["entities"]["branch_regions"]}
    party = {r["party_id"]: r for r in ents.cbs.to_pylist()}
    credit_types = set(generation["entities"]["credit_limit"]["account_types"])
    for row in ents.accounts.to_pylist():
        assert row["branch_region"] in regions
        assert (row["branch_region"], row["branch_code"]) == (
            party[row["party_id"]]["branch_region"],
            party[row["party_id"]]["branch_code"],
        )
        assert (row["credit_limit"] is not None) == (row["account_type"] in credit_types)
        assert row["current_balance"] >= 0


def test_window_is_measured_from_legitimate_legs_only() -> None:
    """M21: the illicit tail must not stretch the income denominator."""
    stamps = [dt.datetime(2022, 9, 1, 8), dt.datetime(2022, 9, 10, 9), dt.datetime(2022, 9, 18, 7)]
    legs = pa.Table.from_pylist(
        [
            {"pair_ref": f"src:{i}", "leg_side": "C", "txn_timestamp": t}
            for i, t in enumerate(stamps)
        ],
        schema=pa.schema(
            [f for f in LEG_SCHEMA if f.name in ("pair_ref", "leg_side", "txn_timestamp")]
        ),
    )
    labels = pa.Table.from_pylist(
        [{"pair_ref": f"src:{i}", "leg_side": "C", "is_illicit": i == 2} for i in range(3)],
        schema=pa.schema(
            [f for f in LABEL_SCHEMA if f.name in ("pair_ref", "leg_side", "is_illicit")]
        ),
    )
    window = observed_window(legs, labels)
    assert (window.first_day, window.last_day, window.span_days) == (
        dt.date(2022, 9, 1),
        dt.date(2022, 9, 10),
        10,
    )


# --------------------------------------------------------------------------- #
# Declared income (GAP-04)
# --------------------------------------------------------------------------- #


def test_income_is_floored_and_rounded(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    income = generation["entities"]["income"]
    ents = enrich(pipeline, generation)
    for row in ents.kyc.to_pylist():
        value = row["declared_annual_income"]
        assert value >= income["floor_inr"]
        assert value % income["round_to_inr"] == 0


def test_income_tracks_legitimate_inflow(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    """Doubling every legit inflow cannot lower anyone's declared income."""
    ents = enrich(pipeline, generation)
    doubled = pipeline["legs"].set_column(
        pipeline["legs"].schema.get_field_index("amount_inr"),
        "amount_inr",
        pa.array(
            [2 * v for v in pipeline["legs"]["amount_inr"].to_pylist()],
            type=pipeline["legs"].schema.field("amount_inr").type,
        ),
    )
    richer = build_entities(pipeline["subgraph"], doubled, pipeline["labels"], generation)
    before = dict(
        zip(
            ents.kyc["party_id"].to_pylist(),
            ents.kyc["declared_annual_income"].to_pylist(),
            strict=True,
        )
    )
    after = dict(
        zip(
            richer.kyc["party_id"].to_pylist(),
            richer.kyc["declared_annual_income"].to_pylist(),
            strict=True,
        )
    )
    assert all(after[p] >= before[p] for p in before)
    assert any(after[p] > before[p] for p in before)


def test_illicit_parties_under_declare(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    honest = enrich(
        pipeline, generation, entities={"income": {"illicit_cohort_under_declare_factor": 1.0}}
    )
    normal = enrich(pipeline, generation)
    sg: Subgraph = pipeline["subgraph"]
    illicit_accounts = {
        r["account_id"]
        for r, lab in zip(pipeline["legs"].to_pylist(), pipeline["labels"].to_pylist(), strict=True)
        if lab["is_illicit"]
    }
    owner = {
        sg.account_ids[k]: sg.party_ids[e] for e, ks in sg.accounts_by_entity.items() for k in ks
    }
    illicit = {owner[a] for a in illicit_accounts} | {sg.party_ids[h] for h in sg.hosts}
    h = dict(
        zip(
            honest.kyc["party_id"].to_pylist(),
            honest.kyc["declared_annual_income"].to_pylist(),
            strict=True,
        )
    )
    n = dict(
        zip(
            normal.kyc["party_id"].to_pylist(),
            normal.kyc["declared_annual_income"].to_pylist(),
            strict=True,
        )
    )
    for pid in n:
        if pid in illicit:
            assert n[pid] <= h[pid]
        else:
            assert n[pid] == h[pid]
    assert normal.stats["income_under_declared_parties"] == len(illicit)


def test_incoherent_date_config_fails(pipeline: dict[str, Any], generation: dict[str, Any]) -> None:
    with pytest.raises(EntityError, match="minimum tenure"):
        enrich(
            pipeline,
            generation,
            entities={"dates": {"individual_age_years": {"min": 18, "max": 75}}},
        )


# --------------------------------------------------------------------------- #
# Relationships
# --------------------------------------------------------------------------- #


def relate(pipeline: dict[str, Any], generation: dict[str, Any], **overrides: Any) -> Any:
    config = deep_merge(generation, {"entities": overrides}) if overrides else generation
    return build_relationships(enrich(pipeline, generation), pipeline["legs"], config)


def test_shared_phones_copy_the_partner_number(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    rel = relate(pipeline, generation, relationships={"shared_phone_pct": 100})
    phones = dict(
        zip(rel.kyc["party_id"].to_pylist(), rel.kyc["phone_number"].to_pylist(), strict=True)
    )
    edges = [e for e in rel.edges.to_pylist() if e["relationship_type"] == "SHARED_PHONE"]
    assert len(edges) == len(phones) // 2
    for e in edges:
        assert phones[e["party_a_id"]] == phones[e["party_b_id"]] == e["shared_value"]
        assert e["party_a_id"] < e["party_b_id"]


def test_beneficial_owners_are_sampled_individuals(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    rel = relate(pipeline, generation, relationships={"beneficial_owner_pct": 100})
    types = dict(
        zip(
            enrich(pipeline, generation).cbs["party_id"].to_pylist(),
            enrich(pipeline, generation).cbs["party_type"].to_pylist(),
            strict=True,
        )
    )
    edges = [e for e in rel.edges.to_pylist() if e["relationship_type"] == "BENEFICIAL_OWNER"]
    assert len(edges) == sum(1 for t in types.values() if t == "LEGAL_PERSON")
    for e in edges:
        assert (types[e["party_a_id"]], types[e["party_b_id"]]) == ("LEGAL_PERSON", "INDIVIDUAL")


def _flow_pairs(pipeline: dict[str, Any], owner: dict[str, str]) -> set[tuple[str, str]]:
    by_pair: dict[str, list[str]] = {}
    for leg in pipeline["legs"].to_pylist():
        if leg["account_id"] in owner:  # a cash pair's desk side belongs to no customer
            by_pair.setdefault(leg["pair_ref"], []).append(leg["account_id"])
    return {
        tuple(sorted((owner[a], owner[b])))
        for accts in by_pair.values()
        if len(accts) == 2
        for a, b in (accts,)
        if owner[a] != owner[b]
    }


def test_colocated_clusters_follow_real_flow(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    rel = relate(
        pipeline,
        generation,
        device={"shared_fingerprint_pct": 100, "colocate_with_fund_flow_pct": 100},
        relationships={"fund_flow_min_txn_count": 1},
    )
    # The flow graph holds fewer accounts than a 100% target (the fixture's cash
    # pair links a customer only to the desk, D27), so the shortfall is placed as
    # decoys by design. Every co-located cluster must still follow real flow.
    assert rel.colocated_clusters
    owner = dict(
        zip(
            rel.accounts["account_id"].to_pylist(),
            rel.accounts["party_id"].to_pylist(),
            strict=True,
        )
    )
    fingerprints = dict(
        zip(
            rel.accounts["account_id"].to_pylist(),
            rel.accounts["device_fingerprint"].to_pylist(),
            strict=True,
        )
    )
    by_pair: dict[str, list[str]] = {}
    for leg in pipeline["legs"].to_pylist():
        by_pair.setdefault(leg["pair_ref"], []).append(leg["account_id"])
    account_flow = {tuple(sorted(v)) for v in by_pair.values()}
    size_min = generation["entities"]["device"]["cluster_size_min"]
    for cluster in rel.colocated_clusters:
        assert len(cluster) >= size_min
        assert len({owner[a] for a in cluster}) >= 2
        assert len({fingerprints[a] for a in cluster}) == 1
        # Connected through flow edges.
        reached, frontier = {cluster[0]}, [cluster[0]]
        while frontier:
            a = frontier.pop()
            for b in cluster:
                if b not in reached and tuple(sorted((a, b))) in account_flow:
                    reached.add(b)
                    frontier.append(b)
        assert reached == set(cluster)


def test_decoy_clusters_share_a_device_but_never_transact(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    rel = relate(
        pipeline,
        generation,
        device={"shared_fingerprint_pct": 100, "colocate_with_fund_flow_pct": 0},
    )
    assert rel.decoy_clusters and not rel.colocated_clusters
    owner = dict(
        zip(
            rel.accounts["account_id"].to_pylist(),
            rel.accounts["party_id"].to_pylist(),
            strict=True,
        )
    )
    flow = _flow_pairs(pipeline, owner)
    for cluster in rel.decoy_clusters:
        parties = sorted({owner[a] for a in cluster})
        assert len(parties) >= 2
        for i, a in enumerate(parties):
            for b in parties[i + 1 :]:
                assert (a, b) not in flow
    shared = [e for e in rel.edges.to_pylist() if e["relationship_type"] == "SHARED_DEVICE"]
    assert shared


def test_unclustered_accounts_keep_unique_devices(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    rel = relate(pipeline, generation)
    clustered = {a for c in (*rel.colocated_clusters, *rel.decoy_clusters) for a in c}
    solo = [
        r["device_fingerprint"]
        for r in rel.accounts.to_pylist()
        if r["account_id"] not in clustered
    ]
    assert len(solo) == len(set(solo))


def test_relationship_ids_are_unique_and_deterministic(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    kwargs = {"relationships": {"shared_phone_pct": 100, "beneficial_owner_pct": 100}}
    a, b = relate(pipeline, generation, **kwargs), relate(pipeline, generation, **kwargs)
    assert a.edges.equals(b.edges)
    ids = a.edges["relationship_id"].to_pylist()
    assert len(ids) == len(set(ids)) and all(i.startswith("REL-") for i in ids)


def test_single_party_clusters_are_rejected_by_config(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    with pytest.raises(RelationshipError, match="cluster_min_distinct_parties"):
        relate(pipeline, generation, device={"cluster_min_distinct_parties": 1})


def test_income_basis_is_exposed_for_the_calibration_gate(
    pipeline: dict[str, Any], generation: dict[str, Any]
) -> None:
    ents = enrich(pipeline, generation)
    assert all(v > Decimal(0) for v in ents.legit_inflow_inr.values())
    assert set(ents.legit_inflow_inr) <= set(ents.kyc["party_id"].to_pylist())
