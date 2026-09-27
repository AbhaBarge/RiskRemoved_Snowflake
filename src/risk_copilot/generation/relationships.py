"""Attribute-based relationship edges -- GAP-05, plan step 5.

Three edge types go to ``RAW_PARTY_RELATIONSHIP_ATTR``. FUND_FLOW is never
generated: it is derived from transactions downstream (``DT_PARTY_RELATIONSHIP``),
so every flow edge is backed by legs.

* **SHARED_PHONE.** ``shared_phone_pct`` of parties are paired in hash order;
  the second of each pair takes the first's number.
* **BENEFICIAL_OWNER.** ``beneficial_owner_pct`` of legal persons get an
  individual owner from the sample, chosen by hash.
* **SHARED_DEVICE (GAP-05).** ``SHARED_DEVICE_CLUSTER`` needs a shared device
  *and* flow between the accounts, so random assignment finds nothing.
  ``colocate_with_fund_flow_pct`` of shared-device accounts are grown along real
  flow edges (account pairs with at least ``fund_flow_min_txn_count`` pairs
  between them). The rest are **decoys** whose parties never transact with each
  other: the false-positive control the rule must not fire on. Both kinds span
  at least ``cluster_min_distinct_parties`` parties.

Every choice ranks keys by ``stable_hash``; nothing depends on iteration order.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from risk_copilot.generation import GenerationError
from risk_copilot.generation.entities import Entities, device_fingerprint
from risk_copilot.ids import derived_rng, stable_hash

RELATIONSHIP_SCHEMA = pa.schema(
    [
        ("relationship_id", pa.string()),
        ("party_a_id", pa.string()),
        ("party_b_id", pa.string()),
        ("relationship_type", pa.string()),
        ("shared_value", pa.string()),
        ("first_seen", pa.timestamp("s")),
        ("last_seen", pa.timestamp("s")),
    ]
)


class RelationshipError(GenerationError):
    """Relationship edges cannot be built under the configured contract."""


@dataclass(frozen=True)
class Relationships:
    kyc: pa.Table  # phone numbers updated for shared phones
    accounts: pa.Table  # device fingerprints updated for clusters
    edges: pa.Table
    #: Each cluster's accounts, sorted: co-located (flow-backed) and decoy.
    colocated_clusters: tuple[tuple[str, ...], ...]
    decoy_clusters: tuple[tuple[str, ...], ...]
    stats: Mapping[str, int]


def _ranked(items: Iterable[str], seed: int, namespace: str) -> list[str]:
    return sorted(items, key=lambda k: (stable_hash(k, seed, namespace=namespace), k))


def _edge_id(prefix: str, kind: str, a: str, b: str, value: str | None, seed: int) -> str:
    return f"{prefix}{stable_hash(kind, a, b, value or '', seed, namespace='relationship'):016X}"


def build_relationships(
    entities: Entities, legs: pa.Table, generation: Mapping[str, Any]
) -> Relationships:
    """Build the three attribute edge types and apply them to KYC and accounts."""
    try:
        seed = int(generation["seed"])
        ent = generation["entities"]
        rel = ent["relationships"]
        dev = ent["device"]
        prefix = str(rel["relationship_id_prefix"])
        min_txn = int(rel["fund_flow_min_txn_count"])
        size_min, size_max = int(dev["cluster_size_min"]), int(dev["cluster_size_max"])
        min_parties = int(dev["cluster_min_distinct_parties"])
    except KeyError as exc:
        raise RelationshipError(f"conf/generation.yml: missing relationship setting {exc}") from exc
    if not 2 <= min_parties <= size_min <= size_max:
        raise RelationshipError(
            "entities.device: need 2 <= cluster_min_distinct_parties <= cluster_size_min "
            "<= cluster_size_max"
        )

    kyc = entities.kyc.to_pylist()
    accounts = entities.accounts.to_pylist()
    ptype = dict(
        zip(
            entities.cbs["party_id"].to_pylist(),
            entities.cbs["party_type"].to_pylist(),
            strict=True,
        )
    )
    owner = {a["account_id"]: a["party_id"] for a in accounts}

    # ---- when each party was seen transacting ------------------------------ #
    seen: dict[str, list[dt.datetime]] = defaultdict(list)
    pair_accounts: dict[str, list[str]] = defaultdict(list)
    for leg in legs.select(["pair_ref", "account_id", "txn_timestamp"]).to_pylist():
        # The cash desk's side of a cash leg (D27) belongs to no customer. Skipping
        # it also keeps the desk out of the flow graph below: otherwise it would be
        # a hub joining every cash customer (D4).
        if leg["account_id"] not in owner:
            continue
        seen[owner[leg["account_id"]]].append(leg["txn_timestamp"])
        pair_accounts[leg["pair_ref"]].append(leg["account_id"])

    def span(parties: Iterable[str]) -> tuple[dt.datetime | None, dt.datetime | None]:
        stamps = [t for p in parties for t in seen.get(p, ())]
        return (min(stamps), max(stamps)) if stamps else (None, None)

    edges: list[dict[str, Any]] = []

    def add_edge(kind: str, a: str, b: str, value: str | None, *, ordered: bool = False) -> None:
        if not ordered:
            a, b = min(a, b), max(a, b)
        first, last = span((a, b))
        edges.append(
            {
                "relationship_id": _edge_id(prefix, kind, a, b, value, seed),
                "party_a_id": a,
                "party_b_id": b,
                "relationship_type": kind,
                "shared_value": value,
                "first_seen": first,
                "last_seen": last,
            }
        )

    # ---- SHARED_PHONE ------------------------------------------------------- #
    by_party = {r["party_id"]: r for r in kyc}
    parties = _ranked(by_party, seed, "shared-phone")
    pair_count = int(len(parties) * float(rel["shared_phone_pct"]) / 100) // 2
    for i in range(pair_count):
        a, b = parties[2 * i], parties[2 * i + 1]
        by_party[b]["phone_number"] = by_party[a]["phone_number"]
        add_edge("SHARED_PHONE", a, b, by_party[a]["phone_number"])

    # ---- BENEFICIAL_OWNER --------------------------------------------------- #
    legal = _ranked((p for p, t in ptype.items() if t == "LEGAL_PERSON"), seed, "bo-legal")
    people = sorted(p for p, t in ptype.items() if t == "INDIVIDUAL")
    owned = int(len(legal) * float(rel["beneficial_owner_pct"]) / 100)
    if owned and not people:
        raise RelationshipError("beneficial_owner_pct > 0 but the sample has no individuals")
    for company in legal[:owned]:
        person = people[stable_hash(company, seed, namespace="bo-owner") % len(people)]
        add_edge("BENEFICIAL_OWNER", company, person, None, ordered=True)

    # ---- SHARED_DEVICE ------------------------------------------------------ #
    flow_count: dict[tuple[str, str], int] = defaultdict(int)
    party_flow: set[tuple[str, str]] = set()
    for members in pair_accounts.values():
        if len(members) != 2 or members[0] == members[1]:
            continue
        a, b = sorted(members)
        flow_count[(a, b)] += 1
        pa_, pb_ = sorted((owner[a], owner[b]))
        party_flow.add((pa_, pb_))
    flow_adj: dict[str, set[str]] = defaultdict(set)
    for (a, b), n in flow_count.items():
        if n >= min_txn and owner[a] != owner[b]:
            flow_adj[a].add(b)
            flow_adj[b].add(a)

    target = int(len(accounts) * float(dev["shared_fingerprint_pct"]) / 100)
    colocated_target = int(target * float(dev["colocate_with_fund_flow_pct"]) / 100)
    used: set[str] = set()
    colocated: list[tuple[str, ...]] = []
    decoys: list[tuple[str, ...]] = []

    def cluster_size(key: str) -> int:
        return derived_rng(key, seed=seed, namespace="cluster-size").randint(size_min, size_max)

    placed = 0
    for start in _ranked(flow_adj, seed, "device-colocate"):
        if placed >= colocated_target:
            break
        if start in used:
            continue
        want = min(cluster_size(start), max(colocated_target - placed, size_min))
        cluster, frontier = [start], [start]
        while frontier and len(cluster) < want:
            grown: list[str] = []
            for acct in frontier:
                for neighbour in _ranked(flow_adj[acct], seed, f"device-grow:{acct}"):
                    if neighbour not in used and neighbour not in cluster and len(cluster) < want:
                        cluster.append(neighbour)
                        grown.append(neighbour)
            frontier = grown
        if len(cluster) >= size_min and len({owner[a] for a in cluster}) >= min_parties:
            colocated.append(tuple(sorted(cluster)))
            used.update(cluster)
            placed += len(cluster)

    decoy_target = target - placed
    decoy_placed = 0
    pool = _ranked((a["account_id"] for a in accounts), seed, "device-decoy")
    for start in pool:
        if decoy_placed + size_min > decoy_target:
            break
        if start in used:
            continue
        want = cluster_size(start)
        cluster = [start]
        for cand in pool:
            if len(cluster) >= want:
                break
            if cand in used or cand in cluster:
                continue
            cand_party = owner[cand]
            if any(
                tuple(sorted((cand_party, owner[m]))) in party_flow
                for m in cluster
                if owner[m] != cand_party
            ):
                continue
            cluster.append(cand)
        if len(cluster) >= size_min and len({owner[a] for a in cluster}) >= min_parties:
            decoys.append(tuple(sorted(cluster)))
            used.update(cluster)
            decoy_placed += len(cluster)

    fingerprint: dict[str, str] = {}
    for group in (*colocated, *decoys):
        # Keyed on the joined member ids, which no single account id can equal.
        value = device_fingerprint("|".join(group), seed=seed, spec=dev)
        members = sorted({owner[a] for a in group})
        for acct in group:
            fingerprint[acct] = value
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                add_edge("SHARED_DEVICE", a, b, value)
    for acct in accounts:
        if acct["account_id"] in fingerprint:
            acct["device_fingerprint"] = fingerprint[acct["account_id"]]

    edges.sort(key=lambda e: e["relationship_id"])
    ids = [e["relationship_id"] for e in edges]
    if len(set(ids)) != len(ids):
        raise RelationshipError("relationship_id collision")
    return Relationships(
        kyc=pa.Table.from_pylist(kyc, schema=entities.kyc.schema),
        accounts=pa.Table.from_pylist(accounts, schema=entities.accounts.schema),
        edges=pa.Table.from_pylist(edges, schema=RELATIONSHIP_SCHEMA),
        colocated_clusters=tuple(colocated),
        decoy_clusters=tuple(decoys),
        stats={
            "shared_phone_edges": pair_count,
            "beneficial_owner_edges": owned,
            "device_clusters_colocated": len(colocated),
            "device_clusters_decoy": len(decoys),
            "device_shared_accounts": placed + decoy_placed,
            "device_shared_target": target,
            "shared_device_edges": sum(
                1 for e in edges if e["relationship_type"] == "SHARED_DEVICE"
            ),
            "distinct_fingerprints": len(
                pc.unique(pa.array([a["device_fingerprint"] for a in accounts]))
            ),
        },
    )
