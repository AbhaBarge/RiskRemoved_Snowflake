"""Reskin sampled AMLworld pairs into Indian-banking legs -- GAP-03 / GAP-06.

One AMLworld pair becomes two legs. They share a pair reference, and each leg
keeps its own side's amount and currency (``legs.use_side_specific_amounts``):

* **debit** on the payer: ``-amount_paid`` in ``payment_currency``
* **credit** on the payee: ``+amount_received`` in ``receiving_currency``

So money is conserved per currency, and a cross-currency pair keeps both of its
real amounts.

Conversion is :class:`~decimal.Decimal` end to end. Rates come from
``fx.rates_to_inr``. A currency with no rate or ISO code, or a payment format
with no channel when ``channel_default`` is null, fails rather than being
guessed. ``retain_foreign_pct`` of pairs keep their source currency, chosen per
pair from a hash of its content, so the choice does not move with row order;
``amount_inr`` is always populated.

Nothing here assigns the final ``txn_pair_id`` / ``txn_id``. Ids are allocated
once, over source and injected pairs together, by :func:`assign_pair_ids`.
Numbering source pairs first would make every injected pair recognisable by the
tail of the id sequence.
"""

from __future__ import annotations

import decimal
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pyarrow as pa

from risk_copilot.generation import GenerationError
from risk_copilot.generation.graph import Subgraph, pair_content_key
from risk_copilot.generation.source import SRC_ROW_NUMBER
from risk_copilot.ids import derived_rng, stable_hash, txn_leg_id, txn_pair_id

#: Leg-level money type, matching ``RAW_TRANSACTIONS.amount`` / ``amount_inr``.
MONEY = pa.decimal128(20, 2)

#: Columns of a reskinned leg. ``pair_ref`` and ``order_key`` are internal: the
#: writer replaces ``pair_ref`` with ids and never writes ``order_key``.
LEG_SCHEMA = pa.schema(
    [
        ("pair_ref", pa.string()),
        ("order_key", pa.string()),
        (SRC_ROW_NUMBER, pa.int64()),
        ("leg_side", pa.string()),
        ("account_id", pa.string()),
        ("counterparty_account_id", pa.string()),
        ("txn_timestamp", pa.timestamp("s")),
        ("amount", MONEY),
        ("currency", pa.string()),
        ("amount_inr", MONEY),
        ("txn_type", pa.string()),
        ("channel", pa.string()),
        ("reference_text", pa.string()),
    ]
)


class ReskinError(GenerationError):
    """A pair cannot be reskinned under the configured maps."""


def source_pair_ref(src_row_number: int) -> str:
    """Internal pair reference of a ledger pair. Never written to a RAW file."""
    return f"src:{src_row_number}"


@dataclass(frozen=True)
class ReskinConfig:
    seed: int
    base_currency: str
    rates: Mapping[str, Decimal]
    iso_codes: Mapping[str, str]
    retain_foreign_pct: Decimal
    quantum: Decimal
    rounding: str
    channel_map: Mapping[str, str]
    channel_default: str | None
    txn_type_default: Mapping[str, str]
    txn_type_by_format: Mapping[str, Mapping[str, str]]
    debit_sign: int
    credit_sign: int
    source_ref_prefix: str
    source_ref_hex_digits: int

    @classmethod
    def from_config(cls, generation: Mapping[str, Any]) -> ReskinConfig:
        try:
            fx = generation["fx"]
            legs = generation["legs"]
            types = generation["txn_types"]
            if legs["use_side_specific_amounts"] is not True:
                raise ReskinError("legs.use_side_specific_amounts: only true is implemented")
            rounding = str(fx["rounding_mode"])
            if not rounding.startswith("ROUND_") or not hasattr(decimal, rounding):
                raise ReskinError(f"fx.rounding_mode {rounding!r} is not a decimal rounding mode")
            if int(legs["debit_sign"]) >= 0 or int(legs["credit_sign"]) <= 0:
                raise ReskinError("legs: debit_sign must be negative and credit_sign positive")
            return cls(
                seed=int(generation["seed"]),
                base_currency=str(fx["base_currency"]),
                # str() first: a YAML float must not become its binary expansion.
                rates={k: Decimal(str(v)) for k, v in fx["rates_to_inr"].items()},
                iso_codes={k: str(v) for k, v in fx["iso_codes"].items()},
                retain_foreign_pct=Decimal(str(fx["retain_foreign_pct"])),
                quantum=Decimal(1).scaleb(-int(fx["rounding_decimals"])),
                rounding=rounding,
                channel_map={k: str(v) for k, v in generation["channel_map"].items()},
                channel_default=generation.get("channel_default"),
                txn_type_default=dict(types["default"]),
                txn_type_by_format={k: dict(v) for k, v in (types.get("by_format") or {}).items()},
                debit_sign=int(legs["debit_sign"]),
                credit_sign=int(legs["credit_sign"]),
                source_ref_prefix=str(legs["source_ref_prefix"]),
                source_ref_hex_digits=int(legs["source_ref_hex_digits"]),
            )
        except KeyError as exc:
            raise ReskinError(f"conf/generation.yml: missing reskin setting {exc}") from exc

    def rate(self, currency: str) -> Decimal:
        if currency not in self.rates:
            raise ReskinError(f"No fx.rates_to_inr entry for currency {currency!r}")
        return self.rates[currency]

    def iso(self, currency: str) -> str:
        if currency not in self.iso_codes:
            raise ReskinError(f"No fx.iso_codes entry for currency {currency!r}")
        return self.iso_codes[currency]

    def channel(self, payment_format: str) -> str:
        if payment_format in self.channel_map:
            return self.channel_map[payment_format]
        if self.channel_default is None:
            raise ReskinError(f"Payment format {payment_format!r} has no channel_map entry")
        return str(self.channel_default)

    def txn_type(self, payment_format: str, side: str) -> str:
        spec = self.txn_type_by_format.get(payment_format, self.txn_type_default)
        return str(spec[side])

    def money(self, value: Decimal) -> Decimal:
        return value.quantize(self.quantum, rounding=self.rounding)

    def exact_money(self, value: Decimal, what: str) -> Decimal:
        """``value`` at 2 dp, failing if that would round (M14: never, post-exclusion)."""
        quantised = value.quantize(self.quantum)
        if quantised != value:
            raise ReskinError(
                f"{what} {value} has more places than rounding_decimals; only excluded "
                "currencies (Bitcoin) carry more"
            )
        return quantised

    def retains_foreign(self, content_key: str) -> bool:
        rng = derived_rng(content_key, seed=self.seed, namespace="fx-retain")
        return Decimal(str(rng.random())) * 100 < self.retain_foreign_pct


def reskin_pairs(subgraph: Subgraph, generation: Mapping[str, Any]) -> pa.Table:
    """Two legs per sampled pair, in :data:`LEG_SCHEMA`, sorted by ``order_key``."""
    cfg = ReskinConfig.from_config(generation)
    ids = subgraph.account_ids
    records: list[dict[str, Any]] = []
    for row in subgraph.pairs.to_pylist():
        content = pair_content_key(row)
        foreign = cfg.retains_foreign(content)
        fmt = row["payment_format"]
        channel = cfg.channel(fmt)
        payer = ids[(row["from_bank"], row["from_account"])]
        payee = ids[(row["to_bank"], row["to_account"])]
        ref = source_pair_ref(row[SRC_ROW_NUMBER])
        sides = (
            (
                "D",
                payer,
                payee,
                row["amount_paid"],
                row["payment_currency"],
                cfg.debit_sign,
                "debit",
            ),
            (
                "C",
                payee,
                payer,
                row["amount_received"],
                row["receiving_currency"],
                cfg.credit_sign,
                "credit",
            ),
        )
        for side, account, counterparty, amount, currency, sign, role in sides:
            inr = cfg.money(amount * cfg.rate(currency))
            if foreign:
                leg_currency = cfg.iso(currency)
                leg_amount = cfg.exact_money(amount, f"{ref} {role} amount")
            else:
                leg_currency, leg_amount = cfg.base_currency, inr
            records.append(
                {
                    "pair_ref": ref,
                    "order_key": content,
                    SRC_ROW_NUMBER: row[SRC_ROW_NUMBER],
                    "leg_side": side,
                    "account_id": account,
                    "counterparty_account_id": counterparty,
                    "txn_timestamp": row["txn_timestamp"],
                    "amount": sign * leg_amount,
                    "currency": leg_currency,
                    "amount_inr": sign * inr,
                    "txn_type": cfg.txn_type(fmt, role),
                    "channel": channel,
                    "reference_text": None,
                }
            )
    records.sort(key=lambda r: (r["order_key"], r[SRC_ROW_NUMBER], r["leg_side"]))
    return pa.Table.from_pylist(records, schema=LEG_SCHEMA)


# --------------------------------------------------------------------------- #
# Identifier allocation (run once, over source + injected pairs)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PairIds:
    """Final identifiers for one pair."""

    txn_pair_id: str
    source_ref: str

    def leg_id(self, side: str) -> str:
        return txn_leg_id(self.txn_pair_id, side)


def assign_pair_ids(
    pairs: Iterable[tuple[str, str]], generation: Mapping[str, Any]
) -> dict[str, PairIds]:
    """Allocate ``txn_pair_id`` and ``source_ref`` for every pair at once.

    Args:
        pairs: ``(pair_ref, order_key)`` for every pair, source and injected.

    Pairs are numbered in ``stable_hash(order_key)`` order, so injected pairs
    interleave with source pairs instead of trailing them, and the numbering is
    collision-free. ``source_ref`` is an opaque token derived from the pair id,
    identical in format for both populations.
    """
    cfg = ReskinConfig.from_config(generation)
    ordered = sorted(
        {(stable_hash(key, cfg.seed, namespace="pair-order"), key, ref) for ref, key in pairs}
    )
    refs = [ref for _, _, ref in ordered]
    if len(set(refs)) != len(refs):
        raise ReskinError("A pair_ref was supplied with two different order keys")
    result: dict[str, PairIds] = {}
    for sequence, ref in enumerate(refs, start=1):
        pair_id = txn_pair_id(sequence)
        token = stable_hash(pair_id, cfg.seed, namespace="source-ref")
        digits = f"{token:016x}"[: cfg.source_ref_hex_digits].upper()
        result[ref] = PairIds(txn_pair_id=pair_id, source_ref=f"{cfg.source_ref_prefix}{digits}")
    return result
