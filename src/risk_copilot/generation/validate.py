"""Realism and contract gates -- GAP-07, plan step 7.

Every gate is *asserted* against the generated tables, never claimed in prose.
A gate whose ``validation.<name>.fail_build`` is true and whose status is FAIL
makes ``riskcopilot generate`` exit non-zero.

Clause values (band, window, minimum accounts) are read from the
``conf/detection.yml`` seed, which is what the ``policy`` deploy stage loads into
``GOVERNANCE.POLICY_CLAUSE``. Offline there is no other source, and the gates
compare *construction* against those values. None of them applies a detection
rule. ``signal_yield`` is evaluated after load, in Snowflake (D7), and is
reported here as ``DEFERRED_TO_LOAD`` once the cohort is shown to be
constructed to be detectable.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

import pyarrow as pa

from risk_copilot.generation.inject import HostPlan


class GateStatus(StrEnum):
    # S105 reads a member named PASS as a password literal; it is a gate verdict.
    PASS = "PASS"  # noqa: S105
    FAIL = "FAIL"
    REPORT = "REPORT"  # informational, never fails
    DEFERRED = "DEFERRED_TO_LOAD"


@dataclass(frozen=True)
class Gate:
    name: str
    status: GateStatus
    fail_build: bool
    value: str
    detail: str = ""

    @property
    def blocks(self) -> bool:
        return self.fail_build and self.status is GateStatus.FAIL


@dataclass(frozen=True)
class GateInputs:
    """Everything the gates read. Built by ``pipeline``."""

    raw: Mapping[str, pa.Table]
    ground_truth: Mapping[str, pa.Table]
    generation: Mapping[str, Any]
    detection: Mapping[str, Any]
    legit_span_days: int
    unresolved_source_pairs: int
    attribution_counts: Mapping[int, int]
    attribution_legit_matches: int
    hosts: Sequence[HostPlan]
    legit_inflow_inr: Mapping[str, Decimal]
    label_values: frozenset[str]
    demo_entity_error: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


def _fail_build(generation: Mapping[str, Any], name: str) -> bool:
    return bool(((generation.get("validation") or {}).get(name) or {}).get("fail_build", False))


def _clause(detection: Mapping[str, Any], clause_id: str) -> Mapping[str, Any]:
    for clause in detection.get("clauses") or []:
        if isinstance(clause, Mapping) and clause.get("clause_id") == clause_id:
            return clause
    raise KeyError(f"conf/detection.yml has no clause {clause_id!r}")


def _gate(g: GateInputs, name: str, ok: bool, value: str, detail: str = "") -> Gate:
    return Gate(
        name,
        GateStatus.PASS if ok else GateStatus.FAIL,
        _fail_build(g.generation, name),
        value,
        detail,
    )


# --------------------------------------------------------------------------- #
# Individual gates
# --------------------------------------------------------------------------- #


def illicit_rate(g: GateInputs) -> Gate:
    """Illicit share of ALL legs: source legs plus pending injected legs."""
    spec = g.generation["validation"]["illicit_rate_pct"]
    labels = g.ground_truth["TXN_LABELS"]
    total = labels.num_rows
    illicit = sum(1 for v in labels["is_illicit"].to_pylist() if v)
    pct = 100 * illicit / total if total else 0.0
    ok = float(spec["min"]) <= pct <= float(spec["max"])
    return _gate(
        g,
        "illicit_rate_pct",
        ok,
        f"{pct:.3f}%",
        f"{illicit} of {total} legs; band {spec['min']}-{spec['max']}%",
    )


def bank_id_resolution(g: GateInputs) -> Gate:
    limit = int(g.generation["validation"]["bank_id_resolution"]["max_unresolved_legs"])
    n = g.unresolved_source_pairs
    return _gate(
        g, "bank_id_resolution", n <= limit, str(2 * n), "unresolved source legs, whole ledger (M1)"
    )


def label_attribution(g: GateInputs) -> Gate:
    unique = set(g.attribution_counts) == {1}
    required = bool(g.generation["validation"]["label_attribution"]["require_unique_match"])
    ok = (unique or not required) and g.attribution_legit_matches == 0
    return _gate(
        g,
        "label_attribution",
        ok,
        f"match counts {dict(g.attribution_counts)}",
        f"{g.attribution_legit_matches} Patterns rows matched a non-illicit ledger row",
    )


def id_map_one_to_one(g: GateInputs) -> Gate:
    party, account = g.raw["ID_MAP_PARTY"], g.raw["ID_MAP_ACCOUNT"]
    problems = []
    for table, cols in (
        (party, ["party_id"]),
        (party, ["src_entity_id"]),
        (account, ["account_id"]),
        (account, ["src_bank_id", "src_account_number"]),
    ):
        keys = list(zip(*(table[c].to_pylist() for c in cols), strict=True))
        if len(keys) != len(set(keys)):
            problems.append(f"{'+'.join(cols)} repeats")
    return _gate(
        g,
        "id_map_one_to_one",
        not problems,
        f"{party.num_rows} parties / {account.num_rows} accounts",
        "; ".join(problems),
    )


def injection_window(g: GateInputs) -> Gate:
    s = g.generation["injection"]["structuring"]
    clause = float(_clause(g.detection, s["window_clause_id"])["threshold_lower"])
    days = int(s["window_days"])
    ok = days <= g.legit_span_days and days <= clause
    return _gate(
        g,
        "injection_window",
        ok,
        f"{days} d",
        f"legit span {g.legit_span_days} d; {s['window_clause_id']} {clause:g} d",
    )


def injection_band(g: GateInputs) -> Gate:
    s = g.generation["injection"]["structuring"]
    lo, hi = (Decimal(str(v)) for v in s["amount_band_pct_of_ctr"])
    clause = _clause(g.detection, s["band_clause_id"])
    c_lo, c_hi = Decimal(str(clause["threshold_lower"])), Decimal(str(clause["threshold_upper"]))
    return _gate(
        g,
        "injection_band",
        c_lo <= lo < hi <= c_hi,
        f"[{lo}, {hi}]",
        f"{s['band_clause_id']} [{c_lo}, {c_hi}]",
    )


def income_calibration(g: GateInputs) -> Gate:
    """Distribution of legit window inflow / declared annual income. Reported only."""
    kyc = g.raw["RAW_PARTY_KYC"]
    declared = dict(
        zip(kyc["party_id"].to_pylist(), kyc["declared_annual_income"].to_pylist(), strict=True)
    )
    ratios = sorted(
        float(g.legit_inflow_inr.get(p, Decimal(0)) / d) for p, d in declared.items() if d
    )
    if not ratios:
        return Gate("income_calibration", GateStatus.REPORT, False, "no parties")

    def q(p: float) -> float:
        return ratios[min(len(ratios) - 1, int(p * len(ratios)))]

    value = f"p50 {q(0.50):.4f} / p90 {q(0.90):.4f} / p99 {q(0.99):.4f} / max {ratios[-1]:.4f}"
    return Gate(
        "income_calibration",
        GateStatus.REPORT,
        _fail_build(g.generation, "income_calibration"),
        value,
        "legit window inflow / declared annual income",
    )


def money_conservation(g: GateInputs) -> Gate:
    limit = int(g.generation["validation"]["money_conservation"]["max_unpaired_legs"])
    sides: dict[str, Counter[str]] = defaultdict(Counter)
    for table in (g.raw["RAW_TRANSACTIONS"], g.ground_truth["INJECTED_TXN_PENDING"]):
        for pair, side in zip(
            table["txn_pair_id"].to_pylist(), table["leg_side"].to_pylist(), strict=True
        ):
            sides[pair][side] += 1
    bad = sum(1 for c in sides.values() if c != Counter({"D": 1, "C": 1}))
    return _gate(
        g, "money_conservation", bad <= limit, f"{bad} unpaired pairs", f"{len(sides)} pairs"
    )


def referential_integrity(g: GateInputs) -> Gate:
    spec = g.generation["validation"]["referential_integrity"]
    accounts = set(g.raw["ID_MAP_ACCOUNT"]["account_id"].to_pylist())
    parties = set(g.raw["ID_MAP_PARTY"]["party_id"].to_pylist())
    cbs = set(g.raw["RAW_PARTY_CBS"]["party_id"].to_pylist())
    orphan_accounts = 0
    for table in (g.raw["RAW_TRANSACTIONS"], g.ground_truth["INJECTED_TXN_PENDING"]):
        for col in ("account_id", "counterparty_account_id"):
            orphan_accounts += sum(1 for a in table[col].to_pylist() if a not in accounts)
    orphan_accounts += sum(
        1 for a in g.raw["RAW_ACCOUNTS"]["account_id"].to_pylist() if a not in accounts
    )
    orphan_parties = sum(
        1 for p in g.raw["ID_MAP_ACCOUNT"]["party_id"].to_pylist() if p not in parties
    )
    orphan_parties += sum(1 for p in g.raw["RAW_ACCOUNTS"]["party_id"].to_pylist() if p not in cbs)
    orphan_parties += sum(1 for p in g.raw["RAW_PARTY_KYC"]["party_id"].to_pylist() if p not in cbs)
    edges = g.raw["RAW_PARTY_RELATIONSHIP_ATTR"]
    for col in ("party_a_id", "party_b_id"):
        orphan_parties += sum(1 for p in edges[col].to_pylist() if p not in cbs)
    ok = orphan_accounts <= int(spec["max_orphan_account_refs"]) and orphan_parties <= int(
        spec["max_orphan_party_refs"]
    )
    return _gate(
        g,
        "referential_integrity",
        ok,
        f"{orphan_accounts} account / {orphan_parties} party orphans",
    )


def _internal_accounts(g: GateInputs) -> set[str]:
    """The bank's own accounts (the cash desk): no customer's degree or amounts."""
    bank = str(g.generation["internal_accounts"]["src_bank_id"])
    ids = g.raw["ID_MAP_ACCOUNT"]
    return {
        a
        for a, b in zip(ids["account_id"].to_pylist(), ids["src_bank_id"].to_pylist(), strict=True)
        if b == bank
    }


def benford(g: GateInputs) -> Gate:
    """Leading-digit chi-square of legit customer credit legs' INR amounts. Reported."""
    labels = g.ground_truth["TXN_LABELS"]
    illicit = {
        t
        for t, v in zip(labels["txn_id"].to_pylist(), labels["is_illicit"].to_pylist(), strict=True)
        if v
    }
    internal = _internal_accounts(g)
    tx = g.raw["RAW_TRANSACTIONS"]
    digits: Counter[int] = Counter()
    for txn, side, account, amount in zip(
        tx["txn_id"].to_pylist(),
        tx["leg_side"].to_pylist(),
        tx["account_id"].to_pylist(),
        tx["amount_inr"].to_pylist(),
        strict=True,
    ):
        if side == "C" and txn not in illicit and account not in internal and amount:
            digits[int(str(abs(amount)).lstrip("0.")[:1] or 0)] += 1
    n = sum(digits[d] for d in range(1, 10))
    if not n:
        return Gate("benford", GateStatus.REPORT, False, "no legs")
    chi2 = sum(
        (digits[d] - n * math.log10(1 + 1 / d)) ** 2 / (n * math.log10(1 + 1 / d))
        for d in range(1, 10)
    )
    limit = float(g.generation["validation"]["benford"]["max_chi_square"])
    status = (
        GateStatus.PASS
        if chi2 <= limit
        else (GateStatus.FAIL if _fail_build(g.generation, "benford") else GateStatus.REPORT)
    )
    return Gate(
        "benford",
        status,
        _fail_build(g.generation, "benford"),
        f"chi2 {chi2:.1f}",
        f"n={n}; limit {limit:g}",
    )


def degree_distribution(g: GateInputs) -> Gate:
    """Discrete power-law MLE (xmin = 1) over customer party degree. Reported.

    The cash desk is excluded (D4): it would be one hub joined to every cash
    customer, which is a bookkeeping artefact, not a relationship.
    """
    owner = dict(
        zip(
            g.raw["ID_MAP_ACCOUNT"]["account_id"].to_pylist(),
            g.raw["ID_MAP_ACCOUNT"]["party_id"].to_pylist(),
            strict=True,
        )
    )
    internal = _internal_accounts(g)
    tx = g.raw["RAW_TRANSACTIONS"]
    neighbours: dict[str, set[str]] = defaultdict(set)
    for a, b in zip(
        tx["account_id"].to_pylist(), tx["counterparty_account_id"].to_pylist(), strict=True
    ):
        if a not in internal and b not in internal and owner[a] != owner[b]:
            neighbours[owner[a]].add(owner[b])
    degrees = [len(v) for v in neighbours.values() if v]
    if not degrees:
        return Gate("degree_distribution", GateStatus.REPORT, False, "no edges")
    alpha = 1 + len(degrees) / sum(math.log(d / 0.5) for d in degrees)
    spec = g.generation["validation"]["degree_distribution"]
    ok = float(spec["min_power_law_alpha"]) <= alpha <= float(spec["max_power_law_alpha"])
    fb = _fail_build(g.generation, "degree_distribution")
    status = GateStatus.PASS if ok else (GateStatus.FAIL if fb else GateStatus.REPORT)
    return Gate(
        "degree_distribution",
        status,
        fb,
        f"alpha {alpha:.2f}",
        f"{len(degrees)} parties with a counterparty",
    )


def label_leakage(g: GateInputs) -> Gate:
    """No forbidden column, and no label-bearing string value, in any RAW file."""
    spec = g.generation["validation"]["label_leakage"]
    forbidden = set(spec["forbidden_columns"])
    # Substring, not prefix: "note src:12" leaks as much as "src:12" does.
    tokens = tuple(spec.get("forbidden_value_prefixes") or ()) + tuple(sorted(g.label_values))
    problems: list[str] = []
    for name, table in g.raw.items():
        leaked = sorted(forbidden & set(table.column_names))
        if leaked:
            problems.append(f"{name} has column(s) {leaked}")
        for col in table.column_names:
            if not pa.types.is_string(table.schema.field(col).type):
                continue
            for value in set(table[col].to_pylist()):
                if value and any(token in value for token in tokens):
                    problems.append(f"{name}.{col} carries label value {value[:40]!r}")
                    break
    return _gate(
        g, "label_leakage", not problems, f"{len(problems)} leaks", "; ".join(problems[:5])
    )


def budget(g: GateInputs) -> Gate:
    sub = g.generation["subgraph"]
    parties = g.raw["RAW_PARTY_CBS"].num_rows
    accounts = g.raw["RAW_ACCOUNTS"].num_rows
    legs = g.raw["RAW_TRANSACTIONS"].num_rows + g.ground_truth["INJECTED_TXN_PENDING"].num_rows
    ok = (
        parties <= int(sub["target_parties"])
        and accounts <= int(sub["target_accounts"])
        and legs <= int(sub["max_transactions"])
    )
    return _gate(
        g,
        "budget",
        ok,
        f"{parties} parties / {accounts} accounts / {legs} legs",
        f"limits {sub['target_parties']} / {sub['target_accounts']} / {sub['max_transactions']}",
    )


def signal_yield(g: GateInputs) -> Gate:
    """Construction check only; the real yield is measured after load (D7)."""
    s = g.generation["injection"]["structuring"]
    minimum = int(float(_clause(g.detection, s["min_accounts_clause_id"])["threshold_lower"]))
    lo, hi = (Decimal(str(v)) for v in s["amount_band_pct_of_ctr"])
    pending = g.ground_truth["INJECTED_TXN_PENDING"]
    credited: dict[str, set[str]] = defaultdict(set)
    for acct, side, ratio, ts in zip(
        pending["account_id"].to_pylist(),
        pending["leg_side"].to_pylist(),
        pending["amount_ctr_ratio"].to_pylist(),
        pending["txn_timestamp"].to_pylist(),
        strict=True,
    ):
        if side == "C" and lo <= ratio <= hi:
            credited[acct].add(ts.date().isoformat())
    short = []
    for plan in g.hosts:
        in_window = [
            a
            for a in plan.accounts
            if any(
                plan.window_start.isoformat() <= d <= plan.window_end.isoformat()
                for d in credited.get(a, ())
            )
        ]
        if len(in_window) < minimum:
            short.append(plan.party_id)
    fb = _fail_build(g.generation, "signal_yield")
    if short:
        return Gate(
            "signal_yield",
            GateStatus.FAIL,
            fb,
            f"{len(short)} hosts under-constructed",
            ", ".join(short[:5]),
        )
    return Gate(
        "signal_yield",
        GateStatus.DEFERRED,
        fb,
        f"{len(g.hosts)} hosts constructed",
        f"each has >= {minimum} accounts with an in-band deposit in its window; "
        "detections are measured after load",
    )


def demo_entity(g: GateInputs) -> Gate:
    ok = g.demo_entity_error is None
    return _gate(g, "demo_entity", ok, "pinned" if ok else "not found", g.demo_entity_error or "")


GATES = (
    illicit_rate,
    bank_id_resolution,
    label_attribution,
    id_map_one_to_one,
    injection_window,
    injection_band,
    income_calibration,
    money_conservation,
    referential_integrity,
    benford,
    degree_distribution,
    label_leakage,
    budget,
    signal_yield,
    demo_entity,
)


def run_gates(g: GateInputs) -> list[Gate]:
    return [gate(g) for gate in GATES]
