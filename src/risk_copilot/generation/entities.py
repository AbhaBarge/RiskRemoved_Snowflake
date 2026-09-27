"""Dress the preserved AMLworld keys as customers -- L1 enrichment, plan step 5.

Nothing here mints an identity. Every sampled entity already has its
``party_id`` and every sampled account its ``account_id`` (``graph``); this
module derives the attributes a bank would hold about them:

* ``RAW_PARTY_CBS``: name, type, dates, branch
* ``RAW_PARTY_KYC``: occupation, declared income, risk, PEP, identifiers, phone
* ``RAW_ACCOUNTS``: type, dates, status, balance, limit, branch, device

Determinism: each attribute draws from ``derived_rng(entity_id, seed=...,
namespace=<attribute>)``, keyed on the AMLworld entity or account, and Faker is
re-seeded per entity with ``stable_hash``. A party's name and branch are
therefore the same whether it is sampled into dev or demo, and adding one
attribute never shifts another.

Dates are anchored to the first day of the legitimate window, measured from the
sampled legs, so every record predates the transactions it explains.

**Declared income (GAP-04, D1)** is derived from the party's own *legitimate*
inflow in the window: annualised by ``annualise_to_days / observed span``,
divided by a floored normal coverage ratio, multiplied by lognormal noise. The
illicit cohort -- every party holding an illicit leg, plus the structuring
hosts -- then under-declares by the configured factor. No ceiling (M12).
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pyarrow as pa
from faker import Faker

from risk_copilot.generation import GenerationError
from risk_copilot.generation.draws import account_type, draw, weighted_options
from risk_copilot.generation.graph import Subgraph
from risk_copilot.generation.reskin import MONEY
from risk_copilot.ids import derived_rng, stable_hash

CBS_SCHEMA = pa.schema(
    [
        ("party_id", pa.string()),
        ("full_name", pa.string()),
        ("party_type", pa.string()),
        ("date_of_birth", pa.date32()),
        ("incorporation_date", pa.date32()),
        ("nationality", pa.string()),
        ("branch_region", pa.string()),
        ("branch_code", pa.string()),
        ("customer_since", pa.date32()),
        ("status", pa.string()),
    ]
)

KYC_SCHEMA = pa.schema(
    [
        ("party_id", pa.string()),
        ("occupation", pa.string()),
        ("declared_annual_income", MONEY),
        ("source_of_funds", pa.string()),
        ("risk_rating", pa.string()),
        ("pep_flag", pa.bool_()),
        ("kyc_status", pa.string()),
        ("kyc_last_reviewed", pa.date32()),
        ("pan_number", pa.string()),
        ("aadhaar_last4", pa.string()),
        ("phone_number", pa.string()),
        ("kyc_note", pa.string()),
    ]
)

ACCOUNT_SCHEMA = pa.schema(
    [
        ("account_id", pa.string()),
        ("party_id", pa.string()),
        ("account_type", pa.string()),
        ("opened_date", pa.date32()),
        ("status", pa.string()),
        ("current_balance", MONEY),
        ("credit_limit", MONEY),
        ("branch_region", pa.string()),
        ("branch_code", pa.string()),
        ("device_fingerprint", pa.string()),
    ]
)

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DAYS_PER_YEAR = 365
_CENTS = Decimal("0.01")


class EntityError(GenerationError):
    """Entity attributes cannot be derived under the configured contract."""


@dataclass(frozen=True)
class Window:
    """The observed legitimate event clock of the sampled legs.

    Measured over **legitimate** legs only. The ledger's legit activity ends on
    2022-09-10 (M21) and 09-11..09-18 is 59% laundering, so a span measured over
    all legs would stretch the income basis over days on which legit customers
    did not transact, and would place injected deposits where legit traffic
    is absent.
    """

    first_day: dt.date
    last_day: dt.date

    @property
    def span_days(self) -> int:
        """Inclusive calendar days: 2022-09-01..2022-09-10 is 10."""
        return (self.last_day - self.first_day).days + 1


@dataclass(frozen=True)
class Entities:
    """Enriched records plus the per-party facts later steps need."""

    cbs: pa.Table
    kyc: pa.Table
    accounts: pa.Table
    window: Window
    branch_region: Mapping[str, str]  # party_id -> region
    #: party_id -> legitimate INR inflow in the window (the income basis).
    legit_inflow_inr: Mapping[str, Decimal]
    stats: Mapping[str, Any]


def observed_window(legs: pa.Table, labels: pa.Table) -> Window:
    """First and last calendar day of the *legitimate* legs' ``txn_timestamp``."""
    illicit = {
        (r["pair_ref"], r["leg_side"])
        for r in labels.select(["pair_ref", "leg_side", "is_illicit"]).to_pylist()
        if r["is_illicit"]
    }
    stamps = [
        r["txn_timestamp"]
        for r in legs.select(["pair_ref", "leg_side", "txn_timestamp"]).to_pylist()
        if (r["pair_ref"], r["leg_side"]) not in illicit
    ]
    if not stamps:
        raise EntityError("No legitimate legs were sampled; there is no window to anchor to")
    return Window(first_day=min(stamps).date(), last_day=max(stamps).date())


def _days_before(anchor: dt.date, rng: Any, low_days: int, high_days: int) -> dt.date:
    return anchor - dt.timedelta(days=rng.randint(low_days, high_days))


def _round_to(value: Decimal, step: int) -> Decimal:
    return (value / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step


def _pan(rng: Any, holder_code: str, name: str) -> str:
    initial = next((c for c in name.upper() if c in _LETTERS), rng.choice(_LETTERS))
    head = "".join(rng.choice(_LETTERS) for _ in range(3))
    digits = "".join(rng.choice("0123456789") for _ in range(4))
    return f"{head}{holder_code}{initial}{digits}{rng.choice(_LETTERS)}"


def phone_number(key: object, *, seed: int, spec: Mapping[str, Any]) -> str:
    """An Indian mobile number in the configured format, drawn per key."""
    rng = derived_rng(key, seed=seed, namespace="phone")
    first = rng.choice([str(d) for d in spec["first_digits"]])
    rest = "".join(rng.choice("0123456789") for _ in range(int(spec["subscriber_digits"])))
    return f"{spec['country_code']}{first}{rest}"


def device_fingerprint(key: object, *, seed: int, spec: Mapping[str, Any]) -> str:
    token = f"{stable_hash(key, seed, namespace='device'):016x}"
    return f"{spec['fingerprint_prefix']}{token[: int(spec['fingerprint_hex_digits'])].upper()}"


def _check_dates(dates: Mapping[str, Any]) -> None:
    age_gap = int(dates["individual_age_years"]["min"]) - int(dates["min_customer_age_years"])
    tenure_min = int(dates["customer_tenure_years"]["min"])
    if age_gap < tenure_min or int(dates["incorporation_years_ago"]["min"]) < tenure_min:
        raise EntityError(
            "entities.dates: the youngest party must be able to have the minimum tenure "
            "(individual_age_years.min - min_customer_age_years >= customer_tenure_years.min, "
            "and incorporation_years_ago.min >= customer_tenure_years.min)"
        )


def build_entities(
    subgraph: Subgraph,
    legs: pa.Table,
    labels: pa.Table,
    generation: Mapping[str, Any],
) -> Entities:
    """Enrich every sampled party and account.

    Args:
        subgraph: The sample (``graph.sample_subgraph``).
        legs: Reskinned source legs (``reskin.reskin_pairs``).
        labels: Their labels (``labels.label_source_legs``), used only to split
            legitimate from illicit inflow; no label value reaches an output.
    """
    try:
        ent = generation["entities"]
        seed = int(generation["seed"])
        dates = ent["dates"]
        income = ent["income"]
        device = ent["device"]
        if income["derive_from"] != "legit_inflow_window_annualised":
            raise EntityError("entities.income.derive_from: only legit_inflow_window_annualised")
        _check_dates(dates)
        regions = weighted_options(ent["branch_regions"], "entities.branch_regions")
        risk = weighted_options(ent["risk_rating_distribution"], "risk_rating_distribution")
        kyc_status = weighted_options(ent["kyc_status_distribution"], "kyc_status_distribution")
        funds = {
            t: weighted_options(v, f"entities.source_of_funds.{t}")
            for t, v in ent["source_of_funds"].items()
        }
        credit = ent["credit_limit"]
    except KeyError as exc:
        raise EntityError(f"conf/generation.yml: missing entity setting {exc}") from exc

    window = observed_window(legs, labels)
    as_of = window.first_day
    party_of_account = {
        subgraph.account_ids[k]: subgraph.party_ids[e]
        for e, keys in subgraph.accounts_by_entity.items()
        for k in keys
    }

    # ---- inflow, from source legs ------------------------------------------ #
    illicit_leg = {
        (r["pair_ref"], r["leg_side"])
        for r in labels.select(["pair_ref", "leg_side", "is_illicit"]).to_pylist()
        if r["is_illicit"]
    }
    legit_inflow: dict[str, Decimal] = defaultdict(Decimal)
    net_flow: dict[str, Decimal] = defaultdict(Decimal)
    illicit_parties: set[str] = {subgraph.party_ids[h] for h in subgraph.hosts}
    for leg in legs.select(["pair_ref", "leg_side", "account_id", "amount_inr"]).to_pylist():
        party = party_of_account[leg["account_id"]]
        net_flow[leg["account_id"]] += leg["amount_inr"]
        if (leg["pair_ref"], leg["leg_side"]) in illicit_leg:
            illicit_parties.add(party)
        elif leg["leg_side"] == "C":
            legit_inflow[party] += leg["amount_inr"]

    annualise = Decimal(int(income["annualise_to_days"])) / Decimal(window.span_days)
    floor_inr = Decimal(int(income["floor_inr"]))
    under = Decimal(str(income["illicit_cohort_under_declare_factor"]))

    faker = Faker(str(ent["faker_locale"]))
    cbs: list[dict[str, Any]] = []
    kyc: list[dict[str, Any]] = []
    region_of: dict[str, str] = {}
    branch_of: dict[str, str] = {}
    since_of: dict[str, dt.date] = {}

    for entity in subgraph.entities:
        pid = subgraph.party_ids[entity]
        ptype = subgraph.party_types[entity]
        if ptype not in funds or ptype not in ent["pan_holder_code"]:
            raise EntityError(f"Party type {ptype!r} has no source_of_funds / pan_holder_code")
        rng = derived_rng(entity, seed=seed, namespace="party")
        faker.seed_instance(stable_hash(entity, seed, namespace="faker"))
        individual = ptype == "INDIVIDUAL"
        name = faker.name() if individual else faker.company()

        if individual:
            age = dates["individual_age_years"]
            anchor = _days_before(
                as_of, rng, int(age["min"]) * _DAYS_PER_YEAR, int(age["max"]) * _DAYS_PER_YEAR
            )
            earliest = anchor + dt.timedelta(
                days=int(dates["min_customer_age_years"]) * _DAYS_PER_YEAR
            )
        else:
            inc = dates["incorporation_years_ago"]
            anchor = _days_before(
                as_of, rng, int(inc["min"]) * _DAYS_PER_YEAR, int(inc["max"]) * _DAYS_PER_YEAR
            )
            earliest = anchor
        tenure = dates["customer_tenure_years"]
        earliest = max(earliest, as_of - dt.timedelta(days=int(tenure["max"]) * _DAYS_PER_YEAR))
        latest = as_of - dt.timedelta(days=int(tenure["min"]) * _DAYS_PER_YEAR)
        customer_since = earliest + dt.timedelta(days=rng.randint(0, (latest - earliest).days))

        region = draw(entity, seed=seed, namespace="branch_region", options=regions)
        branch = str(ent["branch_code_template"]).format(
            region=region, index=rng.randint(1, int(ent["branches_per_region"]))
        )
        region_of[pid], branch_of[pid], since_of[pid] = region, branch, customer_since

        cbs.append(
            {
                "party_id": pid,
                "full_name": name,
                "party_type": ptype,
                "date_of_birth": anchor if individual else None,
                "incorporation_date": None if individual else anchor,
                "nationality": str(ent["nationality"]),
                "branch_region": region,
                "branch_code": branch,
                "customer_since": customer_since,
                "status": str(ent["party_status"]),
            }
        )

        # -- declared income (GAP-04) -- #
        irng = derived_rng(entity, seed=seed, namespace="income")
        coverage = max(
            float(income["coverage_ratio_min"]),
            irng.gauss(float(income["coverage_ratio_mean"]), float(income["coverage_ratio_sigma"])),
        )
        noise = math.exp(irng.gauss(0.0, float(income["noise_lognormal_sigma"])))
        declared = legit_inflow.get(pid, Decimal(0)) * annualise
        declared = declared / Decimal(repr(coverage)) * Decimal(repr(noise))
        if pid in illicit_parties:
            declared *= under
        declared = _round_to(max(declared, floor_inr), int(income["round_to_inr"]))

        krng = derived_rng(entity, seed=seed, namespace="kyc")
        reviewed = max(
            customer_since,
            as_of - dt.timedelta(days=krng.randint(0, int(dates["kyc_review_max_days_ago"]))),
        )
        kyc.append(
            {
                "party_id": pid,
                "occupation": faker.job() if individual else None,
                "declared_annual_income": declared.quantize(_CENTS),
                "source_of_funds": draw(
                    entity, seed=seed, namespace="source_of_funds", options=funds[ptype]
                ),
                "risk_rating": draw(entity, seed=seed, namespace="risk_rating", options=risk),
                "pep_flag": krng.random() * 100 < float(ent["pep_pct"]),
                "kyc_status": draw(entity, seed=seed, namespace="kyc_status", options=kyc_status),
                "kyc_last_reviewed": reviewed,
                "pan_number": _pan(krng, str(ent["pan_holder_code"][ptype]), name),
                "aadhaar_last4": f"{krng.randint(0, 9999):04d}" if individual else None,
                "phone_number": phone_number(entity, seed=seed, spec=ent["phone"]),
                # L3 narration is blocked on this account (S2); NULL until it exists.
                "kyc_note": None,
            }
        )

    # ---- accounts ----------------------------------------------------------- #
    accounts: list[dict[str, Any]] = []
    credit_types = set(credit["account_types"])
    balance = ent["balance"]
    for entity in subgraph.entities:
        pid = subgraph.party_ids[entity]
        for key in subgraph.accounts_by_entity[entity]:
            aid = subgraph.account_ids[key]
            arng = derived_rng(key[0], key[1], seed=seed, namespace="account")
            atype = account_type(aid, generation)
            span = (as_of - dt.timedelta(days=1) - since_of[pid]).days
            opened = since_of[pid] + dt.timedelta(days=arng.randint(0, max(span, 0)))
            draw_opening = arng.lognormvariate(
                float(balance["opening_lognormal_mu"]), float(balance["opening_lognormal_sigma"])
            )
            opening = Decimal(f"{draw_opening:.2f}")
            limit = None
            if atype in credit_types:
                step = int(credit["round_to_inr"])
                limit = Decimal(
                    arng.randint(int(credit["min_inr"]) // step, int(credit["max_inr"]) // step)
                    * step
                ).quantize(_CENTS)
            accounts.append(
                {
                    "account_id": aid,
                    "party_id": pid,
                    "account_type": atype,
                    "opened_date": opened,
                    "status": str(ent["account_status"]),
                    "current_balance": max(
                        opening + net_flow.get(aid, Decimal(0)), Decimal(0)
                    ).quantize(_CENTS),
                    "credit_limit": limit,
                    "branch_region": region_of[pid],
                    "branch_code": branch_of[pid],
                    "device_fingerprint": device_fingerprint(aid, seed=seed, spec=device),
                }
            )

    cbs.sort(key=lambda r: r["party_id"])
    kyc.sort(key=lambda r: r["party_id"])
    accounts.sort(key=lambda r: r["account_id"])
    return Entities(
        cbs=pa.Table.from_pylist(cbs, schema=CBS_SCHEMA),
        kyc=pa.Table.from_pylist(kyc, schema=KYC_SCHEMA),
        accounts=pa.Table.from_pylist(accounts, schema=ACCOUNT_SCHEMA),
        window=window,
        branch_region=region_of,
        legit_inflow_inr=dict(legit_inflow),
        stats={
            "parties": len(cbs),
            "accounts": len(accounts),
            "observed_span_days": window.span_days,
            "income_under_declared_parties": sum(
                1 for r in kyc if r["party_id"] in illicit_parties
            ),
            "faker_locale": str(ent["faker_locale"]),
        },
    )
