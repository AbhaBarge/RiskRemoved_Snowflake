"""Structuring injection in ratio space -- GAP-06/07, decision D2, plan step 6.

AMLworld has no structuring (M6: 3,208 of 3,209 attributed illicit rows are
ACH), so the cohort is injected onto real host parties chosen by ``graph``
(D14). Each host deposits cash into 2-4 of its own deposit accounts (D20), a few
times each, within a ``window_days`` burst inside the measured legitimate window
(D18), at times of day drawn from the sample's genuine cash deposits (D29).

**Amounts are deferred (D2).** Each deposit is a fraction of the CTR threshold,
drawn inside ``amount_band_pct_of_ctr``; ``load`` resolves rupees from
``V_POLICY_CLAUSE_CURRENT`` at run time. The ratio exists only in
``GROUND_TRUTH.INJECTED_TXN_PENDING`` and never in a RAW file.

**Cash desk (D4).** A deposit posts Dr branch cash / Cr customer. The debit leg
sits on one internal account per branch region (:mod:`~risk_copilot.generation.desk`),
the same desks every AMLworld cash pair is posted through (D27). The pipeline
writes their id-map rows.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pyarrow as pa

from risk_copilot.generation import GenerationError
from risk_copilot.generation.desk import CashDesk
from risk_copilot.generation.entities import Entities
from risk_copilot.generation.graph import Subgraph
from risk_copilot.generation.labels import LABEL_SCHEMA
from risk_copilot.ids import derived_rng, stable_hash

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
            if s["deposit_time_of_day"] != "genuine_cash_deposits":
                raise InjectionError(
                    "injection.structuring.deposit_time_of_day: only genuine_cash_deposits "
                    "is implemented"
                )
            if not 0 < lo < hi < 1:
                raise InjectionError("injection.structuring: band must be 0 < lo < hi < 1")
            return cls(
                typology_id=str(s["typology_id"]),
                label=str(s["label"]),
                accounts_min=int(s["accounts_per_party_min"]),
                accounts_max=int(s["accounts_per_party_max"]),
                deposits_min=int(s["deposits_per_account_min"]),
                deposits_max=int(s["deposits_per_account_max"]),
                band=(lo, hi),
                window_days=int(s["window_days"]),
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
    hosts: tuple[HostPlan, ...]
    stats: Mapping[str, int]


def genuine_deposit_times(legs: pa.Table, cfg: StructuringConfig, desk: CashDesk) -> list[dt.time]:
    """Times of day of the sample's genuine cash deposits, sorted (with repeats).

    Planted deposits draw their clock time from this empirical distribution.
    A fixed "branch hours" window made them recognisable: measured with the old
    10:00-16:00 window, 100% of planted deposits fell in it against 24-26% of
    genuine desk-routed cash deposits, which AMLworld spreads over all 24 hours.
    """
    desks = {aid for _, aid in desk.accounts.values()}
    return sorted(
        r["txn_timestamp"].time()
        for r in legs.select(
            ["leg_side", "account_id", "txn_type", "channel", "txn_timestamp"]
        ).to_pylist()
        if r["leg_side"] == "C"
        and r["txn_type"] == cfg.txn_type
        and r["channel"] == cfg.channel
        and r["account_id"] not in desks
    )


def inject_structuring(
    subgraph: Subgraph, entities: Entities, legs: pa.Table, generation: Mapping[str, Any]
) -> Injection:
    """Build the structuring cohort's pending legs and labels.

    Args:
        legs: The reskinned source legs, whose genuine cash deposits supply the
            planted deposits' times of day.
    """
    cfg = StructuringConfig.from_config(generation)
    seed = int(generation["seed"])
    window = entities.window
    if window.span_days < cfg.window_days:
        raise InjectionError(
            f"injection_window: window_days {cfg.window_days} exceeds the observed legitimate "
            f"span of {window.span_days} days"
        )
    desk = CashDesk.from_config(generation)
    clock = genuine_deposit_times(legs, cfg, desk)
    if subgraph.hosts and not clock:
        raise InjectionError(
            "The sample holds no genuine cash deposit to take a time of day from; planted "
            "deposits would need an invented clock"
        )
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
    for entity in subgraph.hosts:
        pid = subgraph.party_ids[entity]
        region = entities.branch_region[pid]
        desk_account = desk.account(region)
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
                ts = dt.datetime.combine(day, clock[rng.randrange(len(clock))])
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
    return Injection(
        pending=pa.Table.from_pylist(pending, schema=PENDING_SCHEMA),
        labels=pa.Table.from_pylist(labels, schema=LABEL_SCHEMA),
        hosts=tuple(plans),
        stats={
            "structuring_hosts": len(plans),
            "structuring_deposits": sum(p.deposits for p in plans),
            "structuring_legs": len(pending),
        },
    )
