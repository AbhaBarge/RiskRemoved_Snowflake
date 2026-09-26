"""Deterministic subgraph sampling -- GAP-02, plan step 3.

AMLworld's graph is authoritative. The sample is a *connected* piece of it, not a
set of independently generated parties that might join, and every decision below
is a pure function of the source files, ``conf/`` and the master seed.

Order of operations:

1. **Exclusions** (``source.exclusion_policy: whole_pair_and_attempt``). A pair is
   excluded if its format or either currency is excluded (Bitcoin, M7), if
   either side belongs to an excluded party type (EXTERNAL, M8), or if either
   side does not resolve to an owning entity (never, measured M1). Any
   laundering attempt containing an excluded pair is dropped whole, with all of
   its pairs (D9).
2. **Hosts.** The structuring cohort's host entities are selected here, because a
   host outside the sample cannot be injected. They are ranked by
   :func:`~risk_copilot.ids.stable_hash` among entities that qualify.
3. **Seeds.** ``laundering_patterns``: every account in a surviving attempt,
   plus every account of every host. ``injection_hosts``: the hosts' accounts
   only. Hosts are seeded whole because the structuring rule is cross-account.
4. **BFS**, ``expansion_hops`` deep along eligible pairs. At each frontier account
   at most ``max_neighbours_per_account`` *new* neighbour accounts are taken,
   ranked by hash. Inclusion is **account-level**: a party carries only its
   sampled accounts, as a bank sees only the accounts it holds. (Party-closed
   inclusion was measured and rejected, M17: demo's laundering seeds alone would
   bring 96,886 accounts.) Candidates are admitted in hash order while the
   account budget -- and, for an account of a new party, the party budget --
   allows; a candidate that would exceed either is skipped.
5. **Pairs** are the eligible pairs with both sides in the sample
   (``require_both_legs_resolvable``). An attempt captured only partly is dropped
   whole (``partial_attempts: drop``). If the legs still exceed
   ``max_transactions`` minus the injection reserve, legitimate pairs are cut in
   hash order. Attempt pairs and pairs touching a host are protected.

No step depends on the ledger's row order. Every ranking hashes stable keys --
entity ids, account keys, pair *content* -- with the key itself as the
tie-break, so a shuffled ledger samples the same customers and the same
transfers.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from risk_copilot.generation import GenerationError
from risk_copilot.generation.patterns import AttemptLocation, Patterns
from risk_copilot.generation.source import (
    ENTITY_PREFIX,
    KEY_SEP,
    LEDGER_COLUMNS,
    SRC_ROW_NUMBER,
    composite_key,
    map_distinct,
    resolve_party_type,
)
from risk_copilot.ids import account_id, party_id, stable_hash

SEED_SOURCES = ("laundering_patterns", "injection_hosts")
EXCLUSION_POLICIES = ("whole_pair_and_attempt",)
PARTIAL_ATTEMPT_POLICIES = ("drop",)

#: Injection cohorts that ``inject`` implements. Enabling any other one fails
#: here, before sampling, rather than after a long build.
IMPLEMENTED_COHORTS = ("structuring",)

AccountKey = tuple[str, str]


class SamplingError(GenerationError):
    """The sample cannot be built within the configured contract."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def _choice(value: Any, allowed: tuple[str, ...], what: str) -> str:
    if value not in allowed:
        raise SamplingError(
            f"conf/generation.yml: {what} must be one of {list(allowed)}, got {value!r}"
        )
    return str(value)


@dataclass(frozen=True)
class SamplingConfig:
    """``subgraph``, ``source`` and ``entities`` settings the sampler consumes."""

    seed: int
    seed_source: str
    expansion_hops: int
    target_parties: int
    target_accounts: int
    max_transactions: int
    max_neighbours_per_account: int
    exclude_payment_formats: tuple[str, ...]
    exclude_currencies: tuple[str, ...]
    party_type_map: Mapping[str, str]
    party_type_default: str | None
    excluded_party_types: tuple[str, ...]

    @classmethod
    def from_config(cls, generation: Mapping[str, Any]) -> SamplingConfig:
        try:
            sub = generation["subgraph"]
            src = generation["source"]
            ent = generation["entities"]
            _choice(src["exclusion_policy"], EXCLUSION_POLICIES, "source.exclusion_policy")
            _choice(sub["partial_attempts"], PARTIAL_ATTEMPT_POLICIES, "subgraph.partial_attempts")
            if sub["require_both_legs_resolvable"] is not True:
                raise SamplingError(
                    "subgraph.require_both_legs_resolvable: only true is implemented; an "
                    "unresolvable counterparty would be an orphan reference in RAW_TRANSACTIONS"
                )
            return cls(
                seed=int(generation["seed"]),
                seed_source=_choice(sub["seed_source"], SEED_SOURCES, "subgraph.seed_source"),
                expansion_hops=int(sub["expansion_hops"]),
                target_parties=int(sub["target_parties"]),
                target_accounts=int(sub["target_accounts"]),
                max_transactions=int(sub["max_transactions"]),
                max_neighbours_per_account=int(sub["max_neighbours_per_account"]),
                exclude_payment_formats=tuple(src.get("exclude_payment_formats") or ()),
                exclude_currencies=tuple(src.get("exclude_currencies") or ()),
                party_type_map=dict(ent["party_type_from_entity_name"]),
                party_type_default=ent.get("party_type_default"),
                excluded_party_types=tuple(ent.get("excluded_party_types") or ()),
            )
        except KeyError as exc:
            raise SamplingError(f"conf/generation.yml: missing sampling setting {exc}") from exc


@dataclass(frozen=True)
class HostCriteria:
    """Which real entities may host the structuring cohort (``injection.structuring``)."""

    enabled: bool
    cohort_parties: int
    party_type: str
    accounts_min: int
    accounts_max: int
    clean_history: bool
    min_ledger_pairs: int

    @classmethod
    def from_config(cls, generation: Mapping[str, Any]) -> HostCriteria:
        spec = (generation.get("injection") or {}).get("structuring") or {}
        if not spec.get("enabled"):
            return cls(False, 0, "", 0, 0, True, 0)
        try:
            return cls(
                enabled=True,
                cohort_parties=int(spec["cohort_parties"]),
                party_type=str(spec["host_party_type"]),
                accounts_min=int(spec["accounts_per_party_min"]),
                accounts_max=int(spec["accounts_per_party_max"]),
                clean_history=bool(spec["host_requires_clean_history"]),
                min_ledger_pairs=int(spec["host_min_ledger_pairs"]),
            )
        except KeyError as exc:
            raise SamplingError(f"injection.structuring: missing setting {exc}") from exc


def reserved_injection_legs(generation: Mapping[str, Any]) -> int:
    """Worst-case legs of every enabled cohort, reserved from ``max_transactions``.

    Reserving the worst case (not the expected value) is what makes the leg
    budget hard: however the per-host draws land, the total cannot exceed it.
    """
    total = 0
    for name, spec in (generation.get("injection") or {}).items():
        if not isinstance(spec, Mapping) or not spec.get("enabled"):
            continue
        if name not in IMPLEMENTED_COHORTS:
            raise SamplingError(
                f"injection.{name} is enabled but not implemented; only "
                f"{list(IMPLEMENTED_COHORTS)} can be generated in this build"
            )
        total += (
            int(spec["cohort_parties"])
            * int(spec["accounts_per_party_max"])
            * int(spec["deposits_per_account_max"])
            * 2
        )
    return total


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Subgraph:
    """The sampled customers, accounts and transfers, plus the evidence for them.

    ``pairs`` holds ledger rows (``src_row_number`` + :data:`LEDGER_COLUMNS`),
    sorted by ``src_row_number``. Every entity is a customer: excluded party
    types never enter.
    """

    entities: tuple[str, ...]
    #: Sampled accounts only, per entity. A host carries all of its accounts.
    accounts_by_entity: Mapping[str, tuple[AccountKey, ...]]
    party_types: Mapping[str, str]
    entity_names: Mapping[str, str]
    pairs: pa.Table
    hosts: tuple[str, ...]
    seed_entities: tuple[str, ...]
    captured_attempts: tuple[int, ...]
    #: attempt_id -> why it is absent: ``excluded_pair`` (D9), ``unlocated``,
    #: ``partial_capture`` or ``not_sampled``.
    dropped_attempts: Mapping[int, str]
    party_ids: Mapping[str, str]
    account_ids: Mapping[AccountKey, str]
    stats: Mapping[str, int] = field(default_factory=dict)

    @property
    def accounts(self) -> tuple[AccountKey, ...]:
        return tuple(sorted(k for keys in self.accounts_by_entity.values() for k in keys))

    def owner_of(self) -> dict[AccountKey, str]:
        """Account key -> owning entity, over the sample."""
        return {k: e for e, keys in self.accounts_by_entity.items() for k in keys}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _split(key: str) -> AccountKey:
    bank, _, number = key.partition(KEY_SEP)
    return (bank, number)


def _join(key: AccountKey) -> str:
    return f"{key[0]}{KEY_SEP}{key[1]}"


def _rank(items: Iterable[str], *, seed: int, namespace: str) -> list[str]:
    """Sort by stable hash, the item itself breaking ties: order-independent."""
    return sorted(items, key=lambda item: (stable_hash(item, seed, namespace=namespace), item))


def _rank_neighbours(account: str, found: Iterable[str], cfg: SamplingConfig) -> list[str]:
    """A frontier account's new neighbours in hash order, keyed on the edge.

    Keying on ``(account, neighbour)`` rather than the neighbour alone means two
    hubs sharing a neighbour rank it independently.
    """

    def key(neighbour: str) -> tuple[int, str]:
        return (stable_hash(account, neighbour, cfg.seed, namespace="bfs-neighbour"), neighbour)

    return sorted(found, key=key)


def _is_in(values: Any, members: Iterable[Any], type_: pa.DataType) -> Any:
    return pc.fill_null(pc.is_in(values, value_set=pa.array(sorted(members), type=type_)), False)


def pair_content_key(row: Mapping[str, Any]) -> str:
    """A pair's content as text -- the rank key for the leg cut.

    Content rather than ``src_row_number``, so the cut does not move when the
    ledger's rows are reordered. Identical duplicate rows share a key; which of
    two identical transfers is kept cannot change the output.
    """
    return KEY_SEP.join(str(row[col]) for col in LEDGER_COLUMNS[:-1])


# --------------------------------------------------------------------------- #
# Sampling
# --------------------------------------------------------------------------- #


def sample_subgraph(
    ledger: pa.Table,
    accounts: pa.Table,
    patterns: Patterns,
    location: AttemptLocation,
    generation: Mapping[str, Any],
) -> Subgraph:
    """Build the sample. See the module docstring for the algorithm.

    Raises:
        SamplingError: if the seeds alone exceed a budget, if too few entities
            qualify as hosts, or if protected pairs alone exceed the leg budget.
            The sample is never quietly shrunk below its contract.
    """
    cfg = SamplingConfig.from_config(generation)
    host_cfg = HostCriteria.from_config(generation)
    reserved = reserved_injection_legs(generation)
    leg_budget = cfg.max_transactions - reserved
    if leg_budget <= 0:
        raise SamplingError(
            f"subgraph.max_transactions ({cfg.max_transactions}) does not cover the "
            f"{reserved} legs reserved for injection"
        )
    stats: dict[str, int] = {"reserved_injection_legs": reserved, "source_pairs": ledger.num_rows}

    # ---- account index ------------------------------------------------------ #
    acct_keys = composite_key(accounts["bank_id"], accounts["account_number"])
    party_type_col = map_distinct(
        accounts[ENTITY_PREFIX],
        lambda prefix: resolve_party_type(prefix, cfg.party_type_map, cfg.party_type_default),
    )
    entity_col = accounts["entity_id"].combine_chunks()
    type_of = dict(zip(entity_col.to_pylist(), party_type_col.to_pylist(), strict=True))
    name_of = dict(zip(entity_col.to_pylist(), accounts["entity_name"].to_pylist(), strict=True))
    accounts_of: dict[str, list[str]] = defaultdict(list)
    for entity, key in zip(entity_col.to_pylist(), acct_keys.to_pylist(), strict=True):
        accounts_of[entity].append(key)

    from_key = composite_key(ledger["from_bank"], ledger["from_account"])
    to_key = composite_key(ledger["to_bank"], ledger["to_account"])
    from_idx = pc.index_in(from_key, value_set=acct_keys)
    to_idx = pc.index_in(to_key, value_set=acct_keys)
    resolved = pc.and_(pc.is_valid(from_idx), pc.is_valid(to_idx))
    from_entity = pc.take(entity_col, from_idx)
    to_entity = pc.take(entity_col, to_idx)
    from_type = pc.take(party_type_col, from_idx)
    to_type = pc.take(party_type_col, to_idx)

    # ---- 1. exclusions ------------------------------------------------------ #
    bad_format = _is_in(ledger["payment_format"], cfg.exclude_payment_formats, pa.string())
    bad_currency = pc.or_(
        _is_in(ledger["payment_currency"], cfg.exclude_currencies, pa.string()),
        _is_in(ledger["receiving_currency"], cfg.exclude_currencies, pa.string()),
    )
    external = pc.or_(
        _is_in(from_type, cfg.excluded_party_types, pa.string()),
        _is_in(to_type, cfg.excluded_party_types, pa.string()),
    )
    unresolved = pc.invert(resolved)
    excluded = pc.or_(pc.or_(bad_format, bad_currency), pc.or_(external, unresolved))
    stats.update(
        unresolved_pairs=pc.sum(unresolved).as_py() or 0,
        excluded_pairs_format=pc.sum(bad_format).as_py() or 0,
        excluded_pairs_currency=pc.sum(bad_currency).as_py() or 0,
        excluded_pairs_party_type=pc.sum(external).as_py() or 0,
        excluded_pairs=pc.sum(excluded).as_py() or 0,
    )

    src_rows = ledger[SRC_ROW_NUMBER]
    excluded_src = set(pc.filter(src_rows, excluded).to_pylist())
    dropped: dict[int, str] = {}
    attempt_rows: dict[int, tuple[int, ...]] = {}
    for attempt in patterns.attempts:
        rows = location.src_rows(attempt)
        attempt_rows[attempt.attempt_id] = rows
        if any(not location.matches[r.line_number] for r in attempt.rows):
            dropped[attempt.attempt_id] = "unlocated"
        elif any(n in excluded_src for n in rows):
            dropped[attempt.attempt_id] = "excluded_pair"
    dropped_src = {n for a in dropped for n in attempt_rows[a]}
    eligible = pc.and_(pc.invert(excluded), pc.invert(_is_in(src_rows, dropped_src, pa.int64())))
    stats["attempts_dropped_by_exclusion"] = len(dropped)

    edges = pa.table(
        {
            SRC_ROW_NUMBER: src_rows,
            "from_key": from_key,
            "to_key": to_key,
            "from_entity": from_entity,
            "to_entity": to_entity,
            "is_laundering": ledger["is_laundering"],
        }
    )
    eligible_edges = edges.filter(eligible)
    stats["eligible_pairs"] = eligible_edges.num_rows

    # ---- 2. hosts ----------------------------------------------------------- #
    hosts: list[str] = []
    if host_cfg.enabled:
        illicit = edges.filter(pc.and_(resolved, pc.equal(edges["is_laundering"], 1)))
        tainted = set(illicit["from_entity"].to_pylist()) | set(illicit["to_entity"].to_pylist())
        pair_count: dict[str, int] = defaultdict(int)
        for e in (
            *eligible_edges["from_entity"].to_pylist(),
            *eligible_edges["to_entity"].to_pylist(),
        ):
            pair_count[e] += 1
        qualifying = [
            e
            for e, keys in accounts_of.items()
            if type_of[e] == host_cfg.party_type
            and host_cfg.accounts_min <= len(keys) <= host_cfg.accounts_max
            and not (host_cfg.clean_history and e in tainted)
            and pair_count.get(e, 0) >= host_cfg.min_ledger_pairs
        ]
        stats["qualifying_hosts"] = len(qualifying)
        if len(qualifying) < host_cfg.cohort_parties:
            raise SamplingError(
                f"Only {len(qualifying)} entities qualify as structuring hosts; "
                f"cohort_parties is {host_cfg.cohort_parties}"
            )
        hosts = sorted(
            _rank(qualifying, seed=cfg.seed, namespace="structuring-host")[
                : host_cfg.cohort_parties
            ]
        )

    # ---- 3. seeds ----------------------------------------------------------- #
    owner = dict(zip(acct_keys.to_pylist(), entity_col.to_pylist(), strict=True))
    seed_accounts = {k for e in hosts for k in accounts_of[e]}
    if cfg.seed_source == "laundering_patterns":
        for attempt in patterns.attempts:
            if attempt.attempt_id not in dropped:
                seed_accounts.update(_join(k) for k in attempt.accounts)
    seed_entities = {owner[k] for k in seed_accounts}
    if len(seed_entities) > cfg.target_parties or len(seed_accounts) > cfg.target_accounts:
        raise SamplingError(
            f"The {cfg.seed_source} seed set alone is {len(seed_entities)} parties / "
            f"{len(seed_accounts)} accounts, over the budget of {cfg.target_parties} / "
            f"{cfg.target_accounts}. Seeds are never subsampled: raise the budget or "
            "change subgraph.seed_source."
        )

    sampled: set[str] = set(seed_entities)
    sampled_accounts: set[str] = set(seed_accounts)
    frontier = set(sampled_accounts)
    skipped = 0

    # ---- 4. BFS ------------------------------------------------------------- #
    for hop in range(1, cfg.expansion_hops + 1):
        if not frontier:
            break
        f_arr = pa.array(sorted(frontier), type=pa.string())
        touch = eligible_edges.filter(
            pc.or_(
                pc.is_in(eligible_edges["from_key"], value_set=f_arr),
                pc.is_in(eligible_edges["to_key"], value_set=f_arr),
            )
        )
        neighbours: dict[str, set[str]] = defaultdict(set)
        for fk, tk in zip(touch["from_key"].to_pylist(), touch["to_key"].to_pylist(), strict=True):
            if fk in frontier and tk not in sampled_accounts:
                neighbours[fk].add(tk)
            if tk in frontier and fk not in sampled_accounts:
                neighbours[tk].add(fk)
        candidates: set[str] = set()
        for account, found in neighbours.items():
            candidates.update(
                _rank_neighbours(account, found, cfg)[: cfg.max_neighbours_per_account]
            )

        admitted: list[str] = []
        for key in _rank(candidates, seed=cfg.seed, namespace="bfs-admit"):
            new_party = owner[key] not in sampled
            if len(sampled_accounts) + 1 > cfg.target_accounts or (
                new_party and len(sampled) + 1 > cfg.target_parties
            ):
                skipped += 1
                continue
            sampled.add(owner[key])
            sampled_accounts.add(key)
            admitted.append(key)
        stats[f"hop{hop}_admitted_accounts"] = len(admitted)
        frontier = set(admitted)
    stats["bfs_skipped_over_budget"] = skipped

    # ---- 5. pairs ----------------------------------------------------------- #
    s_arr = pa.array(sorted(sampled_accounts), type=pa.string())
    inside = eligible_edges.filter(
        pc.and_(
            pc.is_in(eligible_edges["from_key"], value_set=s_arr),
            pc.is_in(eligible_edges["to_key"], value_set=s_arr),
        )
    )
    inside_rows = set(inside[SRC_ROW_NUMBER].to_pylist())
    captured: list[int] = []
    for attempt in patterns.attempts:
        if attempt.attempt_id in dropped:
            continue
        rows = attempt_rows[attempt.attempt_id]
        present = sum(1 for n in rows if n in inside_rows)
        if present == len(rows):
            captured.append(attempt.attempt_id)
        else:
            dropped[attempt.attempt_id] = "partial_capture" if present else "not_sampled"
    if cfg.seed_source == "laundering_patterns":
        uncaptured = [a for a, why in dropped.items() if why in ("partial_capture", "not_sampled")]
        if uncaptured:  # pragma: no cover - every attempt account is a seed: a logic error
            raise SamplingError(f"Seeded attempts not captured: {uncaptured[:10]}")
    partial_src = {
        n for a, why in dropped.items() if why == "partial_capture" for n in attempt_rows[a]
    }
    kept = inside.filter(pc.invert(_is_in(inside[SRC_ROW_NUMBER], partial_src, pa.int64())))

    stats["pairs_before_leg_cut"] = kept.num_rows
    if 2 * kept.num_rows > leg_budget:
        protected_src = {n for a in captured for n in attempt_rows[a]}
        host_keys = {k for e in hosts for k in accounts_of[e]}
        protected_mask = pc.or_(
            _is_in(kept[SRC_ROW_NUMBER], protected_src, pa.int64()),
            pc.or_(
                _is_in(kept["from_key"], host_keys, pa.string()),
                _is_in(kept["to_key"], host_keys, pa.string()),
            ),
        )
        protected = kept.filter(protected_mask)
        if 2 * protected.num_rows > leg_budget:
            raise SamplingError(
                f"Protected pairs (attempts + host traffic) need {2 * protected.num_rows} legs; "
                f"the budget after the injection reserve is {leg_budget}"
            )
        room = leg_budget // 2 - protected.num_rows
        cuttable = kept.filter(pc.invert(protected_mask))[SRC_ROW_NUMBER].to_pylist()
        rest = ledger.filter(_is_in(src_rows, cuttable, pa.int64()))
        cut_order: list[tuple[int, str, int]] = sorted(
            (stable_hash(content, cfg.seed, namespace="leg-budget"), content, row[SRC_ROW_NUMBER])
            for row in rest.to_pylist()
            for content in (pair_content_key(row),)
        )
        keep_src = set(protected[SRC_ROW_NUMBER].to_pylist()) | {n for _, _, n in cut_order[:room]}
    else:
        keep_src = set(kept[SRC_ROW_NUMBER].to_pylist())
    pairs = ledger.filter(_is_in(src_rows, keep_src, pa.int64()))
    pairs = pairs.sort_by([(SRC_ROW_NUMBER, "ascending")])
    stats.update(pairs=pairs.num_rows, legs=2 * pairs.num_rows)

    # ---- result ------------------------------------------------------------- #
    entities = tuple(sorted(sampled))
    grouped: dict[str, list[AccountKey]] = defaultdict(list)
    for key in sampled_accounts:
        grouped[owner[key]].append(_split(key))
    by_entity = {e: tuple(sorted(grouped[e])) for e in entities}
    stats.update(
        parties=len(entities),
        accounts=sum(len(v) for v in by_entity.values()),
        seed_parties=len(seed_entities),
        attempts_captured=len(captured),
    )
    return Subgraph(
        entities=entities,
        accounts_by_entity=by_entity,
        party_types={e: type_of[e] for e in entities},
        entity_names={e: name_of[e] for e in entities},
        pairs=pairs,
        hosts=tuple(hosts),
        seed_entities=tuple(sorted(seed_entities)),
        captured_attempts=tuple(captured),
        dropped_attempts=dict(sorted(dropped.items())),
        party_ids={e: party_id(e, seed=cfg.seed) for e in entities},
        account_ids={
            k: account_id(k[0], k[1], seed=cfg.seed) for keys in by_entity.values() for k in keys
        },
        stats=stats,
    )
