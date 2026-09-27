"""Structuring injection in ratio space -- GAP-06/07, decision D2, plan step 6.

AMLworld has no structuring (M6: 3,208 of 3,209 attributed illicit rows are
ACH), so the cohort is injected onto real host parties chosen by ``graph``
(D14). Each host deposits cash into 2-4 of its own deposit accounts (D20), a few
times each, within a ``window_days`` burst inside the measured legitimate window
(D18), during branch hours.

**Amounts are deferred (D2).** Each deposit is a fraction of the CTR threshold,
drawn inside ``amount_band_pct_of_ctr``; ``load`` resolves rupees from
``V_POLICY_CLAUSE_CURRENT`` at run time. The ratio exists only in
``GROUND_TRUTH.INJECTED_TXN_PENDING`` and never in a RAW file.

**Cash desk (D4).** A deposit posts Dr branch cash / Cr customer. The debit leg
sits on one internal account per branch region, derived through ``ids`` from
``internal_accounts`` -- the single documented exception to "never mint ids".
It is written to ``ID_MAP_PARTY`` / ``ID_MAP_ACCOUNT`` only.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pyarrow as pa

from risk_copilot.generation import GenerationError
from risk_copilot.generation.draws import weighted_options
from risk_copilot.generation.entities import Entities
from risk_copilot.generation.graph import Subgraph
from risk_copilot.generation.labels import LABEL_SCHEMA
from risk_copilot.ids import account_id, derived_rng, party_id, stable_hash

RATIO = pa.decimal128(12, 10)

#: ``INJECTED_TXN_PENDING`` columns, with the internal ``pair_ref`` / ``order_key``
#: in place of the ids the writer allocates.
PENDING_SCHEMA = pa.schema(
    [
        ("pair_ref", pa.string()),
        ("order_key", pa.string()),
        ("leg_side", pa.string()),
        ("account_id", pa.string()),
        ("counterparty_account_id", pa.string()),
        ("txn_timestamp", pa.timestamp("s")),
        ("amount_sign", pa.int8()),
        ("amount_ctr_ratio", RATIO),
        ("ctr_clause_id", pa.string()),
        ("currency", pa.string()),
        ("txn_type", pa.string()),
        ("channel", pa.string()),
        ("reference_text", pa.string()),
        ("cohort", pa.string()),
        ("generation_seed", pa.int64()),
    ]
)

ID_MAP_PARTY_SCHEMA = pa.schema(
    [("party_id", pa.string()), ("src_entity_id", pa.string()), ("generation_seed", pa.int64())]
)
ID_MAP_ACCOUNT_SCHEMA = pa.schema(
    [
        ("account_id", pa.string()),
        ("src_bank_id", pa.string()),
        ("src_account_number", pa.string()),
        ("party_id", pa.string()),
        ("generation_seed", pa.int64()),
    ]
)


class InjectionError(GenerationError):
    """The cohort cannot be injected under the configured contract."""


@dataclass(frozen=True)
class StructuringConfig:
    typology_id: str
    label: str
    accounts_min: int
    accounts_max: int
    deposits_min: int
    deposits_max: int
    band: tuple[Decimal, Decimal]
    window_days: int
    hours: tuple[int, int]
    channel: str
    txn_type: str
    ctr_clause_id: str
    ratio_quantum: Decimal
    deposit_account_types: tuple[str, ...]

    @classmethod
    def from_config(cls, generation: Mapping[str, Any]) -> StructuringConfig:
        try:
            s = generation["injection"]["structuring"]
            if s["debit_side"] != "cash_desk" or s["amount_resolution"] != "deferred_ctr_ratio":
                raise InjectionError(
                    "injection.structuring: only debit_side cash_desk with "
                    "amount_resolution deferred_ctr_ratio is implemented"
                )
            lo, hi = (Decimal(str(v)) for v in s["amount_band_pct_of_ctr"])
            start, end = (int(h) for h in s["deposit_hours"])
            if not (0 < lo < hi < 1) or not (0 <= start < end <= 24):
                raise InjectionError(
                    "injection.structuring: band must be 0 < lo < hi < 1, hours 0-24"
                )
            return cls(
                typology_id=str(s["typology_id"]),
                label=str(s["label"]),
                accounts_min=int(s["accounts_per_party_min"]),
                accounts_max=int(s["accounts_per_party_max"]),
                deposits_min=int(s["deposits_per_account_min"]),
                deposits_max=int(s["deposits_per_account_max"]),
                band=(lo, hi),
                window_days=int(s["window_days"]),
                hours=(start, end),
                channel=str(s["channel"]),
                txn_type=str(s["txn_type"]),
                ctr_clause_id=str(s["ctr_clause_id"]),
                ratio_quantum=Decimal(1).scaleb(-int(s["amount_ctr_ratio_decimals"])),
                deposit_account_types=tuple(s["deposit_account_types"]),
            )
        except KeyError as exc:
            raise InjectionError(f"injection.structuring: missing setting {exc}") from exc


@dataclass(frozen=True)
class HostPlan:
    """What was injected for one host -- the construction evidence."""

    party_id: str
    entity_id: str
    branch_region: str
    accounts: tuple[str, ...]
    deposits: int
    window_start: dt.date
    window_end: dt.date  # inclusive


@dataclass(frozen=True)
class Injection:
    pending: pa.Table
    labels: pa.Table
    id_map_party: pa.Table
    id_map_account: pa.Table
    hosts: tuple[HostPlan, ...]
    stats: Mapping[str, int]


def cash_desk_ids(generation: Mapping[str, Any]) -> tuple[str, str, dict[str, tuple[str, str]]]:
    """``(party_id, owner_key, {region: (account_number, account_id)})`` for the desk."""
    seed = int(generation["seed"])
    internal = generation["internal_accounts"]
    bank = str(internal["src_bank_id"])
    owner = str(internal["owner_entity_key"])
    desk = internal["cash_desk"]
    if desk.get("per") != "branch_region":
        raise InjectionError("internal_accounts.cash_desk.per: only branch_region is implemented")
    regions = [
        code
        for code, _ in weighted_options(generation["entities"]["branch_regions"], "branch_regions")
    ]
    accounts = {}
    for region in regions:
        number = f"{desk['account_number_prefix']}:{region}"
        accounts[region] = (number, account_id(bank, number, seed=seed))
    return party_id(owner, seed=seed), owner, accounts


def inject_structuring(
    subgraph: Subgraph, entities: Entities, generation: Mapping[str, Any]
) -> Injection:
    """Build the structuring cohort's pending legs, labels and cash-desk id maps."""
    cfg = StructuringConfig.from_config(generation)
    seed = int(generation["seed"])
    window = entities.window
    if window.span_days < cfg.window_days:
        raise InjectionError(
            f"injection_window: window_days {cfg.window_days} exceeds the observed legitimate "
            f"span of {window.span_days} days"
        )
    desk_party, desk_owner, desk_accounts = cash_desk_ids(generation)
    internal_bank = str(generation["internal_accounts"]["src_bank_id"])
    atype = dict(
        zip(
            entities.accounts["account_id"].to_pylist(),
            entities.accounts["account_type"].to_pylist(),
            strict=True,
        )
    )

    pending: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    plans: list[HostPlan] = []
    minutes = (cfg.hours[1] - cfg.hours[0]) * 60
    for entity in subgraph.hosts:
        pid = subgraph.party_ids[entity]
        region = entities.branch_region[pid]
        desk_account = desk_accounts[region][1]
        rng = derived_rng(entity, seed=seed, namespace="structuring")
        deposit_accounts = sorted(
            (
                a
                for a in (subgraph.account_ids[k] for k in subgraph.accounts_by_entity[entity])
                if atype[a] in cfg.deposit_account_types
            ),
            key=lambda a: (stable_hash(a, seed, namespace="structuring-account"), a),
        )
        if len(deposit_accounts) < cfg.accounts_min:
            raise InjectionError(f"Host {pid} has {len(deposit_accounts)} deposit accounts")
        chosen = deposit_accounts[
            : rng.randint(cfg.accounts_min, min(cfg.accounts_max, len(deposit_accounts)))
        ]
        start = window.first_day + dt.timedelta(
            days=rng.randint(0, window.span_days - cfg.window_days)
        )
        deposits = 0
        for account in chosen:
            for n in range(rng.randint(cfg.deposits_min, cfg.deposits_max)):
                day = start + dt.timedelta(days=rng.randint(0, cfg.window_days - 1))
                minute = rng.randrange(minutes)
                ts = dt.datetime.combine(day, dt.time(cfg.hours[0] + minute // 60, minute % 60))
                lo, hi = cfg.band
                ratio = (lo + (hi - lo) * Decimal(repr(rng.random()))).quantize(
                    cfg.ratio_quantum, rounding=ROUND_HALF_UP
                )
                if not lo <= ratio <= hi:  # pragma: no cover - quantum is far below the band
                    raise InjectionError(f"ratio {ratio} left the band after quantisation")
                ref = f"inj:{cfg.label}:{account}:{n}"
                order_key = f"{account}|{ts.isoformat()}|{ratio}"
                for side, own, other, sign in (
                    ("D", desk_account, account, -1),
                    ("C", account, desk_account, 1),
                ):
                    pending.append(
                        {
                            "pair_ref": ref,
                            "order_key": order_key,
                            "leg_side": side,
                            "account_id": own,
                            "counterparty_account_id": other,
                            "txn_timestamp": ts,
                            "amount_sign": sign,
                            "amount_ctr_ratio": ratio,
                            "ctr_clause_id": cfg.ctr_clause_id,
                            "currency": str(generation["fx"]["base_currency"]),
                            "txn_type": cfg.txn_type,
                            "channel": cfg.channel,
                            "reference_text": None,
                            "cohort": cfg.label,
                            "generation_seed": seed,
                        }
                    )
                    labels.append(
                        {
                            "pair_ref": ref,
                            "leg_side": side,
                            "src_row_number": None,
                            "is_illicit": True,
                            "typology_id": cfg.typology_id,
                            "amlworld_pattern": None,
                            "attempt_id": None,
                            "injection_method": cfg.label,
                            "match_confidence": None,
                        }
                    )
                deposits += 1
        plans.append(
            HostPlan(
                party_id=pid,
                entity_id=entity,
                branch_region=region,
                accounts=tuple(sorted(chosen)),
                deposits=deposits,
                window_start=start,
                window_end=start + dt.timedelta(days=cfg.window_days - 1),
            )
        )

    pending.sort(key=lambda r: (r["order_key"], r["leg_side"]))
    labels.sort(key=lambda r: (r["pair_ref"], r["leg_side"]))
    id_party = [{"party_id": desk_party, "src_entity_id": desk_owner, "generation_seed": seed}]
    id_accounts = [
        {
            "account_id": aid,
            "src_bank_id": internal_bank,
            "src_account_number": number,
            "party_id": desk_party,
            "generation_seed": seed,
        }
        for _, (number, aid) in sorted(desk_accounts.items())
    ]
    return Injection(
        pending=pa.Table.from_pylist(pending, schema=PENDING_SCHEMA),
        labels=pa.Table.from_pylist(labels, schema=LABEL_SCHEMA),
        id_map_party=pa.Table.from_pylist(id_party, schema=ID_MAP_PARTY_SCHEMA),
        id_map_account=pa.Table.from_pylist(id_accounts, schema=ID_MAP_ACCOUNT_SCHEMA),
        hosts=tuple(plans),
        stats={
            "structuring_hosts": len(plans),
            "structuring_deposits": sum(p.deposits for p in plans),
            "structuring_legs": len(pending),
            "cash_desk_accounts": len(id_accounts),
        },
    )
